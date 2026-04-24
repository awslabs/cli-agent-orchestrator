# TSK-077 — Phase 2 Task 7: WezTerm poller-backed pipe_pane

You are executing Phase 2 Task 7 of PRJ-042 (aws-cao WezTerm port). Self-contained prompt — no prior context.

## Repo state
- Working dir: `C:\dev\aws-cao`, branch `wezterm-multiplexer` (clean tree).
- Tasks 1, 2, 3, 4, 5, 6 + audit committed. `WezTermMultiplexer` exists at `src/cli_agent_orchestrator/multiplexers/wezterm.py` with `pipe_pane` / `stop_pipe_pane` raising `NotImplementedError("Task 7 (poller-backed pipe_pane) not yet implemented")`.
- Plan binding spec: `docs/PLAN-phase2.md` §4 ("`get_text()` / `get_history()` and polling" subsection — read it first), §8 risk #5.
- Phase 1 spike 3 result: `spikes/03-result.md` — empirical 500 ms validated as 0-miss, 144–207 ms first-detection latency, lower CPU than tighter loops.

## Goal

Replace the two `NotImplementedError` stubs with a per-pane background poller. Each poller thread polls `wezterm cli get-text --pane-id <id>` every 500 ms, diffs against the prior snapshot, appends new content to the configured file, and tears down cleanly on `stop_pipe_pane`. Tests use a runner mock + a fake sleeper so they're deterministic and fast.

## Implementation requirements

In `src/cli_agent_orchestrator/multiplexers/wezterm.py`:

### Per-pane poller state

Extend the multiplexer with a registry of active pollers:

```python
@dataclass
class _PollerState:
    thread: threading.Thread
    stop_event: threading.Event
    snapshot: str  # last full get-text output
    file_path: str
```

Keyed by `(session_name, window_name)` pair (matching the existing pane registry shape).

### `pipe_pane(session_name, window_name, file_path)`

1. Look up the pane id from the existing registry. Raise `RuntimeError` if missing (clear message: pane not found).
2. Reject if a poller already exists for this `(session, window)`. Raise `RuntimeError("pipe_pane already running for <session>:<window>")`.
3. Create the file (empty) at `file_path` if it doesn't exist; open it lazily inside the poller for append.
4. Start a daemon thread running `_poll_loop`. Store `_PollerState` in the registry.

### `_poll_loop(session, window, pane_id, stop_event, file_path)`

```
prev = ""
while not stop_event.wait(self._poll_interval):  # 0.5 by default
    try:
        snapshot = self._get_pane_text(pane_id)  # plain wezterm cli get-text
    except RuntimeError:
        # pane gone — exit cleanly
        return
    delta = self._diff_snapshot(prev, snapshot)
    if delta:
        with open(file_path, "a", encoding="utf-8") as fh:
            fh.write(delta)
        prev = snapshot
```

Make `_poll_interval = 0.5` an attribute on the class so tests can shrink it. Allow injection via `WezTermMultiplexer.__init__` similar to the runner seam — add `poll_interval: float = 0.5` and accept a `clock_sleep` injection point too if helpful.

### `_diff_snapshot(prev: str, current: str) -> str`

The load-bearing logic. Plan §8 risk #5 calls out three failure modes:

1. **Append case (most common)**: `current.startswith(prev)` → return `current[len(prev):]`.
2. **Buffer rewrite (TUI redraw, pane clear)**: prefix doesn't match → fall back to line-based suffix matching. Find the longest tail of `prev` lines that appears as a prefix of `current` lines (or vice-versa). Append only the new lines after the match. If no overlap at all, append the entire `current` (accept duplicate over silent loss).
3. **Pane scrolled past buffer**: `len(current) < len(prev)` and prefix doesn't match → use the same line-based approach as #2.

Reference implementation:

```python
def _diff_snapshot(self, prev: str, current: str) -> str:
    if not prev:
        return current
    if current == prev:
        return ""
    if current.startswith(prev):
        return current[len(prev):]
    # Line-based fallback for redraws and scrollback.
    prev_lines = prev.splitlines(keepends=True)
    cur_lines = current.splitlines(keepends=True)
    for k in range(min(len(prev_lines), len(cur_lines)), 0, -1):
        if prev_lines[-k:] == cur_lines[:k]:
            return "".join(cur_lines[k:])
    return current  # no overlap; append entire snapshot
```

Keep this logic pure (no I/O) so it's trivially unit-testable.

### `stop_pipe_pane(session_name, window_name)`

1. Look up the poller state. If absent, raise `RuntimeError("pipe_pane not running for <session>:<window>")`.
2. Set the stop event. Join the thread with a 2-second timeout. If join times out, log a warning but do not block.
3. Remove the entry from the registry.

### Cleanup on `kill_session` / `kill_window`

When a session or window is killed, also stop its poller(s) if any. Reuse `stop_pipe_pane` internally; ignore the "not running" error case.

## Tests

Add to `test/multiplexers/test_wezterm_multiplexer.py` a new `TestPipePane` class:

1. `pipe_pane` raises if the pane is not registered.
2. `pipe_pane` raises if a poller is already running for that `(session, window)`.
3. After 1 tick with no change, file is empty.
4. After 1 tick where `get-text` returns `"hello\n"`, file contains `"hello\n"`.
5. Pure-append: `prev = "hello\n"`, `current = "hello\nworld\n"` → file ends with `"hello\nworld\n"`.
6. Redraw: `prev = "abc\n"`, `current = "xyz\n"` → file contains `"abc\nxyz\n"` (no overlap, full append).
7. Line-suffix overlap: `prev = "a\nb\nc\n"`, `current = "b\nc\nd\n"` → delta is `"d\n"`, file contains `"a\nb\nc\nd\n"`.
8. Pane disappears mid-poll (runner raises `RuntimeError`): poller exits cleanly, no traceback.
9. `stop_pipe_pane` cancels thread, file is closed (subsequent writes don't appear).
10. `stop_pipe_pane` raises when no poller exists.
11. `kill_session` stops the poller automatically.

Use a deterministic test pattern:

```python
def test_pure_append(self, multiplexer, tmp_path, fake_runner):
    # multiplexer fixture creates with poll_interval=0.001
    fake_runner.queue_responses([
        ("hello\n", 0),
        ("hello\nworld\n", 0),
    ])
    multiplexer.pipe_pane("sess", "win", str(tmp_path / "pipe.log"))
    fake_runner.wait_for_queue_drain(timeout=1.0)
    multiplexer.stop_pipe_pane("sess", "win")
    assert (tmp_path / "pipe.log").read_text() == "hello\nworld\n"
```

The fake_runner needs a `queue_responses` + `wait_for_queue_drain` helper. Implement that in the test file as a small helper class — don't add it to production code.

Direct unit-test the pure helper too:

```python
def test_diff_snapshot_pure_append():
    m = WezTermMultiplexer(...)
    assert m._diff_snapshot("hello\n", "hello\nworld\n") == "world\n"
```

## Constraints (HARD)

- DO NOT modify any other multiplexer, provider, or service file.
- DO NOT change existing public method signatures.
- DO NOT use `asyncio` — stick to `threading` (consistent with the existing pyramid).
- DO NOT depend on real wall-clock time in tests. Inject `poll_interval` (0.001 s for tests) and use `stop_event.wait(...)` so tests can drive the cadence via the runner mock.
- DO NOT install or upgrade dependencies.
- Use `.venv\Scripts\python.exe -m pytest` (project's `rtk pytest` shim collects nothing).
- DO NOT commit. Produce a clean working-tree change for the supervising Opus to commit.

## Verification

```
.venv/Scripts/python.exe -m pytest test/multiplexers/test_wezterm_multiplexer.py -v --tb=short
.venv/Scripts/python.exe -m pytest test/clients/ test/multiplexers/ test/providers/ test/services/ test/utils/ --ignore=test/e2e -q --tb=no --no-header
```

Second run failure count must not exceed 43.

## Reporting

Write `spikes/TSK-077-result.md`:

```markdown
# TSK-077 — Task 7 result

## Files touched
<list>

## Implementation summary
<one paragraph: thread model, diff strategy, cleanup>

## Tests
- test_wezterm_multiplexer.py (TestPipePane): <N pass / M fail>
- full (excl. e2e): <N pass / M fail> — must be ≤43

## Diff strategy
Confirm: pure-append fast-path; line-suffix fallback for redraws/scroll; full-append no-overlap fallback. Pure helper unit-tested independently.

## Cleanup verification
Confirm: kill_session / kill_window stop active pollers automatically.

## Deviations
<any>
```

Echo: `TSK-077: PASS|FAIL — <reason>`.

DO NOT commit. Stop after Task 7.

Begin.
