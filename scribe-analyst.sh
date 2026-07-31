#!/usr/bin/env bash
# scribe-analyst.sh — two-tier live analyst for herdr-scribe.
#
# LIGHT TIER (every --interval seconds, default 60): reads the transcript
# content that has arrived since the previous tick (tracked as a byte offset
# stored at <analyst-out>.offset) and, if there is any new (non-blank)
# content, runs ${SCRIBE_LLM_CMD:-claude -p} over a short rolling-brief
# prompt built from just that delta. The prompt also asks the model to emit
# at most ONE machine-readable trigger line —
#
#     RETRIEVE: <short retrieval query>
#
# — when the conversation references a document/section/clause/identifier a
# reader would need the actual text of. That line is control data: it is
# parsed with an anchored pattern (absent/garbled → "no retrieval this
# cycle") and ALWAYS stripped from the pane output.
#
# DEEP TIER (on trigger only; off unless SCRIBE_DEEP_CORPUS_ROOT is set):
# a detached worker searches the corpus READ-ONLY, extracts text via
# scribe-doc2text, and asks ${SCRIBE_LLM_CMD_DEEP:-$SCRIBE_LLM_CMD} for the
# VERBATIM passage plus its source path (never a paraphrase; "not found" is
# an acceptable answer, fabrication is not). Guarantees:
#   - single-in-flight: an atomic mkdir lock (not a file-existence test,
#     which races); a trigger arriving while one is running overwrites a
#     single COALESCING pending slot — one stale answer beats a backlog.
#   - pending is drained at the top of each light cycle, lock permitting.
#   - hard-timeboxed (SCRIBE_DEEP_TIMEOUT, default 120s): a hung retrieval
#     appends a loud failure block; it never wedges the analyst loop.
#   - RAM-only scratch: everything the worker extracts lives next to
#     <analyst-out> (the meeting's RAM dir) and dies with the meeting.
#
# RENDERING: the pane file <analyst-out> is COMPOSED, not appended to: the
# latest light brief (kept at <analyst-out>.brief) plus a bounded tail of
# retrieval blocks (kept at <analyst-out>.deep), so a light-tier rewrite can
# never clobber a retrieval and the pane states its own bound. Deep appends
# are serialized through a lock; block content is passed to the appender as
# a FILE, never on stdin — a worker holding the lock while waiting on stdin
# is the classic test-harness deadlock (parity brief §6.1).
#
# The analyst output is ephemeral: it lives alongside the transcript in the
# meeting's RAM dir and is destroyed with it on stop. This script never
# writes anywhere else.
#
# Optional screen context: when SCRIBE_SCREEN_OCR_CMD is set and resolvable,
# its stdout is captured fresh each tick and prepended to the light prompt.
# Best-effort only; a missing/failing OCR command never aborts the tick.
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
# Hardened against the stop/abort race: the file can vanish between the -f
# test and the read (meeting destroyed mid-tick). Under pipefail that failed
# `wc` would otherwise abort the tick with an empty value; normalize to 0.
transcript_size() {
	local t="${1:?}"
	local n=""
	if [[ -f "$t" ]]; then
		n="$(wc -c < "$t" 2>/dev/null | tr -d '[:space:]' || true)"
	fi
	echo "${n:-0}"
}

# Best-effort capture of optional screen-OCR context (see
# scribe-screen-setup.sh): if SCRIBE_SCREEN_OCR_CMD is unset or its command
# isn't resolvable, print nothing. If it's set but fails at run time, that
# failure is swallowed (`|| true`) -- screen context is a nice-to-have, never
# a reason to fail a tick.
capture_screen_context() {
	local cmd="${SCRIBE_SCREEN_OCR_CMD:-}"
	[[ -z "$cmd" ]] && return 0
	command -v "${cmd%% *}" >/dev/null 2>&1 || return 0
	bash -c "$cmd" 2>/dev/null || true
}

# Build the rolling-brief prompt fed to SCRIBE_LLM_CMD over stdin. When
# screen context is available, it's prepended immediately ahead of the
# transcript delta as "Screen context:\n<ocr>\n\n". The RETRIEVE example
# below is indented so that, echoed back by a passthrough stub, it can never
# anchor-match the trigger parser.
build_prompt() {
	local delta="$1" screen="${2:-}"
	local screen_block=""
	if [[ -n "$screen" ]]; then
		screen_block="Screen context:
$screen

"
	fi
	cat <<PROMPT
You are a live meeting analyst. Below is the newest slice of a running
transcript. Produce a short rolling brief with exactly these four sections:

Now: what's being discussed right now.
Commitments: anything anyone has committed to doing.
Open-questions: unresolved questions raised.
Watch: anything worth flagging or keeping an eye on.

Base the brief only on the transcript delta below (and the screen context
above it, if present). Keep it terse.

Additionally: if, and only if, this slice references a specific document,
section, clause, citation, or identifier whose actual text a reader would
need, append ONE extra final line of exactly this form (at most one; append
nothing when nothing is referenced):

    RETRIEVE: <short retrieval query>

${screen_block}--- transcript delta ---
$delta
PROMPT
}

# --- §1a trigger contract -----------------------------------------------

# Parse the (at most one) trigger line out of a brief on stdin. Anchored:
# only a line that IS "RETRIEVE: <non-blank query>" counts; anything absent
# or garbled prints nothing — "no retrieval this cycle". Never errors.
extract_trigger() {
	grep -m1 -E '^RETRIEVE: [^[:space:]]' 2>/dev/null | sed -E 's/^RETRIEVE: //' || true
	return 0
}

# Strip every trigger-shaped line from the pane copy — control data is not
# something a human should read. Stripping is deliberately looser than
# parsing: even a garbled "RETRIEVE:" line is noise the pane shouldn't show.
strip_trigger() {
	grep -vE '^RETRIEVE:' 2>/dev/null || true
	return 0
}

# --- deep-tier configuration --------------------------------------------

deep_enabled() {
	if [[ "${SCRIBE_DEEP_ENABLE:-}" == "0" ]]; then
		return 1
	fi
	local root="${SCRIBE_DEEP_CORPUS_ROOT:-}"
	if [[ -z "$root" || ! -d "$root" ]]; then
		return 1
	fi
	return 0
}

# Malformed numeric config falls back to the default WITH a warning —
# silence would read as certainty (§6.4).
validated_number() {
	local value="$1" fallback="$2" name="$3"
	if [[ "$value" =~ ^[0-9]+$ ]]; then
		echo "$value"
	else
		echo "scribe-analyst: warning: $name='$value' is not a number; using $fallback" >&2
		echo "$fallback"
	fi
}

deep_timeout()    { validated_number "${SCRIBE_DEEP_TIMEOUT:-120}" 120 "SCRIBE_DEEP_TIMEOUT"; }
deep_max_blocks() { validated_number "${SCRIBE_DEEP_MAX_BLOCKS:-3}" 3 "SCRIBE_DEEP_MAX_BLOCKS"; }
deep_max_files()  { validated_number "${SCRIBE_DEEP_MAX_FILES:-5}" 5 "SCRIBE_DEEP_MAX_FILES"; }

# --- deep-tier locking / submission -------------------------------------

# The in-flight lock is an atomic mkdir (a file-existence test would race).
# The spawner records the worker pid inside it so a stale lock (worker died
# without cleanup) can be detected and reclaimed. A lock without a pid file
# is treated as live — conservative: never steal a lock we can't prove dead.
deep_lock_live() {
	local out="${1:?}"
	local pid_file="$out.deeplock/pid"
	if [[ ! -f "$pid_file" ]]; then
		return 0
	fi
	local pid
	pid="$(cat "$pid_file" 2>/dev/null || true)"
	if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
		return 0
	fi
	return 1
}

# Submit a retrieval query. If a worker is in flight, COALESCE into the
# single pending slot (overwrite, don't queue: one stale answer beats a
# backlog of them). SCRIBE_DEEP_WORKER_CMD is a test seam replacing the real
# detached worker; it receives <transcript> <analyst-out> <query> as $1-$3.
submit_deep() {
	local transcript="${1:?}" out="${2:?}" query="${3:?}"
	local lock="$out.deeplock"
	if ! mkdir "$lock" 2>/dev/null; then
		if deep_lock_live "$out"; then
			printf '%s' "$query" > "$out.pending"
			return 0
		fi
		rm -rf "$lock"
		if ! mkdir "$lock" 2>/dev/null; then
			printf '%s' "$query" > "$out.pending"
			return 0
		fi
	fi
	local worker_cmd="${SCRIBE_DEEP_WORKER_CMD:-}"
	if [[ -n "$worker_cmd" ]]; then
		# The seam command references the transcript/out/query as $1/$2/$3.
		nohup bash -c "$worker_cmd" _ "$transcript" "$out" "$query" >/dev/null 2>&1 &
	else
		nohup bash "$0" --deep-worker "$transcript" "$out" "$query" >/dev/null 2>&1 &
	fi
	echo $! > "$lock/pid" 2>/dev/null || true
	return 0
}

# Drain the coalesced pending query at the top of a light cycle, but only
# when no worker is in flight.
drain_pending() {
	local transcript="${1:?}" out="${2:?}"
	local pending="$out.pending"
	if [[ ! -f "$pending" ]]; then
		return 0
	fi
	if [[ -d "$out.deeplock" ]] && deep_lock_live "$out"; then
		return 0
	fi
	local query
	query="$(cat "$pending" 2>/dev/null || true)"
	rm -f "$pending"
	if [[ -n "$query" ]]; then
		submit_deep "$transcript" "$out" "$query"
	fi
	return 0
}

# --- deep-tier worker ----------------------------------------------------

# Corpus file discovery, READ-ONLY, bounded. The query is model-emitted free
# text: it is only ever used as a FIXED STRING (grep -F) — never as a
# pattern or glob (§6.2). Whole-phrase match first; if nothing matches, fall
# back to an OR over the query's longer words.
find_corpus_files() {
	local corpus="${1:?}" query="${2:?}" max="${3:?}"
	local found
	found="$(grep -rilF -- "$query" "$corpus" 2>/dev/null | sort | head -n "$max" || true)"
	if [[ -z "$found" ]]; then
		local -a words=() args=()
		read -ra words <<< "$query"
		local w
		for w in "${words[@]}"; do
			if [[ "${#w}" -ge 3 && "${#args[@]}" -lt 16 ]]; then
				args+=(-e "$w")
			fi
		done
		if [[ "${#args[@]}" -gt 0 ]]; then
			found="$(grep -rilF -- "${args[@]}" "$corpus" 2>/dev/null | sort | head -n "$max" || true)"
		fi
	fi
	if [[ -n "$found" ]]; then
		printf '%s\n' "$found"
	fi
	return 0
}

# Serialized append of one retrieval block. Content arrives as a FILE path,
# never on stdin: a process that acquired this lock and then blocked reading
# stdin would deadlock any harness feeding two workers sequentially (§6.1).
append_deep_block() {
	local out="${1:?}" blockfile="${2:?}"
	local lock="$out.appendlock"
	local tries=0
	while ! mkdir "$lock" 2>/dev/null; do
		tries=$((tries + 1))
		if [[ "$tries" -gt 200 ]]; then
			echo "scribe-analyst: warning: could not acquire append lock for $out.deep" >&2
			return 1
		fi
		sleep 0.05
	done
	cat "$blockfile" >> "$out.deep"
	rmdir "$lock" 2>/dev/null || true
	return 0
}

# Portable hard timeout (macOS may lack timeout(1)): SIGTERM at N seconds,
# SIGKILL 5s later for a command that ignores the first signal.
#
# The fallback watchdog is detached from our stdout/stderr — callers run this
# inside a command substitution, and a watchdog inheriting that pipe (or an
# orphaned long `sleep` child of it) would hold the substitution open for the
# full timeout even after the command finished. It also re-checks liveness
# every second, so it never outlives the command by more than ~1s.
run_with_timeout() {
	local secs="${1:?}"
	shift
	if command -v timeout >/dev/null 2>&1; then
		timeout -k 5 "$secs" "$@"
		return $?
	fi
	"$@" &
	local pid=$!
	(
		waited=0
		while [ "$waited" -lt "$secs" ] && kill -0 "$pid" 2>/dev/null; do
			sleep 1
			waited=$((waited + 1))
		done
		if kill -0 "$pid" 2>/dev/null; then
			kill "$pid" 2>/dev/null
			sleep 5
			kill -9 "$pid" 2>/dev/null
		fi
	) >/dev/null 2>&1 &
	local watchdog=$!
	local rc=0
	wait "$pid" || rc=$?
	kill "$watchdog" 2>/dev/null || true
	return "$rc"
}

build_deep_prompt_header() {
	local query="${1:?}"
	cat <<PROMPT
You are a document-retrieval assistant supporting a live meeting. The
conversation referenced something; the retrieval query is:

  $query

Below are candidate source documents. Quote the passage(s) that directly
answer the query VERBATIM — the exact text, no paraphrase, no summary — and
after each quote cite the exact source path it came from. A reader will rely
on this live: a paraphrase is worse than nothing. If the passage is not
present in the provided sources, say exactly that. Never fabricate,
approximate, or "reconstruct" text that is not present.
PROMPT
}

# The detached deep worker. Always exits 0 — a retrieval failure appends a
# loud failure block; it must never wedge or crash anything upstream. The
# in-flight lock (held by the submitter on our behalf) and all scratch are
# released on exit. Scratch lives next to <analyst-out>, i.e. inside the
# meeting's RAM dir — extracted corpus text never touches durable storage.
deep_worker() {
	local transcript="${1:?}" out="${2:?}" query="${3:?}"
	query="${query//$'\n'/ }"
	local lock="$out.deeplock"
	local scratch=""
	# The trap fires at script EXIT, when these locals may already be gone —
	# expand with :- defaults or set -u aborts the cleanup itself.
	SCRIBE_DEEP_CLEANUP_SCRATCH=""
	SCRIBE_DEEP_CLEANUP_LOCK="$lock"
	trap 'rm -f "${SCRIBE_DEEP_CLEANUP_SCRATCH:-}" 2>/dev/null || true; rm -rf "${SCRIBE_DEEP_CLEANUP_LOCK:-}" 2>/dev/null || true' EXIT

	local answer="" failed=""
	local corpus="${SCRIBE_DEEP_CORPUS_ROOT:-}"
	if [[ -z "$corpus" || ! -d "$corpus" ]]; then
		failed="no corpus configured (SCRIBE_DEEP_CORPUS_ROOT)"
	else
		local max_files doc2text
		max_files="$(deep_max_files)"
		doc2text="${SCRIBE_DOC2TEXT_CMD:-$(dirname "$0")/scribe-doc2text}"
		local files
		files="$(find_corpus_files "$corpus" "$query" "$max_files")"
		if [[ -z "$files" ]]; then
			answer="No corpus document matched this query (searched $corpus, fixed-string)."
		else
			scratch="$(mktemp "$(dirname "$out")/.deep-scratch.XXXXXX")" || scratch=""
			SCRIBE_DEEP_CLEANUP_SCRATCH="$scratch"
			if [[ -z "$scratch" ]]; then
				failed="could not create scratch file"
			else
				build_deep_prompt_header "$query" > "$scratch"
				local f
				while IFS= read -r f; do
					printf '\n=== source: %s ===\n' "$f" >> "$scratch"
					local extracted
					extracted="$(mktemp "$(dirname "$out")/.deep-extract.XXXXXX")" || extracted=""
					if [[ -n "$extracted" ]] && bash "$doc2text" "$f" > "$extracted" 2>/dev/null; then
						head -c 200000 "$extracted" >> "$scratch"
					else
						printf '[could not extract %s]\n' "$f" >> "$scratch"
					fi
					rm -f "$extracted" 2>/dev/null || true
				done <<< "$files"

				local llm_cmd="${SCRIBE_LLM_CMD_DEEP:-${SCRIBE_LLM_CMD:-claude -p}}"
				local secs answer_file
				secs="$(deep_timeout)"
				# Capture to a FILE, not a command substitution: a timed-out
				# model command can leave orphaned grandchildren holding a
				# substitution pipe open long after the timeout fired.
				answer_file="$(mktemp "$(dirname "$out")/.deep-answer.XXXXXX")" || answer_file=""
				if [[ -z "$answer_file" ]]; then
					failed="could not create answer file"
				elif run_with_timeout "$secs" bash -c "$llm_cmd" < "$scratch" > "$answer_file" 2>/dev/null; then
					answer="$(cat "$answer_file" 2>/dev/null || true)"
					if [[ -z "$(printf '%s' "$answer" | tr -d '[:space:]')" ]]; then
						failed="retrieval command returned nothing"
					fi
				else
					failed="retrieval failed or timed out (${secs}s) — command: $llm_cmd"
				fi
				rm -f "$answer_file" 2>/dev/null || true
			fi
		fi
	fi

	local block
	block="$(mktemp "$(dirname "$out")/.deep-block.XXXXXX")" || return 0
	{
		printf -- '--- retrieved: %s ---\n' "$query"
		if [[ -n "$failed" ]]; then
			printf 'RETRIEVAL FAILED: %s\n' "$failed"
		else
			printf '%s\n' "$answer"
		fi
	} > "$block"
	append_deep_block "$out" "$block" || true
	rm -f "$block" 2>/dev/null || true
	render_analyst_out "$out" || true
	return 0
}

# --- pane rendering ------------------------------------------------------

# Compose <analyst-out> = latest light brief + a bounded tail of retrieval
# blocks. The bound is STATED in the output — silent truncation reads as
# completeness (§6.5). Atomic (temp + rename in the same dir) so the pane
# tail never sees a half-written file.
render_analyst_out() {
	local out="${1:?}"
	local brief=""
	if [[ -f "$out.brief" ]]; then
		brief="$(cat "$out.brief" 2>/dev/null || true)"
	fi
	local tmp
	tmp="$(mktemp "$out.render.XXXXXX")" || return 1
	{
		printf '%s\n' "$brief"
		if [[ -s "$out.deep" ]]; then
			local limit total shown
			limit="$(deep_max_blocks)"
			total="$(grep -c -- '^--- retrieved:' "$out.deep" 2>/dev/null || true)"
			total="${total:-0}"
			shown="$total"
			if [[ "$total" -gt "$limit" ]]; then
				shown="$limit"
			fi
			printf -- '\n--- retrievals (showing last %s of %s) ---\n' "$shown" "$total"
			awk -v n="$limit" '
				/^--- retrieved:/ { starts[++c] = NR }
				{ lines[NR] = $0 }
				END {
					if (c == 0) exit
					first = (c > n) ? starts[c - n + 1] : starts[1]
					for (i = first; i <= NR; i++) print lines[i]
				}
			' "$out.deep"
		fi
	} > "$tmp"
	mv -f "$tmp" "$out"
	# Record how much of the deep file this render consumed (see
	# render_if_deep_updated for why this is a byte count, not an mtime).
	transcript_size "$out.deep" > "$out.deepsize" 2>/dev/null || true
	return 0
}

# On quiet ticks the brief must NOT be rewritten (the pane would flicker) —
# unless a deep block landed since the last render, which must surface even
# in a silent meeting. Staleness is tracked by the deep file's byte count
# recorded at render time — an mtime comparison (-nt) is second-granular on
# some bash/filesystem combinations and misses a block that lands within
# the same second as the previous render.
rendered_deep_size() {
	local out="${1:?}" val=""
	if [[ -f "$out.deepsize" ]]; then
		val="$(cat "$out.deepsize" 2>/dev/null || true)"
	fi
	echo "${val:-0}"
}

render_if_deep_updated() {
	local out="${1:?}"
	if [[ ! -s "$out.deep" ]]; then
		return 0
	fi
	local current
	current="$(transcript_size "$out.deep")"
	if [[ ! -e "$out" || "$current" != "$(rendered_deep_size "$out")" ]]; then
		render_analyst_out "$out"
	fi
	return 0
}

# --- light tier ----------------------------------------------------------

# Run exactly one tick: drain any pending retrieval, then if the transcript
# has grown since the last recorded offset, run the LLM over the new bytes,
# update the pane, and dispatch at most one triggered retrieval. If nothing
# new has arrived, return without touching the brief (deep refresh aside).
analyst_tick() {
	local transcript="${1:?}" out="${2:?}"

	drain_pending "$transcript" "$out"

	local size offset
	size="$(transcript_size "$transcript")"
	offset="$(read_offset "$out")"

	if [[ "$size" -le "$offset" ]]; then
		render_if_deep_updated "$out"
		return 0
	fi

	local delta
	delta="$(tail -c "+$((offset + 1))" "$transcript" 2>/dev/null || true)"

	# Delta is only whitespace (e.g. a flushed-but-not-yet-worded gap):
	# nothing worth analyzing, but still advance the offset so it isn't
	# re-read forever.
	if [[ -z "$(printf '%s' "$delta" | tr -d '[:space:]')" ]]; then
		write_offset "$out" "$size"
		render_if_deep_updated "$out"
		return 0
	fi

	local llm_cmd="${SCRIBE_LLM_CMD:-claude -p}"
	local screen prompt brief
	screen="$(capture_screen_context)"
	prompt="$(build_prompt "$delta" "$screen")"
	if brief="$(printf '%s' "$prompt" | bash -c "$llm_cmd" 2>/dev/null)"; then
		local trigger pane
		trigger="$(printf '%s\n' "$brief" | extract_trigger)"
		pane="$(printf '%s\n' "$brief" | strip_trigger)"
		printf '%s\n' "$pane" > "$out.brief"
		render_analyst_out "$out"
		if [[ -n "$trigger" ]] && deep_enabled; then
			submit_deep "$transcript" "$out" "$trigger"
		fi
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
	--extract-trigger)
		extract_trigger
		exit 0
		;;
	--deep-worker)
		if [[ $# -lt 4 ]]; then
			usage
			exit 1
		fi
		deep_worker "$2" "$3" "$4"
		exit 0
		;;
	--deep-submit)
		if [[ $# -lt 4 ]]; then
			usage
			exit 1
		fi
		submit_deep "$2" "$3" "$4"
		exit 0
		;;
	--append-deep)
		if [[ $# -lt 3 ]]; then
			usage
			exit 1
		fi
		append_deep_block "$2" "$3"
		exit $?
		;;
	--render)
		if [[ $# -lt 2 ]]; then
			usage
			exit 1
		fi
		render_analyst_out "$2"
		exit $?
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
