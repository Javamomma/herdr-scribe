#!/usr/bin/env bash
# scribe.sh — live no-recording meeting transcription for herdr.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
[[ -f "${SCRIBE_CONF:-$HERE/scribe.conf}" ]] && source "${SCRIBE_CONF:-$HERE/scribe.conf}"

RAMROOT="${SCRIBE_RAMROOT:-/dev/shm}"
OUTDIR="${SCRIBE_OUTPUT_DIR:-$PWD/meetings}"

# Core helper functions

validate_consent() {
	case "${1:-}" in
		one-party|all-party)
			return 0
			;;
		*)
			return 1
			;;
	esac
}

slugify() {
	echo "${1:-}" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//'
}

ram_dir() {
	echo "$RAMROOT/scribe/${1:?}"
}

out_dir() {
	echo "$OUTDIR"
}

# Meeting id: <UTC-YYYYMMDD-HHMMSS>[-<topic-slug>]
meeting_id() {
	local topic="${1:-}"
	local ts
	ts="$(date -u +%Y%m%d-%H%M%S)"
	local slug
	slug="$(slugify "$topic")"
	if [[ -n "$slug" ]]; then
		echo "${ts}-${slug}"
	else
		echo "$ts"
	fi
}

# Single-meeting model: the running meeting's id lives in this marker file.
current_file() {
	echo "$RAMROOT/scribe/.current"
}

current_id() {
	local f
	f="$(current_file)"
	[[ -f "$f" ]] || return 1
	local id
	id="$(cat "$f")"
	[[ -n "$id" ]] || return 1
	[[ -d "$(ram_dir "$id")" ]] || return 1
	echo "$id"
}

set_current() {
	mkdir -p "$RAMROOT/scribe"
	echo "${1:?}" > "$(current_file)"
}

clear_current_if_matches() {
	local id="${1:?}"
	local f
	f="$(current_file)"
	if [[ -f "$f" ]] && [[ "$(cat "$f")" == "$id" ]]; then
		rm -f "$f"
	fi
}

# Meeting lifecycle: start / status / abort / stop

start() {
	local consent="" topic="" attendees="" teams=0 no_analyst=0 analyst_interval=60 model=""
	while [[ $# -gt 0 ]]; do
		case "$1" in
			--consent)
				consent="${2:-}"
				shift 2
				;;
			--topic)
				topic="${2:-}"
				shift 2
				;;
			--attendees)
				attendees="${2:-}"
				shift 2
				;;
			--teams)
				teams=1
				shift
				;;
			--no-analyst)
				no_analyst=1
				shift
				;;
			--analyst-interval)
				analyst_interval="${2:-}"
				shift 2
				;;
			--model)
				model="${2:-}"
				shift 2
				;;
			*)
				echo "start: unknown option: $1" >&2
				return 1
				;;
		esac
	done

	if ! validate_consent "$consent"; then
		echo "start: --consent one-party|all-party is required" >&2
		return 1
	fi

	local existing
	if existing="$(current_id)"; then
		echo "start: a meeting is already running ($existing); stop or abort it first" >&2
		return 1
	fi

	local id dir
	id="$(meeting_id "$topic")"
	dir="$(ram_dir "$id")"
	mkdir -p "$dir"
	: > "$dir/transcript.md"

	{
		echo "id: $id"
		echo "consent: $consent"
		echo "topic: $topic"
		echo "attendees: $attendees"
		echo "teams: $teams"
		if [[ "$no_analyst" -eq 1 ]]; then
			echo "analyst: off"
		else
			echo "analyst: on"
		fi
		echo "analyst_interval: $analyst_interval"
		echo "model: $model"
		echo "started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
	} > "$dir/meta"

	set_current "$id"

	# Capture seam: the real mic(+loopback)->transcriber pipeline is composed
	# in Task 4 (compose_capture). Here it's just a placeholder seam so tests
	# can override it with SCRIBE_CAPTURE_CMD=true (no mic/model needed).
	local capture_cmd="${SCRIBE_CAPTURE_CMD:-:}"
	nohup bash -c "$capture_cmd" >/dev/null 2>&1 &
	echo "$!" > "$dir/capture.pid"

	echo "$id"
}

status() {
	local id
	if id="$(current_id)"; then
		echo "$id"
	else
		echo "none"
	fi
}

abort() {
	local id="${1:-}"
	if [[ -z "$id" ]]; then
		if ! id="$(current_id)"; then
			echo "abort: no meeting running" >&2
			return 1
		fi
	fi
	local dir
	dir="$(ram_dir "$id")"
	if [[ ! -d "$dir" ]]; then
		echo "abort: no such meeting: $id" >&2
		return 1
	fi
	if [[ -f "$dir/capture.pid" ]]; then
		kill "$(cat "$dir/capture.pid")" 2>/dev/null || true
	fi
	rm -rf "$dir"
	clear_current_if_matches "$id"
	echo "aborted: $id" >&2
}

stop() {
	local id="${1:-}"
	if [[ -z "$id" ]]; then
		if ! id="$(current_id)"; then
			echo "stop: no meeting running" >&2
			return 1
		fi
	fi
	local dir
	dir="$(ram_dir "$id")"
	if [[ ! -d "$dir" ]]; then
		echo "stop: no such meeting: $id" >&2
		return 1
	fi

	mkdir -p "$(out_dir)"
	local out
	out="$(out_dir)/$id.md"
	cp "$dir/transcript.md" "$out"

	if [[ -f "$dir/capture.pid" ]]; then
		kill "$(cat "$dir/capture.pid")" 2>/dev/null || true
	fi
	rm -rf "$dir"
	clear_current_if_matches "$id"

	# Fail-safe: the transcript is already written out and the ram dir already
	# destroyed above, so a hook/LLM failure here can never lose the meeting.
	local hook="${SCRIBE_ON_STOP:-$HERE/scribe-notes}"
	if ! bash -c "$hook \"\$1\"" _ "$out"; then
		echo "stop: warning: on-stop hook failed ($hook); transcript preserved at $out" >&2
	fi

	echo "$out"
}

# Hidden test flags
case "${1:-}" in
	--validate-consent)
		validate_consent "${2:-}"
		exit $?
		;;
	--slugify)
		slugify "${2:-}"
		exit 0
		;;
	--ram-dir)
		ram_dir "${2:-}"
		exit 0
		;;
	--out-dir)
		out_dir
		exit 0
		;;
esac

# Meeting lifecycle dispatch
case "${1:-}" in
	start)
		shift
		start "$@"
		exit $?
		;;
	stop)
		shift
		stop "$@"
		exit $?
		;;
	status)
		shift
		status "$@"
		exit $?
		;;
	abort)
		shift
		abort "$@"
		exit $?
		;;
esac
