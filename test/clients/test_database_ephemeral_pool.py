"""Terminal registry lookups must reuse a caller's checked-out connection."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from cli_agent_orchestrator.clients import database


@pytest.fixture
def pooled_database(tmp_path, monkeypatch):
    engines = []

    def build(*, single=True):
        options = (
            {"pool_size": 1, "max_overflow": 0, "pool_timeout": 1}
            if single
            else {"pool_timeout": 2}
        )
        engine = create_engine(
            f"sqlite:///{tmp_path / ('single.db' if single else 'concurrent.db')}",
            poolclass=QueuePool,
            connect_args={"check_same_thread": False},
            **options,
        )
        engines.append(engine)
        database.Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine)  # expire_on_commit=True, as in production.
        now = datetime.now()
        with factory() as db:
            for terminal_id in ("ordinary", "bound", "collected"):
                db.add(
                    database.TerminalModel(
                        id=terminal_id,
                        tmux_session="session",
                        tmux_window="window",
                        provider="claude_code",
                    )
                )
            for terminal_id, state in [("bound", "launched"), ("collected", "gc")]:
                db.add(
                    database.EphemeralAgentModel(
                        name=f"Band-log_triage-{terminal_id}",
                        owner_kind="terminal",
                        owner_id="owner",
                        session_name="session",
                        state=state,
                        launched_terminal_id=terminal_id,
                        provider="claude_code",
                        effective_tools="[]",
                        created_at=now,
                        expires_at=now + timedelta(hours=1),
                        spec_sha256="a" * 64,
                        profile_sha256="b" * 64,
                        audit_path="audit.json",
                    )
                )
            db.commit()
        monkeypatch.setattr(database, "SessionLocal", factory)
        monkeypatch.setattr(database, "engine", engine)
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

        return engine, counts

    yield build
    for engine in engines:
        engine.dispose()


def call(operation):
    if operation == "create":
        return database.create_terminal("created", "session", "new-window", "claude_code")
    return database.get_terminal_metadata(operation)


@pytest.mark.parametrize(
    "operation, ephemeral", [("ordinary", False), ("bound", True), ("create", False)]
)
def test_one_connection_pool_returns_metadata(pooled_database, operation, ephemeral):
    _, counts = pooled_database()
    result = call(operation)
    assert result["ephemeral"] is ephemeral
    assert counts == {"active": 0, "peak": 1}


@pytest.mark.parametrize("operation", ["ordinary", "bound", "create"])
def test_each_call_has_one_checkout_peak(pooled_database, operation):
    _, counts = pooled_database(single=False)
    call(operation)
    assert counts == {"active": 0, "peak": 1}


def test_sixteen_callers_finish_without_pool_exhaustion(pooled_database):
    engine, counts = pooled_database(single=False)
    assert engine.pool.size() == 5
    assert engine.pool._max_overflow == 10
    first_wave = threading.Barrier(15)
    ticket_lock = threading.Lock()
    tickets = 0

    # Hold all 15 outer reads at the real SQL boundary, then release together.
    # An implementation requiring a second connection now deterministically stalls.
    @event.listens_for(engine, "after_cursor_execute")
    def synchronize_outer_reads(connection, cursor, statement, parameters, context, executemany):
        nonlocal tickets
        if "FROM terminals" not in statement or not threading.current_thread().name.startswith(
            "pool-reader"
        ):
            return
        with ticket_lock:
            tickets += 1
            synchronize = tickets <= 15
        if synchronize:
            first_wave.wait(timeout=5)

    def worker(_):
        errors = []
        for _ in range(3):
            try:
                assert database.get_terminal_metadata("bound")["ephemeral"] is True
            except Exception as exc:
                errors.append(type(exc).__name__)
        return errors

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=16, thread_name_prefix="pool-reader") as workers:
        errors = [error for group in workers.map(worker, range(16)) for error in group]
    elapsed = time.monotonic() - started
    print(f"16 callers, 48 reads: errors={errors}, elapsed={elapsed:.3f}s, peak={counts['peak']}")
    assert errors == []
    assert elapsed < 5
    assert counts["active"] == 0


@pytest.mark.parametrize(
    "terminal_id, expected", [("ordinary", False), ("bound", True), ("collected", True)]
)
def test_public_wrapper_without_an_open_session(pooled_database, terminal_id, expected):
    _, counts = pooled_database()
    assert database.is_ephemeral_terminal(terminal_id) is expected
    assert counts == {"active": 0, "peak": 1}


def test_public_wrapper_propagates_lookup_errors(pooled_database):
    engine, _ = pooled_database()
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE ephemeral_agents"))
    with pytest.raises(OperationalError):
        database.is_ephemeral_terminal("bound")
