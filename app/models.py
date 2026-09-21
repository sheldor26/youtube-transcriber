from __future__ import annotations

import csv
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import JOBS_DIR
from app.utils import atomic_write_text, destination_dir_for


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
    message: str = "Waiting to start"
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
    message: str = "Waiting to start"
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


def persist_job_snapshot(job_id: str, snapshot: Dict[str, Any]) -> None:
    job_path = JOBS_DIR / job_id / "job.json"
    with state_write_lock:
        atomic_write_text(job_path, json.dumps(snapshot, ensure_ascii=False, indent=2))


def persist_batch_state(batch: Batch) -> None:
    with batch_lock:
        snapshot = public_batch(batch)
    state_path = Path(snapshot["state_path"]) if snapshot.get("state_path") else destination_dir_for(snapshot["output_dir"]) / f"batch-state-{snapshot['id']}.json"
    with state_write_lock:
        atomic_write_text(state_path, json.dumps(snapshot, ensure_ascii=False, indent=2))


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
    from app.youtube import media_id

    destination_dir = destination_dir_for(batch.output_dir)
    row = {
        "batch_id": batch.id,
        "order": order,
        "video_id": media_id(job.url),
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
    from app.youtube import media_id

    destination_dir = destination_dir_for(job.output_dir)
    row = {
        "batch_id": "",
        "order": "",
        "video_id": media_id(job.url),
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
