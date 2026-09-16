"""Integrity, secrecy, size, and shared-run lifecycle for private plan snapshots."""

import json
import os
import sqlite3
import traceback
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

import pytest

from cli_agent_orchestrator import constants
from cli_agent_orchestrator.clients.database import (
    _migrate_workflow_plan_snapshot,
    _migrate_workflow_run,
)
from cli_agent_orchestrator.constants import WORKFLOW_MANIFEST_MAX_BYTES
from cli_agent_orchestrator.services import plan_identifier as pi
from cli_agent_orchestrator.services import private_plan_snapshot as snapshots
from cli_agent_orchestrator.services import workflow_journal

SECRET = "AKIAIOSFODNN7EXAMPLE"
PRIVATE_PATH = "/private/operator/checkout"


@pytest.fixture(autouse=True)
def _private_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "workflow.db"
    assert path.parent == tmp_path
    assert not path.exists()
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path, raising=True)
    assert Path(constants.DATABASE_FILE) == path
    yield path


def _materials(**overrides: bytes) -> dict[str, bytes]:
    input_key_bytes = pi.canonical_key_bytes("token")
    input_key = pi.digest_bytes(input_key_bytes)
    values = {
        "artifact_hash": b"SCOPE = {'main': {}}\n",
        "declaration": snapshots.structured_material_bytes({"version": 1, "targets": {"main": {}}}),
        "targets": snapshots.structured_material_bytes({"main": {"path": PRIVATE_PATH}}),
        "input-key:" + input_key: input_key_bytes,
        "input-value:" + input_key: pi.canonical_component_bytes(SECRET),
        "limits": snapshots.structured_material_bytes({"max_steps": 10}),
        "retry_policy": snapshots.structured_material_bytes({"retries": 3}),
        "policy": snapshots.structured_material_bytes(
            {"prompt": "private prompt", "token": "private mcp token"}
        ),
        "memory": snapshots.structured_material_bytes({"mode": "off"}),
    }
    values.update(overrides)
    return values


def _components(materials: dict[str, bytes]) -> pi.PlanV2Components:
    inputs = pi.digest_inputs({"token": SECRET})
    key_digest = inputs[0].key_digest
    assert materials[f"input-key:{key_digest}"] == pi.canonical_key_bytes("token")
    assert materials[f"input-value:{key_digest}"] == pi.canonical_component_bytes(SECRET)
    return pi.PlanV2Components(
        tier="script",
        artifact_hash=pi.digest_bytes(materials["artifact_hash"]),
        declaration=pi.digest_bytes(materials["declaration"]),
        targets=pi.digest_bytes(materials["targets"]),
        inputs=inputs,
        limits=pi.digest_bytes(materials["limits"]),
        retry_policy=pi.digest_bytes(materials["retry_policy"]),
        policy=pi.digest_bytes(materials["policy"]),
        memory=pi.digest_bytes(materials["memory"]),
    )


def _plan_for_inputs(
    inputs: dict[str, object],
) -> tuple[pi.PlanV2Components, dict[str, bytes]]:
    materials = {
        name: material
        for name, material in _materials().items()
        if not name.startswith(("input-key:", "input-value:"))
    }
    for key, value in inputs.items():
        key_material = pi.canonical_key_bytes(key)
        key_digest = pi.digest_bytes(key_material)
        materials[f"input-key:{key_digest}"] = key_material
        materials[f"input-value:{key_digest}"] = pi.canonical_component_bytes(value)
    components = pi.PlanV2Components(
        tier="script",
        artifact_hash=pi.digest_bytes(materials["artifact_hash"]),
        declaration=pi.digest_bytes(materials["declaration"]),
        targets=pi.digest_bytes(materials["targets"]),
        inputs=pi.digest_inputs(inputs),
        limits=pi.digest_bytes(materials["limits"]),
        retry_policy=pi.digest_bytes(materials["retry_policy"]),
        policy=pi.digest_bytes(materials["policy"]),
        memory=pi.digest_bytes(materials["memory"]),
    )
    return components, materials


def _store(run_id: str = "snapshot-run") -> tuple[str, pi.PlanV2Components, dict[str, bytes]]:
    materials = _materials()
    components = _components(materials)
    _seed_run(run_id)
    plan_id = snapshots.freeze_and_attach(run_id, components, materials)
    return plan_id, components, materials


def _seed_run(run_id: str) -> None:
    workflow_journal.insert_run(
        run_id=run_id,
        workflow_name="wf",
        spec_snapshot="source",
        inputs_json="{}",
        state="running",
        started_at="2026-09-16T00:00:00Z",
        tier="script",
    )


def test_round_trip_returns_exact_executable_bytes_only_after_integrity_check():
    plan_id, components, materials = _store("round-trip")

    loaded = snapshots.private_snapshot_for_run("round-trip", components)

    assert loaded.plan_id == plan_id
    assert dict(loaded.components) == materials
    input_key = next(name for name in materials if name.startswith("input-key:"))
    key_digest = input_key.removeprefix("input-key:")
    assert loaded.material(input_key) == b"token"
    assert loaded.material(f"input-value:{key_digest}") == pi.canonical_component_bytes(SECRET)
    assert PRIVATE_PATH.encode() in loaded.material("targets")


@pytest.mark.parametrize(
    "inputs",
    [
        {},
        {"zeta": {"enabled": True}, "alpha": 600.0, "middle": "private"},
    ],
    ids=["no-inputs", "multiple-inputs"],
)
def test_stored_plan_components_round_trip_from_persisted_identity_rows(
    inputs: dict[str, object],
):
    components, materials = _plan_for_inputs(inputs)
    _seed_run("stored-components")
    plan_id = snapshots.freeze_and_attach("stored-components", components, materials)

    stored_plan_id, stored_components = snapshots.stored_plan_components_for_run(
        "stored-components"
    )

    assert stored_plan_id == plan_id
    assert stored_components == components
    assert tuple(item.key_digest for item in stored_components.inputs) == tuple(
        sorted(item.key_digest for item in stored_components.inputs)
    )
    verified = snapshots.private_snapshot_for_run("stored-components", stored_components)
    assert dict(verified.components) == materials


def test_stored_plan_components_reads_one_explicit_read_only_transaction(
    _private_db: Path, monkeypatch: pytest.MonkeyPatch
):
    components, materials = _plan_for_inputs({"alpha": 1, "beta": "two"})
    _seed_run("consistent-read")
    snapshots.freeze_and_attach("consistent-read", components, materials)
    statements: list[str] = []
    opened: list[sqlite3.Connection] = []
    connect_readonly = snapshots._connect_readonly

    def _traced_readonly_connection() -> sqlite3.Connection:
        conn = connect_readonly()
        conn.set_trace_callback(statements.append)
        opened.append(conn)
        return conn

    monkeypatch.setattr(snapshots, "_connect_readonly", _traced_readonly_connection)

    assert snapshots.stored_plan_components_for_run("consistent-read") == (
        pi.compute_v2(components),
        components,
    )

    assert statements[0] == "BEGIN"
    assert statements[-1] == "ROLLBACK"
    assert sum(statement == "BEGIN" for statement in statements) == 1
    assert sum(statement == "ROLLBACK" for statement in statements) == 1
    assert all(" content " not in f" {statement.lower()} " for statement in statements)
    assert not any(
        token in statement.upper()
        for statement in statements
        for token in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "ALTER ", "DROP ")
    )
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


@pytest.mark.parametrize("missing", ["run", "reference"])
def test_stored_plan_components_refuses_missing_run_or_reference(missing: str, _private_db: Path):
    if missing == "run":
        _migrate_workflow_run()
        _migrate_workflow_plan_snapshot()
        os.chmod(_private_db, 0o600)
    else:
        components, materials = _plan_for_inputs({})
        _seed_run("missing-reference")
        snapshots.freeze_and_attach("missing-reference", components, materials)
        with sqlite3.connect(_private_db) as conn:
            conn.execute(
                "DELETE FROM workflow_run_plan_snapshot WHERE run_id = ?",
                ("missing-reference",),
            )

    with pytest.raises(snapshots.PrivateSnapshotIntegrityError, match="run reference"):
        snapshots.stored_plan_components_for_run("missing-reference")


@pytest.mark.parametrize(
    "drift",
    ["header", "tier", "plan-id", "hash", "invalid-digest"],
)
def test_stored_plan_components_refuses_identity_row_drift(drift: str, _private_db: Path):
    components, materials = _plan_for_inputs({"alpha": 1})
    _seed_run("identity-drift")
    plan_id = snapshots.freeze_and_attach("identity-drift", components, materials)
    with sqlite3.connect(_private_db) as conn:
        if drift == "header":
            conn.execute(
                "UPDATE workflow_plan_snapshot SET component_set_version = '3' "
                "WHERE plan_id = ?",
                (plan_id,),
            )
        elif drift == "tier":
            conn.execute("UPDATE workflow_run SET tier = 'yaml' WHERE run_id = 'identity-drift'")
        elif drift == "plan-id":
            replacement = "plan-v2:" + "0" * 64
            conn.execute(
                "UPDATE workflow_plan_snapshot SET plan_id = ? WHERE plan_id = ?",
                (replacement, plan_id),
            )
            conn.execute(
                "UPDATE workflow_plan_snapshot_component SET plan_id = ? WHERE plan_id = ?",
                (replacement, plan_id),
            )
            conn.execute(
                "UPDATE workflow_run_plan_snapshot SET plan_id = ? WHERE plan_id = ?",
                (replacement, plan_id),
            )
        else:
            digest = "0" * 64 if drift == "hash" else "invalid"
            conn.execute(
                "UPDATE workflow_plan_snapshot_component SET content_digest = ? "
                "WHERE plan_id = ? AND component_name = 'policy'",
                (digest, plan_id),
            )

    with pytest.raises(
        (snapshots.PrivateSnapshotIntegrityError, pi.PlanV2ComponentError)
    ) as excinfo:
        snapshots.stored_plan_components_for_run("identity-drift")
    assert SECRET not in str(excinfo.value)
    assert PRIVATE_PATH not in str(excinfo.value)


@pytest.mark.parametrize("orphan", ["key", "value"])
def test_stored_plan_components_refuses_orphaned_input_rows(orphan: str, _private_db: Path):
    components, materials = _plan_for_inputs({"alpha": 1, "beta": "two"})
    _seed_run("orphan-input")
    plan_id = snapshots.freeze_and_attach("orphan-input", components, materials)
    key_digest = components.inputs[0].key_digest
    with sqlite3.connect(_private_db) as conn:
        conn.execute(
            "DELETE FROM workflow_plan_snapshot_component "
            "WHERE plan_id = ? AND component_name = ?",
            (plan_id, f"input-{orphan}:{key_digest}"),
        )

    with pytest.raises(snapshots.PrivateSnapshotIntegrityError, match="input"):
        snapshots.stored_plan_components_for_run("orphan-input")


def test_stored_plan_components_refuses_unknown_component_name_without_echo(
    _private_db: Path,
):
    components, materials = _plan_for_inputs({})
    _seed_run("unknown-component")
    plan_id = snapshots.freeze_and_attach("unknown-component", components, materials)
    with sqlite3.connect(_private_db) as conn:
        conn.execute(
            "INSERT INTO workflow_plan_snapshot_component "
            "(plan_id, component_name, content_digest, content) VALUES (?, ?, ?, ?)",
            (plan_id, SECRET, "0" * 64, b"not-read"),
        )

    with pytest.raises(snapshots.PrivateSnapshotIntegrityError) as excinfo:
        snapshots.stored_plan_components_for_run("unknown-component")
    assert SECRET not in str(excinfo.value)


@pytest.mark.parametrize("duplicate", ["component", "reference"])
def test_stored_plan_components_refuses_duplicate_rows_when_schema_permits_them(
    duplicate: str, _private_db: Path
):
    components, materials = _plan_for_inputs({"alpha": 1})
    _seed_run("duplicate-row")
    plan_id = snapshots.freeze_and_attach("duplicate-row", components, materials)
    with sqlite3.connect(_private_db) as conn:
        if duplicate == "component":
            conn.execute(
                "ALTER TABLE workflow_plan_snapshot_component "
                "RENAME TO original_snapshot_component"
            )
            conn.execute(
                "CREATE TABLE workflow_plan_snapshot_component ("
                "plan_id TEXT NOT NULL, component_name TEXT NOT NULL, "
                "content_digest TEXT NOT NULL, content BLOB NOT NULL)"
            )
            conn.execute(
                "INSERT INTO workflow_plan_snapshot_component "
                "SELECT * FROM original_snapshot_component"
            )
            conn.execute(
                "INSERT INTO workflow_plan_snapshot_component "
                "SELECT * FROM original_snapshot_component "
                "WHERE plan_id = ? AND component_name = 'policy'",
                (plan_id,),
            )
        else:
            conn.execute("ALTER TABLE workflow_run_plan_snapshot RENAME TO original_run_snapshot")
            conn.execute("CREATE TABLE workflow_run_plan_snapshot (run_id TEXT, plan_id TEXT)")
            conn.execute(
                "INSERT INTO workflow_run_plan_snapshot SELECT * FROM original_run_snapshot"
            )
            conn.execute(
                "INSERT INTO workflow_run_plan_snapshot VALUES (?, ?)",
                ("duplicate-row", plan_id),
            )

    with pytest.raises(snapshots.PrivateSnapshotIntegrityError):
        snapshots.stored_plan_components_for_run("duplicate-row")


@pytest.mark.parametrize("malformed", ["name", "digest"])
def test_stored_plan_components_refuses_invalid_row_element_types(
    malformed: str, _private_db: Path
):
    components, materials = _plan_for_inputs({})
    _seed_run("malformed-row")
    plan_id = snapshots.freeze_and_attach("malformed-row", components, materials)
    with sqlite3.connect(_private_db) as conn:
        value = sqlite3.Binary(b"not-text")
        column = "component_name" if malformed == "name" else "content_digest"
        conn.execute(
            f"UPDATE workflow_plan_snapshot_component SET {column} = ? "
            "WHERE plan_id = ? AND component_name = 'policy'",
            (value, plan_id),
        )

    with pytest.raises(snapshots.PrivateSnapshotIntegrityError):
        snapshots.stored_plan_components_for_run("malformed-row")


def test_stored_plan_components_cold_read_creates_nothing(_private_db: Path):
    with pytest.raises(snapshots.PrivateSnapshotIntegrityError, match="run reference is missing"):
        snapshots.stored_plan_components_for_run("cold-components")

    assert not _private_db.exists()
    assert not any(path.exists() for path in snapshots._database_files(_private_db))


def test_stored_plan_components_refuses_unencodable_run_id_without_echo(
    _private_db: Path,
):
    components, materials = _plan_for_inputs({})
    _seed_run("safe-run")
    snapshots.freeze_and_attach("safe-run", components, materials)
    invalid_run_id = "private-\ud800-value"

    with pytest.raises(snapshots.PrivateSnapshotIntegrityError) as excinfo:
        snapshots.stored_plan_components_for_run(invalid_run_id)

    assert invalid_run_id not in str(excinfo.value)


def test_stored_plan_components_never_migrates_or_repairs_permissions(
    _private_db: Path, monkeypatch: pytest.MonkeyPatch
):
    components, materials = _plan_for_inputs({})
    _seed_run("strict-components-read")
    snapshots.freeze_and_attach("strict-components-read", components, materials)
    before = _private_db.stat()
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database._migrate_workflow_plan_snapshot",
        Mock(side_effect=AssertionError("read must not migrate")),
    )
    monkeypatch.setattr(
        snapshots.os,
        "chmod",
        Mock(side_effect=AssertionError("read must not repair permissions")),
    )

    assert snapshots.stored_plan_components_for_run("strict-components-read") == (
        pi.compute_v2(components),
        components,
    )

    after = _private_db.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def test_material_kind_table_decodes_verified_snapshot_from_real_input_digests():
    inputs = {"token": SECRET, "count": 600.0, "nested": {"enabled": True}}
    materials = {
        name: value
        for name, value in _materials().items()
        if not name.startswith(("input-key:", "input-value:"))
    }
    for item in pi.digest_inputs(inputs):
        key = next(
            input_key
            for input_key in inputs
            if pi.digest_bytes(pi.canonical_key_bytes(input_key)) == item.key_digest
        )
        materials[f"input-key:{item.key_digest}"] = pi.canonical_key_bytes(key)
        materials[f"input-value:{item.key_digest}"] = pi.canonical_component_bytes(inputs[key])
    components = pi.PlanV2Components(
        tier="script",
        artifact_hash=pi.digest_bytes(materials["artifact_hash"]),
        declaration=pi.digest_bytes(materials["declaration"]),
        targets=pi.digest_bytes(materials["targets"]),
        inputs=pi.digest_inputs(inputs),
        limits=pi.digest_bytes(materials["limits"]),
        retry_policy=pi.digest_bytes(materials["retry_policy"]),
        policy=pi.digest_bytes(materials["policy"]),
        memory=pi.digest_bytes(materials["memory"]),
    )
    _seed_run("decode-kinds")
    snapshots.freeze_and_attach("decode-kinds", components, materials)

    verified = snapshots.private_snapshot_for_run("decode-kinds", components)

    assert verified.decode_material("artifact_hash") == materials["artifact_hash"]
    assert verified.decode_material("targets") == {"main": {"path": PRIVATE_PATH}}
    for item in components.inputs:
        key = verified.decode_material(f"input-key:{item.key_digest}")
        assert verified.decode_material(f"input-value:{item.key_digest}") == inputs[key]


def test_component_material_kind_table_is_the_pinned_public_producer_contract():
    assert snapshots.COMPONENT_MATERIAL_KINDS == {
        "artifact_hash": "source-bytes",
        "declaration": "json-utf8",
        "targets": "json-utf8",
        "limits": "json-utf8",
        "retry_policy": "json-utf8",
        "policy": "json-utf8",
        "memory": "json-utf8",
    }


def test_structured_material_contract_is_sorted_compact_json_not_typed_digest_encoding():
    value = {"z": [600.0, True], "a": "ü"}
    material = snapshots.structured_material_bytes(value)

    assert material == json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    assert pi.digest_bytes(material) != pi.digest_json(value)
    assert snapshots.decode_material("policy", material) == value


@pytest.mark.parametrize("value", [float("nan"), object()])
def test_structured_material_encoder_refuses_unsupported_values_without_echo(value):
    with pytest.raises(snapshots.PrivateSnapshotIntegrityError) as excinfo:
        snapshots.structured_material_bytes(value)

    assert SECRET not in str(excinfo.value)
    assert repr(value) not in str(excinfo.value)


@pytest.mark.parametrize(
    ("name", "material"),
    [
        ("policy", ('{"z":1,"a":"' + SECRET + '"}').encode()),
        ("policy", b'{"secret":NaN}'),
        ("input-value:abc", b"i2:+1"),
        ("input-key:abc", b"\xff"),
        ("unknown", SECRET.encode()),
    ],
)
def test_material_decoder_refuses_noncanonical_or_unknown_material_secret_safely(
    name: str, material: bytes
):
    with pytest.raises(snapshots.PrivateSnapshotIntegrityError) as excinfo:
        snapshots.decode_material(name, material)

    assert SECRET not in str(excinfo.value)
    assert PRIVATE_PATH not in str(excinfo.value)


def test_store_restricts_the_shared_database_to_owner_only(_private_db: Path):
    _store()
    assert _private_db.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("failure", ["missing", "tampered", "digest"])
def test_missing_corrupt_or_digest_mismatched_snapshot_refuses_closed(
    failure: str, _private_db: Path
):
    plan_id, components, _ = _store()
    with sqlite3.connect(_private_db) as conn:
        if failure == "missing":
            conn.execute(
                "DELETE FROM workflow_plan_snapshot_component "
                "WHERE plan_id = ? AND component_name = 'policy'",
                (plan_id,),
            )
        elif failure == "tampered":
            conn.execute(
                "UPDATE workflow_plan_snapshot_component SET content = ? "
                "WHERE plan_id = ? AND component_name = 'policy'",
                ("tampered private prompt", plan_id),
            )
        else:
            conn.execute(
                "UPDATE workflow_plan_snapshot_component SET content_digest = ? "
                "WHERE plan_id = ? AND component_name = 'policy'",
                ("0" * 64, plan_id),
            )

    with pytest.raises(snapshots.PrivateSnapshotIntegrityError) as excinfo:
        snapshots.private_snapshot_for_run("snapshot-run", components)
    message = str(excinfo.value)
    assert "policy" in message
    assert SECRET not in message
    assert PRIVATE_PATH not in message
    assert "private prompt" not in message


def test_unexpected_private_material_refuses_without_echoing_it():
    materials = _materials(**{"unexpected": b"do-not-echo-this"})
    components = _components(materials)
    with pytest.raises(snapshots.PrivateSnapshotIntegrityError) as excinfo:
        snapshots.freeze_and_attach("missing-run", components, materials)
    assert "do-not-echo-this" not in str(excinfo.value)


def test_oversize_component_refuses_instead_of_truncating():
    secret_prefix = b"private-oversize-material:"
    materials = _materials(policy=secret_prefix + b"x" * WORKFLOW_MANIFEST_MAX_BYTES)
    components = _components(materials)

    with pytest.raises(snapshots.PrivateSnapshotTooLargeError) as excinfo:
        snapshots.freeze_and_attach("missing-run", components, materials)

    assert "policy" in str(excinfo.value)
    assert str(WORKFLOW_MANIFEST_MAX_BYTES) in str(excinfo.value)
    assert secret_prefix.decode() not in str(excinfo.value)


def test_total_snapshot_ceiling_refuses_even_when_each_component_is_under_limit():
    materials = _materials(
        artifact_hash=b"a" * 40_000,
        declaration=b"b" * 40_000,
        targets=b"c" * 40_000,
        limits=b"d" * 40_000,
        retry_policy=b"e" * 40_000,
        policy=b"f" * 40_000,
        memory=b"g" * 40_000,
    )
    components = _components(materials)

    with pytest.raises(snapshots.PrivateSnapshotTooLargeError, match="total snapshot"):
        snapshots.freeze_and_attach("missing-run", components, materials)


def test_two_runs_share_snapshot_and_one_run_cleanup_cannot_invalidate_the_other(
    _private_db: Path,
):
    plan_id, components, materials = _store("run-a")
    _seed_run("run-b")
    assert snapshots.freeze_and_attach("run-b", components, materials) == plan_id

    workflow_journal.delete_run("run-a")

    assert dict(snapshots.private_snapshot_for_run("run-b", components).components) == materials
    with sqlite3.connect(_private_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM workflow_run_plan_snapshot WHERE plan_id = ?",
            (plan_id,),
        ).fetchone() == (1,)

    workflow_journal.delete_run("run-b")
    with pytest.raises(snapshots.PrivateSnapshotIntegrityError, match="missing"):
        snapshots.private_snapshot_for_run("run-b", components)


def test_failed_attach_to_missing_run_leaves_no_snapshot_rows(_private_db: Path):
    materials = _materials()
    components = _components(materials)

    with pytest.raises(snapshots.PrivateSnapshotIntegrityError, match="run reference"):
        snapshots.freeze_and_attach("run-does-not-exist", components, materials)

    with sqlite3.connect(_private_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM workflow_plan_snapshot").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM workflow_plan_snapshot_component").fetchone() == (
            0,
        )
        assert conn.execute("SELECT COUNT(*) FROM workflow_run_plan_snapshot").fetchone() == (0,)


def test_last_reference_gc_then_refreeze_same_plan_and_bytes(_private_db: Path):
    plan_id, components, materials = _store("run-a")
    workflow_journal.delete_run("run-a")
    _seed_run("run-b")

    assert snapshots.freeze_and_attach("run-b", components, materials) == plan_id
    assert dict(snapshots.private_snapshot_for_run("run-b", components).components) == materials


def test_component_rows_are_not_rendered_by_any_public_snapshot_helper():
    plan_id, components, materials = _store()
    public = json.dumps(pi.v2_component_document(components), sort_keys=True)
    assert plan_id.startswith("plan-v2:")
    for raw in materials.values():
        assert raw.decode(errors="ignore") not in public


def test_owner_only_readonly_mode_is_accepted_without_chmod(
    _private_db: Path, monkeypatch: pytest.MonkeyPatch
):
    _, components, materials = _store("readonly-run")
    os.chmod(_private_db, 0o400)
    chmod = Mock(side_effect=AssertionError("owner-only mode must not be rewritten"))
    monkeypatch.setattr(snapshots.os, "chmod", chmod)

    assert (
        dict(snapshots.private_snapshot_for_run("readonly-run", components).components) == materials
    )
    chmod.assert_not_called()


def test_cold_read_of_absent_database_refuses_without_creating_any_files(
    _private_db: Path,
):
    components = _components(_materials())

    with pytest.raises(
        snapshots.PrivateSnapshotIntegrityError, match="run reference is missing"
    ) as excinfo:
        snapshots.private_snapshot_for_run("cold-read", components)

    rendered = "".join(traceback.format_exception(excinfo.type, excinfo.value, excinfo.tb))
    assert excinfo.value.__suppress_context__ is True
    assert str(_private_db) not in rendered
    assert not _private_db.exists()
    assert not any(path.exists() for path in snapshots._database_files(_private_db))


def test_cold_read_of_existing_database_without_snapshot_tables_is_missing_reference(
    _private_db: Path,
):
    sqlite3.connect(_private_db).close()
    os.chmod(_private_db, 0o600)

    with pytest.raises(snapshots.PrivateSnapshotIntegrityError, match="run reference is missing"):
        snapshots.private_snapshot_for_run("cold-read", _components(_materials()))


def test_read_database_stat_failure_is_store_error_not_missing_reference(
    _private_db: Path, monkeypatch: pytest.MonkeyPatch
):
    original_stat = Path.stat

    def _fail_database_stat(path: Path, *args, **kwargs):
        if path == _private_db:
            raise PermissionError
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _fail_database_stat)

    with pytest.raises(snapshots.PrivateSnapshotStoreError) as excinfo:
        snapshots.private_snapshot_for_run("stat-failure", _components(_materials()))

    assert type(excinfo.value) is snapshots.PrivateSnapshotStoreError


def test_permission_tightening_fails_closed_when_file_cannot_be_statted(
    _private_db: Path, monkeypatch: pytest.MonkeyPatch
):
    original_stat = Path.stat

    def _fail_database_stat(path: Path, *args, **kwargs):
        if path == _private_db:
            raise PermissionError
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _fail_database_stat)

    assert snapshots._tighten_permissions(_private_db) is False


def test_successful_read_is_read_only_and_never_runs_migration(
    _private_db: Path, monkeypatch: pytest.MonkeyPatch
):
    _, components, materials = _store("strict-read")
    before = _private_db.stat()
    sidecars = snapshots._database_files(_private_db)[1:]
    assert not any(path.exists() for path in sidecars)

    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database._migrate_workflow_plan_snapshot",
        Mock(side_effect=AssertionError("read must not migrate")),
    )

    assert (
        dict(snapshots.private_snapshot_for_run("strict-read", components).components) == materials
    )
    after = _private_db.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert not any(path.exists() for path in sidecars)


def test_read_only_connection_refuses_database_writes(_private_db: Path):
    _, _, _ = _store("read-only-sql")

    with closing(snapshots._connect_readonly()) as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE forbidden_write (value TEXT)")


def test_read_only_uri_handles_percent_encoded_database_paths(
    _private_db: Path, monkeypatch: pytest.MonkeyPatch
):
    escaped_path = _private_db.parent / "private store #1" / "workflow db.sqlite"
    escaped_path.parent.mkdir()
    monkeypatch.setattr(
        "cli_agent_orchestrator.constants.DATABASE_FILE", escaped_path, raising=True
    )
    _, components, materials = _store("escaped-uri")

    assert (
        dict(snapshots.private_snapshot_for_run("escaped-uri", components).components) == materials
    )


def test_group_readable_mode_is_tightened_before_private_bytes_are_written(
    _private_db: Path,
):
    materials = _materials()
    components = _components(materials)
    _seed_run("tighten-run")
    os.chmod(_private_db, 0o640)

    snapshots.freeze_and_attach("tighten-run", components, materials)
    assert _private_db.stat().st_mode & 0o777 == 0o600


def test_write_repairs_main_journal_wal_and_shm_before_private_bytes(_private_db: Path):
    materials = _materials()
    components = _components(materials)
    _seed_run("repair-sidecars")
    sidecars = snapshots._database_files(_private_db)
    for path in sidecars[1:]:
        path.touch(mode=0o644)
    for path in sidecars:
        os.chmod(path, 0o644)

    snapshots.freeze_and_attach("repair-sidecars", components, materials)

    for path in sidecars:
        if path.exists():
            assert path.stat().st_mode & 0o077 == 0


def test_group_readable_mode_is_refused_on_read_without_chmod(
    _private_db: Path, monkeypatch: pytest.MonkeyPatch
):
    _, components, _ = _store("permission-run")
    os.chmod(_private_db, 0o644)
    chmod = Mock(side_effect=AssertionError("read must not repair permissions"))
    monkeypatch.setattr(snapshots.os, "chmod", chmod)

    with pytest.raises(snapshots.PrivateSnapshotPermissionsError) as excinfo:
        snapshots.private_snapshot_for_run("permission-run", components)
    assert str(excinfo.value) == ("private snapshot store not owner-only; restrict database file")
    assert _private_db.stat().st_mode & 0o777 == 0o644
    chmod.assert_not_called()


def test_read_refuses_exposed_sidecar_without_chmod(
    _private_db: Path, monkeypatch: pytest.MonkeyPatch
):
    _, components, _ = _store("exposed-sidecar")
    sidecar = _private_db.with_name(_private_db.name + "-wal")
    sidecar.touch(mode=0o644)
    os.chmod(sidecar, 0o644)
    chmod = Mock(side_effect=AssertionError("read must not repair permissions"))
    monkeypatch.setattr(snapshots.os, "chmod", chmod)

    with pytest.raises(snapshots.PrivateSnapshotPermissionsError):
        snapshots.private_snapshot_for_run("exposed-sidecar", components)

    assert sidecar.stat().st_mode & 0o777 == 0o644
    chmod.assert_not_called()


def test_read_refuses_exposed_live_rollback_journal_without_chmod(
    _private_db: Path, monkeypatch: pytest.MonkeyPatch
):
    _, components, _ = _store("live-journal-read")
    journal = _private_db.with_name(_private_db.name + "-journal")
    with sqlite3.connect(_private_db) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE workflow_run SET state = 'failed' WHERE run_id = 'live-journal-read'"
        )
        if not journal.exists():
            pytest.skip("SQLite platform did not produce a rollback journal")
        os.chmod(journal, 0o644)
        monkeypatch.setattr(
            snapshots.os,
            "chmod",
            Mock(side_effect=AssertionError("read must not repair permissions")),
        )

        with pytest.raises(snapshots.PrivateSnapshotPermissionsError):
            snapshots.private_snapshot_for_run("live-journal-read", components)

        assert journal.stat().st_mode & 0o777 == 0o644
        writer.rollback()


def test_borrowed_write_repairs_live_rollback_journal_before_private_bytes(
    _private_db: Path,
):
    materials = _materials()
    components = _components(materials)
    _seed_run("live-journal-write")
    _migrate_workflow_plan_snapshot()
    journal = _private_db.with_name(_private_db.name + "-journal")

    with sqlite3.connect(_private_db) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE workflow_run SET state = 'failed' WHERE run_id = 'live-journal-write'"
        )
        if not journal.exists():
            pytest.skip("SQLite platform did not produce a rollback journal")
        os.chmod(journal, 0o644)

        snapshots.freeze_and_attach("live-journal-write", components, materials, conn=writer)

        assert journal.stat().st_mode & 0o077 == 0
        assert writer.execute(
            "SELECT COUNT(*) FROM workflow_plan_snapshot_component"
        ).fetchone() == (len(materials),)
        writer.rollback()


def test_stale_rollback_journal_is_secured_or_consumed_before_private_bytes(
    _private_db: Path, monkeypatch: pytest.MonkeyPatch
):
    materials = _materials()
    components = _components(materials)
    _seed_run("stale-journal")
    journal = _private_db.with_name(_private_db.name + "-journal")
    journal.touch(mode=0o644)
    os.chmod(journal, 0o644)
    store_or_verify = snapshots._store_or_verify

    def _assert_secured_before_store(*args, **kwargs):
        assert not journal.exists() or journal.stat().st_mode & 0o077 == 0
        return store_or_verify(*args, **kwargs)

    monkeypatch.setattr(snapshots, "_store_or_verify", _assert_secured_before_store)

    snapshots.freeze_and_attach("stale-journal", components, materials)

    if journal.exists():
        assert journal.stat().st_mode & 0o077 == 0


def test_failed_write_permission_repair_refuses_before_private_bytes(
    _private_db: Path, monkeypatch: pytest.MonkeyPatch
):
    materials = _materials()
    components = _components(materials)
    _seed_run("permission-write")
    os.chmod(_private_db, 0o644)
    monkeypatch.setattr(snapshots.os, "chmod", lambda *_args: None)

    with pytest.raises(snapshots.PrivateSnapshotPermissionsError) as excinfo:
        snapshots.freeze_and_attach("permission-write", components, materials)
    assert str(excinfo.value) == ("private snapshot store not owner-only; restrict database file")
    assert SECRET not in str(excinfo.value)
    assert PRIVATE_PATH not in str(excinfo.value)
    with sqlite3.connect(_private_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM workflow_plan_snapshot").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM workflow_plan_snapshot_component").fetchone() == (
            0,
        )


def test_delete_run_repairs_exposed_store_then_removes_run_reference_and_bytes(
    _private_db: Path,
):
    plan_id, _, _ = _store("delete-exposed")
    os.chmod(_private_db, 0o644)

    workflow_journal.delete_run("delete-exposed")

    assert _private_db.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(_private_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM workflow_run WHERE run_id = 'delete-exposed'"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM workflow_run_plan_snapshot WHERE run_id = 'delete-exposed'"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM workflow_plan_snapshot_component WHERE plan_id = ?",
            (plan_id,),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM workflow_plan_snapshot WHERE plan_id = ?", (plan_id,)
        ).fetchone() == (0,)


def test_delete_run_warns_once_but_still_purges_when_permissions_cannot_be_repaired(
    _private_db: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    plan_id, _, _ = _store("delete-unrepairable")
    os.chmod(_private_db, 0o644)
    monkeypatch.setattr(snapshots.os, "chmod", lambda *_args: None)

    with caplog.at_level("WARNING", logger=snapshots.__name__):
        workflow_journal.delete_run("delete-unrepairable")

    assert [
        record.getMessage() for record in caplog.records if record.name == snapshots.__name__
    ] == ["private snapshot store not owner-only; releasing raw bytes anyway"]
    with sqlite3.connect(_private_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM workflow_run WHERE run_id = 'delete-unrepairable'"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM workflow_run_plan_snapshot WHERE run_id = 'delete-unrepairable'"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM workflow_plan_snapshot_component WHERE plan_id = ?",
            (plan_id,),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM workflow_plan_snapshot WHERE plan_id = ?", (plan_id,)
        ).fetchone() == (0,)


def test_unknown_release_on_exposed_store_does_not_chmod_or_change_rows(
    _private_db: Path, monkeypatch: pytest.MonkeyPatch
):
    _store("known-reference")
    os.chmod(_private_db, 0o644)
    with sqlite3.connect(_private_db) as conn:
        before = tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "workflow_plan_snapshot",
                "workflow_plan_snapshot_component",
                "workflow_run_plan_snapshot",
            )
        )
    chmod = Mock(side_effect=AssertionError("unknown release must not repair permissions"))
    monkeypatch.setattr(snapshots.os, "chmod", chmod)

    snapshots.release_run_reference("unknown-reference")

    with sqlite3.connect(_private_db) as conn:
        after = tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "workflow_plan_snapshot",
                "workflow_plan_snapshot_component",
                "workflow_run_plan_snapshot",
            )
        )
    assert after == before
    assert _private_db.stat().st_mode & 0o777 == 0o644
    chmod.assert_not_called()


def test_owned_release_on_absent_database_is_noop_without_creating_files(
    _private_db: Path,
):
    snapshots.release_run_reference("unknown-reference")

    assert not _private_db.exists()
    assert not any(path.exists() for path in snapshots._database_files(_private_db))


def test_manual_release_protocol_is_required_when_foreign_keys_are_disabled(
    _private_db: Path,
):
    plan_id, _, _ = _store("manual-release")

    with sqlite3.connect(_private_db) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (0,)
        conn.execute("DELETE FROM workflow_run WHERE run_id = 'manual-release'")
        assert conn.execute(
            "SELECT COUNT(*) FROM workflow_run_plan_snapshot WHERE run_id = 'manual-release'"
        ).fetchone() == (1,)

        snapshots.release_run_reference("manual-release", conn)

        assert conn.execute(
            "SELECT COUNT(*) FROM workflow_run_plan_snapshot WHERE run_id = 'manual-release'"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM workflow_plan_snapshot_component WHERE plan_id = ?",
            (plan_id,),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM workflow_plan_snapshot WHERE plan_id = ?", (plan_id,)
        ).fetchone() == (0,)


def test_borrowed_release_preserves_caller_transaction_rollback(_private_db: Path):
    _, _, _ = _store("rollback-run")
    with sqlite3.connect(_private_db) as conn:
        conn.execute("UPDATE workflow_run SET state = 'failed' WHERE run_id = 'rollback-run'")
        snapshots.release_run_reference("rollback-run", conn)
        conn.rollback()

    run = workflow_journal.get_run("rollback-run")
    assert run is not None
    assert run.state == "running"
    with sqlite3.connect(_private_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM workflow_run_plan_snapshot WHERE run_id = 'rollback-run'"
        ).fetchone() == (1,)


def test_borrowed_freeze_composes_with_uncommitted_run_creation(_private_db: Path):
    materials = _materials()
    components = _components(materials)
    _migrate_workflow_run()
    _migrate_workflow_plan_snapshot()
    with sqlite3.connect(_private_db) as conn:
        conn.execute(
            "INSERT INTO workflow_run "
            "(run_id, workflow_name, spec_snapshot, inputs_json, state, current_step_id, "
            "started_at, finished_at, tier, generation, manifest_json) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, NULL)",
            (
                "borrowed-run",
                "wf",
                "source",
                "{}",
                "running",
                "2026-09-16T00:00:00Z",
                "script",
                "1",
            ),
        )
        plan_id = snapshots.freeze_and_attach("borrowed-run", components, materials, conn=conn)
        assert plan_id == pi.compute_v2(components)
        assert conn.execute(
            "SELECT COUNT(*) FROM workflow_run_plan_snapshot " "WHERE run_id = 'borrowed-run'"
        ).fetchone() == (1,)
        conn.rollback()

    assert workflow_journal.get_run("borrowed-run") is None
    with sqlite3.connect(_private_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM workflow_plan_snapshot").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM workflow_plan_snapshot_component").fetchone() == (
            0,
        )
        assert conn.execute("SELECT COUNT(*) FROM workflow_run_plan_snapshot").fetchone() == (0,)


def test_failed_borrowed_freeze_then_caller_rollback_leaves_nothing(_private_db: Path):
    _, components, materials = _store("trigger-setup")
    workflow_journal.delete_run("trigger-setup")
    _migrate_workflow_plan_snapshot()
    with sqlite3.connect(_private_db) as conn:
        conn.execute(
            "CREATE TRIGGER fail_snapshot_attach "
            "BEFORE INSERT ON workflow_run_plan_snapshot "
            "BEGIN SELECT RAISE(ABORT, 'forced attach failure'); END"
        )

    with sqlite3.connect(_private_db) as conn:
        conn.execute(
            "INSERT INTO workflow_run "
            "(run_id, workflow_name, spec_snapshot, inputs_json, state, current_step_id, "
            "started_at, finished_at, tier, generation, manifest_json) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, NULL)",
            (
                "failed-borrowed-run",
                "wf",
                "source",
                "{}",
                "running",
                "2026-09-16T00:00:00Z",
                "script",
                "1",
            ),
        )
        with pytest.raises(snapshots.PrivateSnapshotStoreError):
            snapshots.freeze_and_attach("failed-borrowed-run", components, materials, conn=conn)
        conn.rollback()

    assert workflow_journal.get_run("failed-borrowed-run") is None
    with sqlite3.connect(_private_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM workflow_plan_snapshot").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM workflow_plan_snapshot_component").fetchone() == (
            0,
        )
        assert conn.execute("SELECT COUNT(*) FROM workflow_run_plan_snapshot").fetchone() == (0,)


def test_later_delete_failure_rolls_back_run_and_snapshot_reference(
    _private_db: Path, monkeypatch: pytest.MonkeyPatch
):
    _store("delete-failure")

    def _fail_after_release(*_args, **_kwargs):
        raise sqlite3.OperationalError("forced cascade failure")

    monkeypatch.setattr(workflow_journal, "delete_run_events", _fail_after_release)
    with pytest.raises(sqlite3.OperationalError):
        workflow_journal.delete_run("delete-failure")

    assert workflow_journal.get_run("delete-failure") is not None
    with sqlite3.connect(_private_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM workflow_run_plan_snapshot WHERE run_id = 'delete-failure'"
        ).fetchone() == (1,)


def test_self_connecting_read_and_release_close_connections(
    _private_db: Path, monkeypatch: pytest.MonkeyPatch
):
    _, components, _ = _store("close-read")
    _seed_run("close-release")
    snapshots.freeze_and_attach("close-release", components, _materials())
    opened: list[sqlite3.Connection] = []

    def _tracked_connect() -> sqlite3.Connection:
        conn = sqlite3.connect(_private_db)
        opened.append(conn)
        return conn

    monkeypatch.setattr(snapshots, "_connect", _tracked_connect)
    monkeypatch.setattr(snapshots, "_connect_readonly", _tracked_connect)
    snapshots.private_snapshot_for_run("close-read", components)
    snapshots.release_run_reference("close-release")

    assert len(opened) == 2
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


def test_only_atomic_freeze_attach_is_a_public_snapshot_write():
    assert not hasattr(snapshots, "store_private_snapshot")
    assert not hasattr(snapshots, "attach_private_snapshot")


def test_schema_setup_never_uses_executescript():
    source = Path(snapshots.__file__).read_text(encoding="utf-8")
    assert "executescript" not in source
    assert "CREATE TABLE" not in source


def test_delete_unknown_run_on_cold_database_is_noop_and_migrates_snapshot_schema(
    _private_db: Path,
):
    workflow_journal.delete_run("never-existed")

    with sqlite3.connect(_private_db) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE 'workflow_%plan_snapshot%'"
            )
        }
        assert tables == {
            "workflow_plan_snapshot",
            "workflow_plan_snapshot_component",
            "workflow_run_plan_snapshot",
        }
        component_fks = conn.execute(
            "PRAGMA foreign_key_list(workflow_plan_snapshot_component)"
        ).fetchall()
        reference_fks = conn.execute(
            "PRAGMA foreign_key_list(workflow_run_plan_snapshot)"
        ).fetchall()
        assert {(row[2], row[3], row[4], row[6]) for row in component_fks} == {
            ("workflow_plan_snapshot", "plan_id", "plan_id", "CASCADE")
        }
        assert {(row[2], row[3], row[4], row[6]) for row in reference_fks} == {
            ("workflow_plan_snapshot", "plan_id", "plan_id", "RESTRICT"),
            ("workflow_run", "run_id", "run_id", "CASCADE"),
        }
