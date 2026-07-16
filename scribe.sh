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
