"""Tests for the backend-agnostic terminal env policy (utils/terminal_env.py).

Both terminal backends (tmux, herdr) apply these helpers, so one set of tests
pins one policy. Backend-level tests assert each backend actually calls them.
"""

import logging

import pytest

from cli_agent_orchestrator.constants import RUNTIME_IDENTITY_ENV_KEYS, WORKFLOW_ENV_ALLOWLIST
from cli_agent_orchestrator.utils.forwarded_env import FORWARDED_ENV_MAX_VALUE_BYTES
from cli_agent_orchestrator.utils.terminal_env import (
    MAX_ENV_VALUE_BYTES,
    apply_runtime_identity,
    merge_profile_env,
    profile_env_names,
    within_value_cap,
)


def test_value_cap_is_the_forwarded_env_cap():
    """One argv limit, one number: the server-side cap is the client-side one."""
    assert MAX_ENV_VALUE_BYTES == FORWARDED_ENV_MAX_VALUE_BYTES


class TestWithinValueCap:
    def test_one_byte_under_the_cap_is_accepted(self):
        assert within_value_cap("profile", "K", "x" * (MAX_ENV_VALUE_BYTES - 1)) is True

    def test_exactly_the_cap_is_dropped(self):
        assert within_value_cap("profile", "K", "x" * MAX_ENV_VALUE_BYTES) is False

    def test_cap_counts_utf8_bytes_not_characters(self):
        # "é" is two UTF-8 bytes: half the cap in characters is exactly the cap.
        assert within_value_cap("profile", "K", "é" * (MAX_ENV_VALUE_BYTES // 2)) is False

    def test_drop_warning_names_source_and_key_but_never_the_value(self, caplog):
        secret = "s3cr3t" + "x" * MAX_ENV_VALUE_BYTES
        with caplog.at_level(logging.WARNING):
            within_value_cap("profile", "API_TOKEN", secret)
        assert caplog.messages == [
            f"Dropping profile env var API_TOKEN — value exceeds {MAX_ENV_VALUE_BYTES} bytes"
        ]
        assert "s3cr3t" not in caplog.text


class TestMergeProfileEnv:
    """Profile env is installed configuration (the profile can already launch
    arbitrary executables via mcpServers.command), so the operator-env prefix
    blocklist does not apply. The byte cap does: it protects the argv limit,
    not a trust boundary."""

    def test_none_is_noop(self):
        env = {"HOME": "/home/u"}
        assert merge_profile_env(env, None) == []
        assert env == {"HOME": "/home/u"}

    def test_allows_blocked_prefixes(self):
        """CLAUDE_CONFIG_DIR is the motivating case: point one claude_code
        worker at an alternate config/auth directory from its profile."""
        env: dict[str, str] = {}
        applied = merge_profile_env(env, {"CLAUDE_CONFIG_DIR": "/home/u/.claude-b"})
        assert env["CLAUDE_CONFIG_DIR"] == "/home/u/.claude-b"
        assert applied == ["CLAUDE_CONFIG_DIR"]

    @pytest.mark.parametrize(
        "size, kept",
        [(MAX_ENV_VALUE_BYTES - 1, True), (MAX_ENV_VALUE_BYTES, False)],
        ids=["cap-1-accepted", "cap-dropped"],
    )
    def test_byte_cap_boundary(self, size, kept):
        env: dict[str, str] = {}
        applied = merge_profile_env(env, {"BIG": "x" * size, "OK": "y"})
        assert ("BIG" in env) is kept
        assert env["OK"] == "y"
        assert ("BIG" in applied) is kept

    def test_profile_env_names_are_exactly_what_the_merge_writes(self, caplog):
        """The provider-side view (profile_env_names) cannot drift from the
        backend-side merge: same policy, and it logs nothing."""
        profile_env = {
            "CLAUDE_CONFIG_DIR": "/abs/.claude-b",
            "CAO_WORKFLOW_RUN_ID": "spoof",
            "BIG": "x" * MAX_ENV_VALUE_BYTES,
            "OK": "y",
        }
        with caplog.at_level(logging.WARNING):
            names = profile_env_names(profile_env)
        assert caplog.messages == []
        assert names == merge_profile_env({}, profile_env) == ["CLAUDE_CONFIG_DIR", "OK"]

    def test_wins_over_operator_env(self):
        """Profile env is more specific than session-wide operator env."""
        env = {"KIMI_MODEL_NAME": "from-operator"}
        merge_profile_env(env, {"KIMI_MODEL_NAME": "from-profile"})
        assert env["KIMI_MODEL_NAME"] == "from-profile"


class TestRuntimeIdentity:
    """Runtime-owned identity cannot be set from a profile (PR #665 review, P2).

    A profile's ``env:`` merges after operator/runtime env, so without this a
    profile could replace the workflow routing ids and ``workflow_return``
    would post to the wrong run/step, or spoof the terminal/session identity.
    """

    def test_reserved_set_is_the_runtime_owned_identity(self):
        assert RUNTIME_IDENTITY_ENV_KEYS == {
            "CAO_TERMINAL_ID",
            "CAO_SESSION_NAME",
            "CAO_WORKFLOW_RUN_ID",
            "CAO_WORKFLOW_STEP_ID",
            "CAO_WORKFLOW_GENERATION",
            "CAO_CALLBACK_URL",
            "CAO_CALLBACK_TERMINAL_ID",
        }

    def test_reserved_set_covers_every_workflow_routing_key(self):
        assert WORKFLOW_ENV_ALLOWLIST <= RUNTIME_IDENTITY_ENV_KEYS

    @pytest.mark.parametrize("key", sorted(RUNTIME_IDENTITY_ENV_KEYS))
    def test_profile_cannot_set_a_reserved_key(self, key, caplog):
        env = {key: "runtime-value"}
        with caplog.at_level(logging.WARNING):
            applied = merge_profile_env(env, {key: "spoofed", "OK": "y"})
        assert env[key] == "runtime-value"
        assert env["OK"] == "y"
        assert applied == ["OK"]
        assert caplog.messages == [
            f"Dropping profile env var {key} — name is reserved for CAO's runtime identity"
        ]

    def test_profile_cannot_introduce_a_reserved_key_the_runtime_did_not_set(self):
        """A non-workflow terminal must not be made to claim a workflow run."""
        env: dict[str, str] = {}
        merge_profile_env(env, {"CAO_WORKFLOW_RUN_ID": "run-x", "CAO_WORKFLOW_STEP_ID": "s"})
        assert env == {}

    def test_apply_runtime_identity_overwrites_whatever_was_merged(self):
        env = {"CAO_TERMINAL_ID": "spoofed", "CAO_SESSION_NAME": "evil", "OTHER": "x"}
        apply_runtime_identity(env, "real-tid", "cao-real")
        assert env == {"CAO_TERMINAL_ID": "real-tid", "CAO_SESSION_NAME": "cao-real", "OTHER": "x"}
