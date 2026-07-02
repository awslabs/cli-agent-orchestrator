"""Pure snapshot projection and RFC 6902 diff for the MCP App dashboard.

This module is a **pure projection layer**: it turns the raw rows the
App_Tool_Layer fetches from the Backplane over HTTP (``/sessions`` and the
per-session ``/terminals`` listings) into the stable ``DashboardSnapshot`` shape
the iframe renders, and computes the minimal RFC 6902 JSON Patch between two
snapshots so the client can apply small deltas instead of re-rendering.

It performs **no I/O and has no side effects** — every function is a deterministic
mapping from its arguments to a new value. The HTTP fetching lives in
``mcp_server/app_tools.py``; this module only shapes and diffs what it is given,
which keeps it trivially unit-testable and keeps the HTTP-only boundary intact
(it imports nothing from ``clients.*``).

Snapshot shape (kept in sync with ``cao_mcp_apps/src/shared/types.ts``):

    DashboardSnapshot = {
        "sessions":  [SessionView, ...],   # whole-key replaced on any change
        "terminals": [TerminalView, ...],  # whole-key replaced on any change
        "counts":    {"sessions": int, "terminals": int},  # per-key replaced
        "scopes":    [str, ...],           # granted scope set (default-off => full)
    }

Diff granularity (per the design):

* ``terminals`` / ``sessions`` are **whole-key replaced** — a single ``replace``
  op carrying the entire new array when anything inside changes. Collections
  re-order and churn often; a coarse replace is cheaper to compute and apply
  than a per-element patch and avoids brittle index math.
* scalar / nested-scalar keys (``counts``, ``scopes``) are **per-key replaced**
  so a change to one counter does not resend the whole object.
"""

from typing import Any, Dict, List, Optional

# Top-level snapshot keys whose value is replaced wholesale (never diffed
# element-by-element) when anything inside them changes.
_WHOLE_KEY_REPLACE = frozenset({"terminals", "sessions"})


def _escape_token(token: str) -> str:
    """Escape a single JSON Pointer reference token (RFC 6901 section 3).

    ``~`` -> ``~0`` and ``/`` -> ``~1``. The ``~`` substitution must run first
    so an already-substituted ``~1`` is not double-escaped.
    """

    return token.replace("~", "~0").replace("/", "~1")


def build_dashboard_snapshot(
    sessions: List[Dict[str, Any]],
    terminals: List[Dict[str, Any]],
    scopes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Project raw Backplane rows into the ``DashboardSnapshot`` shape.

    Args:
        sessions: Rows from ``GET /sessions`` — each at least ``{"id", "name",
            "status"}``.
        terminals: Flattened terminal rows (e.g. aggregated from the per-session
            ``GET /sessions/{name}/terminals`` listings) — each at least
            ``{"id", "tmux_session", "provider", "agent_profile"}`` and
            optionally ``"status"`` / ``"tmux_window"`` / ``"last_active"``.
        scopes: The granted scope set. With auth off this is the full taxonomy
            (so the UI shows every control); ``None`` is treated as empty.

    Returns:
        A new ``DashboardSnapshot`` dict. Deterministic and side-effect free.
    """

    session_views = [
        {
            "id": s.get("id", ""),
            "name": s.get("name", s.get("id", "")),
            "status": s.get("status", "unknown"),
        }
        for s in sessions
    ]

    terminal_views = [
        {
            "id": t.get("id", ""),
            # ``session_name`` is the public field name; the DB row calls it
            # ``tmux_session``. Accept either so this works against both the
            # ``/terminals/{id}`` Terminal model and the raw DB listing.
            "session_name": t.get("session_name", t.get("tmux_session", "")),
            "provider": t.get("provider", ""),
            "agent_profile": t.get("agent_profile"),
            "window": t.get("tmux_window", t.get("name")),
            "status": t.get("status"),
            "last_active": _isoformat(t.get("last_active")),
        }
        for t in terminals
    ]

    return {
        "sessions": session_views,
        "terminals": terminal_views,
        "counts": {
            "sessions": len(session_views),
            "terminals": len(terminal_views),
        },
        "scopes": list(scopes) if scopes else [],
    }


def build_agent_detail_snapshot(
    terminal: Dict[str, Any],
    scopes: Optional[List[str]] = None,
    output_tail: Optional[str] = None,
) -> Dict[str, Any]:
    """Project a single terminal's data into the per-agent detail snapshot.

    Args:
        terminal: A terminal row from ``GET /terminals/{id}`` (the Terminal
            model) or the DB listing.
        scopes: The granted scope set (full taxonomy when auth is off).
        output_tail: Optional tail of the terminal's rendered output. Bodies of
            *messages* are never included here — this is terminal stdout the
            operator is already authorized to view in the host.

    Returns:
        A new agent-detail snapshot dict. Deterministic and side-effect free.
    """

    return {
        "terminal_id": terminal.get("id", ""),
        "session_name": terminal.get("session_name", terminal.get("tmux_session", "")),
        "provider": terminal.get("provider", ""),
        "agent_profile": terminal.get("agent_profile"),
        "status": terminal.get("status"),
        "last_active": _isoformat(terminal.get("last_active")),
        "output_tail": output_tail or "",
        "scopes": list(scopes) if scopes else [],
    }


def diff_snapshot(prev: Dict[str, Any], curr: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Compute the RFC 6902 JSON Patch transforming ``prev`` into ``curr``.

    Applying the returned ops to ``prev`` yields a value deep-equal to ``curr``
    (RFC-6902 round-trip).

    Granularity:

    * keys in ``_WHOLE_KEY_REPLACE`` (``terminals`` / ``sessions``) emit a single
      whole-value ``replace`` when changed;
    * nested dict keys (e.g. ``counts``) are diffed **per-key**;
    * other scalars emit a per-key ``replace``;
    * keys only in ``curr`` are ``add``ed; keys only in ``prev`` are ``remove``d.
    """

    ops: List[Dict[str, Any]] = []
    for key in sorted(set(prev) | set(curr)):
        pointer = "/" + _escape_token(key)
        in_prev = key in prev
        in_curr = key in curr

        if in_prev and not in_curr:
            ops.append({"op": "remove", "path": pointer})
        elif in_curr and not in_prev:
            ops.append({"op": "add", "path": pointer, "value": curr[key]})
        elif prev[key] == curr[key]:
            continue
        elif key in _WHOLE_KEY_REPLACE:
            ops.append({"op": "replace", "path": pointer, "value": curr[key]})
        elif isinstance(prev[key], dict) and isinstance(curr[key], dict):
            ops.extend(_diff_dict(prev[key], curr[key], pointer))
        else:
            ops.append({"op": "replace", "path": pointer, "value": curr[key]})
    return ops


def _diff_dict(
    prev: Dict[str, Any], curr: Dict[str, Any], base_pointer: str
) -> List[Dict[str, Any]]:
    """Per-key add/remove/replace diff of a nested dict (one level, recursive)."""

    ops: List[Dict[str, Any]] = []
    for key in sorted(set(prev) | set(curr)):
        pointer = f"{base_pointer}/{_escape_token(key)}"
        in_prev = key in prev
        in_curr = key in curr

        if in_prev and not in_curr:
            ops.append({"op": "remove", "path": pointer})
        elif in_curr and not in_prev:
            ops.append({"op": "add", "path": pointer, "value": curr[key]})
        elif prev[key] == curr[key]:
            continue
        elif isinstance(prev[key], dict) and isinstance(curr[key], dict):
            ops.extend(_diff_dict(prev[key], curr[key], pointer))
        else:
            ops.append({"op": "replace", "path": pointer, "value": curr[key]})
    return ops


def _isoformat(value: Any) -> Optional[str]:
    """Coerce a datetime-or-str ``last_active`` into an ISO-8601 string or None."""

    if value is None:
        return None
    if isinstance(value, str):
        return value
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)
