#!/usr/bin/env bash
# live-claude-code.sh: approve and deny one real Claude Code permission prompt
# through the operator dashboard's core/ (the code the dashboard UI uses), and
# print the run-plane evidence. For #1156.
#
# OPT-IN and manual: CI never runs it, because it needs a real, logged-in
# Claude Code and spends two model turns on your account. YOU run this, in your
# own terminal. An unattended agent must not run it.
#
# WHAT IT DOES
#   1. Starts a private cao-server from this checkout's .venv on 127.0.0.1:9897
#      (CAO_API_PORT), with CAO_AGUI_ENABLED=1. It refuses port 9889 and any
#      port that already answers.
#   2. Writes a profile "od-live" into a throwaway CAO home: provider claude_code,
#      permissionMode default, tools fs_read and fs_write. Write is allowed but not
#      pre-approved, so Claude Code asks before writing.
#   3. Creates one Claude Code terminal in an empty throwaway work dir.
#   4. Asks it to create approve.txt, waits for the permission prompt, checks that
#      the run plane reports reason claude-code:permission_request, and approves.
#   5. Asks it to create deny.txt, waits for the prompt, and denies.
#   6. Prints the run-plane results, the terminal status before and after each
#      decision, and whether approve.txt is present and deny.txt is absent. It
#      never prints pane content.
#   7. Stops the server by its recorded PID, stops its private tmux server by
#      socket, and removes the temp dirs, on exit, Ctrl-C or kill.
#
# WHAT IT TOUCHES
#   - Your own Claude Code login and profile, through your real HOME, the same
#     as any normal Claude Code launch. This is your real profile, not a clean
#     one. Claude Code reads the login and may refresh it (write a new token),
#     not only read it.
#   - Two short model turns on your account.
#   - ~/.claude and ~/.claude.json: Claude Code writes its session transcripts
#     and history there, and may record the throwaway work dir as trusted. These
#     stay after the cleanup.
#   - ~/.claude/settings.json: on launch, CAO's claude_code provider rewrites it
#     if skipDangerousModePermissionPrompt is not already true. If the file
#     cannot be read or parsed at that moment, CAO replaces it with only that
#     flag, which loses your hooks and permission rules. That is existing CAO
#     behaviour, not something this script adds. Back the file up yourself
#     before running, if you want to be safe. No agent seat reads or copies it.
#   - Your hooks and permission rules apply. If one answers or skips the
#     permission prompt, the script never sees it waiting, and the run fails
#     with a timeout after spending turns.
#   - Your user-level hooks and MCP servers run inside Claude Code. They inherit
#     this run's private CAO_HOME_DIR, CAO_API_PORT and tmux socket, but one with
#     a hard-coded path, or one that talks to port 9889, could still reach your
#     real CAO state.
#   - The panes and the private tmux server use your real HOME, so your shell rc
#     files and ~/.tmux.conf (and any plugins they load) run in them.
#   - Nothing of CAO's real state directly. CAO_HOME_DIR is a throwaway dir
#     (mode 700), so the real ~/.aws/cli-agent-orchestrator database and
#     profiles are not used.
#   - Not your tmux server. TMUX, TMUX_PANE, CAO_TERMINAL_ID and CAO_HOME_DIR are
#     cleared at the top; tmux runs on a private TMUX_TMPDIR, and every tmux
#     command here names that private socket with -S.
#   - No Keychain or credential store directly. Claude Code itself reads, and
#     may refresh, its own login, as it always does.
#
# USAGE
#   ./live-claude-code.sh --dry-run    # print what it would do, start nothing
#   ./live-claude-code.sh              # run it
#   ./live-claude-code.sh --keep-log   # run it, and copy the server log to
#                                      # cao-live-server.log in the directory you
#                                      # ran the script from, before cleanup.
#                                      # Its contents are not fully known (it
#                                      # may contain local paths and more); read
#                                      # it before sharing it.
#   Any other argument is refused. Environment: CAO_LIVE_PORT (default 9897);
#   CAO_SERVER (default: .venv/bin/cao-server in the checkout this script is
#   in, so run `uv sync --all-extras` at the repository root first).
#
#   Ctrl-C or kill: the cleanup runs once the Node driver in the foreground has
#   exited, so it can take a moment.
#
# RESULT: PASS needs, for both decisions: the interrupt reason
# claude-code:permission_request; a resume run that finished (success or
# interrupt); the card exactly "delivered"; and the terminal leaving
# waiting_user_answer within the timeout. Also approve.txt present with "ok"
# and deny.txt absent. Any failure or timeout stops the run as FAIL.

set -euo pipefail
# Where the script was started, before any cd: --keep-log writes here.
START_DIR="$PWD"

# ── arguments: nothing, --dry-run or --keep-log; anything else is refused ───
DRY_RUN=0
KEEP_LOG=0
if [ "$#" -gt 1 ]; then
    printf '[live] usage: %s [--dry-run | --keep-log]\n' "$0" >&2
    exit 2
fi
case "${1:-}" in
    "") ;;
    --dry-run) DRY_RUN=1 ;;
    --keep-log) KEEP_LOG=1 ;;
    *) printf '[live] unknown argument %s; usage: %s [--dry-run | --keep-log]\n' "$1" "$0" >&2; exit 2 ;;
esac

# ── clear inherited state first ─────────────────────────────────────────────
unset TMUX TMUX_PANE CAO_TERMINAL_ID CAO_HOME_DIR

# Everything is found from this script's own location: the app is its parent
# directory, and the repository root is three levels above the app.
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "${APP_DIR}/../../.." && pwd)"
CAO_SERVER="${CAO_SERVER:-${REPO_ROOT}/.venv/bin/cao-server}"
PORT="${CAO_LIVE_PORT:-9897}"
BASE="http://127.0.0.1:${PORT}"

say() { printf '[live] %s\n' "$*"; }

if [ "${DRY_RUN}" = 1 ]; then
    cat <<EOF
[live] DRY RUN: nothing is started, created or changed.
[live] would check: port ${PORT} is not 9889 and nothing answers ${BASE}/health
[live] would check: ${CAO_SERVER} is executable; tmux, node and claude are on PATH
[live] would create: a throwaway CAO home, tmux dir and work dir under one mktemp -d dir, mode 700
[live] would start: ${CAO_SERVER} with CAO_API_PORT=${PORT} CAO_AGUI_ENABLED=1 CAO_HOME_DIR=<throwaway> TMUX_TMPDIR=<private>, your real HOME
[live] would write: profile od-live (claude_code, permissionMode default, allowedTools fs_read fs_write) into <throwaway>/agent-store
[live] would create: one claude_code terminal in <throwaway work dir>
[live] would ask: "Create a file named approve.txt containing ok" -> wait for waiting_user_answer -> check reason -> approve
[live] would ask: "Create a file named deny.txt containing no" -> wait for waiting_user_answer -> check reason -> deny
[live] would print: run-plane outcomes, card states, reasons, terminal status before and after each decision, approve.txt/deny.txt presence
[live] would check: for each decision, a finished resume run, the card exactly "delivered", the terminal leaving waiting_user_answer; any failure or timeout is FAIL
[live] would clean up: stop the server by PID, kill the private tmux server by -S socket, remove the temp dirs$( [ "${KEEP_LOG}" = 1 ] && printf ' (after copying server.log)' )
EOF
    exit 0
fi

# ── preflight (no changes) ──────────────────────────────────────────────────
if [ "${PORT}" = 9889 ]; then say "refusing port 9889: that is the default fleet port"; exit 1; fi
if curl -s -o /dev/null -m 2 "${BASE}/health"; then say "something already answers ${BASE}; stop it or set CAO_LIVE_PORT"; exit 1; fi
[ -x "${CAO_SERVER}" ] || { say "not found: ${CAO_SERVER} (run 'uv sync --all-extras' in ${REPO_ROOT}, or set CAO_SERVER)"; exit 1; }
for tool in tmux node claude curl; do
    command -v "${tool}" >/dev/null || { say "${tool} is not on PATH"; exit 1; }
done
[ -d "${APP_DIR}/node_modules" ] || { say "run 'npm ci' in ${APP_DIR} first"; exit 1; }

# ── private state ───────────────────────────────────────────────────────────
umask 077
SCRATCH="$(mktemp -d /tmp/cao-live.XXXXXX)"
SOCKET="${SCRATCH}/tmux/tmux-$(id -u)/default"
SERVER_PID=""

private_tmux() { env -u TMUX -u TMUX_PANE tmux -S "${SOCKET}" "$@"; }

cleanup() {
    local code=$?
    if [ -n "${SERVER_PID}" ] && kill -0 "${SERVER_PID}" 2>/dev/null; then
        kill "${SERVER_PID}" 2>/dev/null || true
        wait "${SERVER_PID}" 2>/dev/null || true
    fi
    if [ -S "${SOCKET}" ]; then private_tmux kill-server 2>/dev/null || true; fi
    if [ "${KEEP_LOG}" = 1 ] && [ -f "${SCRATCH}/server.log" ]; then
        cp "${SCRATCH}/server.log" "${START_DIR}/cao-live-server.log" &&
            say "server log copied to ${START_DIR}/cao-live-server.log; its contents are not fully known (local paths and more), so read it before sharing it"
    fi
    for _ in 1 2 3 4 5 6; do rm -rf "${SCRATCH}" 2>/dev/null && break; sleep 0.5; done
    say "cleaned up (server stopped, private tmux stopped, temp dirs removed)"
    exit "${code}"
}
# Set right after mktemp, so a failure from here on still stops the server
# (by PID), then the private tmux server (by -S socket), then removes the dir.
trap cleanup EXIT
# On Ctrl-C or kill, run the cleanup once: exit, and let the EXIT trap do it.
trap 'exit 130' INT
trap 'exit 143' TERM

chmod 700 "${SCRATCH}"
mkdir -m 700 "${SCRATCH}/cao-home" "${SCRATCH}/cao-home/agent-store" "${SCRATCH}/tmux" "${SCRATCH}/work"
mkdir -m 700 "${SCRATCH}/tmux/tmux-$(id -u)"

cat > "${SCRATCH}/cao-home/agent-store/od-live.md" <<'EOF'
---
name: od-live
description: operator dashboard live check (throwaway)
provider: claude_code
permissionMode: default
allowedTools: ["fs_read", "fs_write"]
provider_init_timeout: 180
---
You write files when asked. Use the Write tool.
EOF

# ── server ──────────────────────────────────────────────────────────────────
say "starting cao-server on ${BASE} (throwaway CAO home, private tmux)"
(
    cd "${SCRATCH}/work"
    exec env -u TMUX -u TMUX_PANE -u CAO_TERMINAL_ID \
        CAO_HOME_DIR="${SCRATCH}/cao-home" TMUX_TMPDIR="${SCRATCH}/tmux" \
        CAO_API_PORT="${PORT}" CAO_AGUI_ENABLED=1 \
        "${CAO_SERVER}"
) >"${SCRATCH}/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 120); do
    curl -fsS -m 2 "${BASE}/health" >/dev/null 2>&1 && break
    kill -0 "${SERVER_PID}" 2>/dev/null || { say "cao-server exited before it became healthy"; exit 1; }
    sleep 0.5
done
curl -fsS -m 2 "${BASE}/health" >/dev/null || { say "cao-server did not become healthy"; exit 1; }
# The port answered; make sure our server process is still alive (kill -0 proves only that).
kill -0 "${SERVER_PID}" 2>/dev/null || { say "cao-server exited; something else answers ${BASE}"; exit 1; }
say "server healthy, pid ${SERVER_PID}"

# ── driver: core/ headless, the same code the dashboard uses ────────────────
cat > "${SCRATCH}/driver.ts" <<EOF
import { createController, stateText } from "${APP_DIR}/core/index.ts";

const BASE = "${BASE}";
const WORK = "${SCRATCH}/work";
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const say = (m: string) => console.log("[live] " + m);
const status = async (id: string) => ((await (await fetch(BASE + "/terminals/" + id)).json()) as { status: string }).status;
async function waitStatus(id: string, want: string[], ms: number): Promise<string> {
  const end = Date.now() + ms;
  for (;;) {
    const s = await status(id);
    if (want.includes(s)) return s;
    if (Date.now() > end) throw new Error("terminal is " + s + ", wanted " + want.join("/"));
    await sleep(1000);
  }
}
async function send(id: string, text: string) {
  const res = await fetch(BASE + "/terminals/" + id + "/input?message=" + encodeURIComponent(text), { method: "POST" });
  if (!res.ok) throw new Error("input: HTTP " + res.status);
}
const { existsSync, readFileSync } = await import("node:fs");

const ctl = createController({ base: BASE, threadId: "od-live", timeoutMs: 30_000 });
class Fail extends Error {}
const fail = (m: string): never => {
  throw new Fail(m);
};
async function waitLeft(id: string, ms: number): Promise<string> {
  const end = Date.now() + ms;
  for (;;) {
    const s = await status(id);
    if (s !== "waiting_user_answer") return s;
    if (Date.now() > end) fail("the terminal was still waiting_user_answer after " + ms / 1000 + " s");
    await sleep(1000);
  }
}
try {
  const q = new URLSearchParams({ agent_profile: "od-live", provider: "claude_code", session_name: "od-live", working_directory: WORK });
  const created = await fetch(BASE + "/sessions?" + q, { method: "POST" });
  if (created.status !== 201) fail("create session: HTTP " + created.status);
  const terminal = ((await created.json()) as { id: string }).id;
  say("terminal created; waiting for Claude Code to be ready (up to 180 s)");
  await waitStatus(terminal, ["idle", "completed"], 180_000);
  for (const [file, content, decision] of [["approve.txt", "ok", "approve"], ["deny.txt", "no", "deny"]] as const) {
    say("--- " + decision + ": asking for " + file);
    await send(terminal, "Create a file named " + file + " containing " + content);
    const before = await waitStatus(terminal, ["waiting_user_answer"], 180_000).catch((e: Error) => fail("never waited for an answer: " + e.message));
    say("terminal status before the decision: " + before);
    const listing = await ctl.refresh();
    say("listing run outcome: " + (listing?.kind ?? "none") + (listing?.kind === "INTERRUPT" ? " (" + listing.interrupts.length + " interrupt)" : ""));
    const card = ctl.board.cards.find((c) => c.terminalId === terminal && c.state.s === "pending");
    if (!card) fail("no pending interrupt for the terminal");
    say("interrupt reason: " + card!.reason + "; controls: " + card!.controls.join(","));
    if (card!.reason !== "claude-code:permission_request") fail("unexpected reason " + card!.reason + "; not answering");
    const outcome = await ctl.decide(card!.id, { kind: decision });
    const kind = outcome?.kind ?? "none";
    say("resume run outcome: " + kind);
    const after = ctl.board.cards.find((c) => c.id === card!.id)!;
    say("card state: " + stateText(after));
    if (ctl.board.lastError) say("error shown: " + ctl.board.lastError);
    if (kind !== "SUCCESS" && kind !== "INTERRUPT") fail("the resume run did not finish: " + kind);
    if (after.state.s !== "delivered" || after.state.unconfirmed) fail("the card is not exactly delivered: " + stateText(after));
    const left = await waitLeft(terminal, 120_000);
    say("terminal status after the decision: " + left);
    // Leaving waiting_user_answer is not enough: a terminal that has gone, errored or
    // is unknown also "leaves". It must settle back to idle or completed. No catch:
    // a timeout throws into the catch below as FAIL. This also runs after the
    // approve, before the deny task is sent.
    const settled = await waitStatus(terminal, ["idle", "completed"], 120_000);
    say("terminal status once settled: " + settled);
  }
  const approvePresent = existsSync(WORK + "/approve.txt");
  const approveContent = approvePresent ? readFileSync(WORK + "/approve.txt", "utf8").trim() : "";
  const denyPresent = existsSync(WORK + "/deny.txt");
  say("approve.txt: " + (approvePresent ? "present, content " + JSON.stringify(approveContent) : "ABSENT"));
  say("deny.txt: " + (denyPresent ? "PRESENT" : "absent"));
  if (!approvePresent || approveContent !== "ok") fail("approve.txt is missing or wrong");
  if (denyPresent) fail("deny.txt exists after a deny");
  say("RESULT: PASS");
  process.exit(0);
} catch (error) {
  say("RESULT: FAIL: " + (error instanceof Error ? error.message : String(error)));
  process.exit(1);
}
EOF

cd "${APP_DIR}"
env -u TMUX -u TMUX_PANE node --experimental-strip-types --no-warnings "${SCRATCH}/driver.ts"
