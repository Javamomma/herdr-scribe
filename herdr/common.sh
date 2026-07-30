#!/usr/bin/env bash
# herdr/common.sh — shared plumbing for the herdr action wrappers.
#
# herdr runs plugin commands with a minimal PATH and the plugin runtime env
# (HERDR_BIN_PATH, HERDR_PLUGIN_ROOT, HERDR_PLUGIN_STATE_DIR, ...). No
# set -e: a transient herdr/API hiccup must never strand a meeting; every
# step checks its own result.
set -uo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

H="${HERDR_BIN_PATH:-herdr}"
ROOT="${HERDR_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# The wrappers make decisions (consent default, artifacts pane) before
# scribe.sh ever runs, so they need the operator conf themselves.
if [[ -f "${SCRIBE_CONF:-$ROOT/scribe.conf}" ]]; then
	source "${SCRIBE_CONF:-$ROOT/scribe.conf}"
fi
STATE="${HERDR_PLUGIN_STATE_DIR:-${TMPDIR:-/tmp}/scribe-herdr-state}"
mkdir -p "$STATE" 2>/dev/null || true
PANES_FILE="$STATE/open-panes"

scribe() {
	bash "$ROOT/scribe.sh" "$@"
}

# Open a manifest pane entrypoint and remember its pane id for later close.
# Best-effort: pane failures are logged, never fatal to the meeting.
open_pane() {
	local entrypoint="${1:?}"
	local out pane_id
	out="$("$H" plugin pane open --plugin scribe --entrypoint "$entrypoint" \
		--placement split --no-focus 2>&1)" || {
		echo "scribe: could not open pane '$entrypoint': $out" >&2
		return 1
	}
	# The pane id is in the JSON reply (0.7.3 shape:
	# result.plugin_pane.pane.pane_id). Search recursively so a future
	# nesting change degrades to "pane not recorded", never a crash.
	pane_id="$(printf '%s' "$out" | python3 -c '
import json, sys

def find(node):
    if isinstance(node, dict):
        if "pane_id" in node and isinstance(node["pane_id"], str):
            return node["pane_id"]
        for value in node.values():
            found = find(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = find(value)
            if found:
                return found
    return None

try:
    result = find(json.load(sys.stdin))
except Exception:
    result = None
if result:
    print(result)
' 2>/dev/null || true)"
	if [[ -n "$pane_id" ]]; then
		printf '%s\t%s\n' "$entrypoint" "$pane_id" >> "$PANES_FILE"
	fi
	return 0
}

close_recorded_panes() {
	[[ -f "$PANES_FILE" ]] || return 0
	local entrypoint pane_id
	while IFS=$'\t' read -r entrypoint pane_id; do
		[[ -n "$pane_id" ]] || continue
		"$H" plugin pane close "$pane_id" >/dev/null 2>&1 || true
	done < "$PANES_FILE"
	rm -f "$PANES_FILE" 2>/dev/null || true
	return 0
}
