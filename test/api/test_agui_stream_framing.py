"""``?framing=`` on ``GET /agui/v1/stream``: the legacy opt-in and its guards.

``framing=cao-legacy`` must stay as the stream was at ``8bf6e8f0`` (#1193's ``off``
mode): the golden was captured from that code for a fixed fake-bus input (an empty fleet,
so it does not exercise the terminal rows, which have since gained ``runner_id`` and a live
``status``). The legacy framing is deprecated, so its responses carry
RFC 9745 ``Deprecation`` (a structured date, ``@<unix-seconds>``) and a ``Link``
with ``rel="deprecation"``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from test._support.agui_ambient_records import PRIMITIVE_RECORDS
from test._support.agui_stream_harness import install

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app

client = TestClient(app, base_url="http://localhost")

GOLDEN = (
    Path(__file__).resolve().parents[1] / "fixtures" / "agui_ambient" / "legacy_stream_golden.sse"
)
HISTORY = PRIMITIVE_RECORDS[:6]
LIVE = PRIMITIVE_RECORDS[6:]
SINCE = "2026-11-04T09:59:59Z"


def _legacy_body(monkeypatch: pytest.MonkeyPatch, params: dict) -> tuple:
    install(monkeypatch, history=HISTORY, live=LIVE)
    with client.stream("GET", "/agui/v1/stream", params=params) as resp:
        body = "".join(resp.iter_text())
        return resp.status_code, dict(resp.headers), body


def test_cao_legacy_framing_is_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.environ.get("CAO_REGEN_GOLDEN") == "1":
        # Only valid on the pre-change code, where the route ignores ?framing=.
        code, _, body = _legacy_body(monkeypatch, {"since": SINCE})
        assert code == 200
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(body, encoding="utf-8")
    code, headers, body = _legacy_body(monkeypatch, {"since": SINCE, "framing": "cao-legacy"})
    assert code == 200
    assert body == GOLDEN.read_text(encoding="utf-8")
    assert re.fullmatch(r"@\d+", headers.get("deprecation", "")), headers.get("deprecation")
    link = headers.get("link", "")
    assert 'rel="deprecation"' in link and "docs/agui.md#legacy-framing" in link


def test_unknown_framing_is_400(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    resp = client.get("/agui/v1/stream", params={"framing": "nope"})
    assert resp.status_code == 400
    assert "framing" in resp.text


@pytest.mark.parametrize("bad", ["", "has space", "x" * 129, "semi;colon", "slash/no"])
def test_get_thread_id_validated(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    install(monkeypatch)
    resp = client.get("/agui/v1/stream", params={"threadId": bad, "framing": "cao-legacy"})
    assert resp.status_code == 400
    assert "threadId" in resp.text


def test_get_thread_id_accepts_a_valid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    with client.stream(
        "GET", "/agui/v1/stream", params={"threadId": "app.thread:1-A_b", "framing": "cao-legacy"}
    ) as resp:
        assert resp.status_code == 200


def test_cao_legacy_framing_strips_the_session_subagent_run_id_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # New records carry ``detail.session_subagent_run_id`` (stamped by the
    # EventLogPublisher for the conformant stream). The legacy wire is promised
    # byte-identical, so the same records WITH the stamp must still produce the
    # golden captured before the stamp existed.
    def stamped(records: list) -> list:
        return [
            (
                {
                    **r,
                    "detail": {
                        **(r.get("detail") or {}),
                        "session_subagent_run_id": "session:0123456789abcdef",
                    },
                }
                if r.get("session_name")
                else r
            )
            for r in records
        ]

    assert any(r.get("session_name") for r in HISTORY + LIVE)  # premise: something gets stamped
    install(monkeypatch, history=stamped(HISTORY), live=stamped(LIVE))
    with client.stream(
        "GET", "/agui/v1/stream", params={"since": SINCE, "framing": "cao-legacy"}
    ) as resp:
        body = "".join(resp.iter_text())
    assert "session_subagent_run_id" not in body
    assert body == GOLDEN.read_text(encoding="utf-8")
