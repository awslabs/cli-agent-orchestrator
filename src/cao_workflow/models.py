"""StepHandle — the run_step() return value (E1)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StepHandle:
    """Lightweight, immutable result of one ``run_step`` call.

    A shim-local wrapper around the server's ``RunStepResponse`` body — not a
    redefinition of that wire contract.
    """

    step_id: str
    terminal_id: str
    output: Any
    status: str
