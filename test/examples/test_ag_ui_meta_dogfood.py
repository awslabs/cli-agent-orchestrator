"""The committed meta-dogfood capture shows what the default AG-UI stream sends.

``examples/ag-ui/ag-ui-meta-dogfood/frames.jsonl`` is evidence: the frames
``capture.py`` read off ``GET /agui/v1/stream``. Review note 5 on #1232 found it
still in the pre-1.0 named-frame form after the default framing changed, so this
checks the committed file against the AG-UI 1.0 rules the stream itself is held
to. Regenerate it with ``capture.py`` when the stream's form changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from test._support.agui_conformance import check_lifecycle, validate_frames

FRAMES = (
    Path(__file__).resolve().parents[2]
    / "examples"
    / "ag-ui"
    / "ag-ui-meta-dogfood"
    / "frames.jsonl"
)


def _frames() -> list:
    return [json.loads(line) for line in FRAMES.read_text(encoding="utf-8").splitlines() if line]


def test_every_committed_frame_is_an_ag_ui_1_0_event() -> None:
    frames = _frames()
    assert frames, "frames.jsonl is empty"
    assert all("type" in f for f in frames), "a frame has no `type`: the pre-1.0 form"
    validate_frames(frames)


def test_the_committed_capture_is_one_whole_run() -> None:
    frames = _frames()
    assert frames[0]["type"] == "RUN_STARTED" and frames[0]["protocolVersion"] == "1.0"
    assert frames[-1]["type"] == "RUN_FINISHED"
    check_lifecycle(frames)
