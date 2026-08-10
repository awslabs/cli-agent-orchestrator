"""HTTP surface of the cond-0377D live legacy audit, one-candidate migration,
and provider capability reads.

The audit and capability reads are read-scoped; the migration endpoint is
write-scoped and consumes exactly one explicit candidate.  Typed outcomes
map to HTTP codes without ever leaking pane output, raw exceptions, or
secrets.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from cli_agent_orchestrator.api import roster as roster_api
from cli_agent_orchestrator.services import legacy_identity_migration as lim
from cli_agent_orchestrator.services import native_status_repair as nsr

LEGACY_AUDIT_ENDPOINT = "/roster/legacy-audit"
CAPABILITIES_ENDPOINT = "/roster/provider-capabilities"
MIGRATION_ENDPOINT = "/roster/legacy-migrations"

OPERATION_ID = "00000000-0000-4000-8000-0000000000bb"
OCCURRENCE = "00000000-0000-4000-8000-0000000000aa"
VERSION = "2.1.226"


@pytest.fixture
def _stub_legacy_audit(monkeypatch):
    state: dict[str, Any] = {"calls": []}

    def _fake() -> dict[str, Any]:
        state["calls"].append("audit")
        return state["outcome"]

    monkeypatch.setattr(roster_api.legacy_identity_migration, "run_live_legacy_audit", _fake)
    return state


@pytest.fixture
def _stub_migration(monkeypatch):
    state: dict[str, Any] = {"calls": []}

    def _fake(**kwargs: Any) -> dict[str, Any]:
        state["calls"].append(kwargs)
        return state["outcome"]

    monkeypatch.setattr(
        roster_api.legacy_identity_migration, "migrate_terminal_native_identity", _fake
    )
    return state


@pytest.fixture
def _stub_capabilities(monkeypatch):
    state: dict[str, Any] = {"calls": []}

    def _fake() -> dict[str, Any]:
        state["calls"].append("capabilities")
        return state["outcome"]

    monkeypatch.setattr(roster_api.provider_capabilities, "provider_capability_cells", _fake)
    return state


def _migration_outcome(status: str, reason: Optional[str] = None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": "cao-m3-legacy-migration-v1",
        "status": status,
        "reason": reason,
        "detail": "typed detail",
        "operation_id": OPERATION_ID,
        "request_digest": "c" * 64,
        "repair_operation_id": "00000000-0000-4000-8000-0000000000cc",
        "repair_status": None,
        "repair_reason": None,
        "terminal_id": "a1b2c3d4",
        "provider": "claude_code",
        "generation": None,
        "physical_occurrence": OCCURRENCE,
        "provider_version": VERSION,
        "audit_occurrence_id": "00000000-0000-4000-8000-0000000000dd",
        "audit_candidate_digest": "d" * 64,
        "native_session_id": None,
        "evidence_sha256": None,
        "parser_key": None,
        "attachment": None,
        "task_bytes_submitted": False,
    }
    payload.update(extra)
    return payload


def _migration_payload() -> dict[str, Any]:
    return {
        "operation_id": OPERATION_ID,
        "terminal_id": "a1b2c3d4",
        "provider": "claude_code",
        "generation": None,
        "physical_occurrence": OCCURRENCE,
        "provider_version": VERSION,
        "audit_occurrence_id": "00000000-0000-4000-8000-0000000000dd",
        "audit_candidate_digest": "d" * 64,
    }


def _post_migration(client, **body: Any):
    payload = _migration_payload()
    payload.update(body)
    return client.post(MIGRATION_ENDPOINT, json=payload)


def test_legacy_audit_read_route(client, _stub_legacy_audit):
    _stub_legacy_audit["outcome"] = {
        "schema": "cao-m3-legacy-audit-v1",
        "occurrence_id": "00000000-0000-4000-8000-0000000000dd",
        "generated_at": "now",
        "terminals_total": 1,
        "eligible_count": 1,
        "refusals_count": 0,
        "candidates": [],
    }
    response = client.get(LEGACY_AUDIT_ENDPOINT)
    assert response.status_code == 200
    assert response.json()["schema"] == "cao-m3-legacy-audit-v1"
    assert _stub_legacy_audit["calls"] == ["audit"]


def test_provider_capabilities_read_route(client, _stub_capabilities):
    _stub_capabilities["outcome"] = {
        "schema": "cao-m3-provider-capabilities-v1",
        "generated_at": "now",
        "providers": [],
    }
    response = client.get(CAPABILITIES_ENDPOINT)
    assert response.status_code == 200
    assert response.json()["schema"] == "cao-m3-provider-capabilities-v1"
    assert _stub_capabilities["calls"] == ["capabilities"]


def test_migration_requires_the_full_explicit_request(client, _stub_migration):
    assert client.post(MIGRATION_ENDPOINT, json={"terminal_id": "a1b2c3d4"}).status_code == 422
    assert _stub_migration["calls"] == []


def test_migration_wires_the_exact_request(client, _stub_migration):
    _stub_migration["outcome"] = _migration_outcome(lim.MIGRATION_MIGRATED)
    response = _post_migration(client)
    assert response.status_code == 200
    call = _stub_migration["calls"][0]
    assert call["operation_id"] == OPERATION_ID
    assert call["terminal_id"] == "a1b2c3d4"
    assert call["provider"] == "claude_code"
    assert call["generation"] is None
    assert call["physical_occurrence"] == OCCURRENCE
    assert call["provider_version"] == VERSION
    assert call["audit_occurrence_id"] == "00000000-0000-4000-8000-0000000000dd"
    assert call["audit_candidate_digest"] == "d" * 64


def test_migration_terminal_outcomes_are_200(client, _stub_migration):
    for status in (
        lim.MIGRATION_MIGRATED,
        lim.MIGRATION_ALREADY_KNOWN,
        lim.MIGRATION_IDENTITY_STILL_MISSING,
    ):
        _stub_migration["outcome"] = _migration_outcome(status)
        response = _post_migration(client)
        assert response.status_code == 200, status
        assert response.json()["status"] == status


@pytest.mark.parametrize(
    "reason, expected_status",
    [
        ("invalid-input", 400),
        ("unsupported-provider", 400),
        ("missing-occurrence", 400),
        ("producer-disabled", 409),
        ("operation-conflict", 409),
        ("candidate-drift", 409),
        ("provider-drift", 409),
        ("generation-mismatch", 409),
        ("occurrence-mismatch", 409),
        ("seam-drift", 409),
        ("repair-attempt-ambiguous", 409),
        ("repair-attempt-unresolved", 409),
        ("missing-agent", 409),
        ("terminal-not-found", 404),
        ("no-roster-incarnation", 404),
        ("roster-unavailable", 503),
        ("attachment-unreadable", 503),
        ("binding-unreadable", 503),
        ("persistence-unavailable", 503),
        ("already-known", 409),
        ("ambiguous", 409),
        ("unknown-liveness", 409),
        ("dead", 409),
        ("unreadable", 409),
        ("corrupt", 409),
        ("already-retired", 409),
        ("conflicting-owner", 409),
        ("identity-conflict", 409),
    ],
)
def test_migration_typed_refusals_map_to_http(client, _stub_migration, reason, expected_status):
    _stub_migration["outcome"] = _migration_outcome(lim.MIGRATION_REFUSED, reason=reason)
    response = _post_migration(client)
    assert response.status_code == expected_status, reason
    assert response.json()["reason"] == reason


def test_migration_errored_maps_to_500(client, _stub_migration):
    _stub_migration["outcome"] = _migration_outcome(lim.MIGRATION_ERRORED, reason="errored")
    response = _post_migration(client)
    assert response.status_code == 500
    assert response.json()["reason"] == "errored"


def test_migration_never_leaks_pane_output_or_secrets(client, _stub_migration):
    secret = "super_secret_pane_value_zz9"
    _stub_migration["outcome"] = _migration_outcome(
        lim.MIGRATION_REFUSED,
        reason="candidate-drift",
        detail="the candidate facts changed since the audit",
    )
    response = _post_migration(client)
    body = response.json()
    text = str(body)
    assert secret not in text
    assert "Session ID:" not in text
    assert len(body["detail"]) <= 500


@pytest.fixture
def _enable_auth(monkeypatch):
    from cli_agent_orchestrator.security import auth as _auth_mod

    monkeypatch.setenv("AUTH0_DOMAIN", "test.local")
    monkeypatch.setenv("AUTH0_AUDIENCE", "cao://test")
    _auth_mod.get_jwks_cache().clear()
    yield
    _auth_mod.get_jwks_cache().clear()


def test_legacy_audit_and_capabilities_require_read_scope(client, _enable_auth, monkeypatch):
    from cli_agent_orchestrator.security import auth as _auth_mod

    assert client.get(LEGACY_AUDIT_ENDPOINT).status_code == 401
    assert client.get(CAPABILITIES_ENDPOINT).status_code == 401
    monkeypatch.setattr(_auth_mod, "extract_scopes_from_token", lambda t: ["cao:read"])
    assert (
        client.get(
            LEGACY_AUDIT_ENDPOINT, headers={"Authorization": "Bearer read-token"}
        ).status_code
        == 200
    )
    assert (
        client.get(
            CAPABILITIES_ENDPOINT, headers={"Authorization": "Bearer read-token"}
        ).status_code
        == 200
    )


def test_migration_requires_write_scope(client, _enable_auth, _stub_migration, monkeypatch):
    from cli_agent_orchestrator.security import auth as _auth_mod

    assert client.post(MIGRATION_ENDPOINT, json=_migration_payload()).status_code == 401
    monkeypatch.setattr(_auth_mod, "extract_scopes_from_token", lambda t: ["cao:read"])
    response = client.post(
        MIGRATION_ENDPOINT,
        json=_migration_payload(),
        headers={"Authorization": "Bearer read-only"},
    )
    assert response.status_code == 403
    assert _stub_migration["calls"] == []
    monkeypatch.setattr(_auth_mod, "extract_scopes_from_token", lambda t: ["cao:write"])
    _stub_migration["outcome"] = _migration_outcome(lim.MIGRATION_MIGRATED)
    assert (
        client.post(
            MIGRATION_ENDPOINT,
            json=_migration_payload(),
            headers={"Authorization": "Bearer write-token"},
        ).status_code
        == 200
    )
