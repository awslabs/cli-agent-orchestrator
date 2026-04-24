# TSK-081 — Phase 2 Task 10: smoke harness (opt-in, gated)

You are executing Phase 2 Task 10 of PRJ-042 (aws-cao WezTerm port). Self-contained prompt — no prior context.

## Repo state
- Working dir: `C:\dev\aws-cao`, branch `wezterm-multiplexer` (clean tree).
- Tasks 1–9 committed. `WezTermMultiplexer` exists with full `create_session` / `_paste_text` / `_submit_input` / `send_special_key` / `get_history` / `pipe_pane` / `kill_*` surface. `get_multiplexer()` accessor wired into `terminal_service`. Codex Windows shim resolution via `build_launch_spec`.
- Plan binding spec: `docs/PLAN-phase2.md` §6 ("Real smoke tests").
- Project test config: `pyproject.toml` (no `pytest.ini`).

## Goal

Add an opt-in smoke-test harness that exercises the WezTerm backend against real binaries on the user's machine. Default `pytest` runs MUST NOT execute these tests — they require WezTerm + provider CLIs to be installed and on PATH. Invocation: `pytest -m smoke` (explicit) or `pytest test/smoke -m smoke` (scoped).

The point is to dogfood the Layer 1 abstraction by exercising it end-to-end on a real system. CI will skip these by default.

## Implementation requirements

### Pytest marker registration

Add to `pyproject.toml` under `[tool.pytest.ini_options]` (create the section if missing):

```toml
[tool.pytest.ini_options]
markers = [
    "smoke: opt-in tests that require real wezterm + provider CLIs on PATH; not run by default",
]
addopts = "-m 'not smoke'"
```

The `addopts = "-m 'not smoke'"` line is the gate — default `pytest` invocations won't pick up `@pytest.mark.smoke` tests. Users opt in with `pytest -m smoke` (overrides default `-m`).

If `pyproject.toml` already has a `[tool.pytest.ini_options]` section, add to it without breaking existing keys. If `addopts` already exists, append `-m 'not smoke'` to the existing value carefully (preserve any existing options).

### Test directory layout

Create:

```
test/smoke/
    __init__.py
    README.md
    conftest.py
    test_wezterm_basics.py
    test_claude_startup.py
    test_codex_direct_spawn.py
    test_inbox_poller.py
```

### `test/smoke/conftest.py`

Skip-if-not-available fixtures + helpers:

```python
import os
import shutil
import pytest
import time
from pathlib import Path

from cli_agent_orchestrator.multiplexers.wezterm import WezTermMultiplexer


def _which_or_skip(name: str) -> str:
    path = shutil.which(name) or shutil.which(f"{name}.cmd")
    if not path:
        pytest.skip(f"{name} not on PATH; skipping smoke test")
    return path


@pytest.fixture(scope="session")
def wezterm_bin() -> str:
    return _which_or_skip("wezterm")


@pytest.fixture(scope="session")
def claude_bin() -> str:
    return _which_or_skip("claude")


@pytest.fixture(scope="session")
def codex_bin() -> str:
    return _which_or_skip("codex")


@pytest.fixture
def multiplexer(wezterm_bin) -> WezTermMultiplexer:
    return WezTermMultiplexer(wezterm_bin=wezterm_bin)


def _wait_for_text(multiplexer, session, window, needle: str, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = multiplexer.get_history(session, window)
        if needle in text:
            return True
        time.sleep(0.5)
    return False


@pytest.fixture
def wait_for_text():
    return _wait_for_text
```

Mark every test in `test/smoke/` with `@pytest.mark.smoke` at the module or function level. Use the `pytestmark = pytest.mark.smoke` module-level pattern in each file.

### `test/smoke/test_wezterm_basics.py`

```python
import pytest
pytestmark = pytest.mark.smoke


def test_spawn_send_get_kill(multiplexer, tmp_path, wait_for_text):
    multiplexer.create_session(
        session_name="cao-smoke-basics",
        window_name="bash",
        terminal_id="smoke-basics",
        working_directory=str(tmp_path),
    )
    try:
        multiplexer.send_keys("cao-smoke-basics", "bash", "echo hello-smoke", enter_count=1)
        assert wait_for_text(multiplexer, "cao-smoke-basics", "bash", "hello-smoke", timeout=10)
    finally:
        multiplexer.kill_session("cao-smoke-basics")
```

### `test/smoke/test_claude_startup.py`

Spawn Claude inside a wezterm pane, wait for the trust prompt to appear in `get_history`, accept it via `send_special_key("Enter")`, confirm idle prompt appears.

```python
import pytest
from cli_agent_orchestrator.multiplexers.base import LaunchSpec
from cli_agent_orchestrator.providers.claude_code import (
    TRUST_PROMPT_PATTERN, IDLE_PROMPT_PATTERN,
)
import re

pytestmark = pytest.mark.smoke


def test_trust_prompt_acceptance(multiplexer, claude_bin, tmp_path, wait_for_text):
    spec = LaunchSpec(argv=(claude_bin,), provider="claude")
    multiplexer.create_session(
        session_name="cao-smoke-claude",
        window_name="claude-0",
        terminal_id="smoke-claude",
        working_directory=str(tmp_path),
        launch_spec=spec,
    )
    try:
        # Wait for trust prompt
        deadline = 30
        for _ in range(deadline):
            text = multiplexer.get_history("cao-smoke-claude", "claude-0")
            if re.search(TRUST_PROMPT_PATTERN, text):
                break
            import time as _t
            _t.sleep(1)
        else:
            pytest.fail("Claude trust prompt not seen in 30s")
        # Accept via the abstraction
        multiplexer.send_special_key("cao-smoke-claude", "claude-0", "Enter")
        # Idle prompt should appear after trust
        for _ in range(30):
            text = multiplexer.get_history("cao-smoke-claude", "claude-0")
            if re.search(IDLE_PROMPT_PATTERN, text):
                return
            import time as _t
            _t.sleep(1)
        pytest.fail("Claude idle prompt not seen after trust accept")
    finally:
        multiplexer.kill_session("cao-smoke-claude")
```

If the actual regex constants are not exported under those names, use whatever is currently in `claude_code.py` — read the file first.

### `test/smoke/test_codex_direct_spawn.py`

Use `build_launch_spec("codex", ...)` to resolve the Windows shim path; spawn directly via WezTerm; send a paste then Enter; verify Codex received it.

```python
import pytest
import sys
from cli_agent_orchestrator.multiplexers.launch import build_launch_spec

pytestmark = pytest.mark.smoke


def test_codex_direct_spawn_two_step_send(multiplexer, codex_bin, tmp_path, wait_for_text):
    spec = build_launch_spec(
        "codex",
        ["codex"],
        platform="windows" if sys.platform == "win32" else "unix",
        working_directory=str(tmp_path),
    )
    multiplexer.create_session(
        session_name="cao-smoke-codex",
        window_name="codex-0",
        terminal_id="smoke-codex",
        working_directory=str(tmp_path),
        launch_spec=spec,
    )
    try:
        # paste text then submit Enter separately (the multiplexer two-step)
        multiplexer.send_keys(
            "cao-smoke-codex", "codex-0", "/help", enter_count=1
        )
        # Codex /help output is detectable; just confirm the slash command landed
        assert wait_for_text(multiplexer, "cao-smoke-codex", "codex-0", "/help", timeout=15)
    finally:
        multiplexer.kill_session("cao-smoke-codex")
```

### `test/smoke/test_inbox_poller.py`

Spawn a bash pane, attach `pipe_pane` to a temp file, send rapid output, verify the file is captured at the 500 ms cadence.

```python
import pytest
import time
from pathlib import Path

pytestmark = pytest.mark.smoke


def test_pipe_pane_captures_rapid_output(multiplexer, tmp_path):
    log_path = tmp_path / "pane.log"
    multiplexer.create_session(
        session_name="cao-smoke-pipe",
        window_name="bash",
        terminal_id="smoke-pipe",
        working_directory=str(tmp_path),
    )
    try:
        multiplexer.pipe_pane("cao-smoke-pipe", "bash", str(log_path))
        # Send 5 markers in quick succession
        for i in range(5):
            multiplexer.send_keys("cao-smoke-pipe", "bash", f"echo MARK-{i}", enter_count=1)
        # Wait for poller to catch up
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
            if all(f"MARK-{i}" in text for i in range(5)):
                multiplexer.stop_pipe_pane("cao-smoke-pipe", "bash")
                return
            time.sleep(0.5)
        pytest.fail(f"Poller did not capture all markers; last log:\n{log_path.read_text(encoding='utf-8') if log_path.exists() else '<no file>'}")
    finally:
        multiplexer.kill_session("cao-smoke-pipe")
```

### `test/smoke/README.md`

```markdown
# Smoke harness

Real-world tests for the WezTerm multiplexer backend. NOT run by default.

## What this exercises

- spawn / send / get-text / kill on a real WezTerm pane
- Claude trust-prompt acceptance via `send_special_key("Enter")`
- Codex direct spawn via `build_launch_spec` (resolved Windows shim)
- inbox `pipe_pane` capture at the 500 ms polling cadence

## Prerequisites

- WezTerm GUI running, `wezterm` on PATH (CLI subcommand reachable)
- `claude` on PATH (Claude CLI)
- `codex` / `codex.cmd` on PATH (Codex CLI; Windows users may need the Scoop shim)

Tests skip with a clear message when any prerequisite is missing.

## Running

```
pytest -m smoke                  # runs only smoke tests
pytest test/smoke -m smoke       # scoped to test/smoke/ explicitly
pytest test/smoke -m smoke -v    # verbose
```

Default `pytest` invocations DO NOT run these — they're gated via the
project's `addopts = "-m 'not smoke'"`.

## CI

Skip in CI by default. Optional dedicated workflow: install WezTerm +
provider CLIs, then run `pytest -m smoke` on a Windows runner.
```

## Constraints (HARD)

- DO NOT modify `terminal_service.py`, `multiplexers/wezterm.py`, or any provider — they're locked-in committed work.
- DO NOT add a default smoke run to CI — the user controls when to opt in.
- DO NOT install or upgrade dependencies.
- DO NOT commit. Produce a clean working-tree change for the supervising Opus to commit.

## Verification

```
.venv/Scripts/python.exe -m pytest --collect-only -q 2>&1 | grep -c "smoke"
.venv/Scripts/python.exe -m pytest test/clients/ test/multiplexers/ test/providers/ test/services/ test/utils/ --ignore=test/e2e -q --tb=no --no-header
```

The first command shows that smoke tests ARE registered (count > 0) but the second confirms default invocation still hits exactly 43 failures (smoke tests are NOT collected/run). Then verify the opt-in:

```
.venv/Scripts/python.exe -m pytest test/smoke -m smoke --collect-only -q
```

This should list the smoke tests as collected (running them requires the real binaries; expect skips on a sandbox without them).

## Reporting

Write `spikes/TSK-081-result.md`:

```markdown
# TSK-081 — Task 10 result

## Files touched
<list>

## pytest marker registration
<show the [tool.pytest.ini_options] section after edit>

## Smoke tests added
<list of test functions with one-line description>

## Default-run verification
- full (excl. e2e): <N pass / M fail> — must equal 43, smoke tests NOT collected.

## Opt-in collection
- pytest -m smoke --collect-only: <N tests>

## Deviations
<any>
```

Echo: `TSK-081: PASS|FAIL — <reason>`.

DO NOT commit. Stop after Task 10.

Begin.
