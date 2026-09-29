"""Tests for the per-session forwarded-env store (issue #248)."""

import threading

import pytest

from cli_agent_orchestrator.services import session_env
from cli_agent_orchestrator.services.session_env import (
    clear_session_env,
    get_session_env,
    merge_session_env,
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


def test_merge_puts_delta_on_top_and_keeps_other_keys():
    set_session_env("cao-merge", {"KEEP": "old", "SHARED": "old"})
    try:
        merged = merge_session_env("cao-merge", {"SHARED": "new", "ADDED": "x"})
        assert merged == {"KEEP": "old", "SHARED": "new", "ADDED": "x"}
        assert get_session_env("cao-merge") == merged
    finally:
        clear_session_env("cao-merge")


def test_merge_into_an_unknown_session_creates_the_mapping():
    try:
        assert merge_session_env("cao-merge-new", {"A": "1"}) == {"A": "1"}
        assert get_session_env("cao-merge-new") == {"A": "1"}
    finally:
        clear_session_env("cao-merge-new")


def test_merge_returns_a_copy_not_the_internal_dict():
    try:
        merged = merge_session_env("cao-merge-copy", {"K": "v"})
        merged["K"] = "tampered"
        assert get_session_env("cao-merge-copy") == {"K": "v"}
    finally:
        clear_session_env("cao-merge-copy")


def test_empty_merge_on_an_unknown_session_stores_nothing():
    """Same "empty means none" rule as set_session_env: no empty entry is created."""
    assert merge_session_env("cao-merge-empty", {}) == {}
    assert "cao-merge-empty" not in session_env._session_forwarded_env


def test_validate_sees_the_merged_map_and_a_rejection_stores_nothing():
    set_session_env("cao-merge-validate", {"KEEP": "old"})
    seen = []

    def reject(merged):
        seen.append(dict(merged))
        raise ValueError("over budget")

    try:
        with pytest.raises(ValueError, match="over budget"):
            merge_session_env("cao-merge-validate", {"NEW": "x"}, validate=reject)
        assert seen == [{"KEEP": "old", "NEW": "x"}]
        assert get_session_env("cao-merge-validate") == {"KEEP": "old"}
    finally:
        clear_session_env("cao-merge-validate")


def test_merge_holds_the_lock_across_read_modify_write():
    """A concurrent merge cannot interleave between this merge's read and its write.

    Deterministic rather than a thread storm: from inside the critical section (the
    ``validate`` hook) a second merge is started on another thread, and it must still be
    blocked when this one is about to write. Without the single lock it would finish first,
    and this merge would then overwrite the map it read before that — losing ``B``."""
    session = "cao-merge-atomic"
    other_done = threading.Event()
    threads = []

    def concurrent_merge():
        merge_session_env(session, {"B": "2"})
        other_done.set()

    def start_a_racing_merge(_merged):
        t = threading.Thread(target=concurrent_merge)
        threads.append(t)
        t.start()
        assert not other_done.wait(0.2), "a concurrent merge ran inside this critical section"

    try:
        merge_session_env(session, {"A": "1"}, validate=start_a_racing_merge)
        threads[0].join(timeout=5)
        assert other_done.is_set()
        assert get_session_env(session) == {"A": "1", "B": "2"}
    finally:
        clear_session_env(session)
