# Architecture

> How this project is built, and the reasoning that is too structural to live
> in a code comment. If a section here is longer than a screen, it probably
> wants to be a `DECISIONS.md` entry instead.

## Shape

Everything the app does lives in `app/`. `main.py` is an 11-line entry point:
it creates the FastAPI instance, mounts `static/`, includes the router, and
nothing else. The modules a newcomer has to understand:

- `app/routes.py` — the entire HTTP surface. It parses form data, starts work,
  and returns JSON or the one template. It owns no logic of its own.
- `app/youtube.py` — one video at a time: identifying a URL, fetching its
  info through `yt-dlp` with the retry wrapper for short-lived network
  failures, picking a caption track, and downloading audio for the Whisper
  fallback.
- `app/discovery.py` — many videos at once: channel listing and YouTube
  search, the background metadata enrichment that fills in view counts and
  duration for a batch of results, and the filters applied to them. Imports
  `ydl_flat_options` from `youtube.py`; `youtube.py` does not import from it.
- `app/claude_academy.py` — fetching and validating the public Claude Academy
  webinar catalog. Imports `media_id` / `media_platform` from `youtube.py`.
- `app/transcription.py` — producing text for one video: reuse the caption
  track when there is one, otherwise fall back to local `faster-whisper`.
- `app/pipeline.py` — batch orchestration. The worker thread that walks a list
  of links, calls transcription per item, writes the output files, and keeps
  the job state on disk so an interrupted batch can resume.
- `app/library.py` — the SQLite record of what has already been transcribed,
  which is what makes "skip videos already done" possible across runs.

The supporting cast: `models.py` (the `Job`/`Batch` dataclasses and their
persistence), `content.py` (post-processing of finished text — the CSV index,
the extractive summary), `config.py` (paths and allow-lists), `utils.py`
(filename and text helpers), `templating.py` (the Jinja environment).

`youtube.py`, `discovery.py` and `claude_academy.py` are the only modules that
construct a `YoutubeDL` or import `yt_dlp` directly — `routes.py` and
`transcription.py` call into them instead. `youtube.py` sits underneath the
other two and imports neither, which is what keeps this a one-way dependency
rather than a cycle.

The UI is two files: `app/templates/index.html` and `app/static/styles.css`.
One page, four views, no build step and no JavaScript framework.

## Data

- **Transcripts** are the only real output. They are written into the output
  folder the user chose, one subfolder per topic, TXT by default and SRT on
  request. Nothing else in the project is precious.
- **`data/jobs/<id>.json`** — one state file per batch, owned by `models.py`
  and updated by `pipeline.py`. This is what makes a batch resumable: the
  process can die and the next run reads the file back.
- **`data/library.sqlite3`** — owned by `library.py`. An index of what has
  already been transcribed. Derived, not precious: deleting it costs a
  re-transcription, never a transcript.
- **`data/downloads/`** — audio pulled for the Whisper fallback, deleted once
  the transcription finishes.
- All of `data/` is gitignored. A fresh clone starts with an empty library and
  no jobs, and that is the intended state.
- The disk copy under `data/jobs/<id>/` outlives the in-memory `Job`: `models.
  prune_stale_jobs()` evicts finished jobs from the `jobs` dict after an hour,
  but never touches the directory, because `routes.py`'s `get_job()` and
  `download()` read it directly whenever a job_id isn't in the dict. Deleting
  that directory would break the very fallback pruning relies on.

## Boundaries

- `main.py` knows about FastAPI and about `routes`. It does not know what the
  app does.
- `routes.py` is the only module that raises `fastapi.HTTPException`. Every
  other module raises plain Python exceptions (`ValueError`,
  `FileNotFoundError`, ...); `routes.py` catches and translates them to a
  status code at the edge. See `DECISIONS.md` D-0011.
- `transcription.py` is the only module that imports `faster-whisper`, and it
  imports it **inside the function that needs it**, so the app starts — and
  serves caption-only work — without loading a model or even having one.
- `library.py` does not import any other app module. It is a store, and it
  stays replaceable.
- `models.py` imports `app.youtube` lazily inside two functions. That is
  deliberate: `youtube.py` imports `update_job` from `models.py`, and the
  deferred import is what keeps the cycle from being an import-time crash.

## Conventions

- Every module starts with `from __future__ import annotations` and is fully
  type-hinted at the signature level.
- Files are written through `atomic_write_text` in `utils.py`, never with a
  bare `open(...).write(...)`. A killed process must not leave half a
  transcript that looks complete.
- Anything the user can pass that reaches the filesystem goes through
  `sanitize_filename` / `unique_path`. Nothing builds a path by concatenation.
- Long-running work runs on a thread and reports progress by updating the job
  state; routes never block on it.
- `config.py` holds the allow-lists (`ALLOWED_LANGUAGES`, `ALLOWED_MODELS`).
  Validation of user input happens against those, in `routes.py`, at the edge.
- The record — this file, the logbook, code comments, commit messages — is
  written in English.
