#!/usr/bin/env bash
# restore.sh: restore a backup made by backup.sh.
#
#   restore.sh --drill BACKUP [--to DIR]
#       Unpack BACKUP into DIR (default: a new temp folder), run
#       PRAGMA integrity_check and list the tables. Touches nothing live.
#       Do this monthly.
#
#   restore.sh --live BACKUP --yes [--db PATH]
#       Stop cao-server, keep the current DB as <db>.pre-restore-<stamp>,
#       put the checked backup in its place, start cao-server, wait for
#       /health. herdr is not touched, so running panes keep running.
#
# BACKUP is a .db.gz or .db file, or an s3:// URI (fetched with the instance
# role first).
#
#   CAO_DB      default $HOME/.aws/cli-agent-orchestrator/db/cli-agent-orchestrator.db
#   CAO_URL     default http://127.0.0.1:9889
#   SYSTEMCTL   default "sudo systemctl"
set -euo pipefail

mode=""; backup=""; to=""; yes=0
db="${CAO_DB:-$HOME/.aws/cli-agent-orchestrator/db/cli-agent-orchestrator.db}"
cao_url="${CAO_URL:-http://127.0.0.1:9889}"
read -r -a systemctl_cmd <<< "${SYSTEMCTL:-sudo systemctl}"

usage() { sed -n '2,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --drill) mode=drill; backup="${2:-}"; shift ;;
    --live) mode=live; backup="${2:-}"; shift ;;
    --to) to="$2"; shift ;;
    --db) db="$2"; shift ;;
    --yes) yes=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "restore: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$mode" ] && [ -n "$backup" ] || { usage >&2; exit 2; }
command -v sqlite3 >/dev/null 2>&1 || { echo "restore: sqlite3 is not installed" >&2; exit 1; }

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
  cp -- "${work}/restored.db" "${to}/cli-agent-orchestrator.db"
  echo "restore: drill copy at ${to}/cli-agent-orchestrator.db"
  echo "restore: tables: $(sqlite3 "${to}/cli-agent-orchestrator.db" "SELECT group_concat(name, ' ') FROM sqlite_master WHERE type = 'table';")"
  exit 0
fi

# --live
if [ "$yes" -ne 1 ]; then
  echo "restore: --live replaces the live database; re-run with --yes to confirm" >&2
  exit 2
fi
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
  cp -p -- "$db" "${db}.pre-restore-${stamp}"
  echo "restore: kept the old database as ${db}.pre-restore-${stamp}"
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
