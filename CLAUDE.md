# youtube-transcriber

A local, self-hosted web app that turns public YouTube videos into TXT/SRT
transcripts on your own machine — for anyone who wants a searchable record of
talks, tutorials, lectures and webinars without uploading anything to a cloud
service, paying per minute, or holding an API key.

## Stack in one line

Python 3.10+, FastAPI + Jinja, yt-dlp, faster-whisper, SQLite — details in [ARCHITECTURE.md](ARCHITECTURE.md).

## Non-negotiables

1. **No new dependency without asking.** If you think one is needed, stop and say why.
2. **Simple beats clever.** Two ways to do it? Take the obvious one. Do not abstract for a future that has not arrived.
3. **Do not touch the base stack silently.** Build config, tsconfig, package manifests, CI, headers — say what and why first.
4. **Do not commit unless asked.** Show the diff and wait.
5. **Do not clean up code unrelated to the task at hand.**
6. **Write the record in English.** Talk with me in whatever
   language I use, but `CLAUDE.md`, the logbook, code comments and commit
   messages are written in English. They outlive the conversation
   and are read by people who were not in it.

## The loop

This project keeps a logbook. It is not documentation — it is working memory
that survives between sessions.

| File | What it holds | When you touch it |
| :-- | :-- | :-- |
| [STATE.md](STATE.md) | Where the project is *right now*. A snapshot, not a diary. | End of every session |
| [MISTAKES.md](MISTAKES.md) | Something broke. What, why, and the guardrail that stops it recurring. | The moment it happens |
| [LEARNINGS.md](LEARNINGS.md) | Something worked unusually well and is worth reusing. | The moment it happens |
| [DECISIONS.md](DECISIONS.md) | A choice made, with the alternatives considered. | When the choice is made |
| [ARCHITECTURE.md](ARCHITECTURE.md) | How the thing is built and why. | When the shape changes |

**Read before you plan.** Not before the first edit — before the plan. A plan
written without the history bakes the repeat mistake into it, and implementation
then carries it out faithfully. Pull the relevant history instead of reading
whole files:

```bash
node .bitacora/cli.mjs recall <tag>        # prints the matching entries in full
node .bitacora/cli.mjs stats               # where the recurring friction is
```

This is deliberate. The logs are never `@`-imported into this file: an
`@MISTAKES.md` would burn thousands of tokens on every session for history
that is irrelevant 90% of the time. Retrieve by tag, pay only for what you use.

**Write as you go.** Do not save it for the end of the session:

```bash
node .bitacora/cli.mjs new mistake  "Short, specific title" --tags area,failure-mode --severity high
node .bitacora/cli.mjs new learning "Short, specific title" --tags area
node .bitacora/cli.mjs new decision "Short, specific title" --tags area
```

The command scaffolds the entry and assigns the id; you write the prose.
`doctor` rejects an entry whose sections are unfilled, and rejects a mistake
whose **Guardrail** says nothing — naming a check, a test, a type or a refusal
in the code, not an intention to be more careful next time.

## Commands

```bash
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8000   # run the app
python -m pytest tests                              # the test suite
node .bitacora/cli.mjs doctor                       # is the logbook healthy?
```

## Before closing a session

1. The build passes (see **Commands** above). If it does not, say so in
   `STATE.md` under **In flight** rather than closing quietly on red.
2. `STATE.md` reflects reality, and its `updated:` line is today.
3. Anything that broke is in `MISTAKES.md`, with a guardrail — not just a description.
4. `node .bitacora/cli.mjs doctor` is green.

## What not to do

- No `git push`, no `--force`, no branch deletion unless asked explicitly.
- No emojis in code or files unless asked.
- No new `.md` files at the repo root. The logbook has a place for everything;
  if something genuinely has no place, say so instead of inventing a file.
- Do not mock behaviour that can be tested against real data.
