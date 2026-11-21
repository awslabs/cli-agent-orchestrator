"""Unit tests for cao_workflow._transport (BR-8, BR-9, A5).

Mocks ``urllib.request.urlopen`` directly (one level lower than
test_run_step.py, which mocks ``_post`` itself) to prove the socket-timeout
slack and HTTPError-to-_Response translation at the transport layer.
"""

from __future__ import annotations

import io
import json
from urllib.error import HTTPError

import pytest

import cao_workflow
from cao_workflow._transport import _TRANSPORT_SLACK, _post


class _FakeCM:
    def __init__(self, status: int, body: bytes):
        self._status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body

    def getcode(self):
        return self._status


@pytest.mark.parametrize("surface", ["step", "run_step"])
@pytest.mark.parametrize("timeout", [None, 100.0])
@pytest.mark.parametrize("cap", [None, 400.0])
@pytest.mark.parametrize("replayed", [False, True])
def test_user_wait_budget_and_result_reach_script(monkeypatch, surface, timeout, cap, replayed):
    for key, value in {
        "CAO_WORKFLOW_RUN_ID": "run-1",
        "CAO_WORKFLOW_GENERATION": "1",
        "CAO_API_BASE_URL": "http://localhost:9889",
    }.items():
        monkeypatch.setenv(key, value)
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data)
        return _FakeCM(
            200,
            json.dumps(
                dict(
                    terminal_id="t1",
                    last_message="done",
                    status="completed",
                    replayed=replayed,
                    waited_seconds=300.0,
                )
            ).encode(),
        )

    monkeypatch.setattr("cao_workflow._transport.urlopen", fake_urlopen)
    options = dict(timeout=timeout, timeout_excludes_waiting=True, max_waiting=cap)
    if surface == "step":
        options["recovery"] = "idempotent"
    handle = getattr(cao_workflow, surface)("codex", "developer", "task", **options)

    expected = (
        None if cap is None else (600.0 if timeout is None else timeout) + cap + _TRANSPORT_SLACK
    )
    assert captured["timeout"] == expected
    assert captured["body"]["timeout_excludes_waiting"] is True
    assert captured["body"]["max_waiting"] == cap
    assert handle.waited_seconds == 300.0
    assert handle.replayed is replayed


def test_socket_timeout_includes_transport_slack(monkeypatch):
    captured_kwargs = {}

    def fake_urlopen(request, timeout=None):
        captured_kwargs["timeout"] = timeout
        return _FakeCM(200, b'{"ok": true}')

    monkeypatch.setattr("cao_workflow._transport.urlopen", fake_urlopen)

    _post("http://localhost:9889/terminals/run-step", {"a": 1}, timeout=100.0)

    assert captured_kwargs["timeout"] == 100.0 + _TRANSPORT_SLACK


def test_default_timeout_used_when_none_given(monkeypatch):
    captured_kwargs = {}

    def fake_urlopen(request, timeout=None):
        captured_kwargs["timeout"] = timeout
        return _FakeCM(200, b'{"ok": true}')

    monkeypatch.setattr("cao_workflow._transport.urlopen", fake_urlopen)

    _post("http://localhost:9889/terminals/run-step", {"a": 1}, timeout=None)

    assert captured_kwargs["timeout"] == 600.0 + _TRANSPORT_SLACK


def test_http_error_translated_to_response_not_raised(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise HTTPError(
            url="http://localhost:9889/terminals/run-step",
            code=422,
            msg="Unprocessable",
            hdrs=None,
            fp=io.BytesIO(b'{"detail": "bad request"}'),
        )

    monkeypatch.setattr("cao_workflow._transport.urlopen", fake_urlopen)

    response = _post("http://localhost:9889/terminals/run-step", {"a": 1}, timeout=10.0)

    assert response.status == 422
    assert json.loads(response.body) == {"detail": "bad request"}
