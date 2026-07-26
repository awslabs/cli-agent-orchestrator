"""Production-wiring regressions for exact-generation v2 retirement.

Bare legacy deletion is refused with zero resource/window mutation.  The
ordinary conductor retirement path may tear down a v2 row only when it supplies
the exact generation and session; the endpoint's stronger effect closure
remains supported.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from cli_agent_orchestrator.services import terminal_service as terminals


class _Spy:
    """Records every call; configurable return values by attribute."""

    def __init__(self, **returns):
        self.calls = []
        self._returns = returns

    def __getattr__(self, name):
        def _method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self._returns.get(name)

        return _method


@pytest.fixture
def v2_metadata():
    terminal_id = "a1b2c3d4"
    generation = str(uuid.uuid4())
    return (
        terminal_id,
        generation,
        {
            "terminal_id": terminal_id,
            "generation": generation,
            "tmux_session": "managed-session",
            "tmux_window": terminals.managed_window_name(terminal_id, generation),
            "provider": "codex",
            "agent_profile": "codex",
            "protocol_vintage": "v2",
        },
    )


@pytest.fixture
def spies(monkeypatch, v2_metadata, tmp_path):
    terminal_id, generation, metadata = v2_metadata
    backend = _Spy(get_history="history", get_pane_working_directory=str(tmp_path))
    monkeypatch.setattr(terminals, "get_terminal_metadata", lambda tid: None)
    monkeypatch.setattr(terminals, "get_terminal_metadata_v2", lambda tid: dict(metadata))
    monkeypatch.setattr(terminals, "get_backend", lambda: backend)
    monkeypatch.setattr(terminals, "fifo_manager", _Spy())
    monkeypatch.setattr(terminals, "status_monitor", _Spy())
    monkeypatch.setattr(terminals, "provider_manager", _Spy())
    monkeypatch.setattr(terminals, "get_herdr_inbox_service", lambda: None)
    monkeypatch.setattr(terminals, "TERMINAL_LOG_DIR", tmp_path)
    monkeypatch.setattr(terminals, "dispatch_plugin_event", lambda *a, **k: None)
    db_delete = _Spy(db_delete_terminal_v2_if_generation=True)
    monkeypatch.setattr(
        terminals,
        "db_delete_terminal_v2_if_generation",
        db_delete.db_delete_terminal_v2_if_generation,
    )
    deregister = _Spy()
    monkeypatch.setattr(
        terminals, "_deregister_v2_terminal_resources", deregister._deregister_v2_terminal_resources
    )
    return backend, db_delete, deregister


def test_bare_delete_of_v2_row_refused_with_zero_mutation(v2_metadata, spies):
    terminal_id, generation, _ = v2_metadata
    backend, db_delete, deregister = spies

    with pytest.raises(terminals.DestructiveEndpointRequiredError):
        terminals.delete_terminal(terminal_id)

    assert backend.calls == [], f"window/history mutation leaked: {backend.calls}"
    assert db_delete.calls == [], "the v2 row must survive"
    assert deregister.calls == [], "registry entries must survive"


def test_exact_generation_delete_retires_v2_row(v2_metadata, spies):
    terminal_id, generation, _ = v2_metadata
    backend, db_delete, deregister = spies

    deleted = terminals.delete_terminal(
        terminal_id,
        expected_generation=generation,
        expected_session="managed-session",
    )

    assert deleted is True
    assert (
        "kill_window",
        ("managed-session", terminals.managed_window_name(terminal_id, generation)),
        {},
    ) in backend.calls
    assert db_delete.calls != [], "exact retirement deletes the v2 row"
    assert deregister.calls != [], "exact retirement drains registry entries"


def test_unreadable_window_state_preserves_v2_row(v2_metadata, spies):
    terminal_id, generation, _ = v2_metadata
    backend, db_delete, deregister = spies

    def _unreadable(*_args, **_kwargs):
        raise RuntimeError("tmux server unreadable")

    backend.window_exists = _unreadable

    with pytest.raises(RuntimeError, match="server unreadable"):
        terminals.delete_terminal(
            terminal_id,
            expected_generation=generation,
            expected_session="managed-session",
        )

    assert db_delete.calls == [], "an unreadable server is not proof that the window is gone"
    assert deregister.calls == [], "registry entries must survive an ambiguous teardown"


@pytest.mark.parametrize(
    ("generation", "session"),
    [
        (str(uuid.uuid4()), "managed-session"),
        (None, "wrong-session"),
    ],
)
def test_wrong_v2_identity_refuses_with_zero_mutation(v2_metadata, spies, generation, session):
    terminal_id, expected_generation, _ = v2_metadata
    backend, db_delete, deregister = spies

    with pytest.raises(terminals.TerminalGenerationMismatchError):
        terminals.delete_terminal(
            terminal_id,
            expected_generation=generation or expected_generation,
            expected_session=session,
        )

    assert backend.calls == [], f"window/history mutation leaked: {backend.calls}"
    assert db_delete.calls == [], "the v2 row must survive"
    assert deregister.calls == [], "registry entries must survive"


def test_endpoint_effect_still_tears_down_lawfully(v2_metadata, spies):
    terminal_id, generation, _ = v2_metadata
    backend, db_delete, deregister = spies

    deleted = terminals.delete_terminal(
        terminal_id,
        expected_generation=generation,
        expected_session="managed-session",
        via_destructive_endpoint=True,
    )

    assert deleted is True
    assert (
        "kill_window",
        ("managed-session", terminals.managed_window_name(terminal_id, generation)),
        {},
    ) in backend.calls
    assert db_delete.calls != [], "the endpoint effect deletes the v2 row"
    assert deregister.calls != [], "the endpoint effect drains registry entries"


def test_v1_managed_delete_is_not_gated(monkeypatch, tmp_path):
    """Preservation: v1 managed rows keep the pre-existing conditional path."""
    terminal_id = "bbbb9999"
    generation = str(uuid.uuid4())
    metadata = {
        "terminal_id": terminal_id,
        "generation": generation,
        "tmux_session": "managed-session",
        "tmux_window": terminals.managed_window_name(terminal_id, generation),
        "provider": "codex",
        "agent_profile": "codex",
    }
    backend = _Spy(get_history="history", get_pane_working_directory=str(tmp_path))
    monkeypatch.setattr(terminals, "get_terminal_metadata", lambda tid: dict(metadata))
    monkeypatch.setattr(terminals, "get_terminal_metadata_v2", lambda tid: None)
    monkeypatch.setattr(terminals, "get_backend", lambda: backend)
    monkeypatch.setattr(terminals, "fifo_manager", _Spy())
    monkeypatch.setattr(terminals, "status_monitor", _Spy())
    monkeypatch.setattr(terminals, "provider_manager", _Spy())
    monkeypatch.setattr(terminals, "get_herdr_inbox_service", lambda: None)
    monkeypatch.setattr(terminals, "TERMINAL_LOG_DIR", tmp_path)
    monkeypatch.setattr(terminals, "dispatch_plugin_event", lambda *a, **k: None)
    monkeypatch.setattr(terminals, "db_delete_terminal_if_generation", lambda tid, gen: True)

    deleted = terminals.delete_terminal(
        terminal_id,
        expected_generation=generation,
        expected_session="managed-session",
    )
    assert deleted is True


def test_v2_companion_binding_without_row_uses_exact_window(monkeypatch, tmp_path):
    """Row-absent recovery remains bound to the generation-derived window."""
    terminal_id = "cccc7777"
    generation = str(uuid.uuid4())
    monkeypatch.setattr(terminals, "get_terminal_metadata", lambda tid: None)
    monkeypatch.setattr(terminals, "get_terminal_metadata_v2", lambda tid: None)
    backend = _Spy()
    monkeypatch.setattr(terminals, "get_backend", lambda: backend)

    monkeypatch.setattr(terminals, "provider_manager", _Spy())
    monkeypatch.setattr(terminals, "db_delete_terminal_if_generation", lambda *a: False)

    assert terminals.delete_terminal(
        terminal_id,
        expected_generation=generation,
        expected_session="managed-session",
    )
    assert (
        "kill_window",
        ("managed-session", terminals.managed_window_name(terminal_id, generation)),
        {},
    ) in backend.calls


def test_row_absent_delete_deregisters_exact_generation_registry_entries(monkeypatch, tmp_path):
    """A row-absent generation's live registry entries are drained on cleanup.

    Recovery reruns teardown against the immutable generation window when the
    terminal row is already gone (a crash after teardown, or a prior cleanup).
    That path kills the window but used to skip the resource-registry
    deregister, so a cleaned row-absent generation left live registry entries.
    Deregistration is keyed by exact terminal + generation, so a replacement
    incarnation's entries survive untouched (zero effect on a replacement).
    """
    from cli_agent_orchestrator.services import resource_registry as rr

    terminal_id = "d0e1f2a3"
    generation = str(uuid.uuid4())
    replacement_generation = str(uuid.uuid4())

    # A real registry with two declared managed-window entries: the target
    # generation and a replacement incarnation reusing the terminal id.
    registry = rr.ResourceRegistry(tmp_path / "registry.sqlite")
    for gen in (generation, replacement_generation):
        window = terminals.managed_window_name(terminal_id, gen)
        registry.declare(
            entry_id=window,
            kind="tmux_window",
            protocol_vintage="v2",
            terminal_id=terminal_id,
            generation=gen,
            owner="fork",
            ownership="owned",
            constructor_id="terminal_service.create_terminal",
            deleter_id="terminal_service.delete_terminal",
            rollback_rule="generation-isolated",
            actor_id="terminal_service.create_terminal",
            desired_tmux_name=window,
        )
    monkeypatch.setattr(rr, "get_resource_registry", lambda *a, **k: registry)

    # Row-absent: no terminal metadata rows, and the window is already gone
    # (a bare spy reports window_exists as None -> absent).
    monkeypatch.setattr(terminals, "get_terminal_metadata", lambda tid: None)
    monkeypatch.setattr(terminals, "get_terminal_metadata_v2", lambda tid: None)
    monkeypatch.setattr(terminals, "get_backend", lambda: _Spy())
    monkeypatch.setattr(terminals, "fifo_manager", _Spy())
    monkeypatch.setattr(terminals, "status_monitor", _Spy())
    monkeypatch.setattr(terminals, "provider_manager", _Spy())
    monkeypatch.setattr(terminals, "get_herdr_inbox_service", lambda: None)
    monkeypatch.setattr(terminals, "TERMINAL_LOG_DIR", tmp_path)
    monkeypatch.setattr(terminals, "dispatch_plugin_event", lambda *a, **k: None)

    deleted = terminals.delete_terminal(
        terminal_id,
        expected_generation=generation,
        expected_session="managed-session",
    )

    assert deleted is True
    # The exact generation's entry was deregistered (declared -> aborted on
    # verified window absence); the replacement incarnation is untouched.
    target = registry.resolve(terminals.managed_window_name(terminal_id, generation))
    replacement = registry.resolve(
        terminals.managed_window_name(terminal_id, replacement_generation)
    )
    assert target["lifecycle_state"] == "aborted"
    assert replacement["lifecycle_state"] == "declared"
