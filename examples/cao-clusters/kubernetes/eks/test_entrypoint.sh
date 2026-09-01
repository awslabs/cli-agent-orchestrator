#!/usr/bin/env bash
# Offline exercise of entrypoint.sh: state seeding, install skipping, and
# the three provider warm-up modes.
#
# Stubs `cao`, `claude`, `cao-server` and `timeout` on PATH so the whole script
# can run on a laptop with no image, no Bedrock, and no CAO install. The point is
# to catch what would otherwise only show up as a slow pod: a seed that is
# silently refused, an install that runs anyway, a warm-up that still blocks
# readiness after being told not to.
#
# NOT part of the CAO test suite - it exercises a shell script, and pytest has
# nothing to say about it. Run it directly:
#
#     examples/cao-clusters/kubernetes/eks/test_entrypoint.sh
#
# Prints a PASS/FAIL line per check and exits non-zero if any failed.
#
# The one thing it cannot check is the Dockerfile's half of the seed: that the
# `RUN cao init` layer sits ABOVE `VOLUME ["/home/cao/.cao"]`. Below it, Docker
# discards the writes and this harness would still pass - the tar would exist and
# be empty of anything useful. The Dockerfile guards that with its own
# `tar -tf | grep -q` at build time.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENTRY="${HERE}/entrypoint.sh"
[ -f "${ENTRY}" ] || { echo "cannot find ${ENTRY}"; exit 1; }

ROOT="$(mktemp -d "${TMPDIR:-/tmp}/cao-entrypoint-test.XXXXXX")"
trap 'rm -rf "${ROOT}"' EXIT
BIN="${ROOT}/bin"
mkdir -p "${BIN}" "${ROOT}/work"

# --- stubs -------------------------------------------------------------------
# `cao` writes the same shapes the real one does — a state dir for init, and a
# store file whose frontmatter records the RESOLVED provider for install, which
# is the line the entrypoint's skip logic matches on.
cat >"${BIN}/cao" <<'EOF'
#!/usr/bin/env bash
echo "STUB cao $*"
if [ "${1:-}" = "init" ]; then
  mkdir -p "$CAO_HOME_DIR/db" "$CAO_HOME_DIR/skills"
  : > "$CAO_HOME_DIR/db/cao.db"
elif [ "${1:-}" = "install" ]; then
  mkdir -p "$CAO_HOME_DIR/agent-store"
  prov="claude_code"; [ "${3:-}" = "--provider" ] && prov="${4:-}"
  printf -- '---\nname: %s\nprovider: %s\n---\nbody\n' "$2" "$prov" \
    > "$CAO_HOME_DIR/agent-store/$2.md"
fi
EOF
# Stands in for a Bedrock round trip, and reports whether it OVERLAPPED with
# another one.
#
# Deliberately not a wall-clock measurement. Timing this from the outside means
# comparing the run against a bound, and the bound has to absorb bash startup, a
# tar extract, and whatever the harness itself spends looking at a clock - on this
# machine that noise is the same order as the sleep, so an early version of this
# check failed on code that a trace proved was concurrent.
#
# Instead each invocation drops a marker, removes it on exit, and waits to see a
# sibling's. Run together, both see one. Run serially, neither does - the first
# has nothing to wait for and the second finds the first already gone.
cat >"${BIN}/claude" <<'EOF'
#!/usr/bin/env bash
mine="${WARM_TRACE_DIR}/pid.$$"
trap 'rm -f "${mine}"' EXIT
: > "${mine}"
for _ in $(seq 1 50); do
  if [ "$(ls "${WARM_TRACE_DIR}"/pid.* 2>/dev/null | wc -l | tr -d ' ')" -ge 2 ]; then
    : > "${WARM_TRACE_DIR}.overlap"
    break
  fi
  sleep 0.1
done
echo "ok"
EOF
# The real entrypoint ends in `exec cao-server`, so this stub returning is what
# ends the run.
cat >"${BIN}/cao-server" <<'EOF'
#!/usr/bin/env bash
echo "STUB cao-server $*"
EOF
# GNU coreutils `timeout` is absent on macOS; the entrypoint's use of it is not
# what is under test here.
cat >"${BIN}/timeout" <<'EOF'
#!/usr/bin/env bash
shift; exec "$@"
EOF
chmod +x "${BIN}"/*

export PATH="${BIN}:${PATH}"

# --- build a seed tar the way Dockerfile does --------------------------------
SEED_ROOT="${ROOT}/work/.cao/state"
mkdir -p "$(dirname "${SEED_ROOT}")"
CAO_HOME_DIR="${SEED_ROOT}" cao init >/dev/null
CAO_HOME_DIR="${SEED_ROOT}" cao install developer --provider claude_code >/dev/null
tar -cf "${ROOT}/seed.tar" -C "$(dirname "${SEED_ROOT}")" "$(basename "${SEED_ROOT}")"
rm -rf "${SEED_ROOT}"
echo "seed tar built: $(tar -tf "${ROOT}/seed.tar" | wc -l | tr -d ' ') entries"
echo

FAILS=()
check() {
  if [ "$2" = "1" ]; then
    echo "PASS $1"
  else
    echo "FAIL $1 -- $3"
    FAILS+=("$1")
  fi
}

export WARM_TRACE_DIR="${ROOT}/warm"

run() {  # fresh empty state dir, then one entrypoint run; env comes from the caller
  rm -rf "${ROOT}/work/.cao" "${WARM_TRACE_DIR}" "${WARM_TRACE_DIR}.overlap"
  mkdir -p "${ROOT}/work/.cao" "${WARM_TRACE_DIR}"
  OUT=$(bash "${ENTRY}" 2>&1)
}

# --- 1. the seed replaces init and install -----------------------------------
export CAO_STATE_SEED="${ROOT}/seed.tar" CAO_STATE_SEED_ROOT="${SEED_ROOT}"
export CAO_HOME_DIR="${SEED_ROOT}" CAO_INSTALL_PROFILES="developer:claude_code"
export CAO_WARM_PROVIDER=0
run
check "seed applied" \
  "$(grep -qc 'state seeded from' <<<"$OUT" && echo 1)" "$OUT"
check "cao init skipped" \
  "$(grep -q 'STUB cao init' <<<"$OUT" && echo 0 || echo 1)" "$OUT"
check "install skipped (seeded)" \
  "$(grep -qc 'already installed' <<<"$OUT" && echo 1)" "$OUT"
check "server still exec'd" \
  "$(grep -qc 'STUB cao-server' <<<"$OUT" && echo 1)" "$OUT"

# --- 2. a profile present but pinned to another provider must reinstall ------
# A delegation resolves the provider from the TARGET node's store, so a wrong pin
# fails by naming a CLI nobody asked for. Presence alone is not the question.
export CAO_INSTALL_PROFILES="developer:kiro_cli"
run
check "wrong-provider pin reinstalls" \
  "$(grep -qc 'STUB cao install developer --provider kiro_cli' <<<"$OUT" && echo 1)" "$OUT"

# --- 3. a CAO_HOME_DIR the seed was not built for is refused -----------------
export CAO_INSTALL_PROFILES="developer:claude_code"
export CAO_HOME_DIR="${ROOT}/work/.cao/elsewhere"
run
check "mismatched home refuses the seed" \
  "$(grep -q 'state seeded' <<<"$OUT" && echo 0 || echo 1)" "$OUT"
check "mismatched home runs cao init" \
  "$(grep -qc 'STUB cao init' <<<"$OUT" && echo 1)" "$OUT"
check "mismatched home installs" \
  "$(grep -qc 'STUB cao install developer' <<<"$OUT" && echo 1)" "$OUT"

# --- 4. a non-empty state dir is never overwritten ---------------------------
# The supervisor's state is a PVC that survives restarts; seeding over it would
# clobber real work. (A worker's emptyDir is always empty, so it always seeds.)
export CAO_HOME_DIR="${SEED_ROOT}"
rm -rf "${ROOT}/work/.cao"
mkdir -p "${SEED_ROOT}"
: > "${SEED_ROOT}/pre-existing"
OUT=$(bash "${ENTRY}" 2>&1)
check "non-empty state refuses the seed" \
  "$(grep -q 'state seeded' <<<"$OUT" && echo 0 || echo 1)" "$OUT"
check "pre-existing state preserved" \
  "$([ -f "${SEED_ROOT}/pre-existing" ] && echo 1)" "gone"

# --- 5. an image with no seed at all still works -----------------------------
unset CAO_STATE_SEED CAO_STATE_SEED_ROOT
run
check "absent seed falls through to init" \
  "$(grep -qc 'STUB cao init' <<<"$OUT" && echo 1)" "$OUT"

# --- 6. warm-up modes --------------------------------------------------------
export CAO_STATE_SEED="${ROOT}/seed.tar" CAO_STATE_SEED_ROOT="${SEED_ROOT}"
export CLAUDE_CODE_USE_BEDROCK=1
export ANTHROPIC_MODEL=model-opus ANTHROPIC_DEFAULT_HAIKU_MODEL=model-haiku

export CAO_WARM_PROVIDER=1
run
check "blocking mode warms both tiers" \
  "$([ "$(grep -c 'responded' <<<"$OUT")" = 2 ] && echo 1)" "$OUT"
# Serially these measured 3.6s + 4.6s of an ~18s pod readiness.
check "blocking mode warms tiers concurrently" \
  "$([ -f "${WARM_TRACE_DIR}.overlap" ] && echo 1)" "no overlap observed"
check "blocking mode warms BEFORE the server starts" \
  "$([ "$(grep -n 'responded' <<<"$OUT" | tail -1 | cut -d: -f1)" \
      -lt "$(grep -n 'STUB cao-server' <<<"$OUT" | cut -d: -f1)" ] && echo 1)" "$OUT"
# Each tier gets its own log file, so two concurrent CLIs cannot interleave into
# the line the WARN branch quotes back.
check "each tier's output is reported separately" \
  "$([ "$(grep -c 'ANTHROPIC_MODEL responded' <<<"$OUT")" = 1 ] \
     && [ "$(grep -c 'ANTHROPIC_DEFAULT_HAIKU_MODEL responded' <<<"$OUT")" = 1 ] && echo 1)" "$OUT"

export CAO_WARM_PROVIDER=background
run
check "background mode announces itself" \
  "$(grep -qc 'in the background' <<<"$OUT" && echo 1)" "$OUT"
# The whole point of item 2: readiness stops waiting on Bedrock.
check "background mode starts the server BEFORE warming" \
  "$([ "$(grep -n 'STUB cao-server' <<<"$OUT" | cut -d: -f1)" \
      -lt "$(grep -n 'responded' <<<"$OUT" | head -1 | cut -d: -f1)" ] && echo 1)" "$OUT"

export CAO_WARM_PROVIDER=0
run
check "mode 0 skips warming" \
  "$(grep -q 'warming' <<<"$OUT" && echo 0 || echo 1)" "$OUT"

# Nothing to warm without Bedrock: the preflight exists for marketplace model
# activation, which is a Bedrock-only concern.
export CAO_WARM_PROVIDER=1 CLAUDE_CODE_USE_BEDROCK=0
run
check "non-bedrock skips warming" \
  "$(grep -q 'warming' <<<"$OUT" && echo 0 || echo 1)" "$OUT"

echo
echo "FAILURES: ${FAILS[*]:-none}"
[ ${#FAILS[@]} -eq 0 ]
