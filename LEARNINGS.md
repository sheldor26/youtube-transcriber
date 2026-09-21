# Learnings

> The counterpart to MISTAKES.md. When something works unusually well, the
> transferable part gets written down before it is forgotten.
>
> The test for an entry: would it change how you approach the *next* problem?
> If not, it is a changelog line, not a learning. Newest first.
>
> Add entries with: `node .bitacora/cli.mjs new learning "Title" --tags area`

<!-- bitacora:entry
id: L-0004
date: 2026-09-21
tags: [performance, verification]
-->
### Verify a performance fix by diffing output before/after, not just timing it

**What worked.** A synthetic benchmark proved `content.py`'s dedup fix was ~9-10x faster
(112s to 11.7s on 12,000 sentences). That alone wasn't proof nothing broke —
so the same fixed, seeded input was run through `git stash` (old code) and
the working tree (new code), and the two output files were diffed
byte-for-byte. Identical. Two independent, cheap checks instead of one that
only answers "is it faster."

**Why it worked.** A performance fix makes a claim about behavior, not just speed: that the
output is unchanged. Timing only tests the speed half of that claim: a
benchmark that finishes fast doesn't say whether it also silently started
skipping work. `git stash` gives the "before" version for free, on the exact
same code that's about to be committed, without keeping a manual copy of the
old function around.

**Reuse it when.** Any change whose pitch is "same result, faster": a precomputed cache, a
short-circuit added to a hot loop, swapping one algorithm for another with
the same contract. Skip it for a change that's supposed to alter output
(a bug fix, a new filter) — there diffing before/after is expected to fail
and proves nothing.

<!-- bitacora:entry
id: L-0003
date: 2026-09-21
tags: [ui, process]
-->
### Generate design directions as HTML and compare them side by side

**What worked.** Four self-contained HTML pages were generated from the real markup, each a
different visual direction, and opened side by side. Choosing took seconds and
the feedback that followed was about specific elements rather than about
adjectives.

**Why it worked.** A design described in prose is evaluated by imagining it, and two people
imagine different things. A design rendered in a browser is evaluated by
looking at it. Generating throwaway variants is cheap now; guessing which
adjective the other person meant never was.

**Reuse it when.** Any redesign of an existing surface, whenever the markup is already stable and
only the styling is in question. Not worth it for a single component or one
view.

<!-- bitacora:entry
id: L-0002
date: 2026-09-21
tags: [refactor, verification]
-->
### Verify a pure-move refactor by comparing symbol tables, not by reading diffs

**What worked.** The module split was a pure move: no behaviour was supposed to change. Parsing
the old file and the new modules with Python's own ast module, and comparing
the sets of top-level function and class names, proved it in seconds — with no
test run and no environment.

**Why it worked.** A refactor that claims to move code without changing it makes a claim about
symbols, not about behaviour, and symbols can be compared exactly. Reading a
2,100-line diff tests a human's attention; comparing two sets tests the claim.

**Reuse it when.** Any refactor whose claim is that nothing changed: a file split, a rename sweep,
a move between packages. Worth writing even when a test suite exists, because
it proves a different thing — that nothing was dropped.
