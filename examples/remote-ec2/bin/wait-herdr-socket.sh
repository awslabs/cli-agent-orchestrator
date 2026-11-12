#!/usr/bin/env bash
# wait-herdr-socket.sh: cao-server.service's ExecStartPre gate.
#
# When CAO starts and finds no herdr socket for its session, it starts a herdr
# server of its own. That herdr would skip herdr-start.sh (no clean layout, no
# minimal environment) and live in cao-server's cgroup, so restarting CAO
# would kill every pane. This gate waits for the socket that herdr-cao.service
# creates, and FAILS if it never appears, so cao-server never starts without it.
#
#   BOX_ENV_FILE               default /etc/cao-box/box.env
#   HERDR_CONFIG_DIR           default $HOME/.config/herdr
#   HERDR_SOCKET_WAIT_SECONDS  default 30
set -euo pipefail

box_env="${BOX_ENV_FILE:-/etc/cao-box/box.env}"
wait_s="${HERDR_SOCKET_WAIT_SECONDS:-30}"
[[ "$wait_s" =~ ^[0-9]+$ ]] || { echo "wait-herdr-socket: HERDR_SOCKET_WAIT_SECONDS must be a number" >&2; exit 2; }

[ -r "$box_env" ] || { echo "wait-herdr-socket: cannot read $box_env (run bootstrap.sh)" >&2; exit 1; }
session="$(sed -n 's/^CAO_BOX_SESSION=//p' "$box_env" | head -n 1)"
if ! [[ "$session" =~ ^cao-[a-z0-9-]+$ ]]; then
  echo "wait-herdr-socket: CAO_BOX_SESSION missing or malformed in $box_env" >&2
  exit 1
fi

sock="${HERDR_CONFIG_DIR:-$HOME/.config/herdr}/sessions/${session}/herdr.sock"
waited=0
while [ ! -S "$sock" ]; do
  if [ "$waited" -ge "$wait_s" ]; then
    echo "wait-herdr-socket: no herdr socket at $sock after ${wait_s}s; not starting cao-server" >&2
    echo "wait-herdr-socket: check: systemctl status herdr-cao.service" >&2
    exit 1
  fi
  sleep 1
  waited=$((waited + 1))
done
echo "wait-herdr-socket: herdr socket ready for ${session}"
