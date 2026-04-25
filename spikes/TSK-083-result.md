# TSK-083 — Replace broken `wezterm cli spawn --set-environment` with argv-wrap (result)

## Option chosen

Replaced the broken `wezterm cli spawn --set-environment ...` path in
`WezTermMultiplexer._spawn()` with an argv wrapper that injects
`CAO_TERMINAL_ID` plus any `launch_spec.env` values before starting the real
target. Unix now uses `env KEY=VALUE -- <argv...>` so the target exec-replaces
cleanly, while Windows uses a PowerShell `-Command` wrapper that single-quotes
all injected values and splats an explicit `@(...)` args array into `&
<exe> @args`. When `launch_spec` is missing or has no argv, the wrapper falls
back to the platform default shell so env injection still happens. The module
docstring now explains why this exists, cites wezterm/wezterm#6565, and
documents why the non-`exec` Windows process tree is acceptable for CAO.

## Files changed

- `src/cli_agent_orchestrator/multiplexers/wezterm.py` — `+79/-7`
  rewrote `_spawn()`, added `_default_shell()`, `_wrap_with_env()`,
  `_ps_single_quote()`, and documented the WezTerm limitation and Windows
  process-tree reasoning.
- `test/multiplexers/test_wezterm_multiplexer.py` — `+106/-19`
  replaced the old `--set-environment` assertions with wrapper-shape checks for
  both `sys.platform == "linux"` and `"win32"`, added default-shell coverage,
  direct quoting coverage, and explicit Unix/PowerShell invocation-shape tests.

## Test result

`.venv/Scripts/python.exe -m pytest test/multiplexers test/providers test/services --no-cov --deselect test/providers/test_copilot_cli_unit.py`

→ **========== 6 failed, 807 passed, 16 skipped, 33 deselected in 23.74s ==========**

Failures are the pre-existing Windows environment issues in
`test/providers/test_q_cli_integration.py` and
`test/providers/test_tmux_working_directory.py`.

## Subtleties to review before commit

1. PowerShell quoting is intentionally single-quoted and only escapes embedded
   `'` by doubling it. That covers the target executable path, env values, and
   each argv element without going through `cmd /c` parsing.
2. The Windows wrapper always builds `$args=@(...)` and then runs `&
   <exe> @args`; there is no explicit `exit` because PowerShell propagates the
   child exit code automatically.
3. The new tests force both wrapper shapes by monkeypatching `sys.platform`
   inside the same host environment, so Linux and Windows behavior are both
   exercised even on a Windows runner.
