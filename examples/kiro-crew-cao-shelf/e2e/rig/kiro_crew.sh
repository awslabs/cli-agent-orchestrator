#!/usr/bin/env bash
# Isolated Kiro Crew for the e2e rig.
#
#   kiro_crew.sh setup       install Kiro Crew 0.12.2 into $SHELF_SCRATCH/home if missing; seed config
#   kiro_crew.sh start       start the scratch gateway on 127.0.0.1:$SHELF_KC_PORT (default 5499)
#   kiro_crew.sh status      pid, port, token URL host (no secret printed)
#   kiro_crew.sh token       print the dashboard URL with an access token (a secret; scratch only)
#   kiro_crew.sh install-app run ./install.sh against the scratch home and enable the App
#   kiro_crew.sh stop        stop the scratch gateway by its recorded pid
#   kiro_crew.sh teardown    stop, then uninstall the App from the scratch home (data kept; --purge-data)
#   kiro_crew.sh model-start start only the scripted model (SHELF_MODEL_SCRIPT=<json> selects behaviours)
#   kiro_crew.sh model-stop  stop only the scripted model, by its recorded pid
#   kiro_crew.sh model-log   print the scripted model's decision log (one JSON line per Messages request)
#   kiro_crew.sh claude-settings  (re)write the allow rule and ENABLE_TOOL_SEARCH into the scratch home only
#   add --dry-run before the subcommand to print what would be done
#
# Safety: see lib.sh. This script never calls `kirocrew stop|restart|service|doctor`.
# SHELF_SCRATCH defaults to $HOME/kc-shelf-e2e; export it to use another scratch folder.
#
# Chats run on the Claude backend (agent.acp_backend=claude) through the claude-agent-acp adapter
# ($SHELF_ACP_VERSION, default 0.99.1, in $WORK_DIR/acp) and Claude Code ($SHELF_CLAUDE_CODE, default the
# newest ~/.toolbox claude-code). The gateway's ANTHROPIC_BASE_URL points at e2e/rig/mock_model.py on
# 127.0.0.1:$SHELF_MODEL_PORT (default 9981), started and stopped with the gateway; no real model credential
# is used. The Claude Code allow rule for the four cao-ops-mcp tools and ENABLE_TOOL_SEARCH=false are written
# into $ISO_HOME/.claude/settings.json only. SHELF_CLAUDE_BACKEND=0 turns all of this off.

set -u
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DRY=0
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY=1
    shift
fi
cmd="${1:-}"
shift || true

rig_check_scratch
rig_check_ports

KC="$RIG_LIB_DIR/kc.sh"
GW_LOG="$LOG_DIR/gateway.log"
MODEL_OUT="$LOG_DIR/mock-model.out"
MODEL_LOG="$LOG_DIR/mock-model.jsonl"
MODEL_IDENT="SHELF_MOCK_MODEL=$WORK_DIR"
OPS_ALLOW=(mcp__cao-shelf_ops__send_session_message mcp__cao-shelf_ops__get_terminal_status
    mcp__cao-shelf_ops__read_session_output mcp__cao-shelf_ops__get_session_info
    mcp__kirocrew-core__wait)

kc() {
    "$KC" kirocrew "$@"
}

act() {
    if [[ "$DRY" -eq 1 ]]; then
        echo "+ $*"
    else
        "$@"
    fi
}

gateway_path() {
    local p="$ISO_HOME/.local/bin"
    if [[ -z "$NODE_BIN_DIR" ]] && command -v node >/dev/null 2>&1; then
        NODE_BIN_DIR="$(dirname "$(node -p 'process.execPath')")"
    fi
    [[ -n "$NODE_BIN_DIR" ]] && p="$p:$NODE_BIN_DIR"
    p="$p:/usr/local/bin:/usr/bin:/bin"
    # Last, so its python does not shadow the system one: the App's agent runs cao-ops-mcp-server from here.
    [[ -x "$CAO_VENV/bin/cao-ops-mcp-server" ]] && p="$p:$CAO_VENV/bin"
    echo "$p"
}

claude_code_bin() {
    if [[ -n "$CLAUDE_CODE_BIN" ]]; then
        echo "$CLAUDE_CODE_BIN"
        return 0
    fi
    # The newest Claude Code from the toolbox (read only); `claude` on PATH may be a wrapper script.
    local c
    c="$(find "$REAL_HOME/.toolbox/tools/claude-code" -mindepth 2 -maxdepth 2 -name claude -type f 2>/dev/null | sort -V | tail -n 1)"
    [[ -n "$c" ]] || c="$(command -v claude 2>/dev/null || true)"
    echo "$c"
}

# The adapter: reuse a copy of the spike's install of the same version when one is in the scratch folder,
# else npm-install it into $ACP_DIR with the scratch HOME (its npm cache stays in the scratch home).
setup_acp() {
    if [[ -x "$ACP_BIN" ]]; then
        say "claude-agent-acp already installed in $ACP_DIR"
        return 0
    fi
    local spike="$SHELF_SCRATCH/work-rc/acp"
    local pkg="node_modules/@agentclientprotocol/claude-agent-acp/package.json"
    if [[ -x "$spike/node_modules/.bin/claude-agent-acp" ]] && grep -q "\"version\": \"$ACP_VERSION\"" "$spike/$pkg"; then
        say "copying claude-agent-acp $ACP_VERSION from $spike"
        act mkdir -p "$ACP_DIR"
        act cp -a "$spike/." "$ACP_DIR/"
        return 0
    fi
    say "installing @agentclientprotocol/claude-agent-acp@$ACP_VERSION into $ACP_DIR"
    act mkdir -p "$ACP_DIR"
    act env -i HOME="$ISO_HOME" PATH="$(gateway_path)" npm install --prefix "$ACP_DIR" --no-audit --no-fund \
        "@agentclientprotocol/claude-agent-acp@$ACP_VERSION" >>"$LOG_DIR/npm-acp.log" 2>&1 \
        || die "npm install of claude-agent-acp failed; see $LOG_DIR/npm-acp.log"
}

# Claude Code settings of the SCRATCH home only: allow the four ops tools without a prompt and keep MCP tools
# loaded up front (ENABLE_TOOL_SEARCH=false; with tool search on, Claude Code defers them). Existing keys are kept.
write_claude_settings() {
    local f="$ISO_HOME/.claude/settings.json"
    case "$f" in
        "$SHELF_SCRATCH"/*) ;;
        *) die "refusing to write Claude settings outside the scratch folder: $f" ;;
    esac
    if [[ "$DRY" -eq 1 ]]; then
        echo "+ write $f: permissions.allow += ${OPS_ALLOW[*]}; env.ENABLE_TOOL_SEARCH=false"
        return 0
    fi
    mkdir -p "$ISO_HOME/.claude"
    python3 - "$f" "${OPS_ALLOW[@]}" <<'EOF'
import json, os, sys
path, allow = sys.argv[1], sys.argv[2:]
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
except (OSError, ValueError):
    data = {}
perms = data.setdefault("permissions", {})
perms["allow"] = list(dict.fromkeys(list(perms.get("allow") or []) + allow))
data.setdefault("env", {}).update(
    {"ENABLE_TOOL_SEARCH": "false", "DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1",
     "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)
os.replace(tmp, path)
EOF
    say "scratch Claude settings written: $f"
}

setup_claude_backend() {
    [[ "$CLAUDE_BACKEND" == "1" ]] || return 0
    local cc
    cc="$(claude_code_bin)"
    [[ -n "$cc" && -x "$cc" ]] || die "Claude Code not found; set SHELF_CLAUDE_CODE=<path to the claude binary>"
    setup_acp
    act "$KC" kirocrew config set agent.acp_backend claude
    act "$KC" kirocrew config set agent.member_acp_backend claude
    write_claude_settings
}

model_start() {
    [[ "$CLAUDE_BACKEND" == "1" ]] || return 0
    if rec_alive mock-model; then
        say "mock model already running, pid $(rec_get mock-model pid)"
        return 0
    fi
    if port_listening "$MODEL_PORT"; then
        die "model port $MODEL_PORT is already in use by pid(s): $(port_owner_pids "$MODEL_PORT" | tr '\n' ' ') (not ours, not touching it)"
    fi
    local args=(--port "$MODEL_PORT" --log "$MODEL_LOG")
    [[ -n "$MODEL_SCRIPT" ]] && args+=(--script "$MODEL_SCRIPT")
    if [[ "$DRY" -eq 1 ]]; then
        echo "+ setsid env -i $MODEL_IDENT python3 $RIG_LIB_DIR/mock_model.py ${args[*]}  (pid recorded in $RUN_DIR/mock-model.pid)"
        return 0
    fi
    local pid i
    pid="$(rig_spawn mock-model mock-model "$MODEL_IDENT" "$MODEL_OUT" -- \
        env -i "$MODEL_IDENT" PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 \
        python3 "$RIG_LIB_DIR/mock_model.py" "${args[@]}")" || exit 1
    for ((i = 0; i < 50; i++)); do
        if curl -fsS -m 2 "http://127.0.0.1:$MODEL_PORT/health" >/dev/null 2>&1; then
            say "mock model listening on 127.0.0.1:$MODEL_PORT, pid $pid, log $MODEL_LOG"
            return 0
        fi
        sleep 0.2
    done
    die "mock model did not answer on $MODEL_PORT; see $MODEL_OUT (still recorded, pid $pid)"
}

model_stop() {
    if [[ "$DRY" -eq 1 ]]; then
        echo "+ kill recorded pid of mock-model ($(rec_get mock-model pid 2>/dev/null || echo none))"
        return 0
    fi
    rig_stop mock-model
}

do_setup() {
    if [[ ! -x "$ISO_HOME/.local/bin/kirocrew" ]]; then
        say "kirocrew is not installed in $ISO_HOME; installing $KC_VERSION with the official script"
        local dl="$WORK_DIR/kc-installer"
        act mkdir -p "$dl"
        act curl -fsSL https://download.crew.kiro.dev/cli.sh -o "$dl/cli.sh"
        # The installer runs under env -i with the scratch HOME so nothing touches the real home.
        act env -i HOME="$ISO_HOME" PATH="/usr/local/bin:/usr/bin:/bin" sh "$dl/cli.sh" --version "$KC_VERSION"
    else
        say "kirocrew already installed in the scratch home: $(kc --version 2>&1 | head -n 1)"
    fi
    # Seed. The first three stop the onboarding and privacy modals; the last turns the usage beacon off.
    local trust_all="${SHELF_TRUST_ALL:-1}"
    if [[ "$trust_all" == "1" ]]; then
        act "$KC" kirocrew config set agent.apps_allow_third_party true
    fi
    act "$KC" kirocrew config set dashboard.onboarded true
    act "$KC" kirocrew config set dashboard.privacy_acked true
    act "$KC" kirocrew config set dashboard.crewmates_onboarded true
    act "$KC" kirocrew config set telemetry.beacon_enabled false
    setup_claude_backend
    [[ "$DRY" -eq 1 ]] || echo "scratch home owned by the rig" >"$ISO_HOME/.shelf-rig-owned"
}

do_start() {
    if rec_alive gateway; then
        say "gateway already running, pid $(rec_get gateway pid)"
        model_start
        return 0
    fi
    [[ -x "$ISO_HOME/.local/bin/kirocrew" ]] || die "kirocrew is not installed in $ISO_HOME; run: kiro_crew.sh setup"
    if port_listening "$KC_PORT"; then
        die "port $KC_PORT is already in use by pid(s): $(port_owner_pids "$KC_PORT" | tr '\n' ' ') (not ours, not touching it)"
    fi
    if [[ "$DRY" -eq 1 ]]; then
        model_start
        echo "+ setsid env -i HOME=$ISO_HOME KIROCREW_PORT=$KC_PORT PATH=... [ANTHROPIC_BASE_URL=http://127.0.0.1:$MODEL_PORT CLAUDE_AGENT_ACP_BIN=$ACP_BIN ...] kirocrew gateway --port $KC_PORT --no-open --no-tunnel --json-ready  (pid recorded in $RUN_DIR/gateway.pid)"
        return 0
    fi
    local extra=()
    if [[ "$CLAUDE_BACKEND" == "1" ]]; then
        [[ -x "$ACP_BIN" ]] || die "claude-agent-acp is not installed in $ACP_DIR; run: kiro_crew.sh setup"
        model_start
        # ANTHROPIC_API_KEY is a placeholder the mock ignores; it is not a credential.
        extra=(ANTHROPIC_BASE_URL="http://127.0.0.1:$MODEL_PORT" ANTHROPIC_API_KEY=mock-not-a-credential
            CLAUDE_AGENT_ACP_BIN="$ACP_BIN" CLAUDE_CODE_EXECUTABLE="$(claude_code_bin)" DISABLE_AUTOUPDATER=1)
    fi
    : >"$GW_LOG"
    local pid
    pid="$(rig_spawn gateway kirocrew-gateway "HOME=$ISO_HOME" "$GW_LOG" -- \
        env -i HOME="$ISO_HOME" KIROCREW_PORT="$KC_PORT" PATH="$(gateway_path)" LANG=C.UTF-8 "${extra[@]}" \
        "$ISO_HOME/.local/bin/kirocrew" gateway --port "$KC_PORT" --no-open --no-tunnel --json-ready)" || exit 1
    say "gateway started, pid $pid, log $GW_LOG"
    local i
    for ((i = 0; i < 120; i++)); do
        if port_listening "$KC_PORT"; then
            say "gateway listening on 127.0.0.1:$KC_PORT"
            return 0
        fi
        if ! rec_alive gateway; then
            tail -n 20 "$GW_LOG" >&2
            rm -f "$(rec_file gateway)"
            die "gateway exited during start-up"
        fi
        sleep 0.5
    done
    die "gateway did not listen on $KC_PORT within 60 s; it is still recorded (pid $pid), stop it with: kiro_crew.sh stop"
}

do_stop() {
    if [[ "$DRY" -eq 1 ]]; then
        echo "+ kill recorded pid of gateway ($(rec_get gateway pid 2>/dev/null || echo none)) and its recorded descendants; no kirocrew stop"
        return 0
    fi
    rig_stop gateway
    # Backends the gateway started may sit outside its process tree (sandbox wrappers). List what is left
    # that carries the scratch HOME; they are reported, and killed only through their own recorded pid.
    sleep 1
    sweep_scratch_backends || true
    local i
    for ((i = 0; i < 50; i++)); do
        port_listening "$KC_PORT" || break
        sleep 0.2
    done
    model_stop
    if port_listening "$KC_PORT"; then
        say "warning: port $KC_PORT is still listening (pid(s) $(port_owner_pids "$KC_PORT" | tr '\n' ' '))"
        return 1
    fi
    say "gateway stopped; port $KC_PORT is free"
}

# After the gateway and its recorded descendants are gone, report (never kill) anything that still carries the
# scratch HOME in its environment, so a leftover is visible instead of silent.
sweep_scratch_backends() {
    local p pid_dir found=()
    for pid_dir in /proc/[0-9]*; do
        p="${pid_dir#/proc/}"
        [[ "$p" == "$$" ]] && continue
        [[ -r "$pid_dir/environ" ]] || continue
        proc_env_has "$p" "HOME=$ISO_HOME" && found+=("$p")
    done
    if [[ ${#found[@]} -gt 0 ]]; then
        say "leftover processes with the scratch HOME (NOT killed, not recorded by the rig): ${found[*]}"
        printf '%s\n' "${found[@]}" >"$RUN_DIR/leftover.txt"
        return 1
    fi
    rm -f "$RUN_DIR/leftover.txt"
    return 0
}

do_status() {
    if [[ "$CLAUDE_BACKEND" == "1" ]]; then
        if rec_alive mock-model; then
            say "mock model: running, pid $(rec_get mock-model pid), 127.0.0.1:$MODEL_PORT, $(wc -l <"$MODEL_LOG" 2>/dev/null || echo 0) requests logged in $MODEL_LOG"
        else
            say "mock model: not running (no matching pid record)"
        fi
        say "chat backend: $("$KC" kirocrew config get agent.acp_backend 2>/dev/null | tail -n 1), adapter $ACP_BIN"
    fi
    if rec_alive gateway; then
        say "gateway: running, pid $(rec_get gateway pid), scratch home $ISO_HOME, port $KC_PORT (listening: $(port_listening "$KC_PORT" && echo yes || echo no))"
    else
        say "gateway: not running (no matching pid record)"
        return 1
    fi
}

do_token() {
    rec_alive gateway || die "gateway is not running"
    kc token --port "$KC_PORT"
}

do_install_app() {
    local args=("$@")
    if [[ "${#args[@]}" -eq 0 ]]; then
        args=(--trust-all)
    fi
    # The ops server of the installed agent must address the SCRATCH CAO, never the owner's 9889.
    local cao_url="http://127.0.0.1:$CAO_PORT"
    if [[ "$DRY" -eq 1 ]]; then
        echo "+ $KC $EXAMPLE_DIR/install.sh --no-build --cao-url $cao_url ${args[*]}"
        echo "+ on exit 7: owner_enable.py --port $KC_PORT --app cao-shelf (owner session from the scratch login link)"
        return 0
    fi
    local rc=0
    "$KC" "$EXAMPLE_DIR/install.sh" --no-build --cao-url "$cao_url" "${args[@]}" || rc=$?
    if [[ "$rc" -eq 7 ]]; then
        # Kiro Crew's CLI refuses session-approval consent by design; the rig owns this scratch gateway and
        # gives it through the owner API, like the dashboard would.
        owner_enable || return 1
        rc=0
    fi
    return "$rc"
}

owner_enable() {
    rec_alive gateway || die "gateway is not running; the consent needs the scratch gateway"
    local f="$WORK_DIR/run/login-url.txt"
    (umask 077 && kc token --port "$KC_PORT" >"$f" 2>/dev/null) || die "kirocrew token failed"
    local rc=0
    python3 "$RIG_LIB_DIR/owner_enable.py" --port "$KC_PORT" --app cao-shelf --login-url-file "$f" || rc=$?
    rm -f "$f"
    return "$rc"
}

do_teardown() {
    do_stop
    if [[ "$DRY" -eq 1 ]]; then
        echo "+ kc.sh $EXAMPLE_DIR/install.sh --uninstall"
        return 0
    fi
    "$KC" "$EXAMPLE_DIR/install.sh" --uninstall "$@" || true
}

case "$cmd" in
    setup) do_setup ;;
    start) do_start ;;
    stop) do_stop ;;
    status) do_status ;;
    token) do_token ;;
    install-app) do_install_app "$@" ;;
    teardown) do_teardown "$@" ;;
    model-start) model_start ;;
    model-stop) model_stop ;;
    model-log) cat "$MODEL_LOG" 2>/dev/null || true ;;
    claude-settings) write_claude_settings ;;
    *)
        sed -n '2,/^set -u/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//' >&2
        exit 2
        ;;
esac
