# Architecture

> How this project is built, and the reasoning that is too structural to live
> in a code comment. If a section here is longer than a screen, it probably
> wants to be a `DECISIONS.md` entry instead.

## Shape

Everything the app does lives in `app/`. `main.py` is an 11-line entry point:
it creates the FastAPI instance, mounts `static/`, includes the router, and
nothing else. The five directories or modules a newcomer has to understand:

- `app/routes.py` — the entire HTTP surface. It parses form data, starts work,
  and returns JSON or the one template. It owns no logic of its own.
- `app/youtube.py` — everything that talks to YouTube through `yt-dlp`:
  metadata, channel listing, search, caption tracks, audio download, and the
  retry wrapper for short-lived network failures.
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

## Boundaries

- `main.py` knows about FastAPI and about `routes`. It does not know what the
  app does.
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
