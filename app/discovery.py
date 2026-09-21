from __future__ import annotations

import concurrent.futures
import json
import subprocess
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import re

from yt_dlp import YoutubeDL

from app.youtube import ydl_flat_options

METADATA_WORKERS = 8
SEARCH_METADATA_CANDIDATE_CAP = 40
SEARCH_METADATA_TIME_BUDGET_SECONDS = 15
SEARCH_METADATA_ITEM_TIMEOUT_SECONDS = 5


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
        raise ValueError(f"Unsupported channel filter: {filter_type!r}")

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
