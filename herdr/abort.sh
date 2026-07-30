#!/usr/bin/env bash
# Scribe abort action: discard the meeting (no note), close the live panes.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

scribe abort || {
	close_recorded_panes
	exit 1
}
close_recorded_panes
exit 0
