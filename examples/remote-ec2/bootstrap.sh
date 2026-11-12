#!/usr/bin/env bash
# bootstrap.sh: set up one always-on Ubuntu 24.04 arm64 (Graviton) box to run
# herdr + CAO under systemd. Run it ON THE BOX, as root, once; re-running it is
# safe (every step checks before it acts).
#
#   sudo BOX_USER=<you> BOX_HOME=/Users/<you> DATA_DEVICE=/dev/nvme1n1 \
#        HERDR_VERSION=v0.9.3 CAO_REF=<commit-sha> \
#        bash bootstrap.sh --dry-run      # print the plan, change nothing
#   sudo ... bash bootstrap.sh            # do it
#
# It never creates AWS resources, never starts a service (it only enables
# them), and never logs in to anything. See README.md for the full flow.
#
# Settings (environment variables):
#   BOX_USER        required. The Linux user that owns the agents.
#   BOX_HOME        required. That user's home. Default advice: your laptop's
#                   home path (for example /Users/<you>) so absolute paths in
#                   your kits keep working; /home/<you> works too.
#   DATA_DEVICE     optional. The EBS data volume (for example /dev/nvme1n1).
#                   Formatted ONLY if it has no filesystem; mounted at DATA_MOUNT.
#   DATA_MOUNT      optional. Default: the parent folder of BOX_HOME.
#   HERDR_VERSION   required. Pin the same herdr version as your laptop.
#   CAO_REF         required. The CAO commit to install (a full sha is best).
#   CAO_REPO        optional. Default: the upstream GitHub repository.
#   BACKUP_S3_URI   optional. s3://<bucket>/<prefix>/ for nightly backups.
#   HERDR_SHA256    optional. Checksum of the herdr download, verified if set.
#   GH_VERSION, YAZI_VERSION, HELIX_VERSION   optional tool pins.
#
# Flags:
#   --dry-run       print every planned step; write nothing, run nothing.
#   --root DIR      write files under DIR instead of / (tests, image prep).
#                   Package, user, volume and download steps are skipped.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=0
ROOT=""

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --root)
      [ $# -ge 2 ] || { echo "--root needs a folder" >&2; exit 2; }
      ROOT="${2%/}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

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

# write_text DEST MODE TEXT: write TEXT only if DEST is missing.
write_text_if_missing() {
  local dest="$1" mode="$2" text="$3"
  if [ -e "$dest" ]; then
    log "kept existing: $dest"
    return 0
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] would write %s (mode %s)\n' "$dest" "$mode"
    return 0
  fi
  mkdir -p "$(dirname "$dest")"
  ( umask 077; printf '%s\n' "$text" > "$dest" )
  chmod "$mode" "$dest"
  log "wrote: $dest"
}

# ---------------------------------------------------------------- 0. checks
step "0. check the OS, the settings and the user running this"
OS_RELEASE="${ROOT}/etc/os-release"
[ -r "$OS_RELEASE" ] || die "cannot read $OS_RELEASE; this sample supports Ubuntu 24.04 arm64 only"
os_id="$(sed -n 's/^ID=//p' "$OS_RELEASE" | tr -d '"')"
os_ver="$(sed -n 's/^VERSION_ID=//p' "$OS_RELEASE" | tr -d '"')"
arch="$(uname -m)"
if [ "$os_id" != "ubuntu" ] || [ "$os_ver" != "24.04" ] || [ "$arch" != "aarch64" ]; then
  die "this sample supports Ubuntu 24.04 on aarch64 only (found: ${os_id:-?} ${os_ver:-?} ${arch})"
fi
log "OS: ubuntu 24.04 aarch64"

BOX_USER="${BOX_USER:-}"
BOX_HOME="${BOX_HOME:-}"
HERDR_VERSION="${HERDR_VERSION:-}"
CAO_REF="${CAO_REF:-}"
[ -n "$BOX_USER" ] || die "set BOX_USER"
[ -n "$BOX_HOME" ] || die "set BOX_HOME (for example /Users/<you> or /home/<you>)"
[ -n "$HERDR_VERSION" ] || die "set HERDR_VERSION to the herdr version your laptop runs (for example v0.9.3)"
[ -n "$CAO_REF" ] || die "set CAO_REF to the CAO commit to install"
[[ "$BOX_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "BOX_USER must be a plain Linux user name"
case "$BOX_HOME" in /*) ;; *) die "BOX_HOME must be an absolute path" ;; esac
case "$BOX_HOME" in */) die "BOX_HOME must not end in /" ;; esac
[[ "$HERDR_VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "HERDR_VERSION looks like v0.9.3"
[[ "$CAO_REF" =~ ^[0-9A-Za-z._/-]+$ ]] || die "CAO_REF has unexpected characters"

DATA_DEVICE="${DATA_DEVICE:-}"
DATA_MOUNT="${DATA_MOUNT:-$(dirname "$BOX_HOME")}"
CAO_REPO="${CAO_REPO:-https://github.com/awslabs/cli-agent-orchestrator.git}"
BACKUP_S3_URI="${BACKUP_S3_URI:-}"
GH_VERSION="${GH_VERSION:-2.103.3}"
YAZI_VERSION="${YAZI_VERSION:-v26.9.1}"
HELIX_VERSION="${HELIX_VERSION:-25.07.1}"

if [ -n "$ROOT" ]; then
  log "--root $ROOT: only file-writing steps run; system steps are listed and skipped"
elif [ "$DRY_RUN" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
  die "run as root (sudo), or use --dry-run"
fi

ETC_DIR="${ROOT}/etc/cao-box"
LIB_DIR="${ROOT}/usr/local/lib/cao-box"
UNIT_DIR="${ROOT}/etc/systemd/system"
HOME_DIR="${ROOT}${BOX_HOME}"
system_step() { [ -z "$ROOT" ]; }

# ---------------------------------------------------------- 1. data volume
step "1. data volume -> ${DATA_MOUNT}"
if [ -z "$DATA_DEVICE" ]; then
  log "DATA_DEVICE not set: keeping everything on the root volume"
elif ! system_step; then
  log "skipped under --root: format-if-empty ${DATA_DEVICE}, fstab by UUID, mount at ${DATA_MOUNT}"
else
  [ "$DATA_MOUNT" != "/" ] || die "DATA_MOUNT must not be /"
  if findmnt -rn "$DATA_MOUNT" >/dev/null 2>&1; then
    log "already mounted: $DATA_MOUNT"
  else
    [ -b "$DATA_DEVICE" ] || [ "$DRY_RUN" -eq 1 ] || die "$DATA_DEVICE is not a block device"
    fs_type="$(blkid -s TYPE -o value "$DATA_DEVICE" 2>/dev/null || true)"
    if [ -z "$fs_type" ]; then
      log "$DATA_DEVICE has no filesystem: it will be formatted (ext4)"
      run mkfs.ext4 -L cao-data "$DATA_DEVICE"
    else
      log "$DATA_DEVICE already has a $fs_type filesystem: not formatting"
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
      printf '[dry-run] would add an fstab line: UUID=<uuid> %s ext4 defaults,nofail 0 2\n' "$DATA_MOUNT"
    else
      uuid="$(blkid -s UUID -o value "$DATA_DEVICE")"
      [ -n "$uuid" ] || die "no UUID for $DATA_DEVICE"
      grep -q "UUID=$uuid " /etc/fstab || printf 'UUID=%s %s ext4 defaults,nofail 0 2\n' "$uuid" "$DATA_MOUNT" >> /etc/fstab
    fi
    run mkdir -p "$DATA_MOUNT"
    run mount "$DATA_MOUNT"
  fi
fi

# -------------------------------------------------------------- 2. the user
step "2. user ${BOX_USER} with home ${BOX_HOME}"
if ! system_step; then
  log "skipped under --root: useradd --home-dir ${BOX_HOME}"
elif id "$BOX_USER" >/dev/null 2>&1; then
  log "user exists: $BOX_USER"
else
  run useradd --home-dir "$BOX_HOME" --create-home --shell /bin/bash "$BOX_USER"
fi

# ---------------------------------------------------------- 3. apt packages
step "3. packages: git sqlite3 curl unzip jq xz-utils ca-certificates docker.io"
PKGS=(git sqlite3 curl unzip jq xz-utils ca-certificates docker.io)
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
  if id -nG "$BOX_USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
    log "$BOX_USER is in the docker group"
  else
    run usermod -aG docker "$BOX_USER"
  fi
fi

# ------------------------------------------------------- 4. pinned tools
step "4. pinned tools: herdr ${HERDR_VERSION}, gh ${GH_VERSION}, AWS CLI v2, SAM CLI, yazi ${YAZI_VERSION}, hx ${HELIX_VERSION}, uv"
as_user() { run runuser -u "$BOX_USER" -- env HOME="$BOX_HOME" PATH="$BOX_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" "$@"; }

# fetch URL DEST: download over HTTPS into a temp file (dry run: print only).
fetch() { run curl -fsSL --proto '=https' --tlsv1.2 -o "$2" "$1"; }

if ! system_step; then
  log "skipped under --root: downloads into ${BOX_HOME}/.local/bin and /usr/local"
else
  TMP_DL="$(mktemp -d)"
  trap 'rm -rf -- "${TMP_DL:?}"' EXIT

  herdr_bin="$BOX_HOME/.local/bin/herdr"
  if [ -x "$herdr_bin" ] && "$herdr_bin" --version 2>/dev/null | grep -q "${HERDR_VERSION#v}"; then
    log "herdr ${HERDR_VERSION} present"
  else
    fetch "https://github.com/herdrdev/herdr/releases/download/${HERDR_VERSION}/herdr-linux-aarch64" "$TMP_DL/herdr"
    if [ -n "${HERDR_SHA256:-}" ]; then
      run sh -c "printf '%s  %s\\n' \"\$1\" \"\$2\" | sha256sum -c -" verify "$HERDR_SHA256" "$TMP_DL/herdr"
    fi
    run install -D -m 0755 -o "$BOX_USER" -g "$BOX_USER" "$TMP_DL/herdr" "$herdr_bin"
  fi

  if command -v gh >/dev/null 2>&1; then
    log "gh present"
  else
    fetch "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_arm64.deb" "$TMP_DL/gh.deb"
    run dpkg -i "$TMP_DL/gh.deb"
  fi

  if command -v aws >/dev/null 2>&1; then
    log "AWS CLI present"
  else
    fetch "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" "$TMP_DL/awscli.zip"
    run unzip -q "$TMP_DL/awscli.zip" -d "$TMP_DL/awscli"
    run "$TMP_DL/awscli/aws/install"
  fi

  if command -v sam >/dev/null 2>&1; then
    log "SAM CLI present"
  else
    fetch "https://github.com/aws/aws-sam-cli/releases/latest/download/aws-sam-cli-linux-arm64.zip" "$TMP_DL/sam.zip"
    run unzip -q "$TMP_DL/sam.zip" -d "$TMP_DL/sam"
    run "$TMP_DL/sam/install"
  fi

  if command -v yazi >/dev/null 2>&1; then
    log "yazi present"
  else
    fetch "https://github.com/sxyazi/yazi/releases/download/${YAZI_VERSION}/yazi-aarch64-unknown-linux-gnu.zip" "$TMP_DL/yazi.zip"
    run unzip -q "$TMP_DL/yazi.zip" -d "$TMP_DL/yazi"
    run install -m 0755 "$TMP_DL/yazi/yazi-aarch64-unknown-linux-gnu/yazi" /usr/local/bin/yazi
    run install -m 0755 "$TMP_DL/yazi/yazi-aarch64-unknown-linux-gnu/ya" /usr/local/bin/ya
  fi

  if command -v hx >/dev/null 2>&1; then
    log "hx present"
  else
    fetch "https://github.com/helix-editor/helix/releases/download/${HELIX_VERSION}/helix-${HELIX_VERSION}-aarch64-linux.tar.xz" "$TMP_DL/helix.tar.xz"
    run mkdir -p /opt/helix
    run tar -xJf "$TMP_DL/helix.tar.xz" -C /opt/helix --strip-components=1
    run ln -sf /opt/helix/hx /usr/local/bin/hx
  fi

  if [ -x "$BOX_HOME/.local/bin/uv" ]; then
    log "uv present"
  else
    fetch "https://astral.sh/uv/install.sh" "$TMP_DL/uv-install.sh"
    run chmod 0755 "$TMP_DL"
    run chmod 0644 "$TMP_DL/uv-install.sh"
    as_user env UV_NO_MODIFY_PATH=1 sh "$TMP_DL/uv-install.sh"
  fi
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
step "6. herdr session name (cao-<instance-id>, read once from IMDSv2)"
box_env="${ETC_DIR}/box.env"
if [ -f "$box_env" ] && grep -q '^CAO_BOX_SESSION=cao-' "$box_env"; then
  log "kept existing: $box_env ($(sed -n 's/^CAO_BOX_SESSION=//p' "$box_env"))"
elif [ "$DRY_RUN" -eq 1 ]; then
  printf '[dry-run] would read the instance id from IMDSv2 and write %s:\n' "$box_env"
  printf '[dry-run]   CAO_BOX_SESSION=cao-<instance-id>\n[dry-run]   CAO_HERDR_SESSION=cao-<instance-id>\n'
else
  session="$("$HERE/bin/box-session-name.sh")"
  mkdir -p "$ETC_DIR"
  printf 'CAO_BOX_SESSION=%s\nCAO_HERDR_SESSION=%s\n' "$session" "$session" > "$box_env"
  chmod 0644 "$box_env"
  log "wrote $box_env ($session)"
fi
write_text_if_missing "${ETC_DIR}/backup.env" 0644 "BACKUP_S3_URI=${BACKUP_S3_URI}"

# --------------------------------------------------------------- 7. config
step "7. box config: herdr [session], CAO settings, Claude Code example"
herdr_cfg="$HOME_DIR/.config/herdr/config.toml"
if [ -f "$herdr_cfg" ] && grep -Eq '^[[:space:]]*resume_agents_on_restore[[:space:]]*=[[:space:]]*false' "$herdr_cfg"; then
  log "herdr config already sets resume_agents_on_restore = false"
elif [ -f "$herdr_cfg" ]; then
  warn "$herdr_cfg exists: add the lines from config/herdr-config.toml.snippet by hand"
else
  install_file "$HERE/config/herdr-config.toml.snippet" "$herdr_cfg" 0644
fi

cao_settings="$HOME_DIR/.aws/cli-agent-orchestrator/settings.json"
if [ -f "$cao_settings" ]; then
  log "kept existing: $cao_settings (check terminal.herdr_session matches box.env)"
elif [ "$DRY_RUN" -eq 1 ]; then
  printf '[dry-run] would write %s with terminal.herdr_session from box.env\n' "$cao_settings"
else
  session_now="$(sed -n 's/^CAO_BOX_SESSION=//p' "$box_env")"
  mkdir -p "$(dirname "$cao_settings")"
  sed "s/cao-<instance-id>/${session_now}/" "$HERE/config/cao-settings.json.snippet" > "$cao_settings"
  log "wrote: $cao_settings"
fi

claude_settings="$HOME_DIR/.claude/settings.json"
if [ -e "$claude_settings" ]; then
  log "kept existing: $claude_settings (compare with config/claude-settings.box.json.example)"
else
  install_file "$HERE/config/claude-settings.box.json.example" "$claude_settings" 0600
fi

if system_step; then
  for d in "$BOX_HOME/.config" "$BOX_HOME/.aws" "$BOX_HOME/.claude"; do
    if [ -d "$d" ] || [ "$DRY_RUN" -eq 1 ]; then run chown -R "$BOX_USER:$BOX_USER" "$d"; fi
  done
fi

# ------------------------------------------------------- 8. scripts + units
step "8. helper scripts and systemd units (enabled, NOT started)"
for f in backup.sh restore.sh bin/box-session-name.sh bin/herdr-start.sh \
         bin/herdr-clean-layout.sh bin/resource-probe-linux.sh bin/box-resume.sh; do
  install_file "$HERE/$f" "$LIB_DIR/$(basename "$f")" 0755
done

render_dir="$(mktemp -d "${TMPDIR:-/tmp}/cao-box-render.XXXXXX")"
trap 'rm -rf -- "${render_dir:?}" "${TMP_DL:-/nonexistent-cao-box}"' EXIT
for unit in herdr-cao.service cao-server.service cao-backup.service cao-backup.timer; do
  sed -e "s|@BOX_USER@|${BOX_USER}|g" -e "s|@BOX_HOME@|${BOX_HOME}|g" \
    "$HERE/systemd/$unit" > "$render_dir/$unit"
  install_file "$render_dir/$unit" "$UNIT_DIR/$unit" 0644 "systemd/$unit (rendered)"
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
  2. Start the services: sudo systemctl start herdr-cao.service cao-server.service
  3. Check: curl -fsS http://127.0.0.1:9889/health
  4. On the laptop: herdr machine add <ssh-host> --remote-session <session> --label cao-box
     (answer No if it offers to stop the remote server)
EOF
