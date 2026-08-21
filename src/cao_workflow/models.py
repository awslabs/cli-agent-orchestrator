"""StepHandle — the return value of BOTH ``step()`` and ``run_step()`` (E1)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StepHandle:
    """Lightweight, immutable result of one ``step()`` or ``run_step()`` call.

    A shim-local wrapper around the server's ``RunStepResponse`` body — not a
    redefinition of that wire contract.
    """

    step_id: str
    terminal_id: str
    output: Any
    status: str
    replayed: bool = False
    """True when the server REPLAYED a stored result instead of executing
    (issue #583, FR-1/BR-2).

    Load-bearing rather than cosmetic, and it qualifies ``terminal_id``: a
    replayed response carries the ORIGINAL id, which names a terminal that no
    longer exists, so this flag is the only thing that stops a caller reading,
    writing to, or waiting on a dead id. Mirrors ``RunStepResponse.replayed``
    exactly, including the ``False`` default — so a hand-constructed
    ``StepHandle`` (an author's own test) keeps working unchanged (BR-10).
    """
