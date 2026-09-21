---
name: log-mistake
description: Write a logbook entry when something breaks, works unusually well, or a hard-to-reverse choice gets made. Use the moment it happens, not at the end of the session.
---

# Log it now

The entry gets written when the thing happens, not at the end of the session.
At the end the details are gone, and it is the first task to get cut when the
session is running long. Four seconds now, or nothing ever.

```bash
node .bitacora/cli.mjs new mistake  "Specific title" --tags area,failure-mode --severity high
node .bitacora/cli.mjs new learning "Specific title" --tags area
node .bitacora/cli.mjs new decision "Specific title" --tags area
```

The command assigns the id and scaffolds the sections. You write the prose.

## If a subagent found it, log it in the main session

A subagent runs in its own context window. It follows the main `CLAUDE.md`, so
it can read this skill and run `recall` — but when it returns, it returns a
summary, and everything specific it saw is gone with its context.

That matters more than it sounds, because subagents get the exploratory and
verification work: reading unfamiliar code, checking whether something is
actually broken. That is exactly where mistakes are discovered.

So when a subagent reports something that broke or surprised it, the main
session writes the entry, and writes it before moving on — the detail is in the
summary you are holding right now, and nowhere else. If the summary is too thin
to support a real **Guardrail**, ask the subagent for the specifics before its
findings go stale, rather than filing a vague entry.

## Which kind

- **Mistake** — anything broke, surprised us, or had to be fixed twice. A
  near-miss counts. So does something that worked but only after three wrong
  attempts: the three wrong attempts are the entry.
- **Learning** — something worked unusually well *and* would change how the
  next problem is approached. If it would not, it is a changelog line.
- **Decision** — a choice that would be expensive to reverse, or that a future
  reader might quietly undo because the reasoning is invisible.

## Writing a mistake

**What happened.** Concrete, specific, no blame. Enough detail that the shape
is recognisable when it starts happening again. Name the actual values, files
and commands — "the import overwrote 11 of 15 manually corrected prices" beats
"data got overwritten".

**Root cause.** Why it was *possible*, not what broke. This is the distinction
that determines whether the entry is worth anything:

> "The importer overwrote the value" — that is what broke.
> "Nothing distinguished a verified value from a scraped one, so newer always
> won" — that is why it was possible.

Keep asking "and why was that allowed" until the answer is a property of the
system rather than of the person or the model.

**Guardrail.** The check, test, type or refusal that makes the same failure
impossible rather than merely known. `doctor` rejects the entry if this section
is missing or near-empty, because this is the part that does the work.

A guardrail is not a lesson. It is code, config, or a test:

| Not a guardrail | A guardrail |
| :-- | :-- |
| "Be more careful with prices" | A `priceVerifiedAt` field the importer refuses to overwrite, checked in CI |
| "Remember the API is paginated" | A type that makes the un-paginated call unrepresentable |
| "Always run the build first" | A pre-commit hook, or a `Stop` hook that reports it |
| "Read the script header next time" | The script validates its input and exits with the reason |

If no guardrail is possible, say that explicitly and say why — an honest
"unpreventable, detectable only by reading the output, so the output is now
printed" is a real entry. Silence in that section is not.

## Tagging

Two to four tags: one for the area, one for the failure mode. Good tags name a
thing in the system (`pricing`, `auth`, `migrations`, `deploy`) or a failure
mode (`data-loss`, `race-condition`, `silent-failure`, `type-hole`). Bad tags
describe the entry (`important`, `bug`, `fixed`) and match everything or
nothing.

Check what already exists before inventing a tag:

```bash
node .bitacora/cli.mjs stats
```

Reusing an existing tag is what makes `recall` work later, and what makes a
recurring failure mode visible as a count rather than as five unrelated
entries.

## Finish it

```bash
node .bitacora/cli.mjs doctor
```

Green before moving on. An entry left half-written is how the logbook dies.
