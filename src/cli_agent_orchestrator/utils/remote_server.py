"""Explicit shared-server selection for CLI commands (#745).

Remote mode is an explicit opt-in: set ``CAO_API_BASE_URL`` to the shared
cao-server's base URL (for example ``https://cao.example.com:9889``). When a
shared target is selected, CLI commands whose state is owned by the server
(``cao schedule``, ``cao memory``) must read and change it through the HTTP
API — never a client-local database — and operations that require the
server's filesystem or tmux fail with an explicit error instead of silently
operating on unrelated local state.

Local behavior is unchanged when ``CAO_API_BASE_URL`` is unset: commands keep
using ``CAO_API_HOST``/``CAO_API_PORT`` (default ``127.0.0.1:9889``) and their
existing local-service paths.
"""

import os
from typing import Optional

import click
import requests

#: Optional bearer token for servers that enforce API scopes.
API_TOKEN_ENV = "CAO_API_TOKEN"

_REQUEST_TIMEOUT = 30


def remote_base_url() -> Optional[str]:
    """The explicitly selected shared-server base URL, or None for local mode."""
    url = os.environ.get("CAO_API_BASE_URL", "").strip()
    return url.rstrip("/") or None


def is_remote_server() -> bool:
    return remote_base_url() is not None


def require_local(operation: str) -> None:
    """Fail explicitly when *operation* cannot run against a shared server.

    #745: "unsupported cluster operations are explicit rather than silently
    misrouted" — an operation that needs the server's filesystem, database
    file, or tmux socket must not fall back to client-local state while a
    shared target is selected.
    """
    base = remote_base_url()
    if base is not None:
        raise click.ClickException(
            f"'{operation}' is not supported against a shared server ({base}). "
            "It requires the server's local filesystem or terminal backend. "
            "Unset CAO_API_BASE_URL to operate on this machine's CAO instance."
        )


def api_token() -> Optional[str]:
    """The configured bearer token, or None. For the WS attach query param."""
    return os.environ.get(API_TOKEN_ENV, "").strip() or None


def auth_headers() -> dict:
    """Authorization header for the selected server, empty when no token is set.

    Public because not every call can go through :func:`api_request`: commands
    that assemble their own request (``cao launch``) still need the credential,
    and a hand-rolled call that omits it fails only on a server with auth
    enabled — the configuration least likely to be the one under test.
    """
    token = api_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def server_base_url() -> str:
    """The base URL every CLI request should use: shared target, else local.

    Resolved per call, so a ``CAO_API_BASE_URL`` exported after the process
    started still wins over the import-time constant.
    """
    from cli_agent_orchestrator.constants import API_BASE_URL

    return remote_base_url() or API_BASE_URL


def api_request(method: str, path: str, **kwargs) -> requests.Response:
    """One HTTP call to the selected server with uniform error surfacing.

    Raises click.ClickException on connection errors or non-2xx responses,
    with the server's ``detail`` message when present.
    """
    base = server_base_url()
    kwargs.setdefault("timeout", _REQUEST_TIMEOUT)
    headers = {**auth_headers(), **kwargs.pop("headers", {})}
    try:
        response = requests.request(method, f"{base}{path}", headers=headers, **kwargs)
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"Failed to reach cao-server at {base}: {e}")
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise click.ClickException(
            f"{method.upper()} {path} failed ({response.status_code}): {detail}"
        )
    return response
