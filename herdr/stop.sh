#!/usr/bin/env bash
# Scribe stop action: end the meeting (note → gate → destroy → artifacts),
# close the live panes, and open the artifact review pane when there is
# something to review.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

out="$(scribe stop)" || {
	echo "scribe: stop failed" >&2
	close_recorded_panes
	exit 1
}
echo "scribe: transcript written: $out"
close_recorded_panes

if [[ "${SCRIBE_ARTIFACTS:-0}" == "1" && "${SCRIBE_REVIEW_PANE:-1}" != "0" ]]; then
	open_pane artifacts || true
fi
exit 0
