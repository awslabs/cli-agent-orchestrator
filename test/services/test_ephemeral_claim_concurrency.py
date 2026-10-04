"""Guarded transitions compete over a real SQLite file, not a mock or timer."""

import threading
from concurrent.futures import ThreadPoolExecutor
from test.services.test_ephemeral_claim import NOW, claim
from test.services.test_ephemeral_service import CALLER, create, create_store  # noqa: F401

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database


@pytest.fixture
def file_store(create_store, monkeypatch, tmp_path):
    path = tmp_path / "claim-races.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        "sqlite:///" + str(path), connect_args={"check_same_thread": False, "timeout": 10}
    )
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(database, "_utcnow", lambda: NOW)
    database.create_terminal(CALLER, "cao-session", "owner", "claude_code", allowed_tools=["*"])
    env = (create_store[0], factory, *create_store[2:])
    name = create(env)["name"]
    yield env, name
    engine.dispose()


def outcome(env, name):
    try:
        return claim(env, name)
    except env[0].EphemeralPolicyError as exc:
        return exc.rule


def test_double_claim_has_one_winner(file_store):
    env, name = file_store
    start = threading.Barrier(2)

    def run():
        start.wait(timeout=10)
        return outcome(env, name)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(run), pool.submit(run)
        results = [a.result(timeout=10), b.result(timeout=10)]
    assert sum(isinstance(r, dict) for r in results) == 1
    assert results.count("already_claimed") == 1
    assert database.get_ephemeral_agent(name)["state"] == "claimed"


@pytest.mark.parametrize("bad_lease", [False, True])
def test_winner_recheck_reverts_without_admitting_competitor(file_store, monkeypatch, bad_lease):
    env, name = file_store
    if bad_lease:
        env[4]["ephemeral"]["claim_lease_seconds"] = None
    else:
        env[4]["ephemeral"]["max_depth"] = 2
    service = env[0]
    original = service._recheck_policy
    inside = threading.Barrier(2)
    release = threading.Event()

    def hold(*args, **kwargs):
        inside.wait(timeout=10)
        assert release.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_recheck_policy", hold)
    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(outcome, env, name)
        inside.wait(timeout=10)
        loser = pool.submit(outcome, env, name)
        try:
            assert loser.result(timeout=10) == "already_claimed"
        finally:
            release.set()
        assert winner.result(timeout=10) == (
            "policy_config_error:claim_lease_seconds"
            if bad_lease
            else "policy_config_error:max_depth"
        )
    row = database.get_ephemeral_agent(name)
    assert row["state"] == "pending" and row["claim_id"] is None
    service.end_claim(name, "stale")
    assert database.get_ephemeral_agent(name) == row


def test_registry_survives_reconnect_and_lapses_lazily(file_store, monkeypatch):
    from datetime import timedelta

    env, name = file_store
    first = claim(env, name)
    assert database.get_ephemeral_agent(name)["state"] == "claimed"
    env[1].kw["bind"].dispose()
    assert database.get_ephemeral_agent(name)["claim_id"] == first["claim_id"]
    monkeypatch.setattr(database, "_utcnow", lambda: NOW + timedelta(seconds=60))
    second = claim(env, name)
    assert second["replayed"] is False
    assert database.get_ephemeral_agent(name)["state"] == "claimed"
