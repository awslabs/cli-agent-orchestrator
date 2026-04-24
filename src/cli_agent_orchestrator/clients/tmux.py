"""Deprecated re-export shim for the legacy TmuxClient location.

The real implementation now lives in
``cli_agent_orchestrator.multiplexers.tmux``. This shim keeps existing
imports working until Task 4 wires the runtime backend selector.
"""

from __future__ import annotations

import sys
from types import ModuleType

from cli_agent_orchestrator.multiplexers import tmux as _tmux_module
from cli_agent_orchestrator.multiplexers.tmux import TmuxMultiplexer

libtmux = _tmux_module.libtmux
logger = _tmux_module.logger
subprocess = _tmux_module.subprocess
time = _tmux_module.time
uuid = _tmux_module.uuid

TmuxClient = TmuxMultiplexer

# Singleton kept for backwards compatibility with module-level imports.
tmux_client = TmuxMultiplexer()


class _ShimModule(ModuleType):
    """Keep legacy monkeypatch targets synchronized with the real module."""

    _SYNCED_ATTRS = {"libtmux", "logger", "subprocess", "time", "uuid"}

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name in self._SYNCED_ATTRS:
            setattr(_tmux_module, name, value)


sys.modules[__name__].__class__ = _ShimModule

__all__ = [
    "TmuxClient",
    "TmuxMultiplexer",
    "libtmux",
    "logger",
    "subprocess",
    "time",
    "tmux_client",
    "uuid",
]
