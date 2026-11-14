#!/usr/bin/env bash
# lib-cao-box.sh: small helpers shared by bootstrap.sh, restore.sh,
# herdr-start.sh and wait-herdr-socket.sh. Source it; don't run it.
# Every function prints its reason on stderr and returns non-zero on failure,
# so callers decide how to stop.

# cb_sha256 FILE: print FILE's sha256 (sha256sum on Linux, shasum elsewhere).
cb_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

# cb_verify FILE WANT LABEL: succeed only if FILE's sha256 is exactly WANT.
cb_verify() {
  local file="$1" want="$2" label="$3" got
  if ! [[ "$want" =~ ^[0-9a-f]{64}$ ]]; then
    echo "no valid sha256 for ${label}" >&2
    return 1
  fi
  got="$(cb_sha256 "$file")" || { echo "cannot hash ${file}" >&2; return 1; }
  if [ "$got" != "$want" ]; then
    echo "sha256 mismatch for ${label}: got ${got}, want ${want}" >&2
    return 1
  fi
}

# cb_require_sha FILE WANT LABEL: like cb_verify, but EXITS the calling script
# (status 1) on a missing or wrong checksum, so no caller can carry on.
cb_require_sha() {
  if ! cb_verify "$@"; then
    echo "stopping: the download for ${3} failed its checksum" >&2
    exit 1
  fi
}

# cb_owner_mode PATH: print "<uid> <octal mode>" (GNU stat, then BSD stat).
cb_owner_mode() {
  stat -c '%u %a' "$1" 2>/dev/null || stat -f '%u %Lp' "$1"
}

# cb_safe_path PATH UID: PATH is not a symlink, is owned by UID, and is not
# writable by group or other.
cb_safe_path() {
  local info owner mode
  if [ -L "$1" ]; then
    echo "$1 is a symlink; refusing" >&2
    return 1
  fi
  info="$(cb_owner_mode "$1")" || { echo "cannot stat $1" >&2; return 1; }
  owner="${info%% *}"
  mode="${info##* }"
  if [ "$owner" != "$2" ]; then
    echo "$1 is owned by uid ${owner}, not ${2}" >&2
    return 1
  fi
  if ! [[ "$mode" =~ ^[0-7]+$ ]] || [ $(( 8#$mode & 8#022 )) -ne 0 ]; then
    echo "$1 is writable by group or others (mode ${mode})" >&2
    return 1
  fi
}

# cb_safe_dir DIR STOP UID: DIR and every parent up to STOP (or /) pass
# cb_safe_path, so nobody else can replace what's inside.
cb_safe_dir() {
  local p="$1" stop="$2" uid="$3"
  while :; do
    cb_safe_path "$p" "$uid" || return 1
    if [ "$p" = "$stop" ] || [ "$p" = "/" ]; then
      return 0
    fi
    p="$(dirname "$p")"
  done
}

# cb_read_session FILE: print the session name if FILE is exactly
#   CAO_BOX_SESSION=cao-i-<hex>
#   CAO_HERDR_SESSION=<the same value>
# in either order, with nothing else. Anything else fails.
cb_read_session() {
  local file="$1" line box="" herdr="" count=0
  local re='^cao-i-[0-9a-f]{8,17}$'
  [ -r "$file" ] || { echo "cannot read ${file}" >&2; return 1; }
  while IFS= read -r line || [ -n "$line" ]; do
    count=$((count + 1))
    case "$line" in
      CAO_BOX_SESSION=*)
        [ -z "$box" ] || { echo "${file}: CAO_BOX_SESSION appears twice" >&2; return 1; }
        box="${line#CAO_BOX_SESSION=}" ;;
      CAO_HERDR_SESSION=*)
        [ -z "$herdr" ] || { echo "${file}: CAO_HERDR_SESSION appears twice" >&2; return 1; }
        herdr="${line#CAO_HERDR_SESSION=}" ;;
      *) echo "${file}: unexpected line ${count} (only the two session lines are allowed)" >&2; return 1 ;;
    esac
  done < "$file"
  if [ "$count" -ne 2 ] || ! [[ "$box" =~ $re ]] || ! [[ "$herdr" =~ $re ]]; then
    echo "${file}: needs exactly CAO_BOX_SESSION=cao-i-<hex> and CAO_HERDR_SESSION=cao-i-<hex>" >&2
    return 1
  fi
  if [ "$box" != "$herdr" ]; then
    echo "${file}: CAO_BOX_SESSION (${box}) and CAO_HERDR_SESSION (${herdr}) differ" >&2
    return 1
  fi
  printf '%s\n' "$box"
}

# cb_render_session SESSION: the only content box.env may have.
cb_render_session() {
  printf 'CAO_BOX_SESSION=%s\nCAO_HERDR_SESSION=%s\n' "$1" "$1"
}
