from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, TypeVar
from urllib.parse import parse_qs, urlparse
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

import re

from fastapi import HTTPException
from yt_dlp import YoutubeDL

from app.models import update_job

YOUTUBE_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
GOLDCAST_ON_DEMAND_HOST = "anthropic.ondemand.goldcast.io"
CLAUDE_ACADEMY_WEBINARS_URL = "https://academy.claude.com/assets/data/webinars-latest.json"
METADATA_WORKERS = 8
SEARCH_METADATA_CANDIDATE_CAP = 40
SEARCH_METADATA_TIME_BUDGET_SECONDS = 15
SEARCH_METADATA_ITEM_TIMEOUT_SECONDS = 5
YOUTUBE_OPERATION_ATTEMPTS = 3
YoutubeOperationResult = TypeVar("YoutubeOperationResult")


def is_youtube_host(hostname: Optional[str]) -> bool:
    if not hostname:
        return False
    host = hostname.lower().rstrip(".")
    return host in YOUTUBE_SHORT_HOSTS or host == "youtube.com" or host.endswith(".youtube.com")


def youtube_video_id(url: str) -> Optional[str]:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not is_youtube_host(parsed.hostname):
        return None

    candidate = ""
    if parsed.hostname and parsed.hostname.lower().rstrip(".") in YOUTUBE_SHORT_HOSTS:
        candidate = parsed.path.strip("/").split("/", 1)[0]
    elif parsed.path in {"/watch", "/"}:
        candidate = (parse_qs(parsed.query).get("v") or [""])[0]
    else:
        match = re.match(r"/(?:shorts|embed|live)/([A-Za-z0-9_-]+)", parsed.path)
        candidate = match.group(1) if match else ""

    return candidate if re.fullmatch(r"[A-Za-z0-9_-]{6,}", candidate) else None


def goldcast_webinar_id(url: str) -> Optional[str]:
    """Accept only Anthropic's public on-demand Goldcast recordings."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower().rstrip(".") != GOLDCAST_ON_DEMAND_HOST:
        return None
    match = re.fullmatch(r"/on-demand/([0-9a-fA-F-]{36})/?", parsed.path)
    return match.group(1).lower() if match else None


def media_id(url: str) -> Optional[str]:
    youtube_id = youtube_video_id(url)
    if youtube_id:
        return youtube_id
    goldcast_id = goldcast_webinar_id(url)
    return f"goldcast:{goldcast_id}" if goldcast_id else None


def media_platform(url: str) -> str:
    return "YouTube" if youtube_video_id(url) else "Goldcast"


def parse_urls(raw_text: str) -> List[str]:
    raw_text = raw_text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", " ")
    candidates = re.findall(r"https?://[^\s,;]+", raw_text)
    urls = []
    seen = set()
    for candidate in candidates:
        cleaned = candidate.strip().rstrip(").]")
        identifier = media_id(cleaned)
        if not identifier:
            continue
        if identifier not in seen:
            seen.add(identifier)
            urls.append(cleaned)
    return urls


def parse_claude_academy_webinars(payload: Any) -> List[Dict[str, str]]:
    """Return only public, on-demand webinar recordings supported by this app."""
    records = payload.get("webinars") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise ValueError("Claude Academy returned an invalid webinar catalog.")

    webinars: List[Dict[str, str]] = []
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        recording = record.get("recording")
        if not isinstance(recording, dict):
            continue
        url = recording.get("url")
        identifier = media_id(url) if isinstance(url, str) else None
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        webinars.append(
            {
                "title": record.get("title") if isinstance(record.get("title"), str) else "Untitled webinar",
                "url": url,
                "platform": media_platform(url),
                "id": identifier,
            }
        )
    return webinars


def fetch_claude_academy_webinars() -> List[Dict[str, str]]:
    request = UrlRequest(CLAUDE_ACADEMY_WEBINARS_URL, headers={"User-Agent": "YouTube-Transcriber/1.0"})
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return parse_claude_academy_webinars(payload)


def ydl_base_options() -> Dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 15,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 2,
        "extractor_args": {"youtube": {"player_client": ["android", "ios", "tv"]}},
    }


def is_transient_youtube_error(exc: Exception) -> bool:
    if isinstance(exc, (BrokenPipeError, ConnectionError, TimeoutError)):
        return True
    if isinstance(exc, OSError) and exc.errno in {32, 54, 60, 61, 104, 110}:
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "broken pipe",
            "connection reset",
            "connection aborted",
            "connection refused",
            "remote end closed",
            "timed out",
            "temporarily unavailable",
        )
    )


def run_youtube_operation(
    operation: Callable[[], YoutubeOperationResult],
    *,
    on_retry: Optional[Callable[[int, int, Exception], None]] = None,
) -> YoutubeOperationResult:
    for attempt in range(1, YOUTUBE_OPERATION_ATTEMPTS + 1):
        try:
            return operation()
        except Exception as exc:
            if attempt >= YOUTUBE_OPERATION_ATTEMPTS or not is_transient_youtube_error(exc):
                raise
            if on_retry:
                on_retry(attempt, YOUTUBE_OPERATION_ATTEMPTS, exc)
            time.sleep(attempt)
    raise RuntimeError("The YouTube operation could not be completed.")


def extract_video_info(job: Any) -> Dict[str, Any]:
    def extract() -> Dict[str, Any]:
        with YoutubeDL(ydl_base_options()) as ydl:
            return ydl.extract_info(job.url, download=False)

    return run_youtube_operation(
        extract,
        on_retry=lambda attempt, total, _exc: update_job(
            job.id,
            progress=10,
            message=f"{media_platform(job.url)} interrupted the connection; retrying ({attempt + 1} of {total})",
        ),
    )


def ydl_flat_options(limit: Optional[int] = None) -> Dict[str, Any]:
    options = ydl_base_options()
    options.update(
        {
            "extract_flat": "in_playlist",
            "playlistend": limit,
        }
    )
    options.pop("noplaylist", None)
    return options


def search_extract_info(search_term: str, fetch_limit: int) -> Optional[Dict[str, Any]]:
    with YoutubeDL(ydl_flat_options(fetch_limit)) as ydl:
        return ydl.extract_info(search_term, download=False)


def normalize_youtube_url(entry: Dict[str, Any]) -> str:
    url = entry.get("url") or entry.get("webpage_url") or entry.get("id") or ""
    if url.startswith("http"):
        return url
    return f"https://www.youtube.com/watch?v={url}"


def is_confirmed_short(*urls: Any) -> bool:
    for value in urls:
        if not isinstance(value, str):
            continue
        parsed = urlparse(value)
        if re.match(r"^/shorts/[A-Za-z0-9_-]+", parsed.path):
            return True
    return False


def video_metadata_fields(info: Dict[str, Any], fallback_title: str) -> Dict[str, Any]:
    return {
        "title": info.get("title") or fallback_title,
        "duration": info.get("duration"),
        "upload_date": info.get("upload_date"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "height": info.get("height"),
        "live_status": info.get("live_status"),
        "has_subtitles": bool(info.get("subtitles") or info.get("automatic_captions")),
        "is_short": is_confirmed_short(info.get("webpage_url"), info.get("original_url"), info.get("url")),
        "metadata_available": True,
    }


def video_record(entry: Dict[str, Any], position: int) -> Dict[str, Any]:
    return {
        "position": position,
        "id": entry.get("id"),
        "title": entry.get("title") or "YouTube video",
        "url": normalize_youtube_url(entry),
        "view_count": entry.get("view_count"),
        "like_count": entry.get("like_count"),
        "duration": entry.get("duration"),
        "upload_date": entry.get("upload_date"),
        "height": entry.get("height"),
        "live_status": entry.get("live_status"),
        "has_subtitles": bool(entry.get("subtitles") or entry.get("automatic_captions")),
        "is_short": is_confirmed_short(entry.get("webpage_url"), entry.get("original_url"), entry.get("url")),
        "metadata_available": False,
    }


def extract_channel_videos(channel_url: str, filter_type: str, limit: int) -> List[Dict[str, Any]]:
    if not channel_url.rstrip("/").endswith("/videos"):
        channel_url = channel_url.rstrip("/") + "/videos"

    needs_full_metadata = filter_type in {"most_viewed", "most_liked"}
    flat_limit = limit if filter_type == "newest" and limit > 0 else None
    with YoutubeDL(ydl_flat_options(flat_limit)) as ydl:
        info = ydl.extract_info(channel_url, download=False)

    entries = [entry for entry in info.get("entries", []) if entry and entry.get("id")]
    newest_first = [video_record(entry, index + 1) for index, entry in enumerate(entries)]

    if filter_type == "newest":
        selected = newest_first[:limit]
    elif filter_type == "oldest":
        selected = list(reversed(newest_first))[:limit]
    elif filter_type == "all":
        selected = list(reversed(newest_first))
    elif needs_full_metadata:
        selected = enrich_and_sort_videos(newest_first, filter_type, limit)
    else:
        raise HTTPException(status_code=400, detail="Filtro no valido.")

    return [{**video, "position": index + 1} for index, video in enumerate(selected)]


def fetch_video_metadata_with_timeout(
    url: str,
    timeout: int = SEARCH_METADATA_ITEM_TIMEOUT_SECONDS,
) -> Optional[Dict[str, Any]]:
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        "--no-playlist",
        "--socket-timeout",
        "4",
        "--retries",
        "1",
        "--extractor-retries",
        "1",
        "--extractor-args",
        "youtube:player_client=android,ios,tv",
        url,
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None


def enrich_and_sort_videos(videos: List[Dict[str, Any]], filter_type: str, limit: int) -> List[Dict[str, Any]]:
    scan_limit = len(videos) if limit <= 0 else min(len(videos), max(limit * 10, 40))
    enriched, _ = enrich_search_videos(
        videos,
        candidate_limit=scan_limit,
        time_budget_seconds=SEARCH_METADATA_TIME_BUDGET_SECONDS,
        return_stats=True,
    )

    metric = "view_count" if filter_type == "most_viewed" else "like_count"
    ranked = [video for video in enriched if video.get("metadata_available") and video.get(metric) is not None]
    ranked.sort(key=lambda item: item[metric], reverse=True)
    return ranked[:limit]


def enrich_search_videos(
    videos: List[Dict[str, Any]],
    *,
    candidate_limit: Optional[int] = None,
    time_budget_seconds: Optional[float] = None,
    return_stats: bool = False,
) -> Any:
    """Fill video metadata without letting one slow YouTube response block a search."""
    enriched = [dict(video) for video in videos]
    scan_count = len(enriched) if candidate_limit is None else min(len(enriched), max(candidate_limit, 0))
    scan = enriched[:scan_count]
    stats = {
        "attempted": len(scan),
        "completed": 0,
        "verified": 0,
        "failed": 0,
        "pending": 0,
        "skipped": len(enriched) - len(scan),
        "time_budget_seconds": time_budget_seconds,
    }
    if not scan:
        return (enriched, stats) if return_stats else enriched

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=METADATA_WORKERS)
    futures = {executor.submit(fetch_video_metadata_with_timeout, video["url"]): index for index, video in enumerate(scan)}
    done, pending = concurrent.futures.wait(futures, timeout=time_budget_seconds)
    stats["completed"] = len(done)
    stats["pending"] = len(pending)

    for future in done:
        video = scan[futures[future]]
        try:
            info = future.result()
        except Exception:
            info = None
        if info:
            details = video_metadata_fields(info, video["title"])
            details["is_short"] = video.get("is_short", False) or details["is_short"]
            video.update(details)
            stats["verified"] += 1
        else:
            stats["failed"] += 1

    for future in pending:
        future.cancel()
    executor.shutdown(wait=not pending, cancel_futures=bool(pending))
    return (enriched, stats) if return_stats else enriched


def search_video_matches(
    video: Dict[str, Any],
    video_type: str,
    duration_filter: str,
    upload_filter: str,
    feature: str,
    now: Optional[datetime] = None,
) -> bool:
    duration = video.get("duration")
    is_short = bool(video.get("is_short"))
    if video_type == "videos" and is_short:
        return False
    if video_type == "shorts" and not is_short:
        return False

    if duration_filter != "any" and duration is None:
        return False
    if duration_filter == "short" and duration >= 180:
        return False
    if duration_filter == "medium" and not 180 <= duration <= 1200:
        return False
    if duration_filter == "long" and duration <= 1200:
        return False

    if upload_filter != "any":
        upload_date = video.get("upload_date") or ""
        try:
            uploaded = datetime.strptime(upload_date, "%Y%m%d")
        except ValueError:
            return False
        today = now or datetime.now()
        if upload_filter == "today" and uploaded.date() != today.date():
            return False
        week_start = (today - timedelta(days=today.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        year_start = today.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        if upload_filter == "week" and uploaded < week_start:
            return False
        if upload_filter == "month" and uploaded < month_start:
            return False
        if upload_filter == "year" and uploaded < year_start:
            return False

    if feature == "live" and video.get("live_status") not in {"is_live", "was_live", "post_live"}:
        return False
    if feature == "hd" and (video.get("height") or 0) < 720:
        return False
    if feature == "4k" and (video.get("height") or 0) < 2160:
        return False
    if feature == "subtitles" and not video.get("has_subtitles"):
        return False
    return True


def pick_caption_track(info: Dict[str, Any], language: str) -> Optional[Dict[str, str]]:
    language_candidates = []
    if language == "auto":
        detected_language = info.get("language")
        if detected_language:
            language_candidates.append(detected_language)
    else:
        language_candidates.append(language)
    if "-" in language:
        language_candidates.append(language.split("-")[0])
    language_candidates.extend(["en", "es"])
    language_candidates = list(dict.fromkeys(language_candidates))

    for field_name, source_name in (("subtitles", "manual captions"), ("automatic_captions", "automatic captions")):
        tracks = info.get(field_name) or {}
        for lang in language_candidates:
            if lang not in tracks:
                continue
            formats = tracks[lang]
            selected = next((item for item in formats if item.get("ext") == "vtt"), None)
            if selected:
                return {"url": selected["url"], "language": lang, "source": source_name}
    return None


def download_audio_url(url: str, output_template: str, progress_hooks: List[Callable[[Dict[str, Any]], Any]]) -> None:
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio[ext=m4a]/bestaudio/best[acodec!=none]/best",
        "outtmpl": output_template,
        "socket_timeout": 15,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 2,
        "progress_hooks": progress_hooks,
        "extractor_args": {"youtube": {"player_client": ["android", "ios", "tv"]}},
    }
    with YoutubeDL(options) as ydl:
        ydl.download([url])


def download_caption_file(url: str, destination: Any) -> None:
    def download() -> None:
        request = UrlRequest(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=30) as response:
            content = response.read()
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            temporary.write_bytes(content)
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()

    run_youtube_operation(download)
