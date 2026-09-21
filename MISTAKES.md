# Mistakes

> Every time something breaks, it gets an entry here — what happened, why it
> was possible, and the guardrail that makes it impossible to repeat.
>
> An entry without a guardrail is just a complaint. Newest first.
>
> Add entries with: `node .bitacora/cli.mjs new mistake "Title" --tags area,failure-mode`

<!-- bitacora:entry
id: M-0004
date: 2026-09-21
tags: [i18n, verification]
severity: low
-->
### Four Spanish user-facing strings shipped in an English-only app

**What happened.** `models.py`'s `Job`/`Batch` dataclasses defaulted `message` to `"Esperando
turno"` — the first thing shown for every job or batch ever created — and
three error strings in `pipeline.py` and `utils.py` were also Spanish, in an
app whose README, UI copy and every other message are English. Found by
reading the code during a review pass, not by a test or a bug report.

**Root cause.** Nothing ever asserted the language of a user-facing string. The values were
carried over from an earlier, Spanish-only version of the app and never
touched again once the rest of the UI was translated, so nothing about them
looked new or suspicious in any later diff.

**Guardrail.** Two, covering different parts of what actually leaked: a CI step (`.github/
workflows/ci.yml`) fails the build if any `app/*.py` file outside
`content.py` (which legitimately parses Spanish/Portuguese transcript text)
contains an accented character or ñ/¿/¡; and a new test,
`test_job_and_batch_default_message_is_english`, pins the exact `Job`/`Batch`
defaults. Being honest about the gap: none of the four strings that actually
leaked had an accent, so the CI step would not have caught this specific
mistake — it only catches a future *accented* leak. The regression test is
what actually covers the one root cause found.

<!-- bitacora:entry
id: M-0003
date: 2026-09-21
tags: [structure, drift]
severity: high
-->
### main.py grew to 2,101 lines before anyone split it

**What happened.** A single module accumulated routes, network access, transcription,
orchestration and persistence until it was 2,101 lines. It was split into ten
modules in one sitting, long after the point where any individual change had
stopped being reviewable.

**Root cause.** Nothing tracked the file's size, so the growth registered as a series of small
reasonable additions. There was no moment at which anyone decided main.py
should hold everything; there was just never a moment at which anyone decided
it should not.

**Guardrail.** The logbook this project now carries is the structural half. The mechanical
half: a line-count ceiling checked in CI, so that a module crossing 500 lines
fails the build and forces the split to be argued rather than deferred. Until
CI exists, the ceiling is in ARCHITECTURE.md as the stated convention.

<!-- bitacora:entry
id: M-0002
date: 2026-09-21
tags: [ui, verification]
severity: medium
-->
### A second server from an old copy of the project served stale CSS

**What happened.** The stylesheet was rewritten and the browser showed the old design. Several
minutes went into looking for a mistake in the CSS. There was none: an older
copy of the project, in a different folder, still had a uvicorn process bound
to port 8000, and it was serving its own 418-line stylesheet.

**Root cause.** Verification was done against the file on disk instead of against the bytes the
server actually returned. The two are only the same when exactly one server is
running from exactly the folder being edited, and nothing was checking either.

**Guardrail.** Verify the served artifact, not the source. Fetch the stylesheet over HTTP and
compare its length against the file on disk before concluding anything about a
visual change, and start the app on a port chosen for the session rather than
the default, so a stale process on 8000 cannot answer for it.
