#!/usr/bin/env bash
# Scribe status action: current meeting + bridge/backend availability.
# Output lands in `herdr plugin log list --plugin scribe`.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

echo "meeting: $(scribe status)"
scribe --doctor
exit 0
