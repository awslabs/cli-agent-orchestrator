"""HTTP transport — the shim's one call site (A5, BR-8, BR-9).

stdlib ``urllib`` only, no third-party HTTP client, no connection pooling, no
retry. The socket timeout carries a fixed slack over the caller's/base
timeout so the shim's own socket never races the server's legitimate
long-poll response.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Shim-local tuning constant — lives here, not in the server's constants.py,
# because the shim must not import from cli_agent_orchestrator.* (BR-2).
_TRANSPORT_SLACK = 30.0

_BASE_TIMEOUT_DEFAULT = 600.0


@dataclass(frozen=True)
class _Response:
    status: int
    body: str


def _post(url: str, body: dict, timeout: "float | None" = None) -> _Response:
    """POST ``body`` as JSON to ``url``. One fresh socket, no retry.

    Raises ``URLError`` (including ``socket.timeout``, a URLError subclass)
    unchanged on transport failure — the caller (``run_step``) wraps it into
    ``ShimTransportError``.
    """
    base_timeout = timeout if timeout is not None else _BASE_TIMEOUT_DEFAULT
    socket_timeout = base_timeout + _TRANSPORT_SLACK
    data = json.dumps(body).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(
            request, timeout=socket_timeout
        ) as response:  # noqa: S310 — fixed internal URL
            response_body = response.read().decode("utf-8")
            return _Response(status=response.getcode(), body=response_body)
    except HTTPError as e:
        # A non-2xx status — urllib raises this rather than returning it, but
        # it is a server response (not a transport failure), so it surfaces
        # as a normal _Response for run_step to turn into ShimHTTPError, not
        # ShimTransportError.
        return _Response(status=e.code, body=e.read().decode("utf-8"))


__all__ = ["_post", "_Response", "_TRANSPORT_SLACK", "URLError"]
