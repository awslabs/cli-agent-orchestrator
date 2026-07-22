"""AguiConstruct base class, emitter family, and apply_json_patch_strict helper.

This module defines the foundation layer for all AG-UI L2 constructs:

- ``AguiConstruct``: the abstract base class every construct inherits from.
- ``UiEmitter``: the protocol (structural subtyping) for emit transport.
- ``InProcessUiEmitter``, ``HttpUiEmitter``, ``RecordingUiEmitter``: concrete
  emitter implementations for different deployment shapes.
- ``apply_json_patch_strict``: a pure RFC-6902 subset helper (add/remove/replace)
  that never mutates its inputs.
"""

from __future__ import annotations

import copy
import json
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Protocol, Tuple

from cli_agent_orchestrator.services.agui_stream import (
    _MAX_GENERATIVE_PROPS_BYTES,
    GENERATIVE_UI_COMPONENTS,
)

# Fields whose presence (with a non-empty string value) in a frame data dict
# indicates that the frame carries a message body. Constructs that only handle
# metadata must refuse such frames via ``assert_no_body``.
_BODY_FIELDS = frozenset({"delta", "content", "message_body", "stdout"})


# ---------------------------------------------------------------------------
# Emitter protocol and implementations
# ---------------------------------------------------------------------------


class UiEmitter(Protocol):
    """Structural protocol for generative-UI emit transport."""

    def emit_intent(
        self,
        component: str,
        props: Dict[str, Any],
        terminal_id: Optional[str] = None,
        session_name: Optional[str] = None,
    ) -> None:
        """Publish a validated UI intent to the appropriate transport."""
        ...  # pragma: no cover


class InProcessUiEmitter:
    """Emit by appending to the in-process EventLog and publishing on the SseBus.

    Refuses to emit when the AG-UI surface is disabled (raises ``RuntimeError``).
    """

    def emit_intent(
        self,
        component: str,
        props: Dict[str, Any],
        terminal_id: Optional[str] = None,
        session_name: Optional[str] = None,
    ) -> None:
        from cli_agent_orchestrator.services.agui_enablement import agui_surface_enabled
        from cli_agent_orchestrator.services.event_log_service import get_event_log
        from cli_agent_orchestrator.services.sse_bus import get_bus

        if not agui_surface_enabled():
            raise RuntimeError("AG-UI surface is disabled; cannot emit UI intent")

        event_log = get_event_log()
        detail: Dict[str, Any] = {"ui": {"component": component, "props": props}}
        record = event_log.append(
            kind="other",
            terminal_id=terminal_id,
            session_name=session_name,
            detail=detail,
        )
        get_bus().publish(record)


class HttpUiEmitter:
    """Emit by POSTing to the ``/agui/v1/emit_ui`` endpoint.

    Maps HTTP 400 responses to ``ValueError`` so the caller sees the same
    exception as a local validation failure.
    """

    def __init__(self, base_url: str, access_token: Optional[str] = None) -> None:
        self._url = f"{base_url.rstrip('/')}/agui/v1/emit_ui"
        self._token = access_token

    def emit_intent(
        self,
        component: str,
        props: Dict[str, Any],
        terminal_id: Optional[str] = None,
        session_name: Optional[str] = None,
    ) -> None:
        import requests

        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        payload: Dict[str, Any] = {"component": component, "props": props}
        if terminal_id is not None:
            payload["terminal_id"] = terminal_id
        if session_name is not None:
            payload["session_name"] = session_name

        resp = requests.post(self._url, json=payload, headers=headers, timeout=10)
        if resp.status_code == 400:
            raise ValueError(f"Server refused emit: {resp.text}")
        resp.raise_for_status()


class RecordingUiEmitter:
    """Records all emitted intents in a list without publishing anywhere.

    Useful for testing constructs in isolation.
    """

    def __init__(self) -> None:
        self.intents: List[Dict[str, Any]] = []

    def emit_intent(
        self,
        component: str,
        props: Dict[str, Any],
        terminal_id: Optional[str] = None,
        session_name: Optional[str] = None,
    ) -> None:
        self.intents.append(
            {
                "component": component,
                "props": props,
                "terminal_id": terminal_id,
                "session_name": session_name,
            }
        )


# ---------------------------------------------------------------------------
# apply_json_patch_strict: pure RFC-6902 subset (add/remove/replace)
# ---------------------------------------------------------------------------


def _resolve_pointer(doc: Any, path: str) -> Tuple[Any, str]:
    """Resolve a JSON Pointer to (parent_container, final_key/index).

    Raises KeyError/IndexError/TypeError on invalid paths.
    """
    if path == "":  # pragma: no cover - invariant: root ops short-circuit before this
        raise ValueError("Cannot resolve empty pointer to parent/key")
    parts = path.lstrip("/").split("/")
    # Unescape JSON Pointer ~1 and ~0 per RFC 6901.
    parts = [p.replace("~1", "/").replace("~0", "~") for p in parts]
    parent = doc
    for part in parts[:-1]:
        if isinstance(parent, list):
            parent = parent[int(part)]
        else:
            parent = parent[part]
    return parent, parts[-1]


def apply_json_patch_strict(
    doc: Dict[str, Any], ops: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Apply a list of RFC-6902 ops (add/remove/replace only) to a deep copy of *doc*.

    Returns the patched document on success, or ``None`` on any failure
    (invalid op, bad path, type mismatch). The input *doc* is NEVER mutated.
    """
    try:
        result = copy.deepcopy(doc)
        for op in ops:
            action = op.get("op")
            path = op.get("path", "")

            if action == "add":
                value = op["value"]
                if path == "":
                    # Replace the entire document (RFC 6902 corner case).
                    result = copy.deepcopy(value)
                    continue
                parent, key = _resolve_pointer(result, path)
                if isinstance(parent, list):
                    idx = len(parent) if key == "-" else int(key)
                    parent.insert(idx, copy.deepcopy(value))
                else:
                    parent[key] = copy.deepcopy(value)

            elif action == "remove":
                if path == "":
                    return None  # Cannot remove root
                parent, key = _resolve_pointer(result, path)
                if isinstance(parent, list):
                    del parent[int(key)]
                else:
                    del parent[key]

            elif action == "replace":
                value = op["value"]
                if path == "":
                    result = copy.deepcopy(value)
                    continue
                parent, key = _resolve_pointer(result, path)
                if isinstance(parent, list):
                    idx = int(key)
                    parent[idx] = copy.deepcopy(value)
                else:
                    if key not in parent:
                        return None  # replace requires existing key
                    parent[key] = copy.deepcopy(value)

            else:
                # Unsupported op
                return None
        return result
    except (KeyError, IndexError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# BoundedSeen: insertion-ordered dedup set with a hard size cap
# ---------------------------------------------------------------------------

# Default cap for per-construct dedup sets. On a long-lived stream an unbounded
# ``set`` of event ids grows without limit (a memory leak); BoundedSeen evicts
# the oldest ~half once this cap is exceeded while preserving dedup correctness
# for recent ids.
_SEEN_CAP = 10_000


class BoundedSeen:
    """An insertion-ordered set of ids with a hard size cap.

    Drop-in replacement for the plain ``set`` the constructs used, supporting
    exactly the two operations they rely on — ``x in seen`` and ``seen.add(x)``.
    Once ``cap`` is exceeded it evicts the oldest ~half (in insertion order) in
    a single pass, so memory is bounded while the most recent ids still dedup
    correctly. Eviction only affects ids old enough that a duplicate is
    vanishingly unlikely on an ordered event stream.
    """

    __slots__ = ("_cap", "_ids")

    def __init__(self, cap: int = _SEEN_CAP) -> None:
        if cap < 2:
            raise ValueError("cap must be >= 2")
        self._cap = cap
        # dict preserves insertion order (CPython 3.7+); values are unused.
        self._ids: Dict[str, None] = {}

    def __contains__(self, item: object) -> bool:
        return item in self._ids

    def __len__(self) -> int:
        return len(self._ids)

    def add(self, item: str) -> None:
        """Record *item*. Evicts the oldest ~half once the cap is exceeded."""
        if item in self._ids:
            return
        self._ids[item] = None
        if len(self._ids) > self._cap:
            # Evict oldest half in insertion order; the just-added item is
            # newest and is always retained.
            evict = len(self._ids) - (self._cap // 2)
            for key in list(self._ids)[:evict]:
                del self._ids[key]


# ---------------------------------------------------------------------------
# AguiConstruct base class
# ---------------------------------------------------------------------------


class AguiConstruct(ABC):
    """Abstract base for all AG-UI L2 constructs.

    Subclasses implement ``handle_frame`` to process incoming AG-UI events and
    ``projection`` to return the current state as a JSON-serializable dict.
    The ``emit`` method validates a UI intent against the allow-list and size
    bounds, then delegates to the configured emitter.
    """

    def __init__(self, emitter: UiEmitter) -> None:
        self._emitter = emitter

    @abstractmethod
    def handle_frame(
        self, agui_type: str, data: Dict[str, Any], event_id: Optional[str] = None
    ) -> None:
        """Process one AG-UI frame.

        Subclasses should handle the types they care about and silently ignore
        unknown types (never raise on an unrecognized ``agui_type``).
        """
        ...  # pragma: no cover

    @abstractmethod
    def projection(self) -> Dict[str, Any]:
        """Return the current construct state as a JSON-serializable dict."""
        ...  # pragma: no cover

    @staticmethod
    def assert_no_body(data: Dict[str, Any]) -> None:
        """Raise ``ValueError`` if *data* contains message-body fields.

        A body field is one of ``delta``, ``content``, ``message_body``, or
        ``stdout`` whose value is a non-empty string.
        """
        for field in _BODY_FIELDS:
            value = data.get(field)
            if isinstance(value, str) and value:
                raise ValueError(
                    f"Frame contains message-body field '{field}'; "
                    f"metadata-only constructs must not process bodies"
                )

    def emit(
        self,
        component: str,
        props: Dict[str, Any],
        terminal_id: Optional[str] = None,
        session_name: Optional[str] = None,
    ) -> None:
        """Validate and emit a generative-UI intent.

        Raises ``ValueError`` if:
        - *component* is not in the allow-list
        - *props* is not JSON-serializable
        - serialized *props* exceeds 8192 bytes

        Props are NEVER mutated; validation uses a serialized copy.
        """
        if component not in GENERATIVE_UI_COMPONENTS:
            raise ValueError(
                f"Component '{component}' is not in the allow-list: "
                f"{sorted(GENERATIVE_UI_COMPONENTS)}"
            )

        try:
            encoded = json.dumps(props).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Props are not JSON-serializable: {exc}") from exc

        if len(encoded) > _MAX_GENERATIVE_PROPS_BYTES:
            raise ValueError(
                f"Serialized props ({len(encoded)} bytes) exceed the "
                f"{_MAX_GENERATIVE_PROPS_BYTES}-byte limit"
            )

        # Delegate to the emitter (never mutate props).
        self._emitter.emit_intent(component, props, terminal_id, session_name)


__all__ = [
    "AguiConstruct",
    "HttpUiEmitter",
    "InProcessUiEmitter",
    "RecordingUiEmitter",
    "UiEmitter",
    "apply_json_patch_strict",
]
