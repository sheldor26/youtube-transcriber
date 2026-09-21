# Decisions

> Architecture decisions, lightweight. One entry per choice that would be
> expensive to reverse, or that a future reader would otherwise second-guess.
>
> The point is not the decision — it is the *context*, so that when the context
> changes the decision can be revisited honestly. Newest first.
>
> Add entries with: `node .bitacora/cli.mjs new decision "Title" --tags area`

<!-- bitacora:entry
id: D-0016
date: 2026-09-21
tags: [architecture, memory]
-->
### Give batches a canonical disk copy so they can be pruned too

**Context.** `D-0014` pruned `jobs` but explicitly left `batches` alone: `get_batch()` had
no disk-fallback the way `get_job()` does, because a batch's state file
lives at `<output_dir>/batch-state-<id>.json` — a path only known once the
`Batch` object already exists, not derivable from the id alone the way
`JOBS_DIR/<job_id>/job.json` is.

**Decision.** Gave every batch a second, fixed-location copy: `persist_batch_state()` now
writes the same snapshot to both the existing `<output_dir>` path (still
needed for "Resume batch", which reads a path the user can find and paste)
and `JOBS_DIR/batches/<id>.json`. `get_batch()` falls back to that canonical
copy exactly like `get_job()` falls back to `JOBS_DIR/<id>/job.json`. With
that in place, `prune_stale_batches()` mirrors `prune_stale_jobs()` exactly:
evicts from the `batches` dict only when a batch is terminal
(`done`/`done_with_errors`/`error`/`cancelled`) and older than
`BATCH_RETENTION_SECONDS` (1 hour), called once per batch at the top of
`process_batch()`.

**Consequences.** `batches` now self-limits the same way `jobs` does, closing the second half
of `D-0014`. The cost is a second write on every `persist_batch_state()`
call and a second on-disk copy of every batch's state for as long as
`data/jobs/batches/` isn't cleaned up (small JSON, not the gigabyte-scale
audio `M-0005` was about). Verified the same way as `D-0014`: a unit test
pins `prune_stale_batches()`'s age/status logic, another creates a batch
through the real API, deletes it from `models.batches` to simulate a prune,
and confirms `GET /api/batches/{id}` still returns it; and live against the
real server — ran a batch to completion, restarted the process (which empties
`batches` the same way pruning would), and confirmed the same endpoint still
served the finished batch from `JOBS_DIR/batches/<id>.json`.

<!-- bitacora:entry
id: D-0015
date: 2026-09-21
tags: [performance, content]
-->
### Precompute duplicate-detection signatures instead of per-pair tokenizing

**Context.** Found during a review pass: `build_consolidated_summary()` and
`build_editorial_material()` in `content.py` both dedup candidate sentences
by calling `sentences_are_duplicate(a, b)` for every pair, and that function
re-ran `claim_markers()` and `summary_tokens()` — each a regex pass over the
sentence — from scratch on *every single call*, even though both sentences
had already been tokenized once when they were added to the candidate list.
The pairwise structure is inherently O(n²); this made each of those n²
comparisons redo work that was already sitting in the candidate dict.

**Decision.** Split the function in two: `duplicate_signature(sentence, tokens=None)`
computes the token set, negation/number markers, and lowercased text once
(reusing an already-computed token list when the caller has one), and
`signatures_are_duplicate(sig_a, sig_b)` runs the exact same comparison
`sentences_are_duplicate()` always did, just against precomputed signatures.
`sentences_are_duplicate(a, b)` still exists as a thin wrapper for the one
existing test and any external caller that only has raw strings. Both hot
functions now build each sentence's (and the manufacturer text's) signature
once and reuse it across every comparison.

**Consequences.** No algorithmic complexity change — still O(n²) comparisons — but each
comparison is now several set operations instead of four regex passes.
Benchmarked with a synthetic batch of distinct (non-near-duplicate)
sentences, which is the case that actually stresses this path:
`build_consolidated_summary()` on 300 videos x 40 sentences (12,000
sentences) went from 112s to 11.7s, roughly 9-10x, consistent from 50
videos up. Verified as behavior-preserving, not just faster: ran the
function on a fixed random-seeded dataset before and after the change (via
`git stash`) and diffed the two output files byte-for-byte — identical.
What this does *not* fix: the comparison count itself is still unbounded in
the number of qualifying sentences across a batch, particularly in the
`source_count` step (`len(selected) x len(all_sentences)`). Capping that
would change which sentences get counted as duplicates, i.e. change output,
not just speed — a different, riskier change left for if the 9-10x here
turns out not to be enough.

<!-- bitacora:entry
id: D-0014
date: 2026-09-21
tags: [architecture, memory]
-->
### Prune the jobs dict by age, scope batches out for now

**Context.** Found during a review pass: `models.py`'s `jobs` and `batches` dicts are
never pruned, growing for the life of the process. `jobs` is the bigger
driver — one batch creates a `Job` entry per video. The obvious fix, evicting
a job the moment it reaches a terminal status, breaks `process_batch()`:
right after `process_job(job_id)` returns, it reads `jobs[job_id]` to build
the batch's result row, so evicting inside that same call would raise
`KeyError` on the very next line.

**Decision.** `prune_stale_jobs()` in `models.py` only evicts jobs that are both terminal
(`done`/`error`/`skipped`) *and* older than `JOB_RETENTION_SECONDS` (1 hour,
measured from `created_at`). Called once per job, at the top of
`process_job()` — not on every `update_job()` progress tick, and not on a
timer. The 1-hour floor is what makes the `process_batch()` read safe: a job
that just finished is never old enough to be pruned. `batches` was left
alone: `get_batch()` has no disk-fallback path the way `get_job()` and
`download()` do, and building one isn't as simple, because a batch's state
file lives under the user's chosen `output_dir`, not a fixed location
derivable from the batch id the way `JOBS_DIR/<job_id>/` is.

**Consequences.** The `jobs` dict now self-limits to roughly an hour of activity instead of
growing for the process's entire lifetime, with no new thread, timer, or
broker (`D-0004`). It relies on `get_job()` and `download()`'s existing
disk-fallback reads, which were already there for a different reason
(surviving a server restart) — confirmed by restarting the server after a
job finished (which empties `jobs` exactly like pruning would) and checking
that both endpoints still served the right data straight from
`JOBS_DIR/<id>/job.json` and the copied `.txt`. `batches` growing unbounded
remains open, now with a clearer reason why it's harder than `jobs`.

<!-- bitacora:entry
id: D-0013
date: 2026-09-21
tags: [architecture, ui]
-->
### Add a cancel-batch endpoint that only sets a status flag

**Context.** Found during a review pass: `pipeline.py`'s batch loop already checked
`if current_batch.status == "cancelled": return` between videos, but nothing
anywhere ever set a batch to `"cancelled"` — dead code for a feature that
was never finished. A batch can run for hours (`D-0004`), with no way to
stop one short of killing the whole process.

**Decision.** Added `POST /api/batches/{id}/cancel`, which does nothing but flip
`batch.status` to `"cancelled"` (404 if the batch doesn't exist, 409 if it
isn't queued/running) and persist that. No change to `pipeline.py`: the
existing check was already the right mechanism, just never wired to
anything. A "Cancel batch" button was added to the Batch view, shown only
while `status` is `queued`/`running`.

**Consequences.** Cancelling stops the batch before its *next* video, not mid-transcription —
a video already running when cancel is clicked finishes normally and counts
toward the results. That is a deliberate limit, not an oversight: the app
runs work on plain threads with no cancellation primitive (`D-0004`), and
killing a thread mid-Whisper-run from Python is not something to reach for.
Verified live end-to-end, not just via the endpoint: submitted a real batch
from the browser UI, clicked "Cancel batch" while video 1 of 3 was
transcribing, and confirmed video 1 finished, video 2 never started, the
button hid itself, and a cancel on an already-finished or unknown batch
returns 409/404.

<!-- bitacora:entry
id: D-0012
date: 2026-09-21
tags: [environment, verification]
-->
### Upgrade the local dev venv to Python 3.12, matching the 3.10+ requirement

**Context.** STATE.md's last rough edge: the README asks for Python 3.10+, but every test
run and manual verification this session had actually run on the 3.9.6
`.venv` that predated it. CI already ran on 3.10 and had been green on every
push, so the gap was between the README and the local dev environment, not
between the README and reality — but it meant nobody had actually run this
app locally on a version it claims to require.

**Decision.** Backed up the existing `.venv` (not deleted, in case the newer Python broke a
dependency) and recreated it with the Python 3.12 already available via
Homebrew — newer than 3.10, not an exact match, but within the stated
requirement and the newest interpreter on this machine. Reinstalled
`requirements.txt` from scratch rather than upgrading in place.

**Consequences.** The local environment now matches what the README promises and what CI has
been checking all along, closing the gap for real instead of just re-wording
the rough edge. Verified past "pip install succeeded": all 22 tests pass, and
a live run of search, channel extraction, the Claude Academy import, and a
full audio-download-plus-`faster-whisper` transcription all completed
normally on the new interpreter — the one dependency most likely to break on
a Python bump. The old `.venv` was removed once all of that passed; recreating
it means re-downloading everything `requirements.txt` lists, including the
Whisper model on first use.

<!-- bitacora:entry
id: D-0011
date: 2026-09-21
tags: [architecture]
-->
### Translate plain exceptions to HTTPException at the routes.py edge

**Context.** STATE.md carried a rough edge: `pipeline.py`'s `batch_from_state()` and
`discovery.py`'s `extract_channel_videos()` both raised `fastapi.HTTPException`
directly, so the HTTP concern leaked past the router. It was tolerated because
the stated alternative — a domain error type plus a translation layer — looked
like more machinery than the app's single front end justified.

**Decision.** That framing overstated the cost. No new error type was needed: the two
functions now raise plain `FileNotFoundError` / `ValueError`, and `routes.py`
catches and translates them to the right status code at its two call sites.
Separately, `extract_channel_videos()`'s `ValueError` for an unknown
`filter_type` turned out to be reachable in practice — `routes.py` validated
`language` and `model_size` against `config.py` allow-lists at the edge but
never validated `filter_type` the same way — so `ALLOWED_CHANNEL_FILTERS` was
added and checked in `extract_channel()`, matching the existing convention.

**Consequences.** `pipeline.py` and `discovery.py` no longer import `fastapi` at all — an
`import fastapi` in either now means a route's concern actually leaked in,
not that it always did. The two error messages sent to the client changed
text (now in English, matching the rest of the app's user-facing strings,
where `discovery.py`'s was a Spanish leftover) and the invalid-filter case
now fails before doing any work instead of after fetching the channel
listing. Verified live: a missing resume-state path, an unparseable one, a
valid one, and an invalid channel filter each still return the right status
code, over HTTP, not just via the unit tests.

<!-- bitacora:entry
id: D-0010
date: 2026-09-21
tags: [ui, architecture]
-->
### Compute the stylesheet cache-bust from mtime, not by hand

**Context.** D-0005 accepted "cache-busted by hand" as the cost of shipping the UI with no
build step: `index.html` hardcoded `styles.css?v=20260920-5`, bumped manually
after every CSS edit, and forgetting to bump it is exactly the kind of mistake
M-0002 already happened once (a stale stylesheet looked like a CSS bug).

**Decision.** The `/` route now stats `styles.css` and passes its mtime as `static_version`
into the template, which renders `styles.css?v={{ static_version }}`. No
bundler, no build step, no hash file — one `Path.stat()` call already paid
for by the request FastAPI is already handling.

**Consequences.** The cache-bust is now correct by construction: it changes exactly when the
file's mtime changes, with nothing to remember and nothing to forget. Verified
against the served page, not the template — fetched `/` and confirmed the
query string matches `stat -f %m`, then touched the file and confirmed it
moved with no edit to `index.html`. The cost is one `stat()` syscall per
request to `/`, which is negligible next to the request FastAPI is already
serving.

<!-- bitacora:entry
id: D-0009
date: 2026-09-21
tags: [structure, architecture]
-->
### Split youtube.py into youtube/discovery/claude_academy by responsibility

**Context.** `app/youtube.py` had reached the 500-line CI ceiling from M-0003 with zero
lines of slack, right after the previous session narrowed the yt-dlp surface
into it. Three responsibilities lived in it at once: one-video operations
(identify, fetch info, captions, audio download), many-video listing/search/
enrichment, and the unrelated Claude Academy catalog fetch.

**Decision.** Split preventively, before the ceiling forced an argument mid-feature, into
`discovery.py` (many videos) and `claude_academy.py` (the catalog), keeping
`youtube.py` as the one-video core. `goldcast_webinar_id()` stayed in
`youtube.py` rather than moving to `claude_academy.py` with the rest of the
Goldcast code: `claude_academy.py` already needs `media_id()`, which calls
`goldcast_webinar_id()`, so moving it too would have made `youtube.py` and
`claude_academy.py` import each other. `discovery.py` importing
`ydl_flat_options` from `youtube.py`, and not the reverse, was the same
one-way-dependency choice.

**Consequences.** `youtube.py` dropped to 224 lines, discovery.py to 249, claude_academy.py to
48 — all with real headroom under the ceiling again. Verified as a pure move:
an `ast` symbol-table comparison confirmed all 26 top-level functions landed
in exactly one of the three files, all 22 tests pass, and a live run of
search, channel extraction, the Claude Academy import, and a full
caption-less transcription all completed normally. The cost is one more
import to trace for a reader following how a URL becomes a transcript:
`routes.py` now pulls from three modules instead of one.

<!-- bitacora:entry
id: D-0008
date: 2026-09-21
tags: [architecture, security]
-->
### Keep the Claude Academy host and catalog URL hardcoded, not configurable

**Context.** `GOLDCAST_ON_DEMAND_HOST` and `CLAUDE_ACADEMY_WEBINARS_URL` in `app/youtube.py`
are literal strings. STATE.md carried this as an open question: generalise
them into a configurable catalog source, or keep them and say why. D-0006
had already decided *which* host to accept; this is the separate question of
whether that choice should live in code or in a setting.

**Decision.** Left them as hardcoded module constants, with a code comment pointing at this
entry. No environment variable, no config file entry.

**Consequences.** The choice from D-0006 stays an allowlist, not a preference: nobody running
this app can point it at a different Goldcast tenant, or any other host,
without editing and redeploying the source. That is the entire point — a
configurable "catalog source" is a paywall-bypass feature with extra steps.
The cost is that if Anthropic ever moves the catalog to a new host, updating
it means a code change and a release, not a settings change.

<!-- bitacora:entry
id: D-0007
date: 2026-09-21
tags: [branding, assets]
-->
### Recompose the social preview image instead of stretching the README banner

**Context.** GitHub reads a repo's link-unfurl image ("social preview") from a fixed 1280x640
(2:1) slot, uploaded by hand in Settings — there is no API for it. The project
already had a 900x200 (4.5:1) banner SVG for the README header, built from the
same icon and palette.

**Decision.** Built a second SVG at the exact 1280x640 canvas GitHub wants, with the icon and
wordmark recentred for that shape, rather than feeding the 4.5:1 banner into an
image tool and letting it stretch or letterbox to fit.

**Consequences.** The preview reads correctly at the small size link cards actually render it at,
with no cropped icon or squashed text. The cost is two SVGs to keep in sync
with the palette instead of one: a change to the accent color or wordmark now
has to be made in both `assets/logo.svg` and `assets/social-preview.svg`.

<!-- bitacora:entry
id: D-0006
date: 2026-09-21
tags: [architecture]
-->
### Accept only Anthropic's public on-demand webinar host

**Context.** Public repositories get read by people looking for a way around a paywall. The
app can already fetch the Claude Academy catalog, and that catalog points at
recordings hosted on Goldcast. The question was how much of Goldcast to accept.

**Decision.** Only one host is accepted: anthropic.ondemand.goldcast.io, checked by exact
hostname match in goldcast_webinar_id() in app/youtube.py, and only for
recordings the public catalog already lists. The app never handles a
registration, a sign-in, or any other access control.

**Consequences.** It makes the project safe to publish and easy to explain in the README, and it
makes the rule mechanical rather than a matter of intent. It also means the
feature breaks the day Anthropic changes host, which is the correct failure:
broken and obvious beats working and over-broad.

<!-- bitacora:entry
id: D-0005
date: 2026-09-21
tags: [architecture]
-->
### Ship the UI with no build step and no webfonts

**Context.** The UI is one page served by the same process that does the transcribing, on a
machine that is sometimes offline — it is a local tool, not a website. Any
build step would have to be run before the app could be used, by someone whose
actual goal is a transcript.

**Decision.** No bundler, no framework, no webfonts. One template, one stylesheet of CSS
custom properties, and the system font stack — with a monospace stack for the
terminal-flavoured surfaces.

**Consequences.** Clone, install requirements, run uvicorn: that is the whole setup, and the UI
renders identically with no network. The cost is that the stylesheet has to be
cache-busted by hand, and that any future component work would start from
nothing. Both are cheaper than the build step at this size.

<!-- bitacora:entry
id: D-0004
date: 2026-09-21
tags: [architecture]
-->
### Keep run state in files and SQLite, with no queue or broker

**Context.** A batch can be hundreds of videos and run for hours, and the process will be
killed — a laptop closes, a terminal is quit. Resumability was a requirement.
The obvious industry answer is a queue and a worker process.

**Decision.** State lives in two plain places: one JSON file per batch under data/jobs/, and
one SQLite file for the library of finished transcripts. Work runs on threads
inside the app process. No broker, no external service.

**Consequences.** There is nothing to install and nothing to start besides the app, and a stuck
batch can be inspected or fixed with a text editor. In exchange, the app is
single-machine and single-process by construction: there is no path to
distributing the work across machines without replacing this decision.

<!-- bitacora:entry
id: D-0003
date: 2026-09-21
tags: [architecture]
-->
### Reuse the caption track first, transcribe only as a fallback

**Context.** Whisper is the impressive part, so the temptation is to run it on everything.
But most of the target material — tutorials, conference talks, webinars —
already ships with a caption track, and transcribing it locally costs minutes
of CPU to reproduce text that could be downloaded in a second.

**Decision.** The caption track is the primary path. Whisper is the fallback, used when no
usable track exists, and faster-whisper is imported inside the function that
needs it rather than at module load.

**Consequences.** Most videos finish in seconds instead of minutes, and the app starts and serves
caption-only work without a model on disk. The cost is two code paths producing
the same artifact, with different quality characteristics: caption text carries
the uploader's formatting and errors, Whisper text carries the model's.

<!-- bitacora:entry
id: D-0002
date: 2026-09-21
tags: [architecture]
-->
### Split main.py by responsibility, not by layer

**Context.** app/main.py had reached 2,101 lines holding routes, YouTube access, Whisper,
batch orchestration, persistence and text post-processing. Every change touched
the same file, and no part of it could be read in isolation.

**Decision.** Split by responsibility, not by layer: youtube.py, transcription.py,
pipeline.py, library.py, content.py, models.py. Not routers/, services/,
repositories/ — the modules are named after what they do to a video, not after
their position in an imaginary stack.

**Consequences.** A reader who wants to know how captions are picked opens one file and finds all
of it. main.py drops to 11 lines. The cost is a real import cycle between
models.py and youtube.py, resolved with two deferred imports, which is a scar
this shape leaves and a layer-based split would not have.
