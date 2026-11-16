#!/usr/bin/env bash
# Keyless e2e for the operator dashboard: a private cao-server with mock_cli
# terminals, and e2e/e2e.ts driving core/ against it. See e2e/README.md for
# what is simulated (an agent showing a prompt) and what is covered for real.
#
# Isolation. This script resumes whatever interrupt the run plane reports, so it
# only ever talks to the server it starts:
#   - a port other than 9889 that nothing answers on (CAO_E2E_PORT, default 9896);
#   - a throwaway HOME, so it never opens the real ~/.aws CAO database;
#   - its own tmux server: TMUX and TMUX_PANE removed and a fresh TMUX_TMPDIR, so
#     it never reaches the tmux server you are working in. It is stopped with
#     an explicit -S socket, never a bare kill-server.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "${APP_DIR}/../../.." && pwd)"
PORT="${CAO_E2E_PORT:-9896}"
BASE="http://127.0.0.1:${PORT}"

if [ "${PORT}" = 9889 ]; then
    echo "[e2e] Refusing port 9889: that is the default fleet port." >&2
    exit 1
fi
if curl -s -o /dev/null -m 2 "${BASE}/health"; then
    echo "[e2e] Something already answers on ${PORT}; stop it or set CAO_E2E_PORT." >&2
    exit 1
fi
command -v tmux >/dev/null || { echo "[e2e] tmux is required." >&2; exit 1; }

# Short paths: macOS caps a unix socket path at about 104 bytes.
SCRATCH="$(mktemp -d /tmp/cao-od-e2e.XXXXXX)"
mkdir -m 700 "${SCRATCH}/home" "${SCRATCH}/tmux" "${SCRATCH}/work"
SOCKET="${SCRATCH}/tmux/tmux-$(id -u)/default"
SERVER_PID=""

# The only way this script runs tmux: always -S on the private socket, with TMUX
# and TMUX_PANE removed. SOCKET is built above from the scratch dir this script
# created, so it can only name this run's own server.
private_tmux() {
    env -u TMUX -u TMUX_PANE tmux -S "${SOCKET}" "$@"
}

cleanup() {
    local code=$?
    if [ -n "${SERVER_PID}" ]; then
        kill "${SERVER_PID}" 2>/dev/null || true
        wait "${SERVER_PID}" 2>/dev/null || true
    fi
    if [ -S "${SOCKET}" ]; then
        private_tmux kill-server 2>/dev/null || true
    fi
    if [ "${code}" -ne 0 ] && [ -f "${SCRATCH}/server.log" ]; then
        echo "[e2e] server log tail:" >&2
        tail -40 "${SCRATCH}/server.log" >&2 || true
    fi
    # Pane shells may still be writing their history files as they exit.
    for _ in 1 2 3 4 5 6; do
        rm -rf "${SCRATCH}" 2>/dev/null && break
        sleep 0.5
    done
    exit "${code}"
}
trap cleanup EXIT INT TERM

CAO_SERVER=(uv run --project "${REPO_ROOT}" cao-server)
[ -x "${REPO_ROOT}/.venv/bin/cao-server" ] && CAO_SERVER=("${REPO_ROOT}/.venv/bin/cao-server")

echo "[e2e] starting cao-server on ${BASE}" >&2
(
    cd "${SCRATCH}/work"
    exec env -u TMUX -u TMUX_PANE -u CAO_HOME_DIR -u CAO_TERMINAL_ID \
        HOME="${SCRATCH}/home" TMUX_TMPDIR="${SCRATCH}/tmux" \
        CAO_API_PORT="${PORT}" CAO_AGUI_ENABLED=1 CAO_MOCK_CLI_SCRIPTED_PROMPTS=1 \
        PATH="${REPO_ROOT}/test/providers/fixtures/bin:${PATH}" \
        "${CAO_SERVER[@]}"
) >"${SCRATCH}/server.log" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 120); do
    curl -fsS -m 2 "${BASE}/health" >/dev/null 2>&1 && break
    if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
        echo "[e2e] cao-server exited before it became healthy" >&2
        exit 1
    fi
    sleep 0.5
done
curl -fsS -m 2 "${BASE}/health" >/dev/null || { echo "[e2e] server did not become healthy" >&2; exit 1; }

cd "${APP_DIR}"
env -u TMUX -u TMUX_PANE TMUX_TMPDIR="${SCRATCH}/tmux" \
    CAO_E2E_BASE="${BASE}" CAO_E2E_TMUX_SOCKET="${SOCKET}" CAO_E2E_WORKDIR="${SCRATCH}/work" \
    node --experimental-strip-types --experimental-eventsource --no-warnings e2e/e2e.ts

# Browser evidence: the session timeline panel, saved as e2e/artifacts/timeline-panel.png.
# It runs against the same private server, after the e2e above, and fails if the panel
# does not render. --no-install: the browser is put in place by the CI step (or
# `npm run playwright:install`); this script never installs one.
env -u TMUX -u TMUX_PANE CAO_E2E_BASE="${BASE}" \
    npx --no-install playwright test -c playwright.config.ts
