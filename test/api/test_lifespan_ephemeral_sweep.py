"""The ephemeral sweep's server wiring: one startup pass, then a periodic daemon (#1079).

The lifespan helpers from ``test_lifespan_inbox`` replace task creation, so the daemon
appears as a recorded fake task; its own loop is driven for real in the daemon tests.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path
from test.api.test_lifespan_inbox import _find_task, _patched_lifespan
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator import constants
from cli_agent_orchestrator.api import main
from cli_agent_orchestrator.api.main import app, lifespan
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import ephemeral_service
from cli_agent_orchestrator.utils import agent_profiles, atomic_file

REPO = Path(__file__).resolve().parents[2]
PROBE = Path(__file__).with_name("_lifespan_home_probe.py")


@pytest.mark.asyncio
async def test_startup_pass_runs_before_serving_and_the_daemon_stops_on_shutdown() -> None:
    tasks: list = []
    daemon = object()
    with (
        _patched_lifespan(MagicMock(), tasks),
        patch.object(ephemeral_service, "sweep", return_value={}) as sweep,
        patch.object(main, "ephemeral_sweep_daemon", MagicMock(return_value=daemon)) as factory,
    ):
        async with lifespan(app):
            # Inside the context the app is serving: the startup pass has finished.
            sweep.assert_called_once_with(True)
            factory.assert_called_once_with()
            task = _find_task(tasks, daemon)
            assert task is not None
            task.cancel.assert_not_called()
        task.cancel.assert_called_once_with()
    sweep.assert_called_once_with(True)


@pytest.mark.asyncio
async def test_a_failing_startup_pass_does_not_block_startup(caplog) -> None:
    tasks: list = []
    served = False
    with (
        _patched_lifespan(MagicMock(), tasks),
        patch.object(ephemeral_service, "sweep", side_effect=RuntimeError("boom")),
        caplog.at_level(logging.WARNING, logger=main.__name__),
    ):
        async with lifespan(app):
            served = True
    assert served
    assert "ephemeral startup sweep failed" in caplog.text


@pytest.mark.asyncio
async def test_daemon_runs_periodic_passes_survives_errors_and_cancels_cleanly(
    monkeypatch,
) -> None:
    calls: list = []
    second = threading.Event()

    def fake_sweep(startup):
        calls.append(startup)
        if len(calls) == 1:
            raise RuntimeError("one failing pass")
        second.set()
        return {}

    monkeypatch.setattr(ephemeral_service, "sweep", fake_sweep)
    monkeypatch.setattr(ephemeral_service, "sweep_interval_seconds", lambda: 0.01)
    task = asyncio.create_task(main.ephemeral_sweep_daemon())
    for _ in range(500):
        if second.is_set():
            break
        await asyncio.sleep(0.01)
    assert second.is_set(), "the daemon stopped after a failing pass"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert calls and set(calls) == {False}  # Periodic passes keep the launch grace.


@pytest.mark.asyncio
async def test_daemon_survives_a_failing_interval_read_and_shutdown_completes(monkeypatch):
    """A dead daemon would make the shutdown's await re-raise and skip later steps."""
    reads: list = []
    swept = threading.Event()

    def interval():
        reads.append(None)
        if len(reads) == 1:
            raise RecursionError("settings nested too deeply")
        return 0.01

    monkeypatch.setattr(ephemeral_service, "sweep_interval_seconds", interval)
    monkeypatch.setitem(ephemeral_service.EPHEMERAL_DEFAULTS, "sweep_interval_seconds", 0.01)
    monkeypatch.setattr(ephemeral_service, "sweep", lambda startup: swept.set() or {})
    task = asyncio.create_task(main.ephemeral_sweep_daemon())
    for _ in range(500):
        if swept.is_set():
            break
        await asyncio.sleep(0.01)
    assert swept.is_set() and not task.done(), "the daemon stopped after a failing read"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):  # The lifespan shutdown's pattern.
        await task


# The suite's own hermeticity: no test may sweep or lock in the startup CAO home.


def test_every_test_gets_a_private_ephemeral_store_and_lock_dir():
    assert agent_profiles.EPHEMERAL_LIVE_DIR == ephemeral_service.EPHEMERAL_DIR / "live"
    for path in (ephemeral_service.EPHEMERAL_DIR, atomic_file.LOCK_DIR):
        assert constants.CAO_HOME_DIR not in path.parents


def _seed_home(cao_home: Path) -> None:
    """A running agent with day-old live files, and a 30-day-old collected record."""
    store = cao_home / "ephemeral"
    audit = store / "audit" / "cao-real"
    for folder in (store, store / "live", store / "audit", audit):
        folder.mkdir(mode=0o700)
    running, old = "ACDC-real_task-1a2b", "Queen-old_task-3c4d"
    now = database._utcnow()
    for suffix, payload in ((".md", b"running agent profile"), (".spec.json", b"{}")):
        live = store / "live" / (running + suffix)
        live.write_bytes(payload)
        os.utime(live, ((now - timedelta(days=1)).timestamp(),) * 2)
    for name, when in ((running, now), (old, now - timedelta(days=30))):
        path = audit / (name + ".json")
        path.write_text(json.dumps({"events": [{"event": "created"}]}))
        os.utime(path, (when.timestamp(),) * 2)
    db_file = cao_home / constants.DATABASE_FILE.relative_to(constants.CAO_HOME_DIR)
    db_file.parent.mkdir()
    engine = create_engine(f"sqlite:///{db_file}")
    database.Base.metadata.create_all(engine)
    common = dict(
        owner_kind="terminal",
        owner_id="aaaaaaaa",
        session_name="cao-real",
        provider="claude_code",
        effective_tools="[]",
        spec_sha256="x",
        profile_sha256="y",
    )
    then = now - timedelta(days=30)
    with sessionmaker(bind=engine)() as db:
        for terminal in ("aaaaaaaa", "cccccccc"):
            db.add(
                database.TerminalModel(
                    id=terminal, tmux_session="cao-real", tmux_window="w", provider="claude_code"
                )
            )
        db.add(
            database.EphemeralAgentModel(
                name=running,
                state="launched",
                launched_terminal_id="cccccccc",
                claim_id="c" * 32,
                bound_at=now,
                created_at=now,
                expires_at=now + timedelta(seconds=900),
                audit_path=str(audit / (running + ".json")),
                **common,
            )
        )
        db.add(
            database.EphemeralAgentModel(
                name=old,
                state="gc",
                gc_reason="terminal_deleted",
                launched_terminal_id="dddddddd",
                bound_at=then,
                created_at=then,
                expires_at=then + timedelta(seconds=900),
                audit_path=str(audit / (old + ".json")),
                **common,
            )
        )
        db.commit()
    engine.dispose()


def _home_state(cao_home: Path) -> dict:
    """Every entry under the store and lock dir, plus the registry's ephemeral rows."""
    found: dict = {}
    for root in (cao_home / "ephemeral", cao_home / "locks"):
        for folder, dirs, files in os.walk(root):
            for entry in dirs + files:
                path = Path(folder) / entry
                info = path.lstat()
                digest = hashlib.sha256(path.read_bytes()).hexdigest() if entry in files else None
                found[str(path)] = (info.st_mode, info.st_mtime_ns, digest)
    engine = create_engine(
        f"sqlite:///{cao_home / constants.DATABASE_FILE.relative_to(constants.CAO_HOME_DIR)}"
    )
    with sessionmaker(bind=engine)() as db:
        table = database.EphemeralAgentModel
        found["rows"] = sorted(
            (r.name, r.state, r.gc_reason, r.audit_path) for r in db.query(table).all()
        )
    engine.dispose()
    return found


def test_lifespans_and_creates_under_test_leave_the_startup_cao_home_alone(tmp_path):
    """Run lifespans and a create under a stand-in home; the seeded store must not change.

    The probes run in a subprocess, so its CAO home is the stand-in, and they load the
    repo's conftest, which is what keeps each test's ephemeral store and lock dir
    private. Without that, the startup sweep deletes the running agent's files (they
    look rowless to a replacement database) and prunes the aged record, and the create
    leaves a lock file in the home.
    """
    home = tmp_path / "home"
    cao_home = (home / "cao").resolve(strict=False)
    cao_home.mkdir(parents=True)
    _seed_home(cao_home)
    before = _home_state(cao_home)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "CAO_HOME_DIR": str(cao_home),
        "TMPDIR": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(PROBE),
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            "--basetemp",
            str(tmp_path / "probe-tmp"),
            "-q",
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
        env=env,
        timeout=180,
    )
    assert result.returncode == 0 and re.search(r"\b3 passed\b", result.stdout), (
        result.stdout[-4000:] + result.stderr[-2000:]
    )
    assert _home_state(cao_home) == before
