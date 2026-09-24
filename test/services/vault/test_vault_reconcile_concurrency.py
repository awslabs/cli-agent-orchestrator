"""Concurrency contracts for whole-operation vault projection serialization."""

from __future__ import annotations

import asyncio
import importlib
import multiprocessing
import os
import threading
import time
from contextlib import contextmanager
from test.fixtures.vault_factory import build_vault_fixture
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    MemoryMetadataModel,
    MemoryRelationshipModel,
    VaultExclusionModel,
    VaultNoteModel,
)
from cli_agent_orchestrator.services import memory_service, settings_service
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.vault import migrate as migrate_module
from cli_agent_orchestrator.services.vault import reconcile as reconcile_module
from cli_agent_orchestrator.services.vault import writer as writer_module
from cli_agent_orchestrator.services.vault.binding import VaultBinding
from cli_agent_orchestrator.services.vault.config import VaultConfig


def _vault_service(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'state.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(reconcile_module, "SessionLocal", Session)
    monkeypatch.setattr(reconcile_module, "_replace_vault_edges", lambda *_a, **_k: None)
    monkeypatch.setattr(reconcile_module, "_clear_stale_vault_edges", lambda *_a, **_k: None)
    monkeypatch.setattr(reconcile_module, "_emit_audit_events", lambda *_a, **_k: None)
    monkeypatch.setattr(memory_service, "_is_memory_enabled", lambda: True)
    fixture = build_vault_fixture(tmp_path)
    config = VaultConfig(enabled=True, vaults=[fixture.vault])
    monkeypatch.setattr(settings_service, "get_vault_config", lambda: config)
    service = MemoryService(base_dir=tmp_path / "native", db_engine=engine)
    return service, fixture, Session, engine


def _store(service: MemoryService, key: str, body: str) -> None:
    asyncio.run(
        service.store(
            content=body,
            scope="global",
            memory_type="reference",
            key=key,
        )
    )


def _global_binding(fixture) -> VaultBinding:
    mapping = next(mapping for mapping in fixture.vault.mappings if mapping.writable)
    return VaultBinding(
        scope=mapping.scope,
        scope_id=mapping.scope_id,
        vault_id=fixture.vault.id,
        root=fixture.vault.root,
        mapping=mapping,
    )


def test_projection_lock_reenters_same_thread_but_blocks_second_thread(tmp_path) -> None:
    """Removing PID/thread-bound reentrancy would deadlock or permit overlap."""
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    fixture = build_vault_fixture(tmp_path)
    attempted = threading.Event()
    acquired = threading.Event()

    def contender() -> None:
        attempted.set()
        with vault_lock.vault_projection_lock(fixture.vault, timeout=2.0):
            acquired.set()

    with vault_lock.vault_projection_lock(fixture.vault):
        with vault_lock.vault_projection_lock(fixture.vault):
            thread = threading.Thread(target=contender)
            thread.start()
            assert attempted.wait(1)
            time.sleep(0.1)
            assert not acquired.is_set()

    thread.join(2)
    assert not thread.is_alive()
    assert acquired.is_set()


def test_projection_lock_uses_canonical_resolved_root(tmp_path) -> None:
    """Keying on a lexical alias would permit overlap on the same vault."""
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    fixture = build_vault_fixture(tmp_path)
    alias = tmp_path / "vault-alias"
    alias.symlink_to(fixture.root, target_is_directory=True)
    alias_vault = fixture.vault.model_copy(update={"root": str(alias)})
    acquired = threading.Event()

    def contend() -> None:
        with vault_lock.vault_projection_lock(alias_vault, timeout=2.0):
            acquired.set()

    with vault_lock.vault_projection_lock(fixture.vault):
        thread = threading.Thread(target=contend)
        thread.start()
        time.sleep(0.1)
        assert not acquired.is_set()

    thread.join(2)
    assert acquired.is_set()


def _case_variant_vault(fixture, tmp_path):
    probe = tmp_path / "CaseProbe"
    probe.mkdir()
    if not (tmp_path / "caseprobe").exists():
        pytest.skip("case-variant vault-root test requires a case-insensitive filesystem")
    alias = fixture.root.with_name(fixture.root.name.swapcase())
    assert alias != fixture.root
    assert alias.exists()
    return fixture.vault.model_copy(update={"root": str(alias)})


def test_projection_lock_serializes_case_variant_root_spellings(tmp_path) -> None:
    """One live directory must not acquire two locks through casing aliases."""
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    fixture = build_vault_fixture(tmp_path)
    alias_vault = _case_variant_vault(fixture, tmp_path)
    attempted = threading.Event()
    acquired = threading.Event()

    def contend() -> None:
        attempted.set()
        with vault_lock.vault_projection_lock(alias_vault, timeout=2.0):
            acquired.set()

    with vault_lock.vault_projection_lock(fixture.vault):
        thread = threading.Thread(target=contend)
        thread.start()
        assert attempted.wait(1)
        time.sleep(0.1)
        assert not acquired.is_set()

    thread.join(2)
    assert not thread.is_alive()
    assert acquired.is_set()


def test_projection_lock_reenters_across_case_variant_spellings(tmp_path) -> None:
    """A casing alias must hit the same thread-local depth record and fd."""
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    fixture = build_vault_fixture(tmp_path)
    alias_vault = _case_variant_vault(fixture, tmp_path)

    with vault_lock.vault_projection_lock(fixture.vault):
        outer_fds = set(vault_lock._open_fds)
        assert len(outer_fds) == 1
        with vault_lock.vault_projection_lock(alias_vault, timeout=0.15):
            assert vault_lock.holds_projection_lock(alias_vault)
            assert vault_lock._open_fds == outer_fds


def test_projection_lock_keys_distinct_roots_separately(tmp_path) -> None:
    """Inode identity must not false-share two genuinely distinct roots."""
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    fixture = build_vault_fixture(tmp_path)
    other_root = tmp_path / "other-vault"
    other_root.mkdir()
    other_vault = fixture.vault.model_copy(update={"root": str(other_root)})
    acquired = threading.Event()

    def contend() -> None:
        with vault_lock.vault_projection_lock(other_vault, timeout=0.5):
            acquired.set()

    with vault_lock.vault_projection_lock(fixture.vault):
        thread = threading.Thread(target=contend)
        thread.start()
        assert acquired.wait(0.3)

    thread.join(1)
    assert not thread.is_alive()


def test_projection_lock_falls_back_when_root_is_absent(tmp_path) -> None:
    """An absent root retains stable resolved-path serialization."""
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    fixture = build_vault_fixture(tmp_path)
    missing = tmp_path / "missing-vault"
    missing_vault = fixture.vault.model_copy(update={"root": str(missing)})
    alias_vault = fixture.vault.model_copy(update={"root": str(tmp_path / "." / "missing-vault")})
    acquired = threading.Event()

    def contend() -> None:
        with vault_lock.vault_projection_lock(alias_vault, timeout=0.5):
            acquired.set()

    with vault_lock.vault_projection_lock(missing_vault):
        thread = threading.Thread(target=contend)
        thread.start()
        time.sleep(0.1)
        assert not acquired.is_set()

    thread.join(1)
    assert acquired.is_set()


@pytest.mark.parametrize("stat_result", [OSError("stat failed"), SimpleNamespace(st_ino=0)])
def test_projection_identity_uses_path_fallback_for_unstable_inode(
    tmp_path, monkeypatch, stat_result
) -> None:
    """Stat failure and inode zero must retain the resolved-path identity floor."""
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    root = tmp_path / "fallback-root"
    resolved = str(root.resolve(strict=False))

    real_os = os

    class StatOS:
        def __getattr__(self, name):
            return getattr(real_os, name)

        def stat(self, _path):
            if isinstance(stat_result, BaseException):
                raise stat_result
            return stat_result

    monkeypatch.setattr(vault_lock, "os", StatOS())

    assert vault_lock._projection_identity(str(root)) == f"path:{resolved}"


def test_vault_store_does_not_block_the_event_loop(tmp_path, monkeypatch) -> None:
    """A contended interactive store must leave the loop heartbeat runnable."""
    service, fixture, _Session, _engine = _vault_service(tmp_path, monkeypatch)
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    real_lock = vault_lock.vault_projection_lock
    held = threading.Event()
    release = threading.Event()

    @contextmanager
    def bounded_lock(vault, *, timeout):
        assert timeout == vault_lock.VAULT_PROJECTION_LOCK_INTERACTIVE_TIMEOUT_S
        with real_lock(vault, timeout=min(timeout, 0.15)):
            yield

    monkeypatch.setattr(vault_lock, "vault_projection_lock", bounded_lock)
    monkeypatch.setattr(vault_lock, "VAULT_PROJECTION_LOCK_INTERACTIVE_TIMEOUT_S", 0.15)

    def holder() -> None:
        with real_lock(fixture.vault):
            held.set()
            assert release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    assert held.wait(1)

    async def scenario() -> int:
        ticks = 0
        stopped = asyncio.Event()

        async def heartbeat() -> None:
            nonlocal ticks
            while not stopped.is_set():
                ticks += 1
                await asyncio.sleep(0.005)

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            with pytest.raises(vault_lock.VaultProjectionBusyError) as raised:
                await service.store(
                    content="heartbeat-body",
                    scope="global",
                    memory_type="reference",
                    key="heartbeat-store",
                )
            assert raised.value.timeout == 0.15
        finally:
            stopped.set()
            await heartbeat_task
        return ticks

    try:
        assert asyncio.run(scenario()) >= 5
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()


def test_interactive_projection_lock_budget_is_ten_seconds() -> None:
    """Interactive callers retain the former note-lock contention budget."""
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")

    assert vault_lock.VAULT_PROJECTION_LOCK_INTERACTIVE_TIMEOUT_S == 10.0
    assert vault_lock.VAULT_PROJECTION_LOCK_TIMEOUT_S == 120.0


def test_both_interactive_projection_calls_supply_ten_second_timeout(tmp_path, monkeypatch) -> None:
    """Dropping either explicit timeout must fail at the required-keyword wrapper."""
    service, _fixture, _Session, _engine = _vault_service(tmp_path, monkeypatch)
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    real_lock = vault_lock.vault_projection_lock
    observed: list[float] = []

    @contextmanager
    def require_interactive_timeout(vault, *, timeout):
        assert timeout == vault_lock.VAULT_PROJECTION_LOCK_INTERACTIVE_TIMEOUT_S
        observed.append(timeout)
        with real_lock(vault, timeout=timeout):
            yield

    monkeypatch.setattr(vault_lock, "vault_projection_lock", require_interactive_timeout)

    _store(service, "timeout-wiring", "body")
    forgotten = asyncio.run(service.forget("timeout-wiring", scope="global"))

    assert forgotten.action == "deindexed"
    assert observed == [10.0, 10.0]


def test_vault_forget_offload_survives_awaiter_cancellation(tmp_path, monkeypatch, caplog) -> None:
    """Cancellation leaves an unknown outcome while the worker completes atomically."""
    service, fixture, Session, _engine = _vault_service(tmp_path, monkeypatch)
    _store(service, "cancel-forget", "cancel-body-sensitive")
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    real_native_forget = service._forget_native_memory

    def delayed_native_forget(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        try:
            return real_native_forget(*args, **kwargs)
        finally:
            completed.set()

    monkeypatch.setattr(service, "_forget_native_memory", delayed_native_forget)

    async def scenario() -> None:
        task = asyncio.create_task(service.forget("cancel-forget", scope="global"))
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, task.cancel)
        timer = threading.Timer(0.2, release.set)
        timer.start()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            timer.join(1)

    with caplog.at_level("WARNING"):
        asyncio.run(scenario())

    assert entered.is_set()
    assert completed.wait(1)
    assert "vault_projection_offload_cancelled" in caplog.text
    assert "cancel-body-sensitive" not in caplog.text
    with Session() as db:
        note = db.query(VaultNoteModel).filter_by(cao_key="cancel-forget").one()
        assert note.status == "excluded"
        assert db.query(MemoryMetadataModel).filter_by(key="cancel-forget").count() == 0
    with vault_lock.vault_projection_lock(fixture.vault, timeout=0.15):
        pass
    assert vault_lock._open_fds == set()


def test_offload_runs_inline_when_the_thread_already_holds_the_lock(tmp_path, monkeypatch) -> None:
    """A nested asyncio.run on the owner thread must not self-deadlock."""
    service, fixture, _Session, _engine = _vault_service(tmp_path, monkeypatch)
    caller_thread = threading.get_ident()
    worker_threads: list[int] = []
    real_store = service._store_vault_memory

    def traced_store(**kwargs):
        worker_threads.append(threading.get_ident())
        return real_store(**kwargs)

    monkeypatch.setattr(service, "_store_vault_memory", traced_store)
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")

    with vault_lock.vault_projection_lock(fixture.vault):
        memory = asyncio.run(
            service.store(
                content="inline-body",
                scope="global",
                memory_type="reference",
                key="inline-store",
            )
        )

    assert memory.action == "created"
    assert worker_threads == [caller_thread]


def test_vault_projection_lock_is_never_acquired_on_a_loop_thread(tmp_path, monkeypatch) -> None:
    """Every public vault store/forget depth-zero acquisition is off-loop."""
    service, _fixture, _Session, _engine = _vault_service(tmp_path, monkeypatch)
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    real_lock = vault_lock.vault_projection_lock
    depth_zero_entries = 0

    @contextmanager
    def reject_loop_acquisition(vault, **kwargs):
        nonlocal depth_zero_entries
        if not vault_lock.holds_projection_lock(vault):
            depth_zero_entries += 1
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise AssertionError("depth-zero vault lock acquired on event-loop thread")
        with real_lock(vault, **kwargs):
            yield

    monkeypatch.setattr(vault_lock, "vault_projection_lock", reject_loop_acquisition)
    monkeypatch.setattr(writer_module, "vault_projection_lock", reject_loop_acquisition)
    monkeypatch.setattr(reconcile_module, "vault_projection_lock", reject_loop_acquisition)
    monkeypatch.setattr(migrate_module, "vault_projection_lock", reject_loop_acquisition)

    memory = asyncio.run(
        service.store(
            content="off-loop-body",
            scope="global",
            memory_type="reference",
            key="off-loop-store",
        )
    )
    forgotten = asyncio.run(service.forget("off-loop-store", scope="global"))

    assert memory.action == "created"
    assert forgotten.action == "deindexed"
    assert depth_zero_entries == 2


def test_distinct_process_stores_do_not_retract_projection(tmp_path, monkeypatch) -> None:
    """Removing the outer store lock lets an old scan delete a newer projection."""
    _service, fixture, Session, engine = _vault_service(tmp_path, monkeypatch)
    context = multiprocessing.get_context("fork")
    first_scanned = context.Event()
    release_first = context.Event()
    second_finished = context.Event()
    errors = context.Queue()
    real_scan = reconcile_module.scan_vault

    def synchronized_scan(vault):
        result = real_scan(vault)
        if multiprocessing.current_process().name == "vault-store-a":
            first_scanned.set()
            if not release_first.wait(10):
                raise TimeoutError("first store scan was not released")
        return result

    monkeypatch.setattr(reconcile_module, "scan_vault", synchronized_scan)
    engine.dispose()

    def store_a() -> None:
        try:
            _store(MemoryService(base_dir=tmp_path / "native", db_engine=engine), "topic-a", "A")
        except BaseException as exc:
            errors.put(("a", repr(exc)))

    def store_b() -> None:
        try:
            if not first_scanned.wait(10):
                raise TimeoutError("first store never scanned")
            _store(MemoryService(base_dir=tmp_path / "native", db_engine=engine), "topic-b", "B")
        except BaseException as exc:
            errors.put(("b", repr(exc)))
        finally:
            second_finished.set()

    first = context.Process(target=store_a, name="vault-store-a")
    second = context.Process(target=store_b, name="vault-store-b")
    first.start()
    assert first_scanned.wait(10)
    second.start()
    second_finished.wait(0.5)
    release_first.set()
    first.join(15)
    second.join(15)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert errors.empty()
    with Session() as db:
        assert {row.cao_key for row in db.query(VaultNoteModel).all()} >= {
            "topic-a",
            "topic-b",
        }
        assert {
            row.key for row in db.query(MemoryMetadataModel).filter_by(source_kind="vault").all()
        } >= {"topic-a", "topic-b"}


def test_rebuild_concurrent_with_store_preserves_store(tmp_path, monkeypatch) -> None:
    """Removing rebuild serialization lets its stale scan retract a store."""
    service, fixture, Session, _engine = _vault_service(tmp_path, monkeypatch)
    _store(service, "before", "before")
    first_scanned = threading.Event()
    release_first = threading.Event()
    store_finished = threading.Event()
    real_scan = reconcile_module.scan_vault

    def synchronized_scan(vault):
        result = real_scan(vault)
        if threading.current_thread().name == "rebuild":
            first_scanned.set()
            assert release_first.wait(10)
        return result

    monkeypatch.setattr(reconcile_module, "scan_vault", synchronized_scan)
    errors: list[BaseException] = []

    def rebuild() -> None:
        try:
            reconcile_module.rebuild(fixture.vault)
        except BaseException as exc:
            errors.append(exc)

    def store() -> None:
        try:
            assert first_scanned.wait(10)
            _store(service, "during-rebuild", "newer")
        except BaseException as exc:
            errors.append(exc)
        finally:
            store_finished.set()

    first = threading.Thread(target=rebuild, name="rebuild")
    second = threading.Thread(target=store, name="store")
    first.start()
    assert first_scanned.wait(10)
    second.start()
    store_finished.wait(0.5)
    release_first.set()
    first.join(15)
    second.join(15)

    assert not errors
    with Session() as db:
        assert db.query(VaultNoteModel).filter_by(cao_key="during-rebuild").count() == 1


def test_same_key_store_snapshot_is_taken_under_outer_lock(tmp_path, monkeypatch) -> None:
    """Removing the store's outer lock makes one writer use a stale implicit CAS."""
    service, _fixture, _Session, _engine = _vault_service(tmp_path, monkeypatch)
    _store(service, "same-key", "original")
    first_entered = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()
    real_write = writer_module.write_managed_note

    def synchronized_write(**kwargs):
        entry = kwargs.get("entry")
        payload = entry.content if entry is not None else kwargs["body"]
        if payload == "first":
            first_entered.set()
            assert release_first.wait(10)
        return real_write(**kwargs)

    monkeypatch.setattr(writer_module, "write_managed_note", synchronized_write)
    errors: list[BaseException] = []

    def store(body: str) -> None:
        try:
            _store(service, "same-key", body)
        except BaseException as exc:
            errors.append(exc)
        finally:
            if threading.current_thread().name == "second":
                second_finished.set()

    first = threading.Thread(target=store, args=("first",), name="first")
    second = threading.Thread(target=store, args=("second",), name="second")
    first.start()
    assert first_entered.wait(10)
    second.start()
    second_finished.wait(0.5)
    release_first.set()
    first.join(15)
    second.join(15)

    assert not errors


def test_forget_racing_reconcile_does_not_resurrect_note(tmp_path, monkeypatch) -> None:
    """Removing forget serialization lets a stale decision undo exclusion."""
    service, fixture, Session, _engine = _vault_service(tmp_path, monkeypatch)
    _store(service, "forgotten", "body")
    plan_ready = threading.Event()
    release_plan = threading.Event()
    forget_finished = threading.Event()
    real_exclusions = reconcile_module._vault_exclusion_set

    def stale_exclusions(db, vault_id):
        if threading.current_thread().name == "reconcile":
            plan_ready.set()
            assert release_plan.wait(10)
            return set()
        return real_exclusions(db, vault_id)

    monkeypatch.setattr(reconcile_module, "_vault_exclusion_set", stale_exclusions)
    errors: list[BaseException] = []

    def reconcile() -> None:
        try:
            reconcile_module.reconcile(fixture.vault, apply=True)
        except BaseException as exc:
            errors.append(exc)

    def forget() -> None:
        try:
            assert plan_ready.wait(10)
            asyncio.run(service.forget("forgotten", scope="global"))
        except BaseException as exc:
            errors.append(exc)
        finally:
            forget_finished.set()

    first = threading.Thread(target=reconcile, name="reconcile")
    second = threading.Thread(target=forget, name="forget")
    first.start()
    assert plan_ready.wait(10)
    second.start()
    forget_finished.wait(0.5)
    release_plan.set()
    first.join(15)
    second.join(15)

    assert not errors
    with Session() as db:
        note = db.query(VaultNoteModel).filter_by(cao_key="forgotten").one()
        assert note.status == "excluded"
        assert db.query(VaultExclusionModel).filter_by(cao_key="forgotten").count() == 1
        assert (
            db.query(MemoryMetadataModel).filter_by(key="forgotten", source_kind="vault").count()
            == 0
        )


def test_committed_forget_drains_audit_once_after_native_cleanup_failure(
    tmp_path, monkeypatch
) -> None:
    """Dropping the exception-safe drain loses a committed purge's audit."""
    from cli_agent_orchestrator.services.memory_relationship_service import (
        MemoryRelationshipService,
    )

    service, fixture, Session, _engine = _vault_service(tmp_path, monkeypatch)
    _store(service, "audit-forget", "body")
    with Session() as db:
        db.add(
            MemoryRelationshipModel(
                id="audit-forget-edge",
                scope="global",
                scope_id="",
                source_key="audit-forget",
                target_key="other",
                type="relates_to",
                origin="vault",
                status="active",
            )
        )
        db.commit()

    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    trace: list[tuple[str, bool]] = []

    def fail_audit(*_args) -> None:
        trace.append(("audit", bool(vault_lock._records())))
        raise RuntimeError("induced audit failure")

    monkeypatch.setattr(
        MemoryRelationshipService,
        "_audit_purge",
        fail_audit,
    )
    native_failure = OSError("induced native cleanup failure")

    def fail_native_cleanup(*_args, **_kwargs):
        trace.append(("native", bool(vault_lock._records())))
        raise native_failure

    monkeypatch.setattr(service, "_forget_native_memory", fail_native_cleanup)

    with pytest.raises(OSError, match="induced native cleanup failure") as raised:
        asyncio.run(service.forget("audit-forget", scope="global"))

    assert raised.value is native_failure
    assert trace == [("native", True), ("audit", False)]
    with Session() as db:
        note = db.query(VaultNoteModel).filter_by(cao_key="audit-forget").one()
        assert note.status == "excluded"
        assert db.query(MemoryRelationshipModel).filter_by(id="audit-forget-edge").count() == 0


def test_rolled_back_forget_does_not_emit_queued_purge_audit(tmp_path, monkeypatch) -> None:
    """Draining without a commit guard would report a purge that rolled back."""
    from cli_agent_orchestrator.services.memory_relationship_service import (
        MemoryRelationshipService,
    )

    service, _fixture, Session, _engine = _vault_service(tmp_path, monkeypatch)
    _store(service, "rollback-audit", "body")
    with Session() as db:
        db.add(
            MemoryRelationshipModel(
                id="rollback-audit-edge",
                scope="global",
                scope_id="",
                source_key="rollback-audit",
                target_key="other",
                type="relates_to",
                origin="vault",
                status="active",
            )
        )
        db.commit()

    audits: list[str] = []
    monkeypatch.setattr(
        MemoryRelationshipService,
        "_audit_purge",
        lambda _self, *_args: audits.append("purge"),
    )
    real_purge = MemoryRelationshipService.purge_for_key

    def fail_after_queued_purge(self, *args, **kwargs):
        real_purge(self, *args, **kwargs)
        raise RuntimeError("induced transaction rollback")

    monkeypatch.setattr(
        MemoryRelationshipService,
        "purge_for_key",
        fail_after_queued_purge,
    )

    with pytest.raises(RuntimeError, match="induced transaction rollback"):
        asyncio.run(service.forget("rollback-audit", scope="global"))

    assert audits == []
    with Session() as db:
        note = db.query(VaultNoteModel).filter_by(cao_key="rollback-audit").one()
        assert note.status == "indexed"
        assert db.query(MemoryRelationshipModel).filter_by(id="rollback-audit-edge").count() == 1


def test_committed_forget_drains_audit_when_session_close_raises(tmp_path, monkeypatch) -> None:
    """A post-commit close failure must not skip the committed purge audit."""
    from cli_agent_orchestrator.services.memory_relationship_service import (
        MemoryRelationshipService,
    )

    service, _fixture, Session, _engine = _vault_service(tmp_path, monkeypatch)
    _store(service, "close-failure", "body")
    with Session() as db:
        db.add(
            MemoryRelationshipModel(
                id="close-failure-edge",
                scope="global",
                scope_id="",
                source_key="close-failure",
                target_key="other",
                type="relates_to",
                origin="vault",
                status="active",
            )
        )
        db.commit()

    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    session_closed = threading.Event()
    audit_trace: list[tuple[bool, bool]] = []
    monkeypatch.setattr(
        MemoryRelationshipService,
        "_audit_purge",
        lambda _self, *_args: audit_trace.append(
            (bool(vault_lock._records()), session_closed.is_set())
        ),
    )
    close_error = RuntimeError("induced session close failure")
    real_get_session = service._get_db_session

    def close_failing_session():
        db = real_get_session()
        real_close = db.close

        def close() -> None:
            real_close()
            session_closed.set()
            raise close_error

        db.close = close
        return db

    monkeypatch.setattr(service, "_get_db_session", close_failing_session)

    with pytest.raises(RuntimeError, match="induced session close failure") as raised:
        asyncio.run(service.forget("close-failure", scope="global"))

    assert raised.value is close_error
    assert audit_trace == [(False, True)]
    with Session() as db:
        note = db.query(VaultNoteModel).filter_by(cao_key="close-failure").one()
        assert note.status == "excluded"
        assert db.query(MemoryRelationshipModel).filter_by(id="close-failure-edge").count() == 0


def test_writer_refresh_reenters_projection_lock_without_deadlock(tmp_path, monkeypatch) -> None:
    """Removing the writer's outer lock permits a contender during refresh."""
    service, fixture, _Session, _engine = _vault_service(tmp_path, monkeypatch)
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    contender_acquired = threading.Event()
    contender_done = threading.Event()

    def refresh(_path: str) -> None:
        def contend() -> None:
            with vault_lock.vault_projection_lock(fixture.vault, timeout=2.0):
                contender_acquired.set()
            contender_done.set()

        thread = threading.Thread(target=contend)
        thread.start()
        time.sleep(0.1)
        assert not contender_acquired.is_set()
        reconcile_module.reconcile(fixture.vault, apply=True)

    writer_module.write_managed_note(
        vault=fixture.vault,
        binding=_global_binding(fixture),
        key="reentrant",
        body="body",
        cao={"type": "reference"},
        expected_content_sha256=None,
        refresh=refresh,
    )
    assert contender_done.wait(2)
    assert (fixture.root / "CAO" / "reentrant.md").exists()


def test_apply_plan_rejects_external_stale_plan(tmp_path, monkeypatch) -> None:
    """Removing apply authority lets a preview plan reach mutation code."""
    _service, fixture, Session, _engine = _vault_service(tmp_path, monkeypatch)
    preview = reconcile_module.plan_reconcile(fixture.vault)

    with Session() as db:
        with db.begin(), pytest.raises(AssertionError, match="authoritative"):
            reconcile_module._apply_plan(
                db,
                fixture.vault,
                preview,
                (),
                (),
                [],
                rebuild=False,
                exclusions=set(),
            )


def test_projection_lock_timeout_is_bounded_busy_error(tmp_path) -> None:
    """Replacing the distinct busy error or bounded timeout breaks observability."""
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    fixture = build_vault_fixture(tmp_path)
    started = threading.Event()
    elapsed: list[float] = []
    errors: list[BaseException] = []

    def contend() -> None:
        started.set()
        before = time.monotonic()
        try:
            with vault_lock.vault_projection_lock(fixture.vault, timeout=0.15):
                pass
        except BaseException as exc:
            errors.append(exc)
        elapsed.append(time.monotonic() - before)

    with vault_lock.vault_projection_lock(fixture.vault):
        thread = threading.Thread(target=contend)
        thread.start()
        assert started.wait(1)
        thread.join(2)

    assert len(errors) == 1
    assert isinstance(errors[0], vault_lock.VaultProjectionBusyError)
    assert errors[0].timeout == 0.15
    assert 0.1 <= elapsed[0] < 1.0


def test_projection_lock_default_timeout_is_120_seconds_without_waiting(
    tmp_path, monkeypatch
) -> None:
    """Changing the whole-vault budget from 120 seconds breaks its contract."""
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    fixture = build_vault_fixture(tmp_path)
    monotonic_values = iter((0.0, 10.0, 131.0))
    process_monotonic = time.monotonic
    fake_time = SimpleNamespace(
        monotonic=lambda: next(monotonic_values),
        sleep=lambda _seconds: None,
    )
    monkeypatch.setattr(vault_lock, "time", fake_time)
    errors: list[BaseException] = []

    def contend() -> None:
        try:
            with vault_lock.vault_projection_lock(fixture.vault):
                pass
        except BaseException as exc:
            errors.append(exc)

    with vault_lock.vault_projection_lock(fixture.vault):
        thread = threading.Thread(target=contend)
        thread.start()
        thread.join(2)

    assert len(errors) == 1
    assert isinstance(errors[0], vault_lock.VaultProjectionBusyError)
    assert errors[0].timeout == 120.0
    assert time.monotonic is process_monotonic


def test_forked_child_cannot_inherit_projection_lock_depth(tmp_path) -> None:
    """Removing at-fork cleanup lets a child bypass the parent's held lock."""
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    fixture = build_vault_fixture(tmp_path)
    context = multiprocessing.get_context("fork")
    result = context.Queue()

    def child() -> None:
        try:
            with vault_lock.vault_projection_lock(fixture.vault, timeout=0.15):
                result.put("acquired")
        except vault_lock.VaultProjectionBusyError:
            result.put("blocked")

    with vault_lock.vault_projection_lock(fixture.vault):
        process = context.Process(target=child)
        process.start()
        process.join(3)

    assert process.exitcode == 0
    assert result.get(timeout=1) == "blocked"


@pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded, use of fork\\(\\).*:DeprecationWarning"
)
def test_fork_child_closes_projection_fds_opened_by_other_threads(tmp_path) -> None:
    """Removing the global fd registry leaks another thread's lock into a child."""
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    fixture = build_vault_fixture(tmp_path)
    context = multiprocessing.get_context("fork")
    held = threading.Event()
    release = threading.Event()
    fd_from_thread: list[int] = []
    identity_from_thread: list[tuple[int, int]] = []
    result = context.Queue()

    def holder() -> None:
        with vault_lock.vault_projection_lock(fixture.vault):
            fd = next(iter(vault_lock._open_fds))
            stat_result = os.fstat(fd)
            fd_from_thread.append(fd)
            identity_from_thread.append((stat_result.st_dev, stat_result.st_ino))
            held.set()
            assert release.wait(10)

    thread = threading.Thread(target=holder)
    thread.start()
    assert held.wait(2)

    def child() -> None:
        try:
            stat_result = os.fstat(fd_from_thread[0])
        except OSError:
            result.put("closed")
        else:
            inherited_identity = (stat_result.st_dev, stat_result.st_ino)
            result.put("open" if inherited_identity == identity_from_thread[0] else "closed")

    process = context.Process(target=child)
    process.start()
    process.join(3)
    release.set()
    thread.join(3)

    assert process.exitcode == 0
    assert result.get(timeout=1) == "closed"


@pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded, use of fork\\(\\).*:DeprecationWarning"
)
def test_fork_serializes_open_and_fd_registration(tmp_path, monkeypatch) -> None:
    """Opening outside the registry lock leaks the unregistered fd to a child."""
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    fixture = build_vault_fixture(tmp_path)
    context = multiprocessing.get_context("fork")
    opened = threading.Event()
    release_open = threading.Event()
    holder_acquired = threading.Event()
    release_holder = threading.Event()
    opened_fd: list[int] = []
    opened_identity: list[tuple[int, int]] = []
    result = context.Queue()
    real_os = os

    class BlockingOpenOS:
        def __getattr__(self, name):
            return getattr(real_os, name)

        def open(self, *args, **kwargs):
            fd = real_os.open(*args, **kwargs)
            metadata = real_os.fstat(fd)
            opened_fd.append(fd)
            opened_identity.append((metadata.st_dev, metadata.st_ino))
            opened.set()
            assert release_open.wait(10)
            return fd

    monkeypatch.setattr(vault_lock, "os", BlockingOpenOS())
    holder_errors: list[BaseException] = []

    def holder() -> None:
        try:
            with vault_lock.vault_projection_lock(fixture.vault):
                holder_acquired.set()
                assert release_holder.wait(10)
        except BaseException as exc:
            holder_errors.append(exc)

    holder_thread = threading.Thread(target=holder)
    holder_thread.start()
    assert opened.wait(2)

    def child() -> None:
        try:
            metadata = real_os.fstat(opened_fd[0])
        except OSError:
            result.put("closed")
        else:
            identity = (metadata.st_dev, metadata.st_ino)
            result.put("open" if identity == opened_identity[0] else "closed")

    process = context.Process(target=child)

    def fork_child() -> None:
        process.start()
        process.join(3)

    fork_thread = threading.Thread(target=fork_child)
    fork_thread.start()
    time.sleep(0.1)
    release_open.set()
    assert holder_acquired.wait(2)
    fork_thread.join(4)
    release_holder.set()
    holder_thread.join(3)

    assert not holder_errors
    assert process.exitcode == 0
    assert result.get(timeout=1) == "closed"


@pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded, use of fork\\(\\).*:DeprecationWarning"
)
def test_fifo_lockfile_without_reader_does_not_stall_fork(tmp_path, monkeypatch) -> None:
    """O_NONBLOCK prevents FIFO open from holding the fork registry mutex."""
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    fixture = build_vault_fixture(tmp_path)
    context = multiprocessing.get_context("fork")
    result = context.Queue()
    lock_dir = tmp_path / "projection-locks"
    monkeypatch.setattr(vault_lock, "LOCK_DIR", lock_dir)
    identity = vault_lock._projection_identity(fixture.vault.root)
    lock_path = vault_lock._projection_lock_path(identity)
    assert lock_path.parent == lock_dir
    assert lock_path.resolve(strict=False).is_relative_to(tmp_path.resolve())
    assert not lock_path.exists()

    def probe() -> None:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(lock_path)
        open_finished = threading.Event()

        def attempt_open() -> None:
            try:
                with vault_lock.vault_projection_lock(fixture.vault, timeout=0.15):
                    result.put("unexpected-acquire")
            except OSError:
                result.put("open-failed")
            finally:
                open_finished.set()

        opener = threading.Thread(target=attempt_open, daemon=True)
        opener.start()
        time.sleep(0.1)
        child_pid = os.fork()
        if child_pid == 0:
            os._exit(0)
        os.waitpid(child_pid, 0)
        result.put("fork-completed")
        opener.join(1)
        if not open_finished.is_set():
            result.put("open-stalled")

    process = context.Process(target=probe)
    process.start()
    messages: list[str] = []
    try:
        process.join(2)
        if process.is_alive():
            process.terminate()
            process.join(2)
        exitcode = process.exitcode
        if exitcode == 0:
            messages = [result.get(timeout=1), result.get(timeout=1)]
    finally:
        if process.is_alive():
            process.terminate()
            process.join(2)
        lock_path.unlink(missing_ok=True)

    assert not lock_path.exists()
    assert exitcode == 0, "FIFO open held the registry mutex and stalled fork"
    assert messages == [
        "open-failed",
        "fork-completed",
    ]


def test_mismatched_pid_fallback_discards_inherited_fd(tmp_path, monkeypatch) -> None:
    """Removing the PID fallback trusts stale depth when fork hooks do not run."""
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    fixture = build_vault_fixture(tmp_path)
    inherited_fd, peer_fd = os.pipe()
    vault_lock._open_fds.add(inherited_fd)
    monkeypatch.setattr(vault_lock, "_registry_pid", os.getpid() - 1)

    try:
        with vault_lock.vault_projection_lock(fixture.vault):
            pass
        with pytest.raises(OSError):
            os.fstat(inherited_fd)
    finally:
        os.close(peer_fd)


def test_writer_fcntl_unavailable_fails_before_mutation(tmp_path, monkeypatch) -> None:
    """Removing fail-closed writer entry permits an unserialized publication."""
    _service, fixture, _Session, _engine = _vault_service(tmp_path, monkeypatch)
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    monkeypatch.setattr(vault_lock, "_FCNTL_AVAILABLE", False)

    with pytest.raises(vault_lock.VaultProjectionUnavailableError):
        writer_module.write_managed_note(
            vault=fixture.vault,
            binding=_global_binding(fixture),
            key="unavailable",
            body="body",
            cao={"type": "reference"},
            expected_content_sha256=None,
        )

    assert not (fixture.root / "CAO" / "unavailable.md").exists()


def test_store_fcntl_unavailable_fails_before_snapshot_db_session(tmp_path, monkeypatch) -> None:
    """Removing the store's outer check permits a stale DB snapshot first."""
    service, _fixture, _Session, _engine = _vault_service(tmp_path, monkeypatch)
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    monkeypatch.setattr(vault_lock, "_FCNTL_AVAILABLE", False)
    sessions: list[bool] = []
    real_session = service._get_db_session
    monkeypatch.setattr(
        service,
        "_get_db_session",
        lambda: sessions.append(True) or real_session(),
    )

    with pytest.raises(vault_lock.VaultProjectionUnavailableError):
        _store(service, "unavailable-store", "body")

    assert sessions == []


def test_actual_writer_path_never_acquires_vault_lock_under_note_flock(
    tmp_path, monkeypatch
) -> None:
    """Inverting vault/note order would deadlock and must fail this resident guard."""
    from cli_agent_orchestrator.utils import atomic_file

    service, _fixture, _Session, _engine = _vault_service(tmp_path, monkeypatch)
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    state = threading.local()
    note_entries = 0
    vault_entries = 0
    real_note_lock = atomic_file._file_lock
    real_vault_lock = vault_lock.vault_projection_lock

    def note_depth() -> int:
        return getattr(state, "note_depth", 0)

    @contextmanager
    def traced_note_lock(lock_path, timeout):
        nonlocal note_entries
        note_entries += 1
        state.note_depth = note_depth() + 1
        try:
            with real_note_lock(lock_path, timeout):
                yield
        finally:
            state.note_depth = note_depth() - 1

    @contextmanager
    def guarded_vault_lock(vault, **kwargs):
        nonlocal vault_entries
        vault_entries += 1
        assert note_depth() == 0, "vault lock acquired while note flock is held"
        with real_vault_lock(vault, **kwargs):
            yield

    monkeypatch.setattr(writer_module, "_file_lock", traced_note_lock)
    monkeypatch.setattr(vault_lock, "vault_projection_lock", guarded_vault_lock)
    monkeypatch.setattr(writer_module, "vault_projection_lock", guarded_vault_lock)
    monkeypatch.setattr(reconcile_module, "vault_projection_lock", guarded_vault_lock)
    monkeypatch.setattr(migrate_module, "vault_projection_lock", guarded_vault_lock)

    _store(service, "lock-order", "body")

    assert note_entries > 0
    assert vault_entries >= 3


def test_reconcile_fcntl_unavailable_fails_before_db_mutation(tmp_path, monkeypatch) -> None:
    """Removing fail-closed reconcile entry permits derived-state mutation."""
    _service, fixture, Session, _engine = _vault_service(tmp_path, monkeypatch)
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    monkeypatch.setattr(vault_lock, "_FCNTL_AVAILABLE", False)

    with pytest.raises(vault_lock.VaultProjectionUnavailableError):
        reconcile_module.reconcile(fixture.vault, apply=True)

    with Session() as db:
        assert db.query(VaultNoteModel).count() == 0


def test_reconcile_preview_needs_no_projection_lock(tmp_path, monkeypatch) -> None:
    """Locking preview paths would unnecessarily make reads POSIX-only."""
    _service, fixture, _Session, _engine = _vault_service(tmp_path, monkeypatch)
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    monkeypatch.setattr(vault_lock, "_FCNTL_AVAILABLE", False)

    report = reconcile_module.reconcile(fixture.vault, apply=False)

    assert report.vault_id == fixture.vault.id


def test_migration_fcntl_unavailable_fails_before_source_db_read(tmp_path, monkeypatch) -> None:
    """Removing fail-closed migration entry reaches its DB-backed source scan."""
    service, fixture, _Session, _engine = _vault_service(tmp_path, monkeypatch)
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    monkeypatch.setattr(vault_lock, "_FCNTL_AVAILABLE", False)
    source_reads: list[bool] = []
    monkeypatch.setattr(
        migrate_module,
        "_native_rows",
        lambda *_a, **_k: source_reads.append(True) or [],
    )

    with pytest.raises(vault_lock.VaultProjectionUnavailableError):
        migrate_module.migrate_scope(
            service,
            fixture.vault,
            _global_binding(fixture),
            scope="global",
            scope_id=None,
            apply=True,
        )

    assert source_reads == []


def test_forget_fcntl_unavailable_leaves_projection_unchanged(tmp_path, monkeypatch) -> None:
    """Removing fail-closed forget entry can deindex without serialization."""
    service, fixture, Session, _engine = _vault_service(tmp_path, monkeypatch)
    _store(service, "retained", "body")
    vault_lock = importlib.import_module("cli_agent_orchestrator.services.vault.vault_lock")
    monkeypatch.setattr(vault_lock, "_FCNTL_AVAILABLE", False)

    with pytest.raises(vault_lock.VaultProjectionUnavailableError):
        asyncio.run(service.forget("retained", scope="global"))

    with Session() as db:
        note = db.query(VaultNoteModel).filter_by(cao_key="retained").one()
        assert note.status == "indexed"
        assert db.query(VaultExclusionModel).count() == 0
