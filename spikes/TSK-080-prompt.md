# TSK-080 — Phase 2 Task 9 follow-up: migrate remaining terminal_service test files

You are executing a focused follow-up to Phase 2 Task 9 of PRJ-042 (aws-cao WezTerm port). Self-contained — no prior context.

## Repo state
- Working dir: `C:\dev\aws-cao`, branch `wezterm-multiplexer`.
- Tasks 7 + 8 just committed. Task 9's source change to `terminal_service.py` is in the working tree (uncommitted) along with `test/services/test_terminal_service.py` (already migrated).
- The Task 9 source change replaced `from cli_agent_orchestrator.clients.tmux import tmux_client` with `from cli_agent_orchestrator.multiplexers import get_multiplexer` and migrated 14 call sites from `tmux_client.<m>(...)` to `get_multiplexer().<m>(...)`. The module no longer exposes a `tmux_client` attribute.

## The bug

These three test files still patch the old `terminal_service.tmux_client` attribute that no longer exists:

- `test/services/test_terminal_service_full.py` — 22 failures
- `test/services/test_terminal_service_coverage.py` — 10 failures
- `test/services/test_plugin_event_emission.py` — 8 failures

Each fails with `AttributeError: <module 'cli_agent_orchestrator.services.terminal_service' ...> does not have the attribute 'tmux_client'`.

## Goal

Migrate all three test files to mock the multiplexer accessor seam — the same pattern Task 9 already applied in `test/services/test_terminal_service.py`. Read that file first to learn the seam.

## Implementation

In each of the three failing files:

1. Find every `@patch("cli_agent_orchestrator.services.terminal_service.tmux_client", ...)` decorator and replace with patches against the accessor:
   ```python
   @patch("cli_agent_orchestrator.services.terminal_service.get_multiplexer")
   def test_x(self, mock_get_multiplexer, ...):
       mock_multiplexer = mock_get_multiplexer.return_value
       mock_multiplexer.<method>.return_value = ...
   ```
2. Find every `monkeypatch.setattr(terminal_service, "tmux_client", ...)` style and switch to `monkeypatch.setattr(terminal_service, "get_multiplexer", lambda: <mock>)`.
3. Find every direct `terminal_service.tmux_client.<method>` reference in test bodies (assertions like `mock_tmux_client.send_keys.assert_called_once_with(...)`) and replace with assertions against `mock_multiplexer.<method>`.
4. If `lru_cache` causes test pollution between cases, call `terminal_service.get_multiplexer.cache_clear()` in fixtures (autouse). The accessor is cached per-process via `lru_cache(maxsize=1)`.

DO NOT change any test assertion's behavioral expectation — only the seam through which the mock is wired. The point is to keep the same coverage with the new import path.

DO NOT modify `terminal_service.py` source — that's Task 9's done work.
DO NOT modify `test_terminal_service.py` — already migrated by Task 9.
DO NOT modify any other file.

## Verification

```
.venv/Scripts/python.exe -m pytest test/services/test_terminal_service_full.py test/services/test_terminal_service_coverage.py test/services/test_plugin_event_emission.py test/services/test_terminal_service.py -v --tb=short
.venv/Scripts/python.exe -m pytest test/clients/ test/multiplexers/ test/providers/ test/services/ test/utils/ --ignore=test/e2e -q --tb=no --no-header
```

The second run failure count must return to **43** (the project baseline). Anything above 43 is a regression to investigate.

## Reporting

Write `spikes/TSK-080-result.md`:

```markdown
# TSK-080 — Task 9 follow-up: terminal_service test migration

## Files touched
<list>

## Migration summary
- <file>: N decorator patches + M setattr + K assertion references migrated.

## Tests
- targeted (4 services tests): <N pass / M fail>
- full (excl. e2e): <N pass / M fail> — must equal 43
```

Echo: `TSK-080: PASS|FAIL — <reason>`.

DO NOT commit. Stop after the migration.

Begin.
