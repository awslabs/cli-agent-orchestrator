# Phase 4 — End-to-End Validation and Documentation

## Summary

Added the OpenCode CLI e2e test class (with `require_opencode` fixture), created provider documentation, updated the README provider table, and added a CHANGELOG entry. All unit tests (1434) continue to pass.

---

## Files Created

| File | Purpose |
|------|---------|
| `docs/opencode-cli.md` | Provider documentation (prerequisites, quick start, config isolation, permission/tool mapping, MCP wiring, known limitations, troubleshooting) |
| `development_reports/phase_4_opencode_e2e_and_docs.md` | This report |

## Files Modified

| File | Change |
|------|--------|
| `test/e2e/conftest.py` | Added `require_opencode` fixture (skips if `opencode` not on PATH) |
| `test/e2e/test_assign.py` | Added `TestOpenCodeCliAssign` class with three test methods |
| `README.md` | Added `opencode_cli` row to provider table; added `opencode_cli` to `cao launch` examples |
| `CHANGELOG.md` | Added Unreleased entry announcing OpenCode CLI provider |

---

## Acceptance Criteria Disposition

### AC-1 — E2E test passes: `uv run pytest -m e2e test/e2e/test_assign.py -k opencode`

**Satisfied — all 3 tests passed.**

The dev server was started on port 9888 (`CAO_API_PORT=9888`) leaving the old installed server on port 9889 untouched. Three agent profiles were installed with `--auto-approve` before the run.

```
test/e2e/test_assign.py::TestOpenCodeCliAssign::test_assign_data_analyst     PASSED
test/e2e/test_assign.py::TestOpenCodeCliAssign::test_assign_report_generator PASSED
test/e2e/test_assign.py::TestOpenCodeCliAssign::test_assign_with_callback    PASSED
```

`test_assign_data_analyst` passed in the first run. `test_assign_report_generator` and `test_assign_with_callback` passed together in 2 minutes 4 seconds (`2 passed in 124.53s`).

**Phase 3 regression discovered and fixed (commit 4c30661):**

The live e2e revealed that `cao install --provider opencode_cli` was writing OpenCode's `opencode.json` with the raw CAO MCP server format (`type: "stdio"`, `command` as string, `args` as separate list) instead of OpenCode's format (`type: "local"`, `command` as a combined list, `enabled: true`). OpenCode rejected the config with: `Configuration is invalid: Invalid input mcp.cao-mcp-server`.

Fix: `translate_mcp_server_config()` added to `utils/opencode_config.py`; the opencode_cli install branch now calls it before `upsert_mcp_server()`. Six unit tests added. The opencode.json was regenerated with the correct format after the fix.

### AC-2 — `docs/opencode-cli.md` exists with required sections

**Satisfied.** Document includes:
- Prerequisites (opencode binary, Node.js, first-launch npm install side effect, 5–30s delay)
- Launch examples (basic, `--auto-approve`, `--model`, `--yolo`, HTTP API)
- Permission/tool mapping reference (summary table + pointer to §9 of design doc)
- Known limitations: §10.3 project-local `opencode.json` override, concurrent-write race
- Troubleshooting: first-launch blank TUI, stale installed server, auth errors, permission prompt, PROCESSING stuck

### AC-3 — `README.md` provider table contains `opencode_cli` row

**Satisfied.** Row added after GitHub Copilot CLI:
```
| **OpenCode CLI** | [Provider docs](docs/opencode-cli.md) · [Installation](https://opencode.ai) | Per-model API key |
```
Also added `cao launch --agents code_supervisor --provider opencode_cli` to the launch examples section.

### AC-4 — `CHANGELOG.md` Unreleased section has the new entry

**Satisfied.** Entry added under `## [Unreleased] → ### Added`, following the style of the Copilot CLI / Gemini CLI entries in `## [2.0.0]`. References both `docs/opencode-cli.md` and `docs/feat-opencode-provider-design.md`.

---

## Test Run Summary

Unit tests (post-regression fix):
```
uv run pytest test/ --ignore=test/e2e -q
1440 passed, 16 skipped, 4 warnings in 44.13s
```

E2E tests (`CAO_API_PORT=9888 uv run pytest -m e2e test/e2e/test_assign.py -k opencode -v`):
```
test_assign_data_analyst     PASSED
test_assign_report_generator PASSED  (124.53s combined with test below)
test_assign_with_callback    PASSED
```

No regressions from Phase 4 changes; one Phase 3 regression found and fixed (see AC-1 above).
