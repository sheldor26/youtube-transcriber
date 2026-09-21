# Learnings

> The counterpart to MISTAKES.md. When something works unusually well, the
> transferable part gets written down before it is forgotten.
>
> The test for an entry: would it change how you approach the *next* problem?
> If not, it is a changelog line, not a learning. Newest first.
>
> Add entries with: `node .bitacora/cli.mjs new learning "Title" --tags area`

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
