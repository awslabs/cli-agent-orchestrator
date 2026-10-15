"""Teardown sidecars (.scrollback / .snapshot.json) are private and leave no lock files.

``capture_terminal_snapshot`` runs on every terminal teardown and writes two
files whose names are unique per terminal. They are published 0600 through the
atomic writer so a planted symlink is replaced rather than followed, but through
the UNLOCKED variant: the locked writer keeps one lock file per target in
``LOCK_DIR`` for good, so routing the sidecars through it grew ``LOCK_DIR`` by
two files per teardown with nothing to reap them. The lock-count assertions here
pin that regression.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.constants import LOCK_DIR
from cli_agent_orchestrator.services import terminal_service

TID = "abcd1234"
ROW = {
    "terminal_id": TID,
    "tmux_session": "cao-ses",
    "tmux_window": "win",
    "provider": "kiro_cli",
    "agent_profile": "dev",
    "working_directory": "/launch/cwd",
    "allowed_tools": ["*"],
    "caller_id": None,
}


def _lock_dir_entries() -> set:
    return set(LOCK_DIR.iterdir()) if LOCK_DIR.exists() else set()


@pytest.fixture
def backend():
    be = MagicMock()
    be.get_pane_working_directory.return_value = "/live/cwd"
    be.get_history.return_value = "scrollback with secrets\n"
    return be


@pytest.fixture
def log_dir(tmp_path):
    d = tmp_path / "logs" / "terminal"
    d.mkdir(parents=True)
    return d


def _snapshot(log_dir, backend, row=ROW):
    with (
        patch.object(terminal_service, "get_terminal_metadata", return_value=dict(row)),
        patch.object(terminal_service, "get_backend", return_value=backend),
        patch.object(terminal_service, "TERMINAL_LOG_DIR", log_dir),
    ):
        return terminal_service.capture_terminal_snapshot(TID)


class TestSidecarsLeaveNoLockFiles:
    def test_lock_dir_file_count_is_unchanged_by_one_snapshot(self, log_dir, backend):
        before = _lock_dir_entries()

        metadata = _snapshot(log_dir, backend)

        assert metadata is not None
        assert _lock_dir_entries() == before
        assert (log_dir / f"{TID}.scrollback").exists()
        assert (log_dir / f"{TID}.snapshot.json").exists()

    def test_lock_dir_file_count_is_unchanged_across_many_teardowns(self, log_dir, backend):
        """25 teardowns used to leave 50 permanent lock files."""
        before = _lock_dir_entries()

        for i in range(25):
            row = dict(ROW, terminal_id=f"t{i:07d}")
            with (
                patch.object(terminal_service, "get_terminal_metadata", return_value=row),
                patch.object(terminal_service, "get_backend", return_value=backend),
                patch.object(terminal_service, "TERMINAL_LOG_DIR", log_dir),
            ):
                assert terminal_service.capture_terminal_snapshot(row["terminal_id"]) is not None

        assert _lock_dir_entries() == before
        assert len(list(log_dir.glob("*.scrollback"))) == 25
        assert len(list(log_dir.glob("*.snapshot.json"))) == 25


class TestSidecarsArePrivateAndSymlinkSafe:
    def test_both_sidecars_are_created_0600_with_the_expected_content(self, log_dir, backend):
        _snapshot(log_dir, backend)

        scrollback = log_dir / f"{TID}.scrollback"
        snapshot = log_dir / f"{TID}.snapshot.json"
        assert oct(scrollback.stat().st_mode & 0o777) == oct(0o600)
        assert oct(snapshot.stat().st_mode & 0o777) == oct(0o600)
        assert scrollback.read_text() == "scrollback with secrets\n"
        body = json.loads(snapshot.read_text())
        assert body["terminal_id"] == TID
        assert body["working_directory"] == "/live/cwd"
        assert body["caller_id"] is None

    def test_a_symlink_planted_at_either_sidecar_is_replaced_not_followed(
        self, log_dir, backend, tmp_path
    ):
        victim_a = tmp_path / "victim_a"
        victim_b = tmp_path / "victim_b"
        victim_a.write_text("do not touch")
        victim_b.write_text("do not touch")
        (log_dir / f"{TID}.scrollback").symlink_to(victim_a)
        (log_dir / f"{TID}.snapshot.json").symlink_to(victim_b)

        _snapshot(log_dir, backend)

        assert victim_a.read_text() == "do not touch"
        assert victim_b.read_text() == "do not touch"
        for name in (f"{TID}.scrollback", f"{TID}.snapshot.json"):
            p = log_dir / name
            assert not p.is_symlink()
            assert oct(p.stat().st_mode & 0o777) == oct(0o600)

    def test_an_existing_world_readable_sidecar_is_tightened(self, log_dir, backend):
        for name in (f"{TID}.scrollback", f"{TID}.snapshot.json"):
            p = log_dir / name
            p.write_text("stale")
            p.chmod(0o644)

        _snapshot(log_dir, backend)

        for name in (f"{TID}.scrollback", f"{TID}.snapshot.json"):
            assert oct((log_dir / name).stat().st_mode & 0o777) == oct(0o600)
