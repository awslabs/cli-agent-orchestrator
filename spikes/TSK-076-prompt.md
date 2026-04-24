# TSK-076 — Phase 2 Task 5: WezTermMultiplexer core (spawn / send / get-text / kill)

You are implementing Phase 2 Task 5 of PRJ-042 (aws-cao WezTerm port). This is a TDD task on novel code — write tests first, then the implementation.

## Repo state
- Working dir: `C:\dev\aws-cao`, branch `wezterm-multiplexer` (clean tree).
- Tasks 1, 2, 3, 6 + audit committed. `BaseMultiplexer` and `TmuxMultiplexer` exist.
- Read first:
  - `docs/PLAN-phase2.md` §1, §2, §4 (the WezTerm core spec)
  - `src/cli_agent_orchestrator/multiplexers/base.py` — abstract method set you must implement
  - `src/cli_agent_orchestrator/multiplexers/tmux.py` — reference implementation pattern (especially how it shells out)
  - `spikes/01-result.md`, `spikes/02-result.md`, `spikes/04-result.md` — Phase 1 spike findings on actual WezTerm CLI behavior
  - `docs/multiplexer-api-surface.md` — Phase 0 surface inventory

## Scope of THIS task ONLY (do not exceed)

Implement `WezTermMultiplexer(BaseMultiplexer)` in `src/cli_agent_orchestrator/multiplexers/wezterm.py` with the following methods working against a mocked subprocess CLI runner:

- `create_session()`
- `create_window()`
- `_paste_text()`
- `_submit_input()`
- `send_special_key(... *, literal: bool = False)`
- `get_history()`
- `list_sessions()`
- `kill_session()`
- `kill_window()`
- `session_exists()`
- `get_pane_working_directory()`
- internal pane/session registry per plan §4

Defer to **Task 7**:
- `pipe_pane()` / `stop_pipe_pane()` — implement as `raise NotImplementedError("Task 7 (poller-backed pipe_pane) not yet implemented")`. Task 7 will replace the body.

DO NOT in this task:
- Add a `get_multiplexer()` accessor — Task 4 owns that. Task 4 is running in parallel and will lazy-import your module.
- Modify `multiplexers/__init__.py` — Task 4 owns it. Your class is reachable via `from cli_agent_orchestrator.multiplexers.wezterm import WezTermMultiplexer`.
- Implement Codex-on-Windows launch resolver — Task 8 owns that. For now, when a `LaunchSpec.argv` is provided, spawn it directly via `wezterm cli spawn -- <argv>`.
- Touch any provider, service, or `terminal_service` code.

## Implementation requirements

### CLI invocation seam

Take a runner injection seam so tests can mock without monkeypatching `subprocess`. Recommended:

```python
from typing import Callable, Mapping, Sequence

WezTermRunner = Callable[[Sequence[str], Mapping[str, str] | None], "subprocess.CompletedProcess[str]"]

def _default_runner(argv, env=None):
    import subprocess
    return subprocess.run(list(argv), env=env, capture_output=True, text=True, check=False)

class WezTermMultiplexer(BaseMultiplexer):
    def __init__(self, runner: WezTermRunner | None = None, wezterm_bin: str | None = None):
        self._run = runner or _default_runner
        self._bin = wezterm_bin or os.environ.get("WEZTERM_EXECUTABLE") or "wezterm"
        self._sessions: dict[str, dict] = {}  # session_name → {workspace, windows: {window_name → pane_id, tab_id?, window_id?}}
```

This makes tests fast and deterministic — no real wezterm process launched.

### Spawn (per plan §4)

`create_session()` and `create_window()` should:

1. Validate the working directory via the inherited `_resolve_and_validate_working_directory()` helper.
2. If `launch_spec` is None, spawn the user's interactive shell: `[self._bin, "cli", "spawn", "--new-window", "--cwd", cwd, "--set-environment", f"CAO_TERMINAL_ID={terminal_id}"]`.
3. If `launch_spec.argv` is set, append `"--"` then the argv elements to the spawn command.
4. If `launch_spec.env` is set, emit one `--set-environment KEY=VALUE` arg per pair (in addition to CAO_TERMINAL_ID).
5. Parse the `wezterm cli spawn` stdout — it returns the new pane id as bare digits with optional whitespace. Handle empty stdout / non-numeric output by raising a clear error.
6. Persist the pane id in `self._sessions[session_name][...]`.

For MVP per plan §4, use `--new-window` for both `create_session()` and `create_window()` (one CAO window = one WezTerm OS window). Keep it simple; tab/pane optimization is out of scope.

### `_paste_text()` and `_submit_input()` (two-step delivery, per plan §4)

```
wezterm cli send-text --pane-id <pane_id> -- <text>           # default mode = bracketed paste
wezterm cli send-text --pane-id <pane_id> --no-paste -- $'\r' # submit (separate)
```

Inter-step delays from plan §4 to match tmux:
- `_paste_text`: no internal delay (the submit step is its own call).
- `_submit_input`: 300 ms after entering, then 500 ms between each additional Enter when `enter_count > 1`. Use `time.sleep` (mockable in tests via `monkeypatch.setattr`).

The base class's default `send_keys()` calls `_paste_text` then `_submit_input` — DO NOT override `send_keys` on the subclass.

### `send_special_key(... *, literal: bool = False)`

- When `literal=True`: emit the `key` as raw VT bytes via `wezterm cli send-text --pane-id <id> --no-paste -- <key>`.
- When `literal=False`: map known names (`Enter`, `Tab`, `Up`, `Down`, `Left`, `Right`, `Escape`, `Backspace`) to their VT escape sequences (`\r` for Enter, `\t` for Tab, `\x1b[A` for Up, etc.), then emit via the same no-paste send-text. Document the supported set in the docstring.

### `get_history()`

- Spike 4 (read `spikes/04-result.md`) showed plain mode preserves the patterns CAO providers care about, and `--escapes` breaks Claude trust-prompt matching. So:
  - `wezterm cli get-text --pane-id <pane_id>` (NO `--escapes`).
  - When `tail_lines` is provided, slice the last N lines after capture (rstrip → splitlines → tail).

### `list_sessions()` / `session_exists()` / `kill_session()` / `kill_window()`

- Drive these from the in-memory session registry (`self._sessions`) plus `wezterm cli list` for cross-process visibility if desired.
- For MVP, registry-only is acceptable — provider tests already mock at the abstraction level. Document the limitation in a docstring (the only "WHY" comment allowed).

### `get_pane_working_directory()`

- WezTerm CLI doesn't expose pane CWD reliably in early versions. Per plan §4 ("when the backend exposes it"), return `None` for MVP if the CLI lookup fails. A best-effort `wezterm cli list --format json` parse is acceptable but not required for MVP.

### Error handling (plan §4 last paragraph)

Raise specific, actionable errors on:

- WezTerm binary missing (CalledProcessError on spawn check) → `RuntimeError("WezTerm CLI not available: <bin path>")`.
- `wezterm cli spawn` stdout doesn't contain a pane id → `RuntimeError("WezTerm spawn returned no pane id; stdout=<...>")`.
- pane id no longer present on `send-text` / `get-text` → `RuntimeError("WezTerm pane <id> not found")`.

NO silent fallbacks to tmux. The point of the split is explicit selection and explicit failure.

## TDD discipline

Write `test/multiplexers/test_wezterm_multiplexer.py` FIRST. Coverage target — at minimum:

- `create_session()` builds the right argv with `--new-window`, `--cwd`, `--set-environment CAO_TERMINAL_ID=<id>`, and parses pane id from runner stdout.
- `create_session()` with `LaunchSpec(argv=["codex.cmd", "--yolo"])` appends `-- codex.cmd --yolo` after the env args.
- `create_session()` with `LaunchSpec(env={"FOO": "bar"})` adds `--set-environment FOO=bar`.
- `create_session()` raises `RuntimeError` when runner stdout has no pane id.
- `_paste_text()` calls `wezterm cli send-text --pane-id <id> -- <text>` (default paste).
- `_submit_input(enter_count=1)` calls `wezterm cli send-text --pane-id <id> --no-paste -- "\r"` once after a 300ms sleep.
- `_submit_input(enter_count=3)` produces 3 Enter calls with 500ms inter-Enter sleeps.
- `send_keys(... enter_count=2)` (inherited default) calls _paste_text once then _submit_input with enter_count=2.
- `send_special_key("Enter")` produces `--no-paste -- "\r"`.
- `send_special_key("\x1b[B", literal=True)` produces `--no-paste -- "\x1b[B"`.
- `get_history()` calls `wezterm cli get-text --pane-id <id>` (no --escapes) and returns runner stdout verbatim.
- `get_history(tail_lines=5)` returns last 5 lines.
- `kill_session()` removes the session from registry and calls `wezterm cli kill-pane` for each pane id.
- `pipe_pane()` raises `NotImplementedError` referencing Task 7.

Mock `time.sleep` to keep tests fast (`monkeypatch.setattr("cli_agent_orchestrator.multiplexers.wezterm.time.sleep", lambda *_: None)`).

## Constraints (HARD)

- DO NOT modify `multiplexers/__init__.py` — Task 4 owns it (running in parallel).
- DO NOT eagerly export your class at package level.
- DO NOT add `get_multiplexer()` or selection logic.
- DO NOT depend on real WezTerm. All tests must be deterministic via the runner injection seam.
- DO NOT install or upgrade dependencies.
- Use `.venv\Scripts\python.exe -m pytest` for verification.
- DO NOT commit. Produce a clean working-tree change for the supervising Opus to commit.
- No comments in code unless explaining non-obvious WHY (project rule).
- Type hints: strict, no `Any` outside the runner seam.

## Verification

```
.venv/Scripts/python.exe -m pytest test/multiplexers/test_wezterm_multiplexer.py -v --tb=short
.venv/Scripts/python.exe -m pytest test/clients/ test/multiplexers/ test/providers/ test/services/ test/utils/ --ignore=test/e2e -q --tb=no --no-header
```

Second run failure count must not exceed 43.

## Reporting

Report back to the supervising session with:
1. Files created.
2. Test counts (per-file and full-suite).
3. Any deviations from the plan.
4. Any decisions you punted on (Task-N follow-up suggestions).

DO NOT commit. Stop after Task 5.
