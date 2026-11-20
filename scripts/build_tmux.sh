#!/usr/bin/env bash
# Build a statically linked tmux from the upstream release tarball (#1370).
#
# Used by the AG-UI operator dashboard job, which runs in the Playwright image
# (no tmux, no sudo). The built binary is cached by CI, so apt runs here only on
# a cold cache; on a warm cache this script is skipped and no apt runs at all.
#
# The tmux project publishes a source tarball and no checksum, so TMUX_SHA256
# below is pinned in this repository (computed from the 3.8 release asset). To
# bump tmux: change both values, rebuild, and review the diff of the release.
#
# Usage: scripts/build_tmux.sh <output-dir>   # writes <output-dir>/tmux
set -euo pipefail

TMUX_VERSION="3.8"
TMUX_SHA256="e79c699c7e949dccd0a4a125e17b8d1261e311b16979dbc8eb34542f3966d82e"
TMUX_URL="https://github.com/tmux/tmux/releases/download/${TMUX_VERSION}/tmux-${TMUX_VERSION}.tar.gz"

out_dir="${1:?usage: build_tmux.sh <output-dir>}"
mkdir -p "$out_dir"
out_dir="$(cd "$out_dir" && pwd)"

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
APT=(-o Acquire::Retries=3 -o Acquire::http::Timeout=20)

# Build dependencies only. The result is static, so none of them is needed to run it.
$SUDO apt-get "${APT[@]}" update
$SUDO apt-get "${APT[@]}" install -y --no-install-recommends \
  build-essential bison pkg-config ca-certificates curl \
  libevent-dev libncurses-dev

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cd "$work"

curl --fail --silent --show-error --location --retry 3 --max-time 120 \
  -o tmux.tar.gz "$TMUX_URL"
echo "${TMUX_SHA256}  tmux.tar.gz" | sha256sum --check --strict -

tar -xzf tmux.tar.gz
cd "tmux-${TMUX_VERSION}"
# Output is kept (it is the only way to debug a cold-cache failure); on a configure
# failure the tail of config.log is printed as well.
if ! ./configure --enable-static; then
  tail -n 60 config.log >&2 || true
  exit 1
fi
make -j"$(nproc)"

install -m 0755 tmux "$out_dir/tmux"
"$out_dir/tmux" -V
