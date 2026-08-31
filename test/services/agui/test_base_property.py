"""Hypothesis property tests for the AguiConstruct base layer.

P7: Emit refusal parity - any component not in the allow-list is always refused.
P8: Subclass totality/serializability with fuzzed frames.
P5: Privacy at the base seam - assert_no_body always catches body fields.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from cli_agent_orchestrator.services.agui.base import (
    AguiConstruct,
    RecordingUiEmitter,
    apply_json_patch_strict,
)
from cli_agent_orchestrator.services.agui_stream import (
    _MAX_GENERATIVE_PROPS_BYTES,
    GENERATIVE_UI_COMPONENTS,
)

# ---------------------------------------------------------------------------
# Minimal concrete subclass for property tests
# ---------------------------------------------------------------------------


class _PropTestConstruct(AguiConstruct):
    """A construct that silently accepts all frames and tracks count."""

    def __init__(self, emitter):
        super().__init__(emitter)
        self._count = 0

    def handle_frame(
        self, agui_type: str, data: Dict[str, Any], event_id: Optional[str] = None
    ) -> None:
        self._count += 1

    def projection(self) -> Dict[str, Any]:
        return {"count": self._count}


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# JSON-safe leaf values (no NaN/Inf which are not JSON-serializable).
json_leaf = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**53), max_value=2**53),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=50),
)

# Small JSON-serializable dicts.
json_props = st.dictionaries(
    keys=st.text(min_size=1, max_size=10),
    values=json_leaf,
    max_size=5,
)

# Component names: mix of valid and invalid.
valid_components = st.sampled_from(sorted(GENERATIVE_UI_COMPONENTS))
invalid_components = st.text(min_size=1, max_size=30).filter(
    lambda s: s not in GENERATIVE_UI_COMPONENTS
)

# AG-UI type strings (mix of known and unknown).
agui_types = st.one_of(
    st.sampled_from(
        [
            "RUN_STARTED",
            "RUN_FINISHED",
            "STEP_STARTED",
            "STEP_FINISHED",
            "TEXT_MESSAGE_CONTENT",
            "STATE_DELTA",
            "STATE_SNAPSHOT",
            "GENERATIVE_UI",
            "RAW",
            "RUN_ERROR",
            "TOOL_CALL_START",
        ]
    ),
    st.text(min_size=1, max_size=30),
)


# ===========================================================================
# P7: Emit refusal parity
# ===========================================================================


class TestP7EmitRefusalParity:
    """Any component not in GENERATIVE_UI_COMPONENTS is always refused."""

    @given(component=invalid_components, props=json_props)
    @settings(max_examples=50)
    def test_off_list_always_refused(self, component: str, props: dict):
        rec = RecordingUiEmitter()
        c = _PropTestConstruct(rec)
        with pytest.raises(ValueError, match="not in the allow-list"):
            c.emit(component, props)
        assert len(rec.intents) == 0

    @given(component=valid_components, props=json_props)
    @settings(max_examples=50)
    def test_on_list_with_small_props_always_accepted(self, component: str, props: dict):
        """Valid component + small serializable props always passes."""
        # Ensure props are within size limit.
        encoded_size = len(json.dumps(props).encode("utf-8"))
        assume(encoded_size <= _MAX_GENERATIVE_PROPS_BYTES)
        rec = RecordingUiEmitter()
        c = _PropTestConstruct(rec)
        c.emit(component, props)
        assert len(rec.intents) == 1
        assert rec.intents[0]["component"] == component


# ===========================================================================
# P8: Subclass totality/serializability with fuzzed frames
# ===========================================================================


class TestP8SubclassTotality:
    """Subclass handle_frame never crashes on arbitrary types; projection is serializable."""

    @given(
        agui_type=agui_types, data=json_props, event_id=st.one_of(st.none(), st.text(max_size=20))
    )
    @settings(max_examples=100)
    def test_handle_frame_never_raises(self, agui_type: str, data: dict, event_id):
        rec = RecordingUiEmitter()
        c = _PropTestConstruct(rec)
        # Should never raise regardless of input.
        c.handle_frame(agui_type, data, event_id)
        assert c._count == 1

    @given(
        frames=st.lists(
            st.tuples(agui_types, json_props, st.one_of(st.none(), st.text(max_size=10))),
            max_size=10,
        )
    )
    @settings(max_examples=30)
    def test_projection_always_serializable(self, frames):
        rec = RecordingUiEmitter()
        c = _PropTestConstruct(rec)
        for agui_type, data, event_id in frames:
            c.handle_frame(agui_type, data, event_id)
        proj = c.projection()
        # Must be JSON-serializable.
        serialized = json.dumps(proj)
        assert isinstance(json.loads(serialized), dict)


# ===========================================================================
# P5: Privacy at the base seam
# ===========================================================================


class TestP5Privacy:
    """assert_no_body always catches body fields with non-empty string values."""

    @given(
        field=st.sampled_from(["delta", "content", "message_body", "stdout"]),
        body=st.text(min_size=1, max_size=100),
    )
    @settings(max_examples=50)
    def test_non_empty_body_always_caught(self, field: str, body: str):
        data = {"terminal_id": "t-1", field: body}
        with pytest.raises(ValueError, match=field):
            AguiConstruct.assert_no_body(data)

    @given(
        field=st.sampled_from(["delta", "content", "message_body", "stdout"]),
        value=st.one_of(st.just(""), st.none(), st.integers(), st.lists(st.text(), max_size=3)),
    )
    @settings(max_examples=50)
    def test_non_body_values_never_trigger(self, field: str, value):
        """Empty strings, None, integers, lists never trigger the body check."""
        data = {field: value}
        # Should not raise.
        AguiConstruct.assert_no_body(data)


# ===========================================================================
# Bonus: apply_json_patch_strict immutability property
# ===========================================================================


class TestPatchImmutability:
    """apply_json_patch_strict never mutates the input doc."""

    @given(
        key=st.text(min_size=1, max_size=10).filter(lambda s: "/" not in s and "~" not in s),
        value=json_leaf,
    )
    @settings(max_examples=30)
    def test_add_never_mutates_original(self, key: str, value):
        doc = {"existing": "data", "nested": {"a": 1}}
        import copy

        original = copy.deepcopy(doc)
        ops = [{"op": "add", "path": f"/{key}", "value": value}]
        apply_json_patch_strict(doc, ops)
        assert doc == original
