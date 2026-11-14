#!/usr/bin/env bash
# bootstrap.sh: set up one always-on Ubuntu 24.04 arm64 (Graviton) box to run
# herdr + CAO under systemd. Run it ON THE BOX, as root. Re-running it is safe:
# every step checks before it acts, and anything unexpected stops the run.
#
#   sudo bash bootstrap.sh --settings /opt/cao-box-src/box-settings.env --dry-run   # the plan
#   sudo bash bootstrap.sh --settings /opt/cao-box-src/box-settings.env             # do it
#   ... --format-new-volume                                                         # first run only
#
# As root it runs only from a folder that root owns and nobody else can write,
# outside BOX_HOME (for example /opt/cao-box-src/...): root sources and runs
# files from here, and every agent can write in the box user's home.
#
# It never creates AWS resources, never starts a service (it only enables
# them), and never logs in to anything. See README.md for the full flow.
#
# Required settings (in the --settings file, or environment variables):
#   BOX_USER        the Linux user that owns the agents.
#   BOX_UID         that user's uid (and gid). Use the SAME value on every
#                   rebuild so the data volume's files keep their owner.
#   BOX_HOME        that user's home, for example /Users/<you> (your laptop's
#                   home path, so absolute paths keep working) or /home/<you>.
#   HERDR_VERSION   the herdr version your laptop runs, for example v0.9.3.
#   HERDR_SHA256    sha256 of that release's herdr-linux-aarch64 asset.
#   CAO_REF         the CAO commit to install (a full sha is best).
#
# Optional settings:
#   DATA_VOLUME_ID  the EBS data volume (vol-...). Found through its NVMe
#                   serial in /dev/disk/by-id, never by device name.
#   DATA_DEVICE     only a cross-check: must resolve to the same disk.
#   DATA_MOUNT      where the volume is mounted. Default: BOX_HOME's parent.
#   INSTALL_DOCKER  yes to install Docker and add BOX_USER to the docker
#                   group. Default no. The docker group is root-equivalent.
#   BACKUP_S3_URI, BACKUP_KMS_KEY_ARN   nightly backup target and its key.
#   CAO_REPO        default: the upstream GitHub repository.
#   Tool pins (each download is checked against a sha256; see README.md):
#     GH_VERSION (checked against gh's published checksums file),
#     AWS_CLI_VERSION + AWS_CLI_SHA256, SAM_VERSION + SAM_SHA256,
#     YAZI_VERSION + YAZI_SHA256, HELIX_VERSION + HELIX_SHA256,
#     UV_VERSION + UV_SHA256.
#
# Flags:
#   --settings FILE      read the settings from FILE: KEY=VALUE lines for the
#                        names in this header only (# starts a comment). It is
#                        parsed, never sourced or passed to env, so any other
#                        name (BASH_ENV, PATH, LD_PRELOAD, ...) stops the run.
#                        FILE must not be a symlink, must belong to the user
#                        running this with no group or other write and, as
#                        root, sit in root-only folders outside BOX_HOME.
#   --dry-run            print every planned step; change nothing.
#   --format-new-volume  allow formatting DATA_VOLUME_ID, and only if it is a
#                        blank, whole, non-root disk. Env form:
#                        FORMAT_DATA_DEVICE=yes.
#   --root DIR           write files under DIR instead of / (tests, image
#                        prep). Volume, user, package, download and IMDS steps
#                        are skipped.
#
# For the tests only: BOX_TEST_OS_RELEASE, BOX_TEST_DEVFS, BOX_TEST_SYSFS and
# BOX_TEST_FSTAB point the probes at fake files. They work ONLY with --dry-run;
# a real run with any BOX_TEST_* variable set stops before step 0.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

# >>> checkout-guard
# safe_checkout DIR UID STOP: DIR, every folder above it up to STOP, and
# everything inside DIR (but symlinks, which only their owner can make) belong
# to UID, and nobody else can write them. It is inline, not from
# lib-cao-box.sh, because it decides whether that library may be sourced.
safe_checkout() {
  local p="$1" uid="$2" stop="$3" info bad
  while :; do
    if [ -L "$p" ] || [ ! -d "$p" ]; then
      echo "${p} is not a real folder" >&2; return 1
    fi
    info="$(stat -c '%u %a' "$p" 2>/dev/null || stat -f '%u %Lp' "$p")" || { echo "cannot stat ${p}" >&2; return 1; }
    if [ "${info%% *}" != "$uid" ]; then
      echo "${p} is owned by uid ${info%% *}, not ${uid}" >&2; return 1
    fi
    if ! [[ "${info##* }" =~ ^[0-7]+$ ]] || [ $(( 8#${info##* } & 8#022 )) -ne 0 ]; then
      echo "${p} is writable by group or others (mode ${info##* })" >&2; return 1
    fi
    if [ "$p" = "$stop" ] || [ "$p" = "/" ]; then break; fi
    p="$(dirname "$p")"
  done
  bad="$(find "$1" ! -type l \( ! -user "$uid" -o -perm -g+w -o -perm -o+w \) -print 2>&1 | head -n 1)"
  if [ -n "$bad" ]; then
    echo "${bad} is not owned by uid ${uid}, or is writable by group or others" >&2; return 1
  fi
}
# <<< checkout-guard
# Root sources and runs files from here: refuse a checkout anyone else can change.
if [ "$EUID" -eq 0 ] && ! safe_checkout "$HERE" 0 /; then
  printf '[bootstrap] ERROR: refusing to run as root from %s: keep the sample in a folder that only root can write, outside BOX_HOME (for example /opt/cao-box-src)\n' "$HERE" >&2
  exit 1
fi
# shellcheck source=SCRIPTDIR/bin/lib-cao-box.sh
. "$HERE/bin/lib-cao-box.sh"
SETTINGS_FILE=""
DRY_RUN=0
ROOT=""
FORMAT_NEW=0
FORMAT_FROM_ENV=0
if [ "${FORMAT_DATA_DEVICE:-}" = "yes" ]; then FORMAT_NEW=1; FORMAT_FROM_ENV=1; fi

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --format-new-volume) FORMAT_NEW=1 ;;
    --settings)
      { [ $# -ge 2 ] && [ -n "$2" ]; } || { echo "--settings needs a file" >&2; exit 2; }
      SETTINGS_FILE="$2"; shift ;;
    --root)
      [ $# -ge 2 ] || { echo "--root needs a folder" >&2; exit 2; }
      ROOT="${2%/}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

# Test hooks are for dry runs only (see the header).
test_hooks="$(compgen -v BOX_TEST_ || true)"
if [ -n "$test_hooks" ] && [ "$DRY_RUN" -ne 1 ]; then
  printf '[bootstrap] ERROR: %s set: BOX_TEST_* variables are for --dry-run tests only; unset them\n' \
    "$(printf '%s' "$test_hooks" | tr '\n' ' ')" >&2
  exit 1
fi

log()  { printf '[bootstrap] %s\n' "$*"; }
warn() { printf '[bootstrap] WARN: %s\n' "$*" >&2; }
die()  { printf '[bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }
step() { printf '\n[bootstrap] == %s\n' "$*"; }

# run CMD...: execute, or only print it in a dry run.
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] would run: %s\n' "$*"
  else
    "$@"
  fi
}

# install_file SRC DEST MODE [LABEL]: copy only if missing or different.
install_file() {
  local src="$1" dest="$2" mode="$3" label="${4:-${1#"$HERE"/}}"
  if [ -f "$dest" ] && cmp -s "$src" "$dest"; then
    log "unchanged: $dest"
    return 0
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] would install %s -> %s (mode %s)\n' "$label" "$dest" "$mode"
    return 0
  fi
  mkdir -p "$(dirname "$dest")"
  install -m "$mode" "$src" "$dest"
  log "installed: $dest"
}

# ---------------------------------------------------------------- 0. checks
step "0. check the OS, the settings and the user running this"
OS_RELEASE="${BOX_TEST_OS_RELEASE:-${ROOT}/etc/os-release}"
[ -r "$OS_RELEASE" ] || die "cannot read $OS_RELEASE; this sample supports Ubuntu 24.04 arm64 only"
os_id="$(sed -n 's/^ID=//p' "$OS_RELEASE" | tr -d '"')"
os_ver="$(sed -n 's/^VERSION_ID=//p' "$OS_RELEASE" | tr -d '"')"
arch="$(uname -m)"
if [ "$os_id" != "ubuntu" ] || [ "$os_ver" != "24.04" ] || [ "$arch" != "aarch64" ]; then
  die "this sample supports Ubuntu 24.04 on aarch64 only (found: ${os_id:-?} ${os_ver:-?} ${arch})"
fi
log "OS: ubuntu 24.04 aarch64"

# >>> settings-file
SETTING_NAMES=" BOX_USER BOX_UID BOX_HOME HERDR_VERSION HERDR_SHA256 CAO_REF DATA_VOLUME_ID DATA_DEVICE \
DATA_MOUNT INSTALL_DOCKER BACKUP_S3_URI BACKUP_KMS_KEY_ARN CAO_REPO GH_VERSION AWS_CLI_VERSION \
AWS_CLI_SHA256 SAM_VERSION SAM_SHA256 YAZI_VERSION YAZI_SHA256 HELIX_VERSION HELIX_SHA256 UV_VERSION UV_SHA256 "
# read_settings FILE: set the known settings from FILE. FILE is parsed line by
# line, never sourced and never handed to env, so no line can run code as root.
read_settings() {
  local file="$1" dir line key value n=0 seen=" "
  [ -f "$file" ] || die "--settings ${file}: no such file"
  cb_safe_path "$file" "$EUID" || die "refusing --settings ${file}: it must be yours (uid ${EUID}), not a symlink, with no group or other write"
  dir="$(cd "$(dirname "$file")" && pwd -P)"
  if [ "$EUID" -eq 0 ]; then
    cb_safe_dir "$dir" / 0 || die "refusing --settings ${file}: as root, its folders must be root's and nobody else's to write"
  fi
  SETTINGS_REAL="${dir}/$(basename "$file")"
  while IFS= read -r line || [ -n "$line" ]; do
    n=$((n + 1))
    line="${line%%#*}"
    line="${line%"${line##*[![:space:]]}"}"
    [ -n "$line" ] || continue
    [[ "$line" =~ ^([A-Z][A-Z0-9_]*)=([A-Za-z0-9._:/@+,=-]*)$ ]] \
      || die "${file} line ${n}: expected KEY=VALUE, with only letters, digits and . _ : / @ + , = - in the value"
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    case "$SETTING_NAMES" in
      *" ${key} "*) ;;
      *) die "${file} line ${n}: ${key} is not a bootstrap setting; refusing" ;;
    esac
    case "$seen" in *" ${key} "*) die "${file}: ${key} is set twice" ;; esac
    seen="${seen}${key} "
    printf -v "$key" '%s' "$value"
  done < "$file"
  log "settings read from ${SETTINGS_REAL}"
}
# <<< settings-file
SETTINGS_REAL=""
if [ -n "$SETTINGS_FILE" ]; then read_settings "$SETTINGS_FILE"; fi

PATH_RE='^/[A-Za-z0-9._/-]+$'
BOX_USER="${BOX_USER:-}"
BOX_UID="${BOX_UID:-}"
BOX_HOME="${BOX_HOME:-}"
HERDR_VERSION="${HERDR_VERSION:-}"
HERDR_SHA256="${HERDR_SHA256:-}"
CAO_REF="${CAO_REF:-}"
for var in BOX_USER BOX_UID BOX_HOME HERDR_VERSION HERDR_SHA256 CAO_REF; do
  [ -n "${!var}" ] || die "set $var (see the header of this script)"
done
[[ "$BOX_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "BOX_USER must be a plain Linux user name"
if ! [[ "$BOX_UID" =~ ^[0-9]+$ ]] || [ "$BOX_UID" -lt 1000 ] || [ "$BOX_UID" -ge 60000 ]; then
  die "BOX_UID must be a number from 1000 to 59999"
fi
[[ "$BOX_HOME" =~ $PATH_RE ]] || die "BOX_HOME must be an absolute path of letters, digits, . _ - and /"
case "$BOX_HOME" in */|*/../*|*/..) die "BOX_HOME must not be /, end in /, or contain .." ;; esac
[[ "$HERDR_VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "HERDR_VERSION looks like v0.9.3"
[[ "$HERDR_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "HERDR_SHA256 must be 64 lowercase hex characters"
[[ "$CAO_REF" =~ ^[0-9A-Za-z._/-]+$ ]] || die "CAO_REF has unexpected characters"

DATA_VOLUME_ID="${DATA_VOLUME_ID:-}"
DATA_DEVICE="${DATA_DEVICE:-}"
DATA_MOUNT="${DATA_MOUNT:-$(dirname "$BOX_HOME")}"
if [ -n "$DATA_VOLUME_ID" ]; then
  [[ "$DATA_VOLUME_ID" =~ ^vol-[0-9a-f]{17}$ ]] || die "DATA_VOLUME_ID looks like vol- and 17 hex characters"
elif [ -n "$DATA_DEVICE" ]; then
  die "DATA_DEVICE alone is not accepted: set DATA_VOLUME_ID (device names are not stable)"
fi
if ! [[ "$DATA_MOUNT" =~ $PATH_RE ]] || [ "$DATA_MOUNT" = "/" ]; then
  die "DATA_MOUNT must be an absolute path other than /"
fi
INSTALL_DOCKER="${INSTALL_DOCKER:-no}"
case "$INSTALL_DOCKER" in yes|no) ;; *) die "INSTALL_DOCKER is yes or no" ;; esac
CAO_REPO="${CAO_REPO:-https://github.com/awslabs/cli-agent-orchestrator.git}"
BACKUP_S3_URI="${BACKUP_S3_URI:-}"
BACKUP_KMS_KEY_ARN="${BACKUP_KMS_KEY_ARN:-}"
if [ -n "$BACKUP_S3_URI" ]; then
  [[ "$BACKUP_S3_URI" =~ ^s3://[a-z0-9.-]+/[A-Za-z0-9._/-]*/$ ]] || die "BACKUP_S3_URI looks like s3://<bucket>/<prefix>/"
  [[ "$BACKUP_KMS_KEY_ARN" =~ ^arn:aws:kms:[a-z0-9-]+:[0-9]+:key/[A-Za-z0-9-]+$ ]] \
    || die "set BACKUP_KMS_KEY_ARN (arn:aws:kms:...:key/...) together with BACKUP_S3_URI"
fi
GH_VERSION="${GH_VERSION:-2.103.3}"
YAZI_VERSION="${YAZI_VERSION:-v26.9.1}"
HELIX_VERSION="${HELIX_VERSION:-25.07.1}"
AWS_CLI_VERSION="${AWS_CLI_VERSION:-}"
SAM_VERSION="${SAM_VERSION:-}"
UV_VERSION="${UV_VERSION:-}"

if [ "$FORMAT_FROM_ENV" -eq 1 ]; then
  warn "FORMAT_DATA_DEVICE=yes is set: remove it after the first run (prefer the --format-new-volume flag)"
fi
RUN_UID="$(id -u)"

if [ -n "$ROOT" ]; then
  log "--root $ROOT: only file-writing steps run; system steps are listed and skipped"
elif [ "$DRY_RUN" -eq 0 ] && [ "$RUN_UID" -ne 0 ]; then
  die "run as root (sudo), or use --dry-run"
fi

ETC_DIR="${ROOT}/etc/cao-box"
STATE_DIR="${ROOT}${DATA_MOUNT}/.cao-box"   # on the data volume: survives a rebuild
LIB_DIR="${ROOT}/usr/local/lib/cao-box"
UNIT_DIR="${ROOT}/etc/systemd/system"
HOME_DIR="${ROOT}${BOX_HOME}"
# The checkout and the settings file must be outside the box user's home:
# every agent can write there.
home_real="$HOME_DIR"
if [ -d "$HOME_DIR" ]; then home_real="$(cd "$HOME_DIR" && pwd -P)"; fi
for p in "$HERE" ${SETTINGS_REAL:+"$SETTINGS_REAL"}; do
  case "$p/" in
    "$home_real"/*|"$HOME_DIR"/*)
      die "refusing ${p}: it is inside the box user's home (${HOME_DIR}), where any agent can change it; keep the sample and its settings in a root-owned folder outside BOX_HOME, for example /opt/cao-box-src" ;;
  esac
done
DEVFS="${BOX_TEST_DEVFS:-/dev}"
SYSFS="${BOX_TEST_SYSFS:-/sys}"
FSTAB="${BOX_TEST_FSTAB:-/etc/fstab}"
system_step() { [ -z "$ROOT" ]; }

# The box user's home belongs to the agents: any of them can plant a file or a
# symlink there. So root never runs a file from it, never writes in it and
# never changes an owner through it. Every file operation in the home runs as
# the box user (as_user, put_home_file), after no_links.
# >>> home-helpers
USER_PATH="$BOX_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
as_user() { run runuser -u "$BOX_USER" -- env HOME="$BOX_HOME" PATH="$USER_PATH" "$@"; }

# no_links PATH...: refuse a symlink at PATH or at any folder between PATH and
# the home. It only looks (lstat); it never follows a link.
no_links() {
  local p
  for p in "$@"; do
    while [ "$p" != "$HOME_DIR" ] && [ "$p" != "/" ] && [ "$p" != "." ]; do
      if [ -L "$p" ]; then
        die "${p} is a symlink; refusing (root never follows a link in ${BOX_HOME})"
      fi
      p="$(dirname "$p")"
    done
  done
  return 0
}

# home_put ACTION DEST MODE WANT < CONTENT: the home side of put_home_file. It
# runs in its own bash with the box user's rights, so it uses nothing else
# from this script. If DEST exists it is kept (and checked, for ACTION herdr
# or cao); if not, CONTENT is written there with MODE.
home_put() {
  set -euo pipefail
  local action="$1" dest="$2" mode="$3" want="$4" have tmp
  if [ -e "$dest" ] || [ -L "$dest" ]; then
    case "$action" in
      herdr)
        if grep -Eq '^[[:space:]]*resume_agents_on_restore[[:space:]]*=[[:space:]]*false' "$dest"; then
          printf '[bootstrap] herdr config already sets resume_agents_on_restore = false\n'
        else
          printf '[bootstrap] WARN: %s exists: add the lines from config/herdr-config.toml.snippet by hand\n' "$dest" >&2
        fi
        ;;
      cao)
        have="$(sed -n 's/.*"herdr_session"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$dest")"
        have="${have%%$'\n'*}"
        if [ -n "$want" ] && [ -n "$have" ] && [ "$have" != "$want" ]; then
          printf '[bootstrap] ERROR: %s has herdr_session %s, but box.env says %s; make them match\n' \
            "$dest" "$have" "$want" >&2
          return 1
        fi
        printf '[bootstrap] kept existing: %s\n' "$dest"
        ;;
      *) printf '[bootstrap] kept existing: %s (compare with config/claude-settings.box.json.example)\n' "$dest" ;;
    esac
    return 0
  fi
  if [ "$action" = cao ] && [ -z "$want" ]; then
    printf '[bootstrap] no box.env yet: %s is written on the first real run\n' "$dest"
    return 0
  fi
  umask 077
  mkdir -p -- "${dest%/*}"
  tmp="$(mktemp "${dest}.XXXXXX")"
  cat > "$tmp"
  chmod "$mode" "$tmp"
  mv -f -- "$tmp" "$dest"
  printf '[bootstrap] wrote: %s\n' "$dest"
}

# put_home_file ACTION DEST MODE WANT LABEL < CONTENT: keep or write DEST in the
# home, as the box user (as the user running this, under --root). Root only
# streams CONTENT, which comes from this sample's own files.
put_home_file() {
  local action="$1" dest="$2" mode="$3" want="$4" label="$5" who prog
  no_links "$dest"
  if system_step; then who="$BOX_USER"; else who="the user running this"; fi
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] would, as %s: keep %s if it exists, else write it from %s (mode %s)\n' \
      "$who" "$dest" "$label" "$mode"
    return 0
  fi
  prog="$(declare -f home_put)"$'\n''home_put "$@"'
  if system_step; then
    runuser -u "$BOX_USER" -- env HOME="$BOX_HOME" PATH="$USER_PATH" \
      bash -c "$prog" home_put "$action" "$dest" "$mode" "$want"
  else
    bash -c "$prog" home_put "$action" "$dest" "$mode" "$want"
  fi
}
# <<< home-helpers

# Every path in the home that steps 2 and 7 work on is checked now, before any
# step changes the box. Steps 2 and 7 check again just before they act.
home_paths=("$HOME_DIR/.config/herdr/config.toml" "$HOME_DIR/.aws/cli-agent-orchestrator/settings.json" \
            "$HOME_DIR/.claude/settings.json")
if system_step; then home_paths+=("$BOX_HOME/.local/bin"); fi
no_links "${home_paths[@]}"

# ---------------------------------------------------------- 1. data volume
step "1. data volume ${DATA_VOLUME_ID:-<none>} -> ${DATA_MOUNT}"
if [ -z "$DATA_VOLUME_ID" ]; then
  log "DATA_VOLUME_ID not set: keeping everything on the root volume"
elif ! system_step; then
  log "skipped under --root: find ${DATA_VOLUME_ID} by serial, check it, mount at ${DATA_MOUNT}"
else
  serial="${DATA_VOLUME_ID/-/}"
  link="${DEVFS}/disk/by-id/nvme-Amazon_Elastic_Block_Store_${serial}"
  [ -e "$link" ] || die "no disk for ${DATA_VOLUME_ID} at ${link} (is the volume attached to this instance?)"
  dev="$(readlink -f "$link")"
  name="${dev##*/}"
  if [ -n "$DATA_DEVICE" ] && [ "$(readlink -f "$DATA_DEVICE")" != "$dev" ]; then
    die "DATA_DEVICE ${DATA_DEVICE} is not ${DATA_VOLUME_ID} (that is ${dev})"
  fi
  sys_serial="$(tr -d '[:space:]' < "${SYSFS}/block/${name}/device/serial" 2>/dev/null || true)"
  [ "$sys_serial" = "$serial" ] || die "${dev}'s serial is '${sys_serial:-?}', not ${serial}; refusing"
  disk_type="$(lsblk -dno TYPE "$dev")" || die "lsblk failed on ${dev}"
  [ "$disk_type" = "disk" ] || die "${dev} is a ${disk_type:-?}, not a whole disk"
  root_src="$(findmnt -no SOURCE /)" || die "cannot find the root filesystem"
  root_disk="$(lsblk -no PKNAME "$root_src" 2>/dev/null | head -n 1 || true)"
  if [ "$dev" = "$root_src" ] || [ "$name" = "$root_disk" ]; then
    die "${dev} holds the root filesystem; refusing"
  fi
  log "${DATA_VOLUME_ID} is ${dev} (serial checked, whole disk, not the root disk)"

  mounted_src="$(findmnt -rno SOURCE "$DATA_MOUNT" 2>/dev/null || true)"
  if [ -n "$mounted_src" ]; then
    [ "$(readlink -f "$mounted_src")" = "$dev" ] || die "${DATA_MOUNT} already has ${mounted_src} mounted"
    log "already mounted: ${dev} at ${DATA_MOUNT}"
  else
    [ "$(lsblk -nro NAME "$dev" | wc -l | tr -d ' ')" = "1" ] || die "${dev} has partitions; refusing"
    [ -z "$(ls -A "${SYSFS}/block/${name}/holders" 2>/dev/null)" ] || die "${dev} has holders (LVM, RAID or crypt); refusing"
    [ -z "$(lsblk -nro MOUNTPOINT "$dev" | tr -d '[:space:]')" ] || die "${dev} is mounted somewhere else; refusing"

    set +e
    probe="$(blkid -p -o export "$dev" 2>/dev/null)"
    blkid_rc=$?
    set -e
    case "$blkid_rc" in
      0)
        fs_type="$(printf '%s\n' "$probe" | sed -n 's/^TYPE=//p')"
        case "$fs_type" in
          ext4|xfs) log "${dev} already has an ${fs_type} filesystem: not formatting" ;;
          "") die "${dev} has signatures but no filesystem (a partition table?); refusing" ;;
          *) die "${dev} has a ${fs_type} signature; this sample mounts ext4 or xfs only" ;;
        esac
        ;;
      2)
        sigs="$(wipefs -n "$dev")" || die "wipefs failed on ${dev}"
        [ -z "$sigs" ] || die "wipefs found signatures on ${dev}; refusing"
        if [ "$FORMAT_NEW" -ne 1 ]; then
          die "${dev} (${DATA_VOLUME_ID}) is blank. To format it as ext4, re-run with --format-new-volume"
        fi
        log "${dev} is blank and --format-new-volume was given: formatting as ext4"
        run mkfs.ext4 -L cao-data "$dev"
        fs_type="ext4"
        ;;
      *) die "blkid -p exited ${blkid_rc} on ${dev}; not touching it" ;;
    esac

    if [ "$DRY_RUN" -eq 1 ]; then
      printf '[dry-run] would add to %s: UUID=<uuid> %s %s defaults,nofail 0 2\n' "$FSTAB" "$DATA_MOUNT" "$fs_type"
    else
      uuid="$(blkid -s UUID -o value "$dev")"
      [ -n "$uuid" ] || die "no UUID for ${dev}"
      grep -q "^UUID=${uuid} " "$FSTAB" || printf 'UUID=%s %s %s defaults,nofail 0 2\n' "$uuid" "$DATA_MOUNT" "$fs_type" >> "$FSTAB"
    fi
    run mkdir -p "$DATA_MOUNT"
    run mount "$DATA_MOUNT"
  fi
fi

# -------------------------------------------------------------- 2. the user
step "2. user ${BOX_USER} (uid ${BOX_UID}) with home ${BOX_HOME}"
# fix_home: the home must belong to BOX_USER (a fresh mount is root's).
fix_home() {
  if [ ! -d "$BOX_HOME" ]; then
    return 0
  fi
  local owner
  owner="$(stat -c %u "$BOX_HOME")"
  if [ "$owner" = "$BOX_UID" ]; then
    log "home is owned by uid ${BOX_UID}"
  elif [ "$owner" = "0" ]; then
    log "home is root's (a fresh mount): giving it to ${BOX_USER}"
    run chown "$BOX_USER:$BOX_USER" "$BOX_HOME"
    run chmod 0750 "$BOX_HOME"
    run runuser -u "$BOX_USER" -- cp -rn /etc/skel/. "$BOX_HOME/"
  else
    die "${BOX_HOME} belongs to uid ${owner}, not BOX_UID ${BOX_UID}; set BOX_UID to the volume's owner"
  fi
}
if ! system_step; then
  log "skipped under --root: useradd --uid ${BOX_UID} --home-dir ${BOX_HOME}"
elif id -u "$BOX_USER" >/dev/null 2>&1; then
  have_uid="$(id -u "$BOX_USER")"
  have_home="$(getent passwd "$BOX_USER" | cut -d: -f6)"
  [ "$have_uid" = "$BOX_UID" ] || die "${BOX_USER} exists with uid ${have_uid}, not BOX_UID ${BOX_UID}"
  [ "$have_home" = "$BOX_HOME" ] || die "${BOX_USER}'s home is ${have_home}, not BOX_HOME ${BOX_HOME}"
  log "user exists: ${BOX_USER} (uid ${BOX_UID}, home ${BOX_HOME})"
  fix_home
else
  if getent passwd "$BOX_UID" >/dev/null 2>&1; then
    die "uid ${BOX_UID} already belongs to another user"
  fi
  getent group "$BOX_USER" >/dev/null 2>&1 || run groupadd --gid "$BOX_UID" "$BOX_USER"
  if [ -d "$BOX_HOME" ]; then
    run useradd --uid "$BOX_UID" --gid "$BOX_UID" --home-dir "$BOX_HOME" --no-create-home --shell /bin/bash "$BOX_USER"
    fix_home
  else
    run useradd --uid "$BOX_UID" --gid "$BOX_UID" --home-dir "$BOX_HOME" --create-home --shell /bin/bash "$BOX_USER"
  fi
fi
if system_step; then
  # As the user: root's `install -d -o` would follow a planted link (say
  # ~/.config -> /etc) and give its target to the user.
  no_links "$BOX_HOME/.local/bin" "$BOX_HOME/.config"
  as_user install -d -m 0755 "$BOX_HOME/.local" "$BOX_HOME/.local/bin" "$BOX_HOME/.config"
fi

# ---------------------------------------------------------- 3. apt packages
PKGS=(git sqlite3 curl unzip jq xz-utils ca-certificates)
[ "$INSTALL_DOCKER" = "yes" ] && PKGS+=(docker.io)
step "3. packages: ${PKGS[*]}"
if [ "$INSTALL_DOCKER" = "yes" ]; then
  warn "INSTALL_DOCKER=yes: ${BOX_USER} joins the docker group. That is root on this box for every agent (docker run -v /:/host)."
else
  log "Docker not installed (INSTALL_DOCKER=no)"
fi
if ! system_step; then
  log "skipped under --root: apt-get install ${PKGS[*]}"
else
  missing=()
  for p in "${PKGS[@]}"; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || missing+=("$p")
  done
  if [ "${#missing[@]}" -eq 0 ]; then
    log "all packages present"
  else
    run apt-get update
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
  fi
  if [ "$INSTALL_DOCKER" = "yes" ]; then
    if id -nG "$BOX_USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
      log "${BOX_USER} is in the docker group"
    else
      run usermod -aG docker "$BOX_USER"
    fi
  fi
fi

# ------------------------------------------------------- 4. pinned tools
step "4. pinned, checksum-verified tools: herdr, gh, AWS CLI v2, SAM CLI, yazi, hx, uv"
fetch() { run curl -fsSL --proto '=https' --tlsv1.2 -o "$2" "$1"; }

# verify FILE SHA256 LABEL: stop the run on a missing or wrong checksum.
# >>> verify
verify() {
  local file="$1" want="$2" label="$3"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] would verify sha256 of %s = %s\n' "$label" "$want"
    return 0
  fi
  cb_require_sha "$file" "$want" "$label"
  log "sha256 ok: ${label}"
}
# <<< verify

have() { command -v "$1" >/dev/null 2>&1; }

# >>> herdr-check
# herdr_is_pinned FILE: true if FILE is a regular, executable file (not a
# symlink) whose sha256 is HERDR_SHA256. The box user looks and hashes, under a deadline, so
# root never opens a path in the home: a FIFO, a device or a slow file there
# can't hang root or make it read anything.
SYS_PATH="/usr/bin:/bin"
HASH_SECONDS=30
# The program is expanded by the box user's bash, not here.
# shellcheck disable=SC2016
HERDR_HASH='[ -f "$1" ] && [ -x "$1" ] && [ ! -L "$1" ] || exit 3
exec timeout "$2" sha256sum -- "$1"'
herdr_is_pinned() {
  local got
  got="$(runuser -u "$BOX_USER" -- env -i PATH="$SYS_PATH" LANG=C \
    bash -c "$HERDR_HASH" herdr-hash "$1" "$HASH_SECONDS" 2>/dev/null)" || return 1
  [ "${got%% *}" = "$HERDR_SHA256" ]
}
# herdr_keep FILE: keep the installed herdr? Never run it to ask its version:
# any agent can plant a herdr there, and root would run it. A dry run doesn't
# look in the home at all, so it plans the download.
herdr_keep() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] would check, as %s, that %s is a regular file with sha256 HERDR_SHA256; the plan below assumes it is not\n' \
      "$BOX_USER" "$1"
    return 1
  fi
  herdr_is_pinned "$1"
}
# <<< herdr-check

if ! system_step; then
  log "skipped under --root: downloads into ${BOX_HOME}/.local/bin and /usr/local"
else
  herdr_bin="$BOX_HOME/.local/bin/herdr"
  need=()
  if herdr_keep "$herdr_bin"; then
    log "herdr ${HERDR_VERSION} present (sha256 matches HERDR_SHA256)"
  else
    need+=(herdr)
  fi
  for pair in gh:gh aws:aws sam:sam yazi:yazi hx:hx; do
    if have "${pair%%:*}"; then log "${pair#*:} present"; else need+=("${pair#*:}"); fi
  done
  if [ -x "$BOX_HOME/.local/bin/uv" ]; then log "uv present"; else need+=(uv); fi

  unset_pins=()
  for tool in ${need[@]+"${need[@]}"}; do
    case "$tool" in
      aws)  { [ -n "$AWS_CLI_VERSION" ] && [ -n "${AWS_CLI_SHA256:-}" ]; } || unset_pins+=(AWS_CLI_VERSION+AWS_CLI_SHA256) ;;
      sam)  { [ -n "$SAM_VERSION" ] && [ -n "${SAM_SHA256:-}" ]; } || unset_pins+=(SAM_VERSION+SAM_SHA256) ;;
      yazi) [ -n "${YAZI_SHA256:-}" ] || unset_pins+=(YAZI_SHA256) ;;
      hx)   [ -n "${HELIX_SHA256:-}" ] || unset_pins+=(HELIX_SHA256) ;;
      uv)   { [ -n "$UV_VERSION" ] && [ -n "${UV_SHA256:-}" ]; } || unset_pins+=(UV_VERSION+UV_SHA256) ;;
    esac
  done
  if [ "${#unset_pins[@]}" -gt 0 ]; then
    die "set these pins first (copy each sha256 from the release's published checksums): ${unset_pins[*]}"
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    TMP_DL="<download-dir>"
  else
    TMP_DL="$(mktemp -d)"
    trap 'rm -rf -- "${TMP_DL:?}"' EXIT
    chmod 0755 "$TMP_DL"   # the user-level installs below read from here
  fi

  for tool in ${need[@]+"${need[@]}"}; do
    case "$tool" in
      herdr)
        fetch "https://github.com/herdrdev/herdr/releases/download/${HERDR_VERSION}/herdr-linux-aarch64" "$TMP_DL/herdr"
        verify "$TMP_DL/herdr" "$HERDR_SHA256" "herdr-linux-aarch64 ${HERDR_VERSION}"
        as_user install -m 0755 "$TMP_DL/herdr" "$herdr_bin"
        ;;
      gh)
        asset="gh_${GH_VERSION}_linux_arm64.deb"
        fetch "https://github.com/cli/cli/releases/download/v${GH_VERSION}/${asset}" "$TMP_DL/${asset}"
        fetch "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_checksums.txt" "$TMP_DL/gh-checksums.txt"
        if [ "$DRY_RUN" -eq 1 ]; then
          gh_sha="<from gh_${GH_VERSION}_checksums.txt>"
        else
          gh_sha="$(awk -v f="$asset" '$2 == f { print $1 }' "$TMP_DL/gh-checksums.txt")"
          [ -n "$gh_sha" ] || die "${asset} is not in gh's published checksums"
        fi
        verify "$TMP_DL/${asset}" "$gh_sha" "$asset"
        run dpkg -i "$TMP_DL/${asset}"
        ;;
      aws)
        fetch "https://awscli.amazonaws.com/awscli-exe-linux-aarch64-${AWS_CLI_VERSION}.zip" "$TMP_DL/awscli.zip"
        verify "$TMP_DL/awscli.zip" "$AWS_CLI_SHA256" "awscli-exe-linux-aarch64-${AWS_CLI_VERSION}.zip"
        run unzip -q "$TMP_DL/awscli.zip" -d "$TMP_DL/awscli"
        run "$TMP_DL/awscli/aws/install"
        ;;
      sam)
        fetch "https://github.com/aws/aws-sam-cli/releases/download/v${SAM_VERSION}/aws-sam-cli-linux-arm64.zip" "$TMP_DL/sam.zip"
        verify "$TMP_DL/sam.zip" "$SAM_SHA256" "aws-sam-cli-linux-arm64.zip v${SAM_VERSION}"
        run unzip -q "$TMP_DL/sam.zip" -d "$TMP_DL/sam"
        run "$TMP_DL/sam/install"
        ;;
      yazi)
        fetch "https://github.com/sxyazi/yazi/releases/download/${YAZI_VERSION}/yazi-aarch64-unknown-linux-gnu.zip" "$TMP_DL/yazi.zip"
        verify "$TMP_DL/yazi.zip" "$YAZI_SHA256" "yazi-aarch64-unknown-linux-gnu.zip ${YAZI_VERSION}"
        run unzip -q "$TMP_DL/yazi.zip" -d "$TMP_DL/yazi"
        run install -m 0755 "$TMP_DL/yazi/yazi-aarch64-unknown-linux-gnu/yazi" /usr/local/bin/yazi
        run install -m 0755 "$TMP_DL/yazi/yazi-aarch64-unknown-linux-gnu/ya" /usr/local/bin/ya
        ;;
      hx)
        fetch "https://github.com/helix-editor/helix/releases/download/${HELIX_VERSION}/helix-${HELIX_VERSION}-aarch64-linux.tar.xz" "$TMP_DL/helix.tar.xz"
        verify "$TMP_DL/helix.tar.xz" "$HELIX_SHA256" "helix-${HELIX_VERSION}-aarch64-linux.tar.xz"
        run mkdir -p /opt/helix
        run tar -xJf "$TMP_DL/helix.tar.xz" -C /opt/helix --strip-components=1
        run ln -sf /opt/helix/hx /usr/local/bin/hx
        ;;
      uv)
        fetch "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-aarch64-unknown-linux-gnu.tar.gz" "$TMP_DL/uv.tar.gz"
        verify "$TMP_DL/uv.tar.gz" "$UV_SHA256" "uv-aarch64-unknown-linux-gnu.tar.gz ${UV_VERSION}"
        run tar -xzf "$TMP_DL/uv.tar.gz" -C "$TMP_DL"
        as_user install -m 0755 "$TMP_DL/uv-aarch64-unknown-linux-gnu/uv" "$TMP_DL/uv-aarch64-unknown-linux-gnu/uvx" "$BOX_HOME/.local/bin/"
        ;;
    esac
  done
fi

# ------------------------------------------------------------------ 5. CAO
step "5. CAO at ${CAO_REF}"
cao_marker="${ETC_DIR}/cao-ref"
if [ -f "$cao_marker" ] && [ "$(cat "$cao_marker")" = "$CAO_REF" ] && [ -x "$HOME_DIR/.local/bin/cao-server" ]; then
  log "CAO ${CAO_REF} already installed"
elif ! system_step; then
  log "skipped under --root: uv tool install git+${CAO_REPO}@${CAO_REF}"
else
  as_user uv tool install --force "git+${CAO_REPO}@${CAO_REF}"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] would record %s in %s\n' "$CAO_REF" "$cao_marker"
  else
    mkdir -p "$ETC_DIR"; printf '%s\n' "$CAO_REF" > "$cao_marker"
  fi
fi

# ------------------------------------------------------- 6. session name
step "6. herdr session name (cao-<instance-id>), kept on the data volume"
etc_env="${ETC_DIR}/box.env"
vol_env="${STATE_DIR}/box.env"
STOP_DIR="${ROOT:-/}"

# read_box_env FILE: the session, if FILE and its folders are safe and FILE is
# exactly the two session lines. A file that exists but fails stops the run.
read_box_env() {
  local file="$1" dir
  dir="$(dirname "$file")"
  cb_safe_dir "$dir" "$STOP_DIR" "$RUN_UID" || die "refusing ${file}: its folder must be owned by uid ${RUN_UID} and not writable by others"
  cb_safe_path "$file" "$RUN_UID" || die "refusing ${file}: it must be owned by uid ${RUN_UID}, mode 0644 or tighter"
  cb_read_session "$file" || die "refusing ${file}: it must hold exactly the two session lines"
}

# write_box_env DEST SESSION: render (never copy) the two lines into DEST.
write_box_env() {
  local dest="$1" session="$2"
  if [ -f "$dest" ] && [ "$(cat "$dest")" = "$(cb_render_session "$session")" ]; then
    log "unchanged: $dest"
  elif [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] would write %s (CAO_BOX_SESSION=CAO_HERDR_SESSION=%s)\n' "$dest" "$session"
  else
    mkdir -p "$(dirname "$dest")"
    chmod 0755 "$(dirname "$dest")"
    cb_render_session "$session" > "$dest.tmp"
    chmod 0644 "$dest.tmp"
    mv -- "$dest.tmp" "$dest"
    log "wrote: $dest"
  fi
}

# The volume copy is used only if its folder is safe: owned by root (the user
# running this) all the way up, so no agent can swap it. In the
# BOX_HOME=DATA_MOUNT layout the folder sits in the user's home, so it's skipped.
use_vol=1
if [ "$DATA_MOUNT" = "$BOX_HOME" ]; then
  # The volume IS the user's home: any folder on it can be the user's (and
  # every agent's), so never read or write a copy there, whatever exists.
  use_vol=0
  warn "DATA_MOUNT is BOX_HOME: box.env is kept in /etc/cao-box only (nothing on the volume is read); copy it by hand before a whole-box restore"
elif [ -e "$STATE_DIR" ]; then
  cb_safe_dir "$STATE_DIR" "$STOP_DIR" "$RUN_UID" \
    || die "refusing ${STATE_DIR}: it must be owned by uid ${RUN_UID}, 0755 or tighter, in folders nobody else can write"
elif [ -d "$(dirname "$STATE_DIR")" ] && ! cb_safe_dir "$(dirname "$STATE_DIR")" "$STOP_DIR" "$RUN_UID" 2>/dev/null; then
  use_vol=0
  warn "$(dirname "$STATE_DIR") is not root-only: box.env is kept in /etc/cao-box only; copy it by hand before a whole-box restore"
fi

session_now=""
if [ "$use_vol" -eq 1 ] && [ -e "$vol_env" ]; then
  session_now="$(read_box_env "$vol_env")"
  log "found on the data volume: $vol_env (${session_now})"
elif [ -e "$etc_env" ]; then
  session_now="$(read_box_env "$etc_env")"
  log "found: $etc_env (${session_now})"
elif ! system_step; then
  log "--root: no box.env to copy; the first real run writes it (IMDS is never called under --root)"
elif [ "$DRY_RUN" -eq 1 ]; then
  printf '[dry-run] would read the instance id from IMDSv2 and write %s and %s:\n' "$vol_env" "$etc_env"
  printf '[dry-run]   CAO_BOX_SESSION=cao-<instance-id>\n[dry-run]   CAO_HERDR_SESSION=cao-<instance-id>\n'
else
  session_now="$("$HERE/bin/box-session-name.sh")"
  log "session from IMDSv2: ${session_now}"
fi
if [ -n "$session_now" ]; then
  [[ "$session_now" =~ ^cao-i-[0-9a-f]{8,17}$ ]] || die "unexpected session name ${session_now}"
  [ "$use_vol" -eq 1 ] && write_box_env "$vol_env" "$session_now"
  write_box_env "$etc_env" "$session_now"
fi

# Who owns the agents, for restore.sh --live (root-only, non-secret).
user_env="${ETC_DIR}/box-user.env"
user_text="$(printf 'BOX_USER=%s\nBOX_UID=%s\nBOX_HOME=%s' "$BOX_USER" "$BOX_UID" "$BOX_HOME")"
if [ -f "$user_env" ] && [ "$(cat "$user_env")" = "$user_text" ]; then
  log "unchanged: $user_env"
elif [ "$DRY_RUN" -eq 1 ]; then
  printf '[dry-run] would write %s (BOX_USER=%s BOX_UID=%s)\n' "$user_env" "$BOX_USER" "$BOX_UID"
else
  mkdir -p "$ETC_DIR"
  printf '%s\n' "$user_text" > "$user_env.tmp"
  chmod 0644 "$user_env.tmp"
  mv -- "$user_env.tmp" "$user_env"
  log "wrote: $user_env"
fi

backup_env="${ETC_DIR}/backup.env"
backup_text="$(printf 'BACKUP_S3_URI=%s\nBACKUP_KMS_KEY_ARN=%s' "$BACKUP_S3_URI" "$BACKUP_KMS_KEY_ARN")"
if [ -f "$backup_env" ] && [ -z "$BACKUP_S3_URI" ]; then
  log "kept $backup_env ($(sed -n 's/^BACKUP_S3_URI=//p' "$backup_env" | head -n 1)); set BACKUP_S3_URI and BACKUP_KMS_KEY_ARN to change it"
elif [ -f "$backup_env" ] && [ "$(cat "$backup_env")" = "$backup_text" ]; then
  log "unchanged: $backup_env"
elif [ "$DRY_RUN" -eq 1 ]; then
  printf '[dry-run] would write %s (BACKUP_S3_URI=%s)\n' "$backup_env" "${BACKUP_S3_URI:-<empty>}"
else
  mkdir -p "$ETC_DIR"
  printf '%s\n' "$backup_text" > "$backup_env"
  chmod 0644 "$backup_env"
  log "wrote: $backup_env"
fi

# --------------------------------------------------------------- 7. config
step "7. box config: herdr [session], CAO settings, Claude Code example"
herdr_cfg="$HOME_DIR/.config/herdr/config.toml"
put_home_file herdr "$herdr_cfg" 0644 "" config/herdr-config.toml.snippet \
  < "$HERE/config/herdr-config.toml.snippet"

cao_settings="$HOME_DIR/.aws/cli-agent-orchestrator/settings.json"
put_home_file cao "$cao_settings" 0644 "$session_now" "config/cao-settings.json.snippet (herdr_session from box.env)" \
  < <(sed "s/cao-<instance-id>/${session_now:-cao-<instance-id>}/" "$HERE/config/cao-settings.json.snippet")

claude_settings="$HOME_DIR/.claude/settings.json"
put_home_file claude "$claude_settings" 0600 "" config/claude-settings.box.json.example \
  < "$HERE/config/claude-settings.box.json.example"

# ------------------------------------------------------- 8. scripts + units
step "8. helper scripts and systemd units (enabled, NOT started)"
for f in backup.sh restore.sh bin/lib-cao-box.sh bin/box-session-name.sh bin/herdr-start.sh bin/herdr-clean-layout.sh \
         bin/wait-herdr-socket.sh bin/resource-probe-linux.sh bin/box-resume.sh; do
  install_file "$HERE/$f" "$LIB_DIR/$(basename "$f")" 0755
done

render() { sed -e "s|@BOX_USER@|${BOX_USER}|g" -e "s|@BOX_HOME@|${BOX_HOME}|g" "$HERE/systemd/$1"; }
for unit in herdr-cao.service cao-server.service cao-backup.service cao-backup.timer; do
  dest="$UNIT_DIR/$unit"
  if [ -f "$dest" ] && render "$unit" | cmp -s - "$dest"; then
    log "unchanged: $dest"
  elif [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] would install systemd/%s (rendered) -> %s (mode 0644)\n' "$unit" "$dest"
  else
    mkdir -p "$UNIT_DIR"
    render "$unit" > "$dest.tmp"
    chmod 0644 "$dest.tmp"
    mv -- "$dest.tmp" "$dest"
    log "installed: $dest"
  fi
done

if [ -n "$ROOT" ] && [ "$DRY_RUN" -eq 0 ] && ! command -v systemctl >/dev/null 2>&1; then
  log "no systemctl here: enable the units when the image boots"
elif [ -n "$ROOT" ]; then
  run systemctl --root="$ROOT" enable herdr-cao.service cao-server.service cao-backup.timer
else
  run systemctl daemon-reload
  run systemctl enable herdr-cao.service cao-server.service cao-backup.timer
fi

# ------------------------------------------------------------------ done
step "done"
cat <<EOF
Next steps (you run these; see README.md):
  1. Log in to the agent CLIs as ${BOX_USER}: Claude Code (Bedrock via the instance
     role, see config/claude-settings.box.json.example), codex login --device-auth,
     gh auth login.
  2. Start herdr first, then CAO (cao-server waits for herdr's socket):
     sudo systemctl start herdr-cao.service && sudo systemctl start cao-server.service
  3. Check: curl -fsS http://127.0.0.1:9889/health
  4. On the laptop: herdr machine add <ssh-host> --remote-session <session> --label cao-box
     (answer No if it offers to stop the remote server)
EOF
