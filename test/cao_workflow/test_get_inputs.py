"""Unit tests for cao_workflow.get_inputs (Unit A, FR-A4 / A6 / BR-A6).

The author-facing read of the ``CAO_WORKFLOW_INPUTS`` spawn-env key: returns a
typed dict, ``{}`` on absence (never raises), ``ShimError`` only on malformed
JSON. Env is read at CALL time (deferred), so ``monkeypatch.setenv`` after import
is what the accessor sees.
"""

from __future__ import annotations

import pytest

import cao_workflow
from cao_workflow import get_inputs
from cao_workflow.exceptions import ShimError


def test_get_inputs_exported():
    assert "get_inputs" in cao_workflow.__all__
    assert cao_workflow.get_inputs is get_inputs


def test_get_inputs_returns_typed_dict(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CAO_WORKFLOW_INPUTS", '{"topic":"birds","count":3,"dry":true}')
    result = get_inputs()
    assert result == {"topic": "birds", "count": 3, "dry": True}


def test_get_inputs_absent_returns_empty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CAO_WORKFLOW_INPUTS", raising=False)
    assert get_inputs() == {}


def test_get_inputs_empty_string_returns_empty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CAO_WORKFLOW_INPUTS", "")
    assert get_inputs() == {}


def test_get_inputs_malformed_json_raises_shimerror(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CAO_WORKFLOW_INPUTS", "{not valid json")
    with pytest.raises(ShimError, match="not valid JSON"):
        get_inputs()


def test_get_inputs_non_object_json_raises_shimerror(monkeypatch: pytest.MonkeyPatch):
    # Valid JSON that is not an object (an array) is a delivery bug the author
    # should see, not silently coerced.
    monkeypatch.setenv("CAO_WORKFLOW_INPUTS", "[1, 2, 3]")
    with pytest.raises(ShimError, match="did not decode to a JSON object"):
        get_inputs()
