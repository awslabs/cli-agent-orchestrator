# TSK-079 — Phase 2 Task 9: thread LaunchSpec + multiplexer accessor through terminal_service

You are executing Phase 2 Task 9 of PRJ-042 (aws-cao WezTerm port). Self-contained prompt — no prior context.

## Repo state
- Working dir: `C:\dev\aws-cao`, branch `wezterm-multiplexer` (clean tree at task start; other parallel tasks may modify disjoint files concurrently — DO NOT TOUCH `src/cli_agent_orchestrator/multiplexers/wezterm.py`, `src/cli_agent_orchestrator/providers/codex.py`, or their tests).
- Tasks 1–6 + audit committed. Task 4 added `get_multiplexer()` accessor to `cli_agent_orchestrator.multiplexers`.
- Plan binding spec: `docs/PLAN-phase2.md` §2 (LaunchSpec on create_session/create_window), §1.
- Audit reference: `spikes/TSK-071-result.md` HIDDEN-LEAKAGE entry for `services/terminal_service.py:32, 129-140, 188, 278-312, 364, 395-447, 453`.

## Goal

1. Migrate `terminal_service.py` from the direct `tmux_client` singleton import to the `get_multiplexer()` accessor, so the service uses whichever backend selection picks at runtime. Service stays backend-neutral — it does NOT resolve binaries or build argv.
2. Accept optional `LaunchSpec` on `create_session` / `create_window` and forward to the multiplexer.
3. Update tests to exercise both the new accessor seam and the LaunchSpec pass-through.

## Implementation requirements

### `src/cli_agent_orchestrator/services/terminal_service.py`

1. Replace `from cli_agent_orchestrator.clients.tmux import tmux_client` with `from cli_agent_orchestrator.multiplexers import get_multiplexer` plus `from cli_agent_orchestrator.multiplexers.base import LaunchSpec`.
2. Replace every `tmux_client.<method>(...)` with `get_multiplexer().<method>(...)`. The `lru_cache` on `get_multiplexer` makes repeated calls a constant-time singleton lookup — no need to memoize at module load.
3. Add an optional `launch_spec: LaunchSpec | None = None` parameter to `create_session` and `create_window`. Forward it verbatim to the multiplexer (which Task 5 already accepts on `WezTermMultiplexer`; Task 2 made tmux a no-op consumer).
4. DO NOT change any other public function signature beyond the additive `launch_spec` parameter.
5. DO NOT update any caller in this commit — Task 8 / future tasks will pass `launch_spec` from the provider side. The default `None` keeps every existing caller working.

### `test/services/test_terminal_service.py`

Update the suite:

1. Replace any `mock_tmux_client = ...` fixtures with mocks of the multiplexer accessor: `monkeypatch.setattr("cli_agent_orchestrator.services.terminal_service.get_multiplexer", lambda: mock_multiplexer)`.
2. Confirm existing tests still pass against the accessor seam (no behavioral change beyond the import path).
3. Add new tests:
   - `create_session(... launch_spec=spec)` forwards the spec to `multiplexer.create_session(... launch_spec=spec)`.
   - `create_window(... launch_spec=spec)` forwards the spec.
   - `create_session()` with no `launch_spec` forwards `launch_spec=None` (default preserved).

## Constraints (HARD)

- DO NOT touch `src/cli_agent_orchestrator/multiplexers/wezterm.py` or its test (Task 7 concurrent).
- DO NOT touch `src/cli_agent_orchestrator/providers/codex.py` or its test (Task 8 concurrent).
- DO NOT migrate any other file in `src/` from `tmux_client` to `get_multiplexer()` — that's the explicit Task 14 follow-up. Only `terminal_service.py` in this commit.
- DO NOT change any test file outside `test/services/test_terminal_service.py`.
- DO NOT install dependencies.
- Use `.venv\Scripts\python.exe -m pytest`.
- DO NOT commit.

## Verification (TARGETED ONLY — supervising Opus runs combined regression)

```
.venv/Scripts/python.exe -m pytest test/services/test_terminal_service.py test/services/test_inbox_service.py test/multiplexers/ -v --tb=short
```

Do NOT run the broad `test/clients/ test/multiplexers/ test/providers/ test/services/ test/utils/` suite — three Codex tasks are running in parallel and that race will produce false regressions.

## Reporting

Write `spikes/TSK-079-result.md`:

```markdown
# TSK-079 — Task 9 result

## Files touched
<list>

## Migration summary
- Replaced N call sites of `tmux_client.<method>` with `get_multiplexer().<method>`.
- Added `launch_spec` parameter to: <list>.

## Tests
- targeted (services + multiplexers): <N pass / M fail>

## Deviations
<any>
```

Echo: `TSK-079: PASS|FAIL — <reason>`.

DO NOT commit. Stop after Task 9.

Begin.
