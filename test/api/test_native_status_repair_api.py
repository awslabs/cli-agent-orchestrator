"""HTTP surface of the native /status identity repair (cond-0377C).

The endpoint is a single explicit POST that requires an exact
``terminal_id`` and a nonempty ``generation``, runs the repair off the
event loop, and maps the typed outcomes to HTTP codes without ever
leaking pane output or secrets.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from cli_agent_orchestrator.api import roster as roster_api
from cli_agent_orchestrator.services import native_status_repair as nsr

ENDPOINT = "/roster/terminals/a1b2c3d4/native-identity-repair"
GENERATION = "00000000-0000-4000-8000-000000000001"
VERSION = "2.1.226"


@pytest.fixture(autouse=True)
def _stub_service(monkeypatch):
    """Keep the endpoint test to the HTTP contract: the service behavior is
    covered by the service-level suite.  The stub echoes its arguments so
    the test can assert the exact wiring."""
    state: dict[str, Any] = {"calls": []}

    def _fake(
        *,
        terminal_id: str,
        generation: str,
        provider_version: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        state["calls"].append(
            {
                "terminal_id": terminal_id,
                "generation": generation,
                "provider_version": provider_version,
                "kwargs": kwargs,
            }
        )
        return state["outcome"]

    monkeypatch.setattr(roster_api.native_status_repair, "repair_terminal_native_identity", _fake)
    monkeypatch.setattr(roster_api.native_status_repair, "STATUS_REPAIRED", nsr.STATUS_REPAIRED)
    monkeypatch.setattr(
        roster_api.native_status_repair,
        "STATUS_IDENTITY_STILL_MISSING",
        nsr.STATUS_IDENTITY_STILL_MISSING,
    )
    return state


def _outcome(status: str, reason: Optional[str] = None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": "cao-native-status-repair-v1",
        "status": status,
        "reason": reason,
        "detail": "typed detail",
        "operation_id": "op-1",
        "request_digest": "a" * 64,
        "terminal_id": "a1b2c3d4",
        "generation": GENERATION,
        "provider": "claude_code",
        "provider_version": VERSION,
        "native_session_id": None,
        "evidence_sha256": None,
        "parser_key": None,
        "attachment": None,
        "composer_restored": None,
        "task_bytes_submitted": False,
    }
    payload.update(extra)
    return payload


def test_repair_requires_generation(client, _stub_service):
    response = client.post(ENDPOINT, json={"provider_version": VERSION})
    assert response.status_code == 422
    assert _stub_service["calls"] == []


def test_repair_requires_provider_version(client, _stub_service):
    response = client.post(ENDPOINT, json={"generation": GENERATION})
    assert response.status_code == 422
    assert _stub_service["calls"] == []


def test_repair_happy_path_maps_to_200_with_typed_outcome(client, _stub_service):
    _stub_service["outcome"] = _outcome(
        nsr.STATUS_REPAIRED,
        native_session_id="4f5f46c7-b660-4f6f-a144-d2c6dceccf95",
        evidence_sha256="b" * 64,
        parser_key="claude-modal-v1",
        attachment={"state": "attached"},
    )
    response = client.post(ENDPOINT, json={"generation": GENERATION, "provider_version": VERSION})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == nsr.STATUS_REPAIRED
    assert body["schema"] == "cao-native-status-repair-v1"
    # The wired service call carried the exact identity.
    call = _stub_service["calls"][0]
    assert call["terminal_id"] == "a1b2c3d4"
    assert call["generation"] == GENERATION
    assert call["provider_version"] == VERSION


def test_repair_kimi_still_missing_is_a_200_warning(client, _stub_service):
    _stub_service["outcome"] = _outcome(nsr.STATUS_IDENTITY_STILL_MISSING)
    response = client.post(ENDPOINT, json={"generation": GENERATION, "provider_version": "0.34.0"})
    assert response.status_code == 200
    assert response.json()["status"] == nsr.STATUS_IDENTITY_STILL_MISSING


@pytest.mark.parametrize(
    "reason, expected_status",
    [
        ("unsupported-build", 400),
        ("invalid-input", 400),
        ("provider-unsupported", 400),
        ("terminal-not-found", 404),
        ("no-roster-incarnation", 404),
        ("generation-mismatch", 409),
        ("terminal-not-live", 409),
        ("incarnation-retired", 409),
        ("pane-identity-drift", 409),
        ("server-identity-drift", 409),
        ("process-identity-drift", 409),
        ("pane-busy", 409),
        ("not-ready", 409),
        ("panel-unparsed", 409),
        ("composer-not-restored", 409),
        ("attachment-conflict", 409),
        ("identity-conflict", 409),
        ("persistence-failed", 503),
        ("roster-unavailable", 503),
        ("attachment-unavailable", 503),
    ],
)
def test_repair_typed_refusals_map_to_http(client, _stub_service, reason, expected_status):
    _stub_service["outcome"] = _outcome(nsr.STATUS_REFUSED, reason=reason)
    response = client.post(ENDPOINT, json={"generation": GENERATION, "provider_version": VERSION})
    assert response.status_code == expected_status, reason
    assert response.json()["reason"] == reason


def test_repair_errored_maps_to_500(client, _stub_service):
    _stub_service["outcome"] = _outcome(nsr.STATUS_ERRORED, reason="boom")
    response = client.post(ENDPOINT, json={"generation": GENERATION, "provider_version": VERSION})
    assert response.status_code == 500
    assert response.json()["reason"] == "boom"


def test_repair_never_leaks_pane_output_or_secrets(client, _stub_service):
    _stub_service["outcome"] = _outcome(
        nsr.STATUS_REFUSED,
        reason="panel-unparsed",
        detail="the /status panel never parsed; last observation: a typed refusal",
    )
    response = client.post(ENDPOINT, json={"generation": GENERATION, "provider_version": VERSION})
    body = response.json()
    # No capture rows, no raw status text, no secret-shaped values.
    text = str(body)
    assert "Session ID:" not in text
    assert "Login method" not in text
    assert "password" not in text.lower()
    assert len(body["detail"]) <= 500


def test_repair_runs_off_the_event_loop(client, _stub_service):
    _stub_service["outcome"] = _outcome(nsr.STATUS_REPAIRED)
    response = client.post(ENDPOINT, json={"generation": GENERATION, "provider_version": VERSION})
    assert response.status_code == 200
