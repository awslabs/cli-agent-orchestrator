"""Scope pre-check decorator for MCP tool implementations.

``requires_scopes(*scopes)`` wraps a tool impl so the required scopes are
checked against the local-token scope set *before* the impl runs. It is the
decorator form of the ``submit_command`` choke-point pre-check (a UX gate; the
FastAPI ``Depends(get_current_scopes)`` boundary is the real enforcement point).

Default-off: with ``AUTH0_DOMAIN`` / ``CAO_AUTH_JWKS_URI`` unset,
``get_scopes_for_local_token`` returns the full taxonomy, so every required
scope is present and the check always passes.

Works for both sync and async callables. On denial it returns a structured
``{"success": False, "error": ...}`` result rather than raising, matching the
tool-result shape the iframe expects.
"""

import functools
import inspect
from typing import Any, Callable, List

from cli_agent_orchestrator.security.auth import get_scopes_for_local_token


def _missing_scope(required: List[str]) -> Any:
    granted = get_scopes_for_local_token()
    # A non-empty granted set missing a required scope blocks (auth on); the full
    # set (auth off, the default) grants everything.
    if not granted:
        return required[0] if required else None
    for scope in required:
        if scope not in granted:
            return scope
    return None


def requires_scopes(*scopes: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Return a decorator that pre-checks ``scopes`` before running the impl."""

    required = list(scopes)

    def _decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
                missing = _missing_scope(required)
                if missing is not None:
                    return {"success": False, "error": f"scope {missing} required"}
                return await fn(*args, **kwargs)

            return _async_wrapper

        @functools.wraps(fn)
        def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            missing = _missing_scope(required)
            if missing is not None:
                return {"success": False, "error": f"scope {missing} required"}
            return fn(*args, **kwargs)

        return _sync_wrapper

    return _decorator
