"""Additive activity projection preserves the normal detail endpoint."""

from unittest.mock import Mock

import pytest

from cli_agent_orchestrator.services import session_service


def test_strict_list_returns_unavailable_instead_of_empty(client, monkeypatch):
    read = Mock(side_effect=RuntimeError("unavailable"))
    monkeypatch.setattr(session_service, "list_sessions", read)
    response = client.get("/sessions?strict=true")
    assert response.status_code == 503
    assert response.json()["detail"]["kind"] == "session_read_unavailable"
    read.assert_called_once_with(strict=True)


def test_strict_list_accepts_confirmed_empty(client, monkeypatch):
    monkeypatch.setattr(session_service, "list_sessions", lambda strict=False: [])
    assert client.get("/sessions?strict=true").json() == []


@pytest.mark.parametrize(
    "kind,code",
    [
        ("session_absent", 404),
        ("session_incarnation_changed", 409),
        ("session_identity_unverifiable", 409),
    ],
)
def test_membership_errors_are_structured(client, monkeypatch, kind, code):
    monkeypatch.setattr(
        session_service,
        "get_session_membership",
        Mock(side_effect=session_service.SessionMembershipError(kind, code)),
    )
    response = client.get("/sessions/cao-a?view=membership&expected_incarnation_id=one")
    assert response.status_code == code
    assert response.json()["detail"]["kind"] == kind


def test_membership_is_separate_from_normal_detail(client, monkeypatch):
    read = Mock(return_value={"session": {"id": "cao-a"}, "terminals": []})
    monkeypatch.setattr(session_service, "get_session_membership", read)
    normal = Mock(return_value={"normal": True})
    monkeypatch.setattr(session_service, "get_session", normal)
    assert (
        client.get("/sessions/cao-a?view=membership&expected_incarnation_id=one").status_code == 200
    )
    read.assert_called_once_with("cao-a", "one")
    normal.assert_not_called()
    assert client.get("/sessions/cao-a").json() == {"normal": True}


def test_membership_read_failure(client, monkeypatch):
    monkeypatch.setattr(
        session_service, "get_session_membership", Mock(side_effect=RuntimeError("failed"))
    )
    response = client.get("/sessions/cao-a?view=membership")
    assert response.status_code == 503
