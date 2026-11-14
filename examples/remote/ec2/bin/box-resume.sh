#!/usr/bin/env bash
# box-resume.sh: read-only checks after the box reboots (or herdr restarts).
# It starts nothing and launches nothing; it tells you what is up and what to
# do next. Exit 0 when everything answered, 1 otherwise.
#
#   CAO_URL        default http://127.0.0.1:9889
#   BOX_ENV_FILE   default /etc/cao-box/box.env
#
# Run it as BOX_USER: herdr keeps its state per user, so as another user (the
# admin shell) the herdr check looks at that user's herdr, not the box's.
#   sudo -u <you> -i /usr/local/lib/cao-box/box-resume.sh
set -uo pipefail

cao_url="${CAO_URL:-http://127.0.0.1:9889}"
box_env="${BOX_ENV_FILE:-/etc/cao-box/box.env}"
failures=0

check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    printf 'ok    %s\n' "$label"
  else
    printf 'FAIL  %s\n' "$label"
    failures=$((failures + 1))
  fi
}

# herdr_running SESSION: the session's server says "status: running". A bare
# `herdr status` exits 0 even when nothing runs, so the exit code alone
# proves nothing.
herdr_running() {
  local out
  [ -n "$1" ] || return 1
  out="$(herdr --session "$1" status 2>/dev/null)" || return 1
  printf '%s\n' "$out" | awk '
    /^server:/ { in_server = 1; next }
    /^[^ ]/    { in_server = 0 }
    in_server && $1 == "status:" { found = ($2 == "running"); exit }
    END { exit !found }'
}

session="$(sed -n 's/^CAO_BOX_SESSION=//p' "$box_env" 2>/dev/null | head -n 1)"
printf 'session: %s\n' "${session:-<unknown: no $box_env>}"

check "herdr-cao.service is active" systemctl is-active --quiet herdr-cao.service
check "cao-server.service is active" systemctl is-active --quiet cao-server.service
check "cao-backup.timer is active" systemctl is-active --quiet cao-backup.timer
check "cao-server /health answers" curl -fsS --max-time 5 "${cao_url}/health"
check "herdr session ${session:-<unknown>} is running" herdr_running "$session"

if [ "$failures" -gt 0 ]; then
  echo
  echo "Some checks failed. Look at: journalctl -u herdr-cao -u cao-server -b"
  exit 1
fi
cat <<'TXT'

All up. herdr started with an empty layout (the old one is in
~/.config/herdr/sessions/<session>/restore-archive/). Next, by hand:
  - CAO may keep terminal records of sessions from before the reboot (status
    "unknown"): remove each old session with `cao shutdown --session <name>`;
  - relaunch your agents with `cao launch ...` (add --resume-session-id to
    continue a Claude conversation), and wait until each is idle before typing.
TXT
