# State

updated: 2026-09-20

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

## In flight

- This logbook. It was installed today, after the refactor and the redesign
  were already committed, so `MISTAKES.md` and `LEARNINGS.md` start from the
  things that are still true rather than from the full history.

## Next

1. Decide what the Claude Academy import becomes in a public repository: it is
   currently a hardcoded host and catalog URL in `app/youtube.py`. Either
   generalise it to a configurable catalog source, or keep it and document why.
2. Add CI. There are 441 lines of tests and nothing runs them on push.
3. Narrow the yt-dlp surface: `routes.py` and `transcription.py` both construct
   `YoutubeDL` directly, which was supposed to be `youtube.py`'s job alone.

## Known rough edges

- `pipeline.py` and `youtube.py` both raise `fastapi.HTTPException`, so the
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
