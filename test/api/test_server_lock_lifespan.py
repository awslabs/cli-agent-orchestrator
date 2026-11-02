"""The FastAPI lifespan takes the one-server-per-home lock first and releases it last (#406, #709).

The refusal test holds the lock the way a foreign ``cao-server`` would (raw
``flock`` on a second descriptor; ``flock`` contends per open file description,
so this is the cross-process case without a second process) and asserts the
lifespan exits before ``init_db`` runs. ``test/fixtures/test_cao_server.py``
repeats the refusal against a real ``cao-server`` subprocess.
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.api.main import app, lifespan
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.utils import server_lock
from cli_agent_orchestrator.utils.server_lock import ServerLockHeldError, acquire_server_lock


async def _fake_flow_daemon() -> None:
    """flow_daemon stand-in: create_task needs a real coroutine."""


async def _fake_opencode_daemon(registry: object) -> None:
    del registry


def _patch_startup_side_effects(stack: ExitStack) -> MagicMock:
    """No-op everything the lifespan touches after the lock. Returns the init_db mock."""

    def patch_main(name: str, **kwargs):
        return stack.enter_context(patch(f"cli_agent_orchestrator.api.main.{name}", **kwargs))

    patch_main("setup_logging")
    init_db = patch_main("init_db")
    stack.enter_context(
        patch(
            "cli_agent_orchestrator.services.memory_reconciliation.reconcile_memory_startup",
            return_value=None,
        )
    )
    patch_main("cleanup_old_data")
    patch_main("flow_daemon", new=_fake_flow_daemon)
    patch_main("opencode_inbox_delivery_daemon", new=_fake_opencode_daemon)
    patch_main("get_backend", return_value=MagicMock())
    for runner in ("status_monitor.run", "log_writer.run", "inbox_service.run"):
        patch_main(runner, new_callable=AsyncMock)
    stack.enter_context(patch.object(PluginRegistry, "load", new_callable=AsyncMock))
    stack.enter_context(patch.object(PluginRegistry, "teardown", new_callable=AsyncMock))
    return init_db


@pytest.mark.asyncio
async def test_lifespan_refuses_before_init_db_when_another_server_owns_the_home(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    lock_path = tmp_path / "home" / "server.lock"
    lock_path.parent.mkdir()
    monkeypatch.setattr(server_lock, "SERVER_LOCK_FILE", lock_path)

    # A foreign server: raw flock plus the diagnostics a real one writes.
    foreign_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(foreign_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.write(
        foreign_fd,
        json.dumps(
            {"pid": 4242, "port": 9889, "started_at": "2026-10-21T14:03:27Z", "version": "t"}
        ).encode(),
    )
    try:
        with ExitStack() as stack:
            init_db = _patch_startup_side_effects(stack)
            with pytest.raises(SystemExit) as excinfo:
                async with lifespan(app):
                    pytest.fail("lifespan must not reach its yield while the lock is held")
        assert excinfo.value.code == 1
        init_db.assert_not_called()
        err = capsys.readouterr().err
        assert err.splitlines()[:2] == [
            "cao: another server (PID 4242, port 9889, started 2026-10-21T14:03Z) already owns",
            f"     {lock_path.parent}",
        ]
        assert "different CAO_HOME_DIR and --port" in err
    finally:
        os.close(foreign_fd)


@pytest.mark.asyncio
async def test_lifespan_holds_the_lock_while_running_and_releases_on_shutdown(
    tmp_path: Path, monkeypatch
) -> None:
    lock_path = tmp_path / "home" / "server.lock"
    monkeypatch.setattr(server_lock, "SERVER_LOCK_FILE", lock_path)
    monkeypatch.setattr(app.state, "server_port", 4321, raising=False)

    with ExitStack() as stack:
        init_db = _patch_startup_side_effects(stack)
        async with lifespan(app):
            init_db.assert_called_once()
            diagnostics = json.loads(lock_path.read_text())
            assert diagnostics["pid"] == os.getpid()
            assert diagnostics["port"] == 4321, "main() hands the resolved --port to the lifespan"
            with pytest.raises(ServerLockHeldError):
                acquire_server_lock(port=1, path=lock_path)

    # Released on clean shutdown: a fresh acquire succeeds in this same process.
    acquire_server_lock(port=1, path=lock_path).release()


@pytest.mark.asyncio
async def test_lifespan_falls_back_to_configured_port_without_main(
    tmp_path: Path, monkeypatch
) -> None:
    """``uvicorn cli_agent_orchestrator.api.main:app`` never runs ``main()``."""
    from cli_agent_orchestrator.api import main as api_main

    lock_path = tmp_path / "home" / "server.lock"
    monkeypatch.setattr(server_lock, "SERVER_LOCK_FILE", lock_path)
    if hasattr(app.state, "server_port"):
        monkeypatch.delattr(app.state, "server_port")
    monkeypatch.setattr(api_main, "SERVER_PORT", 6543)

    with ExitStack() as stack:
        _patch_startup_side_effects(stack)
        async with lifespan(app):
            assert json.loads(lock_path.read_text())["port"] == 6543
