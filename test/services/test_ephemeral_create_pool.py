"""The complete create/store path must not acquire nested pooled connections."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import ephemeral_service, settings_service
from cli_agent_orchestrator.utils import agent_profiles

CALLER = "abcd1234"


@pytest.fixture
def pooled_create(tmp_path, monkeypatch):
    # Follow test/clients/test_database_ephemeral_pool.py, but drive the real
    # service's metadata/depth/collision reads and pending INSERT together.
    engines = []

    def build(*, single=True):
        options = {"pool_size": 1, "max_overflow": 0, "pool_timeout": 1} if single else {}
        engine = create_engine(
            f"sqlite:///{tmp_path / 'create.db'}",
            poolclass=QueuePool,
            connect_args={"check_same_thread": False},
            **options,
        )
        engines.append(engine)
        database.Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine)
        with factory() as db:
            db.add(
                database.TerminalModel(
                    id=CALLER,
                    tmux_session="session",
                    tmux_window="window",
                    provider="claude_code",
                    agent_profile="reviewer",
                    allowed_tools=json.dumps(["fs_read", "@cao-mcp-server"]),
                )
            )
            db.commit()
        monkeypatch.setattr(database, "SessionLocal", factory)
        monkeypatch.setattr(database, "engine", engine)
        monkeypatch.setattr(ephemeral_service, "EPHEMERAL_DIR", tmp_path / "ephemeral")
        installed = tmp_path / "installed"
        installed.mkdir()
        monkeypatch.setattr(agent_profiles, "LOCAL_AGENT_STORE_DIR", installed)
        monkeypatch.setattr(settings_service, "get_agent_dirs", lambda: {})
        monkeypatch.setattr(settings_service, "get_extra_agent_dirs", lambda: [])
        monkeypatch.setattr(settings_service, "get_disabled_agent_dirs", lambda: [])
        monkeypatch.setattr(
            settings_service, "_load_or_raise", lambda: {"ephemeral": {"enabled": True}}
        )
        lock = threading.Lock()
        counts = {"active": 0, "peak": 0}

        @event.listens_for(engine, "checkout")
        def checkout(*_):
            with lock:
                counts["active"] += 1
                counts["peak"] = max(counts["peak"], counts["active"])

        @event.listens_for(engine, "checkin")
        def checkin(*_):
            with lock:
                counts["active"] -= 1

        return engine, factory, counts

    yield build
    for engine in engines:
        engine.dispose()


def create(index):
    return ephemeral_service.create_ephemeral_agent(
        {
            "spec_version": 1,
            "purpose": f"pool_check_{index}",
            "brief": "Inspect logs.",
            "tools": ["fs_read"],
        },
        CALLER,
    )


def test_create_stores_pending_with_one_connection(pooled_create):
    _, _, counts = pooled_create()
    result = create(0)
    row = database.get_ephemeral_agent(result["name"])
    assert row["state"] == "pending"
    assert row["owner_id"] == CALLER
    assert row["launched_terminal_id"] is None
    assert counts == {"active": 0, "peak": 1}


def test_sixteen_creators_finish_on_default_pool(pooled_create):
    engine, factory, counts = pooled_create(single=False)
    assert engine.pool.size() == 5
    assert engine.pool._max_overflow == 10
    first_wave = threading.Barrier(16)

    def worker(index):
        first_wave.wait(timeout=5)
        names, errors = [], []
        for attempt in range(3):
            try:
                names.append(create(index * 3 + attempt)["name"])
            except Exception as exc:
                errors.append(type(exc).__name__)
        return names, errors

    with ThreadPoolExecutor(max_workers=16, thread_name_prefix="pool-creator") as workers:
        results = list(workers.map(worker, range(16)))
    errors = [error for _, group in results for error in group]
    names = [name for group, _ in results for name in group]
    assert errors == []
    assert len(set(names)) == 48
    with factory() as db:
        rows = db.query(database.EphemeralAgentModel).all()
        assert {row.name for row in rows} == set(names)
        assert all(row.state == "pending" and row.launched_terminal_id is None for row in rows)
    assert counts["active"] == 0
