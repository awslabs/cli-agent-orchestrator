#!/usr/bin/env bash
# herdr-clean-layout.sh SESSION: run before the herdr server starts.
#
# After a reboot or a herdr restart every pane process is gone. If herdr
# restored the saved layout, the panes would come back as bare shells or as
# agents without CAO's launch flags, and an inbox message pasted into a bare
# shell can run as commands. So this moves the saved layout aside (into a
# dated restore-archive/ folder) and herdr starts empty; CAO relaunches agents
# with the right flags. herdr's own session-snapshots/ are left alone.
#
#   HERDR_CONFIG_DIR   default $HOME/.config/herdr
set -euo pipefail

session="${1:-}"
if ! [[ "$session" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]]; then
  echo "herdr-clean-layout: usage: herdr-clean-layout.sh <session-name>" >&2
  exit 2
fi

dir="${HERDR_CONFIG_DIR:-$HOME/.config/herdr}/sessions/${session}"
layout="${dir}/session.json"

if [ ! -f "$layout" ]; then
  echo "herdr-clean-layout: no saved layout for ${session}; nothing to do"
  exit 0
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="${dir}/restore-archive/${stamp}"
mkdir -p "$archive"
mv -- "$layout" "${archive}/session.json"
echo "herdr-clean-layout: archived the saved layout to ${archive}/session.json"
