#!/usr/bin/env bash
# Local mirror of the `security` and `codeql` CI jobs so contributors can catch
# SSRF/path-injection/SCA findings before pushing. Exits non-zero on any
# scanner failure so it's safe to wire into pre-push hooks or Makefile targets.
#
# Usage:
#   scripts/security-scan.sh                 # run all available scanners
#   scripts/security-scan.sh trivy           # just Trivy
#   scripts/security-scan.sh codeql          # just CodeQL (python)
#
# CodeQL installs from Homebrew (macOS) are often broken by Apple's Gatekeeper
# quarantine (xattr errors followed by silent exit 1). If that's happening,
# either run CodeQL via Docker (see below) or rely on the GitHub Action.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

target="${1:-all}"
exit_code=0

run_trivy() {
    echo "==> Trivy filesystem scan (CRITICAL,HIGH; unfixed ignored, matching CI)"
    if ! command -v trivy >/dev/null 2>&1; then
        echo "  SKIP: trivy not on PATH (brew install aquasecurity/trivy/trivy)"
        return 0
    fi
    uv export --format requirements-txt > requirements.txt
    trivy fs \
        --severity CRITICAL,HIGH \
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

case "$target" in
    trivy)  run_trivy ;;
    codeql) run_codeql ;;
    all)    run_trivy; run_codeql ;;
    *)      echo "Unknown target: $target (use trivy|codeql|all)"; exit 2 ;;
esac

exit "$exit_code"
