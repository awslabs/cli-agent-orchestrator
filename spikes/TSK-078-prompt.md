# TSK-078 — Phase 2 Task 8: Codex LaunchSpec on Windows + WezTerm direct spawn

You are executing Phase 2 Task 8 of PRJ-042 (aws-cao WezTerm port). Self-contained prompt — no prior context.

## Repo state
- Working dir: `C:\dev\aws-cao`, branch `wezterm-multiplexer` (clean tree at task start; other parallel tasks may modify disjoint files concurrently — DO NOT TOUCH `src/cli_agent_orchestrator/multiplexers/wezterm.py`, `src/cli_agent_orchestrator/services/terminal_service.py`, or their tests).
- Tasks 1–6 + audit committed. `WezTermMultiplexer` already consumes `LaunchSpec.argv` via `wezterm cli spawn -- <argv>` (Task 5).
- Plan binding spec: `docs/PLAN-phase2.md` §4 ("Launch command templating and Codex-on-Windows" — read it), §5 codex.py "Patch judgment".
- Phase 1 spike 2b: `spikes/02b-codex-launch.md` for the precise shim path + flag combo that worked on marcwin.

## Goal

1. Add a small backend-owned launch-template helper that builds a `LaunchSpec` for a provider on a given platform.
2. Wire Codex provider to construct a `LaunchSpec` on Windows that points at the explicit Scoop/Node `codex.cmd` shim with `-c hooks=[]` (the hooks override that Phase 1 found load-bearing) plus the existing `--yolo --no-alt-screen --disable shell_snapshot` flags.
3. When the multiplexer is WezTerm and a direct-spawn `LaunchSpec` is in use, skip the shell warm-up echo (there's no shell to echo through). Wait on welcome/trust markers instead.
4. Tmux ignores LaunchSpec.argv and continues shelling in for parity (Task 2 already preserved this; verify).

## Implementation requirements

### Helper: `build_launch_spec`

Add `src/cli_agent_orchestrator/multiplexers/launch.py`:

```python
from __future__ import annotations
import os
import shutil
import sys
from typing import Literal, Sequence
from cli_agent_orchestrator.multiplexers.base import LaunchSpec


def build_launch_spec(
    provider: str,
    command_argv: Sequence[str],
    *,
    platform: Literal["windows", "unix"] | None = None,
    working_directory: str | None = None,
) -> LaunchSpec:
    """Resolve a LaunchSpec for a provider on the current (or stated) platform.

    `command_argv[0]` is treated as the bare command name to resolve.
    The remaining elements are passed through verbatim.

    Resolver order (Windows):
      1. explicit ``CAO_<PROVIDER>_BIN`` env override
      2. ``where.exe <name>.cmd`` lookup (Scoop/Node shim discovery)
      3. fall back to bare ``command_argv[0]``

    On non-Windows: trust shell PATH (use ``command_argv[0]`` verbatim).
    """
```

Implementation rules:
- Default `platform`: `"windows"` if `sys.platform == "win32"` else `"unix"`.
- Default providers (claude, gemini, etc.) — pass `command_argv` through, no resolver. Return `LaunchSpec(argv=tuple(command_argv), provider=provider)`.
- Codex on Windows — apply the resolver. Concretely:
  - Look up `CAO_CODEX_BIN` env var first.
  - Else `shutil.which("codex.cmd")` (use that, NOT `which("codex")`).
  - Else fall back to scanning known Scoop paths (`os.path.expandvars(r"%LOCALAPPDATA%\..\scoop\apps\nodejs-lts\current\bin\codex.cmd")`, `r"C:\Users\<user>\scoop\apps\nodejs-lts\current\bin\codex.cmd"`).
  - If still not found, return `LaunchSpec(argv=tuple(command_argv), provider="codex")` (degraded — caller will see the spawn error).
- Codex on Unix — pass-through.
- Use `tuple(...)` not `list(...)` for `argv` so `LaunchSpec` stays hashable per Task 1's frozen-dataclass design.

### Codex provider patches (`src/cli_agent_orchestrator/providers/codex.py`)

1. In the existing command builder (around lines 130-213), the existing `--yolo --no-alt-screen --disable shell_snapshot` flags stay. Add `-c hooks=[]` to the argv on Windows. Concretely: when `sys.platform == "win32"`, prepend `["-c", "hooks=[]"]` to the existing flags. Do NOT add it on non-Windows.
2. In the provider `initialize()` flow (where `tmux_client.send_keys(self.session_name, self.window_name, "echo ready")` warm-up happens — see line ~252-267 from the audit), branch on backend:
   - When the multiplexer is `WezTermMultiplexer` AND a `LaunchSpec.argv` was used to direct-spawn the process, skip the shell warm-up echo entirely. Wait on the existing welcome/trust markers via the existing `get_history` polling instead.
   - When the multiplexer is `TmuxMultiplexer` (default), keep the warm-up echo unchanged.
   - Detect the multiplexer type via `isinstance(tmux_client, WezTermMultiplexer)` — DO NOT call `get_multiplexer()`; keep the existing `tmux_client` import path so this commit is minimal and Task 9 owns the broader rewire.
   - Detect "direct-spawned via LaunchSpec" by checking whether the provider was started with a launch spec — store a boolean instance attr `self._direct_spawned: bool` set during `__init__` or wherever the spec is constructed. Default False.
3. Where the provider currently kicks off the process, build the `LaunchSpec` via `build_launch_spec("codex", base_argv, ...)` and persist `self._direct_spawned = True` when on Windows + WezTerm.

### Worked example

```text
wezterm cli spawn --new-window --cwd C:\dev\aws-cao --set-environment CAO_TERMINAL_ID=test1234 -- \
  C:\Users\marc\scoop\apps\nodejs-lts\current\bin\codex.cmd \
  -c hooks=[] --yolo --no-alt-screen --disable shell_snapshot
```

This is the exact shape spike 2b validated. Your `build_launch_spec("codex", ...)` output, when fed into `WezTermMultiplexer.create_session(... launch_spec=...)`, must produce that argv.

## Tests

Add/update `test/providers/test_codex_provider_unit.py`:

1. `build_launch_spec("codex", ["codex"], platform="windows")` returns a `LaunchSpec` whose `argv[0]` is the resolved `codex.cmd` path (mock `shutil.which`).
2. `build_launch_spec("codex", ["codex"], platform="windows")` falls through to bare name when no shim is found.
3. `build_launch_spec("codex", ["codex"], platform="unix")` returns the bare name unchanged.
4. `build_launch_spec("claude", ["claude"], platform="windows")` returns the bare name unchanged (no Codex-specific resolver for other providers).
5. The Codex command-builder produces `-c hooks=[]` when platform is windows; does NOT produce it on unix.
6. Codex `initialize()` skips the warm-up echo when `isinstance(tmux_client, WezTermMultiplexer)` AND `self._direct_spawned` is True (assert `tmux_client.send_keys` not called for warm-up).
7. Codex `initialize()` runs warm-up echo when `tmux_client` is `TmuxMultiplexer` (existing behavior preserved).

For tests #6/#7, use `monkeypatch.setattr` on `cli_agent_orchestrator.providers.codex.tmux_client` to inject a fake of the appropriate class.

## Constraints (HARD)

- DO NOT touch `src/cli_agent_orchestrator/multiplexers/wezterm.py` or its test (Task 7 is concurrent on those files).
- DO NOT touch `src/cli_agent_orchestrator/services/terminal_service.py` or its test (Task 9 is concurrent on those files).
- DO NOT migrate Codex provider's `from cli_agent_orchestrator.clients.tmux import tmux_client` line to `get_multiplexer()` — Task 9 owns the broader services rewire; minimize blast radius.
- DO NOT modify other providers (claude, gemini, copilot, q, opencode, kimi, kiro).
- DO NOT install dependencies.
- Use `.venv\Scripts\python.exe -m pytest`.
- DO NOT commit.

## Verification (TARGETED ONLY — supervising Opus runs combined regression)

```
.venv/Scripts/python.exe -m pytest test/providers/test_codex_provider_unit.py test/providers/test_claude_code_unit.py test/multiplexers/ -v --tb=short
```

Do NOT run the broad `test/clients/ test/multiplexers/ test/providers/ test/services/ test/utils/` suite — three Codex tasks are running in parallel and that race will produce false regressions.

## Reporting

Write `spikes/TSK-078-result.md`:

```markdown
# TSK-078 — Task 8 result

## Files touched
<list>

## build_launch_spec behavior verified
<table: provider × platform → argv[0] / extra flags>

## Codex initialize() warm-up branching
<table: multiplexer × direct_spawned → warm-up runs?>

## Tests
- targeted (codex+claude unit + multiplexers): <N pass / M fail>

## Deviations
<any>
```

Echo: `TSK-078: PASS|FAIL — <reason>`.

DO NOT commit. Stop after Task 8.

Begin.
