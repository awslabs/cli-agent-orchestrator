"""Startup and periodic sweeps, the live-name cap and retention (#1079, #801 E3).

Every default and rule exercised here is the issue's proposal, awaiting maintainer
confirmation. Race tests use a real SQLite file and real threads, not timers.
"""

import concurrent.futures
import hashlib
import itertools
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from test.services.test_ephemeral_claim import NOW
from test.services.test_ephemeral_service import CALLER, SPEC, create_store  # noqa: F401
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import settings_service
from cli_agent_orchestrator.services.settings_service import (
    EPHEMERAL_DEFAULTS,
    SettingsUnreadableError,
)
from cli_agent_orchestrator.utils import atomic_file

CHILD = "eeeeeeee"
OTHER = "bbbbbbbb"
TTL = EPHEMERAL_DEFAULTS["pending_ttl_seconds"]
LEASE = EPHEMERAL_DEFAULTS["claim_lease_seconds"]
GRACE = EPHEMERAL_DEFAULTS["launch_grace_seconds"]
RETENTION = timedelta(days=EPHEMERAL_DEFAULTS["retention_days"])
LONG = 10**7  # A TTL or lease that outlives every clock step below.
CANARY = "synthetic-canary-7c41"


class Clock:
    """The registry clock every service call reads; tests move it explicitly."""

    def __init__(self, monkeypatch):
        self.now = NOW
        monkeypatch.setattr(database, "_utcnow", lambda: self.now)

    def at(self, **delta):
        self.now = NOW + timedelta(**delta)


@pytest.fixture
def store(create_store, monkeypatch, tmp_path):
    service, _, home, _, config = create_store
    counter = itertools.count(1)
    monkeypatch.setattr(service.secrets, "token_hex", lambda n: f"{next(counter):0{n * 2}x}")
    monkeypatch.setattr(atomic_file, "LOCK_DIR", tmp_path / "locks")
    return SimpleNamespace(
        service=service, home=home, config=config["ephemeral"], clock=Clock(monkeypatch)
    )


@pytest.fixture
def file_store(store, monkeypatch, tmp_path):
    """The same store over a SQLite file, so threads get real separate connections."""
    engine = create_engine(
        "sqlite:///" + str(tmp_path / "sweep.sqlite"),
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    database.Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    database.create_terminal(CALLER, "cao-session", "owner", "claude_code", allowed_tools=["*"])
    yield store
    engine.dispose()


def make(s, purpose="log_triage", caller=CALLER, brief="Inspect logs."):
    spec = {**SPEC, "purpose": purpose, "brief": brief}
    return s.service.create_ephemeral_agent(spec, caller)["name"]


def row(name):
    return database.get_ephemeral_agent(name)


def rows():
    with database.SessionLocal() as db:
        return db.query(database.EphemeralAgentModel).count()


def live(s, name):
    folder = s.home / "ephemeral" / "live"
    return folder / (name + ".md"), folder / (name + ".spec.json")


def archive(name):
    return Path(row(name)["audit_path"])


def claim(s, name):
    return s.service.claim_ephemeral_agent(name, {}, CALLER)["claim_id"]


def launch(s, name, terminal=CHILD, with_row=True):
    """Claim and bind; without the terminal row this is the SIGKILL stand-in."""
    token = claim(s, name)
    s.service.bind_ephemeral_agent(name, terminal, CALLER, "claude_code", token, None)
    if with_row:
        database.create_terminal(terminal, "cao-session", "child", "claude_code")


def sweep(s, startup=False):
    return s.service.sweep(startup=startup)


def age(path, when):
    os.utime(path, (when.timestamp(), when.timestamp()))


def snapshot(*roots):
    """Every entry under the roots, by type and content, without following links."""
    found = {}
    for root in roots:
        for folder, dirs, files in os.walk(root):
            for entry in dirs + files:
                path = Path(folder) / entry
                info = path.lstat()
                if path.is_symlink():
                    found[path] = ("link", os.readlink(path))
                elif path.is_dir():
                    found[path] = ("dir", info.st_mode)
                else:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    found[path] = ("file", info.st_mode, info.st_mtime_ns, digest)
    return found


def events(path):
    return [entry["event"] for entry in json.loads(path.read_bytes())["events"]]


def outcome(s, **kwargs):
    try:
        return make(s, **kwargs)
    except s.service.EphemeralPolicyError as exc:
        return exc.rule


def paused_create(s, monkeypatch):
    """Start a create that stops after its last live file, before its pending row."""
    paused, resume = threading.Event(), threading.Event()
    original = s.service._write_exclusive

    def write(path, payload, owned):
        original(path, payload, owned)
        if path.parent.name == "live" and path.suffix == ".md" and path.name[0] != ".":
            paused.set()
            assert resume.wait(timeout=10)

    monkeypatch.setattr(s.service, "_write_exclusive", write)
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(outcome, s)
    assert paused.wait(timeout=10)
    return future, resume, pool


def hold_sweep_row(s, monkeypatch):
    """Pause each sweep between its candidate read and the row's own transaction."""
    picked, go = threading.Event(), threading.Event()
    original = s.service._sweep_row

    def held(*args, **kwargs):
        picked.set()
        assert go.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(s.service, "_sweep_row", held)
    return picked, go


# Pending names


def test_periodic_sweep_collects_an_untouched_expired_pending_name(store):
    name = make(store)
    store.clock.at(seconds=TTL - 1)
    assert sweep(store) == {}
    assert row(name)["state"] == "pending"
    assert all(path.is_file() for path in live(store, name))
    store.clock.at(seconds=TTL)
    assert sweep(store).get("ephemeral_expired") == 1
    collected = row(name)
    assert collected["state"] == "gc" and collected["gc_reason"] == "ephemeral_expired"
    assert not any(path.exists() for path in live(store, name))
    record = json.loads(archive(name).read_bytes())
    assert record["events"][-1]["event"] == "released"
    assert record["gc_reason"] == "ephemeral_expired"


@pytest.mark.parametrize("startup", [True, False])
def test_pending_name_whose_owner_is_gone_is_collected(store, startup):
    name = make(store)
    database.delete_terminal(CALLER)
    assert sweep(store, startup).get("owner_gone") == 1
    collected = row(name)
    assert collected["state"] == "gc" and collected["gc_reason"] == "owner_gone"
    assert not any(path.exists() for path in live(store, name))
    assert events(archive(name))[-1] == "released"


def test_sweep_runs_with_creation_disabled(store):
    name = make(store)
    store.config["enabled"] = False
    store.clock.at(seconds=TTL)
    assert sweep(store).get("ephemeral_expired") == 1
    assert row(name)["state"] == "gc"


@pytest.mark.parametrize(
    "error",
    [
        SettingsUnreadableError(Path("settings.json"), ValueError("bad json")),
        RecursionError("settings nested too deeply"),  # The loader doesn't wrap this one.
    ],
)
def test_unreadable_settings_skip_the_whole_pass(store, monkeypatch, caplog, error):
    name = make(store)
    store.clock.at(seconds=TTL)

    def unreadable():
        raise error

    monkeypatch.setattr(settings_service, "_load_or_raise", unreadable)
    assert sweep(store) == {}
    assert row(name)["state"] == "pending"
    assert all(path.is_file() for path in live(store, name))
    assert "ephemeral sweep skipped" in caplog.text


def test_each_pass_logs_one_content_free_line(store, caplog):
    make(store, brief="Inspect logs. " + CANARY)
    store.clock.at(seconds=TTL)
    with caplog.at_level(logging.INFO, logger=store.service.__name__):
        sweep(store)
        lines = [r.getMessage() for r in caplog.records if "ephemeral sweep" in r.getMessage()]
        assert len(lines) == 1 and "ephemeral_expired=1" in lines[0]
        assert CANARY not in caplog.text
        caplog.clear()
        sweep(store)
        assert "ephemeral sweep" not in caplog.text


# Claimed names


@pytest.mark.parametrize("startup", [True, False])
@pytest.mark.parametrize("owner_gone", [False, True])
def test_claim_inside_its_lease_survives_every_sweep(store, startup, owner_gone):
    name = make(store)
    store.clock.at(seconds=TTL - 10)
    claim(store, name)
    if owner_gone:
        database.delete_terminal(CALLER)
    paths = (*live(store, name), archive(name))
    before = row(name), [path.read_bytes() for path in paths]
    store.clock.at(seconds=TTL - 10 + LEASE - 1)  # Past the TTL, inside the lease.
    assert sweep(store, startup) == {}
    assert (row(name), [path.read_bytes() for path in paths]) == before


@pytest.mark.parametrize("startup", [True, False])
def test_lapsed_claim_returns_to_pending_inside_the_ttl(store, startup):
    name = make(store)
    claim(store, name)
    store.clock.at(seconds=LEASE)
    assert sweep(store, startup) == {}
    current = row(name)
    assert current["state"] == "pending"
    assert current["claim_id"] is None and current["claim_expires_at"] is None
    assert all(path.is_file() for path in live(store, name))


def test_lapsed_claim_past_the_ttl_is_collected(store):
    name = make(store)
    store.clock.at(seconds=TTL - 1)
    claim(store, name)
    store.clock.at(seconds=TTL - 1 + LEASE)
    assert sweep(store).get("ephemeral_expired") == 1
    assert row(name)["gc_reason"] == "ephemeral_expired"
    assert not any(path.exists() for path in live(store, name))
    assert archive(name).is_file()


def test_a_claim_in_progress_is_left_alone(file_store, monkeypatch):
    s = file_store
    name = make(s)
    original = s.service._recheck_policy
    inside, go = threading.Event(), threading.Event()

    def hold(*args, **kwargs):
        inside.set()
        assert go.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(s.service, "_recheck_policy", hold)
    with ThreadPoolExecutor(max_workers=1) as pool:
        claiming = pool.submit(claim, s, name)
        assert inside.wait(timeout=10)
        try:
            assert sweep(s, True) == {} and sweep(s, False) == {}
            assert row(name)["state"] == "claimed"
        finally:
            go.set()
        token = claiming.result(timeout=10)
    assert all(path.is_file() for path in live(s, name))
    s.service.bind_ephemeral_agent(name, CHILD, CALLER, "claude_code", token, None)
    assert row(name)["state"] == "launched"


def test_a_reclaim_that_wins_the_race_is_never_swept(file_store, monkeypatch):
    """The sweep reads a lapsed claim; its owner claims again before the sweep's write."""
    s = file_store
    name = make(s)
    first = claim(s, name)
    s.clock.at(seconds=LEASE)
    picked, go = hold_sweep_row(s, monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(sweep, s)
        try:
            assert picked.wait(timeout=10)
            second = claim(s, name)
        finally:
            go.set()
        assert running.result(timeout=10) == {}
    current = row(name)
    assert current["state"] == "claimed" and current["claim_id"] == second != first
    assert all(path.is_file() for path in live(s, name))
    s.service.bind_ephemeral_agent(name, CHILD, CALLER, "claude_code", second, None)
    assert row(name)["state"] == "launched"


# Launched names


@pytest.mark.parametrize("startup", [True, False])
@pytest.mark.parametrize("missing", [None, ".md", ".spec.json"])
def test_launched_row_with_a_terminal_is_never_swept(store, startup, missing):
    name = make(store)
    launch(store, name)
    if missing:
        (store.home / "ephemeral" / "live" / (name + missing)).unlink()
    paths = [path for path in (*live(store, name), archive(name)) if path.exists()]
    before = row(name), [path.read_bytes() for path in paths]
    store.clock.at(days=30)
    assert sweep(store, startup) == {}
    assert (row(name), [path.read_bytes() for path in paths]) == before


def test_terminalless_launch_waits_out_the_grace_on_periodic_passes(store):
    name = make(store)
    launch(store, name, with_row=False)
    store.clock.at(seconds=GRACE - 1)
    assert sweep(store) == {}
    assert row(name)["state"] == "launched"
    assert all(path.is_file() for path in live(store, name))
    store.clock.at(seconds=GRACE)
    assert sweep(store).get("terminal_gone") == 1
    assert row(name)["gc_reason"] == "terminal_gone"
    assert not any(path.exists() for path in live(store, name))
    assert events(archive(name))[-1] == "released"


def test_startup_sweep_collects_a_terminalless_launch_at_once(store):
    """The SIGKILL stand-in: a launched row and its files, with no terminal row."""
    name = make(store)
    launch(store, name, with_row=False)
    store.clock.at(seconds=1)
    assert sweep(store, startup=True).get("terminal_gone") == 1
    assert row(name)["state"] == "gc"
    assert not any(path.exists() for path in live(store, name))
    record = json.loads(archive(name).read_bytes())
    assert record["events"][-1]["event"] == "released"
    assert record["gc_reason"] == "terminal_gone"


def test_unusable_grace_leaves_terminalless_launches_to_startup(store, caplog):
    name = make(store)
    launch(store, name, with_row=False)
    store.config["launch_grace_seconds"] = 0
    store.clock.at(days=1)
    assert sweep(store) == {}
    assert row(name)["state"] == "launched"
    assert "ephemeral.launch_grace_seconds" in caplog.text
    assert sweep(store, startup=True).get("terminal_gone") == 1


def test_a_terminal_inserted_after_the_candidate_read_wins(file_store, monkeypatch):
    """A slow launch inserts its terminal row between the sweep's read and its write."""
    s = file_store
    name = make(s)
    launch(s, name, with_row=False)
    s.clock.at(seconds=GRACE)
    picked, go = hold_sweep_row(s, monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(sweep, s)
        try:
            assert picked.wait(timeout=10)
            database.create_terminal(CHILD, "cao-session", "child", "claude_code")
        finally:
            go.set()
        assert running.result(timeout=10) == {}
    assert row(name)["state"] == "launched"
    assert all(path.is_file() for path in live(s, name))


# Collected names and rowless files


def test_a_later_sweep_retries_a_failed_collection_deletion(store, monkeypatch, tmp_path):
    name = make(store)
    store.clock.at(seconds=10)
    other = make(store, purpose="code_review")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / (name + ".md")).write_bytes(b"decoy")
    real_unlink = os.unlink

    def failing(path, *args, dir_fd=None, **kwargs):
        if dir_fd is not None:
            raise PermissionError("injected deletion failure")
        return real_unlink(path, *args, **kwargs)

    store.clock.at(seconds=TTL)
    with monkeypatch.context() as patch:
        patch.setattr(store.service.os, "unlink", failing)
        assert sweep(store).get("ephemeral_expired") == 1
    assert row(name)["state"] == "gc"
    assert all(path.is_file() for path in live(store, name))
    expected = snapshot(store.home, outside)
    for path in live(store, name):
        del expected[path]
    assert sweep(store).get("retried") == 1
    assert snapshot(store.home, outside) == expected
    assert row(other)["state"] == "pending" and archive(name).is_file()


def test_rowless_live_files_are_removed_by_exact_name_only(store, tmp_path):
    kept = make(store)
    folder = store.home / "ephemeral" / "live"
    residue = [folder / ("Crashed-log_triage-beef" + suffix) for suffix in (".md", ".spec.json")]
    for path in residue:
        path.write_bytes(b"residue")
    (tmp_path / "outside").mkdir()
    target = tmp_path / "outside" / "target.md"
    target.write_bytes(b"outside")
    (folder / "Linked-log_triage-cafe.md").symlink_to(target)
    (folder / "Folder-log_triage-f00d.md").mkdir()
    for other in ("notes.md", "lowercase-log_triage-abcd.md", ".Crashed-log_triage-beef.md.01.tmp"):
        (folder / other).write_bytes(b"other")
    old = (store.clock.now - timedelta(seconds=GRACE + 1)).timestamp()
    for path in folder.iterdir():  # Old enough to go, so only the name and type rules decide.
        os.utime(path, (old, old), follow_symlinks=False)
    expected = snapshot(store.home, target.parent)
    for path in residue:
        del expected[path]
    assert sweep(store).get("rowless") == 2
    assert snapshot(store.home, target.parent) == expected
    assert row(kept)["state"] == "pending"


@pytest.mark.parametrize("startup", [True, False])
@pytest.mark.parametrize("offset,removed", [(-1, True), (0, False), (1, False)])
def test_rowless_files_go_only_once_older_than_the_launch_grace(store, startup, offset, removed):
    """Defense in depth, startup included: a registry that doesn't match this store,
    such as a test's replacement database, can't make fresh live files look rowless."""
    make(store)
    folder = store.home / "ephemeral" / "live"
    residue = [folder / ("Crashed-log_triage-beef" + suffix) for suffix in (".md", ".spec.json")]
    for path in residue:
        path.write_bytes(b"residue")
        age(path, store.clock.now - timedelta(seconds=GRACE - offset))
    assert sweep(store, startup).get("rowless", 0) == (2 if removed else 0)
    assert [path.exists() for path in residue] == [not removed] * 2


def test_unusable_grace_skips_the_rowless_step(store, caplog):
    make(store)
    residue = store.home / "ephemeral" / "live" / "Crashed-log_triage-beef.md"
    residue.write_bytes(b"residue")
    age(residue, NOW - timedelta(days=1))
    store.config["launch_grace_seconds"] = "600"
    assert sweep(store, startup=True).get("rowless") is None and residue.is_file()
    assert "ephemeral.launch_grace_seconds" in caplog.text


@pytest.mark.parametrize("startup", [True, False])
def test_no_sweep_deletes_the_files_of_a_create_in_progress(file_store, monkeypatch, startup):
    s = file_store
    future, resume, pool = paused_create(s, monkeypatch)
    folder = s.home / "ephemeral" / "live"
    try:
        written = sorted(path.name for path in folder.iterdir())
        assert len(written) == 2 and rows() == 0  # Both live files, and no row yet.
        for path in folder.iterdir():  # Past the age guard, so only the lock protects them.
            age(path, s.clock.now - timedelta(seconds=GRACE + 1))
        assert sweep(s, startup) == {}
        assert sorted(path.name for path in folder.iterdir()) == written
    finally:
        resume.set()
        name = future.result(timeout=10)
        pool.shutdown()
    assert row(name)["state"] == "pending"
    assert sweep(s, startup) == {}
    assert all(path.is_file() for path in live(s, name))


# The live-name cap


@pytest.mark.parametrize("limit", [1, 3])
def test_cap_permits_n_names_and_refuses_the_next_before_writing(store, limit):
    store.config["max_live_per_session"] = limit
    for _ in range(limit):
        make(store)
    before = snapshot(store.home)
    with pytest.raises(store.service.EphemeralPolicyError) as refused:
        make(store)
    assert refused.value.rule == "max_live_exceeded" and refused.value.status_code == 409
    assert snapshot(store.home) == before and rows() == limit


def test_cap_counts_pending_claimed_and_launched_names_but_not_gc(store):
    store.config["max_live_per_session"] = 3
    pending, claimed, launched = make(store), make(store), make(store)
    claim(store, claimed)
    launch(store, launched)
    assert outcome(store) == "max_live_exceeded"
    store.service.release(CHILD, "terminal_deleted")
    assert row(launched)["state"] == "gc"
    assert row(make(store))["state"] == "pending"
    assert {row(pending)["state"], row(claimed)["state"]} == {"pending", "claimed"}


def test_cap_does_not_count_an_expired_pending_name(store):
    """Provisional: an expired pending name, which nothing can launch, is not live."""
    store.config["max_live_per_session"] = 1
    make(store)
    assert outcome(store) == "max_live_exceeded"
    store.clock.at(seconds=TTL)
    assert row(make(store))["state"] == "pending"


def test_cap_is_per_session(store):
    store.config["max_live_per_session"] = 1
    make(store)
    tools = ["@cao-mcp-server", "fs_read", "fs_list"]
    database.create_terminal(OTHER, "other-session", "w", "claude_code", allowed_tools=tools)
    assert outcome(store) == "max_live_exceeded"
    assert row(make(store, caller=OTHER))["session_name"] == "other-session"


@pytest.mark.parametrize("configured", [False, True])
def test_the_cap_is_off_by_default_and_zero_disables_it(store, monkeypatch, configured):
    """Provisional (#1079): the cap is an operator opt-in, so current users see no change."""
    if configured:
        store.config["max_live_per_session"] = 0
    counted: list = []
    real = store.service._live_names
    monkeypatch.setattr(store.service, "_live_names", lambda *a: counted.append(a) or real(*a))
    names = {make(store) for _ in range(12)}  # More than the cap proposed in #1079.
    assert len(names) == 12 and rows() == 12 and counted == []


@pytest.mark.parametrize("value", [-1, "10", True, None, 1.5])
def test_unusable_cap_is_a_policy_config_error(store, value):
    store.config["max_live_per_session"] = value
    assert outcome(store) == "policy_config_error:max_live_per_session"
    assert not (store.home / "ephemeral").exists() and rows() == 0


def test_a_store_lock_timeout_refuses_with_store_busy(store, monkeypatch):
    real = store.service._store_lock
    monkeypatch.setattr(store.service, "_store_lock", lambda timeout=10.0: real(min(timeout, 0.2)))
    held, release = threading.Event(), threading.Event()

    def holder():
        with real(timeout=0):
            held.set()
            assert release.wait(timeout=10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        holding = pool.submit(holder)
        assert held.wait(timeout=10)
        try:
            with pytest.raises(store.service.EphemeralPolicyError) as refused:
                make(store)
        finally:
            release.set()
        holding.result(timeout=10)
    assert refused.value.rule == "store_busy" and refused.value.status_code == 503
    assert rows() == 0 and not (store.home / "ephemeral").exists()
    assert row(make(store))["state"] == "pending"  # A retry once the lock is free succeeds.


def test_a_home_without_flock_refuses_creates_with_store_lock_unavailable(store, monkeypatch):
    monkeypatch.setattr(atomic_file, "_FCNTL_AVAILABLE", False)
    with pytest.raises(store.service.EphemeralPolicyError) as refused:
        make(store)
    assert refused.value.rule == "store_lock_unavailable" and refused.value.status_code == 503
    assert rows() == 0 and not (store.home / "ephemeral").exists()


def test_a_create_waiting_on_the_lock_sees_the_cap(file_store, monkeypatch):
    s = file_store
    s.config["max_live_per_session"] = 2
    make(s)
    future, resume, pool = paused_create(s, monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as other:
        second = other.submit(outcome, s, purpose="code_review")
        try:
            with pytest.raises(concurrent.futures.TimeoutError):
                second.result(timeout=0.5)  # Waiting on the create window.
        finally:
            resume.set()
        assert row(future.result(timeout=10))["state"] == "pending"
        assert second.result(timeout=10) == "max_live_exceeded"
    pool.shutdown()
    assert rows() == 2


def test_concurrent_creates_cannot_exceed_the_cap(file_store):
    s = file_store
    s.config["max_live_per_session"] = 3
    start = threading.Barrier(8)

    def run(index):
        start.wait(timeout=10)
        return outcome(s, purpose=f"race_{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, range(8)))
    assert results.count("max_live_exceeded") == 5
    assert rows() == 3


# Retention


def collected(s, purpose="log_triage"):
    """A name collected by expiry at NOW + TTL, with its archive kept."""
    s.clock.at(seconds=0)
    name = make(s, purpose=purpose)
    s.clock.at(seconds=TTL)
    sweep(s)
    assert row(name)["state"] == "gc"
    return name


@pytest.mark.parametrize("offset,kept", [(-1, False), (0, True), (1, True)])
def test_gc_archive_and_row_are_pruned_only_after_the_window(store, offset, kept):
    name = collected(store)
    path = archive(name)
    store.clock.at(days=15)
    age(path, store.clock.now - RETENTION + timedelta(seconds=offset))
    result = sweep(store)
    if kept:
        assert result == {} and path.is_file() and row(name) is not None
    else:
        assert result.get("archives") == 1 and result.get("pruned") == 1
        assert not path.exists() and row(name) is None


def test_a_row_whose_archive_is_already_gone_waits_for_the_window(store):
    name = collected(store)
    archive(name).unlink()
    store.clock.at(days=14)  # created_at has aged out, expires_at has not.
    assert sweep(store) == {} and row(name) is not None
    store.clock.at(days=14, seconds=TTL)
    assert sweep(store).get("pruned") == 1 and row(name) is None


def test_retention_never_removes_pending_claimed_or_live_names(store):
    store.config["pending_ttl_seconds"] = LONG
    store.config["claim_lease_seconds"] = LONG
    pending, claimed, running, surviving = (
        make(store, purpose=p) for p in ("aaa", "bbb", "ccc", "ddd")
    )
    claim(store, claimed)
    launch(store, running)
    launch(store, surviving, terminal="dddddddd")
    store.service.release("dddddddd", "terminal_deleted")  # gc, but its terminal survives
    names = (pending, claimed, running, surviving)
    for name in names:
        age(archive(name), NOW - timedelta(days=365))
    before = {name: (row(name), archive(name).read_bytes()) for name in names}
    store.clock.at(days=60)
    assert sweep(store) == {}
    assert {name: (row(name), archive(name).read_bytes()) for name in names} == before
    assert row(surviving)["state"] == "gc"


def test_a_gc_row_keeps_its_archive_while_a_live_entry_remains(store):
    name = collected(store)
    (store.home / "ephemeral" / "live" / (name + ".md")).mkdir()  # No collection removes it.
    age(archive(name), NOW - timedelta(days=365))
    store.clock.at(days=60)
    assert sweep(store) == {}
    assert archive(name).is_file() and row(name) is not None


def test_an_archive_replaced_by_a_link_is_kept_with_its_target(store, tmp_path):
    name = collected(store)
    path = archive(name)
    target = tmp_path / "elsewhere.json"
    target.write_bytes(b"{}")
    age(target, NOW - timedelta(days=365))
    path.unlink()
    path.symlink_to(target)
    store.clock.at(days=60)
    assert sweep(store) == {}
    assert path.is_symlink() and target.read_bytes() == b"{}" and row(name) is not None


def repoint(name, **values):
    with database.SessionLocal() as db:
        db.query(database.EphemeralAgentModel).filter_by(name=name).update(values)
        db.commit()


@pytest.mark.parametrize("variant", ["linked_session_dir", "foreign_audit_path", "dotdot_session"])
def test_retention_keeps_what_it_cannot_prove_is_this_stores(store, tmp_path, variant):
    """Defense in depth: create validates all three, so none arises in normal use."""
    name = collected(store)
    path = archive(name)
    audit = store.home / "ephemeral" / "audit"
    if variant == "linked_session_dir":
        # The session dir is a link out of the store; the archive behind it isn't ours.
        outside = tmp_path / "outside-session"
        (audit / "cao-session").rename(outside)
        (audit / "cao-session").symlink_to(outside, target_is_directory=True)
        victim = outside / path.name
    elif variant == "foreign_audit_path":
        # Provisional (#1079): a row recorded under another home, such as a moved one.
        other = tmp_path / "old-home" / "ephemeral" / "audit" / "cao-session" / path.name
        repoint(name, audit_path=str(other))
        victim = path
    else:
        # A session of ".." would make the store root the archive folder.
        victim = store.home / "ephemeral" / path.name
        victim.write_bytes(b"{}")
        repoint(name, session_name="..", audit_path=str(audit / ".." / path.name))
    age(victim, NOW - timedelta(days=365))
    before = victim.read_bytes()
    store.clock.at(days=60)
    assert sweep(store) == {}
    assert victim.read_bytes() == before and row(name) is not None


def test_a_refusal_write_racing_retention_leaves_no_orphan_archive(file_store, monkeypatch):
    """A claim refused for a collected name rewrites its archive while retention prunes."""
    s = file_store
    name = collected(s)
    path = archive(name)
    age(path, NOW - timedelta(days=365))
    s.clock.at(days=60)
    paused, resume = threading.Event(), threading.Event()
    original = s.service._write_exclusive

    def write(target, payload, owned):
        original(target, payload, owned)
        if target.parent == path.parent and target.name.startswith("." + path.name + "."):
            paused.set()
            assert resume.wait(timeout=10)

    def refused_claim():
        try:
            claim(s, name)
        except s.service.EphemeralPolicyError as exc:
            return exc.rule

    monkeypatch.setattr(s.service, "_write_exclusive", write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        refusing = pool.submit(refused_claim)
        assert paused.wait(timeout=10)  # Inside the refusal write, before its replace.
        try:
            sweeping = pool.submit(sweep, s)
            concurrent.futures.wait([sweeping], timeout=1)
        finally:
            resume.set()
        assert refusing.result(timeout=10) == "ephemeral_expired"
        sweeping.result(timeout=10)
    assert path.exists() == (row(name) is not None)
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]


@pytest.mark.parametrize("value", [0, -3, "14", None])
def test_unusable_retention_prunes_nothing(store, value, caplog):
    name = collected(store)
    store.config["retention_days"] = value
    age(archive(name), NOW - timedelta(days=365))
    store.clock.at(days=60)
    assert sweep(store) == {}
    assert archive(name).is_file() and row(name) is not None
    assert "ephemeral.retention_days" in caplog.text


def test_a_pruned_name_is_reusable_and_exact_paths_still_guard_reuse(store, monkeypatch):
    suffixes = iter(["aaaa", "aaaa", "aaaa", "bbbb", "cccc", "dddd"])
    counter = itertools.count(1)
    monkeypatch.setattr(
        store.service.secrets,
        "token_hex",
        lambda n: next(suffixes) if n == 2 else f"{next(counter):0{n * 2}x}",
    )
    name = collected(store)
    path = archive(name)
    age(path, NOW - timedelta(days=365))
    store.clock.at(days=60)
    assert sweep(store).get("pruned") == 1
    assert make(store) == name and row(name)["state"] == "pending"
    reused = archive(name)
    assert reused == path and events(reused) == ["created"]
    # Remove only the row: its archive then blocks the exact name, so create picks another.
    with database.SessionLocal() as db:
        db.query(database.EphemeralAgentModel).filter_by(name=name).delete()
        db.commit()
    for live_file in live(store, name):
        live_file.unlink()
    assert make(store).endswith("-bbbb")
    folder = store.home / "ephemeral" / "live"
    (folder / (name.replace("aaaa", "cccc") + ".spec.json")).write_bytes(b"leftover")
    assert make(store).endswith("-dddd")


# Scope of every sweep


def test_sweeps_touch_only_live_files_and_eligible_archives(store, tmp_path):
    s = store
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_bytes(b"keep")
    old, stuck = collected(s, "old_gc"), collected(s, "stuck_gc")
    s.config["pending_ttl_seconds"] = LONG
    s.config["claim_lease_seconds"] = LONG
    s.clock.at(days=20)
    tools = ["@cao-mcp-server", "fs_read", "fs_list"]
    database.create_terminal(OTHER, "other-session", "w", "claude_code", allowed_tools=tools)
    orphan = make(s, purpose="orphan", caller=OTHER)
    database.delete_terminal(OTHER)
    pending, claimed, running = (make(s, purpose=p) for p in ("pending", "claimed", "running"))
    claim(s, claimed)
    launch(s, running)
    folder = s.home / "ephemeral" / "live"
    for path in live(s, stuck):
        path.write_bytes(b"survived a failed deletion")
    residue = [folder / ("Crashed-log_triage-beef" + suffix) for suffix in (".md", ".spec.json")]
    for path in residue:
        path.write_bytes(b"residue")
        age(path, NOW)
    (folder / "notes.md").write_bytes(b"decoy")
    age(folder / "notes.md", NOW)
    (folder / "Linked-log_triage-cafe.md").symlink_to(outside / "keep.txt")
    os.utime(folder / "Linked-log_triage-cafe.md", (NOW.timestamp(),) * 2, follow_symlinks=False)
    audit = s.home / "ephemeral" / "audit" / "cao-session"
    (audit / "foreign.json").write_bytes(b"{}")
    (audit / "Crashed-log_triage-beef.json").write_bytes(b"{}")  # A rowless archive stays.
    for name in (pending, claimed, running):
        age(archive(name), NOW - timedelta(days=365))
    age(archive(old), NOW)
    age(archive(stuck), s.clock.now)
    old_archive, orphan_archive = archive(old), archive(orphan)
    orphan_events = events(orphan_archive)
    expected = snapshot(s.home, outside)
    for path in (*residue, *live(s, orphan), *live(s, stuck), old_archive, orphan_archive):
        del expected[path]
    s.clock.at(days=20, seconds=1)
    result = sweep(s)
    assert result.get("owner_gone") == 1 and result.get("retried") == 1
    assert result.get("rowless") == 2 and result.get("archives") == 1
    assert result.get("pruned") == 1 and row(old) is None
    assert events(orphan_archive)[: len(orphan_events)] == orphan_events
    assert events(orphan_archive)[-1] == "released"
    actual = snapshot(s.home, outside)
    orphan_entry = actual.pop(orphan_archive)
    assert actual == expected
    # Later passes, startup or periodic, find nothing more to do and change nothing.
    assert sweep(s, startup=True) == {} and sweep(s) == {}
    assert snapshot(s.home, outside) == {**expected, orphan_archive: orphan_entry}


# The periodic interval


@pytest.mark.parametrize(
    "value,expected",
    [
        (45, 45),
        (10**9, 86400),
        (0, EPHEMERAL_DEFAULTS["sweep_interval_seconds"]),
        ("60", EPHEMERAL_DEFAULTS["sweep_interval_seconds"]),
        (True, EPHEMERAL_DEFAULTS["sweep_interval_seconds"]),
    ],
)
def test_sweep_interval_reads_settings_with_a_safe_fallback(store, value, expected):
    store.config["sweep_interval_seconds"] = value
    assert store.service.sweep_interval_seconds() == expected


def test_sweep_interval_keeps_the_default_when_settings_are_unreadable(store, monkeypatch):
    def unreadable():
        raise SettingsUnreadableError(Path("settings.json"), ValueError("bad json"))

    monkeypatch.setattr(settings_service, "_load_or_raise", unreadable)
    expected = EPHEMERAL_DEFAULTS["sweep_interval_seconds"]
    assert store.service.sweep_interval_seconds() == expected


def test_sweep_interval_keeps_the_default_for_errors_the_loader_does_not_wrap():
    """Deeply nested JSON raises RecursionError; the daemon's interval read must not."""
    from cli_agent_orchestrator.services import ephemeral_service

    settings_service.SETTINGS_FILE.write_text("[" * 100_000 + "]" * 100_000)
    expected = EPHEMERAL_DEFAULTS["sweep_interval_seconds"]
    assert ephemeral_service.sweep_interval_seconds() == expected
