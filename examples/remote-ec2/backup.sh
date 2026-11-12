#!/usr/bin/env bash
# backup.sh: a safe, online copy of CAO's SQLite database.
#
#   1. sqlite3 .backup (the online backup API: CAO can keep running);
#   2. PRAGMA integrity_check on the copy (refuse to keep a bad copy);
#   3. gzip;
#   4. aws s3 cp to BACKUP_S3_URI (a versioned bucket), unless --no-upload
#      or BACKUP_S3_URI is empty;
#   5. keep the newest BACKUP_KEEP local copies.
#
# cao-backup.service runs this nightly as the box user. It uses the instance
# role for S3; it never reads a profile or a key file.
#
#   backup.sh [--db PATH] [--dest DIR] [--keep N] [--no-upload]
#
#   CAO_DB         default $HOME/.aws/cli-agent-orchestrator/db/cli-agent-orchestrator.db
#   BACKUP_DIR     default $HOME/backups/cao
#   BACKUP_KEEP    default 14
#   BACKUP_S3_URI  for example s3://<bucket>/cao-box/   (empty = no upload)
set -euo pipefail

db="${CAO_DB:-$HOME/.aws/cli-agent-orchestrator/db/cli-agent-orchestrator.db}"
dest="${BACKUP_DIR:-$HOME/backups/cao}"
keep="${BACKUP_KEEP:-14}"
s3_uri="${BACKUP_S3_URI:-}"
upload=1

while [ $# -gt 0 ]; do
  case "$1" in
    --db) db="$2"; shift ;;
    --dest) dest="$2"; shift ;;
    --keep) keep="$2"; shift ;;
    --no-upload) upload=0 ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "backup: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

command -v sqlite3 >/dev/null 2>&1 || { echo "backup: sqlite3 is not installed" >&2; exit 1; }
[ -f "$db" ] || { echo "backup: no database at $db" >&2; exit 1; }
[[ "$keep" =~ ^[1-9][0-9]*$ ]] || { echo "backup: --keep must be a positive number" >&2; exit 2; }
case "$dest" in *"'"*) echo "backup: the destination path must not contain a single quote" >&2; exit 2 ;; esac

mkdir -p "$dest"
chmod 0700 "$dest"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
partial="${dest}/.cao-${stamp}.db.partial"
final="${dest}/cao-${stamp}.db.gz"

cleanup() { [ -e "$partial" ] && rm -f -- "$partial"; return 0; }
trap cleanup EXIT

sqlite3 "$db" ".backup '${partial}'"
check="$(sqlite3 "$partial" 'PRAGMA integrity_check;')"
if [ "$check" != "ok" ]; then
  echo "backup: integrity_check failed on the copy; nothing kept" >&2
  exit 1
fi
gzip -n -c "$partial" > "${final}.tmp"
mv -- "${final}.tmp" "$final"
chmod 0600 "$final"
echo "backup: wrote ${final} (integrity_check ok)"

if [ "$upload" -eq 1 ] && [ -n "$s3_uri" ]; then
  case "$s3_uri" in s3://*/) ;; *) echo "backup: BACKUP_S3_URI must look like s3://<bucket>/<prefix>/" >&2; exit 2 ;; esac
  aws s3 cp --only-show-errors "$final" "${s3_uri}$(basename "$final")"
  echo "backup: uploaded to ${s3_uri}$(basename "$final")"
elif [ "$upload" -eq 1 ]; then
  echo "backup: BACKUP_S3_URI is empty; kept the local copy only" >&2
fi

# Keep the newest $keep copies. The names sort by time (UTC stamps).
old="$(find "$dest" -maxdepth 1 -type f -name 'cao-*.db.gz' | LC_ALL=C sort -r | tail -n +"$((keep + 1))")"
if [ -n "$old" ]; then
  while IFS= read -r f; do
    rm -f -- "$f"
    echo "backup: pruned $(basename "$f")"
  done <<< "$old"
fi
