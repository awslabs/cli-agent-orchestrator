"""Unit tests for cao_workflow._transport (BR-8, BR-9, A5).

Mocks ``urllib.request.urlopen`` directly (one level lower than
test_run_step.py, which mocks ``_post`` itself) to prove the socket-timeout
slack and HTTPError-to-_Response translation at the transport layer.
"""

from __future__ import annotations

import io
import json
from urllib.error import HTTPError

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
