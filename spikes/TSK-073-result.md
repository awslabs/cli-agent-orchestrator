# TSK-073 — Task 3 result

## Files touched
- `src/cli_agent_orchestrator/providers/claude_code.py`
- `src/cli_agent_orchestrator/providers/codex.py`
- `test/providers/test_claude_code_unit.py`
- `test/providers/test_codex_provider_unit.py`
- `test/providers/test_claude_code_coverage.py`

## Bypass replacements
- claude_code.py down-arrow: `~204-212` -> `208-212`, `tmux_client.send_special_key(self.session_name, self.window_name, "\x1b[B", literal=True)`
- claude_code.py trust-enter: `~218-224` -> `223`, `tmux_client.send_special_key(self.session_name, self.window_name, "Enter")`
- codex.py trust-enter: `~233-240` -> `235`, `tmux_client.send_special_key(self.session_name, self.window_name, "Enter")`

## Tests
- claude_code + codex unit suites: `128 pass / 0 fail`
- full (excl. e2e): `1039 pass / 43 fail` — must be <=43 fail

## Deviations
- Updated `test/providers/test_claude_code_coverage.py` in addition to the two requested unit files so the full non-e2e suite returned to the 43-failure baseline after the provider route change.

## Follow-ups
- None
