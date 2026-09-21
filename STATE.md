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
- CI (`.github/workflows/ci.yml`): runs the test suite (23 tests) and fails
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

## Next

Found during a review pass, not yet acted on:

1. `pipeline.py`'s batch loop checks `if current_batch.status == "cancelled":
   return`, but nothing anywhere ever sets a batch to `"cancelled"` — no
   route, no button. Either dead code to remove, or a half-built cancel
   feature worth finishing (a batch can run for hours per `D-0004`, with no
   way to stop one short of killing the process).
2. `content.py`'s `build_consolidated_summary()` / `build_knowledge_base()`
   compare every candidate sentence against every already-selected one with
   `difflib.SequenceMatcher.ratio()`, an O(n²) cost with no ceiling. Fine for
   a handful of videos; a batch of hundreds could make this the slowest part
   of the whole run.
3. `models.py`'s `jobs` and `batches` module-level dicts are never pruned —
   every job and batch created since the process started stays in memory for
   its lifetime. Related: `data/jobs/<id>/` directories (a `job.json` plus a
   duplicate `.txt`) are never removed either, for the same reason the audio
   cleanup fix earlier this session existed — this is the same leak, for
   everything except the audio.

## Known rough edges

- `models.py` imports `app.youtube` inside two functions to break an import
  cycle. It works and it is deliberate, but it is the kind of thing a reader
  will "fix" without realising. Documented in ARCHITECTURE.md.
- The library database and job state live in `data/`, which is gitignored in
  full. A contributor cannot reproduce a reported bug from a state file
  without being sent one.
