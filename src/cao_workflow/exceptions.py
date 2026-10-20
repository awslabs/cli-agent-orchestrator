"""Shim exception hierarchy (E3, and the original WorkflowShim design's
BR-1/BR-4/BR-6/BR-17 — NOT issue #583's ``shim-step-surface`` rules of the same
numbers, whose BR-8 is the recovery key rather than anything here).

Never caught by the shim itself — every failure raises to the author's own
``except`` block or crashes the script (no retry, no recovery, no silent
fallback).

Note for authors: ``ShimHTTPError`` carries a resume HALT and a resume
DIVERGENCE as well as ordinary failures — both arrive as ``.status == 409``
(issue #583). Because it subclasses ``ShimError``, a blanket
``except ShimError`` absorbs them; see the authoring guide.
"""

from __future__ import annotations

import json


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
    """Non-200 response. Carries .status and .body verbatim for author diagnosis.

    A non-empty string ``detail.kind`` renders beside the status, followed by a
    non-empty string ``detail.message`` when present; otherwise the exact fallback
    is ``run-step returned HTTP <status>``. The original ``.body`` is never altered.
    """

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        message = f"run-step returned HTTP {status}"
        try:
            payload = json.loads(body)
        except (ValueError, TypeError, RecursionError):
            # Invalid JSON, a non-string body, or excessive nesting all use the fallback.
            payload = None
        if isinstance(payload, dict):
            detail = payload.get("detail")
            if isinstance(detail, dict):
                kind = detail.get("kind")
                if isinstance(kind, str) and kind:
                    message += f" ({kind})"
                    detail_message = detail.get("message")
                    if isinstance(detail_message, str) and detail_message:
                        message += f": {detail_message}"
        super().__init__(message)
