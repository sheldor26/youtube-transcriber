# State

updated: 2026-09-21

> A snapshot of where this project is right now — the file a new session reads
> first. It answers "what exists, what is half-done, what is next".
>
> It is not a diary. When this file starts telling stories, the stories belong
> in the logbook. `doctor` enforces that with a line budget.

## Shipped

- Single-video transcription: reuse the YouTube caption track when one exists,
  fall back to local `faster-whisper` when it does not. TXT always, SRT on
  request.
- Batch transcription from a pasted list of links, resumable from its state
  file after an interrupted run.
- Link extraction from a channel (oldest, newest, most-viewed, most-liked) and
  YouTube search with filters for type, duration, upload date and captions.
- Import of the public Claude Academy on-demand webinar catalog into a batch.
- A SQLite library that skips videos already transcribed, and a per-topic
  output folder layout.
- A local, extractive consolidated summary for a finished batch.
- One-page UI with four views (Transcribe / Channel / Topic / Batch), dark and
  light, no build step and no webfonts.
- `app/main.py` reduced from 2,101 lines to 11 by splitting the application
  into modules by responsibility — see [ARCHITECTURE.md](ARCHITECTURE.md).
- Public on GitHub with a README, logo and real screenshots.
- This logbook (`CLAUDE.md`, `STATE.md`, `MISTAKES.md`, `LEARNINGS.md`,
  `DECISIONS.md`, the `.bitacora/` CLI, and the session-start/-end hooks that
  invoke it). Seeded from the work already done, so it starts from the things
  that are still true rather than from the full history.
- A GitHub social-preview image at `assets/social-preview.png` (1280x640,
  recomposed for that aspect ratio rather than stretched from the README
  banner — see `D-0007`), uploaded in the repo's Settings.
- CI (`.github/workflows/ci.yml`): runs the test suite (24 tests) and fails
  the build if any `app/*.py` module exceeds 500 lines (`M-0003`) or if any
  file outside `content.py` contains an accented Spanish character (`M-0004`,
  partial coverage — see that entry). Verified green on a real push, not just
  locally.
- The yt-dlp surface is now `youtube.py` (one video), `discovery.py` (many
  videos: search, channel listing, metadata enrichment) and
  `claude_academy.py` (the webinar catalog) — split by responsibility ahead
  of the CI ceiling, not after hitting it. `routes.py` and `transcription.py`
  still construct no `YoutubeDL` of their own; they call into these three.
  See `D-0009`.
- The Claude Academy host and catalog URL stay hardcoded module constants in
  `app/claude_academy.py`, not configurable — a one-line comment there points
  at `D-0008`, so the reasoning survives the next reader without them having
  to find the logbook first.
- The stylesheet cache-bust (`styles.css?v=...`) is computed from the file's
  mtime in `routes.py`'s `/` handler, not hand-bumped in `index.html` — see
  `D-0010`. Still no build step, no bundler.
- Neither `pipeline.py` nor `discovery.py` import `fastapi` any more.
  `batch_from_state()` and `extract_channel_videos()` raise plain
  `FileNotFoundError` / `ValueError`; `routes.py` translates them to
  `HTTPException` at its two call sites. `filter_type` is now validated
  against `config.ALLOWED_CHANNEL_FILTERS` at the edge, same as `language`
  and `model_size` already were. See `D-0011`.
- The local `.venv` runs Python 3.12, matching the README's 3.10+ requirement
  — it ran on the pre-existing 3.9.6 all session until now. See `D-0012`.
- Four leftover Spanish user-facing strings (the `Job`/`Batch` default
  `message`, two error messages) translated to English. See `M-0004`.
- `POST /api/batches/{id}/cancel` and a "Cancel batch" button in the Batch
  view. Finishes the cancel feature `pipeline.py`'s loop already had a check
  for but nothing ever triggered — see `D-0013`. Cancelling stops the batch
  before its next video, not mid-transcription.
- `models.py`'s `jobs` dict now prunes itself: `prune_stale_jobs()`, called
  once per job in `process_job()`, evicts entries that are both terminal and
  older than an hour. Safe because `get_job()` / `download()` already read
  `JOBS_DIR/<id>/job.json` from disk when a job isn't in the dict — verified
  by restarting the server (which empties the dict the same way pruning
  does) and confirming both endpoints still served a finished job correctly.
  See `D-0014`. `batches` is not pruned — no disk-fallback path exists for
  it the way `jobs` has one, and it grows far slower (one entry per batch,
  not per video).

## Next

Found during a review pass, not yet acted on:

1. `content.py`'s `build_consolidated_summary()` / `build_knowledge_base()`
   compare every candidate sentence against every already-selected one with
   `difflib.SequenceMatcher.ratio()`, an O(n²) cost with no ceiling. Fine for
   a handful of videos; a batch of hundreds could make this the slowest part
   of the whole run.
2. `data/jobs/<id>/` directories (a `job.json` plus a duplicate `.txt`) are
   never removed, for every job ever run — the same leak the audio cleanup
   fix earlier this session solved, but for everything except the audio.
   Deliberately not touched by `D-0014`: deleting these would break the
   disk-fallback that pruning the in-memory `jobs` dict now depends on.
3. `models.py`'s `batches` dict still grows for the process's lifetime (see
   `D-0014`'s Consequences for why it wasn't pruned alongside `jobs`).

## Known rough edges

- `models.py` imports `app.youtube` inside two functions to break an import
  cycle. It works and it is deliberate, but it is the kind of thing a reader
  will "fix" without realising. Documented in ARCHITECTURE.md.
- The library database and job state live in `data/`, which is gitignored in
  full. A contributor cannot reproduce a reported bug from a state file
  without being sent one.
- `routes.py`'s `download()` raises three Spanish-language `HTTPException`
  details ("Formato no disponible.", "Archivo no disponible." x2) — the same
  class of bug as `M-0004`, found while verifying this fix, but with no
  accent, so the CI check added for `M-0004` does not catch it either.
