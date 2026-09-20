"""Per-request caller identity for the shared MCP endpoint (#745).

The stdio MCP server is one process per agent, so ``CAO_TERMINAL_ID`` in the
process environment unambiguously names the caller. A shared HTTP endpoint
serves many agents from one process, so a process-global env var cannot
represent "who is calling" — the issue names this exact anti-pattern.

This module holds a ``ContextVar`` scoped to one in-flight request. The HTTP
transport's middleware sets it from an authenticated request header; every
identity read goes through :func:`resolve_caller_terminal_id`, which prefers
the per-request value and falls back to the process env only when no request
context is active (the stdio path, byte-for-byte unchanged).

The header names the terminal; it does not by itself authorize it. Hosting is
gated by a shared runtime token (validated in ``http_app``), matching the
runtime-channel model, until #774 supplies per-caller delegated credentials
whose verified subject replaces the header entirely.
"""

import contextvars
import os
import re
from typing import Optional

_TERMINAL_ID_PATTERN = re.compile(r"^[a-f0-9]{8}$")

# Header the shared HTTP endpoint reads the caller's terminal id from. Only
# trusted once the request has passed the shared-token gate.
CALLER_TERMINAL_HEADER = "x-cao-caller-terminal-id"

_caller_terminal_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "cao_caller_terminal_id", default=None
)


class CallerIdentityError(ValueError):
    """A supplied caller terminal id is malformed."""


def _validate(terminal_id: str) -> str:
    if not _TERMINAL_ID_PATTERN.fullmatch(terminal_id):
        raise CallerIdentityError(
            "Invalid caller terminal id: expected an 8-character lowercase hexadecimal id"
        )
    return terminal_id


def set_caller_terminal_id(terminal_id: Optional[str]) -> contextvars.Token:
    """Bind the caller terminal id for the current request. Returns a reset token."""
    value = _validate(terminal_id) if terminal_id else None
    return _caller_terminal_id.set(value)


def reset_caller_terminal_id(token: contextvars.Token) -> None:
    _caller_terminal_id.reset(token)


def resolve_caller_terminal_id() -> Optional[str]:
    """The caller's terminal id: per-request context first, then process env.

    A pure lookup — it does not validate the env value, preserving the existing
    leniency of the direct-env callers (``_own_terminal_id_or_error`` and the
    discovery/metadata impls). The per-request value was already validated at
    :func:`set_caller_terminal_id` time (the HTTP header path), and callers that
    require a well-formed id (``_current_terminal_id``) validate on top of this.
    ``None`` means no caller identity is available.
    """
    ctx_value = _caller_terminal_id.get()
    if ctx_value is not None:
        return ctx_value
    return os.environ.get("CAO_TERMINAL_ID") or None
