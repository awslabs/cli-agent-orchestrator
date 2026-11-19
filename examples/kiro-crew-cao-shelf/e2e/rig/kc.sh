#!/usr/bin/env bash
# Run `kirocrew` against the ISOLATED home only.
#
#   e2e/rig/kc.sh kirocrew app list
#   e2e/rig/kc.sh kirocrew config get agent.apps_allow_third_party
#   e2e/rig/kc.sh ./install.sh --trust-app        (any command, e.g. the install script)
#
# The command runs under `env -i` with HOME set to the scratch home, an explicit PATH and KIROCREW_PORT
# pinned to the scratch gateway port. `kirocrew stop|restart|service|doctor|gateway` are refused here:
# stop and restart act on the owner's real service whatever HOME says (an earlier run stopped a real gateway that way).
# Start and stop the scratch gateway with e2e/rig/kiro_crew.sh only.

set -u
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
rig_check_scratch
rig_check_ports

[[ $# -ge 1 ]] || die "usage: kc.sh <command> [args...]"

if [[ "$(basename "$1")" == "kirocrew" ]]; then
    sub="${2:-}"
    case "$sub" in
        --version | config | app | token | mcp) ;;
        stop | restart | service | doctor | gateway | update | uninstall)
            die "refused: 'kirocrew $sub' is not allowed through the rig (use e2e/rig/kiro_crew.sh start|stop for the scratch gateway)"
            ;;
        *) die "refused: 'kirocrew ${sub}' is not on the rig allowlist (--version, config, app, token)" ;;
    esac
fi

KC_PATH="$ISO_HOME/.local/bin"
if [[ -z "$NODE_BIN_DIR" ]] && command -v node >/dev/null 2>&1; then
    NODE_BIN_DIR="$(dirname "$(node -p 'process.execPath')")"
fi
[[ -n "$NODE_BIN_DIR" ]] && KC_PATH="$KC_PATH:$NODE_BIN_DIR"
KC_PATH="$KC_PATH:/usr/local/bin:/usr/bin:/bin"
# install.sh resolves cao-ops-mcp-server from PATH; the scratch CAO venv provides it (last, so python is not shadowed).
[[ -x "$CAO_VENV/bin/cao-ops-mcp-server" ]] && KC_PATH="$KC_PATH:$CAO_VENV/bin"

# Temporary files (install.sh stages a copy of the App with mktemp) stay in the scratch folder, never /tmp.
mkdir -p "$WORK_DIR/tmp"
exec env -i HOME="$ISO_HOME" KIROCREW_PORT="$KC_PORT" PATH="$KC_PATH" LANG=C.UTF-8 TMPDIR="$WORK_DIR/tmp" "$@"
