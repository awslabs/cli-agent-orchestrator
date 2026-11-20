"""The error envelope and the codes the API contract allows.

``message`` is written by this app, never copied from CAO, so it cannot carry terminal output or
message bodies.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

HTTP_STATUS: Dict[str, int] = {
    "cao_unreachable": 502,
    "cao_timeout": 504,
    "cao_auth_required": 502,
    "cao_incompatible": 502,
    "cao_error": 502,
    "not_found": 404,
    "not_waiting": 409,
    "conflict_changed": 409,
    "plan_expired": 409,
    "invalid_request": 400,
    "forbidden_host": 400,
    "too_many": 400,
    "internal": 500,
    # remote control
    "no_conductor": 409,
    "relay_exists": 409,
    "relay_cao_mismatch": 409,
    "risk_not_acknowledged": 400,
    "gateway_unavailable": 503,
    "gateway_refused": 502,
}

RETRYABLE = frozenset({"cao_unreachable", "cao_timeout", "gateway_unavailable"})

# Failures that mean "CAO is not answering at all": a caller should stop sending more requests.
TRANSPORT_CODES = frozenset({"cao_unreachable", "cao_timeout", "cao_auth_required"})


class ShelfError(Exception):
    """An error that maps onto one HTTP status and one ``errors.schema.json`` envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        detail: Optional[Dict[str, Any]] = None,
        http_status: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        if code not in HTTP_STATUS:
            raise ValueError(f"unknown error code {code!r}")
        self.code = code
        self.message = message[:300]
        self.detail = detail
        self.http_status = http_status if http_status is not None else HTTP_STATUS[code]

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE

    def inner(self) -> Dict[str, Any]:
        """The ``error`` object, also embedded in ``cao.error`` of the shelf."""
        out: Dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.detail:
            out["detail"] = self.detail
        return out

    def envelope(self) -> Dict[str, Any]:
        return {"error": self.inner()}
