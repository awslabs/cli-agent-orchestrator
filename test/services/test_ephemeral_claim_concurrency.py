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
    env[4]["ephemeral"]["default_tier"] = "small"
    env[4]["model_tiers"] = {"claude_code": {"small": "model-t1"}}
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


@pytest.mark.parametrize(
    "bad_lease", [False, True, "default_tier_unmapped", "max_tier_without_default"]
)
def test_winner_recheck_reverts_without_admitting_competitor(file_store, monkeypatch, bad_lease):
    env, name = file_store
    if bad_lease == "default_tier_unmapped":
        env[4]["model_tiers"] = {}
    elif bad_lease == "max_tier_without_default":
        env[4]["ephemeral"].update(max_tier="medium", default_tier=None)
    elif bad_lease:
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
            "policy_config_error:model_tiers"
            if bad_lease == "default_tier_unmapped"
            else (
                "policy_config_error:default_tier"
                if bad_lease == "max_tier_without_default"
                else (
                    "policy_config_error:claim_lease_seconds"
                    if bad_lease
                    else "policy_config_error:max_depth"
                )
            )
        )
    row = database.get_ephemeral_agent(name)
    assert row["state"] == "pending" and row["claim_id"] is None
    service.end_claim(name, "stale")
    assert database.get_ephemeral_agent(name) == row


def test_claim_competes_with_claimless_bind(file_store):
    env, name = file_store
    start = threading.Barrier(2)

    def run(bind):
        start.wait(timeout=10)
        if not bind:
            return outcome(env, name)
        try:
            env[0].bind_ephemeral_agent(name, "eeeeeeee", CALLER, "claude_code", None, None)
            return "bound"
        except env[0].EphemeralPolicyError as exc:
            return exc.rule

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(run, False), pool.submit(run, True)
        results = [a.result(timeout=10), b.result(timeout=10)]
    assert results.count("already_claimed") == 1
    assert sum(isinstance(r, dict) or r == "bound" for r in results) == 1
    assert database.get_ephemeral_agent(name)["state"] in {"claimed", "launched"}


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


def test_A_X3_stale_finalize_cannot_replace_new_claim(file_store, monkeypatch):
    """A stalled renderer cannot overwrite the current claimer's live inode."""
    import hashlib
    from datetime import timedelta

    env, name = file_store
    service = env[0]
    tokens = iter(range(1, 100))
    monkeypatch.setattr(service.secrets, "token_hex", lambda n: f"{next(tokens):0{n * 2}x}")
    written = threading.Event()
    release = threading.Event()
    writer = service._write_exclusive
    first_thread = None

    def hold(path, payload, owned):
        writer(path, payload, owned)
        if threading.get_ident() == first_thread and ".md." in path.name:
            written.set()
            assert release.wait(timeout=10)

    monkeypatch.setattr(service, "_write_exclusive", hold)

    def first():
        nonlocal first_thread
        first_thread = threading.get_ident()
        return outcome(env, name)

    with ThreadPoolExecutor(max_workers=2) as pool:
        stale = pool.submit(first)
        assert written.wait(timeout=10)
        try:
            monkeypatch.setattr(database, "_utcnow", lambda: NOW + timedelta(seconds=60))
            env[4]["model_tiers"]["claude_code"]["small"] = "model-t2"
            current = claim(env, name)
            live = env[2] / "ephemeral" / "live" / (name + ".md")
            inode = live.stat().st_ino
            contents = live.read_bytes()
            assert b"model: model-t2" in contents
        finally:
            release.set()
        assert stale.result(timeout=10) == "claim_expired"
    row = database.get_ephemeral_agent(name)
    assert row["claim_id"] == current["claim_id"]
    assert live.stat().st_ino == inode
    assert live.read_bytes() == contents
    assert row["profile_sha256"] == hashlib.sha256(contents).hexdigest()
    assert not list(live.parent.glob("*.tmp"))


@pytest.mark.parametrize("fault", ["broken", "tightened"])
def test_A_X2_stale_policy_transition_cannot_touch_new_claim(file_store, monkeypatch, fault):
    from datetime import timedelta
    from test.services.test_ephemeral_claim import row_update
    from test.services.test_ephemeral_tier_policy import mapped

    env, name = file_store
    service = env[0]
    mapped(env[4])
    row_update(env, name, model_tier="large")
    tokens = iter(range(1, 100))
    monkeypatch.setattr(service.secrets, "token_hex", lambda n: f"{next(tokens):0{n*2}x}")
    if fault == "broken":
        env[4]["model_tiers"] = {}
    else:
        env[4]["ephemeral"].update(max_tier="medium", default_tier="small")
    entered = threading.Event()
    release = threading.Event()
    original = service._recheck_policy
    first_thread = None

    def hold(*args, **kwargs):
        if threading.get_ident() == first_thread:
            entered.set()
            assert release.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_recheck_policy", hold)

    def stale():
        nonlocal first_thread
        first_thread = threading.get_ident()
        return outcome(env, name)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(stale)
        assert entered.wait(timeout=10)
        try:
            mapped(env[4])
            env[4]["ephemeral"]["max_tier"] = None
            monkeypatch.setattr(database, "_utcnow", lambda: NOW + timedelta(seconds=60))
            second = claim(env, name)
            before = database.get_ephemeral_agent(name)
            live = env[2] / "ephemeral/live" / (name + ".md")
            contents = live.read_bytes()
        finally:
            release.set()
        assert first.result(timeout=10) == (
            "policy_config_error:model_tiers"
            if fault == "broken"
            else "policy_changed_since_create:tier_exceeds_policy"
        )
    assert database.get_ephemeral_agent(name) == before
    assert before["state"] == "claimed" and before["claim_id"] == second["claim_id"]
    assert live.read_bytes() == contents
    assert (live.parent / (name + ".spec.json")).exists()
