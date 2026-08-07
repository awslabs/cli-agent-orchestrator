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
import socket
import tempfile
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


def _bridge_request(
    *,
    reservation_id: str,
    terminal_id: str,
    generation: str,
    worktree: Path,
) -> dict[str, object]:
    """A complete canonical rendezvous request for bridge wiring tests."""
    return {
        "reservation_id": reservation_id,
        "terminal_id": terminal_id,
        "generation": generation,
        "delivery_id": str(uuid.uuid4()),
        "provider": "codex",
        "rendezvous_identity": {
            "project": "registry-first-tests",
            "task_id": reservation_id,
            "terminal_id": terminal_id,
            "terminal_generation": generation,
            "worktree_realpath": str(worktree.resolve()),
            "repository": "cli-agent-orchestrator",
            "head": "1" * 40,
            "actor": "registry-first-tests",
        },
    }


def _bridge_target(bridge, root: Path, request: dict[str, object]) -> dict[str, Path]:
    target = {
        "root": root,
        "request": root / "request.json",
        "state": root / "state.json",
    }
    target.update(bridge.rendezvous_paths(request["rendezvous_identity"]))
    return target


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
        # declared its socket/state/journal for the same generation.
        from cli_agent_orchestrator.services import managed_provider_bridge as bridge

        reservation_id = str(uuid.uuid4())
        root = tmp_path / "managed-provider-sessions" / reservation_id
        runtime = Path(tempfile.mkdtemp(prefix="cao-rf-", dir="/tmp"))
        monkeypatch.setattr(bridge, "RENDEZVOUS_ROOT", runtime)
        request = _bridge_request(
            reservation_id=reservation_id,
            terminal_id=terminal_id,
            generation=generation,
            worktree=tmp_path,
        )
        target = _bridge_target(bridge, root, request)
        bridge._declare_bridge_resources(
            target,
            request,
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
    """The bridge's socket/state/journal resources are registry-first too:
    declaration is intent-only and never claims the pre-existing
    reservation root, created is recorded only after observed existence,
    and deletion only after real removal."""
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
        runtime = Path(tempfile.mkdtemp(prefix="cao-rf-", dir="/tmp"))
        monkeypatch.setattr(bridge, "RENDEZVOUS_ROOT", runtime)
        request = _bridge_request(
            reservation_id=reservation_id,
            terminal_id="a1b2c3d4",
            generation=generation,
            worktree=tmp_path,
        )
        target = _bridge_target(bridge, root, request)
        # Declaration comes BEFORE any bridge physical construction: the
        # reservation root already exists (write_request creates it in the
        # launcher process) but is NOT the bridge-state resource — the
        # exact state file is, and nothing is marked created yet.
        bridge._declare_bridge_resources(target, request)
        registry = rr.get_resource_registry()
        entries = registry.enumerate(terminal_id="a1b2c3d4", generation=generation)
        assert {e["kind"] for e in entries} == {"socket", "bridge_state", "db_row_set"}
        by_kind = {e["kind"]: e for e in entries}
        assert by_kind["bridge_state"]["desired_fs_path"] == str(target["state"])
        assert all(e["lifecycle_state"] == "declared" for e in entries)
        # Observed creation transitions only the artifact that really exists.
        target["state"].write_text("{}", encoding="utf-8")
        bridge._mark_bridge_resource_created(target, request, "bridge_state")
        by_kind = {
            e["kind"]: e for e in registry.enumerate(terminal_id="a1b2c3d4", generation=generation)
        }
        assert by_kind["bridge_state"]["lifecycle_state"] == "created"
        assert by_kind["bridge_state"]["observed_fs_path"] == str(target["state"])
        assert by_kind["socket"]["lifecycle_state"] == "declared"
        assert by_kind["db_row_set"]["lifecycle_state"] == "declared"
        # The socket and journal appear later: observed creation.
        _, descriptor = bridge._claim_rendezvous(request, target)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(target["socket"]))
        target["socket"].chmod(0o600)
        server.listen(1)
        bridge._publish_socket_claim(
            descriptor,
            target["binding"],
            target["socket"],
            request["rendezvous_identity"],
        )
        (root / "delivery-journal.db").write_text("", encoding="utf-8")
        (root / "session-control-journal.db").write_text("", encoding="utf-8")
        bridge._mark_bridge_resource_created(target, request, "socket")
        bridge._mark_bridge_journal_created(target, request)
        bridge._mark_control_journal_created(target, request)
        entries = registry.enumerate(terminal_id="a1b2c3d4", generation=generation)
        by_kind = {e["kind"]: e for e in entries}
        assert by_kind["socket"]["lifecycle_state"] == "created"
        bridge_journals = [
            entry
            for entry in entries
            if entry["kind"] == "db_row_set"
            and entry["constructor_id"] == "managed_provider_bridge._serve"
        ]
        assert len(bridge_journals) == 2
        assert all(entry["lifecycle_state"] == "created" for entry in bridge_journals)
        # Deregistration physically removes the artifacts and only then
        # records verified absence.
        bridge._deregister_bridge_resources(target, request)
        assert not root.exists(), "the bridge deleter removes its state tree"
        entries = registry.enumerate(terminal_id="a1b2c3d4", generation=generation)
        assert all(e["lifecycle_state"] == "deleted" for e in entries)
        for entry in entries:
            assert not Path(entry["desired_fs_path"]).exists()
        server.close()
        os.close(descriptor)
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
            # The complete five-field tuple, because a v2 launch registers
            # its live incarnation as a precondition and refuses a partial
            # identity. This test is about journal-first ordering and
            # truthful teardown, so the identity only has to be registrable
            # enough to reach them.
            return {
                "pane_id": "%901",
                "window_id": "@902",
                "session_id": "$9",
                "server_socket_path": "/private/tmp/tmux-501/default",
                "pane_pid": 9012,
            }

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


def test_bridge_serve_declares_before_physical_construction(monkeypatch):
    """Production-path ordering regression (P1): inside the real ``_serve``
    the durable declarations precede the state-file write and the socket
    bind/listen, created is receipted only against the exact observed
    artifact, and a controlled pre-exposure failure tears down truthfully
    — the socket really removed before its delete, the never-created
    journal aborted on a verified-absence probe, and the diagnostic state
    file retained with its row closed (present, never synthesized away)."""
    import json
    import tempfile

    from cli_agent_orchestrator.services import managed_provider_bridge as bridge

    with tempfile.TemporaryDirectory(prefix="lb-brq-", dir="/tmp") as root_arg:
        base = Path(root_arg)
        home = base / "cao-home"
        home.mkdir()
        monkeypatch.setattr("cli_agent_orchestrator.constants.CAO_HOME_DIR", home)
        monkeypatch.setattr(bridge, "BRIDGE_ROOT", base / "managed-provider-sessions")
        monkeypatch.setattr(bridge, "RENDEZVOUS_ROOT", base / "rendezvous")
        rr.reset_resource_registry()
        try:
            reservation_id = str(uuid.uuid4())
            generation = str(uuid.uuid4())
            terminal_id = "a1b2c3d4"
            request = _bridge_request(
                reservation_id=reservation_id,
                terminal_id=terminal_id,
                generation=generation,
                worktree=base,
            )
            # The production launcher envelope: write_request creates the
            # reservation root (and request.json) before the bridge starts.
            target = bridge.write_request(reservation_id, request)
            assert target["root"].exists() and not target["state"].exists()
            registry = rr.get_resource_registry()
            observed: dict[str, object] = {}
            real_atomic = bridge._atomic_json

            def _observing_atomic(path, value):
                if path == target["state"] and "at_first_state_write" not in observed:
                    observed["state_file_existed"] = path.exists()
                    observed["socket_existed"] = target["socket"].exists()
                    observed["at_first_state_write"] = {
                        e["kind"]: e["lifecycle_state"]
                        for e in registry.enumerate(terminal_id=terminal_id, generation=generation)
                    }
                return real_atomic(path, value)

            monkeypatch.setattr(bridge, "_atomic_json", _observing_atomic)

            class _InitFailure:
                def __init__(self, _request):
                    pass

                def initialize(self):
                    observed["socket_bound_at_initialize"] = target["socket"].exists()
                    observed["at_initialize"] = {
                        e["kind"]: e["lifecycle_state"]
                        for e in registry.enumerate(terminal_id=terminal_id, generation=generation)
                    }
                    raise bridge.BridgeError("provider initialization failed")

                def close(self):
                    pass

            monkeypatch.setattr(bridge, "_ProviderSession", _InitFailure)
            monkeypatch.setattr(bridge, "verify_launch_binding_identity", lambda *_: None)

            assert bridge._serve(request, target) == 1

            # Declaration-before-physical: at the first state write every
            # entry was already durably declared and neither physical
            # artifact existed.
            assert observed["state_file_existed"] is False
            assert observed["socket_existed"] is False
            assert observed["at_first_state_write"] == {
                "socket": "declared",
                "bridge_state": "declared",
                "db_row_set": "declared",
            }
            # Observed-only creation: by provider initialization the state
            # file and the bound socket were receipted created; the lazy
            # journal stays declared.
            assert observed["socket_bound_at_initialize"] is True
            assert observed["at_initialize"] == {
                "socket": "created",
                "bridge_state": "created",
                "db_row_set": "declared",
            }
            # Truthful teardown of the controlled failure.
            assert not target["socket"].exists()
            assert target["state"].exists(), "the preflight diagnostic survives"
            persisted = json.loads(target["state"].read_text(encoding="utf-8"))
            assert persisted["state"] == "launch-failed-bridge"
            assert "provider initialization failed" in persisted["error"]
            final = {
                e["kind"]: e
                for e in registry.enumerate(terminal_id=terminal_id, generation=generation)
            }
            assert final["socket"]["lifecycle_state"] == "deleted"
            assert final["db_row_set"]["lifecycle_state"] == "aborted"
            assert final["bridge_state"]["lifecycle_state"] == "closed"
            assert final["bridge_state"]["desired_fs_path"] == str(target["state"])
            # No false absence: a deleted entry's artifact is really gone.
            for entry in final.values():
                if entry["lifecycle_state"] == "deleted" and entry["desired_fs_path"]:
                    assert not Path(entry["desired_fs_path"]).exists()
        finally:
            rr.reset_resource_registry()


def test_direct_bridge_serves_do_not_retain_provider_environment(monkeypatch):
    """Sequential in-process serves bind fresh provider state per launch.

    The first launch's provider-only controls and HOME must not reach the
    second launch, while the production fail-closed bridge environment stays
    scrubbed during both serves.
    """
    import json
    import tempfile

    from cli_agent_orchestrator.services import managed_provider_bridge as bridge

    with tempfile.TemporaryDirectory(prefix="lb-env-", dir="/tmp") as root_arg:
        base = Path(root_arg)
        home = base / "cao-home"
        home.mkdir()
        monkeypatch.setattr("cli_agent_orchestrator.constants.CAO_HOME_DIR", home)
        monkeypatch.setattr(bridge, "BRIDGE_ROOT", base / "managed-provider-sessions")
        monkeypatch.setattr(bridge, "RENDEZVOUS_ROOT", base / "rendezvous")
        monkeypatch.setattr(bridge, "verify_launch_binding_identity", lambda *_: None)
        observed: list[dict[str, dict[str, str]]] = []

        class _InitFailure:
            def __init__(self, _request):
                observed.append(
                    {
                        "bridge": dict(os.environ),
                        "provider": bridge._provider_env(),
                    }
                )

            def initialize(self):
                raise bridge.BridgeError("expected direct-serve stop")

            def close(self):
                pass

        monkeypatch.setattr(bridge, "_ProviderSession", _InitFailure)
        rr.reset_resource_registry()
        try:
            requests = [
                _bridge_request(
                    reservation_id=str(uuid.uuid4()),
                    terminal_id=terminal_id,
                    generation=str(uuid.uuid4()),
                    worktree=base,
                )
                for terminal_id in ("a1b2c3d4", "b1c2d3e4")
            ]

            monkeypatch.setenv("HOME", "/home/first")
            monkeypatch.setenv("CODEX_FIRST_ONLY", "first-secret")
            first_target = bridge.write_request(requests[0]["reservation_id"], requests[0])
            assert bridge._serve(requests[0], first_target) == 1
            first_state = json.loads(first_target["state"].read_text(encoding="utf-8"))
            assert bridge._BOUND_PROVIDER_ENV is None
            assert os.environ["HOME"] == "/home/first"

            monkeypatch.setenv("HOME", "/home/second")
            monkeypatch.delenv("CODEX_FIRST_ONLY")
            monkeypatch.setenv("CODEX_SECOND_ONLY", "second-secret")
            second_target = bridge.write_request(requests[1]["reservation_id"], requests[1])
            assert bridge._serve(requests[1], second_target) == 1
            second_state = json.loads(second_target["state"].read_text(encoding="utf-8"))

            assert observed[0]["bridge"]["HOME"] == "/home/first"
            assert "CODEX_FIRST_ONLY" not in observed[0]["bridge"]
            assert observed[0]["provider"]["HOME"] == "/home/first"
            assert observed[0]["provider"]["CODEX_FIRST_ONLY"] == "first-secret"
            assert observed[1]["bridge"]["HOME"] == "/home/second"
            assert "CODEX_SECOND_ONLY" not in observed[1]["bridge"]
            assert observed[1]["provider"]["HOME"] == "/home/second"
            assert observed[1]["provider"]["CODEX_SECOND_ONLY"] == "second-secret"
            assert "CODEX_FIRST_ONLY" not in observed[1]["provider"]

            first_names = first_state["environment_inventory"]["names"]
            second_names = second_state["environment_inventory"]["names"]
            assert "CODEX_FIRST_ONLY" in first_names
            assert "CODEX_FIRST_ONLY" not in second_names
            assert "CODEX_SECOND_ONLY" in second_names
            assert "first-secret" not in json.dumps(first_state["environment_inventory"])
            assert "second-secret" not in json.dumps(second_state["environment_inventory"])
            assert bridge._BOUND_PROVIDER_ENV is None
            assert os.environ["HOME"] == "/home/second"
        finally:
            rr.reset_resource_registry()


def test_failed_provider_version_probe_records_provider_io(monkeypatch):
    """A completed provider subprocess attempt is provider I/O even when
    initialization fails before the RPC client is assigned."""
    import hashlib
    import json
    import tempfile

    from cli_agent_orchestrator.services import managed_provider_bridge as bridge

    with tempfile.TemporaryDirectory(prefix="lb-version-", dir="/tmp") as root_arg:
        base = Path(root_arg).resolve()
        home = base / "cao-home"
        home.mkdir()
        executable = base / "codex"
        executable.write_text("provider", encoding="utf-8")
        executable.chmod(0o755)
        profile_sha256 = "a" * 64
        monkeypatch.setattr("cli_agent_orchestrator.constants.CAO_HOME_DIR", home)
        monkeypatch.setattr(bridge, "BRIDGE_ROOT", base / "managed-provider-sessions")
        monkeypatch.setattr(bridge, "RENDEZVOUS_ROOT", base / "rendezvous")
        monkeypatch.setattr(bridge, "verify_launch_binding_identity", lambda *_: None)
        monkeypatch.setattr(
            bridge,
            "_profile_material",
            lambda *_: {
                "profile": object(),
                "profile_sha256": profile_sha256,
                "allowed_tools": ["*"],
                "system_prompt": "",
                "mcp_servers": [],
            },
        )

        class _FailedVersion:
            returncode = 7
            stdout = "unexpected-version"
            stderr = "provider failed"

        monkeypatch.setattr(bridge.subprocess, "run", lambda *_args, **_kwargs: _FailedVersion())
        rr.reset_resource_registry()
        try:
            request = _bridge_request(
                reservation_id=str(uuid.uuid4()),
                terminal_id="c1d2e3f4",
                generation=str(uuid.uuid4()),
                worktree=base,
            )
            request.update(
                {
                    "bridge_version": bridge.BRIDGE_VERSION,
                    "agent_profile": "reviewer",
                    "profile_sha256": profile_sha256,
                    "model": "gpt-5.6-sol",
                    "effort": "high",
                    "working_directory": str(base),
                    "provider_executable": str(executable),
                    "provider_executable_sha256": hashlib.sha256(
                        executable.read_bytes()
                    ).hexdigest(),
                }
            )
            target = bridge.write_request(request["reservation_id"], request)

            assert bridge._serve(request, target) == 1

            state = json.loads(target["state"].read_text(encoding="utf-8"))
            assert state["state"] == "launch-failed-bridge"
            assert state["launch_failure"]["provider_io_started"] is True
            assert state["launch_failure"]["task_bytes_submitted"] is False
            assert "provider --version exited 7" in state["error"]
        finally:
            rr.reset_resource_registry()


def test_bridge_teardown_never_synthesizes_absence(tmp_path, monkeypatch):
    """A bridge resource that cannot be physically removed keeps its row:
    the registry records deletion only against a real absence probe."""
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
        runtime = Path(tempfile.mkdtemp(prefix="cao-rf-", dir="/tmp"))
        monkeypatch.setattr(bridge, "RENDEZVOUS_ROOT", runtime)
        request = _bridge_request(
            reservation_id=reservation_id,
            terminal_id="a1b2c3d4",
            generation=generation,
            worktree=tmp_path,
        )
        target = _bridge_target(bridge, root, request)
        bridge._declare_bridge_resources(target, request)
        target["state"].write_text("{}", encoding="utf-8")
        _, descriptor = bridge._claim_rendezvous(request, target)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(target["socket"]))
        target["socket"].chmod(0o600)
        server.listen(1)
        bridge._publish_socket_claim(
            descriptor,
            target["binding"],
            target["socket"],
            request["rendezvous_identity"],
        )
        bridge._mark_bridge_resource_created(target, request, "bridge_state")
        bridge._mark_bridge_resource_created(target, request, "socket")
        registry = rr.get_resource_registry()

        real_unlink = Path.unlink

        def _refusing_unlink(self, *args, **kwargs):
            if self == target["socket"]:
                raise OSError("socket unlink refused")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", _refusing_unlink)
        bridge._deregister_bridge_resources(target, request)

        # The surviving socket keeps a closed row — present and
        # discoverable; everything converged around it truthfully.
        assert target["socket"].exists()
        by_kind = {
            e["kind"]: e for e in registry.enumerate(terminal_id="a1b2c3d4", generation=generation)
        }
        assert by_kind["socket"]["lifecycle_state"] == "closed"
        assert by_kind["bridge_state"]["lifecycle_state"] == "deleted"
        assert by_kind["db_row_set"]["lifecycle_state"] == "aborted"
        deleted = [
            e
            for e in by_kind.values()
            if e["lifecycle_state"] == "deleted" and e["desired_fs_path"]
        ]
        assert all(not Path(e["desired_fs_path"]).exists() for e in deleted)
        server.close()
        os.close(descriptor)
    finally:
        rr.reset_resource_registry()


def test_bridge_hard_crash_leaves_durable_declarations():
    """The production hard-crash window (P1): the bridge process dies
    during provider initialization — AFTER ``state.json`` is written and
    the real Unix socket is bound/listening.  The durable declarations and
    the observed creation receipts are already committed, so the surviving
    artifacts stay discoverable for reconciliation instead of becoming
    unregistered orphans."""
    import subprocess
    import sys
    import tempfile
    import textwrap

    repo_root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="lb-bc-", dir="/tmp") as root_arg:
        base = Path(root_arg)
        env = dict(os.environ)
        # HOME is the fixture root directly: the AF_UNIX path under
        # <home>/.aws/.../bridge.sock must stay within the macOS limit.
        env["HOME"] = str(base)
        env["PYTHONPATH"] = str(repo_root / "src")
        env["CAO_BRIDGE_TEST_BASE"] = str(base)
        for var in ("AUTH0_DOMAIN", "AUTH0_AUDIENCE", "CAO_AUTH_JWKS_URI"):
            env.pop(var, None)
        child = textwrap.dedent("""
            import os
            import sys
            from pathlib import Path
            from unittest.mock import patch

            from cli_agent_orchestrator.services import (
                managed_provider_bridge as bridge,
            )

            base = Path(os.environ["CAO_BRIDGE_TEST_BASE"])
            bridge.RENDEZVOUS_ROOT = base / "rendezvous"
            request = {
                "reservation_id": "crash-res",
                "terminal_id": "c1a5c0de",
                    "generation": "generation-crash-probe",
                    "delivery_id": "33333333-3333-4333-8333-333333333333",
                    "provider": "codex",
                "rendezvous_identity": {
                    "project": "registry-first-tests",
                    "task_id": "crash-res",
                    "terminal_id": "c1a5c0de",
                    "terminal_generation": "generation-crash-probe",
                    "worktree_realpath": os.path.realpath(base),
                    "repository": "cli-agent-orchestrator",
                    "head": "1" * 40,
                    "actor": "registry-first-tests",
                },
            }
            target = bridge.write_request("crash-res", request)


            class CrashDuringInitialize:
                def __init__(self, _request):
                    pass

                def initialize(self):
                    # _serve has already written state.json and bound/
                    # listened on the real Unix socket; die abruptly here.
                    os._exit(77)

                def close(self):
                    pass


            with (
                patch.object(bridge, "_ProviderSession", CrashDuringInitialize),
                patch.object(bridge, "verify_launch_binding_identity", lambda *_: None),
            ):
                bridge._serve(request, target)
            os._exit(99)
            """)
        completed = subprocess.run(
            [sys.executable, "-c", child],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            check=False,
        )
        assert completed.returncode == 77, completed.stderr

        cao_home = base / ".aws" / "cli-agent-orchestrator"
        root = cao_home / "managed-provider-sessions" / "crash-res"
        state_path = root / "state.json"
        registry_path = cao_home / "resource-registry.sqlite"
        # The physical artifacts survive the hard crash for reconciliation…
        assert state_path.exists(), "state file must survive the hard crash"
        # …and the durable registry rows already exist to discover them.
        assert registry_path.exists(), "declarations must be committed before the crash"
        registry = rr.ResourceRegistry(registry_path)
        entries = registry.enumerate(terminal_id="c1a5c0de", generation="generation-crash-probe")
        by_kind = {e["kind"]: e for e in entries}
        socket_path = Path(by_kind["socket"]["desired_fs_path"])
        binding_path = socket_path.with_suffix(".json")
        assert socket_path.exists(), "bound socket path must survive the hard crash"
        assert binding_path.exists(), "full-tuple binding sidecar must survive the hard crash"
        assert set(by_kind) == {"socket", "bridge_state", "db_row_set"}
        assert by_kind["bridge_state"]["lifecycle_state"] == "created"
        assert by_kind["bridge_state"]["desired_fs_path"] == str(state_path)
        assert by_kind["socket"]["lifecycle_state"] == "created"
        assert by_kind["socket"]["desired_fs_path"] == str(socket_path)
        assert by_kind["db_row_set"]["lifecycle_state"] == "declared"
        # The surviving artifacts resolve to their live rows: no orphans.
        assert registry.resolve_fs_path(str(socket_path)) is not None
        assert registry.resolve_fs_path(str(state_path)) is not None
