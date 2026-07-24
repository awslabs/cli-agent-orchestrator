"""Lifespan integration for persisted session env (issue #248 durability).

Seeds a "pre-restart" database, runs the real FastAPI lifespan, and asserts:
1. the startup reconcile deletes session_env rows for dead tmux sessions and
   retains live-session rows (alongside the output-pipeline reattach); and
2. a window created after the restart receives the persisted env from the DB
   (cold in-memory cache), with per-step env winning on conflict.
"""

import asyncio
import sqlite3
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.api.main import app, lifespan
from cli_agent_orchestrator.clients import database as db_module
from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.services import session_env as session_env_module
from cli_agent_orchestrator.services import terminal_service

_LIVE_ROW = ("cao-live", '{"PATH": "/shim/bin", "KEEP": "kept"}', "2026-07-21T00:00:00Z")
_DEAD_ROW = ("cao-dead", '{"STALE": "1"}', "2026-07-21T00:00:00Z")


async def _quick_task(*args, **kwargs) -> None:
    del args, kwargs
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_lifespan_reconcile_and_post_restart_window_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_file = tmp_path / "cao.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    sessions = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_module, "SessionLocal", sessions)
    # Cold in-memory cache — this test exercises the post-restart read path.
    with session_env_module._lock:
        session_env_module._session_forwarded_env.clear()

    def initialize_db() -> None:
        Base.metadata.create_all(bind=engine)

    # Seed "pre-restart" state: one live-session row and one dead-session row.
    initialize_db()
    with sqlite3.connect(str(db_file)) as conn:
        conn.execute("INSERT INTO session_env VALUES (?, ?, ?)", _LIVE_ROW)
        conn.execute("INSERT INTO session_env VALUES (?, ?, ?)", _DEAD_ROW)
        conn.commit()

    backend = MagicMock()
    backend.supports_event_inbox.return_value = True  # reattach exits early
    backend.session_exists.side_effect = lambda name: name == "cao-live"
    backend.create_window.return_value = "developer-abcd"
    provider = AsyncMock()
    provider.initialize.return_value = True

    try:
        with ExitStack() as stack:
            stack.enter_context(patch("cli_agent_orchestrator.api.main.setup_logging"))
            stack.enter_context(
                patch("cli_agent_orchestrator.api.main.init_db", side_effect=initialize_db)
            )
            stack.enter_context(
                patch(
                    "cli_agent_orchestrator.services.memory_reconciliation.reconcile_memory_startup",
                    return_value=None,
                )
            )
            stack.enter_context(patch("cli_agent_orchestrator.api.main.cleanup_old_data"))
            stack.enter_context(
                patch(
                    "cli_agent_orchestrator.api.main.cleanup_expired_memories",
                    new=AsyncMock(side_effect=_quick_task),
                )
            )
            for name in (
                "flow_daemon",
                "opencode_inbox_delivery_daemon",
                "inbox_reconciliation_daemon",
            ):
                stack.enter_context(patch(f"cli_agent_orchestrator.api.main.{name}", _quick_task))
            for name in ("status_monitor.run", "log_writer.run", "inbox_service.run"):
                stack.enter_context(
                    patch(f"cli_agent_orchestrator.api.main.{name}", new=AsyncMock())
                )
            stack.enter_context(patch("cli_agent_orchestrator.api.main.bus.set_loop"))
            stack.enter_context(patch("cli_agent_orchestrator.backends.registry._backend", backend))
            stack.enter_context(patch.object(PluginRegistry, "load", new=AsyncMock()))
            stack.enter_context(patch.object(PluginRegistry, "teardown", new=AsyncMock()))
            async with lifespan(app):
                pass

        # Reconcile removed the dead-session row and retained the live one.
        with sqlite3.connect(str(db_file)) as conn:
            rows = conn.execute(
                "SELECT session_name, env_vars FROM session_env ORDER BY session_name"
            ).fetchall()
        assert rows == [(_LIVE_ROW[0], _LIVE_ROW[1])]

        # Post-restart window creation: joins the live session, reads the
        # persisted env from the DB (cache is still cold), and per-step env
        # wins on conflict — {**get_session_env(session), **env_vars}.
        with ExitStack() as stack:
            stack.enter_context(patch("cli_agent_orchestrator.backends.registry._backend", backend))
            stack.enter_context(
                patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
            )
            stack.enter_context(
                patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
            )
            mock_fifo_dir = stack.enter_context(
                patch("cli_agent_orchestrator.services.terminal_service.FIFO_DIR")
            )
            mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")
            mock_pm = stack.enter_context(
                patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
            )
            mock_pm.create_provider.return_value = provider
            stack.enter_context(
                patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal")
            )
            stack.enter_context(
                patch(
                    "cli_agent_orchestrator.services.terminal_service.generate_terminal_id",
                    return_value="test1234",
                )
            )
            stack.enter_context(
                patch(
                    "cli_agent_orchestrator.services.terminal_service.generate_window_name",
                    return_value="developer-abcd",
                )
            )
            stack.enter_context(
                patch(
                    "cli_agent_orchestrator.services.terminal_service.load_agent_profile",
                    return_value=AgentProfile(name="developer", description="Developer"),
                )
            )

            await terminal_service.create_terminal(
                "kiro_cli",
                "developer",
                session_name="cao-live",
                new_session=False,
                env_vars={"PATH": "/step/bin", "CAO_WORKFLOW_RUN_ID": "run-1"},
            )

        extra_env = backend.create_window.call_args.kwargs["extra_env"]
        assert extra_env == {
            "PATH": "/step/bin",  # per-step wins over the persisted value
            "KEEP": "kept",  # persisted key not overridden reaches the window
            "CAO_WORKFLOW_RUN_ID": "run-1",
        }
    finally:
        with session_env_module._lock:
            session_env_module._session_forwarded_env.clear()
        engine.dispose()
