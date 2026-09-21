#!/usr/bin/env bash
# bitacora — session start.
#
# Injects the smallest useful slice of memory into a cold session: what is in
# flight, what is next, and which areas have burned us most. Everything else
# stays on disk until the agent asks for it by tag.
#
# SessionStart is one of the few events whose plain stdout Claude Code adds to
# the model's context, which is why this hook prints markdown rather than JSON.
#
# Called with --after-compaction from the "compact" matcher. That firing is the
# point of the whole project: compaction has just summarised the conversation
# and the specific detail is the first thing it loses, so the durable state
# gets re-stated at exactly the moment the volatile copy was destroyed.

set -uo pipefail
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
[ -f STATE.md ] || exit 0

if [ "${1:-}" = "--after-compaction" ]; then
  echo "## Logbook digest (after compaction)"
  echo
  echo "The conversation was just compacted, so earlier specifics are now a summary."
  echo "This is the durable copy — it is on disk and did not go through that summary."
else
  echo "## Logbook digest"
fi
echo

# The two sections a cold session actually needs.
awk '
  /^## (In flight|Next)/ { show = 1; print; next }
  /^## / { show = 0 }
  show { print }
' STATE.md | grep -v 'bitacora:fill-me' | sed '/^$/N;/^\n$/D'

updated=$(grep -m1 '^updated:' STATE.md | awk '{print $2}')
if [ -n "${updated:-}" ]; then
  echo
  echo "_STATE.md last updated: ${updated}._"
fi

if [ -f .bitacora/cli.mjs ] && command -v node >/dev/null 2>&1; then
  top=$(NO_COLOR=1 node .bitacora/cli.mjs stats 2>/dev/null \
        | awk '/█/ {print $1}' | head -4 | paste -sd, - | sed 's/,/, /g')
  if [ -n "${top:-}" ]; then
    echo
    echo "Most-logged areas: ${top}."
    echo "Before touching one of them, run \`node .bitacora/cli.mjs recall <tag>\` — it prints the entries in full."
  fi
fi

exit 0
