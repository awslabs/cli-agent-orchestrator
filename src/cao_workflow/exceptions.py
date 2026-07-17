"""Shim exception hierarchy (E3, BR-1/BR-4/BR-6/BR-17).

Never caught by the shim itself — every failure raises to the author's own
``except`` block or crashes the script (no retry, no recovery, no silent
fallback).
"""

from __future__ import annotations


class ShimError(Exception):
    """Base for all cao_workflow-raised errors.

    Also raised DIRECTLY (not only as a base class) when
    ``run_step(..., reuse_terminal_id=...)`` is called — BR-17 — since that
    combination is a guaranteed server-side 422 given the shim's env_vars
    payload, so the shim rejects it client-side before any HTTP attempt.
    """


class ShimIdentityError(ShimError):
    """CAO_WORKFLOW_RUN_ID / GENERATION / CAO_API_BASE_URL missing (BR-1).

    Names only the missing var(s) — never echoes a present value.
    """


class ShimTransportError(ShimError):
    """Wraps a urllib URLError/timeout — no retry attempted (BR-5)."""


class ShimHTTPError(ShimError):
    """Non-200 response. Carries .status and .body verbatim for author diagnosis."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"run-step returned HTTP {status}")
