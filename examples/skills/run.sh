#!/usr/bin/env bash
#
# End-to-end CAO skill lifecycle: install, discover, load, apply, update, remove.
#
# Default mode is fully deterministic and needs no cao-server, no provider
# credentials, and no network: it runs inside a temporary CAO_HOME_DIR, so the
# operator's installed skills and profiles are never touched. `--live` adds one
# real claude_code session on top: it starts a DEDICATED cao-server bound to
# the same temporary home on a free port, so the session, profile, and skill
# catalog all live in the example's isolation — the operator's own cao-server
# (if any) is never contacted. claude_code is the example's documented provider
# because its skill delivery is the runtime-prompt catalog this example
# demonstrates — see docs/skills.md, "How Skills Work by Provider".
#
# Usage:
#   ./run.sh            # deterministic lifecycle (default)
#   ./run.sh --live     # + one real claude_code session (needs the claude CLI)
#
# Exit codes:
#   0 — every step and assertion passed
#   1 — an assertion failed (the failing step is printed)
#
# Cleanup: the temporary home (including the --live cao-server) is removed on
# exit; in --live mode the session is shut down first. The operator's real CAO
# home and cao-server are never touched.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_NAME="python-review-checklist"
DECOY_NAME="skills-lifecycle-decoy"
PROFILE_NAME="skills_lifecycle_reviewer"
LIVE=0
[ "${1:-}" = "--live" ] && LIVE=1

# Isolation: CAO_HOME_DIR is read once at import time (constants.py), so it
# must be exported BEFORE any cao invocation — including the very first one.
CAO_TMP_HOME="$(mktemp -d)"
export CAO_HOME_DIR="${CAO_TMP_HOME}"

LIVE_SESSION="skills-lifecycle-$$"
LIVE_PREFIXED="cao-${LIVE_SESSION}"
TIMEOUT="${CAO_SKILLS_LIVE_TIMEOUT:-600}"
POLL_INTERVAL="${CAO_SKILLS_POLL_INTERVAL:-5}"
CAO_SERVER_PID=""

cleanup() {
    if [ "${LIVE}" -eq 1 ] && [ -n "${CAO_SERVER_PID}" ]; then
        cao shutdown --session "${LIVE_PREFIXED}" >/dev/null 2>&1 || true
        kill "${CAO_SERVER_PID}" 2>/dev/null || true
        wait "${CAO_SERVER_PID}" 2>/dev/null || true
    fi
    rm -rf "${CAO_TMP_HOME}"
}
trap cleanup EXIT INT TERM

step() { echo "[skills-example] step: $1" >&2; }
fail() { echo "[skills-example] FAIL: $1" >&2; exit 1; }

# Resolve the interpreter that ships `cao`, so the fixture helpers import the
# same cli_agent_orchestrator package the CLI uses. `uv tool install` puts
# `cao` on PATH as a SYMLINK (~/.local/bin/cao -> .../tools/<name>/bin/cao),
# so the symlink must be resolved before looking for `python` beside it —
# `~/.local/bin/python` does not exist. CAO_PYTHON overrides the resolution.
CAO_BIN="$(command -v cao)" || {
    echo "[skills-example] FAIL: 'cao' is not on PATH" >&2
    exit 1
}
if [ -n "${CAO_PYTHON:-}" ]; then
    PYTHON_BIN="${CAO_PYTHON}"
else
    CAO_REAL="$(realpath "${CAO_BIN}" 2>/dev/null \
        || python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "${CAO_BIN}")"
    PYTHON_BIN="$(dirname "${CAO_REAL}")/python"
    [ -x "${PYTHON_BIN}" ] || PYTHON_BIN="$(command -v python3)"
fi
"${PYTHON_BIN}" -c "import cli_agent_orchestrator" >/dev/null 2>&1 || {
    echo "[skills-example] FAIL: '${PYTHON_BIN}' cannot import cli_agent_orchestrator." >&2
    echo "[skills-example]        Set CAO_PYTHON to the interpreter that installed CAO." >&2
    exit 1
}

# --- Step 1: install the bundled skill into the isolated home ---------------
step "install ${SKILL_NAME} into a temporary CAO home"
cao skills add "${HERE}/python-review-checklist" >/dev/null
[ -f "${CAO_HOME_DIR}/skills/${SKILL_NAME}/SKILL.md" ] \
    || fail "skill was not installed into the isolated home"

# --- Step 2: reinstall without --force is refused; --force is the only overwrite
step "reinstall guard (refused without --force, succeeds with --force)"
if cao skills add "${HERE}/python-review-checklist" >/dev/null 2>&1; then
    fail "reinstall without --force should have been refused"
fi
cao skills add "${HERE}/python-review-checklist" --force >/dev/null

# --- Step 3: discovery -------------------------------------------------------
step "cao skills list reports the installed skill"
cao skills list | grep -q "${SKILL_NAME}" || fail "skill missing from cao skills list"

# --- Step 4: a decoy skill, so the scoping check has something to exclude ----
step "install a decoy skill (${DECOY_NAME})"
DECOY_SRC="${CAO_TMP_HOME}/decoy-src/${DECOY_NAME}"
mkdir -p "${DECOY_SRC}"
cat >"${DECOY_SRC}/SKILL.md" <<'EOF'
---
name: skills-lifecycle-decoy
description: A second skill installed only so the profile-scoped catalog can be seen excluding it
---

# Decoy skill

This skill exists to make the example's scoping assertion meaningful: the
profile-scoped catalog must advertise python-review-checklist and must NOT
advertise this skill.
EOF
cao skills add "${DECOY_SRC}" >/dev/null

# --- Step 5: the full Markdown body is retrievable ---------------------------
step "load_skill retrieval path returns the complete body"
BODY="$("${PYTHON_BIN}" "${HERE}/fixture/show_skill_body.py")" \
    || fail "show_skill_body.py could not load the installed skill"
echo "${BODY}" | grep -q "Checklist applied" \
    || fail "loaded body does not contain the skill's output contract"

# --- Step 6: a profile declares the skill; the scoped catalog contains it ----
# and excludes the decoy — with only one skill installed, "scoped" and "full"
# catalogs would be identical and the check would prove nothing.
step "profile ${PROFILE_NAME} scopes the runtime catalog to the skill"
cao install "${HERE}/profiles/${PROFILE_NAME}.md" >/dev/null
CATALOG="$("${PYTHON_BIN}" "${HERE}/fixture/show_scoped_catalog.py")" \
    || fail "show_scoped_catalog.py could not load the installed profile"
echo "${CATALOG}" | grep -q -- "- \*\*${SKILL_NAME}\*\*" \
    || fail "scoped catalog does not advertise ${SKILL_NAME}"
if echo "${CATALOG}" | grep -q -- "- \*\*${DECOY_NAME}\*\*"; then
    fail "scoped catalog advertises the decoy ${DECOY_NAME} — the profile's skills filter did not engage"
fi

# --- Step 7: fixture task — observable result derived from the skill ---------
step "fixture task applies the skill guidance to sample_note.md"
APPLY_OUTPUT="$("${PYTHON_BIN}" "${HERE}/fixture/apply_checklist.py")" \
    || fail "apply_checklist.py failed — see its error above"
echo "${APPLY_OUTPUT}" | grep -q "^Checklist applied: 4/4 items$" \
    || fail "fixture task did not report the checklist as applied"
for EXPECTED_FINDING in "used but never imported" "mutable default argument" "bare 'except:'" "missing type hints"; do
    echo "${APPLY_OUTPUT}" | grep -q "${EXPECTED_FINDING}" \
        || fail "fixture task did not report the expected finding: ${EXPECTED_FINDING}"
done

# --- Step 8 (optional): one real provider session ----------------------------
if [ "${LIVE}" -eq 1 ]; then
    step "live: start a dedicated cao-server on the temporary home"
    LIVE_PORT="$("${PYTHON_BIN}" - <<'PY'
import socket

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"
    export CAO_API_HOST="127.0.0.1" CAO_API_PORT="${LIVE_PORT}"
    cao-server >"${CAO_TMP_HOME}/cao-server.log" 2>&1 &
    CAO_SERVER_PID=$!
    "${PYTHON_BIN}" - "${LIVE_PORT}" "${CAO_SERVER_PID}" <<'PY' \
        || fail "live cao-server did not become ready (log: ${CAO_TMP_HOME}/cao-server.log)"
import os
import sys
import time
import urllib.request

port, pid = sys.argv[1], int(sys.argv[2])
url = f"http://127.0.0.1:{port}/workflows"
for _ in range(60):
    try:
        urllib.request.urlopen(url, timeout=1)
        sys.exit(0)
    except OSError:
        try:
            os.kill(pid, 0)
        except OSError:
            print(f"cao-server (pid {pid}) exited during startup", file=sys.stderr)
            sys.exit(1)
        time.sleep(0.5)
sys.exit(1)
PY

    step "live: launching ${PROFILE_NAME} on claude_code (session ${LIVE_PREFIXED})"
    # --auto-approve, NOT --yolo: --yolo resolves allowed_tools to ["*"] and
    # strips the reviewer role's read-only policy, contradicting the profile.
    cao launch --agents "${PROFILE_NAME}" \
        --headless --async --auto-approve \
        --session-name "${LIVE_SESSION}" \
        --working-directory "${HERE}/fixture" \
        "Review sample_note.md using the python-review-checklist skill." >/dev/null

    step "live: poll the session to a terminal state"
    START="$(date +%s)"
    while true; do
        STATUS_JSON="$(cao session status "${LIVE_PREFIXED}" --json 2>/dev/null || true)"
        STATUS="$(printf '%s' "${STATUS_JSON}" | "${PYTHON_BIN}" -c '
import json
import sys

try:
    data = json.load(sys.stdin)
except Exception:
    print("unavailable")
    raise SystemExit
print((data.get("conductor") or {}).get("status") or "unavailable")
' || echo unavailable)"
        case "${STATUS}" in
            idle | completed)
                echo "[skills-example] live session reached ${STATUS}" >&2
                break
                ;;
            error)
                cao session status "${LIVE_PREFIXED}" --json || true
                fail "live session reached error"
                ;;
            waiting_user_answer)
                cao session status "${LIVE_PREFIXED}" --json || true
                fail "live session is waiting for input (headless cannot answer)"
                ;;
        esac
        NOW="$(date +%s)"
        [ "$((NOW - START))" -lt "${TIMEOUT}" ] \
            || fail "live session timed out after ${TIMEOUT}s (last status: ${STATUS:-unknown})"
        sleep "${POLL_INTERVAL}"
    done

    # The skill's output contract is the observable proof it engaged. Read the
    # FULL last response from the JSON — the text rendering truncates to the
    # first 20 lines, and the contract line is by definition the last one.
    LAST_OUTPUT="$(printf '%s' "${STATUS_JSON}" | "${PYTHON_BIN}" -c '
import json
import sys

try:
    data = json.load(sys.stdin)
except Exception:
    print("")
    raise SystemExit
print(((data.get("conductor") or {}).get("last_output")) or "")
' || true)"
    echo "${LAST_OUTPUT}" | grep -q "Checklist applied:" \
        || fail "live session output does not contain the skill's contract line"
fi

# --- Step 9: removal leaves the isolated catalog clean ------------------------
step "remove both skills and verify the isolated store is clean"
cao skills remove "${SKILL_NAME}" >/dev/null
cao skills remove "${DECOY_NAME}" >/dev/null
[ ! -e "${CAO_HOME_DIR}/skills/${SKILL_NAME}" ] \
    || fail "${SKILL_NAME} directory still present after removal"
[ ! -e "${CAO_HOME_DIR}/skills/${DECOY_NAME}" ] \
    || fail "${DECOY_NAME} directory still present after removal"
if [ "${LIVE}" -eq 0 ]; then
    cao skills list | grep -q "No skills found" || fail "skills still listed after removal"
else
    # A running cao-server seeds built-in skills into its isolated home. Only
    # our two example skills must disappear; those built-ins belong to CAO.
    if cao skills list | grep -E "${SKILL_NAME}|${DECOY_NAME}"; then
        fail "example skills still listed after removal"
    fi
fi

echo "[skills-example] OK: full skill lifecycle verified" >&2
