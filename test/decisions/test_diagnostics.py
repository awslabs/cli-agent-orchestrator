"""Content-free decider diagnostics, record target kinds and read-only decider status."""

import asyncio
import json
import logging
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from test.decisions.test_engine import (  # noqa: F401  # pytest fixture, injected by name into the tests below
    setup_engine,
)
from test.fixtures.decision_conformance import ConfidentDecider
from typing import Any, Mapping

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.models.agent_profile import AgentProfile

CANARY = "synthetic-canary-5f1c09"
ENGINE_LOGGER = "cli_agent_orchestrator.decisions.engine"
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


class ScriptedDecider(ConfidentDecider):
    """Answers, raises with a secret in the exception, or returns nothing."""

    def __init__(self) -> None:
        super().__init__("small", 0.91)
        self.mode = "answer"

    async def decide(self, request: Any, config: Mapping[str, Any]) -> Any:
        if self.mode == "error":
            raise RuntimeError(CANARY)
        if self.mode == "no_answer":
            return None
        if self.mode == "unsure":
            return {point: self._unsure() for point in request.points}
        return await super().decide(request, config)

    @staticmethod
    def _unsure() -> Any:
        from cli_agent_orchestrator.decisions.types import DecisionAnswer

        return DecisionAnswer("unsure", {"unsure": 1.0})


def observed(setup_engine, *, settings=None, tick=None, **kwargs):
    from cli_agent_orchestrator.decisions.engine import DecisionEngine
    from cli_agent_orchestrator.decisions.registry import DeciderRegistry

    engine, req, _, store, _, base_settings = setup_engine
    decider = ScriptedDecider()
    current = settings or base_settings
    clock = tick or [0.0]
    built = DecisionEngine(
        DeciderRegistry({decider.name: lambda: decider}),
        store,
        engine.runner,
        settings_loader=lambda: current,
        profile_loader=engine.profile_loader,
        honors_model=engine.honors_model,
        clock=lambda: clock[0],
        **kwargs,
    )
    return built, req, decider, store


def engine_lines(caplog, level: int, text: str) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == ENGINE_LOGGER and record.levelno == level and text in record.msg
    ]


def failing(caplog):
    return engine_lines(caplog, logging.WARNING, "decider failing")


def recovered(caplog):
    return engine_lines(caplog, logging.INFO, "decider recovered")


@pytest.mark.asyncio
async def test_repeated_error_warns_once_with_labels_then_one_recovery(setup_engine, caplog):
    tick = [0.0]
    engine, req, decider, store = observed(setup_engine, tick=tick)
    caplog.set_level(logging.INFO, logger=ENGINE_LOGGER)
    decider.mode = "error"
    # Spaced well past the rate window: a repeat is not a transition, whatever the clock.
    for now in (0.0, 600.0, 1200.0):
        tick[0] = now
        await engine.prepare_launch(req)
    assert len(failing(caplog)) == 1
    assert failing(caplog)[0].args == (decider.name, "model.route", "error")
    assert "reason=error" in failing(caplog)[0].getMessage()
    decider.mode = "answer"
    for _ in range(2):
        await engine.prepare_launch(req)
    assert len(recovered(caplog)) == 1
    assert recovered(caplog)[0].args == (decider.name, "model.route")
    assert [row["reason"] for row in reversed(store.list())] == ["error"] * 3 + [None] * 2


@pytest.mark.asyncio
async def test_flapping_decider_is_rate_limited_and_a_lasting_failure_still_warns(
    setup_engine, caplog
):
    tick = [0.0]
    engine, req, decider, _ = observed(setup_engine, tick=tick, warning_interval_s=60.0)
    caplog.set_level(logging.INFO, logger=ENGINE_LOGGER)
    for second in range(5):
        tick[0] = float(second)
        decider.mode = "error"
        await engine.prepare_launch(req)
        decider.mode = "answer"
        await engine.prepare_launch(req)
    assert (len(failing(caplog)), len(recovered(caplog))) == (1, 1)
    decider.mode = "error"
    tick[0] = 10.0
    await engine.prepare_launch(req)
    assert len(failing(caplog)) == 1
    # The suppressed transition is reported once the window has passed and it still fails.
    tick[0] = 70.0
    await engine.prepare_launch(req)
    assert len(failing(caplog)) == 2
    decider.mode = "answer"
    await engine.prepare_launch(req)
    assert len(recovered(caplog)) == 2


@pytest.mark.asyncio
async def test_each_failure_reason_is_its_own_transition(setup_engine, caplog):
    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.types import PointState

    tick = [0.0]
    engine, req, decider, _ = observed(setup_engine, tick=tick)
    caplog.set_level(logging.INFO, logger=ENGINE_LOGGER)
    for mode in ("error", "no_answer", "error"):
        decider.mode = mode
        await engine.prepare_launch(req)
    # error -> no_answer warns again; the quick return to error is inside error's window.
    assert [record.args[2] for record in failing(caplog)] == ["error", "no_answer"]
    decider.mode = "answer"
    await engine.prepare_launch(req)
    assert len(recovered(caplog)) == 1
    _, _, _, _, _, settings = setup_engine
    missing = replace(
        settings, points={"model.route": PointSettings(PointState.ON, "not-installed")}
    )
    engine.settings_loader = lambda: missing
    await engine.prepare_launch(req)
    assert failing(caplog)[-1].args == ("not-installed", "model.route", "decider_unavailable")


@pytest.mark.asyncio
async def test_abstaining_answers_recover_and_unasked_points_say_nothing(setup_engine, caplog):
    engine, req, decider, store = observed(setup_engine)
    caplog.set_level(logging.INFO, logger=ENGINE_LOGGER)
    decider.mode = "error"
    await engine.prepare_launch(req)
    # A point the provider cannot honor never asks its decider, so the failure streak stays.
    engine.honors_model = lambda *args: False
    await engine.prepare_launch(req)
    engine.honors_model = lambda *args: True
    await engine.prepare_launch(req)
    assert len(failing(caplog)) == 1 and recovered(caplog) == []
    # unsure is an answer from a healthy decider, not a failure.
    decider.mode = "unsure"
    await engine.prepare_launch(req)
    await engine.prepare_launch(req)
    assert len(failing(caplog)) == 1 and len(recovered(caplog)) == 1
    assert [row["reason"] for row in reversed(store.list())] == [
        "error",
        "not_honored",
        "error",
        "unsure",
        "unsure",
    ]


def _fixed_table_engine(setup_engine, *, table, state=None):
    from cli_agent_orchestrator.decisions.engine import DecisionEngine
    from cli_agent_orchestrator.decisions.fixed_table import FixedTableDecider
    from cli_agent_orchestrator.decisions.registry import DeciderRegistry
    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.types import PointState

    engine, req, _, store, _, base_settings = setup_engine
    table_settings = replace(
        base_settings,
        points={"model.route": PointSettings(state or PointState.ON, "fixed_table")},
        deciders={"fixed_table": {"model.route": table}},
    )
    built = DecisionEngine(
        DeciderRegistry({"fixed_table": FixedTableDecider}),
        store,
        engine.runner,
        settings_loader=lambda: table_settings,
        profile_loader=lambda name: AgentProfile(name=name, description=name),
        honors_model=lambda *args: True,
    )
    return built, req, store


@pytest.mark.asyncio
@pytest.mark.parametrize("state_name", ("on", "shadow"))
async def test_fixed_table_abstention_never_warns_even_while_alternating(
    setup_engine, caplog, state_name
):
    from cli_agent_orchestrator.decisions.targets import TargetKind
    from cli_agent_orchestrator.decisions.types import PointState

    state = PointState.ON if state_name == "on" else PointState.SHADOW
    engine, req, store = _fixed_table_engine(
        setup_engine, table={"profiles": {"developer": "small"}}, state=state
    )
    caplog.set_level(logging.INFO, logger=ENGINE_LOGGER)
    for profile in ("developer", "reviewer") * 5:
        await engine.prepare_launch(replace(req, agent_profile=profile))
    # Every ephemeral target abstains too, since DecisionFacts.profile is None there.
    for _ in range(3):
        await engine.prepare_launch(
            replace(
                req,
                agent_profile=None,
                target_kind=TargetKind.EPHEMERAL,
                field_states={"model.route": "auto"},
            )
        )
    await engine.runner.drain()
    assert failing(caplog) == [] and recovered(caplog) == []
    reasons = {row["reason"] for row in store.list()}
    assert "no_answer" in reasons


@pytest.mark.asyncio
async def test_fixed_table_exemption_is_keyed_on_identity_not_name(setup_engine, caplog):
    from cli_agent_orchestrator.decisions.engine import DecisionEngine
    from cli_agent_orchestrator.decisions.registry import DeciderRegistry
    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.types import POINTS, PointState

    class ImpostorFixedTable:
        """A non-built-in decider registered under the name "fixed_table"."""

        name = "fixed_table"
        version = "1"
        points = frozenset(POINTS)

        async def decide(self, request, config):
            return None

        async def close(self) -> None:
            pass

    engine, req, _, store, _, base_settings = setup_engine
    impostor_settings = replace(
        base_settings, points={"model.route": PointSettings(PointState.ON, "fixed_table")}
    )
    built = DecisionEngine(
        DeciderRegistry({"fixed_table": ImpostorFixedTable}),
        store,
        engine.runner,
        settings_loader=lambda: impostor_settings,
        profile_loader=engine.profile_loader,
        honors_model=engine.honors_model,
    )
    caplog.set_level(logging.INFO, logger=ENGINE_LOGGER)
    for _ in range(3):
        await built.prepare_launch(req)
    assert len(failing(caplog)) == 1
    assert failing(caplog)[0].args == ("fixed_table", "model.route", "no_answer")


@pytest.mark.asyncio
async def test_external_decider_stuck_on_no_answer_still_warns_once(setup_engine, caplog):
    tick = [0.0]
    engine, req, decider, _ = observed(setup_engine, tick=tick)
    caplog.set_level(logging.INFO, logger=ENGINE_LOGGER)
    decider.mode = "no_answer"
    for now in (0.0, 600.0, 1200.0):
        tick[0] = now
        await engine.prepare_launch(req)
    assert len(failing(caplog)) == 1
    assert failing(caplog)[0].args == (decider.name, "model.route", "no_answer")


@pytest.mark.asyncio
async def test_shadow_results_are_observed_when_they_finish(setup_engine, caplog):
    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.types import PointState

    _, _, _, _, _, settings = setup_engine
    shadow = replace(settings, points={"model.route": PointSettings(PointState.SHADOW, "fixture")})
    engine, req, decider, store = observed(setup_engine, settings=shadow)
    caplog.set_level(logging.INFO, logger=ENGINE_LOGGER)
    decider.mode = "error"
    await engine.prepare_launch(req)
    await engine.runner.drain()
    assert failing(caplog)[0].args == ("fixture", "model.route", "error")
    decider.mode = "answer"
    await engine.prepare_launch(req)
    await engine.runner.drain()
    assert len(recovered(caplog)) == 1
    assert [row["reason"] for row in reversed(store.list())] == ["error", None]
    await engine.runner.close()


@pytest.mark.asyncio
async def test_spy_logs_hold_no_task_text_profile_purpose_config_or_exception(setup_engine, caplog):
    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.targets import TargetKind
    from cli_agent_orchestrator.decisions.types import PointState

    _, _, _, _, _, settings = setup_engine
    secret_settings = replace(
        settings,
        points={
            "model.route": PointSettings(PointState.ON, "fixture", (CANARY,)),
            # A malformed decider name, as a hand edit could leave it.
            "effort.route": PointSettings(PointState.SHADOW, f"bad\n{CANARY}"),
        },
        deciders={"fixture": {"token": CANARY}},
    )
    engine, req, decider, store = observed(setup_engine, settings=secret_settings)
    engine.profile_loader = lambda _: AgentProfile(
        name="worker", description=f"brief {CANARY}", role=f"role-{CANARY}"
    )
    caplog.set_level(logging.DEBUG)
    both = {"model.route": "auto", "effort.route": "auto"}
    requests = [
        replace(req, message=f"task {CANARY}", field_states=both),
        replace(
            req,
            agent_profile="Ramones-log_triage-3f9a",
            target_kind=TargetKind.EPHEMERAL,
            message=f"task {CANARY}",
            purpose=f"purpose {CANARY}",
            field_states=both,
        ),
    ]
    for mode in ("error", "no_answer", "answer", "error"):
        decider.mode = mode
        for request in requests:
            await engine.prepare_launch(request)
    await engine.runner.drain()
    formatter = logging.Formatter("%(name)s %(message)s %(exc_info)s")
    for record in caplog.records:
        rendered = formatter.format(record) + repr(record.args) + str(record.exc_text)
        assert CANARY not in rendered, record.name
    # The spy saw the failures it was meant to see.
    reasons = {record.args[2] for record in failing(caplog)}
    assert {"error", "no_answer", "decider_unavailable"} <= reasons
    assert any(record.args[0] == "<invalid>" for record in failing(caplog))
    assert engine_lines(caplog, logging.INFO, "Decision settings")
    await engine.runner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["warning", "info"])
async def test_a_raising_log_call_never_changes_the_launch(setup_engine, monkeypatch, level):
    from cli_agent_orchestrator.decisions import engine as engine_module

    async def launches(engine, req, decider, store):
        results = []
        for mode in ("error", "error", "answer", "no_answer", "answer"):
            decider.mode = mode
            plan = await engine.prepare_launch(req)
            row = store.get(plan.record_ids[0])
            results.append((plan.model, row["outcome"], row["reason"], row["target_kind"]))
        return results

    expected = await launches(*observed(setup_engine))

    def explode(*args, **kwargs):
        raise RuntimeError("logging failed")

    monkeypatch.setattr(engine_module.logger, level, explode)
    assert await launches(*observed(setup_engine)) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["installed", "ephemeral", "workflow"])
async def test_new_records_carry_their_target_kind(setup_engine, kind):
    from cli_agent_orchestrator.decisions.owner import WORKFLOW_STEP
    from cli_agent_orchestrator.decisions.targets import TargetKind

    engine, req, _, store, _, _ = setup_engine
    if kind == "ephemeral":
        req = replace(
            req,
            agent_profile="Ramones-log_triage-3f9a",
            target_kind=TargetKind.EPHEMERAL,
            purpose="log_triage",
            field_states={"model.route": "auto"},
        )
    elif kind == "workflow":
        req = replace(req, owner=WORKFLOW_STEP)
    await engine.prepare_launch(req)
    rows = store.list()
    assert rows and {row["target_kind"] for row in rows} == {kind}
    await engine.runner.close()


def _legacy_decision_table(path: Path) -> list[tuple[Any, ...]]:
    rows = [
        (1, "model.route", "fixture", "error", "worker", "assign"),
        (2, "model.route", None, "above_ceiling", "Ramones-x-3f9a", "handoff"),
    ]
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE decision_records (id INTEGER PRIMARY KEY, point TEXT, decider TEXT, "
            "reason TEXT, agent_profile TEXT, kind TEXT)"
        )
        db.executemany("INSERT INTO decision_records VALUES (?,?,?,?,?,?)", rows)
    return rows


def test_target_kind_migration_is_additive_nullable_and_leaves_rows(tmp_path, monkeypatch):
    from cli_agent_orchestrator.clients import database

    path = tmp_path / "legacy.db"
    rows = _legacy_decision_table(path)
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)
    database._migrate_decision_records_target_kind()
    database._migrate_decision_records_target_kind()
    with sqlite3.connect(path) as db:
        columns = [row for row in db.execute("PRAGMA table_info(decision_records)")]
        migrated = db.execute(
            "SELECT id, point, decider, reason, agent_profile, kind, target_kind "
            "FROM decision_records ORDER BY id"
        ).fetchall()
    added = [column for column in columns if column[1] == "target_kind"]
    assert len(added) == 1
    assert added[0][3] == 0 and added[0][4] is None  # nullable, no default
    assert migrated == [row + (None,) for row in rows]


def test_target_kind_migration_creates_nothing_without_the_table(tmp_path, monkeypatch):
    from cli_agent_orchestrator.clients import database

    path = tmp_path / "other.db"
    sqlite3.connect(path).close()
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)
    database._migrate_decision_records_target_kind()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master").fetchall() == []


def test_init_db_adds_target_kind_to_an_existing_database(tmp_path, monkeypatch):
    from unittest.mock import patch

    from cli_agent_orchestrator.clients import database

    path = tmp_path / "legacy.db"
    _legacy_decision_table(path)
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)
    with patch.object(database, "Base"):
        database.init_db()
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(decision_records)")}
    assert "target_kind" in columns


def _seed(store, rows: list[dict[str, Any]]) -> None:
    base = dict(
        point="model.route",
        state="on",
        kind="assign",
        provider="codex",
        agent_profile=f"profile-{CANARY}",
        fallback_source="none",
        outcome="fallback",
    )
    for row in rows:
        assert store.insert({**base, **row}, f"message {CANARY}") is not None


def _status_rows(now: datetime) -> list[dict[str, Any]]:
    since = now - timedelta(hours=24)
    return [
        dict(decider="fixture", reason="timeout", created_at=since - timedelta(microseconds=1)),
        dict(decider="fixture", reason="error", created_at=since),
        dict(decider="fixture", reason="error", created_at=now - timedelta(hours=2)),
        dict(decider="fixture", outcome="applied", created_at=now - timedelta(hours=1)),
        dict(
            decider="fixture",
            state="shadow",
            outcome="shadow",
            decision_status="pending",
            created_at=now - timedelta(minutes=30),
        ),
        dict(decider="other", reason="no_answer", point="effort.route", created_at=now),
        dict(decider=None, outcome="rejected", reason="above_ceiling", created_at=now),
        dict(decider="stale", reason="error", created_at=now - timedelta(days=3)),
    ]


def _fold(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Independent oracle: count list records by decider and reason."""
    folded: dict[str, dict[str, int]] = {}
    for row in rows:
        if row["decider"] is None:
            continue
        label = row["reason"] or (
            row["decision_status"]
            if row["decision_status"] in ("pending", "interrupted")
            else "answer"
        )
        counts = folded.setdefault(row["decider"], {})
        counts[label] = counts.get(label, 0) + 1
    return folded


def test_status_opens_read_only_and_matches_list_for_the_window(setup_engine, monkeypatch):
    from cli_agent_orchestrator.decisions import operator
    from cli_agent_orchestrator.decisions import store as store_module

    _, _, _, store, _, _ = setup_engine
    _seed(store, _status_rows(NOW))
    path = Path(store.sessions.kw["bind"].url.database)
    before = path.read_bytes()
    connect, opened = sqlite3.connect, []

    def read_only(database, **kwargs):
        opened.append((database, kwargs))
        connection = connect(database, **kwargs)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM decision_records")
        return connection

    monkeypatch.setattr(store_module.sqlite3, "connect", read_only)
    status = store_module.read_decider_status(path, now=NOW)
    monkeypatch.setattr(store_module.sqlite3, "connect", connect)
    assert opened and all("mode=ro" in uri and kwargs["uri"] for uri, kwargs in opened)
    assert path.read_bytes() == before
    assert status["available"] and status["window_hours"] == 24
    since = operator.parse_date(status["since"])
    listed = store.list(since=since, limit=10000)
    counts = {name: entry["counts"] for name, entry in status["deciders"].items()}
    assert {name: value for name, value in counts.items() if value} == _fold(listed)
    assert counts["fixture"] == {"error": 2, "answer": 1, "pending": 1}
    assert counts["stale"] == {}
    everything = store.list(limit=10000)
    for name, entry in status["deciders"].items():
        newest = next(row for row in everything if row["decider"] == name)
        assert entry["last"] == {
            "point": newest["point"],
            "outcome": newest["outcome"],
            "reason": newest["reason"],
            "decision_status": newest["decision_status"],
            "created_at": newest["created_at"],
        }
    rendered = json.dumps(status)
    for row in everything:
        assert row["message_hash"] not in rendered
    assert CANARY not in rendered


def test_status_reads_last_and_counts_in_one_transaction(setup_engine, monkeypatch):
    from cli_agent_orchestrator.decisions import store as store_module

    _, _, _, store, _, _ = setup_engine
    _seed(store, _status_rows(NOW))
    path = Path(store.sessions.kw["bind"].url.database)
    calls: list[str] = []
    connect = store_module.sqlite3.connect

    def spy(database, **kwargs):
        connection = connect(database, **kwargs)
        connection.set_trace_callback(lambda sql: calls.append(sql.split(None, 1)[0]))
        return connection

    monkeypatch.setattr(store_module.sqlite3, "connect", spy)
    store_module.read_decider_status(path, now=NOW)
    assert calls[0] == "BEGIN"
    assert calls.count("SELECT") == 2


def test_status_counts_label_interrupted_separately_from_an_answer(setup_engine):
    from cli_agent_orchestrator.decisions.store import read_decider_status

    _, _, _, store, _, _ = setup_engine
    _seed(
        store,
        [
            dict(
                decider="fixture",
                state="shadow",
                outcome="shadow",
                decision_status="interrupted",
                created_at=NOW,
            ),
            dict(decider="fixture", outcome="applied", created_at=NOW),
        ],
    )
    path = Path(store.sessions.kw["bind"].url.database)
    status = read_decider_status(path, now=NOW)
    assert status["deciders"]["fixture"]["counts"] == {"interrupted": 1, "answer": 1}


@pytest.mark.parametrize("decision_status", ["pending", "interrupted"])
def test_status_last_shows_decision_status_for_an_unfinished_shadow_record(
    setup_engine, decision_status
):
    """#1076 P3-1: a pending or interrupted shadow record has no reason, so it
    looked identical to an answered record in `last`. `last` must carry
    `decision_status` so a reader can tell the two apart."""
    from cli_agent_orchestrator.decisions.store import read_decider_status

    _, _, _, store, _, _ = setup_engine
    _seed(
        store,
        [
            dict(
                decider="fixture",
                state="shadow",
                outcome="shadow",
                decision_status=decision_status,
                created_at=NOW,
            )
        ],
    )
    path = Path(store.sessions.kw["bind"].url.database)
    status = read_decider_status(path, now=NOW)
    last = status["deciders"]["fixture"]["last"]
    assert last["reason"] is None
    assert last["decision_status"] == decision_status


def test_status_since_is_utc_even_under_a_non_utc_local_timezone(monkeypatch, tmp_path):
    import time as time_module

    from cli_agent_orchestrator.decisions import operator
    from cli_agent_orchestrator.decisions.store import read_decider_status

    monkeypatch.setenv("TZ", "Asia/Taipei")  # UTC+8, no DST, so the skew can't cancel out.
    time_module.tzset()
    assert datetime.now().astimezone().utcoffset() == timedelta(hours=8)
    try:
        before = datetime.now(timezone.utc)
        status = read_decider_status(tmp_path / "absent.db", now=None)
        after = datetime.now(timezone.utc)
    finally:
        monkeypatch.undo()
        time_module.tzset()
    since = operator.parse_date(status["since"])
    assert before - timedelta(hours=24) <= since <= after - timedelta(hours=24)


def test_status_reports_a_missing_database_without_creating_it(tmp_path, monkeypatch):
    from cli_agent_orchestrator import constants
    from cli_agent_orchestrator.decisions import operator

    missing = tmp_path / "absent" / "cli-agent-orchestrator.db"
    monkeypatch.setattr(constants, "DATABASE_FILE", missing)
    result = operator.status()
    status = result["decider_status"]
    assert not status["available"]
    assert set(status["deciders"]) == {item["name"] for item in result["installed_deciders"]}
    assert all(entry == {"last": None, "counts": {}} for entry in status["deciders"].values())
    assert not missing.exists() and not missing.parent.exists()


def test_status_reads_a_database_from_before_the_migration(tmp_path, monkeypatch):
    from cli_agent_orchestrator.decisions.store import read_decider_status

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE decision_records (id INTEGER PRIMARY KEY, created_at TEXT, point TEXT, "
            "decider TEXT, outcome TEXT, reason TEXT, decision_status TEXT)"
        )
        db.execute(
            "INSERT INTO decision_records VALUES (1, ?, 'model.route', 'fixture', "
            "'fallback', 'timeout', 'done')",
            ((NOW - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S.%f"),),
        )
    status = read_decider_status(path, now=NOW)
    assert status["available"] and status["deciders"]["fixture"]["counts"] == {"timeout": 1}
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(decision_records)")}
    assert "target_kind" not in columns


def test_cli_status_counts_match_cli_list(setup_engine, monkeypatch):
    from test.decisions.test_external_controls import package

    from cli_agent_orchestrator import constants
    from cli_agent_orchestrator.cli.commands.decisions import decisions
    from cli_agent_orchestrator.decisions import operator, packages

    _, _, _, store, _, _ = setup_engine
    _seed(store, _status_rows(datetime.now(timezone.utc)))
    monkeypatch.setattr(constants, "DATABASE_FILE", Path(store.sessions.kw["bind"].url.database))
    monkeypatch.setattr(operator, "init_db", lambda: None)
    monkeypatch.setattr(operator, "DecisionStore", lambda: store)
    installed, _ = package("idle", "idle-decider")
    monkeypatch.setattr(packages, "entry_points", lambda **kwargs: [installed])
    result = CliRunner().invoke(decisions, ["status"])
    assert result.exit_code == 0, result.output
    status = json.loads(result.output)["decider_status"]
    listed = CliRunner().invoke(decisions, ["list", "--since", status["since"], "--limit", "10000"])
    assert listed.exit_code == 0, listed.output
    counts = {name: entry["counts"] for name, entry in status["deciders"].items()}
    assert {name: value for name, value in counts.items() if value} == _fold(
        json.loads(listed.output)
    )
    assert status["deciders"]["idle"] == {"last": None, "counts": {}}
    assert CANARY not in result.output
    assert "decider_status" not in operator.set_point("model.route", "off")


def test_settings_log_is_labels_only_and_repeats_only_on_change(setup_engine, caplog):
    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.types import PointState

    engine, _, decider, _, _, settings = setup_engine
    caplog.set_level(logging.INFO, logger=ENGINE_LOGGER)
    tuned = replace(
        settings,
        points={
            "model.route": PointSettings(PointState.ON, decider.name, (CANARY,)),
            "effort.route": PointSettings(PointState.OFF, decider.name),
        },
        deciders={decider.name: {"token": CANARY}},
    )
    for _ in range(2):
        engine.log_settings(tuned)
    engine.log_settings(replace(tuned, confidence_threshold=0.6, on_timeout_ms=1200))
    lines = [record.getMessage() for record in engine_lines(caplog, logging.INFO, "settings")]
    assert len(lines) == 2
    assert "threshold=0.7 on_timeout_ms=1000 shadow_timeout_ms=10000" in lines[0]
    assert "threshold=0.6 on_timeout_ms=1200" in lines[1]
    assert all(
        "effort.route=off:fixture" in line and "model.route=on:fixture" in line for line in lines
    )
    assert CANARY not in caplog.text


@pytest.mark.asyncio
async def test_launch_logs_settings_it_has_not_logged(setup_engine, caplog):
    engine, req, _, _, _, settings = setup_engine
    caplog.set_level(logging.INFO, logger=ENGINE_LOGGER)
    await engine.prepare_launch(req)
    await engine.prepare_launch(req)
    engine.settings_loader = lambda: replace(settings, shadow_timeout_ms=2000)
    await engine.prepare_launch(req)
    lines = [record.getMessage() for record in engine_lines(caplog, logging.INFO, "settings")]
    assert len(lines) == 2 and "shadow_timeout_ms=2000" in lines[1]


@pytest.mark.asyncio
async def test_settings_watch_logs_a_change_without_a_launch(setup_engine, caplog):
    engine, _, _, store, _, settings = setup_engine
    snapshots, version = [settings], [0]

    def flaky_version():
        if version[0] == 1:
            version[0] = 2
            raise OSError("transient")
        return version[0]

    engine.settings_loader = lambda: snapshots[0]
    engine._settings_version = flaky_version
    caplog.set_level(logging.INFO, logger=ENGINE_LOGGER)
    engine.log_settings(settings)
    task = asyncio.create_task(engine.watch_settings(interval_s=0.001))
    try:
        await asyncio.sleep(0.02)
        snapshots[0] = replace(settings, confidence_threshold=0.55)
        version[0] = 1
        for _ in range(200):
            if "threshold=0.55" in caplog.text:
                break
            await asyncio.sleep(0.005)
        assert "threshold=0.55" in caplog.text
        assert len(engine_lines(caplog, logging.INFO, "settings")) == 2
        assert store.list() == []
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_settings_version_changes_with_settings_file_and_halt_marker(tmp_path):
    from cli_agent_orchestrator.decisions import egress, settings
    from cli_agent_orchestrator.decisions.engine import _settings_version

    first = _settings_version()
    settings.tune(threshold=0.5)
    second = _settings_version()
    egress.HALT_FILE.touch()
    third = _settings_version()
    assert len({first, second, third}) == 3


@pytest.mark.asyncio
async def test_server_logs_settings_at_startup_and_stops_the_watch(monkeypatch, caplog):
    from test.api.test_lifespan_inbox import _patched_lifespan
    from unittest.mock import AsyncMock, MagicMock

    from cli_agent_orchestrator.api import main
    from cli_agent_orchestrator.decisions.store import DecisionStore

    monkeypatch.setattr(DecisionStore, "sweep", lambda self: None)
    monkeypatch.setattr(
        main.terminal_service,
        "recover_interrupted_deferred_init_external_owners",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(main, "_seed_default_skills_at_startup", lambda: None)
    monkeypatch.setattr(main, "_reconcile_memory_at_startup", lambda: None)
    caplog.set_level(logging.INFO, logger=ENGINE_LOGGER)
    tasks: list[Any] = []
    with _patched_lifespan(MagicMock(), tasks):
        async with main.lifespan(main.app):
            assert engine_lines(caplog, logging.INFO, "Decision settings")
    watch = [
        task
        for task in tasks
        if getattr(getattr(task.source, "cr_code", None), "co_name", "") == "watch_settings"
    ]
    assert len(watch) == 1 and watch[0].cancel.called
