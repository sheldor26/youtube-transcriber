from __future__ import annotations

import html
import csv
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from collections import Counter
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar
from urllib.parse import parse_qs, urlparse
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

import webvtt
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from yt_dlp import YoutubeDL

from app.library import LibraryError, LibraryStore


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
JOBS_DIR = DATA_DIR / "jobs"
DOWNLOADS_DIR = DATA_DIR / "downloads"

JOBS_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
library = LibraryStore(DATA_DIR / "library.sqlite3")

app = FastAPI(title="YouTube Transcriber")
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass
class Job:
    id: str
    url: str
    language: str
    model_size: str
    prefer_captions: bool
    output_dir: str
    save_srt: bool
    filename_prefix: Optional[str] = None
    skip_existing: bool = False
    status: str = "queued"
    progress: int = 0
    message: str = "Esperando turno"
    title: Optional[str] = None
    source: Optional[str] = None
    duration: Optional[float] = None
    output_basename: Optional[str] = None
    transcript_path: Optional[str] = None
    srt_path: Optional[str] = None
    index_on_completion: bool = True
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)


@dataclass
class Batch:
    id: str
    urls: List[str]
    language: str
    model_size: str
    prefer_captions: bool
    output_dir: str
    save_srt: bool
    project_id: Optional[str] = None
    start_number: Optional[int] = None
    skip_existing: bool = True
    generate_knowledge_base: bool = False
    generate_consolidated_summary: bool = False
    summary_max_words: int = 3500
    status: str = "queued"
    progress: int = 0
    message: str = "Esperando turno"
    total: int = 0
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    current_job_id: Optional[str] = None
    results: List[Dict[str, Any]] = field(default_factory=list)
    index_path: Optional[str] = None
    global_index_path: Optional[str] = None
    knowledge_base_path: Optional[str] = None
    consolidated_summary_path: Optional[str] = None
    state_path: Optional[str] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)


jobs: Dict[str, Job] = {}
batches: Dict[str, Batch] = {}
jobs_lock = threading.Lock()
batch_lock = threading.Lock()
batch_workers: set[str] = set()
batch_workers_lock = threading.Lock()
state_write_lock = threading.Lock()
index_write_lock = threading.Lock()
whisper_models: Dict[str, Any] = {}
whisper_models_lock = threading.Lock()

ALLOWED_MODELS = {"tiny", "base", "small", "medium"}
ALLOWED_LANGUAGES = {"auto", "es", "en", "pt", "fr", "de"}
YOUTUBE_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
METADATA_WORKERS = 8
SEARCH_METADATA_CANDIDATE_CAP = 40
SEARCH_METADATA_TIME_BUDGET_SECONDS = 15
SEARCH_METADATA_ITEM_TIMEOUT_SECONDS = 5
YOUTUBE_OPERATION_ATTEMPTS = 3
YoutubeOperationResult = TypeVar("YoutubeOperationResult")


def update_job(job_id: str, **changes: Any) -> None:
    with jobs_lock:
        job = jobs[job_id]
        for key, value in changes.items():
            setattr(job, key, value)
        snapshot = public_job(job)
    persist_job_snapshot(job_id, snapshot)


def public_job(job: Job) -> Dict[str, Any]:
    data = asdict(job)
    data["created_at"] = int(job.created_at)
    return data


def update_batch(batch_id: str, **changes: Any) -> None:
    with batch_lock:
        batch = batches[batch_id]
        for key, value in changes.items():
            setattr(batch, key, value)


def public_batch(batch: Batch) -> Dict[str, Any]:
    data = asdict(batch)
    data["created_at"] = int(batch.created_at)
    return data


def clean_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def default_output_dir() -> str:
    return str(Path.home() / "Downloads" / "Transcripciones YouTube")


def atomic_write_text(path: Path, content: str) -> None:
    """Replace a text file only after its complete content reached disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def persist_job_snapshot(job_id: str, snapshot: Dict[str, Any]) -> None:
    job_path = JOBS_DIR / job_id / "job.json"
    with state_write_lock:
        atomic_write_text(job_path, json.dumps(snapshot, ensure_ascii=False, indent=2))


def form_checkbox_enabled(value: Optional[str]) -> bool:
    return value in {"on", "true", "1", "yes"}


def is_youtube_host(hostname: Optional[str]) -> bool:
    if not hostname:
        return False
    host = hostname.lower().rstrip(".")
    return host in YOUTUBE_SHORT_HOSTS or host == "youtube.com" or host.endswith(".youtube.com")


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\n\r\t]+', " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:150] or "youtube-transcript"


def topic_output_dir(root_dir: str, topic_name: str) -> Path:
    """Keep each topic in a single safe folder below the user-selected root."""
    root = destination_dir_for(root_dir.strip() or default_output_dir())
    return root / sanitize_filename(topic_name.strip())


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"No pude generar un nombre disponible para {path.name}.")


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


def destination_dir_for(output_dir: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(output_dir))).resolve()


def find_indexed_transcript(output_dir: str, video_id: Optional[str]) -> Optional[Path]:
    if not video_id:
        return None

    destination_dir = destination_dir_for(output_dir)
    index_files = [destination_dir / "transcription-index.csv", *destination_dir.glob("batch-index-*.csv")]
    for index_file in index_files:
        if not index_file.exists():
            continue
        try:
            with index_file.open("r", encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    if row.get("video_id") != video_id:
                        continue
                    transcript = row.get("transcript_path") or ""
                    if transcript and Path(transcript).exists():
                        return Path(transcript)
        except Exception:
            continue
    return None


def find_existing_transcript(job: Job, basename: str, info: Dict[str, Any]) -> Optional[Path]:
    video_id = info.get("id") or youtube_video_id(job.url)
    indexed = find_indexed_transcript(job.output_dir, video_id)
    if indexed:
        return indexed

    # A title is not a stable identity. Preserve legacy title-only detection only
    # when the URL itself does not expose a usable YouTube video ID.
    if not video_id:
        expected = destination_dir_for(job.output_dir) / f"{basename}.txt"
        if expected.exists():
            return expected
    return None


def append_csv_row(path: Path, fieldnames: List[str], row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with index_write_lock:
        should_write_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            if should_write_header:
                writer.writeheader()
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def append_batch_indexes(batch: Batch, job: Job, order: int) -> None:
    destination_dir = destination_dir_for(batch.output_dir)
    row = {
        "batch_id": batch.id,
        "order": order,
        "video_id": youtube_video_id(job.url),
        "title": job.title or "",
        "url": job.url,
        "status": job.status,
        "source": job.source or "",
        "transcript_path": job.transcript_path or "",
        "srt_path": job.srt_path or "",
        "error": job.error or "",
        "created_at": int(time.time()),
    }
    fields = list(row.keys())
    append_csv_row(destination_dir / f"batch-index-{batch.id}.csv", fields, row)
    append_csv_row(destination_dir / "transcription-index.csv", fields, row)


def append_single_job_index(job: Job) -> None:
    destination_dir = destination_dir_for(job.output_dir)
    row = {
        "batch_id": "",
        "order": "",
        "video_id": youtube_video_id(job.url),
        "title": job.title or "",
        "url": job.url,
        "status": job.status,
        "source": job.source or "",
        "transcript_path": job.transcript_path or "",
        "srt_path": job.srt_path or "",
        "error": job.error or "",
        "created_at": int(time.time()),
    }
    append_csv_row(destination_dir / "transcription-index.csv", list(row.keys()), row)


def persist_batch_state(batch: Batch) -> None:
    with batch_lock:
        snapshot = public_batch(batch)
    state_path = Path(snapshot["state_path"]) if snapshot.get("state_path") else destination_dir_for(snapshot["output_dir"]) / f"batch-state-{snapshot['id']}.json"
    with state_write_lock:
        atomic_write_text(state_path, json.dumps(snapshot, ensure_ascii=False, indent=2))


def seconds_to_srt_time(value: float) -> str:
    milliseconds = int(round(value * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def vtt_time_to_seconds(value: str) -> float:
    parts = value.split(":")
    seconds = float(parts[-1])
    minutes = int(parts[-2]) if len(parts) >= 2 else 0
    hours = int(parts[-3]) if len(parts) >= 3 else 0
    return hours * 3600 + minutes * 60 + seconds


def write_outputs(job_dir: Path, segments: List[TranscriptSegment], basename: str, save_srt: bool) -> Dict[str, Path]:
    text_path = job_dir / f"{basename}.txt"

    transcript_text = " ".join(segment.text for segment in segments if segment.text)
    paragraphs = textwrap.wrap(transcript_text, width=100)
    atomic_write_text(text_path, "\n".join(paragraphs) + "\n")

    outputs = {"txt": text_path}
    if save_srt:
        srt_path = job_dir / f"{basename}.srt"
        srt_blocks = []
        for index, segment in enumerate(segments, start=1):
            srt_blocks.append(
                "\n".join(
                    [
                        str(index),
                        f"{seconds_to_srt_time(segment.start)} --> {seconds_to_srt_time(segment.end)}",
                        segment.text,
                    ]
                )
            )
        atomic_write_text(srt_path, "\n\n".join(srt_blocks) + "\n")
        outputs["srt"] = srt_path

    return outputs


def copy_outputs_to_destination(job: Job, outputs: Dict[str, Path]) -> Dict[str, Path]:
    destination_dir = destination_dir_for(job.output_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)

    copied_txt = unique_path(destination_dir / outputs["txt"].name)
    shutil.copy2(outputs["txt"], copied_txt)
    copied = {"txt": copied_txt}

    if "srt" in outputs:
        copied_srt = unique_path(destination_dir / outputs["srt"].name)
        shutil.copy2(outputs["srt"], copied_srt)
        copied["srt"] = copied_srt

    return copied


def normalize_caption_word(word: str) -> str:
    return re.sub(r"(^[^\w]+|[^\w]+$)", "", word.lower())


def find_word_overlap(previous_words: List[str], current_words: List[str]) -> int:
    previous_normalized = [normalize_caption_word(word) for word in previous_words]
    current_normalized = [normalize_caption_word(word) for word in current_words]
    max_size = min(len(previous_normalized), len(current_normalized))

    for size in range(max_size, 0, -1):
        if previous_normalized[-size:] == current_normalized[:size]:
            return size
    return 0


def remove_rolling_caption_repeats(segments: List[TranscriptSegment]) -> List[TranscriptSegment]:
    cleaned_segments: List[TranscriptSegment] = []
    # A rolling YouTube caption can only overlap the preceding caption window.
    # Keeping the full transcript here makes long podcast captions quadratic.
    max_segment_words = max((len(segment.text.split()) for segment in segments), default=0)
    emitted_tail: List[str] = []

    for segment in segments:
        words = segment.text.split()
        if not words:
            continue

        overlap = find_word_overlap(emitted_tail[-len(words):], words) if emitted_tail else 0
        new_words = words[overlap:]
        if not new_words:
            continue

        emitted_tail.extend(new_words)
        if len(emitted_tail) > max_segment_words:
            del emitted_tail[:-max_segment_words]
        cleaned_segments.append(
            TranscriptSegment(
                start=segment.start,
                end=segment.end,
                text=" ".join(new_words),
            )
        )

    return cleaned_segments


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


def extract_video_info(job: Job) -> Dict[str, Any]:
    def extract() -> Dict[str, Any]:
        with YoutubeDL(ydl_base_options()) as ydl:
            return ydl.extract_info(job.url, download=False)

    return run_youtube_operation(
        extract,
        on_retry=lambda attempt, total, _exc: update_job(
            job.id,
            progress=10,
            message=f"YouTube interrupted the connection; retrying ({attempt + 1} of {total})",
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


def captions_to_segments(vtt_path: Path) -> List[TranscriptSegment]:
    segments: List[TranscriptSegment] = []
    seen = set()
    for caption in webvtt.read(str(vtt_path)):
        text = clean_text(caption.text)
        if not text:
            continue
        key = (caption.start, caption.end, text)
        if key in seen:
            continue
        seen.add(key)
        segments.append(
            TranscriptSegment(
                start=vtt_time_to_seconds(caption.start),
                end=vtt_time_to_seconds(caption.end),
                text=text,
            )
        )
    return remove_rolling_caption_repeats(segments)


def download_caption_file(url: str, destination: Path) -> None:
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


def try_captions(job: Job, info: Dict[str, Any], job_dir: Path) -> Optional[List[TranscriptSegment]]:
    track = pick_caption_track(info, job.language)
    if not track:
        return None

    update_job(job.id, progress=35, message="Downloading YouTube captions")
    caption_path = job_dir / "captions.vtt"
    if not caption_path.exists() or caption_path.stat().st_size == 0:
        download_caption_file(track["url"], caption_path)
    segments = captions_to_segments(caption_path)
    if not segments:
        return None

    update_job(job.id, source=f"{track['source']} ({track['language']})")
    return segments


def format_bytes(value: Optional[float]) -> str:
    if not value:
        return "?"
    units = ["B", "KB", "MB", "GB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def download_progress_hook(job_id: str):
    last_update = {"time": 0.0}

    def hook(data: Dict[str, Any]) -> None:
        status = data.get("status")
        now = time.time()
        if status == "downloading" and now - last_update["time"] < 1.5:
            return
        last_update["time"] = now

        downloaded = data.get("downloaded_bytes") or 0
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        if status == "downloading":
            if total:
                ratio = max(0.0, min(1.0, downloaded / total))
                progress = 30 + int(ratio * 18)
                message = f"Descargando audio: {format_bytes(downloaded)} de {format_bytes(total)}"
            else:
                progress = 32
                message = f"Descargando audio: {format_bytes(downloaded)}"
            update_job(job_id, progress=progress, message=message)
        elif status == "finished":
            update_job(job_id, progress=49, message="Audio descargado; preparando Whisper")

    return hook


def download_audio(job: Job, job_dir: Path) -> Path:
    cached_audio = sorted(
        (
            path
            for path in job_dir.iterdir()
            if path.is_file() and path.name.startswith("audio.") and not path.name.endswith(".part") and path.stat().st_size > 0
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if cached_audio:
        update_job(job.id, progress=49, message="Reutilizando audio descargado anteriormente")
        return cached_audio[0]

    output_template = str(job_dir / "audio.%(ext)s")
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
        "progress_hooks": [download_progress_hook(job.id)],
        "extractor_args": {"youtube": {"player_client": ["android", "ios", "tv"]}},
    }
    update_job(job.id, progress=30, message="Descargando audio")

    def download() -> None:
        with YoutubeDL(options) as ydl:
            ydl.download([job.url])

    run_youtube_operation(
        download,
        on_retry=lambda attempt, total, _exc: update_job(
            job.id,
            progress=30,
            message=f"YouTube interrupted the download; retrying ({attempt + 1} of {total})",
        ),
    )

    audio_files = [
        path
        for path in job_dir.iterdir()
        if path.is_file() and path.name.startswith("audio.") and not path.name.endswith(".part") and path.stat().st_size > 0
    ]
    if not audio_files:
        raise RuntimeError("The video audio could not be downloaded.")
    return max(audio_files, key=lambda path: path.stat().st_mtime)


def keep_job_alive(job_id: str, stop_event: threading.Event, message: str, start: int, end: int) -> None:
    progress = start
    while not stop_event.wait(12):
        progress = min(end, progress + 1)
        update_job(job_id, progress=progress, message=message)


def transcribe_with_whisper(job: Job, audio_path: Path) -> List[TranscriptSegment]:
    from faster_whisper import WhisperModel

    model = whisper_models.get(job.model_size)
    if model is None:
        update_job(job.id, progress=50, message=f"Cargando Whisper {job.model_size}. La primera vez puede tardar.")
        load_stop = threading.Event()
        load_thread = threading.Thread(
            target=keep_job_alive,
            args=(job.id, load_stop, f"Cargando Whisper {job.model_size}. La primera vez puede tardar.", 50, 58),
            daemon=True,
        )
        load_thread.start()
        try:
            with whisper_models_lock:
                model = whisper_models.get(job.model_size)
                if model is None:
                    model = WhisperModel(job.model_size, device="auto", compute_type="int8")
                    whisper_models[job.model_size] = model
        finally:
            load_stop.set()
    else:
        update_job(job.id, progress=58, message=f"Whisper {job.model_size} is ready; starting transcription")

    def collect_segments(vad_filter: bool, beam_size: int) -> List[TranscriptSegment]:
        segments_iter, _info = model.transcribe(
            str(audio_path),
            language=None if job.language == "auto" else job.language,
            vad_filter=vad_filter,
            beam_size=beam_size,
            best_of=1,
            condition_on_previous_text=False,
        )
        segments: List[TranscriptSegment] = []
        for segment in segments_iter:
            text = clean_text(segment.text)
            if not text:
                continue
            segments.append(
                TranscriptSegment(
                    start=float(segment.start),
                    end=float(segment.end),
                    text=text,
                )
            )
            if job.duration:
                ratio = max(0.0, min(1.0, float(segment.end) / job.duration))
                progress = min(92, 60 + int(ratio * 32))
                minutes_done = int(float(segment.end) // 60)
                minutes_total = int(job.duration // 60)
                update_job(
                    job.id,
                    progress=progress,
                    message=f"Transcribing audio: minute {minutes_done} of {minutes_total}",
                )
            elif len(segments) % 10 == 0:
                update_job(job.id, progress=min(92, 60 + len(segments)))
        return segments

    update_job(job.id, progress=60, message="Transcribing audio")
    segments = collect_segments(vad_filter=True, beam_size=1)
    if not segments:
        update_job(job.id, progress=60, message="No speech detected with the normal filter; trying the full audio")
        segments = collect_segments(vad_filter=False, beam_size=3)
    update_job(job.id, source=f"Whisper local ({job.model_size})")
    return segments


def process_job(job_id: str) -> None:
    with jobs_lock:
        job = jobs[job_id]

    job_dir = JOBS_DIR / job.id
    job_dir.mkdir(parents=True, exist_ok=True)
    persist_job_snapshot(job.id, public_job(job))

    try:
        update_job(job.id, status="running", progress=10, message="Leyendo informacion del video")
        info = extract_video_info(job)

        title = info.get("title") or "YouTube video"
        basename_source = f"{job.filename_prefix} {title}" if job.filename_prefix else title
        basename = sanitize_filename(basename_source)
        update_job(job.id, title=title, duration=info.get("duration"), output_basename=basename)

        if job.skip_existing:
            existing_transcript = find_existing_transcript(job, basename, info)
            if existing_transcript:
                update_job(
                    job.id,
                    status="skipped",
                    progress=100,
                    message="A transcript already exists; video skipped",
                    source="Existing file",
                    transcript_path=str(existing_transcript),
                )
                return

        segments: Optional[List[TranscriptSegment]] = None
        if job.prefer_captions:
            try:
                segments = try_captions(job, info, job_dir)
            except Exception:
                update_job(job.id, progress=35, message="Captions could not be used; continuing with audio")

        if not segments:
            audio_path = download_audio(job, job_dir)
            segments = transcribe_with_whisper(job, audio_path)

        if not segments:
            raise RuntimeError("Whisper no detecto voz hablada en el audio; el video puede ser musical, cinematografico o no tener narracion.")

        update_job(job.id, progress=94, message="Guardando archivos")
        outputs = write_outputs(job_dir, segments, job.output_basename or "youtube-transcript", job.save_srt)
        saved_outputs = copy_outputs_to_destination(job, outputs)
        update_job(
            job.id,
            status="done",
            progress=100,
            message=f"Transcripcion lista en {Path(job.output_dir).expanduser()}",
            transcript_path=str(saved_outputs["txt"]),
            srt_path=str(saved_outputs["srt"]) if "srt" in saved_outputs else None,
        )
        if job.index_on_completion:
            try:
                append_single_job_index(job)
            except Exception as exc:
                update_job(job.id, message=f"Transcripcion lista, pero no pude actualizar el indice: {exc}")
    except Exception as exc:
        update_job(job.id, status="error", error=str(exc), message="Could not complete the job", progress=100)


def parse_urls(raw_text: str) -> List[str]:
    raw_text = raw_text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", " ")
    candidates = re.findall(r"https?://[^\s,;]+", raw_text)
    urls = []
    seen = set()
    for candidate in candidates:
        cleaned = candidate.strip().rstrip(").]")
        if not youtube_video_id(cleaned):
            continue
        dedupe_key = youtube_video_id(cleaned) or cleaned
        if dedupe_key not in seen:
            seen.add(dedupe_key)
            urls.append(cleaned)
    return urls


KNOWLEDGE_THEMES = {
    "backlinks": ["backlink", "link building", "anchor", "guest post", "niche edit", "link"],
    "authority": ["authority", "trust", "brand", "domain rating", "dr ", "da "],
    "content": ["content", "article", "page", "topic", "keyword", "copy"],
    "relevance": ["relevance", "relevant", "topical", "niche", "semantic"],
    "competition": ["competition", "competitor", "serp", "ranking", "rank"],
    "audit": ["audit", "crawl", "index", "technical", "schema", "internal link"],
    "local_seo": ["local seo", "maps", "gbp", "google business", "location"],
    "ai": ["ai", "chatgpt", "llm", "automation", "generated"],
    "pbn": ["pbn", "private blog", "network", "expired domain"],
    "scams": ["scam", "cheap", "fiverr", "lifetime", "guarantee", "package"],
    "penalties": ["penalty", "manual action", "spam", "deindex", "risk"],
    "metrics": ["ahrefs", "semrush", "majestic", "traffic", "metrics"],
}


def split_sentences(text: str) -> List[str]:
    compact = re.sub(r"\s+", " ", text).strip()
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", compact) if part.strip()]


SUMMARY_STOPWORDS = {
    "a", "al", "con", "de", "del", "el", "en", "es", "la", "las", "lo", "los", "para", "por", "que", "se", "su", "un", "una", "y",
    "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "it", "of", "on", "or", "that", "the", "to", "with",
}


def summary_tokens(text: str) -> List[str]:
    return [word for word in re.findall(r"[a-zA-ZÀ-ÿ0-9]{3,}", text.lower()) if word not in SUMMARY_STOPWORDS]


NEGATION_WORDS = {"no", "nunca", "jamas", "jamás", "sin", "ningun", "ninguna", "ninguno"}
NUMBER_WORDS = {
    "cero", "uno", "una", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve", "diez",
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
}


def claim_markers(text: str) -> Dict[str, set[str]]:
    words = re.findall(r"[a-zA-ZÀ-ÿ0-9]+", text.lower())
    return {
        "negations": {word for word in words if word in NEGATION_WORDS},
        "numbers": {word for word in words if word.isdigit() or word in NUMBER_WORDS},
    }


def sentences_are_duplicate(first: str, second: str) -> bool:
    first_markers = claim_markers(first)
    second_markers = claim_markers(second)
    if bool(first_markers["negations"]) != bool(second_markers["negations"]):
        return False
    if first_markers["numbers"] and second_markers["numbers"] and first_markers["numbers"] != second_markers["numbers"]:
        return False
    first_tokens = set(summary_tokens(first))
    second_tokens = set(summary_tokens(second))
    if not first_tokens or not second_tokens:
        return False
    overlap = len(first_tokens & second_tokens) / max(1, len(first_tokens | second_tokens))
    return overlap >= 0.78 and SequenceMatcher(None, first.lower(), second.lower()).ratio() >= 0.78


def build_consolidated_summary(batch: Batch) -> Optional[Path]:
    completed_results = [
        result
        for result in batch.results
        if result.get("status") in {"done", "skipped"} and result.get("transcript_path") and Path(result["transcript_path"]).exists()
    ]
    if not completed_results:
        return None

    all_sentences = []
    document_frequency = Counter()
    for order, result in enumerate(completed_results, start=1):
        path = Path(result["transcript_path"])
        text = path.read_text(encoding="utf-8", errors="ignore")
        sentences = split_sentences(text)
        for position, sentence in enumerate(sentences):
            tokens = summary_tokens(sentence)
            if len(tokens) < 5:
                continue
            document_frequency.update(set(tokens))
            all_sentences.append({
                "order": order,
                "position": position,
                "title": result.get("title") or path.stem,
                "url": result.get("url") or "",
                "sentence": sentence,
                "tokens": tokens,
            })

    if not all_sentences:
        return None

    ranked = sorted(
        all_sentences,
        key=lambda item: (
            sum(document_frequency[token] for token in set(item["tokens"])) / len(set(item["tokens"])),
            min(len(item["tokens"]), 45),
        ),
        reverse=True,
    )
    selected = []
    selected_words = 0
    max_words = max(500, batch.summary_max_words)
    for candidate in ranked:
        if any(sentences_are_duplicate(candidate["sentence"], item["sentence"]) for item in selected):
            continue
        candidate_words = len(candidate["tokens"])
        if selected and selected_words + candidate_words > max_words:
            continue
        selected.append(candidate)
        selected_words += candidate_words
        if selected_words >= max_words:
            break

    total_sources = len(completed_results)
    for item in selected:
        item["source_count"] = len({
            source["order"]
            for source in all_sentences
            if sentences_are_duplicate(item["sentence"], source["sentence"])
        })
    selected.sort(key=lambda item: (item["order"], item["position"]))
    lines = [
        "CONSOLIDATED SUMMARY",
        "",
        f"Videos analyzed: {len(completed_results)}",
        "This summary was generated locally by selecting frequent sentences and removing exact or near-textual repetitions.",
        "The count indicates textual matches, not semantic agreement or factual accuracy. Review the original transcripts before using a conclusion.",
        "Each point retains its source so it can be checked against the original transcript.",
        "",
    ]
    current_order = None
    for item in selected:
        if item["order"] != current_order:
            current_order = item["order"]
            lines.extend([f"## {item['title']}", ""])
            if item["url"]:
                lines.append(f"Source: {item['url']}")
                lines.append("")
        lines.append(f"- [Textual match: {item['source_count']} of {total_sources} videos] {item['sentence']}")
    lines.extend(["", f"Selected words: {selected_words}", ""])

    destination_dir = destination_dir_for(batch.output_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    summary_path = unique_path(destination_dir / f"consolidated-summary-{batch.id}.txt")
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return summary_path


EDITORIAL_THEMES = {
    "Performance and results": ["works", "result", "cooks", "heats", "power", "performance", "quality"],
    "Ease of use": ["easy", "simple", "clean", "cleaning", "use", "program", "button"],
    "Limitations and issues": ["problem", "failure", "noise", "difficult", "worse", "drawback", "disadvantage", "smell"],
    "Durability and materials": ["durable", "lasts", "material", "plastic", "metal", "broken", "resistant", "warranty"],
    "Purchasing and user profiles": ["worth", "price", "value", "family", "person", "space", "buy", "recommend"],
}


def editorial_theme(sentence: str) -> str:
    lowered = sentence.lower()
    scores = {theme: sum(keyword in lowered for keyword in keywords) for theme, keywords in EDITORIAL_THEMES.items()}
    return max(scores, key=scores.get) if max(scores.values(), default=0) else "General observations"


def build_editorial_material(
    product_name: str,
    manufacturer_text: str,
    transcriptions_dir: str,
    max_words: int,
    transcript_files: Optional[List[Path]] = None,
) -> Path:
    directory = destination_dir_for(transcriptions_dir)
    if transcript_files is None and (not directory.exists() or not directory.is_dir()):
        raise RuntimeError("The transcript folder does not exist.")

    if transcript_files is None:
        transcript_files = sorted(
            path for path in directory.glob("*.txt")
            if not path.name.startswith(("editorial-material-", "consolidated-summary-"))
        )
    else:
        transcript_files = sorted(
            {path.resolve() for path in transcript_files if path.exists() and path.is_file()},
            key=lambda path: path.name.casefold(),
        )
    if not transcript_files:
        raise RuntimeError("No usable transcripts were found for this research project.")

    manufacturer_sentences = split_sentences(manufacturer_text)
    candidates = []
    frequency = Counter()
    for source_index, path in enumerate(transcript_files, start=1):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for position, sentence in enumerate(split_sentences(text)):
            tokens = summary_tokens(sentence)
            if len(tokens) < 5:
                continue
            if any(sentences_are_duplicate(sentence, known) for known in manufacturer_sentences):
                continue
            frequency.update(set(tokens))
            candidates.append({"sentence": sentence, "tokens": tokens, "theme": editorial_theme(sentence), "position": position, "source_index": source_index})

    ranked = sorted(
        candidates,
        key=lambda item: sum(frequency[token] for token in set(item["tokens"])) / len(set(item["tokens"])),
        reverse=True,
    )
    selected = []
    selected_words = 0
    for candidate in ranked:
        if any(sentences_are_duplicate(candidate["sentence"], item["sentence"]) for item in selected):
            continue
        words = len(candidate["tokens"])
        if selected and selected_words + words > max_words:
            continue
        selected.append(candidate)
        selected_words += words
        if selected_words >= max_words:
            break

    total_sources = len(transcript_files)
    for item in selected:
        item["source_count"] = len({
            source["source_index"]
            for source in candidates
            if sentences_are_duplicate(item["sentence"], source["sentence"])
        })
    grouped: Dict[str, List[str]] = {}
    for item in selected:
        grouped.setdefault(item["theme"], []).append(item["sentence"])
    lines = [
        "EDITORIAL RESEARCH MATERIAL",
        "",
        f"Product or topic: {product_name or 'Not specified'}",
        f"Files analyzed: {len(transcript_files)}",
        "",
        "This document groups practical experience statements found in the selected transcripts.",
        "Statements similar to supplied manufacturer information were excluded.",
        "The count measures textual matches, not consensus or factual accuracy. This is not publication-ready copy and should be reviewed before editorial use.",
        "",
        "## POTENTIALLY NEW INFORMATION",
        "",
    ]
    for theme, sentences in grouped.items():
        lines.extend([f"### {theme}", ""])
        lines.extend(f"- [Textual match: {item['source_count']} of {total_sources} videos] {item['sentence']}" for item in selected if item["theme"] == theme)
        lines.append("")
    lines.extend([
        "## AI INSTRUCTIONS",
        "",
        "Use this material to improve a buying guide. Do not mention reviewers or copy exact phrases.",
        "Form an original conclusion from repeated patterns, distinguish experience from specifications, and do not invent facts.",
        "",
    ])

    filename = sanitize_filename(f"editorial-material-{product_name or 'product'}")
    directory.mkdir(parents=True, exist_ok=True)
    output_path = unique_path(directory / f"{filename}.txt")
    atomic_write_text(output_path, "\n".join(lines))
    return output_path


def matching_theme(sentence: str) -> Optional[str]:
    lowered = sentence.lower()
    best_theme = None
    best_score = 0
    for theme, keywords in KNOWLEDGE_THEMES.items():
        score = sum(1 for keyword in keywords if keyword in lowered)
        if score > best_score:
            best_score = score
            best_theme = theme
    return best_theme


def build_knowledge_base(batch: Batch) -> Optional[Path]:
    completed_results = [
        result
        for result in batch.results
        if result.get("status") in {"done", "skipped"} and result.get("transcript_path") and Path(result["transcript_path"]).exists()
    ]
    if not completed_results:
        return None

    destination_dir = destination_dir_for(batch.output_dir)
    kb_dir = destination_dir / f"knowledge-base-{batch.id}"
    kb_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir = kb_dir / "evidence-by-theme"
    evidence_dir.mkdir(exist_ok=True)

    theme_hits: Dict[str, List[Dict[str, str]]] = {theme: [] for theme in KNOWLEDGE_THEMES}
    index_rows = []
    for order, result in enumerate(completed_results, start=1):
        path = Path(result["transcript_path"])
        text = path.read_text(encoding="utf-8", errors="ignore")
        words = len(re.findall(r"\w+", text))
        index_rows.append(
            {
                "order": order,
                "title": result.get("title") or path.stem,
                "url": result.get("url") or "",
                "status": result.get("status") or "",
                "source": result.get("source") or "",
                "transcript_path": str(path),
                "words": words,
            }
        )

        for sentence in split_sentences(text):
            theme = matching_theme(sentence)
            if theme and len(theme_hits[theme]) < 80:
                theme_hits[theme].append(
                    {
                        "title": result.get("title") or path.stem,
                        "url": result.get("url") or "",
                        "sentence": sentence[:600],
                    }
                )

    index_fields = ["order", "title", "url", "status", "source", "transcript_path", "words"]
    with (kb_dir / "index.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=index_fields)
        writer.writeheader()
        writer.writerows(index_rows)

    theme_rows = []
    for theme, hits in sorted(theme_hits.items(), key=lambda item: len(item[1]), reverse=True):
        theme_rows.append({"theme": theme, "mentions": len(hits)})
        if not hits:
            continue
        lines = [f"# {theme}", ""]
        for hit in hits[:40]:
            lines.append(f"- {hit['sentence']}")
            if hit["url"]:
                lines.append(f"  Source: {hit['title']} - {hit['url']}")
        (evidence_dir / f"{theme}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    with (kb_dir / "themes.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["theme", "mentions"])
        writer.writeheader()
        writer.writerows(theme_rows)

    top_themes = [row for row in theme_rows if row["mentions"] > 0][:8]
    readme = [
        "# Knowledge base",
        "",
        f"Batch: {batch.id}",
        f"Transcripts analyzed: {len(completed_results)}",
        "",
        "## Files",
        "",
        "- `index.csv`: inventory of the transcripts used.",
        "- `themes.csv`: detected-theme counts.",
        "- `evidence-by-theme/`: relevant sentences grouped by theme.",
        "- `action-ideas.md`: starting checklist for turning the material into SEO improvements.",
        "",
        "## Top themes",
        "",
    ]
    readme.extend([f"- {row['theme']}: {row['mentions']} mentions" for row in top_themes] or ["- No themes had enough evidence."])
    (kb_dir / "README.md").write_text("\n".join(readme) + "\n", encoding="utf-8")

    ideas = [
        "# Action ideas",
        "",
        "1. Review the themes with the most evidence in `themes.csv` and open the files in `evidence-by-theme/`.",
        "2. Turn each important statement into an operational rule: what to inspect, how to measure it, and what action to take.",
        "3. Separate actions by impact: content, links, technical audit, authority, competition, and risk.",
        "4. Use the source material to ask an AI for a deeper playbook without losing traceability to each video.",
        "",
        "Suggested prompt:",
        "",
        "Study this SEO transcript knowledge base. Extract principles, warnings, repeated tactics, contradictions, and opportunities to improve my tools and sites. Cite the source file or video when you propose an action.",
    ]
    (kb_dir / "action-ideas.md").write_text("\n".join(ideas) + "\n", encoding="utf-8")

    return kb_dir


def process_batch(batch_id: str) -> None:
    with batch_lock:
        batch = batches[batch_id]

    destination_dir = destination_dir_for(batch.output_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    if not batch.state_path:
        batch.state_path = str(destination_dir / f"batch-state-{batch.id}.json")
    update_batch(
        batch.id,
        status="running",
        message="Processing batch",
        total=len(batch.urls),
        index_path=str(destination_dir / f"batch-index-{batch.id}.csv"),
        global_index_path=str(destination_dir / "transcription-index.csv"),
    )
    persist_batch_state(batch)
    completed_urls = {
        result.get("url")
        for result in batch.results
        if result.get("status") in {"done", "skipped"}
    }
    for index, url in enumerate(batch.urls):
        if url in completed_urls:
            continue
        with batch_lock:
            current_batch = batches[batch_id]
            if current_batch.status == "cancelled":
                return

        prefix = str(batch.start_number + index) if batch.start_number is not None else None
        job_id = f"{batch.id}-{index + 1}"
        job = Job(
            id=job_id,
            url=url,
            language=batch.language,
            model_size=batch.model_size,
            prefer_captions=batch.prefer_captions,
            output_dir=batch.output_dir,
            save_srt=batch.save_srt,
            filename_prefix=prefix,
            skip_existing=batch.skip_existing,
            index_on_completion=False,
        )
        with jobs_lock:
            jobs[job_id] = job

        update_batch(
            batch.id,
            current_job_id=job_id,
            progress=int((index / max(1, len(batch.urls))) * 100),
            message=f"Transcribing {index + 1} of {len(batch.urls)}",
        )
        process_job(job_id)

        with jobs_lock:
            finished_job = jobs[job_id]
            result = public_job(finished_job)

        with batch_lock:
            batch = batches[batch_id]
            batch.results.append(result)
            if finished_job.status == "done":
                batch.completed += 1
            elif finished_job.status == "skipped":
                batch.skipped += 1
            else:
                batch.failed += 1
            batch.progress = int(((index + 1) / max(1, len(batch.urls))) * 100)
            batch.message = f"Procesados {index + 1} de {len(batch.urls)}"

        try:
            append_batch_indexes(batch, finished_job, index + 1)
        except Exception as exc:
            update_batch(batch.id, error=f"Could not update the batch index: {exc}")
        if batch.project_id and finished_job.status in {"done", "skipped"} and finished_job.transcript_path:
            try:
                library.register_transcript(
                    batch.project_id,
                    Path(finished_job.transcript_path),
                    {
                        "video_id": youtube_video_id(finished_job.url),
                        "title": finished_job.title,
                        "source_url": finished_job.url,
                        "source_kind": finished_job.source,
                        "language": finished_job.language,
                        "model_size": finished_job.model_size,
                    },
                )
            except LibraryError as exc:
                update_batch(batch.id, error=f"The video was transcribed, but could not be added to the research project: {exc}")
        persist_batch_state(batch)

    if batch.generate_knowledge_base or batch.generate_consolidated_summary:
        update_batch(batch.id, message="Preparando resultados consolidados")
        try:
            if batch.generate_knowledge_base:
                kb_path = build_knowledge_base(batch)
                if kb_path:
                    update_batch(batch.id, knowledge_base_path=str(kb_path))
            if batch.generate_consolidated_summary:
                summary_path = build_consolidated_summary(batch)
                if summary_path:
                    update_batch(batch.id, consolidated_summary_path=str(summary_path))
        except Exception as exc:
            update_batch(batch.id, error=f"No pude crear los resultados consolidados: {exc}")

    has_incidents = bool(batch.failed or batch.error)
    final_status = "done_with_errors" if has_incidents else "done"
    final_message = "Batch finished with issues" if has_incidents else "Batch finished"
    update_batch(batch.id, status=final_status, current_job_id=None, progress=100, message=final_message)
    persist_batch_state(batch)


def start_batch_worker(batch_id: str) -> bool:
    with batch_workers_lock:
        if batch_id in batch_workers:
            return False
        batch_workers.add(batch_id)

    def run() -> None:
        try:
            process_batch(batch_id)
        except Exception as exc:
            update_batch(batch_id, status="error", current_job_id=None, progress=100, message="The batch stopped", error=str(exc))
            with batch_lock:
                batch = batches[batch_id]
            persist_batch_state(batch)
        finally:
            with batch_workers_lock:
                batch_workers.discard(batch_id)

    thread = threading.Thread(target=run, name=f"batch-{batch_id}", daemon=True)
    thread.start()
    return True


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request, "default_output_dir": default_output_dir()})


@app.get("/api/projects")
def list_projects() -> JSONResponse:
    return JSONResponse({"projects": library.list_projects()})


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> JSONResponse:
    try:
        project = library.get_project(project_id)
        transcripts = library.project_transcripts(project_id)
    except LibraryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse({"project": project, "transcripts": transcripts})


@app.post("/api/projects")
def create_project(
    name: str = Form(...),
    aliases: str = Form(""),
    output_dir: str = Form(""),
    manufacturer_text: str = Form(""),
) -> JSONResponse:
    project_dir = topic_output_dir(output_dir, name)
    try:
        project_dir.mkdir(parents=True, exist_ok=True)
        project = library.create_project(
            name=name,
            aliases=aliases,
            output_dir=str(project_dir),
            manufacturer_text=manufacturer_text,
        )
    except (LibraryError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"project": project})


@app.post("/api/projects/{project_id}/scan")
def scan_project_transcripts(
    project_id: str,
    transcriptions_dir: str = Form(""),
    filename_filter: str = Form(""),
) -> JSONResponse:
    try:
        project = library.get_project(project_id)
        directory = transcriptions_dir.strip() or project["output_dir"]
        candidates = library.scan_directory(directory, filename_filter.strip())
    except LibraryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"project": project, "directory": directory, "count": len(candidates), "candidates": candidates})


@app.post("/api/projects/{project_id}/import")
def import_project_transcripts(
    project_id: str,
    transcriptions_dir: str = Form(""),
    filename_filter: str = Form(""),
    paths_json: str = Form("[]"),
) -> JSONResponse:
    try:
        selected_paths = json.loads(paths_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="The selected files are not valid.") from exc
    if not isinstance(selected_paths, list) or not all(isinstance(path, str) for path in selected_paths):
        raise HTTPException(status_code=400, detail="The selected files are not valid.")
    try:
        project = library.get_project(project_id)
        result = library.import_paths(
            project_id=project_id,
            directory=transcriptions_dir.strip() or project["output_dir"],
            selected_paths=selected_paths,
            filename_filter=filename_filter.strip(),
        )
    except LibraryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)


def batch_from_state(state_path: str) -> Batch:
    path = Path(os.path.expandvars(os.path.expanduser(state_path))).resolve()
    if not path.exists():
        raise HTTPException(status_code=404, detail="The batch state file was not found.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        batch_fields = {field.name for field in Batch.__dataclass_fields__.values()}
        batch = Batch(**{key: value for key, value in data.items() if key in batch_fields})
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"The batch state is not valid: {exc}") from exc
    batch.state_path = str(path)
    retryable_results = [result for result in batch.results if result.get("status") in {"done", "skipped"}]
    batch.results = retryable_results
    batch.completed = sum(1 for result in retryable_results if result.get("status") == "done")
    batch.skipped = sum(1 for result in retryable_results if result.get("status") == "skipped")
    batch.failed = 0
    batch.status = "queued"
    batch.error = None
    return batch


@app.post("/api/open-folder")
def open_folder(output_dir: str = Form("")) -> JSONResponse:
    destination_dir = destination_dir_for(output_dir.strip() or default_output_dir())
    destination_dir.mkdir(parents=True, exist_ok=True)

    if sys.platform == "darwin":
        command = ["open", str(destination_dir)]
    elif sys.platform.startswith("linux"):
        command = ["xdg-open", str(destination_dir)]
    else:
        raise HTTPException(status_code=400, detail="Opening folders from the app is only supported on macOS and Linux.")

    try:
        subprocess.Popen(command)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not open the folder: {exc}") from exc

    return JSONResponse({"path": str(destination_dir)})


@app.post("/api/batches/resume")
def resume_batch(state_path: str = Form(...)) -> JSONResponse:
    batch = batch_from_state(state_path.strip())
    with batch_lock:
        existing = batches.get(batch.id)
        if existing and existing.status in {"queued", "running"}:
            raise HTTPException(status_code=409, detail="That batch is already running in this application.")
        batches[batch.id] = batch
    persist_batch_state(batch)
    if not start_batch_worker(batch.id):
        raise HTTPException(status_code=409, detail="That batch is already running in this application.")
    return JSONResponse(public_batch(batch))


@app.post("/api/editorial-material")
def create_editorial_material(
    product_name: str = Form(""),
    manufacturer_text: str = Form(""),
    transcriptions_dir: str = Form(""),
    max_words: str = Form("3500"),
    project_id: str = Form(""),
) -> JSONResponse:
    parsed_max_words = int(max_words) if max_words.strip().isdigit() else 3500
    try:
        transcript_files = None
        if project_id.strip():
            project = library.get_project(project_id.strip())
            product_name = product_name.strip() or project["name"]
            manufacturer_text = manufacturer_text.strip() or project["manufacturer_text"]
            transcriptions_dir = transcriptions_dir.strip() or project["output_dir"]
            transcript_files = library.project_paths(project_id.strip())
        output_path = build_editorial_material(
            product_name.strip(),
            manufacturer_text.strip(),
            transcriptions_dir.strip() or default_output_dir(),
            max(500, min(parsed_max_words, 20000)),
            transcript_files=transcript_files,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"path": str(output_path)})


@app.post("/api/jobs")
def create_job(
    url: str = Form(...),
    language: str = Form("auto"),
    model_size: str = Form("tiny"),
    prefer_captions: str = Form(""),
    output_dir: str = Form(""),
    topic_name: str = Form(""),
    save_srt: str = Form(""),
) -> JSONResponse:
    if not youtube_video_id(url):
        raise HTTPException(status_code=400, detail="Enter a valid YouTube URL.")

    job_id = uuid.uuid4().hex[:12]
    selected_model = model_size if model_size in ALLOWED_MODELS else "tiny"
    selected_output_dir = output_dir.strip() or default_output_dir()
    if topic_name.strip():
        selected_output_dir = str(topic_output_dir(selected_output_dir, topic_name))
    try:
        destination_dir_for(selected_output_dir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Could not create the output folder: {exc}") from exc

    job = Job(
        id=job_id,
        url=url.strip(),
        language=language.strip() if language.strip() in ALLOWED_LANGUAGES else "auto",
        model_size=selected_model,
        prefer_captions=form_checkbox_enabled(prefer_captions),
        output_dir=selected_output_dir,
        save_srt=save_srt == "on",
    )
    with jobs_lock:
        jobs[job_id] = job

    thread = threading.Thread(target=process_job, args=(job_id,), daemon=True)
    thread.start()
    return JSONResponse(public_job(job))


@app.post("/api/channel/extract")
def extract_channel(
    channel_url: str = Form(...),
    filter_type: str = Form("all"),
    limit: int = Form(10),
) -> JSONResponse:
    if not is_youtube_host(urlparse(channel_url.strip()).hostname):
        raise HTTPException(status_code=400, detail="Enter a valid YouTube URL.")
    if limit <= 0 and filter_type != "all":
        raise HTTPException(status_code=400, detail="The limit must be greater than zero.")
    videos = extract_channel_videos(channel_url.strip(), filter_type, limit)
    return JSONResponse({"count": len(videos), "videos": videos, "links": [video["url"] for video in videos]})


@app.post("/api/search")
def search_videos(
    query: str = Form(...),
    limit: int = Form(10),
    sort: str = Form("relevance"),
    video_type: str = Form("all"),
    duration_filter: str = Form("any"),
    upload_filter: str = Form("any"),
    feature: str = Form("all"),
) -> JSONResponse:
    if not query.strip():
        raise HTTPException(status_code=400, detail="Enter a video search query.")
    if not 1 <= limit <= 50:
        raise HTTPException(status_code=400, detail="The limit must be between 1 and 50.")
    if sort not in {"relevance", "most_viewed", "newest"}:
        raise HTTPException(status_code=400, detail="The requested sort order is not valid.")
    if video_type not in {"all", "videos", "shorts"}:
        raise HTTPException(status_code=400, detail="The requested video type is not valid.")
    if duration_filter not in {"any", "short", "medium", "long"}:
        raise HTTPException(status_code=400, detail="The requested duration is not valid.")
    if upload_filter not in {"any", "today", "week", "month", "year"}:
        raise HTTPException(status_code=400, detail="The requested date filter is not valid.")
    if feature not in {"all", "live", "hd", "4k", "subtitles"}:
        raise HTTPException(status_code=400, detail="The requested feature is not valid.")

    advanced_filter = video_type != "all" or duration_filter != "any" or upload_filter != "any" or feature != "all"
    fetch_limit = min(max(limit * (5 if advanced_filter else 1), 20), 100)
    prefix = "ytsearchdate" if sort == "newest" else "ytsearch"
    search_term = f"{prefix}{fetch_limit}:{query.strip()}"
    with YoutubeDL(ydl_flat_options(fetch_limit)) as ydl:
        info = ydl.extract_info(search_term, download=False)

    entries = [entry for entry in (info.get("entries", []) if info else []) if entry and entry.get("id")]
    received_count = len(entries)
    seen_ids = set()
    videos = []
    for entry in entries:
        video = video_record(entry, len(videos) + 1)
        if not video["id"] or video["id"] in seen_ids:
            continue
        seen_ids.add(video["id"])
        videos.append(video)

    metadata = {
        "attempted": 0,
        "completed": 0,
        "verified": 0,
        "failed": 0,
        "pending": 0,
        "skipped": 0,
        "time_budget_seconds": None,
    }
    needs_metadata = sort in {"most_viewed", "newest"} or advanced_filter
    if needs_metadata:
        candidate_limit = min(len(videos), max(limit * 2, 20), SEARCH_METADATA_CANDIDATE_CAP)
        videos, metadata = enrich_search_videos(
            videos,
            candidate_limit=candidate_limit,
            time_budget_seconds=SEARCH_METADATA_TIME_BUDGET_SECONDS,
            return_stats=True,
        )
        videos = videos[:candidate_limit]
    evaluated_count = len(videos)
    videos = [
        video
        for video in videos
        if (not needs_metadata or video.get("metadata_available"))
        and search_video_matches(video, video_type, duration_filter, upload_filter, feature)
    ]
    matched_count = len(videos)
    if sort == "most_viewed":
        videos.sort(key=lambda item: item.get("view_count") or -1, reverse=True)
    elif sort == "newest":
        videos.sort(key=lambda item: item.get("upload_date") or "", reverse=True)
    videos = videos[:limit]
    videos = [{**video, "position": index + 1} for index, video in enumerate(videos)]
    return JSONResponse(
        {
            "count": len(videos),
            "requested": limit,
            "received": received_count,
            "evaluated": evaluated_count,
            "matched": matched_count,
            "scanned": evaluated_count,
            "metadata": metadata,
            "videos": videos,
            "links": [video["url"] for video in videos],
        }
    )


@app.post("/api/batches")
async def create_batch(
    links_text: str = Form(""),
    links_file: Optional[UploadFile] = File(None),
    language: str = Form("auto"),
    model_size: str = Form("tiny"),
    prefer_captions: str = Form(""),
    output_dir: str = Form(""),
    save_srt: str = Form(""),
    topic_name: str = Form(""),
    start_number: str = Form(""),
    skip_existing: str = Form(""),
    generate_knowledge_base: str = Form(""),
    generate_consolidated_summary: str = Form(""),
    summary_max_words: str = Form("3500"),
    project_id: str = Form(""),
) -> JSONResponse:
    file_text = ""
    if links_file and links_file.filename:
        file_text = (await links_file.read()).decode("utf-8", errors="ignore")
    urls = parse_urls(f"{links_text}\n{file_text}")
    if not urls:
        raise HTTPException(status_code=400, detail="No YouTube links were found.")

    selected_model = model_size if model_size in ALLOWED_MODELS else "tiny"
    parsed_start_number = int(start_number) if start_number.strip().isdigit() else None
    parsed_summary_max_words = int(summary_max_words) if summary_max_words.strip().isdigit() else 3500
    selected_project_id = project_id.strip() or None
    selected_project = None
    if selected_project_id:
        try:
            selected_project = library.get_project(selected_project_id)
        except LibraryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    selected_output_dir = output_dir.strip() or default_output_dir()
    if selected_project:
        selected_output_dir = selected_project["output_dir"]
    elif topic_name.strip():
        selected_output_dir = str(topic_output_dir(selected_output_dir, topic_name))
    try:
        destination_dir_for(selected_output_dir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Could not create the output folder: {exc}") from exc
    batch_id = uuid.uuid4().hex[:12]
    batch = Batch(
        id=batch_id,
        urls=urls,
        language=language.strip() if language.strip() in ALLOWED_LANGUAGES else "auto",
        model_size=selected_model,
        prefer_captions=form_checkbox_enabled(prefer_captions),
        output_dir=selected_output_dir,
        save_srt=save_srt == "on",
        project_id=selected_project_id,
        start_number=parsed_start_number,
        skip_existing=form_checkbox_enabled(skip_existing),
        generate_knowledge_base=form_checkbox_enabled(generate_knowledge_base),
        generate_consolidated_summary=form_checkbox_enabled(generate_consolidated_summary),
        summary_max_words=max(500, min(parsed_summary_max_words, 20000)),
        total=len(urls),
    )
    batch.state_path = str(destination_dir_for(batch.output_dir) / f"batch-state-{batch_id}.json")
    with batch_lock:
        batches[batch_id] = batch

    persist_batch_state(batch)

    start_batch_worker(batch_id)
    return JSONResponse(public_batch(batch))


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str) -> JSONResponse:
    with batch_lock:
        batch = batches.get(batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail="Batch not found.")
        data = public_batch(batch)
    return JSONResponse(data)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> JSONResponse:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            job_file = JOBS_DIR / job_id / "job.json"
            if job_file.exists():
                return JSONResponse(json.loads(job_file.read_text(encoding="utf-8")))
            raise HTTPException(status_code=404, detail="Job not found.")
        data = public_job(job)
    return JSONResponse(data)


@app.get("/api/jobs/{job_id}/download/{kind}")
def download(job_id: str, kind: str) -> FileResponse:
    if kind not in {"txt", "srt"}:
        raise HTTPException(status_code=404, detail="Formato no disponible.")

    with jobs_lock:
        job = jobs.get(job_id)

    if job:
        selected_path = job.transcript_path if kind == "txt" else job.srt_path
        if not selected_path:
            raise HTTPException(status_code=404, detail="Archivo no disponible.")
        path = Path(selected_path)
        filename = f"{job.output_basename or 'youtube-transcript'}.{kind}"
    else:
        candidates = sorted((JOBS_DIR / job_id).glob(f"*.{kind}"))
        path = candidates[0] if candidates else JOBS_DIR / job_id / f"transcript.{kind}"
        filename = path.name

    if not path.exists():
        raise HTTPException(status_code=404, detail="Archivo no disponible.")
    return FileResponse(path, filename=filename)
