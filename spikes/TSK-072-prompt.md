# TSK-072 — Phase 2 Task 2: TmuxClient → TmuxMultiplexer (mechanical refactor)

You are executing Phase 2 Task 2 of PRJ-042 (aws-cao WezTerm port). This is a **mechanical refactor** with full repo write access. The supervising Opus session has decomposed the work — your job is just Task 2, end to end, ready for review.

## Repo state

- Working dir: `C:\dev\aws-cao`
- Branch: `wezterm-multiplexer` (already checked out, clean tree)
- Plan (binding spec): `docs/PLAN-phase2.md` — read §1, §2, §3 first.
- **Task 1 just landed** (commit on this branch): `src/cli_agent_orchestrator/multiplexers/base.py` now defines `BaseMultiplexer` ABC and `LaunchSpec` dataclass. Read it before you start.
- The file you are moving: `src/cli_agent_orchestrator/clients/tmux.py` (existing TmuxClient implementation — ~430 lines).
- Existing tests that MUST stay green: `test/clients/test_tmux_client.py`, `test/clients/test_tmux_send_keys.py`, `test/providers/test_tmux_working_directory.py`, plus every other test that imports `tmux_client` (search them).

## Goal

Move TmuxClient into the multiplexers package as a `BaseMultiplexer` subclass without changing any external behavior. Existing call sites (`from cli_agent_orchestrator.clients.tmux import tmux_client`) keep working via a re-export shim.

## Scope (HARD — do not exceed)

- DO NOT modify any provider, service, or test file beyond updating imports if absolutely required (cross-check first).
- DO NOT change `send_special_key`'s call sites or signature beyond what the abstract method already declares (Task 3 handles the `literal=True` rollout).
- DO NOT add WezTerm code (Task 5).
- DO NOT add a backend selection helper (Task 4).
- DO NOT touch `_resolve_and_validate_working_directory` — it now lives on `BaseMultiplexer`. Remove it from the moved class body and verify the inherited version is used. Existing tmux tests for it must pass against the inherited helper.
- DO NOT install or upgrade dependencies.
- DO NOT commit or push. Produce a clean working-tree change for the supervising Opus to review and commit.
- Use `rtk` prefix for any git inspection commands (`rtk git diff`, `rtk git status`).

## What to do

### Step 1 — read Task 1 output
1. Read `src/cli_agent_orchestrator/multiplexers/base.py` end to end. Note the abstract method set and the default `send_keys()` implementation that calls `_paste_text` then `_submit_input`.
2. Read `src/cli_agent_orchestrator/clients/tmux.py` end to end. Locate the existing `send_keys()` body (around lines 198–251 per plan §3) and the `_resolve_and_validate_working_directory` helper (around lines 40–115).

### Step 2 — create the new module
Create `src/cli_agent_orchestrator/multiplexers/tmux.py`:
- `class TmuxMultiplexer(BaseMultiplexer)` with all the abstract methods implemented.
- Move the implementation bodies from the old `TmuxClient`. Names should match the abstract method names from the base class (`create_session`, `create_window`, `_paste_text`, `_submit_input`, `send_special_key`, `get_history`, `list_sessions`, `kill_session`, `kill_window`, `session_exists`, `get_pane_working_directory`, `pipe_pane`, `stop_pipe_pane`).
- **Split the existing `send_keys()` body** into `_paste_text(session, window, text)` (the paste-buffer / paste-buffer -p / temp-file portion) and `_submit_input(session, window, enter_count=1)` (the Enter submission portion, including the inter-Enter delay loop). The base class's default `send_keys()` will recompose them — DO NOT override `send_keys()` on the subclass; let the inherited default do the work. This is the load-bearing change of Task 2 — verify with the existing tmux send_keys tests.
- DO NOT redefine `_resolve_and_validate_working_directory`. Inherit it. Confirm with `python -c "from cli_agent_orchestrator.multiplexers.tmux import TmuxMultiplexer; m = TmuxMultiplexer(); print(m._resolve_and_validate_working_directory.__qualname__)"` — should report `BaseMultiplexer._resolve_and_validate_working_directory`.
- Update `src/cli_agent_orchestrator/multiplexers/__init__.py` to export `TmuxMultiplexer` alongside the existing `BaseMultiplexer`/`LaunchSpec` exports.
- Keep `send_special_key` signature exactly as the base abstract declares: `send_special_key(self, session_name: str, window_name: str, key: str, *, literal: bool = False) -> None`. The current TmuxClient may not have the `literal` keyword — add the keyword and **make it a no-op wired to existing behavior for now** (the actual `literal=True` Unix-bypass routing is Task 3's job). Specifically: when `literal=True`, send the key as-is via `tmux send-keys -l`. When `literal=False` (default), preserve the current branch.

### Step 3 — make `clients/tmux.py` a shim
Replace the entire body of `src/cli_agent_orchestrator/clients/tmux.py` with:

```python
"""Deprecated re-export shim for the legacy TmuxClient location.

The real implementation now lives in
``cli_agent_orchestrator.multiplexers.tmux``. This shim keeps existing
imports working until Task 4 wires the runtime backend selector.
"""
from cli_agent_orchestrator.multiplexers.tmux import TmuxMultiplexer

# Singleton kept for backwards compatibility with module-level imports.
tmux_client = TmuxMultiplexer()

__all__ = ["TmuxMultiplexer", "tmux_client"]
```

If the original file exposed any other module-level symbols (e.g., constants, helper functions used elsewhere), re-export them from the shim too — grep the project for `from cli_agent_orchestrator.clients.tmux import` to find them.

### Step 4 — verify

1. Run **only** the tmux-related tests first to bisect quickly:
   ```
   rtk pytest test/clients/test_tmux_client.py test/clients/test_tmux_send_keys.py test/providers/test_tmux_working_directory.py -x
   ```
   All must pass. If any fails, stop and report which test, the assertion, and your hypothesis. Do not paper over with `pytest -k` or skips.

2. Then run the full provider suite to catch any indirect breakage:
   ```
   rtk pytest test/providers/ test/services/ -x
   ```
   These should all still pass.

3. Then run the full suite excluding pre-existing platform-incompatible failures (Task 1 reported 43 pre-existing failures unrelated to multiplexers — match that count or better):
   ```
   rtk pytest test/ --ignore=test/e2e -x
   ```

### Step 5 — clone the most representative tmux tests into multiplexers/

Create `test/multiplexers/test_tmux_multiplexer.py` (note: package init `test/multiplexers/__init__.py` already exists from Task 1):

- Pick the **3 most representative** tests from each of `test/clients/test_tmux_client.py` and `test/clients/test_tmux_send_keys.py` (so ~6 cloned cases total).
- Update them to import from the new path (`from cli_agent_orchestrator.multiplexers.tmux import TmuxMultiplexer`).
- These cloned tests are smoke coverage at the new home; the original tests STAY where they are and still run against the import shim — that's the regression bar.
- DO NOT delete or relocate the original test files.

## Reporting

When done, write `spikes/TSK-072-result.md` with:

```markdown
# TSK-072 — Task 2 result

## Files touched
<list>

## Tests
- tmux suite: <N passed / M failed>
- provider+service: <N passed / M failed>
- full (excl. e2e): <N passed / M failed>
- multiplexers: <N passed / M failed>

## Send-keys split verification
Confirm: send_keys is NOT overridden on TmuxMultiplexer; inherited default works against the unchanged tmux tests.

## Working-directory inheritance verification
Confirm: TmuxMultiplexer does NOT define _resolve_and_validate_working_directory; inherited from BaseMultiplexer; existing tests pass.

## Deviations
<any departure from this prompt>

## Follow-ups
<anything you punted on>
```

Echo a one-line verdict to stdout: `TSK-072: PASS|FAIL — <reason>`.

## Order

1. Read base.py + tmux.py.
2. Create `multiplexers/tmux.py`.
3. Replace `clients/tmux.py` with shim.
4. Run tmux test suite first, then full suite.
5. Clone 6 representative tests into `test/multiplexers/test_tmux_multiplexer.py`.
6. Write `spikes/TSK-072-result.md`.
7. Echo verdict.

DO NOT commit. Stop after Task 2.

Begin.
