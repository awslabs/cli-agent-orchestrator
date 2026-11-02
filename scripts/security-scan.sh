#!/usr/bin/env bash
# Local mirror of the `dependency-security`, `security`, and `codeql` CI jobs.
# Contributors can catch
# SSRF/path-injection/SCA/secret findings before pushing. Exits non-zero on any
# scanner failure so it's safe to wire into pre-push hooks or Makefile targets.
#
# Usage:
#   scripts/security-scan.sh                 # full dependency gate, then available other scanners
#   scripts/security-scan.sh trivy           # just Trivy
#   scripts/security-scan.sh dependencies    # full locked dependency gate
#   scripts/security-scan.sh codeql          # just CodeQL (python)
#   scripts/security-scan.sh gitleaks        # just gitleaks (secret scan, #457)
#
# CodeQL installs from Homebrew (macOS) are often broken by Apple's Gatekeeper
# quarantine (xattr errors followed by silent exit 1). If that's happening,
# either run CodeQL via Docker (see below) or rely on the GitHub Action.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

target="${1:-all}"
exit_code=0

# Mirrors the CI `security` job's two gates (#568 OQ-1 / DR-5,
# docs/issues/568-js-yaml-omap-dos/design.md; see SECURITY.md, "Running
# security scans locally"):
#   1. secrets at EVERY severity: `--scanners secret`, no --severity. Trivy's
#      --severity filter applies to every scanner in a run, so folding secrets
#      into run 2 would stop gating MEDIUM/LOW secrets;
#   2. fixable CRITICAL, HIGH and UNKNOWN (not yet scored) vulnerabilities, in
#      table format, which honours --severity (only CI's SARIF report step has
#      it unset by trivy-action, and that step never gates).
# Fixable MEDIUM/LOW vulnerabilities are report-only in CI. Don't add
# --severity to run 1, and don't remove run 1: either makes this wrapper
# weaker than CI.
run_trivy() {
    echo "==> Trivy filesystem scan (secrets: all severities; vulns: fixable CRITICAL,HIGH,UNKNOWN; matching the CI gates)"
    if ! command -v trivy >/dev/null 2>&1; then
        echo "  SKIP: trivy not on PATH (brew install aquasecurity/trivy/trivy)"
        return 0
    fi
    # `--frozen` is load-bearing: a plain `uv export` REWRITES the tracked
    # uv.lock as a side effect (it reverts the cli-agent-orchestrator version to
    # the last locked one), leaving a dirty tree that a contributor running this
    # script before committing can easily sweep into an unrelated commit. With
    # --frozen the export reads the lockfile and never rewrites it (#568).
    uv export --frozen --format requirements-txt > requirements.txt
    trivy fs \
        --scanners secret \
        --exit-code 1 \
        . || exit_code=1
    trivy fs \
        --severity CRITICAL,HIGH,UNKNOWN \
        --ignore-unfixed \
        --exit-code 1 \
        . || exit_code=1
    rm -f requirements.txt
}

run_codeql() {
    echo "==> CodeQL (python, security-and-quality)"
    if ! command -v codeql >/dev/null 2>&1; then
        echo "  SKIP: codeql not on PATH"
        echo "  Install: https://github.com/github/codeql-cli-binaries/releases"
        return 0
    fi

    local db_dir="${CODEQL_DB:-./.codeql-db}"
    local sarif_out="${CODEQL_SARIF:-./codeql-results.sarif}"

    echo "  Building database at $db_dir (may take a minute)"
    # If the CLI is quarantined (common on macOS Homebrew), this exits 1 with
    # only xattr errors on stderr. We surface a hint in that case.
    if ! codeql database create "$db_dir" \
            --language=python \
            --source-root="$ROOT_DIR" \
            --overwrite >/tmp/codeql.log 2>&1; then
        if grep -q "xattr:" /tmp/codeql.log; then
            echo "  ERROR: CodeQL CLI appears to be under Gatekeeper quarantine."
            echo "  Fix (macOS): sudo xattr -dr com.apple.quarantine \$(brew --prefix codeql)"
            echo "  Or run via Docker: docker run --rm -v \$PWD:/src ghcr.io/github/codeql-action/codeql"
        fi
        grep -v "xattr:" /tmp/codeql.log | tail -20
        exit_code=1
        return 0
    fi

    echo "  Analyzing with security-and-quality suite"
    codeql database analyze "$db_dir" \
        codeql/python-queries:codeql-suites/python-security-and-quality.qls \
        --format=sarif-latest \
        --output="$sarif_out" \
        --download \
        2>&1 | grep -vE "^xattr:" || exit_code=1

    # Fail the run if the SARIF contains any result at error/warning level.
    if command -v jq >/dev/null 2>&1; then
        local count
        count=$(jq '[.runs[].results[]? | select(.level=="error" or .level=="warning")] | length' \
                "$sarif_out")
        if [[ "$count" -gt 0 ]]; then
            echo "  FOUND $count error/warning-level CodeQL results in $sarif_out"
            exit_code=1
        fi
    fi
}

# CI pins this gitleaks version; the config's custom rules and the built-in
# ruleset are validated against it. Older local builds may behave differently
# (e.g. the private-key rule's key-length threshold changed), so warn on a
# mismatch rather than trusting whatever is installed.
GITLEAKS_EXPECTED_VERSION="8.30.1"

run_gitleaks() {
    echo "==> gitleaks secret scan (git history; config .gitleaks.toml)"
    if ! command -v gitleaks >/dev/null 2>&1; then
        echo "  SKIP: gitleaks not on PATH (brew install gitleaks, or see"
        echo "        https://github.com/gitleaks/gitleaks/releases)"
        return 0
    fi
    local have
    have="$(gitleaks version 2>/dev/null | tr -d '[:space:]')"
    if [[ "$have" != "$GITLEAKS_EXPECTED_VERSION" ]]; then
        echo "  NOTE: local gitleaks $have != CI-pinned $GITLEAKS_EXPECTED_VERSION;"
        echo "        results may differ from CI. Match the pinned version for parity."
    fi
    gitleaks detect \
        --source "$ROOT_DIR" \
        --config "$ROOT_DIR/.gitleaks.toml" \
        --redact \
        --verbose \
        --exit-code 1 || exit_code=1
}

case "$target" in
    dependencies) python3 scripts/dependency_security.py || exit_code=1 ;;
    trivy)    run_trivy ;;
    codeql)   run_codeql ;;
    gitleaks) run_gitleaks ;;
    all)      python3 scripts/dependency_security.py || exit_code=1
              run_trivy; run_codeql; run_gitleaks ;;
    *)        echo "Unknown target: $target (use dependencies|trivy|codeql|gitleaks|all)"; exit 2 ;;
esac

exit "$exit_code"
