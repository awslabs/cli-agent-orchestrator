"""Production-wiring regressions: registry-first legacy cleanup and manifest.

The aged-owned-log attack: a live registry-owned v2 log is aged past
retention and legacy ``cleanup_old_data`` runs. Pre-fix it was unlinked by
name+mtime while the registry still read ``created``. Post-fix the file
and the registry lifecycle stay coherent, unowned aged files are still
collected, an unreadable registry fails closed, and the checked source
manifest is complete and non-vacuous.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import cleanup_service
from cli_agent_orchestrator.services import resource_registry as rr


def _age(path: Path) -> None:
    old = time.time() - (cleanup_service.RETENTION_DAYS + 10) * 86400
    os.utime(path, (old, old))


@pytest.fixture
def cleanup_env(tmp_path, monkeypatch):
    """Isolated DB + log dirs + registry home for one cleanup run."""
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    database.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)
    monkeypatch.setattr(cleanup_service, "SessionLocal", session)
    terminal_logs = tmp_path / "terminal-logs"
    server_logs = tmp_path / "server-logs"
    terminal_logs.mkdir()
    server_logs.mkdir()
    monkeypatch.setattr(cleanup_service, "TERMINAL_LOG_DIR", terminal_logs)
    monkeypatch.setattr(cleanup_service, "LOG_DIR", server_logs)
    home = tmp_path / "cao-home"
    home.mkdir()
    monkeypatch.setattr("cli_agent_orchestrator.constants.CAO_HOME_DIR", home)
    # Retention must not kill real FIFO readers / status state.
    monkeypatch.setattr(cleanup_service, "fifo_manager", _NullManager())
    monkeypatch.setattr(cleanup_service, "status_monitor", _NullManager())
    return terminal_logs, server_logs, home, session


class _NullManager:
    def __getattr__(self, name):
        return lambda *a, **k: None


def test_aged_owned_log_survives_and_unowned_is_collected(cleanup_env):
    terminal_logs, _, home, _ = cleanup_env
    terminal_id = "a1b2c3d4"
    generation = str(uuid.uuid4())

    # Registry-owned v2 log: declared AND marked created, file aged.
    registry = rr.ResourceRegistry(home / "resource-registry.sqlite")
    owned_log = terminal_logs / f"{terminal_id}.log"
    owned_log.write_text("v2 log bytes", encoding="utf-8")
    registry.declare(
        entry_id=f"{terminal_id}.log",
        kind="log",
        protocol_vintage="v2",
        terminal_id=terminal_id,
        generation=generation,
        owner="fork",
        ownership="owned",
        constructor_id="terminal_service.create_terminal",
        deleter_id="terminal_service.delete_terminal",
        rollback_rule="generation-isolated",
        actor_id="terminal_service.create_terminal",
        desired_fs_path=str(owned_log),
    )
    registry.register_created(
        f"{terminal_id}.log",
        actor_id="terminal_service.create_terminal",
        existence_receipt_digest=rr.receipt_digest({"entry_id": f"{terminal_id}.log"}),
    )
    _age(owned_log)

    # An unowned aged log must still be collected.
    stray = terminal_logs / "ffffffff.log"
    stray.write_text("stray", encoding="utf-8")
    _age(stray)

    cleanup_service.cleanup_old_data()

    # The attack is closed: file and registry lifecycle are coherent.
    assert owned_log.exists(), "registry-owned v2 log must survive legacy retention"
    entry = registry.resolve(f"{terminal_id}.log")
    assert entry["lifecycle_state"] == "created"
    assert not stray.exists(), "unowned aged logs are still collected"


def test_unreadable_registry_fails_closed(cleanup_env):
    terminal_logs, _, home, _ = cleanup_env
    (home / "resource-registry.sqlite").write_bytes(b"not a sqlite database")
    aged = terminal_logs / "ffffffff.log"
    aged.write_text("bytes", encoding="utf-8")
    _age(aged)

    cleanup_service.cleanup_old_data()

    assert aged.exists(), "unknown ownership must preserve, never delete"


def test_v2_name_shaped_file_survives_without_registry(cleanup_env):
    terminal_logs, _, home, session = cleanup_env
    # No registry DB at all; a v2 terminal row exists in the vintage surface.
    with session() as db:
        db.add(
            database.ManagedLaunchV2TerminalModel(
                id="eeee1234",
                tmux_session="cao-x",
                tmux_window="w",
                provider="codex",
                generation=str(uuid.uuid4()),
                protocol_vintage="v2",
            )
        )
        db.commit()
    shaped = terminal_logs / "eeee1234.scrollback"
    shaped.write_text("v2 scrollback", encoding="utf-8")
    _age(shaped)
    stray = terminal_logs / "dddd4321.scrollback"
    stray.write_text("stray", encoding="utf-8")
    _age(stray)

    cleanup_service.cleanup_old_data()

    assert shaped.exists(), "v2-name-shaped files are invisible to legacy cleanup"
    assert not stray.exists()


def test_manifest_is_complete_non_vacuous_and_call_site_truthful():
    """The checked source manifest: exact {call_site, api_verb,
    resource_kind, constructor_id} shape, real call sites, and coverage of
    every bridge/socket/state/lock/log resource class."""
    repo_root = Path(__file__).resolve().parents[2]
    assert len(rr.RUNTIME_RESOURCE_MANIFEST) >= 30, "manifest must be non-vacuous"
    for item in rr.RUNTIME_RESOURCE_MANIFEST:
        assert set(item) == {"call_site", "api_verb", "resource_kind", "constructor_id"}
        assert item["api_verb"] in rr.MANIFEST_API_VERBS
        assert item["resource_kind"] in rr.RESOURCE_KINDS
        path, _, line = item["call_site"].rpartition(":")
        source = (repo_root / path).read_text(encoding="utf-8").splitlines()
        assert 1 <= int(line) <= len(source), f"call_site line out of range: {item}"
        # The named constructor's function/component exists at the call site.
        leaf = item["constructor_id"].split(".")[-1]
        assert leaf in "\n".join(source), f"constructor not at call site: {item}"
    kinds = {item["resource_kind"] for item in rr.RUNTIME_RESOURCE_MANIFEST}
    for required in (
        "log",
        "scrollback",
        "snapshot",
        "fifo",
        "socket",
        "bridge_state",
        "db_row_set",
        "tmux_window",
        "provider_instance",
        "session_env",
        "herdr",
        "pipe_pane",
        "watchdog",
        "status_map",
        "memory_injection",
        "curator_lock",
        "other",
    ):
        assert required in kinds, f"manifest misses {required}"
    assert kinds == set(rr.MANIFEST_REQUIRED_KINDS)


def test_register_v2_terminal_resources_covers_manifest_kinds(tmp_path, monkeypatch):
    """The v2 constructor registers every manifest-required kind against a
    real registry, and the generation-conditional deleter drains them."""
    from cli_agent_orchestrator.services import terminal_service as terminals

    home = tmp_path / "cao-home"
    home.mkdir()
    monkeypatch.setattr("cli_agent_orchestrator.constants.CAO_HOME_DIR", home)
    monkeypatch.setattr("cli_agent_orchestrator.constants.COMPANION_DIR", tmp_path / "companion")
    rr.reset_resource_registry()
    try:
        terminal_id = "a1b2c3d4"
        generation = str(uuid.uuid4())
        terminals._register_v2_terminal_resources(
            terminal_id, generation, f"managed-{terminal_id}-abcdef123456", "cao-test"
        )
        # Production wiring is complete only once the bridge has also
        # registered its socket/state/journal for the same generation.
        from cli_agent_orchestrator.services import managed_provider_bridge as bridge

        reservation_id = str(uuid.uuid4())
        root = tmp_path / "managed-provider-sessions" / reservation_id
        target = {
            "root": root,
            "state": root / "state.json",
            "socket": root / "bridge.sock",
        }
        bridge._register_bridge_resources(
            target,
            {
                "reservation_id": reservation_id,
                "terminal_id": terminal_id,
                "generation": generation,
            },
        )
        registry = rr.get_resource_registry()
        missing = rr.verify_runtime_wiring(registry, terminal_id=terminal_id, generation=generation)
        assert missing == [], f"unwired manifest kinds: {missing}"
        # Owned entries embed their entry_id (the registry crash-window rule).
        for entry in registry.enumerate(terminal_id=terminal_id, generation=generation):
            if entry["ownership"] == "owned":
                identity = (
                    entry["desired_fs_path"]
                    or entry["desired_db_key"]
                    or entry["desired_tmux_name"]
                    or entry["desired_memory_key"]
                )
                assert entry["entry_id"] in identity
        # Generation-conditional deregistration drains only this generation.
        terminals._deregister_v2_terminal_resources(terminal_id, generation)
        for entry in registry.enumerate(terminal_id=terminal_id, generation=generation):
            assert entry["lifecycle_state"] in ("deleted", "aborted")
    finally:
        rr.reset_resource_registry()


def test_bridge_resources_register_and_deregister(tmp_path, monkeypatch):
    """The bridge's socket/state/journal resources are registry-first too."""
    from cli_agent_orchestrator.services import managed_provider_bridge as bridge

    home = tmp_path / "cao-home"
    home.mkdir()
    monkeypatch.setattr("cli_agent_orchestrator.constants.CAO_HOME_DIR", home)
    rr.reset_resource_registry()
    try:
        reservation_id = str(uuid.uuid4())
        generation = str(uuid.uuid4())
        root = tmp_path / "managed-provider-sessions" / reservation_id
        target = {
            "root": root,
            "state": root / "state.json",
            "socket": root / "bridge.sock",
        }
        request = {
            "reservation_id": reservation_id,
            "terminal_id": "a1b2c3d4",
            "generation": generation,
        }
        bridge._register_bridge_resources(target, request)
        registry = rr.get_resource_registry()
        entries = registry.enumerate(terminal_id="a1b2c3d4", generation=generation)
        assert {e["kind"] for e in entries} == {"socket", "bridge_state", "db_row_set"}
        assert all(e["lifecycle_state"] == "created" for e in entries)
        # A crash/restart converges instead of conflicting.
        bridge._register_bridge_resources(target, request)
        bridge._deregister_bridge_resources(target, request)
        entries = registry.enumerate(terminal_id="a1b2c3d4", generation=generation)
        assert all(e["lifecycle_state"] == "deleted" for e in entries)
    finally:
        rr.reset_resource_registry()
