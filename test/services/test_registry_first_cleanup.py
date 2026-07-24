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
    every bridge/socket/state/lock/log resource class.  A call site is
    truthful only when the declared API verb is CALLED on the exact named
    line, inside the named constructor/deleter or a helper it directly
    calls — never a function definition or another non-verb line."""
    import ast

    repo_root = Path(__file__).resolve().parents[2]
    assert len(rr.RUNTIME_RESOURCE_MANIFEST) >= 30, "manifest must be non-vacuous"
    for item in rr.RUNTIME_RESOURCE_MANIFEST:
        assert set(item) == {"call_site", "api_verb", "resource_kind", "constructor_id"}
        assert item["api_verb"] in rr.MANIFEST_API_VERBS
        assert item["resource_kind"] in rr.RESOURCE_KINDS
        path, _, line = item["call_site"].rpartition(":")
        source = (repo_root / path).read_text(encoding="utf-8").splitlines()
        assert 1 <= int(line) <= len(source), f"call_site line out of range: {item}"
        source_line = source[int(line) - 1]
        # The named line must be the executable verb call itself.
        assert (
            f".{item['api_verb']}(" in source_line
        ), f"call_site does not invoke its declared verb: {item} -> {source_line.strip()}"
        # ... and it must sit inside the named constructor/deleter or a
        # helper that constructor directly calls.
        leaf = item["constructor_id"].split(".")[-1]
        tree = ast.parse("\n".join(source))
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        enclosing = max(
            (
                node
                for node in functions
                if node.lineno <= int(line) <= (node.end_lineno or node.lineno)
            ),
            key=lambda node: node.lineno,
            default=None,
        )
        assert enclosing is not None, f"call_site is not inside any function: {item}"
        if enclosing.name != leaf:
            owner = next((node for node in functions if node.name == leaf), None)
            assert owner is not None, f"constructor not present at call site: {item}"
            calls = {
                node.func.id
                for node in ast.walk(owner)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            assert (
                enclosing.name in calls
            ), f"call_site helper {enclosing.name} is not invoked by {leaf}: {item}"
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
    """The v2 constructor DECLARES every manifest-required kind against a
    real registry (created transitions happen only on observed creation),
    and the generation-conditional deleter converges them truthfully."""
    from cli_agent_orchestrator.services import terminal_service as terminals

    home = tmp_path / "cao-home"
    home.mkdir()
    monkeypatch.setattr("cli_agent_orchestrator.constants.CAO_HOME_DIR", home)
    monkeypatch.setattr("cli_agent_orchestrator.constants.COMPANION_DIR", tmp_path / "companion")
    monkeypatch.setattr(terminals, "FIFO_DIR", tmp_path / "fifos")
    monkeypatch.setattr(terminals, "TERMINAL_LOG_DIR", tmp_path / "logs")
    # Deterministic teardown probes: nothing physical exists here.
    monkeypatch.setattr(terminals, "get_terminal_metadata_v2", lambda tid: None)
    monkeypatch.setattr(terminals, "get_session_env", lambda session: {})
    monkeypatch.setattr(terminals, "get_herdr_inbox_service", lambda: None)

    class _Backend:
        def window_exists(self, session, window):
            return False

    monkeypatch.setattr(terminals, "get_backend", lambda: _Backend())
    rr.reset_resource_registry()
    try:
        terminal_id = "a1b2c3d4"
        generation = str(uuid.uuid4())
        window = f"managed-{terminal_id}-abcdef123456"
        terminals._register_v2_terminal_resources(terminal_id, generation, window, "cao-test")
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
        entries = registry.enumerate(terminal_id=terminal_id, generation=generation)
        for entry in entries:
            if entry["ownership"] == "owned":
                identity = (
                    entry["desired_fs_path"]
                    or entry["desired_db_key"]
                    or entry["desired_tmux_name"]
                    or entry["desired_memory_key"]
                )
                assert entry["entry_id"] in identity
        # Declaration is intent-only: no owned entry is marked created while
        # its physical identity does not exist.
        created_but_absent = [
            entry["entry_id"]
            for entry in entries
            if entry["ownership"] == "owned"
            and entry["lifecycle_state"] == "created"
            and entry["desired_fs_path"]
            and not Path(entry["desired_fs_path"]).exists()
        ]
        assert created_but_absent == []
        # Observed creation transitions only the entries whose artifact
        # really exists.
        log_path = tmp_path / "logs" / f"{terminal_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("output", encoding="utf-8")
        terminals._mark_existing_v2_fs_artifacts(terminal_id)
        assert registry.resolve(f"{terminal_id}.log")["lifecycle_state"] == "created"
        assert registry.resolve(f"{terminal_id}.scrollback")["lifecycle_state"] == "declared"
        # Generation-conditional deregistration drains only this generation:
        # the present log is actually removed before its delete is recorded;
        # never-created entries abort on verified-empty probes.
        terminals._deregister_v2_terminal_resources(terminal_id, generation, "cao-test")
        assert not log_path.exists(), "the deleter removes owned fs artifacts"
        for entry in registry.enumerate(terminal_id=terminal_id, generation=generation):
            if entry["constructor_id"] == "managed_provider_bridge._serve":
                continue
            assert entry["lifecycle_state"] in ("deleted", "aborted"), entry
        # No false absence: nothing marked deleted may still exist on disk.
        for entry in registry.enumerate(terminal_id=terminal_id, generation=generation):
            if entry["lifecycle_state"] == "deleted" and entry["desired_fs_path"]:
                assert not Path(entry["desired_fs_path"]).exists()
    finally:
        rr.reset_resource_registry()


def test_bridge_resources_register_and_deregister(tmp_path, monkeypatch):
    """The bridge's socket/state/journal resources are registry-first too,
    with created only after observed existence and deletion only after
    real removal."""
    from cli_agent_orchestrator.services import managed_provider_bridge as bridge

    home = tmp_path / "cao-home"
    home.mkdir()
    monkeypatch.setattr("cli_agent_orchestrator.constants.CAO_HOME_DIR", home)
    rr.reset_resource_registry()
    try:
        reservation_id = str(uuid.uuid4())
        generation = str(uuid.uuid4())
        root = tmp_path / "managed-provider-sessions" / reservation_id
        root.mkdir(parents=True)
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
        # Only the state tree exists at registration: socket and journal
        # entries are declared, never manufactured as created.
        target["state"].write_text("{}", encoding="utf-8")
        bridge._register_bridge_resources(target, request)
        registry = rr.get_resource_registry()
        entries = registry.enumerate(terminal_id="a1b2c3d4", generation=generation)
        assert {e["kind"] for e in entries} == {"socket", "bridge_state", "db_row_set"}
        by_kind = {e["kind"]: e for e in entries}
        assert by_kind["bridge_state"]["lifecycle_state"] == "created"
        assert by_kind["bridge_state"]["observed_fs_path"] == str(root)
        assert by_kind["socket"]["lifecycle_state"] == "declared"
        assert by_kind["db_row_set"]["lifecycle_state"] == "declared"
        # The socket and journal appear later: observed creation.
        target["socket"].write_text("", encoding="utf-8")
        (root / "delivery-journal.db").write_text("", encoding="utf-8")
        bridge._register_bridge_resources(target, request)
        bridge._mark_bridge_journal_created(target, request)
        by_kind = {
            e["kind"]: e for e in registry.enumerate(terminal_id="a1b2c3d4", generation=generation)
        }
        assert by_kind["socket"]["lifecycle_state"] == "created"
        assert by_kind["db_row_set"]["lifecycle_state"] == "created"
        # Deregistration physically removes the artifacts and only then
        # records verified absence.
        bridge._deregister_bridge_resources(target, request)
        assert not root.exists(), "the bridge deleter removes its state tree"
        entries = registry.enumerate(terminal_id="a1b2c3d4", generation=generation)
        assert all(e["lifecycle_state"] == "deleted" for e in entries)
        for entry in entries:
            assert not Path(entry["desired_fs_path"]).exists()
    finally:
        rr.reset_resource_registry()


def test_v2_construction_is_journal_first_and_teardown_is_truthful(tmp_path, monkeypatch):
    """Production-path regression (P1): registry declaration precedes any
    physical window/DB-row construction; created is recorded only after
    observed creation; teardown proves real absence instead of
    synthesizing it."""
    import asyncio

    from cli_agent_orchestrator import constants
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.services import terminal_service as terminals

    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    database.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", session)
    home = tmp_path / "cao-home"
    home.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    fifos = tmp_path / "fifos"
    fifos.mkdir()
    monkeypatch.setattr(constants, "CAO_HOME_DIR", home)
    monkeypatch.setattr(constants, "COMPANION_DIR", tmp_path / "companion")
    monkeypatch.setattr(terminals, "FIFO_DIR", fifos)
    monkeypatch.setattr(terminals, "TERMINAL_LOG_DIR", logs)
    monkeypatch.setattr(terminals, "_verify_managed_pane_process", lambda *a: None)
    monkeypatch.setattr(terminals, "dispatch_plugin_event", lambda *a, **k: None)
    monkeypatch.setattr(
        terminals,
        "load_agent_profile",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("missing profile")),
    )
    monkeypatch.setattr(terminals, "get_herdr_inbox_service", lambda: None)

    events: list[str] = []

    class _Backend:
        def session_exists(self, _session):
            return True

        def create_window_with_argv(self, _session, window, _terminal, _argv, _cwd, extra_env=None):
            events.append("physical-window-created")
            return window

        def window_identity(self, _session, _window):
            return {"pane_id": "%901", "window_id": "@902"}

        def window_exists(self, _session, _window):
            return False

        def supports_event_inbox(self):
            return True

    monkeypatch.setattr(terminals, "get_backend", lambda: _Backend())
    real_declare = terminals._register_v2_terminal_resources

    def _observed_declare(*args, **kwargs):
        events.append("registry-declaration")
        return real_declare(*args, **kwargs)

    monkeypatch.setattr(terminals, "_register_v2_terminal_resources", _observed_declare)
    real_db_create = terminals.db_create_terminal_v2

    def _observed_db_create(*args, **kwargs):
        events.append("db-row-created")
        return real_db_create(*args, **kwargs)

    monkeypatch.setattr(terminals, "db_create_terminal_v2", _observed_db_create)

    rr.reset_resource_registry()
    try:
        terminal_id = "d1e2f3a4"
        generation = str(uuid.uuid4())
        terminal = asyncio.run(
            terminals.create_terminal(
                provider="codex",
                agent_profile="missing-profile",
                session_name="cao-independent",
                working_directory=str(tmp_path),
                reserved_terminal_id=terminal_id,
                terminal_generation=generation,
                managed_native_command=["/bin/true"],
                protocol_vintage="v2",
            )
        )
        assert terminal.id == terminal_id
        # Journal-first: the durable declaration precedes BOTH the physical
        # window and the v2 DB row.
        assert events[:3] == [
            "registry-declaration",
            "physical-window-created",
            "db-row-created",
        ]
        registry = rr.get_resource_registry()
        entries = registry.enumerate(terminal_id=terminal_id, generation=generation)
        by_id = {entry["entry_id"]: entry for entry in entries}
        # Observed creations are marked created; lazy/absent artifacts stay
        # declared (the event-inbox backend builds no FIFO pipeline here).
        assert by_id[terminal.name]["lifecycle_state"] == "created"
        assert by_id[terminal.name]["observed_tmux_id"] == "@902"
        assert by_id[f"{terminal_id}.db-row"]["lifecycle_state"] == "created"
        assert by_id[f"{terminal_id}.provider"]["lifecycle_state"] == "created"
        assert by_id[f"{terminal_id}.fifo"]["lifecycle_state"] == "declared"
        assert by_id[f"{terminal_id}.log"]["lifecycle_state"] == "declared"
        created_but_absent = [
            entry["entry_id"]
            for entry in entries
            if entry["ownership"] == "owned"
            and entry["lifecycle_state"] == "created"
            and entry["desired_fs_path"]
            and not Path(entry["desired_fs_path"]).exists()
        ]
        assert created_but_absent == []
        # Teardown (production order: the deleter removes the v2 row first):
        # a surviving artifact is REALLY removed before its delete is
        # recorded, and declared entries abort on verified-empty probes.
        owned_log = logs / f"{terminal_id}.log"
        owned_log.write_text("kept output", encoding="utf-8")
        assert database.delete_terminal_v2_if_generation(terminal_id, generation)
        terminals._deregister_v2_terminal_resources(
            terminal_id, generation, session_name="cao-independent"
        )
        assert not owned_log.exists(), "teardown physically removes owned artifacts"
        final = registry.enumerate(terminal_id=terminal_id, generation=generation)
        false_absence = [
            entry["entry_id"]
            for entry in final
            if entry["lifecycle_state"] == "deleted"
            and entry["desired_fs_path"]
            and Path(entry["desired_fs_path"]).exists()
        ]
        assert false_absence == []
        by_id = {entry["entry_id"]: entry for entry in final}
        assert by_id[f"{terminal_id}.log"]["lifecycle_state"] == "deleted"
        assert by_id[terminal.name]["lifecycle_state"] == "deleted"
        assert by_id[f"{terminal_id}.db-row"]["lifecycle_state"] == "deleted"
        assert by_id[f"{terminal_id}.fifo"]["lifecycle_state"] == "aborted"
        assert by_id[f"{terminal_id}.scrollback"]["lifecycle_state"] == "aborted"
    finally:
        rr.reset_resource_registry()
        engine.dispose()
