"""GET /fleet/status — the read-only fleet summary behind the web UI.

The route mirrors ``cao fleet status``: it asks the broker for its workers through
``FleetClient`` and reduces them to counts. No broker is reached here — ``from_env``
is patched — so this checks the shape of the reduction and the 503 mapping, not
the broker protocol (``test/utils/test_fleet_client.py`` covers that).
"""

from unittest.mock import MagicMock, patch

import click


def _fake_client(workers):
    fake = MagicMock()
    fake.url = "http://127.0.0.1:9890"
    fake.workers.return_value = workers
    return fake


def test_summarises_workers_into_counts(client):
    workers = [
        {"state": "leased", "workload_present": True, "cleanup_pending": False},
        {"state": "leased", "workload_present": True, "cleanup_pending": False},
        {"state": "released", "workload_present": False, "cleanup_pending": True},
        {"workload_present": False},
    ]
    with patch(
        "cli_agent_orchestrator.utils.fleet.FleetClient.from_env",
        return_value=_fake_client(workers),
    ):
        response = client.get("/fleet/status")

    assert response.status_code == 200
    assert response.json() == {
        "broker": "http://127.0.0.1:9890",
        "live": 2,
        "cleanup_pending": 1,
        "states": {"leased": 2, "released": 1, "unknown": 1},
    }


def test_no_broker_configured_is_503_with_the_client_message(client):
    with patch(
        "cli_agent_orchestrator.utils.fleet.FleetClient.from_env",
        side_effect=click.ClickException("No fleet configured. Set CAO_ELASTIC_BROKER_URL"),
    ):
        response = client.get("/fleet/status")

    assert response.status_code == 503
    assert "CAO_ELASTIC_BROKER_URL" in response.json()["detail"]


def test_unreachable_broker_is_503(client):
    fake = _fake_client([])
    fake.workers.side_effect = click.ClickException("Failed to reach the fleet broker")
    with patch("cli_agent_orchestrator.utils.fleet.FleetClient.from_env", return_value=fake):
        response = client.get("/fleet/status")

    assert response.status_code == 503
    assert "Failed to reach" in response.json()["detail"]
