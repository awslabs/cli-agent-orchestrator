# PR #39 Review Fixes (Codex CLI Provider)

PR: https://github.com/awslabs/cli-agent-orchestrator/pull/39

This document records **all review feedback** found on PR #39 and how each item was addressed.

## Review Feedback → Fixes

### 1) Avoid unrelated changes / formatting noise

**Feedback (tuanknguyen):**
- “are these changes related to Codex CLI?” (inline on `src/cli_agent_orchestrator/api/main.py`)
- “appreciate all the formatting changes, but could we have a separate PR for them?” (inline on `test/api/test_inbox_messages.py`)

**Fix:**
- Removed non-Codex changes from PR #39 by restoring the following files to match `upstream/main`:
  - `src/cli_agent_orchestrator/api/main.py`
  - `test/api/test_inbox_messages.py`
- Also restored unrelated changes in existing providers/tests (not required for Codex support):
  - `src/cli_agent_orchestrator/providers/q_cli.py`
  - `src/cli_agent_orchestrator/providers/kiro_cli.py`
  - `test/providers/test_q_cli_unit.py`
  - `test/providers/test_kiro_cli_unit.py`
- Formatting-only fixes were handled separately (per the thread) and are not included in this PR.

### 2) `status` vs `message_status` parameter naming

**Feedback (tuanknguyen):**
- “this changes the param name from `status` to `message_status` but the model below still uses `status`. Could we standardize?”

**Fix:**
- This was part of the unrelated inbox endpoint changes; the entire change was removed from PR #39 by restoring `src/cli_agent_orchestrator/api/main.py` to `upstream/main`.

### 3) Add Codex CLI documentation + README intro

**Feedback (haofeif):**
- Add a `/docs` section for Codex CLI.
- Update root `README.md` with a brief intro and link to the new docs.

**Fix:**
- Added dedicated documentation: `docs/codex-cli.md`.
- Added/updated a README section: `README.md` → “Codex CLI Provider” linking to `docs/codex-cli.md`.

### 4) Add step-by-step examples (similar to `examples/assign`)

**Feedback (haofeif):**
- Add an `/examples` section for Codex CLI, following the style of `examples/assign` so users can reproduce the workflow.

**Fix:**
- Added `examples/codex-basic/` with:
  - `examples/codex-basic/README.md` (step-by-step)
  - `examples/codex-basic/codex_developer.md`
  - `examples/codex-basic/codex_reviewer.md`
  - `examples/codex-basic/codex_documenter.md`

### 5) Documentation correctness (commands)

**Issue found during verification:**
- The CLI does not provide `cao create`, `cao send`, or `cao get-output` subcommands.

**Fix:**
- Updated `README.md` Codex quickstart to use actual commands:
  - `cao-server`
  - `cao install ... --provider codex`
  - `cao launch --agents ... --provider codex`
  - Optional HTTP API calls for sending input and fetching output.

## Verification

Local checks to run:

```bash
uv run black --check src/ test/
uv run isort --check-only src/ test/
uv run mypy src/
uv run pytest -v
```

CI status is expected to be green after pushing updates to the PR head branch.
