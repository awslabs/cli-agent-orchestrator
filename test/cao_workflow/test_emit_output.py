"""Unit tests for cao_workflow.emit_output (Q1=A — the sentinel convenience wrapper)."""

from __future__ import annotations

import json

import cao_workflow


def test_emit_output_prints_sentinel_prefixed_json(capsys):
    cao_workflow.emit_output({"result": "ok", "count": 3})

    captured = capsys.readouterr()
    assert captured.out.startswith("CAO_WORKFLOW_OUTPUT:")
    payload = captured.out[len("CAO_WORKFLOW_OUTPUT:") :].strip()
    assert json.loads(payload) == {"result": "ok", "count": 3}


def test_emit_output_handles_scalar_value(capsys):
    cao_workflow.emit_output(42)

    captured = capsys.readouterr()
    assert captured.out.strip() == "CAO_WORKFLOW_OUTPUT:42"
