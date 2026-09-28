"""Single-observation reads on the runtime registry (#745, #802 stream Q).

Three new registry methods route status/placement/enumeration through one lock
acquisition (or, for ``is_bound``, a lock-only in-memory check) so callers on
worker threads stop splitting a decision across reads the channel loop mutates
between:

- ``observe`` — ``(is_remote, status)`` from one observation, replacing
  ``effective_status``' ``is_remote`` + ``get_status`` pair (Copilot 4104866574,
  4070130514);
- ``live_remote_bindings`` — ``(terminal_id, runtime_id)`` for connected runtimes
  under one lock, replacing ``remote_terminal_ids`` + per-row
  ``runtime_for_terminal`` in session enumeration;
- ``is_bound`` — a lock-only in-memory fast check for the status monitor's loop.
"""

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.runtime_channel.registry import RuntimeChannelRegistry

TID = "aaaa1111"


def _row(runtime_id=None):
    return {"id": TID, "tmux_session": "cao-aaaa1111", "metadata": {"runtime_id": runtime_id}}


@pytest.fixture()
def rows(monkeypatch):
    """A stand-in central database with a call counter and a failure switch."""
    state = {"row": None, "calls": 0, "raise": False}

    def get_terminal_metadata(terminal_id):
        state["calls"] += 1
        if state["raise"]:
            raise RuntimeError("database is unreachable")
        return state["row"]

    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        get_terminal_metadata,
    )
    return state


@pytest.fixture()
def registry():
    return RuntimeChannelRegistry()


class _Conn:
    """Minimal stand-in for a live RuntimeConnection."""


class TestObserve:
    def test_bound_and_connected_reports_remote_with_status(self, registry):
        registry._runtimes["worker-1"] = _Conn()
        registry.bind_terminal(TID, "worker-1")
        registry.set_status(TID, TerminalStatus.COMPLETED)

        assert registry.observe(TID) == (True, TerminalStatus.COMPLETED)

    def test_bound_runtime_disconnected_reads_unknown_not_stale_completed(self, registry):
        # A bound terminal whose runtime unregisters must read UNKNOWN, never the
        # stale COMPLETED that a two-read path could still be holding.
        conn = _Conn()
        registry._runtimes["worker-1"] = conn
        registry.bind_terminal(TID, "worker-1")
        registry.set_status(TID, TerminalStatus.COMPLETED)

        del registry._runtimes["worker-1"]

        assert registry.observe(TID) == (True, TerminalStatus.UNKNOWN)

    def test_placement_read_failure_gives_remote_unknown(self, registry, rows):
        rows["raise"] = True
        assert registry.observe(TID) == (True, TerminalStatus.UNKNOWN)

    def test_local_row_reports_local(self, registry, rows):
        rows["row"] = _row(runtime_id=None)
        assert registry.observe(TID) == (False, None)

    def test_remote_row_but_no_hello_reads_unknown(self, registry, rows):
        # Durable row names a runtime, but no hello has bound it here yet.
        rows["row"] = _row(runtime_id="worker-1")
        assert registry.observe(TID) == (True, TerminalStatus.UNKNOWN)


class TestLiveRemoteBindings:
    def test_returns_terminal_and_runtime_for_connected_runtimes(self, registry):
        registry._runtimes["worker-1"] = _Conn()
        registry.bind_terminal(TID, "worker-1")
        registry.bind_terminal("bbbb2222", "worker-1")

        assert sorted(registry.live_remote_bindings()) == [
            ("aaaa1111", "worker-1"),
            ("bbbb2222", "worker-1"),
        ]

    def test_excludes_disconnected_runtime_bindings(self, registry):
        registry._runtimes["worker-1"] = _Conn()
        registry.bind_terminal(TID, "worker-1")
        # A binding whose runtime never connected (or has dropped) is not live.
        registry.bind_terminal("cccc3333", "worker-gone")

        assert registry.live_remote_bindings() == [("aaaa1111", "worker-1")]


class TestIsBound:
    def test_true_only_when_bound_in_memory(self, registry, rows):
        # A durable remote row is NOT an in-memory binding: is_bound must not read
        # the DB (rows.calls stays 0), so it answers False here.
        rows["row"] = _row(runtime_id="worker-1")
        assert registry.is_bound(TID) is False
        assert rows["calls"] == 0

        registry._runtimes["worker-1"] = _Conn()
        registry.bind_terminal(TID, "worker-1")
        assert registry.is_bound(TID) is True
        assert rows["calls"] == 0
