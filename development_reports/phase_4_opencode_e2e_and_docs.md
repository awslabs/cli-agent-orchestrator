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

**Blocked — cannot execute.** Explanation:

- `opencode` binary: **FOUND** at `/usr/local/bin/opencode`
- `require_opencode` fixture: would **not** skip (binary is present)
- `require_cao_server` fixture: would **skip** — the old installed `cao-server` binary (PID 55944, `/home/bajablast69/.local/bin/cao-server`) is running on port 9889 but predates Phase 3 and does not know the `opencode_cli` provider type
- Per user instruction: cannot start a new server on port 9889 in this session

The e2e test is structurally complete and follows the exact same pattern as all other provider test classes in `TestKiroCliAssign`, `TestCopilotCliAssign`, etc. The `test_assign_with_callback` method covers all four orchestration modes as required by the acceptance criteria.

**Phase 3 regression check:** No regression was discovered during Phase 4 code authoring. The provider dispatch (`manager.py`), launch command composition (`opencode_cli.py::_build_launch_command`), and env propagation were re-verified via direct Python invocation in Phase 3 review. The e2e run remains the authoritative check per the reviewer's contingent ruling — it should be executed when the port constraint is lifted.

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

```
uv run pytest test/ --ignore=test/e2e -q
1434 passed, 16 skipped, 4 warnings in 43.78s
```

No regressions introduced by Phase 4 changes.

---

## E2E Port Constraint — Recommended Unblock Path

To run the e2e test in a future session:

```bash
# Kill the old installed binary
pkill -f '/home/bajablast69/.local/bin/cao-server'

# Start the dev server
uv run cao-server &

# Install profiles for opencode_cli
uv run cao install examples/assign/data_analyst.md --provider opencode_cli --auto-approve
uv run cao install examples/assign/report_generator.md --provider opencode_cli --auto-approve
uv run cao install developer --provider opencode_cli --auto-approve

# Run e2e
uv run pytest -m e2e test/e2e/test_assign.py -k opencode -v
```

If the e2e reveals a Phase 3 launch-path regression, it is fixed before Phase 4 closes per the reviewer's contingent ruling.
