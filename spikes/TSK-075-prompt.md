# TSK-075 — Phase 2 Task 4: backend selection accessor

You are executing Phase 2 Task 4 of PRJ-042 (aws-cao WezTerm port). Self-contained prompt — no prior context.

## Repo state
- Working dir: `C:\dev\aws-cao`, branch `wezterm-multiplexer` (clean tree).
- Tasks 1, 2, 3, 6 + audit (TSK-071) committed. `BaseMultiplexer`, `LaunchSpec`, `TmuxMultiplexer` exist.
- Plan binding spec: `docs/PLAN-phase2.md` §7 ("backend selection shim", 140 LoC bucket).
- Live evidence on the supervising shell — TMUX env conventions confirmed:
  - tmux sets `TMUX` env var (canonical)
  - WezTerm sets `WEZTERM_PANE`, `WEZTERM_EXECUTABLE`, `TERM_PROGRAM=WezTerm`
- Task 5 is running in parallel and will create `src/cli_agent_orchestrator/multiplexers/wezterm.py` with a class named `WezTermMultiplexer` — DO NOT depend on its presence at import time. Use lazy import.

## Goal

Create a `get_multiplexer()` accessor that returns a singleton multiplexer chosen at runtime, plus contract tests covering all branches. Don't break anything if the WezTerm module is absent.

## Selection logic (priority order)

1. **`CAO_MULTIPLEXER` env override** (highest priority). Values: `tmux`, `wezterm`. Anything else raises `ValueError("Unknown CAO_MULTIPLEXER: <value>; expected 'tmux' or 'wezterm'")`.
2. Else if `os.environ.get("TMUX")` is non-empty → tmux.
3. Else if `os.environ.get("WEZTERM_PANE")` is non-empty OR `os.environ.get("TERM_PROGRAM") == "WezTerm"` → wezterm.
4. Else platform default: `sys.platform == "win32"` → wezterm; otherwise tmux.

## Implementation requirements

In `src/cli_agent_orchestrator/multiplexers/__init__.py`, add:

```python
from __future__ import annotations
import os
import sys
from functools import lru_cache
from typing import Literal

from cli_agent_orchestrator.multiplexers.base import BaseMultiplexer, LaunchSpec
from cli_agent_orchestrator.multiplexers.tmux import TmuxMultiplexer

_BackendName = Literal["tmux", "wezterm"]


def _select_backend() -> _BackendName:
    override = os.environ.get("CAO_MULTIPLEXER", "").strip().lower()
    if override:
        if override not in ("tmux", "wezterm"):
            raise ValueError(
                f"Unknown CAO_MULTIPLEXER: {override!r}; expected 'tmux' or 'wezterm'"
            )
        return override  # type: ignore[return-value]
    if os.environ.get("TMUX"):
        return "tmux"
    if os.environ.get("WEZTERM_PANE") or os.environ.get("TERM_PROGRAM") == "WezTerm":
        return "wezterm"
    return "wezterm" if sys.platform == "win32" else "tmux"


@lru_cache(maxsize=1)
def get_multiplexer() -> BaseMultiplexer:
    """Return the process-singleton multiplexer for the current environment."""
    backend = _select_backend()
    if backend == "tmux":
        return TmuxMultiplexer()
    # Lazy import: WezTermMultiplexer module may not exist yet during dev,
    # and we don't want tmux-only environments to fail import on missing
    # wezterm support.
    from cli_agent_orchestrator.multiplexers.wezterm import WezTermMultiplexer
    return WezTermMultiplexer()


__all__ = ["BaseMultiplexer", "LaunchSpec", "TmuxMultiplexer", "get_multiplexer"]
```

Note: do NOT eagerly export `WezTermMultiplexer` from `__all__` at the package level — leave it accessible via `from cli_agent_orchestrator.multiplexers.wezterm import WezTermMultiplexer` only. This isolates Task 5's not-yet-committed module from this commit's import surface.

## Tests

Create `test/multiplexers/test_selection.py` covering:

1. `CAO_MULTIPLEXER=tmux` → returns TmuxMultiplexer (regardless of other env vars).
2. `CAO_MULTIPLEXER=wezterm` → tries wezterm (mock the import to return a sentinel; assert the sentinel is returned).
3. Invalid `CAO_MULTIPLEXER=foo` → raises `ValueError`.
4. No override, `TMUX=/tmp/tmux-1000/default,1234,0` → tmux.
5. No override, no TMUX, `WEZTERM_PANE=66` → wezterm (mocked).
6. No override, no TMUX, `TERM_PROGRAM=WezTerm` → wezterm (mocked).
7. No override, no env signals, `sys.platform == "win32"` → wezterm (mocked).
8. No override, no env signals, `sys.platform == "linux"` → tmux.
9. `lru_cache` returns same instance on second call.
10. `lru_cache` is invalidated between tests (use `get_multiplexer.cache_clear()` in fixture or autouse fixture).

For #2/#5/#6/#7, mock the wezterm import using `monkeypatch.setattr` on `sys.modules["cli_agent_orchestrator.multiplexers.wezterm"]` with a fake module exposing `WezTermMultiplexer = <sentinel>`, OR use `monkeypatch.setattr` on the function-level import via patching `__import__`. Pick whichever is simpler.

For env-var manipulation, use `monkeypatch.setenv` / `monkeypatch.delenv`. Always clear `CAO_MULTIPLEXER`, `TMUX`, `WEZTERM_PANE`, `TERM_PROGRAM` before each test (autouse fixture).

For `sys.platform`, use `monkeypatch.setattr(sys, "platform", "win32")`.

## Constraints (HARD)

- DO NOT modify `base.py`, `tmux.py`, or any provider/service file.
- DO NOT create `wezterm.py` — Task 5 owns that.
- DO NOT eagerly import `wezterm` at module load time.
- DO NOT install or upgrade dependencies.
- Use `.venv\Scripts\python.exe -m pytest` (project's `rtk pytest` shim collects nothing).
- DO NOT commit. Produce a clean working-tree change for the supervising Opus to commit.

## Verification

```
.venv/Scripts/python.exe -m pytest test/multiplexers/test_selection.py -v --tb=short
.venv/Scripts/python.exe -m pytest test/clients/ test/multiplexers/ test/providers/ test/services/ test/utils/ --ignore=test/e2e -q --tb=no --no-header
```

Second run failure count must not exceed 43.

## Reporting

Write `spikes/TSK-075-result.md`:

```markdown
# TSK-075 — Task 4 result

## Files touched
<list>

## Selection branches verified
<table: env vars / platform → backend>

## Tests
- test_selection.py: <N pass / M fail>
- full (excl. e2e): <N pass / M fail> — must be ≤43

## Lazy-import behavior
Confirm: `import cli_agent_orchestrator.multiplexers` works without Task 5's wezterm.py present. WezTerm only resolved when `get_multiplexer()` actually picks the wezterm branch.

## Deviations
<any>
```

Echo: `TSK-075: PASS|FAIL — <reason>`.

DO NOT commit. Stop after Task 4.

Begin.
