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
- CI (`.github/workflows/ci.yml`): runs the 22 existing tests and fails the
  build if any `app/*.py` module exceeds 500 lines, the mechanical half of the
  guardrail `M-0003` asked for. Verified green on a real push, not just
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

## Next

Nothing queued. `app/youtube.py` (224 lines), `app/discovery.py` (249) and
`app/claude_academy.py` (48) all have headroom under the 500-line CI ceiling
again.

## Known rough edges

- `pipeline.py` and `discovery.py` both raise `fastapi.HTTPException`, so the
  HTTP concern leaks past the router. Tolerated because the alternative today
  is a domain error type plus a translation layer in `routes.py`, and the app
  has exactly one front end.
- `app/templates/index.html` hardcodes a cache-busting query string on the
  stylesheet (`styles.css?v=...`). It has to be bumped by hand after a CSS
  change. Tolerated because there is no build step to generate one, and adding
  a build step to a zero-build app costs more than it saves.
- `models.py` imports `app.youtube` inside two functions to break an import
  cycle. It works and it is deliberate, but it is the kind of thing a reader
  will "fix" without realising. Documented in ARCHITECTURE.md.
- The library database and job state live in `data/`, which is gitignored in
  full. A contributor cannot reproduce a reported bug from a state file
  without being sent one.
- The README asks for Python 3.10+; the environment this project has actually
  been run and tested in all session is 3.9.6. Nothing has broken, likely
  because every module starts with `from __future__ import annotations`. CI
  targets 3.10, matching what the README promises, not what was locally
  verified — untested until CI actually runs against a contribution.
