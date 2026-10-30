"""GET /fleet/workers and /fleet/workers/{id} - the worker table behind the web UI.

They mirror ``cao worker list`` / ``cao worker status`` through ``FleetClient``. No
broker is reached here: ``FleetClient.from_env`` is patched. What these pin down is
the allowlist (nothing the broker adds - least of all a release token or target host
- reaches the browser), the live-only default, the degrade-to-lease behaviour, the id
check that keeps a worker id from walking the broker's proxy path, and the 503/404
mapping.
"""

from unittest.mock import MagicMock, patch

import click

from cli_agent_orchestrator.utils.fleet import BrokerRequestError

FROM_ENV = "cli_agent_orchestrator.utils.fleet.FleetClient.from_env"

LIVE = {
    "worker_id": "a1b2c3d4",
    "state": "leased",
    "reason": None,
    "agent_profile": "developer",
    "provider": "claude_code",
    "age_seconds": 42,
    "workload_present": True,
    "cleanup_pending": False,
    "lease_tracked": True,
    # Fields the broker might carry that must never be forwarded.
    "release_token": "SECRET-RELEASE",
    "target_host": "worker-a1b2c3d4.cao.svc.cluster.local",
}
SETTLED = {
    "worker_id": "e5f6a7b8",
    "state": "released",
    "reason": "released by caller",
    "agent_profile": "developer",
    "provider": "claude_code",
    "age_seconds": 300,
    "workload_present": False,
    "cleanup_pending": False,
    "lease_tracked": True,
}
ALLOWED = {
    "worker_id",
    "state",
    "reason",
    "agent_profile",
    "provider",
    "age_seconds",
    "workload_present",
    "cleanup_pending",
    "lease_tracked",
}


def _client(workers, *, terminal=None, output="done", terminal_exc=None):
    fake = MagicMock()
    fake.url = "http://127.0.0.1:9890"
    fake.workers.return_value = workers
    if terminal_exc is not None:
        fake.sole_terminal.side_effect = terminal_exc
    else:
        fake.sole_terminal.return_value = {"id": "t1"}
    fake.terminal.return_value = terminal or {
        "id": "t1",
        "name": "developer-1",
        "session_name": "cao-work",
        "agent_profile": "developer",
        "provider": "claude_code",
        "status": "completed",
        "caller_id": "should-not-leak",
    }
    fake.terminal_output.return_value = output
    return fake


def test_list_is_live_only_by_default_and_allowlisted(client):
    with patch(FROM_ENV, return_value=_client([LIVE, SETTLED])):
        response = client.get("/fleet/workers")

    assert response.status_code == 200
    body = response.json()
    assert body["broker"] == "http://127.0.0.1:9890"
    assert [w["worker_id"] for w in body["workers"]] == ["a1b2c3d4"]
    assert set(body["workers"][0]) == ALLOWED
    assert "SECRET-RELEASE" not in response.text
    assert "svc.cluster.local" not in response.text


def test_list_all_includes_settled_leases(client):
    with patch(FROM_ENV, return_value=_client([LIVE, SETTLED])):
        response = client.get("/fleet/workers", params={"all": "true"})

    assert [w["worker_id"] for w in response.json()["workers"]] == ["a1b2c3d4", "e5f6a7b8"]


def test_list_without_a_broker_is_503(client):
    with patch(FROM_ENV, side_effect=click.ClickException("No fleet configured. Set ...")):
        response = client.get("/fleet/workers")

    assert response.status_code == 503
    assert "No fleet configured" in response.json()["detail"]


def test_detail_has_lease_terminal_and_last_output(client):
    with patch(FROM_ENV, return_value=_client([LIVE])):
        response = client.get("/fleet/workers/a1b2c3d4")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"worker_id", "lease", "terminal", "terminal_error", "last_output"}
    assert set(body["lease"]) == ALLOWED
    assert body["terminal"] == {
        "id": "t1",
        "name": "developer-1",
        "session_name": "cao-work",
        "agent_profile": "developer",
        "provider": "claude_code",
        "status": "completed",
    }
    assert body["terminal_error"] is None
    assert body["last_output"] == "done"
    assert "should-not-leak" not in response.text
    assert "SECRET-RELEASE" not in response.text


def test_detail_caps_the_last_output_preview(client):
    long_output = "x" * 10_000 + "TAIL"
    with patch(FROM_ENV, return_value=_client([LIVE], output=long_output)):
        body = client.get("/fleet/workers/a1b2c3d4").json()

    assert len(body["last_output"]) == 4000
    assert body["last_output"].endswith("TAIL")


def test_detail_degrades_to_the_lease_while_the_worker_boots(client):
    booting = click.ClickException("Worker a1b2c3d4 has no terminal yet.")
    with patch(FROM_ENV, return_value=_client([LIVE], terminal_exc=booting)):
        response = client.get("/fleet/workers/a1b2c3d4")

    assert response.status_code == 200
    body = response.json()
    assert body["lease"]["worker_id"] == "a1b2c3d4"
    assert body["terminal"] is None
    assert "no terminal yet" in body["terminal_error"]


def test_detail_of_a_settled_worker_does_not_touch_its_terminal(client):
    fake = _client([SETTLED])
    with patch(FROM_ENV, return_value=fake):
        body = client.get("/fleet/workers/e5f6a7b8").json()

    assert body["lease"]["state"] == "released"
    assert body["terminal"] is None
    fake.sole_terminal.assert_not_called()


def test_detail_unknown_worker_is_404(client):
    missing = BrokerRequestError("route not allowlisted", 404)
    with patch(FROM_ENV, return_value=_client([], terminal_exc=missing)):
        response = client.get("/fleet/workers/deadbeef")

    assert response.status_code == 404


def test_detail_unreachable_broker_is_503(client):
    unreachable = BrokerRequestError("Failed to reach the fleet broker", None)
    with patch(FROM_ENV, return_value=_client([], terminal_exc=unreachable)):
        response = client.get("/fleet/workers/deadbeef")

    assert response.status_code == 503


def test_detail_without_a_broker_is_503(client):
    with patch(FROM_ENV, side_effect=click.ClickException("No fleet configured. Set ...")):
        assert client.get("/fleet/workers/a1b2c3d4").status_code == 503


def test_detail_refuses_an_id_that_could_walk_the_proxy_path(client):
    fake = _client([LIVE])
    with patch(FROM_ENV, return_value=fake):
        # %2E%2E = "..": a raw ".." segment would be normalised away by the client.
        for bad in ("a1b2%2E%2E", "-leading-dash", "x" * 65, "a1b2c3d4%3Fx%3D1"):
            assert client.get(f"/fleet/workers/{bad}").status_code == 400, bad
    fake.workers.assert_not_called()
