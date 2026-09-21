from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, TypeVar
from urllib.parse import parse_qs, urlparse
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

import re

from yt_dlp import YoutubeDL

from app.models import update_job

YOUTUBE_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
# Hardcoded on purpose: this is a fixed allowlist, not a setting. See D-0006 / D-0008.
GOLDCAST_ON_DEMAND_HOST = "anthropic.ondemand.goldcast.io"
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
