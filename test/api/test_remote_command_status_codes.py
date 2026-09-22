"""A disconnected runtime is a 503 on every remote terminal arm, not a 500.

The PR body says "a disconnected runtime is an explicit 503". That was true
only for ``DELETE /terminals/{id}``, because only it re-raised ``HTTPException``
before its catch-all. ``remote_terminal_command`` maps a not-connected runtime
to 503 *inside* each handler's ``try``, so input/key/output rewrapped it as a
500 with the 503 stringified into the detail (guojing1217 on #802, measured
against a live server+bridge pair). 503 is the retryable/at-capacity signal and
500 is not, so during a routine bridge rollout every in-flight input became an
alarming 500 that no retry policy or 5xx alarm could tell from a real bug.
"""

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.runtime_channel.registry import RuntimeNotDispatchedError


@pytest.fixture
def disconnected_remote():
    """The terminal is placed on a runtime that is not currently connected."""
    with (
        patch(
            "cli_agent_orchestrator.api.main.runtime_registry.is_remote",
            return_value=True,
        ),
        patch(
            "cli_agent_orchestrator.runtime_channel.registry.runtime_registry.send_terminal_command",
            side_effect=RuntimeNotDispatchedError(
                "runtime rt-1 for terminal 76550a9e is not connected"
            ),
        ),
    ):
        yield


class TestADisconnectedRuntimeIs503OnEveryArm:
    TID = "76550a9e"

    def test_input_returns_503(self, client, disconnected_remote):
        resp = client.post(f"/terminals/{self.TID}/input", params={"message": "hi"})
        assert resp.status_code == 503
        assert "not connected" in resp.json()["detail"]

    def test_key_returns_503(self, client, disconnected_remote):
        resp = client.post(f"/terminals/{self.TID}/key", params={"key": "Enter"})
        assert resp.status_code == 503
        assert "not connected" in resp.json()["detail"]

    def test_output_returns_503(self, client, disconnected_remote):
        resp = client.get(f"/terminals/{self.TID}/output")
        assert resp.status_code == 503
        assert "not connected" in resp.json()["detail"]

    def test_the_detail_is_not_a_500_wrapping_a_503(self, client, disconnected_remote):
        # The exact regression: the 503 must not be stringified into a 500 body.
        resp = client.post(f"/terminals/{self.TID}/input", params={"message": "hi"})
        assert resp.status_code != 500
        assert "500" not in resp.json()["detail"]
