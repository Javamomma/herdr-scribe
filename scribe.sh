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

# A scope is the neutral key for the work item a meeting belongs to. It is
# used as a path component under SCRIBE_SCOPE_ROOT, so it must be a single
# safe filename token — never a path (no /, no .., no leading dot).
validate_scope() {
	[[ "${1:-}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || return 1
	[[ "$1" == *..* ]] && return 1
	return 0
}

# §3 per-meeting glossary: emit hotword terms derived from this meeting's own
# context — the scope's glossary file under SCRIBE_SCOPE_ROOT (one term per
# line, '#' comments), the attendee list (comma-separated), and the topic
# (one phrase). The reviewed global SCRIBE_GLOSSARY file is never read or
# written here: per-meeting terms are additive on top of it downstream
# (scribe-transcribe.py --glossary-extra) and die with the meeting's RAM dir.
emit_hotword_sources() {
	local scope="${1:-}" topic="${2:-}" attendees="${3:-}"
	local root="${SCRIBE_SCOPE_ROOT:-}"
	if [[ -n "$root" && -n "$scope" && -f "$root/$scope/glossary.txt" ]]; then
		grep -vE '^[[:space:]]*(#|$)' "$root/$scope/glossary.txt" || true
	fi
	if [[ -n "$attendees" ]]; then
		printf '%s\n' "$attendees" | tr ',' '\n' \
			| sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' | grep -v '^$' || true
	fi
	if [[ -n "$topic" ]]; then
		printf '%s\n' "$topic"
	fi
	return 0
}

derive_hotwords() {
	emit_hotword_sources "$@" | awk '!seen[$0]++'
	return 0
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

# Capture-pipeline composer: prints (never runs directly) the shell pipeline
# that reads the system mic capture source and pipes raw PCM straight into
# scribe-transcribe.py — no intermediate audio file, no redirect to one.
#
# Capture source is env-configurable via SCRIBE_CAPTURE_SOURCE (default the
# neutral PulseAudio alias "@DEFAULT_SOURCE@", which resolves to whatever the
# host's default recording source is — on WSL2 that's the WSLg PulseAudio
# bridge. No machine-specific device name is ever hardcoded here.
#
# When SCRIBE_TEAMS=1 (set by `start --teams`) and SCRIBE_LOOPBACK_EXE points
# at a file that exists, a second [them] stream is composed from the loopback
# exe. The loopback exe emits the Windows render device's raw mix format
# (commonly 32-bit float PCM at 48kHz stereo -- see scribe-loopback.cs), not
# the s16le/16k/mono scribe-transcribe.py's FasterWhisperBackend hard-assumes
# (the mic path only works today because `parec` above is told to produce
# that format directly). So the [them] stream is resampled through `ffmpeg`
# before it ever reaches the transcriber; the assumed input format is
# env-overridable (SCRIBE_LOOPBACK_FORMAT/_RATE/_CHANNELS) in case a given
# device's mix format differs from the common default. If teams is requested
# but the exe is missing, OR the exe exists but ffmpeg isn't available to
# resample it, a warning is printed to stderr and the pipeline falls back to
# mic-only -- raw device-format audio is never handed to the worker.
compose_capture() {
	local id="${1:?}"
	local dir transcript
	dir="$(ram_dir "$id")"
	transcript="$dir/transcript.md"

	local source="${SCRIBE_CAPTURE_SOURCE:-@DEFAULT_SOURCE@}"
	local glossary_arg=""
	if [[ -n "${SCRIBE_GLOSSARY:-}" ]]; then
		glossary_arg=" --glossary \"${SCRIBE_GLOSSARY}\""
	fi
	# Per-meeting hotwords (written by `start` when a --scope was supplied)
	# ride along as an additive second glossary; the global file above is
	# passed through untouched.
	if [[ -f "$dir/hotwords.txt" ]]; then
		glossary_arg="${glossary_arg} --glossary-extra \"$dir/hotwords.txt\""
	fi

	local lines=()
	lines+=("parec --raw --format=s16le --rate=16000 --channels=1 -d \"$source\" | python3 \"$HERE/scribe-transcribe.py\" --transcript \"$transcript\" --channel me${glossary_arg} &")

	if [[ "${SCRIBE_TEAMS:-0}" == "1" ]]; then
		local exe="${SCRIBE_LOOPBACK_EXE:-}"
		# Deliberate symlink semantics: -e follows symlinks, so a BROKEN
		# symlink counts as "not available" and degrades to mic-only with
		# the warning below — the safe direction for an optional bridge.
		if [[ -n "$exe" && -e "$exe" ]]; then
			local ffmpeg_bin="${SCRIBE_FFMPEG_BIN:-ffmpeg}"
			if command -v "$ffmpeg_bin" >/dev/null 2>&1; then
				local in_fmt="${SCRIBE_LOOPBACK_FORMAT:-f32le}"
				local in_rate="${SCRIBE_LOOPBACK_RATE:-48000}"
				local in_channels="${SCRIBE_LOOPBACK_CHANNELS:-2}"
				lines+=("\"$exe\" | \"$ffmpeg_bin\" -loglevel error -f \"$in_fmt\" -ar \"$in_rate\" -ac \"$in_channels\" -i pipe:0 -f s16le -ar 16000 -ac 1 pipe:1 | python3 \"$HERE/scribe-transcribe.py\" --transcript \"$transcript\" --channel them${glossary_arg} &")
			else
				echo "warning: --teams requested but ffmpeg (${ffmpeg_bin}) was not found; cannot resample loopback audio to what the transcriber needs -- falling back to mic-only for the [them] stream" >&2
			fi
		else
			echo "warning: --teams requested but SCRIBE_LOOPBACK_EXE not found (${exe:-<unset>}); falling back to mic-only" >&2
		fi
	fi

	lines+=("wait")

	# If any backgrounded stream dies (or this process is killed), tear down
	# the rest rather than leaving orphans writing into a since-deleted dir.
	printf '%s\n' "trap 'kill \$(jobs -p) 2>/dev/null' TERM EXIT"
	printf '%s\n' "${lines[@]}"
}

# Pane path resolvers: used by herdr-plugin.toml's pane commands to find the
# currently-running meeting's transcript / analyst-brief path, whatever its
# id is. Not test-only (unlike the --validate-consent-style flags below) --
# these back real plugin-surface behavior.
current_transcript_path() {
	local id
	id="$(current_id)" || return 1
	echo "$(ram_dir "$id")/transcript.md"
}

current_analyst_path() {
	local id
	id="$(current_id)" || return 1
	echo "$(ram_dir "$id")/analyst.md"
}

# --doctor: report which optional host bridges (Windows remote-participant
# loopback, screen-OCR) are currently available. Never errors -- a missing
# bridge is normal, expected, and fully supported (mic-only / no-screen-
# context); this only ever *reports*, it doesn't gate anything.
doctor() {
	echo "scribe doctor -- optional bridge availability"
	echo ""

	local loopback_exe="${SCRIBE_LOOPBACK_EXE:-}"
	if [[ -n "$loopback_exe" && -e "$loopback_exe" ]]; then
		echo "  loopback (remote-participant capture): available ($loopback_exe)"
	else
		echo "  loopback (remote-participant capture): not available -- set SCRIBE_LOOPBACK_EXE (see scribe-loopback-setup.sh); falls back to mic-only"
	fi

	local screen_cmd="${SCRIBE_SCREEN_OCR_CMD:-}"
	if [[ -n "$screen_cmd" ]] && command -v "${screen_cmd%% *}" >/dev/null 2>&1; then
		echo "  screen-ocr (screen context for analyst): available ($screen_cmd)"
	else
		echo "  screen-ocr (screen context for analyst): not available -- set SCRIBE_SCREEN_OCR_CMD (see scribe-screen-setup.sh); analyst runs without screen context"
	fi

	return 0
}

# Meeting lifecycle: start / status / abort / stop

start() {
	local consent="" topic="" attendees="" scope="" teams=0 no_analyst=0
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
			--scope)
				scope="${2:-}"
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

	if [[ -n "$scope" ]] && ! validate_scope "$scope"; then
		echo "start: --scope must be a plain name (letters/digits/._-, no path separators)" >&2
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

	# §3: per-meeting hotwords, derived only when a scope was supplied.
	# Lives in the RAM dir (destroyed with the meeting); the global
	# SCRIBE_GLOSSARY file is never mutated.
	if [[ -n "$scope" && "${SCRIBE_MEETING_GLOSSARY:-1}" != "0" ]]; then
		derive_hotwords "$scope" "$topic" "$attendees" > "$dir/hotwords.txt"
		if [[ ! -s "$dir/hotwords.txt" ]]; then
			rm -f "$dir/hotwords.txt"
		fi
	fi

	{
		echo "id: $id"
		echo "consent: $consent"
		echo "topic: $topic"
		echo "attendees: $attendees"
		echo "scope: $scope"
		echo "teams: $teams"
		if [[ "$no_analyst" -eq 1 ]]; then
			echo "analyst: off"
		else
			echo "analyst: on"
		fi
		echo "started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
	} > "$dir/meta"

	set_current "$id"

	if [[ "$teams" -eq 1 ]]; then
		export SCRIBE_TEAMS=1
	fi

	# Capture seam: by default, run the composed mic(+optional loopback)
	# ->transcriber pipeline (compose_capture). Tests override this with
	# SCRIBE_CAPTURE_CMD=true so no mic/model is ever needed.
	local capture_cmd="${SCRIBE_CAPTURE_CMD:-}"
	if [[ -z "$capture_cmd" ]]; then
		capture_cmd="$(compose_capture "$id")"
	fi
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
	--validate-scope)
		validate_scope "${2:-}"
		exit $?
		;;
	--derive-hotwords)
		derive_hotwords "${2:-}" "${3:-}" "${4:-}"
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
	--compose-capture)
		compose_capture "${2:?}"
		exit 0
		;;
	--current-transcript)
		current_transcript_path || { echo "scribe.sh: no meeting running" >&2; exit 1; }
		exit 0
		;;
	--current-analyst)
		current_analyst_path || { echo "scribe.sh: no meeting running" >&2; exit 1; }
		exit 0
		;;
	--doctor)
		doctor
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
