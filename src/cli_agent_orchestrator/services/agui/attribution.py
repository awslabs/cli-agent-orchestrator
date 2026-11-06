"""AG-UI 1.0 subagent attribution ids for CAO terminals and sessions.

One invocation id per terminal and per session incarnation, shared by every
AG-UI surface so the same terminal carries the same ``subagentRunId`` on the
ambient stream and on a run-plane interrupt. The ids are opaque to consumers
(AG-UI 1.0 basic#identifiers: "a consumer MUST NOT parse structure out of them").

- A terminal's id is derived from the terminal id the stream already carries.
- A session's id is a one-way hash of the session's incarnation id, so a
  relaunched session under the same name gets a new id and the internal
  incarnation id is never exposed.
"""

from __future__ import annotations

import hashlib

_TERMINAL_PREFIX = "terminal:"
_SESSION_PREFIX = "session:"
_SESSION_HASH_CHARS = 16


def subagent_run_id_for_terminal(terminal_id: str) -> str:
    """Return the ``subagentRunId`` for one terminal: ``'terminal:' + terminal_id``."""
    if not terminal_id:
        raise ValueError("terminal_id is required for a subagentRunId")
    return f"{_TERMINAL_PREFIX}{terminal_id}"


def subagent_run_id_for_session(incarnation_id: str) -> str:
    """Return the ``subagentRunId`` for one session incarnation.

    ``'session:' + sha256(incarnation_id)[:16]``; the incarnation id itself is not exposed.
    """
    if not incarnation_id:
        raise ValueError("incarnation_id is required for a subagentRunId")
    digest = hashlib.sha256(incarnation_id.encode("utf-8")).hexdigest()[:_SESSION_HASH_CHARS]
    return f"{_SESSION_PREFIX}{digest}"
