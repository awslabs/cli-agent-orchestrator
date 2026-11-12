#!/usr/bin/env bash
# restore.sh: restore a backup made by backup.sh.
#
#   restore.sh --drill BACKUP [--to DIR]
#       Unpack BACKUP into DIR (default: a new temp folder), run
#       PRAGMA integrity_check and list the tables. DIR must be new or empty,
#       and must not be inside the live CAO home: a drill never touches
#       anything live. Do this monthly.
#
#   sudo restore.sh --live BACKUP --yes        (root only, from the admin shell)
#       Reads the box user from /etc/cao-box/box-user.env (written by
#       bootstrap.sh) and finds the live DB in that user's home. Checks the
#       backup, stops cao-server, keeps the current DB as
#       <db>.pre-restore-<stamp>, puts the backup in its place (both owned by
#       the box user, with the live DB's mode), starts cao-server, waits for
#       /health and checks the live DB has the restored tables. herdr is not
#       touched, so running panes keep running. Short-lived `cao` CLI commands
#       also open the DB: don't run any during a live restore.
#
# BACKUP is a .db.gz or .db file, or an s3:// URI (fetched with the instance
# role first).
#
#   Drill (as the box user):
#     CAO_HOME    default $HOME/.aws/cli-agent-orchestrator
#     CAO_DB      default $CAO_HOME/db/cli-agent-orchestrator.db
#   Live (as root):
#     CAO_BOX_ETC default /etc/cao-box (must be owned by root, not writable
#                 by others; tests point it at a folder they own)
#     CAO_URL     default http://127.0.0.1:9889
#     SYSTEMCTL   default "systemctl"
set -euo pipefail

mode=""; backup=""; to=""; yes=0; db_given=0
cao_home="${CAO_HOME:-$HOME/.aws/cli-agent-orchestrator}"
db="${CAO_DB:-$cao_home/db/cli-agent-orchestrator.db}"
cao_url="${CAO_URL:-http://127.0.0.1:9889}"
box_etc="${CAO_BOX_ETC:-/etc/cao-box}"
read -r -a systemctl_cmd <<< "${SYSTEMCTL:-systemctl}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${here}/lib-cao-box.sh" ]; then
  # shellcheck source=SCRIPTDIR/bin/lib-cao-box.sh
  . "${here}/lib-cao-box.sh"
else
  # shellcheck source=SCRIPTDIR/bin/lib-cao-box.sh
  . "${here}/bin/lib-cao-box.sh"
fi

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"; }
need_value() { [ -n "${2:-}" ] || { echo "restore: $1 needs a value" >&2; exit 2; }; }
while [ $# -gt 0 ]; do
  case "$1" in
    --drill) need_value "$1" "${2:-}"; mode=drill; backup="$2"; shift ;;
    --live) need_value "$1" "${2:-}"; mode=live; backup="$2"; shift ;;
    --to) need_value "$1" "${2:-}"; to="$2"; shift ;;
    --db) need_value "$1" "${2:-}"; db="$2"; db_given=1; shift ;;
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
else
  if [ "$yes" -ne 1 ]; then
    echo "restore: --live replaces the live database; re-run with --yes to confirm" >&2
    exit 2
  fi
  if [ "$(id -u)" != "0" ]; then
    echo "restore: --live is root-only: run it with sudo from the admin shell" >&2
    exit 2
  fi
  if [ "$db_given" -eq 1 ]; then
    echo "restore: --live finds the database from ${box_etc}/box-user.env; --db is for drills only" >&2
    exit 2
  fi
  user_env="${box_etc}/box-user.env"
  if ! cb_safe_path "$box_etc" 0 || ! cb_safe_path "$user_env" 0; then
    echo "restore: ${user_env} must be root's and not writable by others" >&2
    exit 1
  fi
  box_user="$(sed -n 's/^BOX_USER=//p' "$user_env")"
  box_uid="$(sed -n 's/^BOX_UID=//p' "$user_env")"
  box_home="$(sed -n 's/^BOX_HOME=//p' "$user_env")"
  if ! [[ "$box_user" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || ! [[ "$box_uid" =~ ^[0-9]+$ ]] \
     || ! [[ "$box_home" =~ ^/[A-Za-z0-9._/-]+$ ]] || [ "$(wc -l < "$user_env" | tr -d ' ')" != "3" ]; then
    echo "restore: ${user_env} must hold exactly BOX_USER, BOX_UID and BOX_HOME" >&2
    exit 1
  fi
  passwd="$(getent passwd "$box_user")" || { echo "restore: no user ${box_user}" >&2; exit 1; }
  [ "$(printf '%s' "$passwd" | cut -d: -f3)" = "$box_uid" ] || { echo "restore: ${box_user}'s uid isn't ${box_uid}" >&2; exit 1; }
  [ "$(printf '%s' "$passwd" | cut -d: -f6)" = "$box_home" ] || { echo "restore: ${box_user}'s home isn't ${box_home}" >&2; exit 1; }
  box_gid="$(printf '%s' "$passwd" | cut -d: -f4)"
  db="${box_home}/.aws/cli-agent-orchestrator/db/cli-agent-orchestrator.db"
  [ -f "$db" ] || { echo "restore: no live database at ${db}" >&2; exit 1; }
  db_info="$(cb_owner_mode "$db")"
  [ "${db_info%% *}" = "$box_uid" ] || { echo "restore: ${db} isn't owned by ${box_user} (uid ${box_uid}); refusing" >&2; exit 1; }
  db_mode="${db_info##* }"
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
kept="${db}.pre-restore-${stamp}"
# install -o/-g: the files belong to the box user, with the live DB's mode.
install -o "$box_uid" -g "$box_gid" -m "$db_mode" "$db" "$kept"
echo "restore: kept the old database as ${kept}"
install -o "$box_uid" -g "$box_gid" -m "$db_mode" "${work}/restored.db" "${db}.restoring"
mv -- "${db}.restoring" "$db"
"${systemctl_cmd[@]}" start cao-server.service

want_tables="$(sqlite3 -readonly "${work}/restored.db" "SELECT group_concat(name, ' ') FROM (SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name);")"
for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 "${cao_url}/health" >/dev/null 2>&1; then
    have_tables="$(sqlite3 -readonly "$db" "SELECT group_concat(name, ' ') FROM (SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name);")"
    if [ "$have_tables" != "$want_tables" ]; then
      echo "restore: /health answers, but the live DB's tables differ from the backup's; check ${db}" >&2
      exit 1
    fi
    echo "restore: cao-server is back on the restored database and /health answers"
    exit 0
  fi
  sleep 1
done
echo "restore: cao-server did not answer /health in 30 s; check journalctl -u cao-server" >&2
exit 1
