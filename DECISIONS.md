# Decisions

> Architecture decisions, lightweight. One entry per choice that would be
> expensive to reverse, or that a future reader would otherwise second-guess.
>
> The point is not the decision — it is the *context*, so that when the context
> changes the decision can be revisited honestly. Newest first.
>
> Add entries with: `node .bitacora/cli.mjs new decision "Title" --tags area`

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
