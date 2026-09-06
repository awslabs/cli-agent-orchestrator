"""Tests for the shared forwarded-env validator (issue #248)."""

import pytest

from cli_agent_orchestrator.utils.forwarded_env import (
    FORWARDED_ENV_MAX_ENTRIES,
    FORWARDED_ENV_MAX_KEY_BYTES,
    FORWARDED_ENV_MAX_TOTAL_BYTES,
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


def test_nul_byte_in_value_rejected():
    """A NUL byte passes the length check but breaks Popen ("embedded null
    byte") and leaks the argv into logs -- reject it up front (P1)."""
    with pytest.raises(ForwardedEnvError, match="NUL byte"):
        validate_forwarded_env({"TOKEN": "bad\x00value"})


def test_nul_byte_error_never_echoes_value():
    """The NUL-byte rejection must not leak the secret value it rejected."""
    secret = "TOP-SECRET-729"
    with pytest.raises(ForwardedEnvError) as excinfo:
        validate_forwarded_env({"TOKEN": f"{secret}\x00tail"})
    assert secret not in str(excinfo.value)


def test_nul_in_one_var_does_not_leak_a_separate_secret():
    """haofeif's exact repro: {"TOKEN": "TOP-SECRET-729", "BROKEN": "bad\\0value"}.

    A NUL in one var must be rejected before launch, and the *other* var's
    secret value (the one that would ride the argv into libtmux's error log)
    must not appear in the raised error.
    """
    with pytest.raises(ForwardedEnvError) as excinfo:
        validate_forwarded_env({"TOKEN": "TOP-SECRET-729", "BROKEN": "bad\x00value"})
    message = str(excinfo.value)
    assert "NUL byte" in message
    assert "TOP-SECRET-729" not in message


def test_lone_surrogate_value_rejected():
    """A lone surrogate is a valid str (JSON/FastMCP accept it) but is not
    UTF-8 encodable; it must become a ForwardedEnvError, not a raw
    UnicodeEncodeError (P2)."""
    with pytest.raises(ForwardedEnvError, match="not valid UTF-8"):
        validate_forwarded_env({"X": "\ud800"})


def test_lone_surrogate_error_never_echoes_value():
    """The non-UTF-8 rejection names the key only, never the value."""
    with pytest.raises(ForwardedEnvError) as excinfo:
        validate_forwarded_env({"X": "secret-\ud800-suffix"})
    assert "\ud800" not in str(excinfo.value)


def test_valid_multibyte_utf8_value_allowed():
    """Brackets the non-UTF-8 rejection: a legitimate multibyte UTF-8 value
    (accented Latin, CJK, emoji) is valid input and passes unchanged."""
    value = "cafe\u0301 - \u4f60\u597d - \u2615"
    assert validate_forwarded_env({"GREETING": value}) == {"GREETING": value}


def test_oversized_key_rejected():
    with pytest.raises(ForwardedEnvError, match="exceeds"):
        validate_forwarded_env({"K" * (FORWARDED_ENV_MAX_KEY_BYTES + 1): "x"})


def test_key_at_cap_allowed():
    key = "K" * FORWARDED_ENV_MAX_KEY_BYTES
    assert validate_forwarded_env({key: "x"}) == {key: "x"}


def test_too_many_entries_rejected():
    mapping = {f"K{i}": "x" for i in range(FORWARDED_ENV_MAX_ENTRIES + 1)}
    with pytest.raises(ForwardedEnvError, match="exceeds the limit"):
        validate_forwarded_env(mapping)


def test_max_entries_allowed():
    mapping = {f"K{i}": "x" for i in range(FORWARDED_ENV_MAX_ENTRIES)}
    assert validate_forwarded_env(mapping) == mapping


def test_aggregate_argv_budget_rejected():
    """A set that stays under the per-entry caps can still overflow the argv
    in aggregate; the total-bytes budget must reject it (P1)."""
    # Each value is just under the per-value cap, so only the aggregate budget
    # can catch the set. Enough entries to exceed the total but stay within the
    # entry-count limit.
    per_value = FORWARDED_ENV_MAX_VALUE_BYTES - 1
    n_entries = (FORWARDED_ENV_MAX_TOTAL_BYTES // per_value) + 2
    assert n_entries <= FORWARDED_ENV_MAX_ENTRIES  # isolate the aggregate rule
    mapping = {f"K{i}": "x" * per_value for i in range(n_entries)}
    with pytest.raises(ForwardedEnvError, match="total argv budget"):
        validate_forwarded_env(mapping)


def test_aggregate_argv_budget_just_under_allowed():
    """Brackets the aggregate rule: a set whose total argv size lands at or
    just below the budget is accepted. Fixed-width keys (``K0000``..) make the
    per-entry cost exact: 5 (key) + value_bytes + 3 (``-e`` and ``=``)."""
    value_bytes = FORWARDED_ENV_MAX_VALUE_BYTES - 9  # each value well under the per-value cap
    per_entry = 5 + value_bytes + 3
    n_entries = FORWARDED_ENV_MAX_TOTAL_BYTES // per_entry  # largest set that still fits
    assert n_entries <= FORWARDED_ENV_MAX_ENTRIES  # aggregate, not entry-count, is the binding rule
    mapping = {f"K{i:04d}": "x" * value_bytes for i in range(n_entries)}
    assert n_entries * per_entry <= FORWARDED_ENV_MAX_TOTAL_BYTES  # confirm we are under budget
    assert validate_forwarded_env(mapping) == mapping
