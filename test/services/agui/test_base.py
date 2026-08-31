"""Unit tests for the AguiConstruct base class, emitters, and apply_json_patch_strict."""

from __future__ import annotations

import copy
import json
import math
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.services.agui.base import (
    AguiConstruct,
    InProcessUiEmitter,
    RecordingUiEmitter,
    apply_json_patch_strict,
)
from cli_agent_orchestrator.services.agui_stream import (
    _MAX_GENERATIVE_PROPS_BYTES,
    GENERATIVE_UI_COMPONENTS,
)

# ---------------------------------------------------------------------------
# Minimal concrete subclass for testing
# ---------------------------------------------------------------------------


class _StubConstruct(AguiConstruct):
    """Minimal concrete construct for testing the base class."""

    def __init__(self, emitter):
        super().__init__(emitter)
        self.frames_received = []

    def handle_frame(
        self, agui_type: str, data: Dict[str, Any], event_id: Optional[str] = None
    ) -> None:
        self.frames_received.append((agui_type, data, event_id))

    def projection(self) -> Dict[str, Any]:
        return {"count": len(self.frames_received)}


# ===========================================================================
# Test emit validation
# ===========================================================================


class TestEmitValidation:
    """Emit validation: allow-list, size boundary, serialization, immutability."""

    def test_emit_valid_component_passes(self):
        rec = RecordingUiEmitter()
        c = _StubConstruct(rec)
        c.emit("metric", {"label": "tokens", "value": 42})
        assert len(rec.intents) == 1
        assert rec.intents[0]["component"] == "metric"
        assert rec.intents[0]["props"] == {"label": "tokens", "value": 42}

    def test_emit_refuses_off_list_component(self):
        rec = RecordingUiEmitter()
        c = _StubConstruct(rec)
        with pytest.raises(ValueError, match="not in the allow-list"):
            c.emit("iframe", {"src": "http://evil.com"})
        assert len(rec.intents) == 0

    def test_emit_refuses_non_serializable_props(self):
        rec = RecordingUiEmitter()
        c = _StubConstruct(rec)
        with pytest.raises(ValueError, match="not JSON-serializable"):
            c.emit("metric", {"value": {1, 2, 3}})  # sets are not JSON-serializable
        assert len(rec.intents) == 0

    def test_emit_refuses_props_with_non_serializable_object(self):
        rec = RecordingUiEmitter()
        c = _StubConstruct(rec)
        with pytest.raises(ValueError, match="not JSON-serializable"):
            c.emit("metric", {"callback": lambda: None})
        assert len(rec.intents) == 0

    def test_emit_exact_boundary_8192_bytes_passes(self):
        """Props at exactly 8192 bytes (UTF-8) should pass."""
        rec = RecordingUiEmitter()
        c = _StubConstruct(rec)
        # Build props whose JSON serialization is exactly 8192 bytes.
        # json.dumps({"x": "..."}) with padding.
        overhead = len(json.dumps({"x": ""}).encode("utf-8"))
        pad_len = _MAX_GENERATIVE_PROPS_BYTES - overhead
        props = {"x": "a" * pad_len}
        assert len(json.dumps(props).encode("utf-8")) == _MAX_GENERATIVE_PROPS_BYTES
        c.emit("metric", props)
        assert len(rec.intents) == 1

    def test_emit_one_byte_over_8192_refused(self):
        """Props at 8193 bytes should be refused."""
        rec = RecordingUiEmitter()
        c = _StubConstruct(rec)
        overhead = len(json.dumps({"x": ""}).encode("utf-8"))
        pad_len = _MAX_GENERATIVE_PROPS_BYTES - overhead + 1
        props = {"x": "a" * pad_len}
        assert len(json.dumps(props).encode("utf-8")) == _MAX_GENERATIVE_PROPS_BYTES + 1
        with pytest.raises(ValueError, match="exceed"):
            c.emit("metric", props)
        assert len(rec.intents) == 0

    def test_emit_does_not_mutate_props(self):
        """The caller's props dict must never be mutated."""
        rec = RecordingUiEmitter()
        c = _StubConstruct(rec)
        props = {"label": "tokens", "value": 42}
        original = copy.deepcopy(props)
        c.emit("metric", props)
        assert props == original

    def test_emit_passes_terminal_id_and_session_name(self):
        rec = RecordingUiEmitter()
        c = _StubConstruct(rec)
        c.emit("metric", {"v": 1}, terminal_id="t-1", session_name="s-1")
        assert rec.intents[0]["terminal_id"] == "t-1"
        assert rec.intents[0]["session_name"] == "s-1"


# ===========================================================================
# Test assert_no_body
# ===========================================================================


class TestAssertNoBody:
    """assert_no_body raises on frames with message-body content."""

    @pytest.mark.parametrize(
        "field",
        ["delta", "content", "message_body", "stdout"],
    )
    def test_raises_on_non_empty_string_body_field(self, field):
        data = {field: "some content here"}
        with pytest.raises(ValueError, match=field):
            AguiConstruct.assert_no_body(data)

    @pytest.mark.parametrize(
        "field",
        ["delta", "content", "message_body", "stdout"],
    )
    def test_allows_empty_string_body_field(self, field):
        # Empty string is not considered a body (per spec: empty delta is metadata).
        data = {field: ""}
        AguiConstruct.assert_no_body(data)  # Should not raise

    def test_allows_non_string_body_field(self):
        # Non-string values (e.g. None, list) are not body indicators.
        data = {"delta": None, "content": [], "stdout": 0}
        AguiConstruct.assert_no_body(data)  # Should not raise

    def test_allows_clean_metadata_frame(self):
        data = {"terminal_id": "t-1", "session_name": "s-1", "kind": "launch"}
        AguiConstruct.assert_no_body(data)  # Should not raise


# ===========================================================================
# Test apply_json_patch_strict
# ===========================================================================


class TestApplyJsonPatchStrict:
    """apply_json_patch_strict: add/remove/replace, failure returns None, immutable."""

    def test_add_new_key(self):
        doc = {"a": 1}
        ops = [{"op": "add", "path": "/b", "value": 2}]
        result = apply_json_patch_strict(doc, ops)
        assert result == {"a": 1, "b": 2}

    def test_add_replaces_existing_key(self):
        doc = {"a": 1}
        ops = [{"op": "add", "path": "/a", "value": 99}]
        result = apply_json_patch_strict(doc, ops)
        assert result == {"a": 99}

    def test_add_to_array(self):
        doc = {"arr": [1, 2, 3]}
        ops = [{"op": "add", "path": "/arr/1", "value": 99}]
        result = apply_json_patch_strict(doc, ops)
        assert result == {"arr": [1, 99, 2, 3]}

    def test_add_to_array_end(self):
        doc = {"arr": [1, 2]}
        ops = [{"op": "add", "path": "/arr/-", "value": 3}]
        result = apply_json_patch_strict(doc, ops)
        assert result == {"arr": [1, 2, 3]}

    def test_remove_key(self):
        doc = {"a": 1, "b": 2}
        ops = [{"op": "remove", "path": "/a"}]
        result = apply_json_patch_strict(doc, ops)
        assert result == {"b": 2}

    def test_remove_from_array(self):
        doc = {"arr": [1, 2, 3]}
        ops = [{"op": "remove", "path": "/arr/1"}]
        result = apply_json_patch_strict(doc, ops)
        assert result == {"arr": [1, 3]}

    def test_replace_existing_key(self):
        doc = {"a": 1}
        ops = [{"op": "replace", "path": "/a", "value": 42}]
        result = apply_json_patch_strict(doc, ops)
        assert result == {"a": 42}

    def test_replace_nonexistent_key_returns_none(self):
        doc = {"a": 1}
        ops = [{"op": "replace", "path": "/missing", "value": 42}]
        result = apply_json_patch_strict(doc, ops)
        assert result is None

    def test_unsupported_op_returns_none(self):
        doc = {"a": 1}
        ops = [{"op": "move", "from": "/a", "path": "/b"}]
        result = apply_json_patch_strict(doc, ops)
        assert result is None

    def test_invalid_path_returns_none(self):
        doc = {"a": 1}
        ops = [{"op": "remove", "path": "/nonexistent"}]
        result = apply_json_patch_strict(doc, ops)
        assert result is None

    def test_input_doc_never_mutated(self):
        doc = {"a": {"nested": [1, 2, 3]}}
        original = copy.deepcopy(doc)
        ops = [
            {"op": "add", "path": "/a/nested/-", "value": 4},
            {"op": "add", "path": "/b", "value": "new"},
        ]
        result = apply_json_patch_strict(doc, ops)
        assert result is not None
        assert result != doc
        assert doc == original  # Input unchanged

    def test_multiple_ops_applied_sequentially(self):
        doc = {"x": 1}
        ops = [
            {"op": "add", "path": "/y", "value": 2},
            {"op": "replace", "path": "/x", "value": 10},
            {"op": "remove", "path": "/y"},
        ]
        result = apply_json_patch_strict(doc, ops)
        assert result == {"x": 10}

    def test_empty_ops_returns_copy(self):
        doc = {"a": 1}
        result = apply_json_patch_strict(doc, [])
        assert result == doc
        assert result is not doc

    def test_add_replaces_root(self):
        doc = {"a": 1}
        ops = [{"op": "add", "path": "", "value": {"b": 2}}]
        result = apply_json_patch_strict(doc, ops)
        assert result == {"b": 2}

    def test_nested_path(self):
        doc = {"a": {"b": {"c": 1}}}
        ops = [{"op": "replace", "path": "/a/b/c", "value": 99}]
        result = apply_json_patch_strict(doc, ops)
        assert result == {"a": {"b": {"c": 99}}}

    def test_json_pointer_escape(self):
        """Handles ~0 and ~1 escapes per RFC 6901."""
        doc = {"a/b": 1, "c~d": 2}
        ops = [{"op": "replace", "path": "/a~1b", "value": 10}]
        result = apply_json_patch_strict(doc, ops)
        assert result == {"a/b": 10, "c~d": 2}

        ops2 = [{"op": "replace", "path": "/c~0d", "value": 20}]
        result2 = apply_json_patch_strict(doc, ops2)
        assert result2 == {"a/b": 1, "c~d": 20}


# ===========================================================================
# Test RecordingUiEmitter
# ===========================================================================


class TestRecordingUiEmitter:
    """RecordingUiEmitter captures all intents without side effects."""

    def test_records_multiple_intents(self):
        rec = RecordingUiEmitter()
        rec.emit_intent("metric", {"v": 1})
        rec.emit_intent("progress", {"pct": 50}, terminal_id="t-1")
        assert len(rec.intents) == 2
        assert rec.intents[0] == {
            "component": "metric",
            "props": {"v": 1},
            "terminal_id": None,
            "session_name": None,
        }
        assert rec.intents[1]["terminal_id"] == "t-1"

    def test_starts_empty(self):
        rec = RecordingUiEmitter()
        assert rec.intents == []


# ===========================================================================
# Test InProcessUiEmitter surface-disabled refusal
# ===========================================================================


class TestInProcessUiEmitterDisabled:
    """InProcessUiEmitter refuses when AG-UI surface is disabled."""

    def test_raises_when_surface_disabled(self, monkeypatch):
        monkeypatch.delenv("CAO_AGUI_ENABLED", raising=False)
        monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "false")
        emitter = InProcessUiEmitter()
        with pytest.raises(RuntimeError, match="disabled"):
            emitter.emit_intent("metric", {"v": 1})

    def test_succeeds_when_surface_enabled(self, monkeypatch):
        monkeypatch.setenv("CAO_AGUI_ENABLED", "true")
        emitter = InProcessUiEmitter()
        # Should not raise; it will append to the event log and bus.
        emitter.emit_intent("metric", {"v": 1})


# ===========================================================================
# Test handle_frame basic contract
# ===========================================================================


class TestHandleFrame:
    """handle_frame: subclass receives frames; unknown types do not crash."""

    def test_receives_known_frame(self):
        rec = RecordingUiEmitter()
        c = _StubConstruct(rec)
        c.handle_frame("RUN_STARTED", {"thread_id": "s-1"}, event_id="e-1")
        assert len(c.frames_received) == 1
        assert c.frames_received[0] == ("RUN_STARTED", {"thread_id": "s-1"}, "e-1")

    def test_unknown_type_does_not_crash(self):
        rec = RecordingUiEmitter()
        c = _StubConstruct(rec)
        # Should silently record the frame without raising.
        c.handle_frame("COMPLETELY_UNKNOWN_TYPE", {"foo": "bar"})
        assert len(c.frames_received) == 1

    def test_projection_returns_serializable_dict(self):
        rec = RecordingUiEmitter()
        c = _StubConstruct(rec)
        c.handle_frame("X", {})
        proj = c.projection()
        assert proj == {"count": 1}
        # Must be JSON-serializable
        json.dumps(proj)
