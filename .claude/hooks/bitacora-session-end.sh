#!/usr/bin/env bash
# bitacora — session end.
#
# The habit is the hard part, so the close is checked rather than trusted.
#
# Stop is NOT one of the events whose plain stdout reaches the model — that
# goes to the debug log, where nobody reads it. The reminder therefore has to
# travel as JSON on stdout, in the `systemMessage` field, with exit 0.
#
# Exit 2 would also work and would block the stop, but blocking a session over
# bookkeeping is how a hook ends up deleted. This reports; it never blocks.

set -uo pipefail
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
[ -f bitacora.config.json ] || exit 0
command -v node >/dev/null 2>&1 || exit 0

problems=()

if [ -f STATE.md ]; then
  updated=$(grep -m1 '^updated:' STATE.md | awk '{print $2}')
  if [ "${updated:-}" != "$(date +%F)" ]; then
    problems+=("- STATE.md still says \`updated: ${updated:-none}\`. If anything changed this session, rewrite the sections that moved and set today's date.")
  fi
fi

if [ -f .bitacora/cli.mjs ]; then
  if ! out=$(NO_COLOR=1 node .bitacora/cli.mjs doctor 2>&1); then
    problems+=("- \`doctor\` is failing:")
    problems+=("$(echo "$out" | sed 's/^/      /')")
  fi
fi

[ ${#problems[@]} -eq 0 ] && exit 0

{
  echo "Before this session closes:"
  echo
  printf '%s\n' "${problems[@]}"
  echo
  echo "Also worth a moment: did anything break, or work surprisingly well? Log it now —"
  echo '`node .bitacora/cli.mjs new mistake|learning "..." --tags <area>`. At the end of'
  echo "the next session the details are gone."
} | node -e '
  let s = "";
  process.stdin.on("data", (d) => (s += d));
  process.stdin.on("end", () => process.stdout.write(JSON.stringify({ systemMessage: s.trim() })));
'

exit 0
