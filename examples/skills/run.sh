#!/usr/bin/env bash
#
# End-to-end CAO skill lifecycle: install, discover, load, apply, update, remove.
#
# Default mode is fully deterministic and needs no cao-server, no provider
# credentials, and no network: it runs inside a temporary CAO_HOME_DIR, so the
# operator's installed skills and profiles are never touched. `--live` adds one
# real claude_code session on top and requires cao-server to be running with
# the claude_code CLI installed and authenticated (claude_code is the example's
# documented provider because its skill delivery is the runtime-prompt catalog
# this example demonstrates — see docs/skills.md, "How Skills Work by Provider").
#
# Usage:
#   ./run.sh            # deterministic lifecycle (default)
#   ./run.sh --live     # + one real claude_code session (needs cao-server)
#
# Exit codes:
#   0 — every step and assertion passed
#   1 — an assertion failed (the failing step is printed)
#
# Cleanup: the temporary home is removed on exit (trap); in --live mode the
# session is shut down first. The operator's real CAO home is never touched.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_NAME="python-review-checklist"
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

cleanup() {
    if [ "${LIVE}" -eq 1 ]; then
        cao shutdown --session "${LIVE_PREFIXED}" >/dev/null 2>&1 || true
    fi
    rm -rf "${CAO_TMP_HOME}"
}
trap cleanup EXIT INT TERM

# Resolve the interpreter that ships `cao`, so the fixture helpers import the
# same cli_agent_orchestrator package the CLI uses (uv venvs place `python`
# next to the entry-point shims).
CAO_BIN="$(command -v cao)" || {
    echo "[skills-example] FAIL: 'cao' is not on PATH" >&2
    exit 1
}
PYTHON_BIN="$(dirname "${CAO_BIN}")/python"
[ -x "${PYTHON_BIN}" ] || PYTHON_BIN="$(command -v python3)"

step() { echo "[skills-example] step: $1" >&2; }
fail() { echo "[skills-example] FAIL: $1" >&2; exit 1; }

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

# --- Step 4: the full Markdown body is retrievable ---------------------------
step "load_skill retrieval path returns the complete body"
BODY="$("${PYTHON_BIN}" "${HERE}/fixture/show_skill_body.py")" \
    || fail "show_skill_body.py could not load the installed skill"
echo "${BODY}" | grep -q "Checklist applied" \
    || fail "loaded body does not contain the skill's output contract"

# --- Step 5: a profile declares the skill; the scoped catalog contains it ----
step "profile ${PROFILE_NAME} scopes the runtime catalog to the skill"
cao install "${HERE}/profiles/${PROFILE_NAME}.md" >/dev/null
CATALOG="$("${PYTHON_BIN}" "${HERE}/fixture/show_scoped_catalog.py")" \
    || fail "show_scoped_catalog.py could not load the installed profile"
echo "${CATALOG}" | grep -q -- "- \*\*${SKILL_NAME}\*\*" \
    || fail "scoped catalog does not advertise the skill"

# --- Step 6: fixture task — observable result derived from the skill ---------
step "fixture task applies the skill guidance"
APPLY_OUTPUT="$("${PYTHON_BIN}" "${HERE}/fixture/apply_checklist.py")" \
    || fail "apply_checklist.py failed — see its error above"
echo "${APPLY_OUTPUT}" | grep -q "^Checklist applied: 4/4 items$" \
    || fail "fixture task did not report the checklist as applied"

# --- Step 7 (optional): one real provider session ----------------------------
if [ "${LIVE}" -eq 1 ]; then
    step "live: launching ${PROFILE_NAME} on claude_code (session ${LIVE_PREFIXED})"
    cao launch --agents "${PROFILE_NAME}" \
        --headless --async --yolo \
        --session-name "${LIVE_SESSION}" \
        --working-directory "${HERE}/fixture" \
        "Review sample_note.md using the python-review-checklist skill." >/dev/null

    START="$(date +%s)"
    while true; do
        STATUS_JSON="$(cao session status "${LIVE_PREFIXED}" --workers 2>/dev/null || echo "")"
        STATUS="$(echo "${STATUS_JSON}" \
            | grep -oE '"status":[[:space:]]*"[A-Z_]+"' | head -1 | sed -E 's/.*"([A-Z_]+)"/\1/')"
        case "${STATUS}" in
            IDLE | COMPLETED)
                echo "[skills-example] live session reached ${STATUS}" >&2
                break
                ;;
            ERROR)
                cao session status "${LIVE_PREFIXED}" --workers || true
                fail "live session reached ERROR"
                ;;
            WAITING_USER_ANSWER)
                cao session status "${LIVE_PREFIXED}" --workers || true
                fail "live session is waiting for input (headless cannot answer)"
                ;;
        esac
        NOW="$(date +%s)"
        [ "$((NOW - START))" -lt "${TIMEOUT}" ] \
            || fail "live session timed out after ${TIMEOUT}s (last status: ${STATUS:-unknown})"
        sleep "${POLL_INTERVAL}"
    done

    # The skill's output contract is the observable proof it engaged.
    cao session status "${LIVE_PREFIXED}" --workers | grep -q "Checklist applied:" \
        || fail "live session output does not contain the skill's contract line"
fi

# --- Step 8: removal leaves the isolated catalog clean ------------------------
step "remove ${SKILL_NAME} and verify the isolated store is clean"
cao skills remove "${SKILL_NAME}" >/dev/null
cao skills list | grep -q "No skills found" || fail "skill still listed after removal"
[ ! -e "${CAO_HOME_DIR}/skills/${SKILL_NAME}" ] \
    || fail "skill directory still present after removal"

echo "[skills-example] OK: full skill lifecycle verified" >&2
