"""Tests for POST /sessions/{session_name}/env (session env re-hydration).

The per-session forwarded env map (``cao launch --env``) lives only in
cao-server memory; a server restart wipes it and workers spawned afterwards
lose the forwarded vars. The endpoint re-registers them for a live session.
"""

from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.services.session_env import (
    clear_session_env,
    get_session_env,
    set_session_env,
)

SESSION = "cao-env-test"


def _mock_backend(exists=True):
    backend = MagicMock()
    backend.session_exists.return_value = exists
    return backend


class TestSetSessionEnvEndpoint:
    def teardown_method(self):
        clear_session_env(SESSION)

    def test_rehydrates_env_for_live_session(self, client):
        with patch(
            "cli_agent_orchestrator.api.main.get_backend", return_value=_mock_backend()
        ):
            resp = client.post(
                f"/sessions/{SESSION}/env",
                json={"env_vars": {"KIMI_MODEL_NAME": "kimi-k2.5"}},
            )

        assert resp.status_code == 200
        assert resp.json()["env_keys"] == ["KIMI_MODEL_NAME"]
        assert get_session_env(SESSION) == {"KIMI_MODEL_NAME": "kimi-k2.5"}

    def test_merges_on_top_of_existing_map(self, client):
        set_session_env(SESSION, {"KEEP": "old", "SHARED": "old"})
        with patch(
            "cli_agent_orchestrator.api.main.get_backend", return_value=_mock_backend()
        ):
            resp = client.post(
                f"/sessions/{SESSION}/env",
                json={"env_vars": {"SHARED": "new", "ADDED": "x"}},
            )

        assert resp.status_code == 200
        assert get_session_env(SESSION) == {"KEEP": "old", "SHARED": "new", "ADDED": "x"}

    def test_missing_session_is_404(self, client):
        with patch(
            "cli_agent_orchestrator.api.main.get_backend",
            return_value=_mock_backend(exists=False),
        ):
            resp = client.post(
                f"/sessions/{SESSION}/env", json={"env_vars": {"A": "b"}}
            )

        assert resp.status_code == 404
        assert get_session_env(SESSION) == {}

    def test_blocked_prefix_rejected_loudly(self, client):
        """The merge layer would silently drop CLAUDE*-prefixed keys at window
        creation; the boundary must reject them with a clear 400 instead."""
        with patch(
            "cli_agent_orchestrator.api.main.get_backend", return_value=_mock_backend()
        ):
            resp = client.post(
                f"/sessions/{SESSION}/env",
                json={"env_vars": {"CLAUDE_SECRET": "x"}},
            )

        assert resp.status_code == 400
        assert "blocked prefix" in resp.json()["detail"]
        assert get_session_env(SESSION) == {}

    def test_invalid_name_and_oversized_value_rejected(self, client):
        with patch(
            "cli_agent_orchestrator.api.main.get_backend", return_value=_mock_backend()
        ):
            bad_name = client.post(
                f"/sessions/{SESSION}/env", json={"env_vars": {"1BAD": "x"}}
            )
            too_big = client.post(
                f"/sessions/{SESSION}/env", json={"env_vars": {"BIG": "x" * 4096}}
            )

        assert bad_name.status_code == 400
        assert too_big.status_code == 400
        assert get_session_env(SESSION) == {}
