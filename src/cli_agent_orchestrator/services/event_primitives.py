"""Semantic event normalization for the CAO MCP App event stream.

CAO emits internal lifecycle events whose ``event_type`` strings
(``post_create_terminal``, ``post_send_message``, ...) are an implementation
detail of the orchestration core. The MCP App event-stream view, however,
renders a *stable* governance ticker expressed in a small, closed vocabulary
of six semantic primitives. This module is the single, pure mapping between
the two.

``normalize_kind`` is **total**: every possible string input maps to exactly
one value in the closed return set and the function never raises. Lifecycle
event types with no explicit mapping pass through as the reserved value
``"other"`` so the ring buffer never silently drops an event — the iframe
groups unknowns under "other" rather than losing them.
"""

from typing import Dict, Optional

# Closed vocabulary the MCP App renders. Mirrors the ``CaoEvent.kind`` union in
# cao_mcp_apps/src/shared/types.ts. ``"other"`` is the reserved pass-through
# value for unmapped lifecycle event types and is intentionally *not* a member
# of PRIMITIVES (it is a sentinel, not a first-class primitive).
PRIMITIVES = ("launch", "handoff", "a2a_delegation", "file_mod", "completion", "error")

# The full closed set ``normalize_kind`` may return: the six primitives plus the
# reserved pass-through sentinel.
KINDS = PRIMITIVES + ("other",)

# Map a CAO lifecycle ``event_type`` -> semantic primitive. ``post_send_message``
# is deliberately absent here: it is disambiguated by ``orchestration_type`` in
# ``normalize_kind`` rather than mapped to a single static primitive.
_DOMAIN_TO_PRIMITIVE: Dict[str, str] = {
    "post_create_terminal": "launch",
    "post_create_session": "launch",
    "post_kill_terminal": "completion",
    "post_kill_session": "completion",
}


def normalize_kind(event_type: str, detail: Optional[dict] = None) -> str:
    """Map a CAO lifecycle ``event_type`` to exactly one semantic primitive.

    Total function: for *any* string input it returns exactly one value from
    ``{launch, handoff, a2a_delegation, file_mod, completion, error, other}``
    and never raises.

    ``post_send_message`` is disambiguated by the event detail's
    ``orchestration_type``:

    - the value contains ``"a2a"``  -> ``"a2a_delegation"``
    - otherwise (assign/handoff/send_message) -> ``"handoff"``

    Args:
        event_type: The CAO lifecycle event type string.
        detail: Optional event metadata; only ``orchestration_type`` is read,
            and only for ``post_send_message``.

    Returns:
        One member of ``{launch, handoff, a2a_delegation, file_mod, completion,
        error, other}``.
    """
    # Defensive coercion keeps the function total even if a non-str slips in
    # (never raise on any input).
    if not isinstance(event_type, str):
        return "other"

    if event_type == "post_send_message":
        meta = detail or {}
        orchestration_type = str(meta.get("orchestration_type", "")).lower()
        return "a2a_delegation" if "a2a" in orchestration_type else "handoff"

    return _DOMAIN_TO_PRIMITIVE.get(event_type, "other")
