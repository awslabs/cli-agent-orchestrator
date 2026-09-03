"""Tests for the shared forwarded-env validator (issue #248)."""

import pytest

from cli_agent_orchestrator.utils.forwarded_env import (
    FORWARDED_ENV_MAX_VALUE_BYTES,
    ForwardedEnvError,
    validate_forwarded_env,
)


def test_valid_mapping_returned_unchanged():
    """A well-formed mapping is returned as a plain dict, values intact."""
    result = validate_forwarded_env({"FOO": "bar", "AWS_REGION": "us-west-2"})
    assert result == {"FOO": "bar", "AWS_REGION": "us-west-2"}


def test_empty_value_is_allowed():
    assert validate_forwarded_env({"EMPTY": ""}) == {"EMPTY": ""}


def test_value_with_url_query_is_preserved():
    """Values are opaque; '=' and '&' in a value are not re-parsed."""
    assert validate_forwarded_env({"URL": "https://x?a=1&b=2"}) == {"URL": "https://x?a=1&b=2"}


@pytest.mark.parametrize("bad_key", ["1FOO", "FOO-BAR", "FOO BAR", "föö", ""])
def test_invalid_key_rejected(bad_key):
    with pytest.raises(ForwardedEnvError, match="must match"):
        validate_forwarded_env({bad_key: "x"})


@pytest.mark.parametrize("blocked_key", ["CLAUDE_SECRET", "CODEX_TOKEN", "__MISE_X"])
def test_blocked_prefix_rejected(blocked_key):
    with pytest.raises(ForwardedEnvError, match="blocked prefix"):
        validate_forwarded_env({blocked_key: "x"})


@pytest.mark.parametrize(
    "allowed_key",
    [
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_SKIP_VERTEX_AUTH",
    ],
)
def test_allowlisted_claude_flags_permitted(allowed_key):
    """The documented Claude Code auth flags are exempt from the block."""
    assert validate_forwarded_env({allowed_key: "1"}) == {allowed_key: "1"}


def test_oversized_value_rejected():
    with pytest.raises(ForwardedEnvError, match="exceeds"):
        validate_forwarded_env({"BIG": "x" * FORWARDED_ENV_MAX_VALUE_BYTES})


def test_value_just_under_cap_allowed():
    value = "x" * (FORWARDED_ENV_MAX_VALUE_BYTES - 1)
    assert validate_forwarded_env({"SMALL": value}) == {"SMALL": value}


def test_error_message_never_echoes_value():
    """A rejected value must not leak into the error string (secret safety)."""
    secret = "super-secret-token-value"
    with pytest.raises(ForwardedEnvError) as excinfo:
        # Blocked prefix triggers before any value check; value must not appear.
        validate_forwarded_env({"CLAUDE_LEAK": secret})
    assert secret not in str(excinfo.value)
