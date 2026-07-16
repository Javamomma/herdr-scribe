#!/usr/bin/env bash
# scribe-analyst.sh — live rolling-brief analyst loop for herdr-scribe.
#
# Every --interval seconds (default 60), reads the transcript content that
# has arrived since the previous tick (tracked as a byte offset stored at
# <analyst-out>.offset) and, if there is any new (non-blank) content, runs
# ${SCRIBE_LLM_CMD:-claude -p} over a short rolling-brief prompt built from
# just that delta, writing the result to <analyst-out>. When there is
# nothing new, <analyst-out> is left untouched (offset respected) so the
# pane doesn't flicker/reset on quiet ticks.
#
# The analyst output is ephemeral: it is meant to live alongside the
# transcript in the meeting's RAM dir and is destroyed with it on stop.
# This script never writes anywhere else.
#
# Exits cleanly once the transcript's parent directory no longer exists
# (the meeting has been stopped or aborted).
#
# Usage:
#   scribe-analyst.sh <transcript> <analyst-out> [--interval N]
#   scribe-analyst.sh --analyst-once <transcript> <analyst-out>   # one tick (tests)
set -euo pipefail

usage() {
	echo "usage: scribe-analyst.sh <transcript> <analyst-out> [--interval N]" >&2
	echo "       scribe-analyst.sh --analyst-once <transcript> <analyst-out>" >&2
}

offset_file() {
	echo "${1:?}.offset"
}

# Print the byte offset already consumed for <analyst-out>, or 0 if none
# recorded yet (or the recorded value is somehow empty/garbled).
read_offset() {
	local out="${1:?}" off_file val
	off_file="$(offset_file "$out")"
	val=""
	if [[ -f "$off_file" ]]; then
		val="$(cat "$off_file" 2>/dev/null || true)"
	fi
	if [[ "$val" =~ ^[0-9]+$ ]]; then
		echo "$val"
	else
		echo 0
	fi
}

write_offset() {
	local out="${1:?}" value="${2:?}"
	printf '%s' "$value" > "$(offset_file "$out")"
}

# Current size (bytes) of the transcript, or 0 if it doesn't exist yet.
transcript_size() {
	local t="${1:?}"
	if [[ -f "$t" ]]; then
		wc -c < "$t" 2>/dev/null | tr -d '[:space:]'
	else
		echo 0
	fi
}

# Build the rolling-brief prompt fed to SCRIBE_LLM_CMD over stdin.
build_prompt() {
	local delta="$1"
	cat <<PROMPT
You are a live meeting analyst. Below is the newest slice of a running
transcript. Produce a short rolling brief with exactly these four sections:

Now: what's being discussed right now.
Commitments: anything anyone has committed to doing.
Open-questions: unresolved questions raised.
Watch: anything worth flagging or keeping an eye on.

Base the brief only on the transcript delta below. Keep it terse.

--- transcript delta ---
$delta
PROMPT
}

# Run exactly one tick: if the transcript has grown since the last recorded
# offset, run the LLM over the new bytes and (re)write <analyst-out>; then
# advance the offset to the transcript's current size. If nothing new has
# arrived, return immediately without touching <analyst-out> at all.
analyst_tick() {
	local transcript="${1:?}" out="${2:?}"
	local size offset
	size="$(transcript_size "$transcript")"
	offset="$(read_offset "$out")"

	if [[ "$size" -le "$offset" ]]; then
		return 0
	fi

	local delta
	delta="$(tail -c "+$((offset + 1))" "$transcript" 2>/dev/null || true)"

	# Delta is only whitespace (e.g. a flushed-but-not-yet-worded gap):
	# nothing worth analyzing, but still advance the offset so it isn't
	# re-read forever.
	if [[ -z "$(printf '%s' "$delta" | tr -d '[:space:]')" ]]; then
		write_offset "$out" "$size"
		return 0
	fi

	local llm_cmd="${SCRIBE_LLM_CMD:-claude -p}"
	local prompt brief
	prompt="$(build_prompt "$delta")"
	if brief="$(printf '%s' "$prompt" | bash -c "$llm_cmd" 2>/dev/null)"; then
		printf '%s\n' "$brief" > "$out"
	else
		echo "scribe-analyst: warning: analyst command failed ($llm_cmd); brief not updated" >&2
	fi

	write_offset "$out" "$size"
	return 0
}

# Loop forever (one tick per --interval seconds) until the transcript's
# parent directory disappears -- that's the meeting-stopped signal.
analyst_loop() {
	local transcript="${1:?}" out="${2:?}" interval="${3:-60}"
	local dir
	dir="$(dirname "$transcript")"
	while [[ -d "$dir" ]]; do
		analyst_tick "$transcript" "$out" || true
		sleep "$interval"
	done
}

# --- dispatch ---

case "${1:-}" in
	--analyst-once)
		if [[ $# -lt 3 ]]; then
			usage
			exit 1
		fi
		analyst_tick "$2" "$3"
		exit 0
		;;
	-h|--help)
		usage
		exit 0
		;;
esac

TRANSCRIPT="${1:-}"
OUT="${2:-}"
if [[ -z "$TRANSCRIPT" || -z "$OUT" ]]; then
	usage
	exit 1
fi
shift 2 || true

INTERVAL=60
while [[ $# -gt 0 ]]; do
	case "$1" in
		--interval)
			INTERVAL="${2:?--interval requires a value}"
			shift 2
			;;
		*)
			echo "scribe-analyst.sh: unknown option: $1" >&2
			usage
			exit 1
			;;
	esac
done

analyst_loop "$TRANSCRIPT" "$OUT" "$INTERVAL"
