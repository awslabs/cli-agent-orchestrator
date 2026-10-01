import asyncio
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CAO_HOME_DIR", str(tmp_path / "cao"))
    from cli_agent_orchestrator.services import settings_service

    monkeypatch.setattr(settings_service, "CAO_HOME_DIR", tmp_path)
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", tmp_path / "settings.json")
    for key in (
        "CAO_DECISION_MODEL_ROUTE",
        "CAO_DECISION_EFFORT_ROUTE",
        "CAO_DECISION_ON_TIMEOUT_MS",
        "CAO_DECISION_CONFIDENCE_THRESHOLD",
    ):
        monkeypatch.delenv(key, raising=False)


def test_ordered_options_and_typed_fallback():
    from cli_agent_orchestrator.decisions.policy import (
        IDENTITY_POLICY,
        PolicyBounds,
        resolve_fallback,
    )
    from cli_agent_orchestrator.decisions.types import (
        EFFORT_ROUTE,
        MODEL_ROUTE,
        Fallback,
        MissingTier,
    )

    assert MODEL_ROUTE.options == ("small", "medium", "large")
    assert EFFORT_ROUTE.options == ("low", "medium", "high")
    assert resolve_fallback("model.route", None, IDENTITY_POLICY, {}, "codex") == Fallback(
        None, "none"
    )
    missing = resolve_fallback("model.route", None, PolicyBounds(default_tier="small"), {}, "codex")
    assert isinstance(missing, MissingTier)
    assert not hasattr(missing, "value")
    assert (
        resolve_fallback(
            "effort.route", None, PolicyBounds(default_effort="low"), {}, "codex"
        ).value
        == "low"
    )


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"max_tier": "medium"}, "model.route ceiling requires a default"),
        ({"max_effort": "medium"}, "effort.route ceiling requires a default"),
        ({"default_tier": "large", "max_tier": "small"}, "model.route default exceeds ceiling"),
        ({"default_effort": "high", "max_effort": "low"}, "effort.route default exceeds ceiling"),
        ({"default_tier": "simple"}, "invalid policy value for model.route"),
        ({"default_effort": "extreme"}, "invalid policy value for effort.route"),
        ({"allowed_providers": frozenset({"invalid"})}, "invalid allowed provider"),
    ],
)
def test_policy_invalid(kwargs, expected):
    from cli_agent_orchestrator.decisions.policy import PolicyBounds, PolicyConfigError

    with pytest.raises(PolicyConfigError, match=expected):
        PolicyBounds(**kwargs).validate({})


def test_policy_mapping_and_cap():
    from cli_agent_orchestrator.decisions.policy import (
        PolicyBounds,
        PolicyConfigError,
        PolicyViolation,
        UnmappedTierError,
    )

    policy = PolicyBounds(
        default_tier="small", max_tier="medium", default_effort="low", max_effort="medium"
    )
    policy.validate({"codex": {"small": "x", "medium": "y"}})
    with pytest.raises(PolicyConfigError, match=r"model_tiers.codex.small"):
        PolicyBounds(default_tier="small").validate({"codex": {"medium": "y"}})
    assert policy.cap("model.route", "large") == ("medium", True)
    assert policy.cap("effort.route", "high") == ("medium", True)
    with pytest.raises(PolicyViolation, match="medium"):
        policy.check_explicit("effort.route", "high", "codex", {})
    with pytest.raises(UnmappedTierError, match="explicit tier"):
        policy.check_explicit("model.route", "small", "codex", {})


def test_settings_precedence_clamps_and_invalid(monkeypatch):
    from cli_agent_orchestrator.decisions.settings import (
        load_settings,
        set_exclusions,
        set_point,
        set_table,
        set_tier,
        tune,
    )

    set_point("model.route", "shadow")
    monkeypatch.setenv("CAO_DECISION_MODEL_ROUTE", "on")
    assert load_settings().points["model.route"].state.value == "on"
    assert load_settings(flags={"model.route": "off"}).points["model.route"].state.value == "off"
    monkeypatch.setenv("CAO_DECISION_MODEL_ROUTE", "invalid")
    assert load_settings().points["model.route"].state.value == "off"
    tune(on_timeout_ms=1, threshold=9, retention_days=30)
    assert load_settings().on_timeout_ms == 50
    assert load_settings().confidence_threshold == 1
    set_tier("codex", "small", "model-x")
    assert load_settings().model_tiers == {"codex": {"small": "model-x"}}
    set_tier("codex", "small", None)
    set_table("model.route", "small", profile="worker")
    set_exclusions(add="worker")
    assert load_settings().points["model.route"].exclude_profiles == ("worker",)
    set_exclusions(remove="worker")
    assert load_settings().points["model.route"].exclude_profiles == ()
    with pytest.raises(ValueError):
        set_table("model.route", "simple", role="worker")
    from cli_agent_orchestrator.services import settings_service

    settings_service.SETTINGS_FILE.write_text("broken")
    assert all(
        p.state.value == "off" for p in load_settings(flags={"model.route": "on"}).points.values()
    )


def test_model_tiers_invalid_entries(caplog):
    from cli_agent_orchestrator.services.model_tiers import load_model_tiers

    assert load_model_tiers(
        {
            "model_tiers": {
                "codex": {"small": "x", "large": "bad model", "simple": "y"},
                "invalid": {"small": "x"},
            }
        }
    ) == {"codex": {"small": "x"}}
    assert caplog.records


@pytest.mark.asyncio
async def test_registry_lazy_cached_and_fixed_table():
    from cli_agent_orchestrator.decisions.fixed_table import FixedTableDecider
    from cli_agent_orchestrator.decisions.registry import DeciderRegistry
    from cli_agent_orchestrator.decisions.types import DecisionFacts, DecisionRequest

    calls = []

    def factory():
        calls.append(1)
        return FixedTableDecider()

    registry = DeciderRegistry({"fixed_table": factory})
    assert calls == []
    decider = registry.get("fixed_table", "model.route")
    assert registry.get("fixed_table", "model.route") is decider
    assert calls == [1]
    request = DecisionRequest(
        1,
        "request",
        ("model.route",),
        "text",
        None,
        DecisionFacts("codex", "assign", "worker", "developer", 4, False),
    )
    response = await decider.decide(
        request, {"model.route": {"profiles": {"worker": "small"}, "roles": {"developer": "large"}}}
    )
    assert response["model.route"].option == "small"
    assert await decider.decide(request, {}) is None
    assert (
        await decider.decide(request, {"model.route": {"profiles": {"worker": "simple"}}}) is None
    )
    assert registry.get("missing", "model.route") is None
    await registry.close()


@pytest.mark.asyncio
async def test_registry_failure_cached(caplog):
    from cli_agent_orchestrator.decisions.registry import DeciderRegistry

    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("not logged")

    registry = DeciderRegistry({"broken": broken})
    assert registry.get("broken", "model.route") is None
    assert registry.get("broken", "model.route") is None
    assert calls == [1]
    assert len(caplog.records) == 1
    await registry.close()


def test_key_rotation_race_and_short_key(tmp_path):
    from cli_agent_orchestrator.decisions.hashing import (
        KeyCache,
        cleanup_temps,
        load_key,
        rotate_key,
    )

    path = tmp_path / "decision-hash.key"
    with ThreadPoolExecutor(2) as pool:
        keys = list(pool.map(lambda _: load_key(path), range(2)))
    assert keys[0] == keys[1]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    first = KeyCache(path).digest("message")
    rotate_key(path)
    assert KeyCache(path).digest("message")[1] != first[1]
    assert not list(tmp_path.glob(".decision-hash-*"))
    (tmp_path / ".decision-hash-leftover").write_bytes(b"x")
    import os
    import time

    old = time.time() - 120
    os.utime(tmp_path / ".decision-hash-leftover", (old, old))
    cleanup_temps(path)
    assert not (tmp_path / ".decision-hash-leftover").exists()
    path.write_bytes(b"")
    with pytest.raises(OSError, match="32 bytes"):
        load_key(path)


def test_store_migration_bind_sweep_purge_retention(tmp_path):
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.decisions.store import DecisionStore

    engine = create_engine("sqlite:///" + str(tmp_path / "state.db"))
    database.DecisionRecordModel.__table__.create(engine)
    database._migrate_decision_records(engine)
    assert len(inspect(engine).get_indexes("decision_records")) == 3
    store = DecisionStore(
        sessionmaker(bind=engine), tmp_path / "decision-hash.key", emit=lambda row: None
    )
    row = dict(
        point="model.route",
        state="on",
        kind="assign",
        provider="codex",
        agent_profile="worker",
        fallback_source="profile",
        fallback_value="model-y",
        outcome="fallback",
        reason="unsure",
        decision_status="done",
    )
    record_id = store.insert(row, "message")
    store.bind(
        (record_id,),
        launch_status="launched",
        terminal_id="worker",
        launched_model="model-y",
        model_honored=True,
    )
    store.bind((record_id,), launch_status="not_launched")
    assert store.list()[0]["launch_status"] == "launched"
    store.bind((record_id,), launch_status="launch_failed")
    assert store.list()[0]["launch_status"] == "launch_failed"
    pending = store.insert({**row, "decision_status": "pending"}, "other")
    store.sweep()
    assert store.get(pending)["decision_status"] == "interrupted"
    assert store.get(pending)["launch_status"] == "unknown"
    assert store.purge(before=datetime.now(timezone.utc) - timedelta(days=1)) == 0
    assert store.purge(all_records=True, rotate=True) == 2
    assert store.list() == []


def test_key_two_process_publish_race(tmp_path):
    import os
    import subprocess
    import sys

    code = """import os, sys
from pathlib import Path
from cli_agent_orchestrator.decisions.hashing import load_key
link = os.link
def synchronized_link(source, target):
    print('ready', flush=True)
    sys.stdin.readline()
    return link(source, target)
os.link = synchronized_link
print(load_key(Path(sys.argv[1])).hex(), flush=True)
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(tmp_path / "decision-hash.key")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "HOME": str(tmp_path), "CAO_HOME_DIR": str(tmp_path / "cao")},
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            assert process.stdout.readline().strip() == "ready"
        for process in processes:
            process.stdin.write("publish\n")
            process.stdin.flush()
        keys = [process.communicate(timeout=10) for process in processes]
        assert keys[0][0] == keys[1][0] and len(keys[0][0].strip()) == 64
        assert all(process.returncode == 0 for process in processes)
        assert not list(tmp_path.glob(".decision-hash-*"))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()


def test_tier_reader_unreadable_and_invalid_root():
    from cli_agent_orchestrator.services import settings_service
    from cli_agent_orchestrator.services.model_tiers import load_model_tiers

    assert load_model_tiers() == {}
    settings_service.SETTINGS_FILE.write_text("broken")
    assert load_model_tiers() == {}
    assert load_model_tiers({"model_tiers": []}) == {}


def test_migration_is_additive_and_idempotent(tmp_path):
    from sqlalchemy import text

    from cli_agent_orchestrator.clients import database

    db = create_engine("sqlite:///" + str(tmp_path / "old.db"))
    with db.begin() as conn:
        conn.execute(text("CREATE TABLE old_data (value TEXT)"))
        conn.execute(text("INSERT INTO old_data VALUES ('preserved')"))
    database._migrate_decision_records(db)
    database._migrate_decision_records(db)
    with db.begin() as conn:
        assert conn.execute(text("SELECT value FROM old_data")).scalar() == "preserved"
    assert "decision_records" in inspect(db).get_table_names()


def test_cleanup_uses_decision_retention_window(tmp_path, monkeypatch):
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.decisions.store import DecisionStore
    from cli_agent_orchestrator.services import cleanup_service, settings_service

    db = create_engine("sqlite:///" + str(tmp_path / "cleanup.db"))
    database.Base.metadata.create_all(db)
    sessions = sessionmaker(bind=db)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(cleanup_service, "SessionLocal", sessions)
    monkeypatch.setattr(cleanup_service, "TERMINAL_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(cleanup_service, "LOG_DIR", tmp_path / "logs")
    settings_service.SETTINGS_FILE.write_text('{"decisions": {"retention_days": 90}}')
    store = DecisionStore(sessions, tmp_path / "decision-hash.key", emit=lambda row: None)
    row = dict(
        point="model.route",
        state="on",
        kind="assign",
        provider="codex",
        fallback_source="none",
        outcome="fallback",
    )
    old = store.insert(row, "old")
    recent = store.insert(row, "recent")
    with sessions() as session:
        session.query(database.DecisionRecordModel).filter_by(id=old).update(
            {"created_at": datetime.now(timezone.utc) - timedelta(days=100)}
        )
        session.query(database.DecisionRecordModel).filter_by(id=recent).update(
            {"created_at": datetime.now(timezone.utc) - timedelta(days=30)}
        )
        session.commit()
    cleanup_service.cleanup_old_data()
    assert store.get(old) is None and store.get(recent) is not None
