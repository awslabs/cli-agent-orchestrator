"""HTTP calls from the ``cao`` CLI process to its own cao-server, with the bearer attached.

The CLI commands (``launch``, ``session``, ``terminal``, ``workflow``, ``info``,
``shutdown``) reach the API over loopback HTTP. When the auth layer is on (an
IdP, or a standalone ``CAO_AUTH_LOCAL_TOKEN``), every scope-gated route wants
``Authorization: Bearer <token>``; the MCP servers and the orchestration
helpers already forward ``CAO_AUTH_LOCAL_TOKEN`` for their internal hop, but
the CLI sent nothing and got a bare ``401`` (#807). This module is the one
place the CLI's ``get``/``post``/``delete`` go through -- the command modules
and the polling helpers in ``utils.terminal`` alike -- so the header is
attached once, and only for URLs that name this node. It lives in ``utils``
rather than ``cli`` because ``utils.terminal`` needs it and must not import
the CLI package.

Same surface as the parts of ``requests`` the commands use, so a command
module can ``from cli_agent_orchestrator.utils import api_http`` and call
``api_http.get(...)`` exactly as it called ``requests.get(...)``; tests patch
``cli.commands.<module>.api_http.<verb>`` and see the call unchanged, because
the header is added inside the verb, not at the call site.

Default-off: with no token in the CLI's environment no header is attached
and the request is byte-for-byte what it was. Whether the SERVER wants a
bearer is not something this process can see in its own environment (the
server's auth mode lives in the server's), so the signal is the response: a
``401`` from this node's API on a request that carried no bearer becomes
:class:`AuthNotConfiguredError`, a ``RequestException`` the commands' existing
error handling prints, carrying the actionable message instead of the bare
status. A ``401`` on a request that DID carry a token is the caller's to
handle (wrong token, wrong scopes, expired).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import requests
from requests import Response, exceptions  # noqa: F401  (re-exported for the commands)

from cli_agent_orchestrator.constants import API_BASE_URL
from cli_agent_orchestrator.security.auth import get_local_bearer

__all__ = [
    "NO_TOKEN_MESSAGE",
    "AuthNotConfiguredError",
    "Response",
    "auth_headers_for",
    "delete",
    "exceptions",
    "get",
    "is_local_api",
    "post",
]


class AuthNotConfiguredError(requests.exceptions.RequestException):
    """The server wants a bearer and the CLI had none to send.

    Raised in place of a bare ``401`` from this node's API when the request
    went out without an ``Authorization`` header, i.e. ``CAO_AUTH_LOCAL_TOKEN``
    is not set in the CLI's environment (or auth is off here and on there).
    """


def is_local_api(url: str) -> bool:
    """True when ``url`` is on this node's own cao-server.

    The same rule as ``utils.orchestration._is_local_api``, kept here rather
    than imported because that module pulls in the orchestration stack a
    short-lived CLI has no reason to load: an exact compare of the base URL
    against ``API_BASE_URL`` after trailing-slash normalisation. A differently
    spelled self-reference (``localhost`` for ``127.0.0.1``) fails closed, no
    bearer, rather than leaking the token on a guess. The test suite asserts
    the two spellings agree.
    """
    base = API_BASE_URL.rstrip("/")
    return url == base or url.startswith(base + "/")


def auth_headers_for(url: str) -> Dict[str, str]:
    """``Authorization`` header for ``url``, or ``{}``.

    Attached only when auth is enabled, a local token is configured, and
    ``url`` is this node's API. ``CAO_AUTH_LOCAL_TOKEN`` is this node's
    loopback credential; it is never sent anywhere else.
    """
    if not is_local_api(url):
        return {}
    token = get_local_bearer()
    return {"Authorization": f"Bearer {token}"} if token else {}


NO_TOKEN_MESSAGE = (
    "The cao-server requires authentication (401) and CAO_AUTH_LOCAL_TOKEN is not set "
    "in this shell, so the cao CLI sent no credential. Export the same "
    "CAO_AUTH_LOCAL_TOKEN the server was started with (local-token mode), or a "
    "machine token from the IdP with the scopes this command needs, then retry."
)


def _send(method: str, url: str, **kwargs: Any) -> Response:
    headers: Dict[str, str] = dict(kwargs.pop("headers", None) or {})
    for key, value in auth_headers_for(url).items():
        headers.setdefault(key, value)
    # ``requests.<verb>`` rather than ``requests.request`` so a test that patches
    # ``requests.get`` globally still intercepts the call.
    verb = getattr(requests, method.lower())
    response = verb(url, headers=headers or None, **kwargs)
    sent_bearer = any(k.lower() == "authorization" for k in headers)
    if response.status_code == 401 and is_local_api(url) and not sent_bearer:
        raise AuthNotConfiguredError(NO_TOKEN_MESSAGE, response=response)
    return response


def get(url: str, **kwargs: Any) -> Response:
    return _send("GET", url, **kwargs)


def post(url: str, **kwargs: Any) -> Response:
    return _send("POST", url, **kwargs)


def delete(url: str, **kwargs: Any) -> Response:
    return _send("DELETE", url, **kwargs)
