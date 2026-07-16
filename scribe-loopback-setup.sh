#!/usr/bin/env bash
# scribe-loopback-setup.sh — build the optional Windows remote-participant
# loopback bridge exe.
#
# Compiles scribe-loopback.cs (a minimal userland WASAPI loopback capture,
# via NAudio) using the in-box .NET Framework C# compiler (csc.exe) — no
# Visual Studio, no elevated privileges, no meeting-app/tenant access
# required, either to build or to run the result.
#
# This bridge is Windows-only. This script is fully runnable here (Linux/
# WSL2) and is expected to degrade gracefully: with no csc.exe on this
# machine, it warns and exits nonzero without touching or breaking anything
# — that is the correct, tested behavior, not a failure of the script. Only
# on a real Windows host, with csc.exe and a NAudio.dll available, does it
# actually produce scribe-loopback.exe.
#
# Usage:
#   scribe-loopback-setup.sh [--out <path-to-exe>]
#
# Configuration (env, all optional):
#   SCRIBE_CSC_EXE      path to csc.exe (searched for if unset)
#   SCRIBE_NAUDIO_DLL   path to NAudio.dll (searched for if unset)
#
# On success: compiles the exe (default: scribe-loopback.exe next to this
# script, or --out), prints its path on stdout, and prints the
# SCRIBE_LOOPBACK_EXE export line to wire it into scribe.conf on stderr.
#
# On a missing csc.exe, missing NAudio.dll, or missing scribe-loopback.cs:
# prints a "warning:" line to stderr and exits nonzero. Never deletes or
# modifies anything that already exists.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/scribe-loopback.cs"
OUT="${SCRIBE_LOOPBACK_EXE:-$HERE/scribe-loopback.exe}"

usage() {
	echo "usage: scribe-loopback-setup.sh [--out <path-to-exe>]" >&2
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--out)
			OUT="${2:?--out requires a path}"
			shift 2
			;;
		-h|--help)
			usage
			exit 0
			;;
		*)
			echo "scribe-loopback-setup.sh: unknown option: $1" >&2
			usage
			exit 1
			;;
	esac
done

# Locate csc.exe: an explicit SCRIBE_CSC_EXE, else PATH, else a short list of
# neutral (not user/machine-specific) in-box .NET Framework locations that
# show up under WSL2's /mnt/c or a Windows-native bash (Git Bash, MSYS).
find_csc() {
	if [[ -n "${SCRIBE_CSC_EXE:-}" && -x "${SCRIBE_CSC_EXE}" ]]; then
		echo "$SCRIBE_CSC_EXE"
		return 0
	fi
	if command -v csc.exe >/dev/null 2>&1; then
		command -v csc.exe
		return 0
	fi
	local candidates=(
		"/mnt/c/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe"
		"/mnt/c/Windows/Microsoft.NET/Framework/v4.0.30319/csc.exe"
		"/c/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe"
		"/c/Windows/Microsoft.NET/Framework/v4.0.30319/csc.exe"
	)
	local c
	for c in "${candidates[@]}"; do
		[[ -e "$c" ]] && { echo "$c"; return 0; }
	done
	return 1
}

# Locate NAudio.dll: an explicit SCRIBE_NAUDIO_DLL, else a copy dropped next
# to this script (the simplest self-contained option — download NAudio.dll
# once from nuget.org and place it alongside scribe-loopback.cs).
find_naudio() {
	if [[ -n "${SCRIBE_NAUDIO_DLL:-}" && -e "${SCRIBE_NAUDIO_DLL}" ]]; then
		echo "$SCRIBE_NAUDIO_DLL"
		return 0
	fi
	if [[ -e "$HERE/NAudio.dll" ]]; then
		echo "$HERE/NAudio.dll"
		return 0
	fi
	return 1
}

if [[ ! -f "$SRC" ]]; then
	echo "warning: scribe-loopback-setup.sh: source not found ($SRC); skipping build — scribe will run mic-only" >&2
	exit 1
fi

CSC="$(find_csc)" || {
	echo "warning: scribe-loopback-setup.sh: csc.exe not found (set SCRIBE_CSC_EXE, or run this on a Windows host); skipping build — scribe will run mic-only" >&2
	exit 1
}

NAUDIO="$(find_naudio)" || {
	echo "warning: scribe-loopback-setup.sh: NAudio.dll not found (set SCRIBE_NAUDIO_DLL, or place NAudio.dll next to this script); skipping build — scribe will run mic-only" >&2
	exit 1
}

echo "scribe-loopback-setup.sh: building $OUT" >&2
echo "  csc:    $CSC" >&2
echo "  naudio: $NAUDIO" >&2
echo "  source: $SRC" >&2

"$CSC" /nologo /target:exe "/out:$OUT" "/reference:$NAUDIO" "$SRC"

echo "$OUT"
echo "scribe-loopback-setup.sh: built $OUT" >&2
echo "  add to scribe.conf (or your shell profile):  export SCRIBE_LOOPBACK_EXE=\"$OUT\"" >&2
