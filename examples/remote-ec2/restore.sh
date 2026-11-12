#!/usr/bin/env bash
# restore.sh: restore a backup made by backup.sh.
#
#   restore.sh --drill BACKUP [--to DIR]
#       Unpack BACKUP into DIR (default: a new temp folder), run
#       PRAGMA integrity_check and list the tables. DIR must be new or empty,
#       and must not be inside the live CAO home: a drill never touches
#       anything live. Do this monthly.
#
#   restore.sh --live BACKUP --yes [--db PATH]
#       Check the backup, stop cao-server, keep the current DB as
#       <db>.pre-restore-<stamp> (mode 0600), put the backup in its place,
#       start cao-server, wait for /health. herdr is not touched, so running
#       panes keep running. Short-lived `cao` CLI commands also open the DB:
#       don't run any during a live restore.
#
# BACKUP is a .db.gz or .db file, or an s3:// URI (fetched with the instance
# role first).
#
#   CAO_HOME    default $HOME/.aws/cli-agent-orchestrator
#   CAO_DB      default $CAO_HOME/db/cli-agent-orchestrator.db
#   CAO_URL     default http://127.0.0.1:9889
#   SYSTEMCTL   default "sudo systemctl"
set -euo pipefail

mode=""; backup=""; to=""; yes=0
cao_home="${CAO_HOME:-$HOME/.aws/cli-agent-orchestrator}"
db="${CAO_DB:-$cao_home/db/cli-agent-orchestrator.db}"
cao_url="${CAO_URL:-http://127.0.0.1:9889}"
read -r -a systemctl_cmd <<< "${SYSTEMCTL:-sudo systemctl}"

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"; }
need_value() { [ -n "${2:-}" ] || { echo "restore: $1 needs a value" >&2; exit 2; }; }
while [ $# -gt 0 ]; do
  case "$1" in
    --drill) need_value "$1" "${2:-}"; mode=drill; backup="$2"; shift ;;
    --live) need_value "$1" "${2:-}"; mode=live; backup="$2"; shift ;;
    --to) need_value "$1" "${2:-}"; to="$2"; shift ;;
    --db) need_value "$1" "${2:-}"; db="$2"; shift ;;
    --yes) yes=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "restore: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
if [ -z "$mode" ]; then
  usage >&2
  exit 2
fi
command -v sqlite3 >/dev/null 2>&1 || { echo "restore: sqlite3 is not installed" >&2; exit 1; }

# real_dir PATH: the physical path of PATH, or of its nearest existing parent
# with the rest appended (PATH need not exist yet).
real_dir() {
  local p="$1" rest=""
  while [ ! -d "$p" ]; do
    rest="/$(basename "$p")${rest}"
    p="$(dirname "$p")"
  done
  printf '%s%s\n' "$(cd "$p" && pwd -P)" "$rest"
}

if [ "$mode" = drill ]; then
  if [ -n "$to" ]; then
    to_real="$(real_dir "$to")"
    for live in "$cao_home" "$(dirname "$db")"; do
      live_real="$(real_dir "$live")"
      case "${to_real}/" in
        "${live_real}/"*)
          echo "restore: --to ${to} is inside the live CAO home (${live}); a drill never writes there" >&2
          exit 2 ;;
      esac
    done
    if [ -d "$to" ] && [ -n "$(ls -A "$to")" ]; then
      echo "restore: --to ${to} is not empty; give a new or empty folder" >&2
      exit 2
    fi
  fi
elif [ "$yes" -ne 1 ]; then
  echo "restore: --live replaces the live database; re-run with --yes to confirm" >&2
  exit 2
fi

work="$(mktemp -d)"
trap 'rm -rf -- "${work:?}"' EXIT

# Fetch and unpack into $work/restored.db
src="$backup"
case "$backup" in
  s3://*) aws s3 cp --only-show-errors "$backup" "${work}/$(basename "$backup")"; src="${work}/$(basename "$backup")" ;;
esac
[ -f "$src" ] || { echo "restore: no backup file at $src" >&2; exit 1; }
case "$src" in
  *.gz) gzip -d -c "$src" > "${work}/restored.db" ;;
  *) cp -- "$src" "${work}/restored.db" ;;
esac

check="$(sqlite3 "${work}/restored.db" 'PRAGMA integrity_check;')"
if [ "$check" != "ok" ]; then
  echo "restore: integrity_check failed on the backup; nothing changed" >&2
  exit 1
fi
echo "restore: integrity_check ok"

if [ "$mode" = drill ]; then
  [ -n "$to" ] || to="$(mktemp -d)"
  mkdir -p "$to"
  chmod 0700 "$to"
  cp -- "${work}/restored.db" "${to}/cli-agent-orchestrator.db"
  chmod 0600 "${to}/cli-agent-orchestrator.db"
  echo "restore: drill copy at ${to}/cli-agent-orchestrator.db"
  echo "restore: tables: $(sqlite3 "${to}/cli-agent-orchestrator.db" "SELECT group_concat(name, ' ') FROM sqlite_master WHERE type = 'table';")"
  exit 0
fi

# --live: stop first, then check nothing is left half-written, keep a copy, swap.
"${systemctl_cmd[@]}" stop cao-server.service
for side in -journal -wal -shm; do
  if [ -e "${db}${side}" ]; then
    echo "restore: ${db}${side} exists after stopping cao-server; not restoring over it" >&2
    echo "restore: cao-server is stopped; investigate, then start it again" >&2
    exit 1
  fi
done
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$(dirname "$db")"
if [ -f "$db" ]; then
  kept="${db}.pre-restore-${stamp}"
  ( umask 077; cp -- "$db" "$kept" )
  chmod 0600 "$kept"
  echo "restore: kept the old database as ${kept}"
fi
cp -- "${work}/restored.db" "${db}.restoring"
chmod 0600 "${db}.restoring"
mv -- "${db}.restoring" "$db"
"${systemctl_cmd[@]}" start cao-server.service

for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 "${cao_url}/health" >/dev/null 2>&1; then
    echo "restore: cao-server is back and /health answers"
    exit 0
  fi
  sleep 1
done
echo "restore: cao-server did not answer /health in 30 s; check journalctl -u cao-server" >&2
exit 1
