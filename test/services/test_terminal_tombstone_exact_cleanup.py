"""A retained tombstone is torn down by exact identity, or reported incomplete.

``dismantle_terminal_runtime`` is the only place that decides whether a
deferred-init tombstone's runtime can be marked reclaimed. These tests pin the
two halves of that decision: the destructive backend call is keyed by terminal
id (never by the reusable session/window name), and anything other than
"exactly DELETED or ABSENT, and every other component succeeded" leaves the
runtime incomplete so a later pass can retry it.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.backends.base import (
    TerminalCleanupOutcome,
    TerminalCleanupResult,
)
from cli_agent_orchestrator.services import terminal_service

TOMBSTONE = {
    "tmux_session": "cao-x",
    "tmux_window": "coder-3",
    "deferred_init_external_owner": True,
}


class FakeBackend:
    """Records the destructive backend calls dismantle is allowed to make."""

    def __init__(self, result=None, raises=None):
        self._result = result
        self._raises = raises
        self.cleanup_calls = []
        self.kill_window_calls = []
        self.stop_pipe_pane_calls = []

    def cleanup_terminal_exact(
        self, terminal_id, session_name=None, window_name=None, *, close=True
    ):
        self.cleanup_calls.append((terminal_id, session_name, window_name, close))
        if self._raises is not None:
            raise self._raises
        return self._result

    def kill_window(self, session_name, window_name):
        self.kill_window_calls.append((session_name, window_name))
        return True

    def stop_pipe_pane(self, session_name, window_name):
        self.stop_pipe_pane_calls.append((session_name, window_name))


def outcome(value, detail="test"):
    return TerminalCleanupResult(value, detail)


@pytest.fixture
def runtime(monkeypatch):
    """Neutralize the per-terminal side services so only the backend shows."""
    monkeypatch.setattr(terminal_service, "get_herdr_inbox_service", lambda: None)
    monkeypatch.setattr(terminal_service.fifo_manager, "stop_reader", lambda terminal_id: True)
    monkeypatch.setattr(terminal_service.status_monitor, "clear_terminal", lambda terminal_id: None)
    monkeypatch.setattr(
        terminal_service.provider_manager, "cleanup_provider", lambda terminal_id: True
    )


def use_backend(monkeypatch, backend):
    import cli_agent_orchestrator.backends.registry as registry

    monkeypatch.setattr(registry, "_backend", backend)


class TestTombstoneUsesExactIdentity:
    @pytest.mark.parametrize(
        "value",
        [
            TerminalCleanupOutcome.UNKNOWN,
            TerminalCleanupOutcome.STILL_PRESENT,
        ],
    )
    def test_an_unproven_cleanup_keeps_the_runtime_incomplete(self, monkeypatch, runtime, value):
        backend = FakeBackend(outcome(value))
        use_backend(monkeypatch, backend)

        complete = terminal_service.dismantle_terminal_runtime("tid-1", dict(TOMBSTONE))

        assert complete is False
        assert backend.cleanup_calls == [("tid-1", "cao-x", "coder-3", True)]
        # The label-addressed pair must never run for a tombstone: the name may
        # now belong to a replacement.
        assert backend.kill_window_calls == []
        assert backend.stop_pipe_pane_calls == []

    @pytest.mark.parametrize(
        "value",
        [
            TerminalCleanupOutcome.DELETED,
            TerminalCleanupOutcome.ABSENT,
        ],
    )
    def test_a_proven_cleanup_completes_the_runtime(self, monkeypatch, runtime, value):
        backend = FakeBackend(outcome(value))
        use_backend(monkeypatch, backend)

        complete = terminal_service.dismantle_terminal_runtime("tid-1", dict(TOMBSTONE))

        assert complete is True
        assert backend.cleanup_calls == [("tid-1", "cao-x", "coder-3", True)]
        assert backend.kill_window_calls == []
        assert backend.stop_pipe_pane_calls == []

    def test_a_raising_cleanup_keeps_the_runtime_incomplete(self, monkeypatch, runtime):
        backend = FakeBackend(raises=RuntimeError("backend exploded"))
        use_backend(monkeypatch, backend)

        complete = terminal_service.dismantle_terminal_runtime("tid-1", dict(TOMBSTONE))

        assert complete is False
        assert backend.kill_window_calls == []

    def test_kill_window_false_makes_it_an_identity_proof_only(self, monkeypatch, runtime):
        backend = FakeBackend(outcome(TerminalCleanupOutcome.ABSENT))
        use_backend(monkeypatch, backend)

        complete = terminal_service.dismantle_terminal_runtime(
            "tid-1", dict(TOMBSTONE), kill_window=False
        )

        assert complete is True
        assert backend.cleanup_calls == [("tid-1", "cao-x", "coder-3", False)]

    def test_a_deferred_failure_tombstone_uses_exact_identity_too(self, monkeypatch, runtime):
        backend = FakeBackend(outcome(TerminalCleanupOutcome.UNKNOWN))
        use_backend(monkeypatch, backend)
        metadata = {
            "tmux_session": "cao-x",
            "tmux_window": "coder-3",
            "deferred_init_failure": "{}",
        }

        assert terminal_service.dismantle_terminal_runtime("tid-1", metadata) is False
        assert backend.cleanup_calls == [("tid-1", "cao-x", "coder-3", True)]

    def test_an_already_reclaimed_tombstone_uses_exact_identity_too(self, monkeypatch, runtime):
        backend = FakeBackend(outcome(TerminalCleanupOutcome.ABSENT))
        use_backend(monkeypatch, backend)
        metadata = {
            "tmux_session": "cao-x",
            "tmux_window": "coder-3",
            "deferred_init_runtime_reclaimed": True,
        }

        assert terminal_service.dismantle_terminal_runtime("tid-1", metadata) is True
        assert backend.cleanup_calls == [("tid-1", "cao-x", "coder-3", True)]

    def test_metadata_without_tmux_names_still_uses_the_terminal_id(self, monkeypatch, runtime):
        backend = FakeBackend(outcome(TerminalCleanupOutcome.ABSENT))
        use_backend(monkeypatch, backend)

        assert (
            terminal_service.dismantle_terminal_runtime(
                "tid-1", {"deferred_init_external_owner": True}
            )
            is True
        )
        assert backend.cleanup_calls == [("tid-1", None, None, True)]


class TestOtherComponentsStillGateCompletion:
    def test_a_proven_cleanup_plus_failing_provider_cleanup_is_incomplete(
        self, monkeypatch, runtime
    ):
        backend = FakeBackend(outcome(TerminalCleanupOutcome.ABSENT))
        use_backend(monkeypatch, backend)
        monkeypatch.setattr(
            terminal_service.provider_manager, "cleanup_provider", lambda terminal_id: False
        )

        assert terminal_service.dismantle_terminal_runtime("tid-1", dict(TOMBSTONE)) is False

    def test_a_proven_cleanup_plus_a_leaked_fifo_reader_is_incomplete(self, monkeypatch, runtime):
        backend = FakeBackend(outcome(TerminalCleanupOutcome.DELETED))
        use_backend(monkeypatch, backend)
        monkeypatch.setattr(terminal_service.fifo_manager, "stop_reader", lambda terminal_id: False)

        assert terminal_service.dismantle_terminal_runtime("tid-1", dict(TOMBSTONE)) is False

    def test_a_raising_fifo_stop_is_incomplete(self, monkeypatch, runtime):
        backend = FakeBackend(outcome(TerminalCleanupOutcome.DELETED))
        use_backend(monkeypatch, backend)

        def boom(terminal_id):
            raise OSError("fifo busy")

        monkeypatch.setattr(terminal_service.fifo_manager, "stop_reader", boom)

        assert terminal_service.dismantle_terminal_runtime("tid-1", dict(TOMBSTONE)) is False


class TestOrdinaryTerminalKeepsLabelBehavior:
    def test_a_live_terminal_still_uses_the_label_addressed_steps(self, monkeypatch, runtime):
        backend = FakeBackend(outcome(TerminalCleanupOutcome.UNKNOWN))
        use_backend(monkeypatch, backend)
        metadata = {"tmux_session": "cao-x", "tmux_window": "coder-3"}

        assert terminal_service.dismantle_terminal_runtime("tid-1", metadata) is True
        assert backend.cleanup_calls == []
        assert backend.stop_pipe_pane_calls == [("cao-x", "coder-3")]
        assert backend.kill_window_calls == [("cao-x", "coder-3")]

    def test_a_live_terminal_with_kill_window_false_touches_no_backend(self, monkeypatch, runtime):
        backend = FakeBackend(outcome(TerminalCleanupOutcome.UNKNOWN))
        use_backend(monkeypatch, backend)
        metadata = {"tmux_session": "cao-x", "tmux_window": "coder-3"}

        assert terminal_service.dismantle_terminal_runtime("tid-1", metadata, kill_window=False)
        assert backend.cleanup_calls == []
        assert backend.kill_window_calls == []
        assert backend.stop_pipe_pane_calls == []


class TestRuntimeReclaimedIsGatedOnDismantle:
    @pytest.mark.parametrize(
        "value, expect_reclaimed",
        [
            (TerminalCleanupOutcome.UNKNOWN, False),
            (TerminalCleanupOutcome.STILL_PRESENT, False),
            (TerminalCleanupOutcome.ABSENT, True),
            (TerminalCleanupOutcome.DELETED, True),
        ],
    )
    def test_the_reclaim_marker_is_only_written_for_a_proven_dismantle(
        self, monkeypatch, runtime, value, expect_reclaimed
    ):
        """The durable marker is written off ``dismantle``'s return value."""
        import cli_agent_orchestrator.clients.database as database
        from cli_agent_orchestrator.services import herdr_inbox_service

        backend = FakeBackend(outcome(value))
        use_backend(monkeypatch, backend)
        marker_writes = []
        deferrals = []

        monkeypatch.setattr(database, "get_terminal_metadata", lambda terminal_id: dict(TOMBSTONE))
        monkeypatch.setattr(
            terminal_service,
            "should_retain_deferred_failure_tombstone",
            lambda tid, meta=None: True,
        )
        monkeypatch.setattr(
            terminal_service, "capture_terminal_snapshot", lambda tid: dict(TOMBSTONE)
        )
        monkeypatch.setattr(
            database,
            "update_terminal_deferred_init_runtime_reclaimed",
            lambda terminal_id, reclaimed: marker_writes.append((terminal_id, reclaimed)) or True,
        )

        retained = herdr_inbox_service._retain_deferred_failure_tombstone(
            "tid-1", on_cleanup_deferred=deferrals.append
        )

        assert retained is True
        assert marker_writes == ([("tid-1", True)] if expect_reclaimed else [])
        assert (deferrals == []) is expect_reclaimed


@pytest.mark.parametrize("unproven", ["still_present", "unknown", "raises"])
def test_rediscovery_preserves_real_worktree_until_exact_runtime_absence(
    monkeypatch, tmp_path, isolated_memory_db, runtime, unproven
):
    """Restart rediscovery must not delete a live worker's uncommitted checkout."""
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.services import herdr_inbox_service, worktree_service

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "Fixture",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    checkout = Path(worktree_service.create_worktree(str(repo), "tid-1"))
    sentinel = checkout / "uncommitted.txt"
    sentinel.write_text("The original worker still owns this work.\n")
    database.create_terminal(
        "tid-1",
        "cao-x",
        "coder-3",
        "kimi_cli",
        working_directory=str(checkout),
        deferred_init_external_owner=True,
    )
    database.update_terminal_deferred_init_failure(
        "tid-1", {"kind": "interrupted_init", "message": "CAO restarted during init"}
    )
    backend = FakeBackend(
        outcome(
            TerminalCleanupOutcome.STILL_PRESENT
            if unproven == "still_present"
            else TerminalCleanupOutcome.UNKNOWN
        ),
        raises=RuntimeError("backend unavailable") if unproven == "raises" else None,
    )
    use_backend(monkeypatch, backend)
    cleanup_provider = MagicMock(return_value=True)
    monkeypatch.setattr(terminal_service.provider_manager, "cleanup_provider", cleanup_provider)
    monkeypatch.setattr(terminal_service, "TERMINAL_LOG_DIR", tmp_path)
    service = herdr_inbox_service.HerdrInboxService(socket_path="/tmp/unused.sock")

    # Exercise the real discovery -> retention -> snapshot -> dismantle path.
    assert service._rediscover_deferred_failure_tombstones()

    assert sentinel.read_text() == "The original worker still owns this work.\n"
    assert checkout.is_dir()
    cleanup_provider.assert_not_called()
    assert database.get_terminal_metadata("tid-1")["deferred_init_runtime_reclaimed"] is False
    assert service._pending_tombstone_runtime_cleanup == {"tid-1"}
    assert backend.cleanup_calls == [("tid-1", "cao-x", "coder-3", False)]

    # Once exact identity proof establishes absence, the same retry may reclaim
    # resources and mark completion, while preserving the durable failure row.
    backend._raises = None
    backend._result = outcome(TerminalCleanupOutcome.ABSENT)
    assert service._rediscover_deferred_failure_tombstones()
    assert not checkout.exists()
    cleanup_provider.assert_called_once_with("tid-1")
    retained = database.get_terminal_metadata("tid-1")
    assert retained["deferred_init_runtime_reclaimed"] is True
    assert retained["deferred_init_failure"]["kind"] == "interrupted_init"
