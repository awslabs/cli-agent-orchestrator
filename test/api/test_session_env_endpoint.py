"""Tests for POST /sessions/{session_name}/env (session env re-hydration).

The per-session forwarded env map (``cao launch --env``) lives only in
cao-server memory; a server restart wipes it and workers spawned afterwards
lose the forwarded vars. The endpoint re-registers them for a live session.
"""

from unittest.mock import MagicMock, patch

import pytest

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
        with patch("cli_agent_orchestrator.api.main.get_backend", return_value=_mock_backend()):
            resp = client.post(
                f"/sessions/{SESSION}/env",
                json={"env_vars": {"KIMI_MODEL_NAME": "kimi-k2.5"}},
            )

        assert resp.status_code == 200
        assert resp.json()["env_keys"] == ["KIMI_MODEL_NAME"]
        assert get_session_env(SESSION) == {"KIMI_MODEL_NAME": "kimi-k2.5"}

    def test_merges_on_top_of_existing_map(self, client):
        set_session_env(SESSION, {"KEEP": "old", "SHARED": "old"})
        with patch("cli_agent_orchestrator.api.main.get_backend", return_value=_mock_backend()):
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
            resp = client.post(f"/sessions/{SESSION}/env", json={"env_vars": {"A": "b"}})

        assert resp.status_code == 404
        assert get_session_env(SESSION) == {}

    def test_blocked_prefix_rejected_loudly(self, client):
        """The merge layer would silently drop CLAUDE*-prefixed keys at window
        creation; the boundary must reject them with a clear 400 instead."""
        with patch("cli_agent_orchestrator.api.main.get_backend", return_value=_mock_backend()):
            resp = client.post(
                f"/sessions/{SESSION}/env",
                json={"env_vars": {"CLAUDE_SECRET": "x"}},
            )

        assert resp.status_code == 400
        assert "blocked prefix" in resp.json()["detail"]
        assert get_session_env(SESSION) == {}

    def test_invalid_name_and_oversized_value_rejected(self, client):
        with patch("cli_agent_orchestrator.api.main.get_backend", return_value=_mock_backend()):
            bad_name = client.post(f"/sessions/{SESSION}/env", json={"env_vars": {"1BAD": "x"}})
            too_big = client.post(
                f"/sessions/{SESSION}/env", json={"env_vars": {"BIG": "x" * 4096}}
            )

        assert bad_name.status_code == 400
        assert too_big.status_code == 400
        assert get_session_env(SESSION) == {}


def _post_env(client, env_vars, *, session=SESSION, backend=None):
    with patch(
        "cli_agent_orchestrator.api.main.get_backend",
        return_value=backend if backend is not None else _mock_backend(),
    ):
        return client.post(f"/sessions/{session}/env", json={"env_vars": env_vars})


class TestSetSessionEnvValidation:
    """The endpoint validates with the same shared validator as ``cao launch --env`` and the
    ops-MCP ``launch_session`` tool (``utils/forwarded_env.py``), and rejects loudly."""

    def teardown_method(self):
        clear_session_env(SESSION)

    def test_allowlisted_blocked_prefix_key_is_accepted(self, client):
        """``CLAUDE`` is a blocked prefix, but the documented auth-routing flags are
        allowlisted; dropping the allowlist would pass every other test in this file."""
        resp = _post_env(client, {"CLAUDE_CODE_USE_BEDROCK": "1"})

        assert resp.status_code == 200, resp.text
        assert get_session_env(SESSION) == {"CLAUDE_CODE_USE_BEDROCK": "1"}

    def test_value_byte_cap_boundary(self, client):
        """2047 bytes is the largest accepted value; 2048 is rejected (the cap is ``>=``)."""
        at_limit = _post_env(client, {"EDGE": "x" * 2047})
        over_limit = _post_env(client, {"EDGE": "y" * 2048})

        assert at_limit.status_code == 200, at_limit.text
        assert over_limit.status_code == 400
        assert "exceeds 2048 bytes" in over_limit.json()["detail"]
        # The rejected request left the accepted value in place.
        assert get_session_env(SESSION) == {"EDGE": "x" * 2047}

    def test_empty_key_is_rejected(self, client):
        resp = _post_env(client, {"": "x"})

        assert resp.status_code == 400
        assert "must match" in resp.json()["detail"]
        assert get_session_env(SESSION) == {}

    def test_invalid_session_name_is_400_before_the_backend_is_consulted(self, client):
        backend = _mock_backend()

        resp = _post_env(client, {"A": "b"}, session="bad.name", backend=backend)

        assert resp.status_code == 400
        assert "Invalid session_name" in resp.json()["detail"]
        backend.session_exists.assert_not_called()

    def test_nul_byte_value_is_rejected_without_echoing_it(self, client):
        """A NUL survives the byte cap but breaks ``Popen`` at window creation, and libtmux
        then logs the whole argv — values included. The shared validator rejects it."""
        resp = _post_env(client, {"TOKEN": "s3cret\x00tail"})

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "NUL byte" in detail
        assert "s3cret" not in detail
        assert get_session_env(SESSION) == {}

    def test_overlong_key_is_rejected(self, client):
        resp = _post_env(client, {"K" * 129: "x"})

        assert resp.status_code == 400
        assert "exceeds 128 bytes" in resp.json()["detail"]
        assert get_session_env(SESSION) == {}

    def test_too_many_entries_in_one_request_is_rejected(self, client):
        resp = _post_env(client, {f"K{i:04d}": "x" for i in range(257)})

        assert resp.status_code == 400
        assert "exceeds the limit of 256" in resp.json()["detail"]
        assert get_session_env(SESSION) == {}


class TestSetSessionEnvMergedBudget:
    """Merge-on-top accumulates across calls, so the bounds ``validate_forwarded_env`` puts on
    one request must also hold for the merged map — otherwise repeated calls grow it past the
    tmux argv limit (E2BIG at window creation, and libtmux logging the argv, values included)."""

    def teardown_method(self):
        clear_session_env(SESSION)

    def test_merge_that_would_exceed_the_entry_cap_is_rejected_and_stores_nothing(self, client):
        seeded = {f"K{i:04d}": "x" for i in range(256)}
        set_session_env(SESSION, seeded)

        resp = _post_env(client, {"ONE_MORE": "y"})

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "exceeds the limit of 256" in detail
        assert "already holds" in detail
        assert get_session_env(SESSION) == seeded

    def test_merge_that_would_exceed_the_argv_budget_is_rejected_and_stores_nothing(self, client):
        # 65 x (5 + 2000 + 3) = 130,520 bytes fits the 131,072-byte budget; one more does not.
        seeded = {f"K{i:04d}": "x" * 2000 for i in range(65)}
        set_session_env(SESSION, seeded)

        resp = _post_env(client, {"K9999": "y" * 2000})

        assert resp.status_code == 400
        assert "total argv budget" in resp.json()["detail"]
        assert get_session_env(SESSION) == seeded

    def test_overwriting_an_existing_key_does_not_count_twice(self, client):
        seeded = {f"K{i:04d}": "x" for i in range(256)}
        set_session_env(SESSION, seeded)

        resp = _post_env(client, {"K0000": "rotated"})

        assert resp.status_code == 200, resp.text
        assert get_session_env(SESSION)["K0000"] == "rotated"
        assert len(get_session_env(SESSION)) == 256


class TestSetSessionEnvExecVectorDenylist:
    """This endpoint mutates a LIVE session's env map, which every worker spawned in it
    afterwards inherits, so it denies the well-known code-execution vectors. The launch path
    (``POST /sessions``) is deliberately unchanged."""

    def teardown_method(self):
        clear_session_env(SESSION)

    @pytest.mark.parametrize(
        "key",
        [
            "LD_PRELOAD",
            "DYLD_INSERT_LIBRARIES",
            "BASH_ENV",
            "PROMPT_COMMAND",
            "NODE_OPTIONS",
            "PATH",
        ],
    )
    def test_exec_vector_is_rejected_with_a_clear_400(self, client, key):
        resp = _post_env(client, {key: "/tmp/payload"})

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert key in detail and "code-execution vector" in detail
        assert "/tmp/payload" not in detail
        assert get_session_env(SESSION) == {}

    def test_one_denied_key_rejects_the_whole_request(self, client):
        set_session_env(SESSION, {"KEEP": "old"})

        resp = _post_env(client, {"TOKEN": "fine", "LD_PRELOAD": "/x.so"})

        assert resp.status_code == 400
        assert get_session_env(SESSION) == {"KEEP": "old"}
