#!/usr/bin/env bash
# box-resume.sh: read-only checks after the box reboots (or herdr restarts).
# It starts nothing and launches nothing; it tells you what is up and what to
# do next. Exit 0 when everything answered, 1 otherwise.
#
#   CAO_URL        default http://127.0.0.1:9889
#   BOX_ENV_FILE   default /etc/cao-box/box.env
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

session="$(sed -n 's/^CAO_BOX_SESSION=//p' "$box_env" 2>/dev/null | head -n 1)"
printf 'session: %s\n' "${session:-<unknown: no $box_env>}"

check "herdr-cao.service is active" systemctl is-active --quiet herdr-cao.service
check "cao-server.service is active" systemctl is-active --quiet cao-server.service
check "cao-backup.timer is active" systemctl is-active --quiet cao-backup.timer
check "cao-server /health answers" curl -fsS --max-time 5 "${cao_url}/health"
check "herdr status answers" herdr status

if [ "$failures" -gt 0 ]; then
  echo
  echo "Some checks failed. Look at: journalctl -u herdr-cao -u cao-server -b"
  exit 1
fi
cat <<'TXT'

All up. herdr started with an empty layout (the old one is in
~/.config/herdr/sessions/<session>/restore-archive/). Next, by hand:
  - look in the cao-server log for the startup "ghost terminal" cleanup line;
  - relaunch your agents with `cao launch ...` (add --resume-session-id to
    continue a Claude conversation), and wait until each is idle before typing.
TXT
