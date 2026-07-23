"""Production-wiring regressions: v2 rows are never destroyed by legacy DELETE.

Every v2 destructive action — including the ordinary terminal DELETE path
through ``terminal_service.delete_terminal`` — is lawful only as the effect
of the conditional destructive endpoint. Legacy callers are refused with
zero resource/window mutation; the endpoint's own effect closure (which
passes ``via_destructive_endpoint=True`` after the heartbeat, identity /
fence, dual-exit, and containment decisions) still tears down lawfully.
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


def test_legacy_delete_of_v2_row_refused_with_zero_mutation(v2_metadata, spies):
    terminal_id, generation, _ = v2_metadata
    backend, db_delete, deregister = spies

    # Both the bare legacy DELETE and the generation-conditional legacy
    # form are refused before any subsystem is touched.
    for kwargs in (
        {},
        {"expected_generation": generation, "expected_session": "managed-session"},
    ):
        with pytest.raises(terminals.DestructiveEndpointRequiredError):
            terminals.delete_terminal(terminal_id, **kwargs)

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


def test_v2_companion_binding_without_row_is_refused(monkeypatch, tmp_path):
    """Row-absent v2 generations (companion binding state only) are still
    endpoint-only: legacy cleanup may not take the row-absent kill path."""
    terminal_id = "cccc7777"
    generation = str(uuid.uuid4())
    monkeypatch.setattr(terminals, "get_terminal_metadata", lambda tid: None)
    monkeypatch.setattr(terminals, "get_terminal_metadata_v2", lambda tid: None)
    backend = _Spy()
    monkeypatch.setattr(terminals, "get_backend", lambda: backend)

    from cli_agent_orchestrator import constants
    from cli_agent_orchestrator.services.destructive_endpoint import binding_record_path

    monkeypatch.setattr(constants, "COMPANION_DIR", tmp_path / "companion")
    binding = binding_record_path(constants.COMPANION_DIR, terminal_id, generation)
    binding.parent.mkdir(parents=True, exist_ok=True)
    binding.write_text("{}", encoding="utf-8")
    with pytest.raises(terminals.DestructiveEndpointRequiredError):
        terminals.delete_terminal(
            terminal_id,
            expected_generation=generation,
            expected_session="managed-session",
        )
    assert backend.calls == [], "the row-absent kill path must not run for v2"
