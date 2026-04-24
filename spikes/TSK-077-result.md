# TSK-077 — Task 7 result

## Files touched
- `src/cli_agent_orchestrator/multiplexers/wezterm.py`
- `test/multiplexers/test_wezterm_multiplexer.py`
- `spikes/TSK-077-result.md`

## Implementation summary
Implemented a per-pane WezTerm poller registry keyed by `(session_name, window_name)` that starts a daemon thread on `pipe_pane`, polls `wezterm cli get-text` every configurable interval (default `0.5s`), diffs each full snapshot against the prior snapshot with a pure helper, appends only the delta to the pipe log, and tears down cleanly on `stop_pipe_pane`; `kill_session` and `kill_window` now stop any active pollers before killing panes.

## Tests
- `test_wezterm_multiplexer.py (TestPipePane)`: 12 pass / 0 fail
- `test_wezterm_multiplexer.py (full file)`: 47 pass / 0 fail
- `full (excl. e2e)`: 1067 pass / 83 fail — exceeds the required `<=43` budget in this workspace

## Diff strategy
Confirm: pure-append fast-path; line-suffix fallback for redraws/scroll; full-append no-overlap fallback. Pure helper unit-tested independently.

## Cleanup verification
Confirm: `kill_session` / `kill_window` stop active pollers automatically.

## Deviations
- The required broad verification command completed but exceeded the allowed failure budget: `83` failures. Sampled failures were outside Task 7 scope, including `terminal_service` tests patching a missing `tmux_client` attribute and Windows file-handle cleanup failures in logging tests.
- The repo working tree was not clean at verification time; unrelated pre-existing edits were left untouched.
