from __future__ import annotations

import html
import os
import re
import threading
from pathlib import Path


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


def form_checkbox_enabled(value: str | None) -> bool:
    return value in {"on", "true", "1", "yes"}


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\n\r\t]+', " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:150] or "youtube-transcript"


def destination_dir_for(output_dir: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(output_dir))).resolve()


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
    raise RuntimeError(f"Could not generate an available name for {path.name}.")


def format_bytes(value: float | None) -> str:
    if not value:
        return "?"
    units = ["B", "KB", "MB", "GB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


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
