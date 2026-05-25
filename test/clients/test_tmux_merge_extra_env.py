"""Tests for TmuxClient._merge_extra_env (issue #248).

Server-side defensive validation that mirrors the CLI's --env parser. CLI
already rejects bad entries up front; this layer is the safety net for
callers that bypass the CLI (cao-mcp-server, direct HTTP).
"""

from cli_agent_orchestrator.clients.tmux import TmuxClient


def test_merge_with_none_is_noop():
    env = {"HOME": "/home/u"}
    TmuxClient._merge_extra_env(env, None)
    assert env == {"HOME": "/home/u"}


def test_merge_with_empty_dict_is_noop():
    env = {"HOME": "/home/u"}
    TmuxClient._merge_extra_env(env, {})
    assert env == {"HOME": "/home/u"}


def test_merge_adds_user_supplied_keys():
    env = {"HOME": "/home/u"}
    TmuxClient._merge_extra_env(env, {"MNEMOSYNE_DIR": "/root/mn", "X": "y"})
    assert env["MNEMOSYNE_DIR"] == "/root/mn"
    assert env["X"] == "y"


def test_merge_overrides_inherited_value():
    """An explicit --env entry wins over the inherited value on key collision."""
    env = {"AWS_REGION": "us-east-1"}
    TmuxClient._merge_extra_env(env, {"AWS_REGION": "us-west-2"})
    assert env["AWS_REGION"] == "us-west-2"


def test_merge_drops_blocked_prefix(caplog):
    env: dict[str, str] = {}
    TmuxClient._merge_extra_env(env, {"CLAUDE_SECRET": "x", "OK": "y"})
    assert "CLAUDE_SECRET" not in env
    assert env["OK"] == "y"


def test_merge_keeps_allowlisted_claude_auth_var():
    env: dict[str, str] = {}
    TmuxClient._merge_extra_env(env, {"CLAUDE_CODE_USE_BEDROCK": "1"})
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"


def test_merge_drops_value_at_or_above_cap():
    env: dict[str, str] = {}
    TmuxClient._merge_extra_env(env, {"BIG": "x" * 2048, "SMALL": "x" * 2047})
    assert "BIG" not in env
    assert env["SMALL"] == "x" * 2047


def test_is_blocked_env_key_classification():
    assert TmuxClient._is_blocked_env_key("CLAUDE_SESSION_ID") is True
    assert TmuxClient._is_blocked_env_key("CODEX_TOKEN") is True
    assert TmuxClient._is_blocked_env_key("__MISE_WATCH") is True
    # Allowlist matches the full key, not a prefix — only the exact entries
    # in _BLOCKED_PREFIX_ALLOWLIST are exempted from the blocked prefixes.
    assert TmuxClient._is_blocked_env_key("CLAUDE_CODE_USE_BEDROCK") is False
    assert TmuxClient._is_blocked_env_key("CLAUDE_CODE_SKIP_FOUNDRY_AUTH") is False
    # Unrelated keys aren't blocked.
    assert TmuxClient._is_blocked_env_key("AWS_REGION") is False
    assert TmuxClient._is_blocked_env_key("MNEMOSYNE_DIR") is False
