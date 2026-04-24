# Phase 2 Implementation Plan for PRJ-042

## 1. Executive summary
Phase 2 introduces a real multiplexer boundary under the existing provider/service stack: `BaseMultiplexer`, a behavior-preserving `TmuxMultiplexer`, and a new `WezTermMultiplexer` that replaces tmux-only primitives with WezTerm CLI equivalents. The largest design change is making message delivery explicitly two-step at the multiplexer layer, because tmux already pastes then submits (`src/cli_agent_orchestrator/clients/tmux.py:198-251`) and WezTerm must do the same. Rough size is ~0.9-1.1 kLoC touched, with ~5-6 solo-maintainer days for Claude + Codex MVP on Windows and tmux parity retained on Unix. Main risks are provider regex drift outside the spike coverage, Codex's Windows shim/config workaround going stale, and WezTerm CLI behavior changing across releases.

## 2. BaseMultiplexer interface
The public surface should stay at the same 11 active methods from Phase 0, so `terminal_service`, `session_service`, `wait_for_shell()`, and provider status logic do not need a full rewrite (`docs/multiplexer-api-surface.md`, `src/cli_agent_orchestrator/services/terminal_service.py:122-188`, `src/cli_agent_orchestrator/utils/terminal.py:37-80`). The two generalizations are:

1. `create_session()` / `create_window()` accept an optional `LaunchSpec` so backends that must spawn the target CLI directly can do so.
2. `send_keys()` becomes a default method built on two abstract primitives: paste text, then submit separately.

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence


@dataclass(frozen=True)
class LaunchSpec:
    """Concrete process spawn request for a new pane/window.

    argv:
        Exact argv to execute as the pane's initial process. When None, start
        the backend's default interactive shell.
    env:
        Extra environment variables to inject into the spawned process.
    provider:
        Optional provider key used by backend-specific launch templating and
        executable resolution.
    """

    argv: Optional[Sequence[str]] = None
    env: Optional[Mapping[str, str]] = None
    provider: Optional[str] = None


class BaseMultiplexer(ABC):
    """Backend-neutral pane/session control surface for CAO."""

    def _resolve_and_validate_working_directory(
        self, working_directory: Optional[str]
    ) -> str:
        """Canonicalize, validate, and return a safe working directory."""

    @abstractmethod
    def create_session(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        launch_spec: Optional[LaunchSpec] = None,
    ) -> str:
        """Create a detached CAO session/workspace and return the actual window name."""

    @abstractmethod
    def create_window(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        launch_spec: Optional[LaunchSpec] = None,
    ) -> str:
        """Create another CAO window/pane inside an existing session."""

    def send_keys(
        self, session_name: str, window_name: str, keys: str, enter_count: int = 1
    ) -> None:
        """Paste text, wait for the TUI to settle, then submit Enter separately."""
        self._paste_text(session_name, window_name, keys)
        self._submit_input(session_name, window_name, enter_count=enter_count)

    @abstractmethod
    def _paste_text(self, session_name: str, window_name: str, text: str) -> None:
        """Inject literal text without submitting it."""

    @abstractmethod
    def _submit_input(
        self, session_name: str, window_name: str, enter_count: int = 1
    ) -> None:
        """Submit already-pasted input with one or more Enter presses."""

    @abstractmethod
    def send_special_key(
        self,
        session_name: str,
        window_name: str,
        key: str,
        *,
        literal: bool = False,
    ) -> None:
        """Send a control key or a literal VT sequence without paste semantics."""

    @abstractmethod
    def get_history(
        self, session_name: str, window_name: str, tail_lines: Optional[int] = None
    ) -> str:
        """Return normalized pane text for provider regex/status parsing."""

    @abstractmethod
    def list_sessions(self) -> list[dict[str, str]]:
        """List CAO-visible sessions as {id, name, status}."""

    @abstractmethod
    def kill_session(self, session_name: str) -> bool:
        """Terminate a session and all owned panes/windows."""

    @abstractmethod
    def kill_window(self, session_name: str, window_name: str) -> bool:
        """Terminate one CAO window/pane."""

    @abstractmethod
    def session_exists(self, session_name: str) -> bool:
        """Return True when the named session/workspace exists."""

    @abstractmethod
    def get_pane_working_directory(
        self, session_name: str, window_name: str
    ) -> Optional[str]:
        """Return the active pane's working directory when the backend exposes it."""

    @abstractmethod
    def pipe_pane(self, session_name: str, window_name: str, file_path: str) -> None:
        """Start backend-specific output capture into a CAO log file."""

    @abstractmethod
    def stop_pipe_pane(self, session_name: str, window_name: str) -> None:
        """Stop backend-specific output capture for a CAO log file."""
```

Abstract: `create_session`, `create_window`, `_paste_text`, `_submit_input`, `send_special_key`, `get_history`, `list_sessions`, `kill_session`, `kill_window`, `session_exists`, `get_pane_working_directory`, `pipe_pane`, `stop_pipe_pane`.

Default-implemented: `_resolve_and_validate_working_directory` lifted from `TmuxClient` (`src/cli_agent_orchestrator/clients/tmux.py:40-115`) and `send_keys()` as the shared paste-then-submit primitive. That matches CAO's current provider contract, including `BaseProvider.paste_enter_count` (`src/cli_agent_orchestrator/providers/base.py:75-85`) and `terminal_service.send_input()` (`src/cli_agent_orchestrator/services/terminal_service.py:288-320`).

`LaunchSpec` is the smallest interface change that cleanly covers the Codex-on-Windows requirement from spike 2b without hard-coding Codex logic into providers or services. Tmux can ignore it for the MVP path and remain shell-first; WezTerm can use it where shell-resolved `codex` is wrong.

## 3. TmuxMultiplexer identity refactor
This should be mechanical.

Move:
- `src/cli_agent_orchestrator/clients/tmux.py` implementation into `src/cli_agent_orchestrator/multiplexers/tmux.py` as `class TmuxMultiplexer(BaseMultiplexer)`.
- Path validation stays byte-for-byte unless typing/docstrings are adjusted (`src/cli_agent_orchestrator/clients/tmux.py:40-115`).
- Session/window creation logic, env filtering, history capture, list/kill/existence checks, pane CWD lookup, and `pipe-pane` behavior all carry over unchanged (`src/cli_agent_orchestrator/clients/tmux.py:117-430` and remainder).

Stay:
- `src/cli_agent_orchestrator/clients/tmux.py` becomes a thin compatibility shim:

```python
from cli_agent_orchestrator.multiplexers.tmux import TmuxMultiplexer

tmux_client = TmuxMultiplexer()
```

- Existing imports in providers/services remain valid in Phase 3, so the tmux refactor is low-risk and easy to bisect.

Non-trivial behavior changes to call out as risk:
- `send_special_key()` should grow `literal: bool = False` so Claude's startup handler can stop bypassing the abstraction with raw `tmux send-keys -l "\x1b[B"` (`src/cli_agent_orchestrator/providers/claude_code.py:204-224`). Tmux behavior stays the same; only the route changes.
- If `create_session()` / `create_window()` gain `launch_spec`, tmux should accept it but default to the old interactive-shell startup so provider init order remains unchanged. Anything more aggressive than that is unnecessary risk for the tmux path.

## 4. WezTermMultiplexer — new
### Pane/window model mapping
CAO's external address is still `session_name + window_name`. Internally, `WezTermMultiplexer` should maintain a small registry:

```python
session_name -> {
    "workspace": session_name,
    "window_name" -> {
        "pane_id": str,
        "tab_id": str | None,
        "window_id": str | None,
    }
}
```

Mapping:
- tmux session -> WezTerm workspace
- tmux window -> WezTerm pane owned by a dedicated tab or window
- tmux pane -> WezTerm pane id

For MVP, the simplest stable mapping is one CAO "window" per WezTerm OS window spawned with `--new-window`. It is slightly heavier than tabs, but it matches the spike setup, avoids tab-focus ambiguity, and keeps pane ids isolated. A later optimization can consolidate into tabs once the backend is stable.

### Session/window creation
`create_session()` and `create_window()` should:
- validate `working_directory` with the shared base helper
- resolve the WezTerm executable once up front and raise a clear error if it is unavailable
- inject `CAO_TERMINAL_ID` plus provider-safe env vars through `wezterm cli spawn`
- persist the pane id returned by `wezterm cli spawn`

Representative spawn:

```text
wezterm cli spawn --new-window --cwd <dir> --set-environment CAO_TERMINAL_ID=<id> -- <argv...>
```

If `launch_spec` is omitted, spawn the backend's default interactive shell so current provider `initialize()` flows keep working. If `launch_spec.argv` is present, spawn that process directly.

### `send_message()` / `send_keys()` two-step flow
The backend must never treat WezTerm submission as a one-shot paste. The flow should be:

```text
wezterm cli send-text --pane-id <pane_id> -- <body>
wezterm cli send-text --pane-id <pane_id> --no-paste -- "\r"
```

Implementation details:
- `_paste_text()` uses default paste mode so the target TUI sees bracketed paste, matching tmux `paste-buffer -p` semantics (`src/cli_agent_orchestrator/clients/tmux.py:203-242`).
- `_submit_input()` sends carriage return separately in `--no-paste` mode. Repeated Enters honor `enter_count` from the provider contract (`src/cli_agent_orchestrator/providers/base.py:75-85`).
- Keep the same small inter-submit delays tmux already needs: ~300 ms after paste, ~500 ms between extra Enters (`src/cli_agent_orchestrator/clients/tmux.py:229-241`).

This is not a WezTerm-only quirk. It is the explicit backend-neutral form of what CAO already does in tmux.

### `get_text()` / `get_history()` and polling
Use plain `wezterm cli get-text`, not `--escapes`; spike 4 showed plain mode preserves the patterns CAO cares about while `--escapes` breaks Claude trust-prompt matching. `get_history()` should therefore normalize to plain text by default for WezTerm exactly as providers expect today.

For `pipe_pane()` / `stop_pipe_pane()`:
- implement a background poller per pane instead of a real stream
- poll every 500 ms per spike 3; that interval saw 0 misses and ~144-207 ms first-detection latency with lower CPU than tighter loops
- each poll reads `get-text`, diffs against the prior snapshot, and appends only new content to `file_path`
- `stop_pipe_pane()` cancels the poller and clears its state

This preserves the `terminal_service` contract (`src/cli_agent_orchestrator/services/terminal_service.py:184-188`, `:445-447`) and minimizes churn above the backend.

### Launch command templating and Codex-on-Windows
WezTerm needs a backend-owned launch template registry, because the correct process to spawn is not always the shell-resolved provider binary. The minimum viable design is:

```python
def build_launch_spec(
    provider: str,
    command_argv: Sequence[str],
    *,
    platform: Literal["windows", "unix"],
    working_directory: str,
) -> LaunchSpec:
    ...
```

Rules:
- default providers pass through `command_argv`
- Codex on Windows resolves an explicit shim path, never bare `codex`
- launch resolution happens in the backend or a tiny shared helper, not inside `terminal_service`

Worked Codex example from spike 2b:

```text
wezterm cli spawn --new-window --cwd C:\dev\aws-cao -- \
  C:\Users\marc\scoop\apps\nodejs-lts\current\bin\codex.cmd \
  -c hooks=[] --yolo --no-alt-screen --disable shell_snapshot
```

Load-bearing parts:
- explicit Windows `codex.cmd` path, because shell-domain resolution hit a Linux/WSL wrapper and exited
- `-c hooks=[]`, because local interactive Codex rejected the user's existing hooks config
- `--yolo --no-alt-screen --disable shell_snapshot`, which already come from the tmux provider command builder (`src/cli_agent_orchestrator/providers/codex.py:136-142`, `:261-267`)

Phase 3 should implement platform-specific resolver order roughly as:

1. explicit configured override
2. Windows known shim lookup (`where.exe codex.cmd`, Scoop/Node install candidates)
3. shell-resolved binary on non-Windows

### Claude trust-prompt handler port
Do not re-invent the logic in `ClaudeCodeProvider._handle_startup_prompts()`; port it to the abstraction by replacing the tmux bypasses:
- raw down-arrow currently bypasses the wrapper with `tmux send-keys -l "\x1b[B"` (`src/cli_agent_orchestrator/providers/claude_code.py:204-212`)
- trust confirmation currently reaches through `tmux_client.server.sessions...pane.send_keys("", enter=True)` (`src/cli_agent_orchestrator/providers/claude_code.py:218-224`)

The plan for Phase 3 is:
- keep the regexes unchanged
- replace direct tmux subprocess/libtmux calls with `multiplexer.send_special_key(..., literal=True)` and `multiplexer.send_special_key(..., "Enter")`
- keep polling against plain `get_history()` output; spike 4 already showed `TRUST_PROMPT_PATTERN` works in plain WezTerm capture

### Error handling
`WezTermMultiplexer` should fail early and specifically on:
- WezTerm binary missing or not executable
- `wezterm cli spawn` returning no pane id
- pane id no longer present when sending input or reading output
- poller thread/task already running or missing on stop

These should become actionable exceptions, not silent fallbacks to tmux. The point of the backend split is explicit backend selection and explicit failure.

One related service patch is worth doing in the same phase: replace the `tail -n` subprocess in `inbox_service._get_log_tail()` (`src/cli_agent_orchestrator/services/inbox_service.py:42-55`) with a pure-Python tail reader. Otherwise the WezTerm backend still depends on Unix tooling on Windows.

## 5. Per-provider patches
### `claude_code.py`
Inspected:
- idle / waiting / trust / bypass regexes (`src/cli_agent_orchestrator/providers/claude_code.py:46-52`)
- startup prompt handler (`src/cli_agent_orchestrator/providers/claude_code.py:180-236`)
- init snapshot logic (`src/cli_agent_orchestrator/providers/claude_code.py:238-290`)
- status parser (`src/cli_agent_orchestrator/providers/claude_code.py:326-389`)

Patch judgment:
- Regex patch: none for MVP. Spike 4 showed `IDLE_PROMPT_PATTERN` and `TRUST_PROMPT_PATTERN` match plain WezTerm capture; `BYPASS_PROMPT_PATTERN` was absent because the settings-based bypass already suppresses it most of the time.
- Code patch: yes. Remove direct tmux access in `_handle_startup_prompts()` and route both actions through the new multiplexer API. The logic should otherwise stay verbatim.

### `codex.py`
Inspected:
- prompt/footer/waiting/trust/welcome patterns (`src/cli_agent_orchestrator/providers/codex.py:18-65`)
- command builder (`src/cli_agent_orchestrator/providers/codex.py:130-213`)
- trust prompt handler (`src/cli_agent_orchestrator/providers/codex.py:215-248`)
- warm-up + init (`src/cli_agent_orchestrator/providers/codex.py:250-281`)
- status parser (`src/cli_agent_orchestrator/providers/codex.py:283-386`)

Patch judgment:
- Regex patch: probably none for MVP once launch is fixed. Spike 4's Codex misses were against the crashed process, not a live TUI.
- Code patch: yes.
  - keep current regex/status logic unchanged first
  - replace the trust-prompt Enter path with the multiplexer abstraction instead of `tmux_client.server.sessions...pane.send_keys("", enter=True)` (`src/cli_agent_orchestrator/providers/codex.py:233-240`)
  - add a WezTerm launch-spec path for Codex-on-Windows so the backend can direct-spawn the explicit shim
  - keep the warm-up echo for tmux; for WezTerm direct-spawned Codex, skip shell warm-up and wait on welcome/trust markers instead

### `gemini_cli.py`
Inspected:
- idle/welcome/responding patterns (`src/cli_agent_orchestrator/providers/gemini_cli.py:63-138`)
- command builder and `GEMINI.md`/settings writes (`src/cli_agent_orchestrator/providers/gemini_cli.py:191-250` and surrounding method)
- warm-up/init (`src/cli_agent_orchestrator/providers/gemini_cli.py:417-509`)
- status parser (`src/cli_agent_orchestrator/providers/gemini_cli.py:520-610`)

Patch judgment:
- Regex patch: none proposed now. Phase 1 did not get a live Gemini WezTerm capture on this machine, so there is no evidence of regex breakage; spike 03's plain-output finding argues to leave the patterns alone until a real runtime proves otherwise.
- Backend patch: defer Gemini WezTerm wiring from MVP. Gemini is explicitly allowed to slip by the task brief, and its startup path is already the most stateful provider because it writes `GEMINI.md`, edits `~/.gemini/settings.json`, and distinguishes post-init IDLE vs COMPLETED (`src/cli_agent_orchestrator/providers/gemini_cli.py:163-189`, `:476-592`).

## 6. Test strategy
Phase 3 verification should have three layers.

Unit and contract tests:
- Add `test/clients/test_base_multiplexer.py`-style contract tests for the shared `send_keys()` behavior: one paste call, delayed submit, `enter_count` honored.
- Clone/retarget current tmux tests (`test/clients/test_tmux_client.py`, `test/clients/test_tmux_send_keys.py`) so `TmuxMultiplexer` proves zero regression.
- Add WezTerm backend unit tests with a mocked CLI runner for spawn/send/get-text/kill and poller diff behavior.
- Replace `tail` subprocess assumptions in `test/services/test_inbox_service.py` with pure-Python tailing so Windows CI is possible.

Provider unit tests:
- Re-run existing provider suites with minimal fixture churn:
  - `test/providers/test_claude_code_unit.py`
  - `test/providers/test_codex_provider_unit.py`
  - `test/providers/test_gemini_cli_unit.py`
  - `test/providers/test_permission_prompt_detection.py`
- Add WezTerm-specific tests only where provider code changes:
  - Claude startup prompt handler uses `send_special_key(..., literal=True)` instead of raw tmux subprocess
  - Codex launch-spec generation on Windows resolves shim + `hooks=[]`

Real smoke tests:
- gated, opt-in tests on a Windows runner with WezTerm installed
- at minimum:
  - spawn shell pane, send text, get text, kill pane
  - Claude startup/trust prompt acceptance
  - Codex direct spawn via resolved `codex.cmd`, send pasted text, separate Enter submission
  - inbox delivery through the poller-backed `pipe_pane()` path at 500 ms
- Existing E2E paths worth reusing after backend parameterization:
  - `test/e2e/test_send_message.py`
  - `test/e2e/test_cross_provider.py`
  - `test/e2e/test_supervisor_orchestration.py`

The clean parameterization point is the multiplexer singleton import, not the providers. If tests can swap `tmux_client` for a generic `multiplexer_client`, most provider fixtures should survive intact.

## 7. LoC + day estimate
| Component | Lines added | Lines moved | Days |
|---|---:|---:|---:|
| `BaseMultiplexer` + `LaunchSpec` + backend selection shim | 140 | 0 | 0.75 |
| `TmuxMultiplexer` refactor | 70 | 320 | 0.5 |
| `WezTermMultiplexer` core spawn/send/get-text/kill/session registry | 260 | 0 | 1.5 |
| WezTerm poller-backed `pipe_pane` + pure-Python log tailing | 140 | 30 | 0.75 |
| Claude provider de-tmuxing | 35 | 10 | 0.5 |
| Codex provider launch-spec + trust-path changes | 80 | 15 | 0.75 |
| Tests and smoke harness | 220 | 20 | 1.25 |
| Total, Claude + Codex MVP | 945 | 395 | 6.0 |

Stretch:
- Gemini WezTerm MVP: +120-180 added LoC, +0.75-1.0 day after the binary/runtime blocker is gone.

## 8. Risks
1. Per-provider regex drift that spike 03/04 did not hit. Claude plain-output matches are encouraging, but Codex was only validated after fixing launch, and Gemini still lacks live WezTerm proof.
2. Codex `hooks=[]` shim can go stale if upstream Codex config loading changes. The workaround is explicitly machine-sensitive from spike 2b, so it needs a backend override slot rather than being baked blindly into the generic provider command builder.
3. WezTerm CLI surface can change across versions. The spike used `wezterm 20260331-040028-577474d8`; Phase 3 should pin the commands used and validate them against at least one more release.
4. Gemini-on-Windows PATH/runtime remains blocked on the target machine. Even if the backend abstraction is correct, Gemini MVP wiring is not testable until the executable is reachable.
5. Poller-backed `pipe_pane()` can regress inbox responsiveness or duplicate log output if snapshot diffing is naive. Spike 3 says 500 ms is viable, but the implementation still has to handle redraw-heavy TUIs and pane clears.
6. Windows-native support is still incomplete if Unix subprocess assumptions survive elsewhere. `inbox_service` calling `tail` is the obvious current example (`src/cli_agent_orchestrator/services/inbox_service.py:42-55`).

## 9. Out of scope
- Layer 2 marc-hq meta-observer orchestration.
- Non-tmux, non-WezTerm backends.
- Gemini WezTerm MVP wiring unless the binary/runtime blocker clears quickly during Phase 3.
- UX-only tmux attachment commands in CLI/API paths (`attach-session`, `display-message`) beyond what is needed to keep the core agent orchestration working.
