#!/usr/bin/env bash
# herdr-start.sh: the herdr-cao.service ExecStart.
#
# 1. Reads the session name from /etc/cao-box/box.env (parsed, not sourced).
# 2. Runs herdr-clean-layout.sh for that session.
# 3. Starts `herdr --session <name> server` with ONLY HOME, USER, PATH, LANG and
#    TERM in its environment (env -i). Every pane inherits the herdr server's
#    environment, so nothing else (no systemd extras, no tokens) reaches them.
#
#   BOX_ENV_FILE   default /etc/cao-box/box.env
#   HERDR_BIN      default $HOME/.local/bin/herdr
set -euo pipefail

box_env="${BOX_ENV_FILE:-/etc/cao-box/box.env}"
herdr_bin="${HERDR_BIN:-$HOME/.local/bin/herdr}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ -r "$box_env" ] || { echo "herdr-start: cannot read $box_env (run bootstrap.sh)" >&2; exit 1; }
session="$(sed -n 's/^CAO_BOX_SESSION=//p' "$box_env" | head -n 1)"
if ! [[ "$session" =~ ^cao-[a-z0-9-]+$ ]]; then
  echo "herdr-start: CAO_BOX_SESSION missing or malformed in $box_env" >&2
  exit 1
fi

"${here}/herdr-clean-layout.sh" "$session"

exec env -i \
  HOME="$HOME" \
  USER="${USER:-$(id -un)}" \
  PATH="${PATH}" \
  LANG="${LANG:-C.UTF-8}" \
  TERM="${TERM:-xterm-256color}" \
  "$herdr_bin" --session "$session" server
