"""Tests for the per-session forwarded-env store (issue #248)."""

from cli_agent_orchestrator.services.session_env import (
    clear_session_env,
    get_session_env,
    set_session_env,
)


def test_get_returns_empty_dict_for_unknown_session():
    assert get_session_env("cao-unknown-xyz") == {}


def test_set_and_get_roundtrip():
    set_session_env("cao-roundtrip", {"FOO": "bar", "BAZ": "qux"})
    try:
        assert get_session_env("cao-roundtrip") == {"FOO": "bar", "BAZ": "qux"}
    finally:
        clear_session_env("cao-roundtrip")


def test_get_returns_a_copy_not_the_internal_dict():
    """Caller mutation of the returned dict must not leak into the store."""
    set_session_env("cao-copy", {"K": "v"})
    try:
        got = get_session_env("cao-copy")
        got["K"] = "tampered"
        got["NEW"] = "x"
        assert get_session_env("cao-copy") == {"K": "v"}
    finally:
        clear_session_env("cao-copy")


def test_set_with_empty_dict_clears_mapping():
    """Passing an empty dict drops the entry — avoids two ways to say "none"."""
    set_session_env("cao-empty", {"X": "1"})
    set_session_env("cao-empty", {})
    assert get_session_env("cao-empty") == {}


def test_clear_is_idempotent():
    clear_session_env("cao-never-set")  # must not raise
    set_session_env("cao-clear", {"X": "1"})
    clear_session_env("cao-clear")
    clear_session_env("cao-clear")  # second call — still must not raise
    assert get_session_env("cao-clear") == {}


def test_overwrite_replaces_previous_mapping():
    """A second set fully replaces the prior mapping (not merge)."""
    set_session_env("cao-overwrite", {"A": "1", "B": "2"})
    set_session_env("cao-overwrite", {"C": "3"})
    try:
        assert get_session_env("cao-overwrite") == {"C": "3"}
    finally:
        clear_session_env("cao-overwrite")
