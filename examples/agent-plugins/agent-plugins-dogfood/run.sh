#!/usr/bin/env bash
#
# Agent Plugins dog-food — the feature installing CAO's own `cao` package
# through CAO's own new agent-plugin pipeline, asserting each step and exiting
# non-zero on ANY drift. This script IS the shift-left test that
# tools/record-dogfood-demo.mjs gates the recording on: a broken pipeline
# cannot print the PASS marker, so it cannot produce a green GIF.
#
# Two modes (per the handoff's offline-by-default / live-behind-a-flag rule):
#
#   OFFLINE (default): steps 1-5 run with NO provider binary, network, or
#     secrets. This is what CI records. Step 5's OpenCode half asserts the
#     written config shape (cao-ops -> enabled:false) — it proves CAO's OWN
#     write, per docs/issues/573-agent-plugins/opencode-verification.md.
#
#   LIVE (CAO_DOGFOOD_LIVE=1, needs the `opencode` binary): adds the
#     OBSERVATIONAL step 5 proof §2a demands — `opencode mcp list` reports the
#     removed server as `disabled` (○) and a sentinel wrapper confirms NO
#     subprocess is spawned for it, with a positive control that DOES spawn so
#     "no spawn" is observed, not assumed. `opencode` is not on CI, so this is
#     gated; the README states which assertion runs where.
#
# Everything runs under a scratch HOME + CAO_HOME_DIR + CAO_AGENTS_DIR so the
# operator's real ~/.aws/opencode/opencode.json, real Kiro agents dir, and real
# CAO home are never read or written. The scratch path is anonymous (a mktemp
# dir), so the PLUGIN_ROOT deliberately shown in step 3 carries no real home
# path — see CONTRIBUTING.md § "Recording test fixtures safely" (incident #436).
#
# Usage:
#   ./examples/agent-plugins/agent-plugins-dogfood/run.sh                 # offline
#   CAO_DOGFOOD_LIVE=1 ./examples/agent-plugins/agent-plugins-dogfood/run.sh  # + live OpenCode

set -uo pipefail

MARK="[agent-plugins-dogfood]"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
LIVE_MODE="${CAO_DOGFOOD_LIVE:-0}"

# THIS branch's code, not the installed cao (which predates the feature).
CAO=(uv run --project "$REPO_ROOT" cao)
# Plain python3 for JSON assertions — no CAO import, present on CI + macOS.
PY="${CAO_DOGFOOD_PYTHON:-python3}"

# ── isolation ───────────────────────────────────────────────────────────────
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/cao-dogfood.XXXXXX")"
# Canonicalize to the physical path (macOS /tmp -> /private/tmp) so the paths we
# print match what CAO writes after its own Path.resolve(), keeping the display
# consistent and the `rel()` collapse exact.
SCRATCH="$(cd "$SCRATCH" && pwd -P)"
export HOME="$SCRATCH/home"              # COPILOT_AGENTS_DIR + OpenCode config derive from here
export CAO_HOME_DIR="$SCRATCH/cao-home"  # relocates the whole CAO tree (skills, plugins, store, db)
export CAO_AGENTS_DIR="$SCRATCH/kiro-agents"  # KIRO_AGENTS_DIR reads this env var
# Requirement 16.5's ship gate. The management surface is default-off and refuses
# to execute without this — the CLI group errors out and the HTTP routes 404 —
# because hiding a verb from `cao --help` was never an execution gate. A recorder
# that drives the surface must open the gate explicitly, which also makes the GIF
# an honest demonstration of how an operator reaches it before M1 lands.
export CAO_AGENT_PLUGINS_ENABLED=1
KIRO_JSON="$CAO_AGENTS_DIR/dogfood-agent.json"
OPENCODE_JSON="$HOME/.aws/opencode/opencode.json"
CAP="$SCRATCH/capture"
mkdir -p "$HOME" "$CAO_HOME_DIR" "$CAO_AGENTS_DIR" "$CAP"

cleanup() {
    local code=$?
    rm -rf "$SCRATCH" >/dev/null 2>&1 || true
    exit "${code}"
}
trap cleanup EXIT INT TERM

# ── output helpers ───────────────────────────────────────────────────────────
# The recorder renders THIS script's stdout. Raw `cao` output goes to capture
# files (it carries a benign "no such table: terminals" warning without a DB,
# and `ls -l` would leak the OS username in its owner column — #436's class of
# leak). Only curated, identity-free lines are echoed for the GIF.
hdr()  { echo "$*"; }
ok()   { echo "    $*"; }
pass() { echo "    PASS: $*"; }
fail() { echo "$MARK FAIL: $*" >&2; exit 1; }
# Collapse the scratch/home prefix to a short token so long paths stay readable
# AND provably synthetic in the GIF.
rel()  { printf '%s' "${1/$CAO_HOME_DIR/\$CAO_HOME}"; }

hdr "=== CAO Agent Plugins — dog-food $([ "$LIVE_MODE" = 1 ] && echo '(live)' || echo '(offline)') ==="
hdr "Note: the 'cao plugin' surface is gated off by default (maintainer gate M1):"
hdr "      absent from 'cao --help', and it refuses to run without"
hdr "      CAO_AGENT_PLUGINS_ENABLED=1, which this run exports."
hdr "Isolated scratch (no real home, no secrets): $SCRATCH"

# ── step 1: validate ─────────────────────────────────────────────────────────
hdr ""
hdr "[1] cao plugin validate ./agent-plugin/cao"
"${CAO[@]}" plugin validate "$REPO_ROOT/agent-plugin/cao" --json >"$CAP/validate.json" 2>"$CAP/validate.err" \
    || fail "validate exited non-zero"
"$PY" - "$CAP/validate.json" <<'PY' >"$CAP/validate.summary" 2>&1 || fail "validate assertions failed (see below)$(cat "$CAP/validate.summary" 2>/dev/null)"
import json, sys
r = json.load(open(sys.argv[1]))
want = {"cao-agent-routing", "cao-session-management", "cao-supervisor-protocols", "cao-worker-protocols"}
got = {s["name"] for s in r.get("skills", [])}
assert r.get("loadable") is True, f"loadable != true: {r.get('loadable')!r}"
assert r.get("mcp_present") is True, f"mcp_present != true: {r.get('mcp_present')!r}"
assert want <= got, f"missing skills: {sorted(want - got)}"
srv = {s["name"] for s in r.get("mcp_servers", [])}
assert "cao-ops" in srv, f"cao-ops not in mcp_servers: {sorted(srv)}"
print(",".join(sorted(got)))
PY
ok "loadable=true   mcp_present=true"
ok "skills: $(cat "$CAP/validate.summary")"
pass "manifest loadable, 4 shipped skills named, MCP server 'cao-ops' present"

# ── step 2: add (skills projected as symlinks into the plugin store) ─────────
hdr ""
hdr "[2] cao plugin add ./agent-plugin/cao"
"${CAO[@]}" plugin add "$REPO_ROOT/agent-plugin/cao" >"$CAP/add.out" 2>"$CAP/add.err" \
    || fail "plugin add exited non-zero"
grep -q "installed" "$CAP/add.out" || fail "add did not report an install"
ok "$(grep -m1 'installed' "$CAP/add.out")"
ok "skills projected into the skill store as symlinks -> the plugin store:"
store_ok=1
for skill in cao-agent-routing cao-session-management cao-supervisor-protocols cao-worker-protocols; do
    link="$CAO_HOME_DIR/skills/$skill"
    [ -L "$link" ] || { store_ok=0; break; }
    target="$(readlink "$link")"
    case "$target" in
        */agent-plugins/cao/skills/"$skill") : ;;   # resolves into the plugin store
        *) store_ok=0; break ;;
    esac
done
[ "$store_ok" = 1 ] || fail "a projected skill is not a symlink into the plugin store"
# readlink (NOT `ls -l`) so no OS-username owner column reaches the GIF.
ok "cao-session-management -> $(rel "$(readlink "$CAO_HOME_DIR/skills/cao-session-management")")"
pass "all 4 skills are symlinks whose target resolves into …/agent-plugins/cao/skills/"

# ── synthetic profile: minimal, no real content, plugin-derived MCP only ─────
cat >"$SCRATCH/dogfood-agent.md" <<'MD'
---
name: dogfood-agent
description: Synthetic profile for the agent-plugins dog-food recording.
role: developer
---
Synthetic agent used only to prove plugin MCP + skill delivery end to end.
MD

# ── step 3: install for Kiro — the R1 fix, end to end ────────────────────────
hdr ""
hdr "[3] cao install dogfood-agent --provider kiro_cli    (R1 fix, end to end)"
"${CAO[@]}" install "$SCRATCH/dogfood-agent.md" --provider kiro_cli >"$CAP/inst-kiro.out" 2>"$CAP/inst-kiro.err" \
    || fail "kiro install exited non-zero"
[ -f "$KIRO_JSON" ] || fail "kiro agent JSON not written: $KIRO_JSON"
PLUGIN_ROOT="$("$PY" - "$KIRO_JSON" <<'PY' 2>"$CAP/kiro.err" || true
import json, sys
d = json.load(open(sys.argv[1]))
res = d.get("resources", [])
assert any(str(r).startswith("skill://") for r in res), "no skill:// glob in resources"
mcp = d.get("mcpServers", {})
assert "cao-ops" in mcp, "cao-ops not delivered into the agent profile"
env = mcp["cao-ops"].get("env", {})
root = env.get("PLUGIN_ROOT"); data = env.get("PLUGIN_DATA")
assert root and root.startswith("/"), f"PLUGIN_ROOT not an absolute path: {root!r}"
assert data and data.startswith("/"), f"PLUGIN_DATA not an absolute path: {data!r}"
assert root.rstrip("/").endswith("/agent-plugins/cao"), f"PLUGIN_ROOT not in plugin store: {root!r}"
# The x-cao-pre-expanded marker is CAO-internal and must never reach a provider file.
assert "x-cao-pre-expanded" not in json.dumps(d), "x-cao-pre-expanded marker leaked into provider config"
print(root)
PY
)"
[ -n "$PLUGIN_ROOT" ] || fail "kiro R1 assertions failed: $(cat "$CAP/kiro.err" 2>/dev/null)"
grep -q '"@cao-ops"' "$KIRO_JSON" || fail "@cao-ops not granted in allowedTools"
ok "resources: skill:// globs present"
ok "mcpServers.cao-ops.env.PLUGIN_ROOT = $PLUGIN_ROOT"
ok "PLUGIN_DATA present; allowedTools granted @cao-ops"
ok "x-cao-pre-expanded marker: absent (stripped before write)"
pass "cao-ops delivered with expanded PLUGIN_ROOT/PLUGIN_DATA; no marker leak"

# ── step 4: install for OpenCode + Finding 2 (isolated) ──────────────────────
hdr ""
hdr "[4] cao install dogfood-agent --provider opencode_cli"
"${CAO[@]}" install "$SCRATCH/dogfood-agent.md" --provider opencode_cli >"$CAP/inst-oc.out" 2>"$CAP/inst-oc.err" \
    || fail "opencode install exited non-zero"
[ -f "$OPENCODE_JSON" ] || fail "opencode.json not written: $OPENCODE_JSON"
"$PY" - "$OPENCODE_JSON" <<'PY' 2>"$CAP/oc.err" || fail "opencode enabled:true assertion failed: $(cat "$CAP/oc.err" 2>/dev/null)"
import json, sys
d = json.load(open(sys.argv[1]))
e = d["mcp"]["cao-ops"]["enabled"]
assert e is True, f'enabled must be a JSON boolean true, got {e!r} ({type(e).__name__})'
PY
ok "opencode.json  mcp.cao-ops.enabled = true   (real JSON boolean)"

# Finding 2 (the more serious defect) in a SEPARATE HOME so the collision demo
# cannot corrupt the main config that step 5 removes from. Same installed
# plugin (CAO_HOME_DIR unchanged); only OpenCode's config location moves.
OC2_HOME="$SCRATCH/oc-collision-home"
mkdir -p "$OC2_HOME/.aws/opencode"
"$PY" - "$OC2_HOME/.aws/opencode/opencode.json" <<'PY'
import json, sys
json.dump({"$schema": "https://opencode.ai/config.json",
           "mcp": {"cao-ops": {"type": "local",
                               "command": ["/usr/local/bin/user-own-cao-ops"],
                               "enabled": True}}},
          open(sys.argv[1], "w"), indent=2)
PY
HOME="$OC2_HOME" "${CAO[@]}" install "$SCRATCH/dogfood-agent.md" --provider opencode_cli \
    >"$CAP/inst-oc2.out" 2>"$CAP/inst-oc2.err" || fail "opencode collision reinstall exited non-zero"
grep -q "was not written to opencode.json" "$CAP/inst-oc2.err" \
    || fail "Finding 2 collision was not reported"
"$PY" - "$OC2_HOME/.aws/opencode/opencode.json" <<'PY' 2>"$CAP/oc2.err" || fail "Finding 2: user entry NOT preserved: $(cat "$CAP/oc2.err" 2>/dev/null)"
import json, sys
d = json.load(open(sys.argv[1]))
cmd = d["mcp"]["cao-ops"]["command"]
assert cmd == ["/usr/local/bin/user-own-cao-ops"], f"user command overwritten: {cmd!r}"
PY
ok "Finding 2 (isolated): install over a user's own 'cao-ops' entry ->"
ok "  user entry PRESERVED; finding emitted:"
ok "  \"…was not written to opencode.json … would destroy user configuration\""
pass "enabled:true written; a user's hand-written config is never clobbered"

# ── step 5: remove — cross-provider, per §2a ─────────────────────────────────
hdr ""
hdr "[5] cao plugin remove cao    (cross-provider removal, per handoff §2a)"
"${CAO[@]}" plugin remove cao --yes >"$CAP/remove.out" 2>"$CAP/remove.err" \
    || fail "plugin remove exited non-zero"

# Kiro / Copilot: wholesale rewrite -> the server is ABSENT.
"$PY" - "$KIRO_JSON" <<'PY' 2>"$CAP/kiro5.err" || fail "Kiro removal assertion failed: $(cat "$CAP/kiro5.err" 2>/dev/null)"
import json, sys
d = json.load(open(sys.argv[1]))
assert "cao-ops" not in d.get("mcpServers", {}), "cao-ops still present in Kiro config after removal"
assert "@cao-ops" not in d.get("allowedTools", []), "@cao-ops grant not withdrawn"
PY
ok "Kiro:     cao-ops ABSENT from the rewritten agent JSON; @cao-ops grant withdrawn"

# OpenCode: no delete exists, so removal sets enabled:false (offline substitute
# — proves CAO's own write; the live step below proves the behaviour it buys).
"$PY" - "$OPENCODE_JSON" <<'PY' 2>"$CAP/oc5.err" || fail "OpenCode removal assertion failed: $(cat "$CAP/oc5.err" 2>/dev/null)"
import json, sys
d = json.load(open(sys.argv[1]))
e = d["mcp"]["cao-ops"]["enabled"]
assert e is False, f"cao-ops must be disabled (JSON false) after removal, got {e!r} ({type(e).__name__})"
PY
ok "OpenCode: cao-ops enabled:false  (offline substitute — proves CAO's write)"

# ── step 5 (LIVE): observational no-spawn proof, per §2a + findings file ─────
if [ "$LIVE_MODE" = 1 ]; then
    if ! command -v opencode >/dev/null 2>&1; then
        fail "CAO_DOGFOOD_LIVE=1 but the 'opencode' binary is not on PATH"
    fi
    PROBE="$SCRATCH/oc-probe"
    mkdir -p "$PROBE/home" "$PROBE/xdg/config/opencode" "$PROBE/xdg/data" \
             "$PROBE/xdg/state" "$PROBE/xdg/cache" "$PROBE/sentinels"
    for kind in disabled enabled; do
        cat >"$PROBE/wrap_$kind.sh" <<EOF
#!/usr/bin/env bash
echo "spawn $kind pid=\$\$" >> "$PROBE/sentinels/$kind.spawned"
exit 0
EOF
        chmod +x "$PROBE/wrap_$kind.sh"
    done
    # Mirror CAO's post-removal shape (cao-ops enabled:false) but point the
    # command at a sentinel wrapper so "no spawn" is OBSERVED, plus a positive
    # control (enabled:true) that MUST spawn so the harness is proven able to see one.
    cat >"$PROBE/xdg/config/opencode/opencode.json" <<EOF
{
  "\$schema": "https://opencode.ai/config.json",
  "mcp": {
    "cao-ops":          { "type": "local", "command": ["$PROBE/wrap_disabled.sh", "cao-ops"], "enabled": false },
    "positive-control": { "type": "local", "command": ["$PROBE/wrap_enabled.sh", "pc"], "enabled": true }
  }
}
EOF
    # Inline env additions (not `env -i`) — matches the verified isolation recipe
    # in docs/issues/573-agent-plugins/opencode-verification.md. `--pure` disables external plugins.
    HOME="$PROBE/home" OPENCODE_TEST_HOME="$PROBE/home" \
        XDG_CONFIG_HOME="$PROBE/xdg/config" XDG_DATA_HOME="$PROBE/xdg/data" \
        XDG_STATE_HOME="$PROBE/xdg/state" XDG_CACHE_HOME="$PROBE/xdg/cache" \
        OPENCODE_DISABLE_PROJECT_CONFIG=1 OPENCODE_DISABLE_AUTOUPDATE=1 OPENCODE_DISABLE_MODELS_FETCH=1 \
        opencode mcp list --pure --print-logs --log-level INFO >"$CAP/mcp-list.out" 2>&1 || true
    # cao-ops reported disabled (○), and NO sentinel written for it.
    grep -Eq 'cao-ops.*disabled' "$CAP/mcp-list.out" || fail "opencode mcp list did not report cao-ops as disabled"
    [ ! -f "$PROBE/sentinels/disabled.spawned" ] || fail "disabled cao-ops WAS spawned — enabled:false did not stop it"
    [ -f "$PROBE/sentinels/enabled.spawned" ] || fail "positive control did NOT spawn — the harness cannot observe a spawn"
    ok "LIVE: opencode mcp list -> cao-ops shows '○ disabled'"
    ok "LIVE: no subprocess spawned for cao-ops (sentinel absent)"
    ok "LIVE: positive control DID spawn (harness proven to observe spawns)"
    pass "removal is ABSENT on Kiro/Copilot, DISABLED + observed no-spawn on OpenCode"
else
    ok "LIVE observational proof (opencode mcp list -> '○ disabled' + sentinel no-spawn):"
    ok "  skipped offline; runs under CAO_DOGFOOD_LIVE=1. See README + the report."
    pass "removal is ABSENT on Kiro/Copilot, DISABLED on OpenCode (config-shape substitute)"
fi

hdr ""
hdr "$MARK PASS: dog-food pipeline asserted end to end (validate, add, install x2, remove)."
exit 0
