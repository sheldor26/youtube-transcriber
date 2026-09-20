from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union


IGNORED_TRANSCRIPT_PREFIXES = (
    "material-editorial-",
    "consolidated-summary-",
    "batch-state-",
)


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    output_dir TEXT NOT NULL,
    manufacturer_text TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS transcripts (
    id TEXT PRIMARY KEY,
    video_id TEXT REFERENCES videos(video_id) ON UPDATE CASCADE ON DELETE SET NULL,
    path TEXT NOT NULL COLLATE NOCASE UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    model_size TEXT NOT NULL DEFAULT '',
    checksum TEXT NOT NULL,
    word_count INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    imported_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS transcripts_video_idx ON transcripts(video_id);

CREATE TABLE IF NOT EXISTS project_transcripts (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    transcript_id TEXT NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
    added_at INTEGER NOT NULL,
    PRIMARY KEY (project_id, transcript_id)
);

CREATE INDEX IF NOT EXISTS project_transcripts_project_idx ON project_transcripts(project_id, added_at DESC);
"""


class LibraryError(ValueError):
    pass


def _aliases(values: str) -> List[str]:
    seen = set()
    aliases = []
    for value in re.split(r"[,;\n]", values):
        cleaned = value.strip()
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            aliases.append(cleaned)
    return aliases


def _file_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _word_count(path: Path) -> int:
    text = path.read_text(encoding="utf-8", errors="ignore")
    return len(re.findall(r"\w+", text))


def _normalized_path(value: Union[str, Path]) -> Path:
    return Path(value).expanduser().resolve()


class LibraryStore:
    """Persistent product research library without owning the original TXT files."""

    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            connection.execute("INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', '1')")

    def schema_version(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        return int(row["value"]) if row else 0

    def create_project(self, name: str, aliases: str, output_dir: str, manufacturer_text: str = "") -> Dict[str, Any]:
        cleaned_name = name.strip()
        if not cleaned_name:
            raise LibraryError("Enter a product or research project name.")
        directory = _normalized_path(output_dir)
        now = int(time.time())
        project_id = uuid.uuid4().hex[:12]
        try:
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO projects (id, name, aliases_json, output_dir, manufacturer_text, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (project_id, cleaned_name, json.dumps(_aliases(aliases), ensure_ascii=False), str(directory), manufacturer_text.strip(), now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise LibraryError("A research project with this name already exists.") from exc
        return self.get_project(project_id)

    def list_projects(self) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT p.*, COUNT(pt.transcript_id) AS transcript_count
                FROM projects p
                LEFT JOIN project_transcripts pt ON pt.project_id = p.id
                GROUP BY p.id
                ORDER BY p.updated_at DESC, p.name COLLATE NOCASE
                """
            ).fetchall()
        return [self._project_row(row) for row in rows]

    def get_project(self, project_id: str) -> Dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT p.*, COUNT(pt.transcript_id) AS transcript_count
                FROM projects p
                LEFT JOIN project_transcripts pt ON pt.project_id = p.id
                WHERE p.id = ?
                GROUP BY p.id
                """,
                (project_id,),
            ).fetchone()
        if not row:
            raise LibraryError("That research project was not found.")
        return self._project_row(row)

    def project_transcripts(self, project_id: str) -> List[Dict[str, Any]]:
        self.get_project(project_id)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT t.*, pt.added_at
                FROM project_transcripts pt
                JOIN transcripts t ON t.id = pt.transcript_id
                WHERE pt.project_id = ?
                ORDER BY pt.added_at DESC, t.title COLLATE NOCASE
                """,
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def project_paths(self, project_id: str) -> List[Path]:
        paths = []
        for transcript in self.project_transcripts(project_id):
            path = Path(transcript["path"])
            if path.exists() and path.is_file():
                paths.append(path)
        return paths

    def scan_directory(self, directory: str, filename_filter: str = "") -> List[Dict[str, Any]]:
        root = _normalized_path(directory)
        if not root.exists() or not root.is_dir():
            raise LibraryError("The transcript folder does not exist.")

        terms = [term.casefold() for term in filename_filter.split() if term.strip()]
        indexed = self._legacy_index(root)
        candidates = []
        for path in sorted(root.glob("*.txt"), key=lambda item: item.name.casefold()):
            if path.name.startswith(IGNORED_TRANSCRIPT_PREFIXES):
                continue
            if terms and not all(term in path.name.casefold() for term in terms):
                continue
            metadata = indexed.get(str(path.resolve()), {})
            candidates.append(
                {
                    "path": str(path.resolve()),
                    "filename": path.name,
                    "title": metadata.get("title") or path.stem,
                    "video_id": metadata.get("video_id") or "",
                    "source_url": metadata.get("url") or "",
                    "source_kind": metadata.get("source") or "",
                    "size": path.stat().st_size,
                }
            )
        return candidates

    def import_paths(self, project_id: str, directory: str, selected_paths: Iterable[str], filename_filter: str = "") -> Dict[str, Any]:
        project = self.get_project(project_id)
        candidates = self.scan_directory(directory, filename_filter)
        allowed = {item["path"]: item for item in candidates}
        selected = list(dict.fromkeys(selected_paths))
        if not selected:
            raise LibraryError("Select at least one transcript to import.")
        invalid = [path for path in selected if path not in allowed]
        if invalid:
            raise LibraryError("A selected file is no longer in the scanned folder. Refresh the list and try again.")

        added = 0
        already_in_project = 0
        for path_string in selected:
            result = self.register_transcript(project_id, Path(path_string), allowed[path_string])
            if result["added_to_project"]:
                added += 1
            else:
                already_in_project += 1

        return {
            "project": self.get_project(project_id),
            "requested": len(selected),
            "added": added,
            "already_in_project": already_in_project,
            "output_dir": project["output_dir"],
        }

    def register_transcript(self, project_id: str, path: Path, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.get_project(project_id)
        resolved_path = _normalized_path(path)
        if not resolved_path.exists() or not resolved_path.is_file() or resolved_path.suffix.lower() != ".txt":
            raise LibraryError("The selected transcript no longer exists or is not a TXT file.")
        metadata = metadata or {}
        now = int(time.time())
        title = str(metadata.get("title") or resolved_path.stem).strip()
        source_url = str(metadata.get("source_url") or metadata.get("url") or "").strip()
        video_id = str(metadata.get("video_id") or "").strip() or None
        source_kind = str(metadata.get("source_kind") or metadata.get("source") or "").strip()
        language = str(metadata.get("language") or "").strip()
        model_size = str(metadata.get("model_size") or "").strip()
        checksum = _file_checksum(resolved_path)
        word_count = _word_count(resolved_path)

        with self.connect() as connection:
            if video_id:
                connection.execute(
                    """
                    INSERT INTO videos (video_id, url, title, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(video_id) DO UPDATE SET
                        url = CASE WHEN excluded.url <> '' THEN excluded.url ELSE videos.url END,
                        title = CASE WHEN excluded.title <> '' THEN excluded.title ELSE videos.title END,
                        updated_at = excluded.updated_at
                    """,
                    (video_id, source_url, title, now, now),
                )

            existing = connection.execute("SELECT id FROM transcripts WHERE path = ?", (str(resolved_path),)).fetchone()
            transcript_id = existing["id"] if existing else uuid.uuid4().hex[:12]
            connection.execute(
                """
                INSERT INTO transcripts (
                    id, video_id, path, title, source_url, source_kind, language, model_size,
                    checksum, word_count, created_at, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    video_id = COALESCE(excluded.video_id, transcripts.video_id),
                    title = CASE WHEN excluded.title <> '' THEN excluded.title ELSE transcripts.title END,
                    source_url = CASE WHEN excluded.source_url <> '' THEN excluded.source_url ELSE transcripts.source_url END,
                    source_kind = CASE WHEN excluded.source_kind <> '' THEN excluded.source_kind ELSE transcripts.source_kind END,
                    language = CASE WHEN excluded.language <> '' THEN excluded.language ELSE transcripts.language END,
                    model_size = CASE WHEN excluded.model_size <> '' THEN excluded.model_size ELSE transcripts.model_size END,
                    checksum = excluded.checksum,
                    word_count = excluded.word_count,
                    imported_at = excluded.imported_at
                """,
                (
                    transcript_id,
                    video_id,
                    str(resolved_path),
                    title,
                    source_url,
                    source_kind,
                    language,
                    model_size,
                    checksum,
                    word_count,
                    now,
                    now,
                ),
            )
            transcript = connection.execute("SELECT id FROM transcripts WHERE path = ?", (str(resolved_path),)).fetchone()
            mapping = connection.execute(
                "SELECT 1 FROM project_transcripts WHERE project_id = ? AND transcript_id = ?",
                (project_id, transcript["id"]),
            ).fetchone()
            if not mapping:
                connection.execute(
                    "INSERT INTO project_transcripts (project_id, transcript_id, added_at) VALUES (?, ?, ?)",
                    (project_id, transcript["id"], now),
                )
            connection.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))

        return {"transcript_id": transcript_id, "added_to_project": not bool(mapping)}

    def _legacy_index(self, directory: Path) -> Dict[str, Dict[str, str]]:
        metadata: Dict[str, Dict[str, str]] = {}
        index_files = [directory / "transcription-index.csv", *directory.glob("batch-index-*.csv")]
        for index_file in index_files:
            if not index_file.exists():
                continue
            try:
                with index_file.open("r", encoding="utf-8", newline="") as handle:
                    for row in csv.DictReader(handle):
                        transcript_path = (row.get("transcript_path") or "").strip()
                        if not transcript_path:
                            continue
                        resolved = _normalized_path(transcript_path)
                        if resolved.parent != directory or resolved.suffix.lower() != ".txt":
                            continue
                        current = metadata.setdefault(str(resolved), {})
                        for key in ("video_id", "title", "url", "source"):
                            value = (row.get(key) or "").strip()
                            if value:
                                current[key] = value
            except (OSError, csv.Error):
                continue
        return metadata

    @staticmethod
    def _project_row(row: sqlite3.Row) -> Dict[str, Any]:
        project = dict(row)
        try:
            project["aliases"] = json.loads(project.pop("aliases_json"))
        except (TypeError, json.JSONDecodeError):
            project["aliases"] = []
            project.pop("aliases_json", None)
        return project
