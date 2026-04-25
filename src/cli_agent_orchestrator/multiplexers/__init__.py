"""Multiplexer abstraction layer for CAO."""

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
        return override
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

    # Lazy import: tmux-only environments avoid loading the WezTerm module.
    from cli_agent_orchestrator.multiplexers.wezterm import WezTermMultiplexer

    return WezTermMultiplexer()


__all__ = ["BaseMultiplexer", "LaunchSpec", "TmuxMultiplexer", "get_multiplexer"]
