"""Provider-native built-in macros (Compact/Stop) for the §5.4 visible set.

**Lane A stub seam.**  §5.5 synthesizes built-ins from the §4 provider-control
registry (Lane A's ``services/provider_controls.py``), which has not merged on
this base.  Until it lands, this module supplies the registry data *exactly*
as pinned in §3.5's ``provider_controls`` block — no invented keys, events,
or providers — behind one function, :func:`builtin_macros_for_provider`.
When Lane A merges, integration re-points that single function at the real
registry; the macro store, the routes, and every test keep working unchanged.

Built-ins are synthesized, never persisted (D6): immutability is structural
— there is nothing on disk to overwrite — and duplicating one mints a real
user record.  IDs are deterministic and namespaced (§5.5):
``builtin:<provider>:compact`` / ``builtin:<provider>:stop``.  The
``builtin:`` prefix is reserved; the store rejects user records carrying it.
"""

from typing import Any, Dict, List, Optional

BUILTIN_ID_PREFIX = "builtin:"

# The §3.5 provider_controls registry subset the built-ins are synthesized
# from.  Providers with no registry entry (codex and all others on this
# base) synthesize no built-ins; the dashboard hides them and states why
# (§13, OD3).  Only the compact/stop control data lives here — steer chords
# and dispatch grace are capture/pacing facts the client reads from the
# advertised capabilities, not macro data.
_PROVIDER_BUILTIN_CONTROLS: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
    "kimi_cli": {
        "compact": [
            {"type": "text", "text": "/compact"},
            {"type": "key", "key": "Enter"},
        ],
        "stop": [{"type": "key", "key": "Escape"}],
    },
    "claude_code": {
        "compact": [
            {"type": "text", "text": "/compact"},
            {"type": "key", "key": "Enter"},
        ],
        "stop": [{"type": "key", "key": "Escape"}],
    },
}

# Display metadata for the two built-ins.  Compact is command-class (§4.1):
# its send declares payload_class "command" once the server advertises the
# command_controls block; Stop is a bare key and never declares.
_BUILTIN_LABELS = {
    "compact": {"name": "Compact", "description": "Provider-native /compact"},
    "stop": {"name": "Stop", "description": "Interrupt the current turn (Escape)"},
}


def builtin_macro_id(provider: str, kind: str) -> str:
    """The deterministic built-in ID: ``builtin:<provider>:<kind>``."""
    return f"{BUILTIN_ID_PREFIX}{provider}:{kind}"


def builtin_macros_for_provider(provider: Optional[str]) -> List[Dict[str, Any]]:
    """Synthesize the §5.5 built-ins for one provider (registry data only).

    Each record carries ``origin: "builtin"``, ``mutable: False``,
    ``favorite: True`` (built-ins sort first in resolution order and cannot
    be un-favorited — duplicating one makes a user macro that can), and the
    provider scope group.  A provider with no registry entry yields nothing.
    """
    if not provider:
        return []
    controls = _PROVIDER_BUILTIN_CONTROLS.get(provider)
    if not controls:
        return []
    builtins: List[Dict[str, Any]] = []
    for kind in ("compact", "stop"):
        labels = _BUILTIN_LABELS[kind]
        builtins.append(
            {
                "id": builtin_macro_id(provider, kind),
                "name": labels["name"],
                "description": labels["description"],
                "scope": {"kind": "provider", "provider": provider},
                "events": [dict(event) for event in controls[kind]],
                "favorite": True,
                "origin": "builtin",
                "mutable": False,
                "builtin_kind": kind,
                "created_at": None,
                "updated_at": None,
            }
        )
    return builtins


def resolve_builtin(macro_id: str) -> Optional[Dict[str, Any]]:
    """Resolve a deterministic built-in ID back to its synthesized record.

    ``POST /macros/{id}/duplicate`` uses this so a built-in id fetched from a
    list response resolves to the same built-in at duplicate time (§5.5 ID
    stability).  Unknown or malformed ids return ``None``.
    """
    if not isinstance(macro_id, str) or not macro_id.startswith(BUILTIN_ID_PREFIX):
        return None
    remainder = macro_id[len(BUILTIN_ID_PREFIX) :]
    provider, sep, kind = remainder.rpartition(":")
    if not sep or not provider or kind not in _BUILTIN_LABELS:
        return None
    for record in builtin_macros_for_provider(provider):
        if record["id"] == macro_id:
            return record
    return None
