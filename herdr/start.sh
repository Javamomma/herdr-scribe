#!/usr/bin/env bash
# Scribe start action: begin a meeting with env-configured defaults, then
# open the transcript (+ analyst) panes. herdr 0.7 actions can't prompt for
# flags — set SCRIBE_DEFAULT_CONSENT (required) and the optional
# SCRIBE_DEFAULT_TOPIC / _SCOPE / _ATTENDEES in scribe.conf.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

consent="${SCRIBE_DEFAULT_CONSENT:-}"
if [[ -z "$consent" ]]; then
	echo "scribe: start refused — set SCRIBE_DEFAULT_CONSENT=one-party|all-party (in scribe.conf) so consent is explicit, or run scribe.sh start --consent ... in a terminal" >&2
	exit 1
fi

args=(start --consent "$consent")
[[ -n "${SCRIBE_DEFAULT_TOPIC:-}" ]] && args+=(--topic "$SCRIBE_DEFAULT_TOPIC")
[[ -n "${SCRIBE_DEFAULT_SCOPE:-}" ]] && args+=(--scope "$SCRIBE_DEFAULT_SCOPE")
[[ -n "${SCRIBE_DEFAULT_ATTENDEES:-}" ]] && args+=(--attendees "$SCRIBE_DEFAULT_ATTENDEES")
[[ "${SCRIBE_TEAMS:-0}" == "1" ]] && args+=(--teams)

id="$(scribe "${args[@]}")" || {
	echo "scribe: start failed" >&2
	exit 1
}
echo "scribe: meeting started: $id"

open_pane transcript || true
if [[ "${SCRIBE_NO_ANALYST:-0}" != "1" ]]; then
	open_pane analyst || true
fi
exit 0
