from __future__ import annotations

import csv
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from app.config import JOBS_DIR, library
from app.content import build_consolidated_summary, build_knowledge_base
from app.library import LibraryError
from app.models import (
    Batch,
    Job,
    TranscriptSegment,
    append_batch_indexes,
    append_single_job_index,
    batch_lock,
    batch_workers,
    batch_workers_lock,
    batches,
    jobs,
    jobs_lock,
    persist_batch_state,
    persist_job_snapshot,
    public_job,
    update_batch,
    update_job,
)
from app.transcription import copy_outputs_to_destination, download_audio, transcribe_with_whisper, try_captions, write_outputs
from app.utils import destination_dir_for, sanitize_filename
from app.youtube import extract_video_info, media_id


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
    source_id = media_id(job.url) or info.get("id")
    indexed = find_indexed_transcript(job.output_dir, source_id)
    if indexed:
        return indexed

    # A title is not a stable identity. Preserve title-only detection only for
    # legacy URLs without a stable provider identifier.
    if not source_id:
        expected = destination_dir_for(job.output_dir) / f"{basename}.txt"
        if expected.exists():
            return expected
    return None


def process_job(job_id: str) -> None:
    with jobs_lock:
        job = jobs[job_id]

    job_dir = JOBS_DIR / job.id
    job_dir.mkdir(parents=True, exist_ok=True)
    persist_job_snapshot(job.id, public_job(job))

    try:
        update_job(job.id, status="running", progress=10, message="Reading video information")
        info = extract_video_info(job)

        title = info.get("title") or "Video"
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
            raise RuntimeError("Whisper did not detect speech in the audio. The video may be musical, cinematic, or have no narration.")

        update_job(job.id, progress=94, message="Saving files")
        outputs = write_outputs(job_dir, segments, job.output_basename or "youtube-transcript", job.save_srt)
        saved_outputs = copy_outputs_to_destination(job, outputs)
        update_job(
            job.id,
            status="done",
            progress=100,
            message=f"Transcript ready in {Path(job.output_dir).expanduser()}",
            transcript_path=str(saved_outputs["txt"]),
            srt_path=str(saved_outputs["srt"]) if "srt" in saved_outputs else None,
        )
        if job.index_on_completion:
            try:
                append_single_job_index(job)
            except Exception as exc:
                update_job(job.id, message=f"Transcript is ready, but the index could not be updated: {exc}")
    except Exception as exc:
        update_job(job.id, status="error", error=str(exc), message="Could not complete the job", progress=100)


def start_job_worker(job_id: str) -> None:
    thread = threading.Thread(target=process_job, args=(job_id,), daemon=True)
    thread.start()


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
            batch.message = f"Processed {index + 1} of {len(batch.urls)}"

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
                        "video_id": media_id(finished_job.url),
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
        update_batch(batch.id, message="Preparing consolidated results")
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
