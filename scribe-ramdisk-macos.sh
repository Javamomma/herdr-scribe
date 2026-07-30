#!/usr/bin/env bash
# scribe-ramdisk-macos.sh — create (or tear down) a small macOS RAM disk for
# use as SCRIBE_RAMROOT, restoring the RAM-only transcript guarantee on a
# platform with no tmpfs. No root/sudo required.
#
# Usage:
#   scribe-ramdisk-macos.sh            # create/mount, print the mount point
#   scribe-ramdisk-macos.sh --detach   # unmount + free the RAM disk
#   scribe-ramdisk-macos.sh --status   # print the mount point if mounted
#
# Size via SCRIBE_RAMDISK_MB (default 64 — transcripts are text; this is
# plenty). Then:
#   export SCRIBE_RAMROOT="$(bash scribe-ramdisk-macos.sh)"
#
# The volume name is fixed ("scribe-ram") so repeat runs find the existing
# disk instead of stacking new ones. Contents vanish on detach or reboot —
# that is the point.
set -euo pipefail

VOLNAME="scribe-ram"
MOUNTPOINT="/Volumes/$VOLNAME"

usage() {
	echo "usage: scribe-ramdisk-macos.sh [--detach|--status]" >&2
	echo "  creates a RAM disk and prints its mount point; point SCRIBE_RAMROOT at it" >&2
}

if [[ "$(uname -s)" != "Darwin" ]]; then
	echo "scribe-ramdisk-macos.sh: warning: this helper is macOS-only (on Linux use /dev/shm, which scribe already defaults to)" >&2
	exit 1
fi

case "${1:-}" in
	-h|--help)
		usage
		exit 0
		;;
	--status)
		if mount | grep -q " $MOUNTPOINT "; then
			echo "$MOUNTPOINT"
			exit 0
		fi
		echo "scribe-ramdisk-macos.sh: not mounted" >&2
		exit 1
		;;
	--detach)
		if ! mount | grep -q " $MOUNTPOINT "; then
			echo "scribe-ramdisk-macos.sh: not mounted; nothing to detach" >&2
			exit 0
		fi
		diskutil eject "$MOUNTPOINT" >/dev/null
		echo "detached $MOUNTPOINT" >&2
		exit 0
		;;
	"")
		;;
	*)
		usage
		exit 64
		;;
esac

if mount | grep -q " $MOUNTPOINT "; then
	echo "$MOUNTPOINT"
	exit 0
fi

SIZE_MB="${SCRIBE_RAMDISK_MB:-64}"
if ! [[ "$SIZE_MB" =~ ^[0-9]+$ ]] || [[ "$SIZE_MB" -lt 8 ]]; then
	echo "scribe-ramdisk-macos.sh: warning: SCRIBE_RAMDISK_MB='$SIZE_MB' invalid; using 64" >&2
	SIZE_MB=64
fi
SECTORS=$((SIZE_MB * 2048))

DEV="$(hdiutil attach -nomount "ram://$SECTORS" | tr -d '[:space:]')"
if [[ -z "$DEV" ]]; then
	echo "scribe-ramdisk-macos.sh: warning: hdiutil could not allocate a RAM disk" >&2
	exit 1
fi
if ! diskutil erasevolume HFS+ "$VOLNAME" "$DEV" >/dev/null; then
	hdiutil detach "$DEV" >/dev/null 2>&1 || true
	echo "scribe-ramdisk-macos.sh: warning: could not format/mount the RAM disk" >&2
	exit 1
fi
echo "$MOUNTPOINT"
