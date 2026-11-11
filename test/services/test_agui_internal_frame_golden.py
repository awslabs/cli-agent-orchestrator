"""Golden pin of the INTERNAL AG-UI frame (the contract other code consumes).

``agui_stream.to_agui_event`` and ``ToolCallLifecycleTracker`` produce the
``(agui_type, data)`` tuples that the in-process L2 constructs and the run plane's
live projection (``run_plane.py`` ``_translate_live_frame``) consume. The ambient
stream's conformant wire encoding is a separate edge layer and must never change
this internal frame. Any drift fails here first.

Pinned: one record per primitive, the generative-UI CUSTOM tuple, the off-list RAW
tuple, one legacy dotted-name envelope, and the tracker's feed/close_all output,
including the a2a TOOL_CALL_RESULT + TOOL_CALL_END pair.

Regenerate (only when an internal change is intended and every consumer is
updated): ``CAO_REGEN_GOLDEN=1 pytest test/services/test_agui_internal_frame_golden.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from test._support.agui_ambient_records import (
    HANDOFF,
    LEGACY_ENVELOPE,
    PRIMITIVE_RECORDS,
    TRACKER_SEQUENCE,
)
from typing import Any, Dict, List

from cli_agent_orchestrator.services.agui.lifecycle_tracker import ToolCallLifecycleTracker
from cli_agent_orchestrator.services.agui_stream import to_agui_event

GOLDEN = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "agui_ambient"
    / "internal_frames_golden.json"
)


def _frames() -> Dict[str, Any]:
    per_record = [{"id": r["id"], "frame": list(to_agui_event(r))} for r in PRIMITIVE_RECORDS]
    legacy = list(to_agui_event(LEGACY_ENVELOPE))

    tracker = ToolCallLifecycleTracker()
    fed: List[Any] = []
    for record in TRACKER_SEQUENCE:
        fed.extend(list(f) for f in tracker.feed(record, to_agui_event(record)))
    closed_after_sequence = [list(f) for f in tracker.close_all()]

    open_tracker = ToolCallLifecycleTracker()
    opened = [list(f) for f in open_tracker.feed(HANDOFF, to_agui_event(HANDOFF))]
    closed_open = [list(f) for f in open_tracker.close_all()]

    return {
        "per_record": per_record,
        "legacy_envelope": legacy,
        "tracker_sequence_feed": fed,
        "tracker_sequence_close_all": closed_after_sequence,
        "tracker_open_handoff_feed": opened,
        "tracker_open_handoff_close_all": closed_open,
    }


def test_internal_frames_match_the_golden() -> None:
    actual = json.loads(json.dumps(_frames(), sort_keys=True))
    if os.environ.get("CAO_REGEN_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert actual == expected


def test_golden_covers_what_it_claims() -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    types = [entry["frame"][0] for entry in golden["per_record"]]
    assert "CUSTOM" in types and "RAW" in types  # generative UI and the off-list refusal
    assert golden["legacy_envelope"][0] == "STEP_STARTED"
    fed_types = [frame[0] for frame in golden["tracker_sequence_feed"]]
    i = fed_types.index("TOOL_CALL_RESULT")
    assert fed_types[i + 1] == "TOOL_CALL_END"  # the a2a RESULT + END pair
    assert [f[0] for f in golden["tracker_open_handoff_close_all"]] == ["TOOL_CALL_END"]
