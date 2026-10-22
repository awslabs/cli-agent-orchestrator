"""Strict activity reads must preserve absence/error and incarnation boundaries."""

from unittest.mock import Mock

import pytest

from cli_agent_orchestrator.services import session_service as service


@pytest.fixture
def reads(monkeypatch):
    backend = Mock()
    backend.list_sessions_strict.return_value = [
        {"id": "cao-a", "name": "cao-a", "status": "active"}
    ]
    monkeypatch.setattr(service, "get_backend", lambda: backend)
    pointer = Mock(return_value="current")
    monkeypatch.setattr(service, "get_session_incarnation", pointer)
    rows = Mock(
        return_value=[
            {"id": "old", "session_incarnation_id": "historical"},
            {"id": "new", "session_incarnation_id": "current"},
        ]
    )
    monkeypatch.setattr(service, "list_terminals_by_session", rows)
    return backend, pointer, rows


def test_membership_reads_only_current_rows_without_deriving_states(reads, monkeypatch):
    monitor = Mock(side_effect=AssertionError("No status derivation"))
    monkeypatch.setattr(service, "_collect_deferred_failures", monitor)
    assert service.get_session_membership("cao-a", "current")["terminals"] == [
        {"id": "new", "session_incarnation_id": "current"}
    ]
    monitor.assert_not_called()


def test_empty_membership_is_valid_only_with_live_identity(reads):
    backend, pointer, rows = reads
    rows.return_value = []
    assert service.get_session_membership("cao-a", "current")["terminals"] == []
    backend.list_sessions_strict.return_value = []
    with pytest.raises(service.SessionMembershipError) as err:
        service.get_session_membership("cao-a", "current")
    assert (err.value.status_code, err.value.kind) == (404, "session_absent")


@pytest.mark.parametrize(
    "pointer,expected,kind",
    [
        (None, "current", "session_identity_unverifiable"),
        ("next", "current", "session_incarnation_changed"),
        ("current", None, "session_incarnation_changed"),
    ],
)
def test_unverifiable_or_changed_identity(reads, pointer, expected, kind):
    reads[1].return_value = pointer
    with pytest.raises(service.SessionMembershipError) as err:
        service.get_session_membership("cao-a", expected)
    assert err.value.kind == kind
    reads[2].assert_not_called()


def test_rechecks_pointer_and_rejects_duplicate_members(reads):
    _, pointer, rows = reads
    pointer.side_effect = ["current", "next"]
    with pytest.raises(service.SessionMembershipError):
        service.get_session_membership("cao-a", "current")
    pointer.side_effect = None
    rows.return_value *= 2
    with pytest.raises(service.SessionMembershipError):
        service.get_session_membership("cao-a", "current")


def test_backend_and_database_failures_propagate(reads):
    backend, _, rows = reads
    rows.side_effect = RuntimeError("DB unavailable")
    with pytest.raises(RuntimeError, match="DB unavailable"):
        service.get_session_membership("cao-a", "current")
    backend.list_sessions_strict.side_effect = RuntimeError("Backend unavailable")
    with pytest.raises(RuntimeError, match="Backend unavailable"):
        service.list_sessions(strict=True)
    assert service.list_sessions() == []  # Legacy contract remains lenient.


def test_strict_listing_identity_and_db_failure(reads, monkeypatch):
    monkeypatch.setattr(
        service, "get_session_incarnations", Mock(return_value={"cao-a": "current"})
    )
    monkeypatch.setattr(service, "_terminals_grouped_by_session", Mock(return_value={}))
    monkeypatch.setattr(
        service, "_enrich_session_ownership", lambda backend, session, rows: session
    )
    assert service.list_sessions(strict=True)[0]["session_incarnation_id"] == "current"
    monkeypatch.setattr(
        service, "get_session_incarnations", Mock(side_effect=RuntimeError("DB failed"))
    )
    with pytest.raises(RuntimeError, match="DB failed"):
        service.list_sessions(strict=True)


@pytest.mark.parametrize("data", [None, [{}], [{"id": "cao-a", "name": None, "status": "active"}]])
def test_strict_listing_rejects_incomplete_envelopes(reads, data):
    reads[0].list_sessions_strict.return_value = data
    with pytest.raises((ValueError, TypeError)):
        service.list_sessions(strict=True)
