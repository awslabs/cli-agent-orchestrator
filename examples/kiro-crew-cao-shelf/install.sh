#!/usr/bin/env bash
# Install (or update, or remove) the CAO Shelf in the Kiro Crew found on PATH.
#
#   ./install.sh                 install or update, then enable
#   ./install.sh --uninstall     disable and uninstall (app data is kept; add --purge-data to delete it)
#   ./install.sh --dry-run       print every command, run none of the ones that change anything
#   ./install.sh --relay-model claude-haiku-4.5   run the remote-control relay agent on that model
#
# What it does, in order:
#   1. finds `kirocrew` and checks its version (this App needs 0.12.2 or later)
#   2. makes sure ui/bundle/index.mjs exists and was built from the current ui/src (the input hash
#      recorded in ui/bundle/.inputs-sha256 matches; file times are not used); rebuilds it when needed
#      (needs Node 22.12 or later and npm; skipped when the committed bundle is fresh)
#   3. installs the App from this folder (`kirocrew app install`), or updates it when already installed
#   4. reports the third-party trust setting and enables the App
#
# Remote control. The App's `cao-remote-control` agent runs CAO's `cao-ops-mcp-server`. The script
# resolves it to an absolute path on PATH and takes the CAO host and port from --cao-url, else from
# CAO_SHELF_CAO_URL, else from the Shelf's default `cao_url` (http://127.0.0.1:9889), then installs
# a staged copy of this folder whose app.json names both. This folder's own app.json is never
# changed. Without `cao-ops-mcp-server` on PATH the script installs nothing. The staged copy is made
# under $TMPDIR, or ~/.cache/cao-shelf-install when TMPDIR is unset, and removed at the end.
#
# Relay model. --relay-model <id> (else CAO_SHELF_RELAY_MODEL) sets the `model` of the staged copy's
# `cao-remote-control` agent, so the relay can run on a cheaper model, `claude-haiku-4.5` say.
# Without it the agent keeps "auto", the backend's own default. The id must have the shape Kiro Crew
# accepts for a model name (letters, digits, '.', '_' and '-' starting with a letter or digit, with
# at most one trailing [qualifier], or a provider/model id); anything else exits 2. The shape check
# does not establish entitlement: on KAS a model your account is not served is not used (the chat
# stays on its current model), and on another backend it can fail at the relay's first message. Kiro Crew keeps the model already on an installed agent when it updates the App, so the
# id takes effect on a first install or a reinstall (--uninstall, then install again).
#
# Consent. The App asks Kiro Crew for session approval: Kiro Crew refuses an App's
# POST /api/chat/mode without it, and the App sets each relay chat's trust mode to normal. The App
# approves no tool request. Kiro Crew gives that consent only
# in the dashboard, never from a script, so on a first install or an upgrade from 0.1.0 the enable
# step stops with exit 7 and the dashboard link: enable CAO Shelf there.
#
# Trust. Kiro Crew refuses to run code from a third party until you allow it, so `kirocrew app enable`
# fails with `app_execution_denied` on a fresh install. There are two ways to allow it:
#   --trust-app   adds only "cao-shelf" to agent.apps_trusted (the narrow grant; recommended)
#   --trust-all   sets agent.apps_allow_third_party=true, which trusts EVERY third-party App (a shortcut)
# Without either option the script changes no trust setting, tells you what is set, and tells you what
# to run. You can also do it in the dashboard: Settings, Security, Third-party apps.
#
# Installing into your real Kiro Crew (documented here, not run by anything in this repository):
#   examples/kiro-crew-cao-shelf/install.sh --trust-app
# The line "Target:" printed at the start names the HOME and the gateway port the commands act on.
# To rehearse safely first, use the isolated rig: e2e/rig/harness.sh up (see e2e/rig/README.md).
#
# Exit codes: 0 done, 2 usage or kirocrew missing, 3 bundle missing or stale and cannot be rebuilt,
#             4 enable refused for trust, 5 kirocrew command failed, 6 cao-ops-mcp-server not found,
#             7 enable waits for your session-approval consent in the dashboard.

set -euo pipefail

APP_NAME="cao-shelf"
MIN_KIROCREW="0.12.2"
MIN_NODE_MAJOR=22
MIN_NODE_MINOR=12

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=0
NO_BUILD=0
UNINSTALL=0
PURGE_DATA=0
TRUST=""
CAO_URL="${CAO_SHELF_CAO_URL:-}"
CAO_URL_FROM="CAO_SHELF_CAO_URL"
RELAY_MODEL="${CAO_SHELF_RELAY_MODEL:-}"
RELAY_MODEL_FROM="CAO_SHELF_RELAY_MODEL"

usage() {
    sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}

fail() {
    local code="$1"
    shift
    echo "install.sh: $*" >&2
    exit "$code"
}

info() {
    echo "install.sh: $*"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --no-build) NO_BUILD=1 ;;
        --uninstall) UNINSTALL=1 ;;
        --purge-data) PURGE_DATA=1 ;;
        --trust-all) TRUST="all" ;;
        --trust-app) TRUST="app" ;;
        --cao-url)
            [[ $# -ge 2 ]] || { echo "install.sh: --cao-url needs a URL" >&2; exit 2; }
            CAO_URL="$2"
            CAO_URL_FROM="--cao-url"
            shift
            ;;
        --relay-model)
            [[ $# -ge 2 ]] || { echo "install.sh: --relay-model needs a model id" >&2; exit 2; }
            RELAY_MODEL="$2"
            RELAY_MODEL_FROM="--relay-model"
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            echo "install.sh: unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [[ "$UNINSTALL" -eq 0 && "$PURGE_DATA" -eq 1 ]]; then
    fail 2 "--purge-data only makes sense with --uninstall"
fi

# scratch files (logs, the staged copy) go under $TMPDIR, else under ~/.cache, never into this folder
TMP_BASE="${TMPDIR:-${HOME:?HOME is not set}/.cache/cao-shelf-install}"

# run a command that changes state (honours --dry-run)
run() {
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "+ $*"
        return 0
    fi
    "$@"
}

# version_ge A B: true when version A >= version B (numeric dotted parts, missing parts are 0)
version_ge() {
    local IFS=.
    local -a a b
    read -r -a a <<<"$1"
    read -r -a b <<<"$2"
    local i x y
    for ((i = 0; i < 4; i++)); do
        x="${a[i]:-0}"
        y="${b[i]:-0}"
        x="${x//[!0-9]/}"
        y="${y//[!0-9]/}"
        x="${x:-0}"
        y="${y:-0}"
        if ((10#$x > 10#$y)); then return 0; fi
        if ((10#$x < 10#$y)); then return 1; fi
    done
    return 0
}

# ---------------------------------------------------------------------------------------------------
# 1. kirocrew
# ---------------------------------------------------------------------------------------------------
if ! command -v kirocrew >/dev/null 2>&1; then
    fail 2 "kirocrew was not found on PATH. Install Kiro Crew ${MIN_KIROCREW} or later first (https://github.com/kirodotdev/KiroCrew), then run this script again."
fi
KC="$(command -v kirocrew)"
KC_VERSION_LINE="$("$KC" --version 2>&1 | head -n 1 || true)"
KC_VERSION="$(printf '%s' "$KC_VERSION_LINE" | grep -oE '[0-9]+(\.[0-9]+)+' | head -n 1 || true)"
info "Target: HOME=${HOME:-unset} KIROCREW_PORT=${KIROCREW_PORT:-default} kirocrew=${KC} (${KC_VERSION_LINE})"
if [[ -z "$KC_VERSION" ]]; then
    info "warning: could not read the kirocrew version from '${KC_VERSION_LINE}'; continuing"
elif ! version_ge "$KC_VERSION" "$MIN_KIROCREW"; then
    fail 2 "kirocrew ${KC_VERSION} is too old; this App needs ${MIN_KIROCREW} or later."
fi

kc() {
    "$KC" "$@"
}

is_installed() {
    kc app info "$APP_NAME" >/dev/null 2>&1
}

config_get() {
    kc config get "$1" 2>/dev/null | head -n 1 | tr '[:upper:]' '[:lower:]' || true
}

trust_state() {
    # prints: all, app, or none
    local all grants
    all="$(config_get agent.apps_allow_third_party)"
    grants="$(kc config get agent.apps_trusted 2>/dev/null || true)"
    if [[ "$all" == "true" ]]; then
        echo all
    elif [[ "$grants" == *"\"$APP_NAME\""* || "$grants" == *"'$APP_NAME'"* ]]; then
        echo app
    else
        echo none
    fi
}

set_app_grant() {
    # Adds the App name to a JSON list setting without touching the other entries.
    local key="$1" current
    current="$(kc config get "$key" 2>/dev/null || echo '[]')"
    local new
    new="$(printf '%s' "$current" | python3 -c '
import ast, json, sys
name = sys.argv[1]
raw = sys.stdin.read().strip() or "[]"
try:
    items = json.loads(raw)
except ValueError:
    items = ast.literal_eval(raw)
items = [i for i in items if isinstance(i, str)]
if name not in items:
    items.append(name)
print(json.dumps(items))' "$APP_NAME")"
    local b64
    b64="$(printf '%s' "$new" | base64 | tr -d '\n')"
    run "$KC" config set "$key" "--value-base64=$b64"
}

remove_app_grant() {
    local key="$1" current
    current="$(kc config get "$key" 2>/dev/null || echo '[]')"
    [[ "$current" == *"$APP_NAME"* ]] || return 0
    local new b64
    new="$(printf '%s' "$current" | python3 -c '
import ast, json, sys
name = sys.argv[1]
raw = sys.stdin.read().strip() or "[]"
try:
    items = json.loads(raw)
except ValueError:
    items = ast.literal_eval(raw)
print(json.dumps([i for i in items if i != name]))' "$APP_NAME")"
    b64="$(printf '%s' "$new" | base64 | tr -d '\n')"
    run "$KC" config set "$key" "--value-base64=$b64"
}

# ---------------------------------------------------------------------------------------------------
# Uninstall path
# ---------------------------------------------------------------------------------------------------
if [[ "$UNINSTALL" -eq 1 ]]; then
    if is_installed; then
        run "$KC" app disable "$APP_NAME" || info "disable reported a problem (continuing)"
        if [[ "$PURGE_DATA" -eq 1 ]]; then
            run "$KC" app uninstall --purge-data "$APP_NAME"
        else
            run "$KC" app uninstall "$APP_NAME"
        fi
        info "uninstalled ${APP_NAME}."
    else
        info "${APP_NAME} is not installed; nothing to uninstall."
    fi
    # Only the per-app grant is ours; the blanket flag is never touched here.
    remove_app_grant agent.apps_trusted
    remove_app_grant agent.apps_trusted_local
    if [[ "$(config_get agent.apps_allow_third_party)" == "true" ]]; then
        info "agent.apps_allow_third_party is still true (it trusts every third-party App). To turn it off:"
        info "  kirocrew config set agent.apps_allow_third_party false"
    fi
    if [[ "$PURGE_DATA" -eq 0 ]]; then
        info "App data (settings, ledger) was kept. To delete it: ./install.sh --uninstall --purge-data"
    fi
    exit 0
fi

# ---------------------------------------------------------------------------------------------------
# 2. the ops MCP server and the CAO address for the remote-control agent
# ---------------------------------------------------------------------------------------------------
OPS_SERVER="$(command -v cao-ops-mcp-server 2>/dev/null || true)"
if [[ -z "$OPS_SERVER" || ! -x "$OPS_SERVER" ]]; then
    fail 6 "cao-ops-mcp-server was not found on PATH. It comes with CAO (pip or uv install of cli-agent-orchestrator). Put it on PATH, for the shell that runs this script and for the Kiro Crew gateway, then run this script again. Nothing was installed."
fi
case "$OPS_SERVER" in
    /*) ;;
    *) OPS_SERVER="$(cd "$(dirname "$OPS_SERVER")" && pwd)/$(basename "$OPS_SERVER")" ;;
esac
# host and port: --cao-url, else CAO_SHELF_CAO_URL, else the Shelf's default cao_url
# (backend/cao_shelf/config.py, DEFAULTS); loopback http only, as the backend's own check
if [[ -z "$CAO_URL" && "$CAO_URL_FROM" == "CAO_SHELF_CAO_URL" ]]; then
    CAO_URL_FROM="default"
fi
# The backend's own check (cao.validate_cao_url, which also needs localhost to resolve to loopback)
# and default (config.DEFAULTS), imported rather than copied, so the two cannot drift.
rc=0
CAO_ADDRESS="$(python3 -B - "$HERE/backend" "$CAO_URL_FROM" "$CAO_URL" <<'PY'
import sys, urllib.parse
backend, source, given = sys.argv[1:4]
sys.path.insert(0, backend)
try:
    from cao_shelf.cao import validate_cao_url
    from cao_shelf.config import DEFAULTS
    from cao_shelf.errors import ShelfError
except Exception:
    sys.exit(3)
if source == "default":
    given = DEFAULTS["cao_url"]
try:
    url = validate_cao_url(given)
except ShelfError:
    sys.exit(1)
parts = urllib.parse.urlsplit(url)
host = parts.hostname or ""
print("[::1]" if host == "::1" else host, parts.port or 80)
PY
)" || rc=$?
if [[ "$rc" -eq 3 ]]; then
    fail 2 "could not load the backend's CAO address check from ${HERE}/backend with $(command -v python3); python3 must be able to import it."
elif [[ "$rc" -ne 0 ]]; then
    fail 2 "the CAO address from ${CAO_URL_FROM} ('${CAO_URL}') is not allowed: --cao-url and CAO_SHELF_CAO_URL must be http on 127.0.0.1, localhost (resolving to loopback) or ::1, with no path."
fi
CAO_HOST="${CAO_ADDRESS% *}"
CAO_PORT="${CAO_ADDRESS#* }"
info "cao-ops-mcp-server: ${OPS_SERVER} (CAO at ${CAO_HOST}:${CAO_PORT})"
# the relay agent's model: --relay-model, else CAO_SHELF_RELAY_MODEL (an empty one counts as unset),
# else the agent file's own "auto". Checked against Kiro Crew's model-name grammar, copied from
# kiro_crew/validation_parts/input_bounds.py (_MODEL_SEGMENT, QUALIFIED_MODEL_PATTERN, _MODEL_NAME_RE)
# with its MAX_SHORT_STRING cap (constants.py): Kiro Crew runs in its own environment, so the
# python3 here cannot import it.
if [[ "$RELAY_MODEL_FROM" == "--relay-model" || -n "$RELAY_MODEL" ]]; then
    python3 -B - "$RELAY_MODEL" <<'PY' || fail 2 "the relay model from ${RELAY_MODEL_FROM} is not a model id Kiro Crew accepts: expected letters, digits, '.', '_' and '-' starting with a letter or digit (optionally with one [qualifier]), or a provider/model id of at most 256 characters, such as claude-haiku-4.5 or auto. Nothing was installed."
import re, sys
segment = r"[A-Za-z0-9][A-Za-z0-9._-]*"
qualified = rf"{segment}(?:/{segment})+(?::{segment})?"
name = re.compile(rf"(?:{segment}(?:\[[A-Za-z0-9._-]+\])?|(?=.{{1,256}}\Z){qualified})\Z")
model = sys.argv[1]
sys.exit(0 if len(model) <= 500 and name.match(model) else 1)
PY
    info "relay agent model: ${RELAY_MODEL} (from ${RELAY_MODEL_FROM})"
else
    RELAY_MODEL=""
fi

# ---------------------------------------------------------------------------------------------------
# 3. bundle
# ---------------------------------------------------------------------------------------------------
BUNDLE="$HERE/ui/bundle/index.mjs"

bundle_state() {
    # prints: fresh, stale, missing. Fresh means the content hash of the build inputs (ui/src and the
    # build config) equals the one the build recorded in ui/bundle/.inputs-sha256. File times are
    # not used: a fresh clone writes ui/src after ui/bundle, so every source looks newer.
    if [[ ! -f "$BUNDLE" ]]; then
        echo missing
    elif python3 -B "$HERE/ui/scripts/inputs_hash.py" --check "$HERE/ui" 2>/dev/null; then
        echo fresh
    else
        echo stale
    fi
}

node_ok() {
    command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 || return 1
    local v major minor
    v="$(node --version 2>/dev/null | sed 's/^v//')"
    major="${v%%.*}"
    minor="${v#*.}"
    minor="${minor%%.*}"
    [[ "$major" =~ ^[0-9]+$ && "$minor" =~ ^[0-9]+$ ]] || return 1
    ((major > MIN_NODE_MAJOR || (major == MIN_NODE_MAJOR && minor >= MIN_NODE_MINOR)))
}

STATE="$(bundle_state)"
if [[ "$STATE" != "fresh" ]]; then
    info "UI bundle is ${STATE}."
    if [[ "$NO_BUILD" -eq 1 ]]; then
        if [[ "$STATE" == "missing" ]]; then
            fail 3 "ui/bundle/index.mjs is missing and --no-build was given. Build it (cd ui && npm ci && npm run build) or drop --no-build."
        fi
        info "warning: bundle was not built from the current ui/src and --no-build was given; installing the existing bundle."
    elif ! node_ok; then
        if [[ "$STATE" == "missing" ]]; then
            fail 3 "ui/bundle/index.mjs is missing and Node ${MIN_NODE_MAJOR}.${MIN_NODE_MINOR} or later with npm was not found. Install Node, or use a checkout that includes the built bundle."
        fi
        fail 3 "ui/bundle/index.mjs was not built from the current ui/src (the input hash in ui/bundle/.inputs-sha256 differs) and Node ${MIN_NODE_MAJOR}.${MIN_NODE_MINOR} or later with npm was not found, so it cannot be rebuilt. Install Node, or pass --no-build to install the existing bundle anyway."
    else
        info "building the UI bundle (npm ci, npm run build)"
        if [[ "$DRY_RUN" -eq 1 ]]; then
            echo "+ (cd ui && npm ci && npm run build)"
        else
            (cd "$HERE/ui" && npm ci && npm run build) || fail 3 "the UI build failed; see the output above."
        fi
    fi
fi

# ---------------------------------------------------------------------------------------------------
# 4. stage a copy whose app.json names the resolved server, then trust, install, enable
# ---------------------------------------------------------------------------------------------------
mkdir -p "$TMP_BASE"
STAGE_ROOT="$(mktemp -d "$TMP_BASE/stage.XXXXXX")"
ENABLE_LOG="$(mktemp "$TMP_BASE/enable.XXXXXX")"
trap 'rm -rf "$STAGE_ROOT"; rm -f "$ENABLE_LOG"' EXIT
STAGE="$STAGE_ROOT/$APP_NAME"
python3 - "$HERE" "$STAGE" "$OPS_SERVER" "$CAO_HOST" "$CAO_PORT" "$RELAY_MODEL" <<'PY' || fail 5 "could not stage a copy of the App for installing"
import json, os, shutil, sys
src, dst, server, host, port, relay_model = sys.argv[1:7]
skip = shutil.ignore_patterns(
    "node_modules", ".git", "__pycache__", ".venv", "artifacts", ".install-stage"
)


def ignore(folder, names):
    # the folder's own data/ (from make dev, say) is never installed over the App's data
    out = set(skip(folder, names))
    if os.path.abspath(folder) == os.path.abspath(src) and "data" in names:
        out.add("data")
    return out


shutil.copytree(src, dst, symlinks=True, ignore=ignore)
path = dst + "/app.json"
with open(path, encoding="utf-8") as handle:
    manifest = json.load(handle)
# The ops server lives only in the relay agent's own file, never in app.json: Kiro Crew writes every
# manifest mcpServers entry into the host's default agent config (kirocrew.json), which would give
# ordinary chats all of cao-ops-mcp. The agent's entry lists every other cao-ops-mcp tool in
# disabledTools (Kiro Crew's Claude backend mounts the whole server for an @cao-shelf:ops/<tool>
# grant and narrows it only through that list, as permissions.deny), and gets the resolved command
# and CAO address here. The relay agent also takes the --relay-model id, when one was given.
if manifest.get("mcpServers"):
    sys.exit("app.json declares mcpServers; the ops server belongs in the relay agent's file only")
found = False
for rel in manifest.get("agents") or []:
    agent_path = dst + "/" + rel
    with open(agent_path, encoding="utf-8") as handle:
        agent = json.load(handle)
    entry = (agent.get("mcpServers") or {}).get("cao-shelf:ops")
    changed = False
    if isinstance(entry, dict):
        entry["command"] = server
        entry["env"] = {"CAO_API_HOST": host, "CAO_API_PORT": port}
        changed = True
        found = found or agent.get("name") == "cao-remote-control"
    if relay_model and agent.get("name") == "cao-remote-control":
        agent["model"] = relay_model
        changed = True
    if changed:
        with open(agent_path, "w", encoding="utf-8") as handle:
            json.dump(agent, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
if not found:
    sys.exit("the cao-remote-control agent has no cao-shelf:ops server entry to write")
PY

if is_installed; then
    info "${APP_NAME} is already installed; updating from ${HERE}"
    UPDATE_LOG="$(mktemp "$TMP_BASE/update.XXXXXX")"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "+ $KC app update $APP_NAME --source $STAGE"
    elif ! "$KC" app update "$APP_NAME" --source "$STAGE" >"$UPDATE_LOG" 2>&1; then
        cat "$UPDATE_LOG"
        if grep -qi 'no running gateway' "$UPDATE_LOG"; then
            # `app update` needs a live gateway. Without one, reinstall from this folder; app data is kept.
            info "the Kiro Crew gateway is not running; reinstalling from ${HERE} instead (app data is kept)"
            "$KC" app uninstall "$APP_NAME" || fail 5 "kirocrew app uninstall failed; see the output above."
            "$KC" app install "$STAGE" || fail 5 "kirocrew app install failed; see the output above."
        else
            rm -f "$UPDATE_LOG"
            fail 5 "kirocrew app update failed; see the output above."
        fi
    else
        cat "$UPDATE_LOG"
        if [[ -n "$RELAY_MODEL" ]]; then
            # Kiro Crew carries an agent's `model` across an update as the user's preference
            # (kiro_crew/apps/bridges_agents.py, _preserve_user_agent_edits); a reinstall does not.
            info "note: Kiro Crew keeps the model already set on an installed agent when it updates an App, so the relay agent keeps its current model. To run it on ${RELAY_MODEL}: ./install.sh --uninstall, then this command again (app data is kept; enable CAO Shelf in the dashboard again)."
        fi
    fi
    rm -f "$UPDATE_LOG"
else
    info "installing ${APP_NAME} from ${HERE}"
    run "$KC" app install "$STAGE" || fail 5 "kirocrew app install failed; see the output above."
fi

# Trust is applied after install/update on purpose: uninstalling an App also removes its per-app grant.
case "$TRUST" in
    all)
        info "--trust-all: setting agent.apps_allow_third_party=true. This trusts EVERY third-party App on this Kiro Crew, not only this one."
        run "$KC" config set agent.apps_allow_third_party true
        ;;
    app)
        info "--trust-app: granting trust to ${APP_NAME} only (agent.apps_trusted)."
        set_app_grant agent.apps_trusted
        set_app_grant agent.apps_trusted_local
        ;;
esac

TRUST_NOW="$(trust_state)"
if [[ "$DRY_RUN" -eq 1 ]]; then
    TRUST_NOW="${TRUST:-$TRUST_NOW}"
fi
info "Third-party trust: ${TRUST_NOW}."
if [[ "$TRUST_NOW" == "none" ]]; then
    info "Kiro Crew will refuse to run this App until you trust it. Choose one:"
    info "  ./install.sh --trust-app        (this App only; recommended)"
    info "  ./install.sh --trust-all        (every third-party App; a shortcut)"
    info "  or in the dashboard: Settings, Security, Third-party apps"
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "+ $KC app enable $APP_NAME"
elif ! "$KC" app enable "$APP_NAME" >"$ENABLE_LOG" 2>&1; then
    cat "$ENABLE_LOG" >&2
    if grep -qiE 'asks to control your chats|session_approval_consent|sessionApprovalConsent' "$ENABLE_LOG"; then
        fail 7 "Kiro Crew installed ${APP_NAME} but keeps it off until you give it session-approval consent, which Kiro Crew accepts only in the dashboard (never from a script). Open the link above (or the dashboard: Apps, Library, CAO Shelf; a disabled App is not in the sidebar yet) and enable it there. CAO Shelf uses the grant only to set each relay chat's trust mode to normal; it approves no tool request."
    fi
    if grep -qiE 'app_execution_denied|third.party|not trusted|trust' "$ENABLE_LOG"; then
        fail 4 "Kiro Crew refused to enable ${APP_NAME} because third-party Apps are not trusted. The App is installed but disabled. Run ./install.sh --trust-app (or --trust-all) and it will finish."
    fi
    fail 5 "kirocrew app enable failed; see the output above."
else
    cat "$ENABLE_LOG"
fi

[[ "$DRY_RUN" -eq 0 ]] || info "dry run: nothing above was executed."
info "done. Open the Kiro Crew dashboard and choose 'CAO Shelf' in the sidebar (route /apps/${APP_NAME})."
info "The App reads CAO at http://127.0.0.1:9889 by default; change it in the page settings."
info "If Kiro Crew was already running and the page says the App has no reachable backend, restart Kiro Crew the way you normally do (the backend starts at gateway start)."
