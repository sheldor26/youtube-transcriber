from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import shutil
import textwrap

import webvtt

from app.models import Job, TranscriptSegment, update_job
from app.utils import (
    atomic_write_text,
    clean_text,
    destination_dir_for,
    format_bytes,
    seconds_to_srt_time,
    unique_path,
    vtt_time_to_seconds,
)
from app.youtube import (
    download_audio_url,
    download_caption_file,
    media_platform,
    pick_caption_track,
    run_youtube_operation,
)

whisper_models: Dict[str, Any] = {}
whisper_models_lock = threading.Lock()


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


def try_captions(job: Job, info: Dict[str, Any], job_dir: Path) -> Optional[List[TranscriptSegment]]:
    track = pick_caption_track(info, job.language)
    if not track:
        return None

    update_job(job.id, progress=35, message="Downloading available captions")
    caption_path = job_dir / "captions.vtt"
    if not caption_path.exists() or caption_path.stat().st_size == 0:
        download_caption_file(track["url"], caption_path)
    segments = captions_to_segments(caption_path)
    if not segments:
        return None

    update_job(job.id, source=f"{track['source']} ({track['language']})")
    return segments


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
                message = f"Downloading audio: {format_bytes(downloaded)} of {format_bytes(total)}"
            else:
                progress = 32
                message = f"Downloading audio: {format_bytes(downloaded)}"
            update_job(job_id, progress=progress, message=message)
        elif status == "finished":
            update_job(job_id, progress=49, message="Audio downloaded; preparing Whisper")

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
        update_job(job.id, progress=49, message="Reusing previously downloaded audio")
        return cached_audio[0]

    output_template = str(job_dir / "audio.%(ext)s")
    update_job(job.id, progress=30, message="Downloading audio")

    def download() -> None:
        download_audio_url(job.url, output_template, [download_progress_hook(job.id)])

    run_youtube_operation(
        download,
        on_retry=lambda attempt, total, _exc: update_job(
            job.id,
            progress=30,
            message=f"{media_platform(job.url)} interrupted the download; retrying ({attempt + 1} of {total})",
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
                    model = WhisperModel(
                        job.model_size,
                        device="auto",
                        compute_type="int8",
                        cpu_threads=os.cpu_count() or 4,
                    )
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


def delete_downloaded_media(job_dir: Path) -> None:
    """Remove the raw audio/captions once the transcript is written; they can weigh
    gigabytes and are never read again after this point."""
    for pattern in ("audio.*", "captions.vtt"):
        for path in job_dir.glob(pattern):
            if path.is_file():
                path.unlink(missing_ok=True)


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
