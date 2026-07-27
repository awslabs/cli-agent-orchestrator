"""Tmux window ids may be reused after the tmux server restarts."""

from __future__ import annotations

import sqlite3

import pytest

from cli_agent_orchestrator.services import resource_registry as rr
from cli_agent_orchestrator.services import terminal_service as terminals

OLD_ENTRY = "managed-deadbeef-111111111111"
NEW_ENTRY = "managed-cafebabe-222222222222"
OBSERVED_ID = "@28"


def _declare_window(registry: rr.ResourceRegistry, entry_id: str, terminal_id: str) -> None:
    registry.declare(
        entry_id=entry_id,
        kind="tmux_window",
        protocol_vintage="v2",
        terminal_id=terminal_id,
        generation=f"{terminal_id}-generation",
        owner="fork",
        ownership="owned",
        constructor_id="terminal_service.create_terminal",
        deleter_id="terminal_service.delete_terminal",
        rollback_rule="generation-isolated",
        desired_tmux_name=entry_id,
        actor_id="test",
    )


def _created_window(registry: rr.ResourceRegistry, entry_id: str, terminal_id: str) -> None:
    _declare_window(registry, entry_id, terminal_id)
    registry.register_created(
        entry_id,
        actor_id="test",
        observed={"observed_tmux_id": OBSERVED_ID},
        existence_receipt_digest="1" * 64,
    )


def _new_identity() -> dict[str, str]:
    return {
        "pane_id": "%42",
        "window_id": OBSERVED_ID,
        "session_id": "$0",
        "server_socket_path": "/private/tmp/tmux-501/default",
        "pane_pid": "4242",
    }


def _new_pane() -> dict[str, str]:
    return {
        **_new_identity(),
        "window_name": NEW_ENTRY,
        "session_name": "cao-restarted",
        "dead": "0",
    }


class _Backend:
    def __init__(self, panes):
        self.panes = panes

    def observe_pane_identities(self):
        return self.panes


@pytest.fixture
def registry(tmp_path, monkeypatch):
    value = rr.ResourceRegistry(tmp_path / "resource-registry.sqlite")
    monkeypatch.setattr(rr, "_REGISTRY_SINGLETON", value)
    return value


def test_reused_id_retires_only_a_proven_absent_old_window(registry, monkeypatch):
    _created_window(registry, OLD_ENTRY, "deadbeef")
    _declare_window(registry, NEW_ENTRY, "cafebabe")
    monkeypatch.setattr(
        terminals,
        "get_backend",
        lambda: _Backend({"%42": _new_pane()}),
    )

    terminals._retire_reused_tmux_observation(NEW_ENTRY, _new_identity())
    registry.register_created(
        NEW_ENTRY,
        actor_id="test",
        observed={"observed_tmux_id": OBSERVED_ID},
        existence_receipt_digest="2" * 64,
    )

    old = registry.resolve(OLD_ENTRY)
    new = registry.resolve(NEW_ENTRY)
    assert old["lifecycle_state"] == "deleted"
    assert [event["to_state"] for event in old["events"][-3:]] == [
        "draining",
        "closed",
        "deleted",
    ]
    assert new["lifecycle_state"] == "created"
    assert new["observed_tmux_id"] == OBSERVED_ID


@pytest.mark.parametrize("old_state", ["active", "draining"])
def test_reused_id_retires_post_crash_live_states(registry, monkeypatch, old_state):
    _created_window(registry, OLD_ENTRY, "deadbeef")
    registry.activate(
        OLD_ENTRY,
        actor_id="test",
        existence_receipt_digest="2" * 64,
    )
    if old_state == "draining":
        registry.drain(OLD_ENTRY, actor_id="test")
    _declare_window(registry, NEW_ENTRY, "cafebabe")
    monkeypatch.setattr(
        terminals,
        "get_backend",
        lambda: _Backend({"%42": _new_pane()}),
    )

    terminals._retire_reused_tmux_observation(NEW_ENTRY, _new_identity())
    registry.register_created(
        NEW_ENTRY,
        actor_id="test",
        observed={"observed_tmux_id": OBSERVED_ID},
        existence_receipt_digest="3" * 64,
    )

    assert registry.resolve(OLD_ENTRY)["lifecycle_state"] == "deleted"
    assert registry.resolve(NEW_ENTRY)["observed_tmux_id"] == OBSERVED_ID


@pytest.mark.parametrize("crash_method", ["close", "delete"])
def test_crash_during_retirement_redrives_without_duplicate_events(
    registry, monkeypatch, crash_method
):
    _created_window(registry, OLD_ENTRY, "deadbeef")
    _declare_window(registry, NEW_ENTRY, "cafebabe")
    monkeypatch.setattr(
        terminals,
        "get_backend",
        lambda: _Backend({"%42": _new_pane()}),
    )
    original = getattr(registry, crash_method)

    def crash_once(*args, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(registry, crash_method, crash_once)
    with pytest.raises(RuntimeError, match="simulated crash"):
        terminals._retire_reused_tmux_observation(NEW_ENTRY, _new_identity())

    monkeypatch.setattr(registry, crash_method, original)
    terminals._retire_reused_tmux_observation(NEW_ENTRY, _new_identity())
    registry.register_created(
        NEW_ENTRY,
        actor_id="test",
        observed={"observed_tmux_id": OBSERVED_ID},
        existence_receipt_digest="4" * 64,
    )

    old = registry.resolve(OLD_ENTRY)
    transitions = [event["to_state"] for event in old["events"]]
    assert transitions == ["declared", "created", "draining", "closed", "deleted"]
    assert old["lifecycle_state"] == "deleted"
    assert registry.resolve(NEW_ENTRY)["observed_tmux_id"] == OBSERVED_ID


def test_readable_inventory_that_still_contains_old_name_refuses(registry, monkeypatch):
    _created_window(registry, OLD_ENTRY, "deadbeef")
    _declare_window(registry, NEW_ENTRY, "cafebabe")
    old_pane = {
        **_new_pane(),
        "pane_id": "%7",
        "window_id": "@7",
        "window_name": OLD_ENTRY,
        "pane_pid": "7000",
    }
    monkeypatch.setattr(
        terminals,
        "get_backend",
        lambda: _Backend({"%42": _new_pane(), "%7": old_pane}),
    )

    with pytest.raises(rr.RegistryConflict, match="still present"):
        terminals._retire_reused_tmux_observation(NEW_ENTRY, _new_identity())

    assert registry.resolve(OLD_ENTRY)["lifecycle_state"] == "created"
    assert registry.resolve(NEW_ENTRY)["lifecycle_state"] == "declared"


def test_unreadable_inventory_refuses_without_retiring_old_owner(registry, monkeypatch):
    _created_window(registry, OLD_ENTRY, "deadbeef")
    _declare_window(registry, NEW_ENTRY, "cafebabe")
    monkeypatch.setattr(terminals, "get_backend", lambda: _Backend(None))

    with pytest.raises(rr.RegistryConflict, match="inventory is unreadable"):
        terminals._retire_reused_tmux_observation(NEW_ENTRY, _new_identity())

    assert registry.resolve(OLD_ENTRY)["lifecycle_state"] == "created"
    assert registry.resolve(NEW_ENTRY)["lifecycle_state"] == "declared"


def test_inventory_must_bind_the_exact_new_pane_identity(registry, monkeypatch):
    _created_window(registry, OLD_ENTRY, "deadbeef")
    _declare_window(registry, NEW_ENTRY, "cafebabe")
    wrong = {**_new_pane(), "pane_pid": "9999"}
    monkeypatch.setattr(
        terminals,
        "get_backend",
        lambda: _Backend({"%42": wrong}),
    )

    with pytest.raises(rr.RegistryConflict, match="exact window identity"):
        terminals._retire_reused_tmux_observation(NEW_ENTRY, _new_identity())

    assert registry.resolve(OLD_ENTRY)["lifecycle_state"] == "created"
    assert registry.resolve(NEW_ENTRY)["lifecycle_state"] == "declared"


def test_no_registered_collision_requires_no_inventory(registry, monkeypatch):
    _declare_window(registry, NEW_ENTRY, "cafebabe")

    class _UnexpectedBackend:
        def observe_pane_identities(self):
            raise AssertionError("no inventory is needed without a collision")

    monkeypatch.setattr(terminals, "get_backend", lambda: _UnexpectedBackend())
    terminals._retire_reused_tmux_observation(NEW_ENTRY, _new_identity())

    assert registry.resolve(NEW_ENTRY)["lifecycle_state"] == "declared"


def test_observed_tmux_uniqueness_ddl_is_unchanged(registry):
    with sqlite3.connect(registry._path) as connection:
        (sql,) = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='resource_live_observed_tmux'"
        ).fetchone()

    assert "UNIQUE INDEX resource_live_observed_tmux" in sql
    assert "lifecycle_state IN ('created','active','draining')" in sql
