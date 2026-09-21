from __future__ import annotations

from pathlib import Path

from app.library import LibraryStore

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
JOBS_DIR = DATA_DIR / "jobs"
DOWNLOADS_DIR = DATA_DIR / "downloads"

JOBS_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

library = LibraryStore(DATA_DIR / "library.sqlite3")

ALLOWED_MODELS = {"tiny", "base", "small", "medium"}
ALLOWED_LANGUAGES = {"auto", "es", "en", "pt", "fr", "de"}
ALLOWED_CHANNEL_FILTERS = {"all", "newest", "oldest", "most_viewed", "most_liked"}
