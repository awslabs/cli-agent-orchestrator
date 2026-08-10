"""HTTP surface of the bounded exact-live-pane identity resolution
(cond-0377D M3-A read seam).

Read-authenticated, bounded, and byte-for-byte read-only.  The request
carries only the exact physical pane facts (pane id + canonical server
socket); a caller-supplied terminal id or environment label can never
override the pane mapping.  Typed non-identity outcomes map to 200 with a
closed status (absence and ambiguity are normal typed answers, not
errors).
"""

from __future__ import annotations

from typing import Any

import pytest

from cli_agent_orchestrator.api import roster as roster_api

ENDPOINT = "/roster/pane-identity"
PANE_ID = "%7"
SERVER_SOCKET = "/private/tmp/cao-native.sock"


@pytest.fixture
def _stub_resolver(monkeypatch):
    state: dict[str, Any] = {"calls": []}

    def _fake(*, pane_id: str, server_socket_path: str) -> dict[str, Any]:
        state["calls"].append({"pane_id": pane_id, "server_socket_path": server_socket_path})
        return state["outcome"]

    monkeypatch.setattr(roster_api.pane_identity_resolution, "resolve_pane_identity", _fake)
    return state


def _resolved_outcome(**extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": "cao-m3-pane-identity-resolution-v1",
        "status": "resolved",
        "reason": None,
        "observed_at": "now",
        "pane": {
            "pane_id": PANE_ID,
            "window_id": "@7",
            "session_id": "$1",
            "pane_pid": 4242,
            "server_socket_path": SERVER_SOCKET,
        },
        "terminal": {
            "terminal_id": "a1b2c3d4",
            "generation": "00000000-0000-4000-8000-000000000001",
            "physical_occurrence": "00000000-0000-4000-8000-000000000001",
            "vintage": "legacy",
        },
        "incarnation": {
            "incarnation_id": "00000000-0000-4000-8000-0000000000ee",
            "disposition": "bound",
            "lineage_id": "00000000-0000-4000-8000-0000000000ff",
        },
        "agent": {
            "agent_id": "11111111-1111-4111-8111-111111111111",
            "lineage_id": "00000000-0000-4000-8000-0000000000ff",
            "harness": "claude_code",
            "native_session_id": "4f5f46c7-b660-4f6f-a144-d2c6dceccf95",
            "disposition": "live",
        },
    }
    payload.update(extra)
    return payload


def test_pane_identity_resolution_wires_exact_physical_facts(client, _stub_resolver):
    _stub_resolver["outcome"] = _resolved_outcome()
    response = client.get(
        ENDPOINT, params={"pane_id": PANE_ID, "server_socket_path": SERVER_SOCKET}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "cao-m3-pane-identity-resolution-v1"
    assert body["status"] == "resolved"
    assert _stub_resolver["calls"] == [{"pane_id": PANE_ID, "server_socket_path": SERVER_SOCKET}]


def test_pane_identity_requires_both_physical_facts(client, _stub_resolver):
    assert client.get(ENDPOINT, params={"pane_id": PANE_ID}).status_code == 422
    assert client.get(ENDPOINT, params={"server_socket_path": SERVER_SOCKET}).status_code == 422
    assert _stub_resolver["calls"] == []


@pytest.mark.parametrize(
    "status",
    [
        "pane-unreadable-or-dead",
        "pane-unregistered",
        "terminal-pane-mismatch-or-superseded",
        "roster-incarnation-missing",
        "roster-incarnation-ambiguous-or-invalid",
    ],
)
def test_typed_non_identity_outcomes_are_200(client, _stub_resolver, status):
    _stub_resolver["outcome"] = _resolved_outcome(
        status=status,
        reason="typed detail",
        pane=None,
        terminal=None,
        incarnation=None,
        agent=None,
    )
    response = client.get(
        ENDPOINT, params={"pane_id": PANE_ID, "server_socket_path": SERVER_SOCKET}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == status
    assert body["agent"] is None
    assert body["terminal"] is None
    assert body["incarnation"] is None


def test_caller_terminal_id_is_never_accepted(client, _stub_resolver):
    _stub_resolver["outcome"] = _resolved_outcome()
    # An unknown query parameter (terminal id / env label) is ignored: the
    # mapping is always derived from the observed pane alone.
    response = client.get(
        ENDPOINT,
        params={
            "pane_id": PANE_ID,
            "server_socket_path": SERVER_SOCKET,
            "terminal_id": "a1b2c3d4",
            "CAO_TERMINAL_ID": "a1b2c3d4",
            "TMUX_PANE": "%7",
        },
    )
    assert response.status_code == 200
    assert _stub_resolver["calls"] == [{"pane_id": PANE_ID, "server_socket_path": SERVER_SOCKET}]


@pytest.fixture
def _enable_auth(monkeypatch):
    from cli_agent_orchestrator.security import auth as _auth_mod

    monkeypatch.setenv("AUTH0_DOMAIN", "test.local")
    monkeypatch.setenv("AUTH0_AUDIENCE", "cao://test")
    _auth_mod.get_jwks_cache().clear()
    yield
    _auth_mod.get_jwks_cache().clear()


def test_pane_identity_requires_read_scope(client, _enable_auth, monkeypatch):
    from cli_agent_orchestrator.security import auth as _auth_mod

    assert (
        client.get(
            ENDPOINT, params={"pane_id": PANE_ID, "server_socket_path": SERVER_SOCKET}
        ).status_code
        == 401
    )
    monkeypatch.setattr(_auth_mod, "extract_scopes_from_token", lambda t: ["cao:read"])
    assert (
        client.get(
            ENDPOINT,
            params={"pane_id": PANE_ID, "server_socket_path": SERVER_SOCKET},
            headers={"Authorization": "Bearer read-token"},
        ).status_code
        == 200
    )
