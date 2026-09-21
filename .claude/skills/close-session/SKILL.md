---
name: close-session
description: Use when a working session is ending, or before /clear when switching tasks — verify the build, refresh STATE.md, log what broke and what worked, and leave doctor green.
---

# Close the session

Run this before ending any working session. The point is that the *next*
session — which remembers nothing — starts from an accurate picture.

## 1. Verify

Run the project's build and lint. If either fails, fix it or write down exactly
where it stopped in `STATE.md` under **In flight**. Never close on a red build
without saying so in writing.

## 2. Refresh STATE.md

Rewrite the sections that changed — do not append. `STATE.md` is a snapshot; it
has a line budget and `doctor` enforces it.

- **Shipped** — move anything that is now genuinely done.
- **In flight** — where work stopped, precisely enough to resume cold. "Halfway
  through the checkout form" is useless; "form validates but the Stripe webhook
  handler in `app/api/stripe/route.ts` is unwritten" is usable.
- **Next** — the next two or three moves, in order.
- **Known rough edges** — anything wrong and knowingly tolerated, with why.

Set the `updated:` line to today's date.

## 3. Log

Ask two questions honestly:

**Did anything break, surprise us, or get fixed twice?** → a mistake entry.
The entry is only finished when it has a *guardrail*: the check, test, type or
rule that makes the same failure impossible rather than merely known.

```bash
node .bitacora/cli.mjs new mistake "Specific title" --tags area,failure-mode --severity high
```

**Did anything work unusually well?** → a learning entry, but only if it would
change how the next problem is approached. Otherwise it is a changelog line.

```bash
node .bitacora/cli.mjs new learning "Specific title" --tags area
```

Fill every `bitacora:fill-me` block with real prose. Placeholders left behind
fail `doctor`.

## 4. Confirm

```bash
node .bitacora/cli.mjs doctor
```

Green, or say out loud what is not.

## 5. Do not commit unless asked

Show the diff and wait.
