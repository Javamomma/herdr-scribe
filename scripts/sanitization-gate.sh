#!/usr/bin/env bash
# sanitization-gate.sh — must pass (exit 0, zero HARD hits) before herdr-scribe
# is made public. Greps the whole tree for personal/organization identifiers
# that must never appear in the published plugin.
#
# HARD list  -> any hit fails the gate (exit 1). Unambiguous identifiers.
# SOFT list  -> hits are printed as warnings for human judgment (exit unaffected).
#              These are words that shouldn't appear in a generic transcription
#              tool but are common enough to false-positive in prose.
#
# The gate excludes .git/ and this script itself (which necessarily contains the
# denylist terms). Extend HARD/SOFT as you think of more identifiers.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
SELF="scripts/sanitization-gate.sh"

HARD='<redacted -- identifier denylist now lives untracked in scripts/denylist.local; see denylist.example>'
SOFT='\bprivilege|\bmatter\b|\bvault\b|litigation|retention|/ingest|potx|records.management'

# Build a file list (tracked files if in git, else everything), excluding .git and self.
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  mapfile -t FILES < <(git ls-files | grep -vF "$SELF")
else
  mapfile -t FILES < <(find . -type f -not -path './.git/*' -not -path "./$SELF")
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
grep -rInE -- "$SOFT" "${FILES[@]}" || echo "  (clean)"

echo ""
echo "PASS: no hard-denylist identifiers. Human-review the SOFT hits (if any) before flipping public."
exit 0
