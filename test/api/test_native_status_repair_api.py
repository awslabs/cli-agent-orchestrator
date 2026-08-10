"""HTTP surface of the panel-attested native /status identity repair.

The endpoint is a single explicit POST that requires an explicit canonical
``operation_id`` and takes the expected model generation and provider
build as optional plan metadata; it maps the typed outcomes to HTTP codes
without ever leaking pane output, raw exceptions, or secrets.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from cli_agent_orchestrator.api import roster as roster_api
from cli_agent_orchestrator.services import native_status_repair as nsr

ENDPOINT = "/roster/terminals/a1b2c3d4/native-identity-repair"
GENERATION = "00000000-0000-4000-8000-000000000001"
VERSION = "2.1.226"
OPERATION_ID = "00000000-0000-4000-8000-0000000000bb"


@pytest.fixture(autouse=True)
def _stub_service(monkeypatch):
    """Keep the endpoint test to the HTTP contract; the service behavior is
    covered by the service-level suite."""
    state: dict[str, Any] = {"calls": []}

    def _fake(
        *,
        terminal_id: str,
        generation: Optional[str],
        provider_version: Optional[str],
        physical_occurrence: Optional[str],
        operation_id: str,
    ) -> dict[str, Any]:
        state["calls"].append(
            {
                "terminal_id": terminal_id,
                "generation": generation,
                "provider_version": provider_version,
                "physical_occurrence": physical_occurrence,
                "operation_id": operation_id,
            }
        )
        return state["outcome"]

    monkeypatch.setattr(roster_api.native_status_repair, "repair_terminal_native_identity", _fake)
    return state


def _outcome(status: str, reason: Optional[str] = None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": "cao-native-status-repair-v1",
        "status": status,
        "reason": reason,
        "detail": "typed detail",
        "operation_id": OPERATION_ID,
        "request_digest": "a" * 64,
        "terminal_id": "a1b2c3d4",
        "generation": GENERATION,
        "model_generation": GENERATION,
        "physical_occurrence": GENERATION,
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


def _post(client, **body: Any):
    payload = {
        "operation_id": OPERATION_ID,
        "generation": GENERATION,
        "provider_version": VERSION,
        "physical_occurrence": GENERATION,
    }
    payload.update(body)
    return client.post(ENDPOINT, json=payload)


def test_repair_requires_operation_id(client, _stub_service):
    response = client.post(ENDPOINT, json={"generation": GENERATION, "provider_version": VERSION})
    assert response.status_code == 422
    assert _stub_service["calls"] == []


def test_repair_wires_the_exact_identity(client, _stub_service):
    _stub_service["outcome"] = _outcome(
        nsr.STATUS_REPAIRED,
        native_session_id="4f5f46c7-b660-4f6f-a144-d2c6dceccf95",
        evidence_sha256="b" * 64,
        parser_key="claude-modal-v1",
        attachment={"state": "attached"},
    )
    response = _post(client)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == nsr.STATUS_REPAIRED
    assert body["schema"] == "cao-native-status-repair-v1"
    call = _stub_service["calls"][0]
    assert call["terminal_id"] == "a1b2c3d4"
    assert call["generation"] == GENERATION
    assert call["provider_version"] == VERSION
    assert call["physical_occurrence"] == GENERATION
    assert call["operation_id"] == OPERATION_ID


def test_legacy_repair_binds_the_physical_occurrence(client, _stub_service):
    occurrence = "00000000-0000-4000-8000-0000000000aa"
    _stub_service["outcome"] = _outcome(
        nsr.STATUS_REPAIRED,
        generation=None,
        model_generation=None,
        physical_occurrence=occurrence,
    )
    response = client.post(
        ENDPOINT, json={"operation_id": OPERATION_ID, "physical_occurrence": occurrence}
    )
    assert response.status_code == 200
    call = _stub_service["calls"][0]
    assert call["generation"] is None
    assert call["provider_version"] is None
    assert call["physical_occurrence"] == occurrence


def test_repair_already_known_and_kimi_still_missing_are_200(client, _stub_service):
    for status in (nsr.STATUS_ALREADY_KNOWN, nsr.STATUS_IDENTITY_STILL_MISSING):
        _stub_service["outcome"] = _outcome(status)
        response = _post(client)
        assert response.status_code == 200
        assert response.json()["status"] == status


@pytest.mark.parametrize(
    "reason, expected_status",
    [
        ("invalid-input", 400),
        ("provider-unsupported", 400),
        ("unsupported-build", 400),
        ("generation-required", 400),
        ("physical-occurrence-required", 400),
        ("version-drift", 409),
        ("binding-unreadable", 503),
        ("operation-conflict", 409),
        ("terminal-not-found", 404),
        ("no-roster-incarnation", 404),
        ("callback-target-missing", 409),
        ("roster-unavailable", 503),
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
        ("attachment-unresolved", 409),
        ("attachment-reconcile", 409),
        ("identity-conflict", 409),
        ("persistence-failed", 503),
        ("attachment-unavailable", 503),
    ],
)
def test_repair_typed_refusals_map_to_http(client, _stub_service, reason, expected_status):
    _stub_service["outcome"] = _outcome(nsr.STATUS_REFUSED, reason=reason)
    response = _post(client)
    assert response.status_code == expected_status, reason
    assert response.json()["reason"] == reason


def test_repair_errored_maps_to_500(client, _stub_service):
    _stub_service["outcome"] = _outcome(nsr.STATUS_ERRORED, reason="errored")
    response = _post(client)
    assert response.status_code == 500
    assert response.json()["reason"] == "errored"


def test_repair_never_leaks_pane_output_or_secrets(client, _stub_service):
    secret = "super_secret_pane_value_zz9"
    _stub_service["outcome"] = _outcome(
        nsr.STATUS_REFUSED,
        reason="panel-unparsed",
        detail="the /status panel never rendered a usable identity",
    )
    response = _post(client)
    body = response.json()
    text = str(body)
    assert secret not in text
    assert "Session ID:" not in text
    assert "Login method" not in text
    assert len(body["detail"]) <= 500
