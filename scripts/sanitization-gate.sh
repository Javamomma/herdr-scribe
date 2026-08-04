#!/usr/bin/env bash
# sanitization-gate.sh — must pass (exit 0, zero HARD hits) before herdr-scribe
# is made public. Greps the whole tree for personal/organization identifiers
# that must never appear in the published plugin.
#
# The identifier lists live OUTSIDE this script, in an untracked local file
# (scripts/denylist.local, gitignored) — publishing the denylist would leak
# exactly the identifiers it exists to keep private. Copy
# scripts/denylist.example to scripts/denylist.local and fill in your terms.
#
# HARD list  -> any hit fails the gate (exit 1). Unambiguous identifiers.
# SOFT list  -> hits are printed as warnings for human judgment (exit unaffected).
#              These are words that shouldn't appear in a generic transcription
#              tool but are common enough to false-positive in prose.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
SELF="scripts/sanitization-gate.sh"

DENYLIST="${SCRIBE_DENYLIST_FILE:-scripts/denylist.local}"
if [ ! -f "$DENYLIST" ]; then
  echo "gate: no denylist at $DENYLIST" >&2
  echo "gate: copy scripts/denylist.example there and fill in your identifiers" >&2
  exit 2
fi
HARD="" SOFT=""
# shellcheck source=/dev/null
. "$DENYLIST"
if [ -z "$HARD" ]; then
  echo "gate: $DENYLIST sets no HARD pattern" >&2
  exit 2
fi

# Build a file list (tracked files if in git, else everything), excluding .git,
# this script, and the local denylist itself.
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  mapfile -t FILES < <(git ls-files | grep -vF -e "$SELF" -e "$DENYLIST")
else
  mapfile -t FILES < <(find . -type f -not -path './.git/*' -not -path "./$SELF" -not -path "./$DENYLIST")
fi
[ "${#FILES[@]}" -eq 0 ] && { echo "gate: no files to scan"; exit 0; }

echo "== HARD denylist =="
if grep -rInE -- "$HARD" "${FILES[@]}"; then
  echo ""
  echo "FAIL: hard-denylist identifiers found above. Remove them before publishing."
  exit 1
fi
echo "  (clean)"

echo ""
echo "== SOFT list (review — not a failure) =="
if [ -n "$SOFT" ]; then
  grep -rInE -- "$SOFT" "${FILES[@]}" || echo "  (clean)"
else
  echo "  (no SOFT pattern set)"
fi

echo ""
echo "PASS: no hard-denylist identifiers. Human-review the SOFT hits (if any) before flipping public."
exit 0
