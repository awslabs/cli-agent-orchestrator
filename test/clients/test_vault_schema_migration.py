"""U2 schema migration tests for the vault source discriminator."""

import ast
import inspect
import sqlite3
import textwrap
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as db_mod
from cli_agent_orchestrator.clients.database import (
    VAULT_NOTE_SCOPE_ID_SENTINEL,
    MemoryMetadataModel,
    VaultNoteModel,
)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "vault-schema.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(bind=engine))
    yield db_path, engine
    engine.dispose()


def _secondary_indexes(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {
            row[1]
            for row in conn.execute("PRAGMA index_list(memory_metadata)")
            if row[1] in {"idx_memory_scope", "idx_memory_updated", "idx_memory_type"}
        }


def _create_legacy_memory_metadata(db_path: Path, *, related_keys: str | None = None) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE memory_metadata (
                id VARCHAR NOT NULL PRIMARY KEY,
                key VARCHAR NOT NULL,
                memory_type VARCHAR NOT NULL,
                scope VARCHAR NOT NULL,
                scope_id VARCHAR,
                file_path VARCHAR NOT NULL,
                tags VARCHAR NOT NULL,
                source_provider VARCHAR,
                source_terminal_id VARCHAR,
                token_estimate INTEGER,
                created_at DATETIME,
                updated_at DATETIME,
                access_count INTEGER NOT NULL DEFAULT 0,
                last_accessed_at DATETIME,
                last_compiled_at DATETIME,
                related_keys TEXT,
                CONSTRAINT uq_memory_key_scope UNIQUE (key, scope, scope_id)
            )
            """)
        conn.execute(
            """
            INSERT INTO memory_metadata (
                id, key, memory_type, scope, scope_id, file_path, tags, related_keys
            ) VALUES (?, 'legacy', 'project', 'global', NULL, '/legacy.md', '', ?)
            """,
            (str(uuid.uuid4()), related_keys),
        )


def _insert(engine, *, key: str, source_kind: str) -> None:
    with sessionmaker(bind=engine)() as session:
        session.add(
            MemoryMetadataModel(
                id=str(uuid.uuid4()),
                key=key,
                memory_type="project",
                scope="project",
                scope_id="project-id",
                source_kind=source_kind,
                file_path=f"/{source_kind}-{key}.md",
                tags="",
            )
        )
        session.commit()


def test_fresh_database_has_vault_schema_and_secondary_indexes(isolated_db):
    db_path, _ = isolated_db

    db_mod.init_db()

    with sqlite3.connect(db_path) as conn:
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(memory_metadata)")}
        vault_note_columns = {row[1]: row for row in conn.execute("PRAGMA table_info(vault_note)")}
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert columns["source_kind"][3] == 1
    assert columns["source_kind"][4] == "'native'"
    assert vault_note_columns["scope"][3] == 1
    assert vault_note_columns["scope_id"][3] == 1
    assert {"vault_note", "vault_finding", "vault_note_alias"} <= tables
    assert _secondary_indexes(db_path) == {
        "idx_memory_scope",
        "idx_memory_updated",
        "idx_memory_type",
    }


def test_legacy_database_rebuilds_once_preserves_rows_and_indexes(isolated_db, monkeypatch):
    db_path, _ = isolated_db
    _create_legacy_memory_metadata(db_path)
    rebuilds = 0
    original_info = db_mod.logger.info

    def count_rebuilds(message, *args, **kwargs):
        nonlocal rebuilds
        if message == "Migration: widened memory_metadata identity with source_kind":
            rebuilds += 1
        return original_info(message, *args, **kwargs)

    monkeypatch.setattr(db_mod.logger, "info", count_rebuilds)

    db_mod.init_db()
    db_mod.init_db()

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT key, source_kind FROM memory_metadata WHERE key = 'legacy'"
        ).fetchone()
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(memory_metadata)")}
    assert row == ("legacy", "native")
    assert columns["source_kind"][3] == 1
    assert columns["source_kind"][4] == "'native'"
    assert rebuilds == 1
    assert _secondary_indexes(db_path) == {
        "idx_memory_scope",
        "idx_memory_updated",
        "idx_memory_type",
    }


def test_widened_constraint_allows_backends_but_rejects_native_duplicates(isolated_db):
    _, engine = isolated_db
    db_mod.init_db()

    _insert(engine, key="shared", source_kind="native")
    _insert(engine, key="shared", source_kind="vault")

    with pytest.raises(IntegrityError):
        _insert(engine, key="shared", source_kind="native")


@pytest.mark.xfail(
    strict=True,
    reason="#657: memory_metadata's nullable global scope_id makes SQLite UNIQUE non-total",
)
def test_global_native_duplicate_remains_rejected_after_widening(isolated_db):
    _, engine = isolated_db
    db_mod.init_db()

    with sessionmaker(bind=engine)() as session:
        session.add(
            MemoryMetadataModel(
                id=str(uuid.uuid4()),
                key="global-shared",
                memory_type="project",
                scope="global",
                scope_id=None,
                source_kind="native",
                file_path="/global-shared.md",
                tags="",
            )
        )
        session.commit()

    with pytest.raises(IntegrityError):
        with sessionmaker(bind=engine)() as session:
            session.add(
                MemoryMetadataModel(
                    id=str(uuid.uuid4()),
                    key="global-shared",
                    memory_type="project",
                    scope="global",
                    scope_id=None,
                    source_kind="native",
                    file_path="/global-shared-duplicate.md",
                    tags="",
                )
            )
            session.commit()


def test_rebuild_omits_related_keys_check_and_preserves_overlong_value(isolated_db):
    db_path, _ = isolated_db
    overlong = "x" * 2048
    _create_legacy_memory_metadata(db_path, related_keys=overlong)

    db_mod.init_db()

    with sqlite3.connect(db_path) as conn:
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(memory_metadata)")}
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'memory_metadata'"
        ).fetchone()[0]
        stored = conn.execute(
            "SELECT related_keys FROM memory_metadata WHERE key = 'legacy'"
        ).fetchone()[0]
    # Existing databases omit the fresh-install CHECK so legacy overlong values
    # survive; fresh databases retain the model-level CHECK.
    assert "source_kind" in columns
    assert columns["source_kind"][3] == 1
    assert columns["source_kind"][4] == "'native'"
    assert "ck_related_keys_length" not in ddl
    assert stored == overlong


def test_vault_note_global_identity_uses_non_null_scope_id_sentinel(isolated_db):
    _, engine = isolated_db
    db_mod.init_db()

    with sessionmaker(bind=engine)() as session:
        session.add(
            VaultNoteModel(
                note_uid="first",
                vault_id="primary",
                scope="global",
                scope_id=VAULT_NOTE_SCOPE_ID_SENTINEL,
                cao_key="glossary",
                vault_relpath="Reference/Glossary.md",
                managed=False,
                status="indexed",
            )
        )
        session.commit()

    with pytest.raises(IntegrityError):
        with sessionmaker(bind=engine)() as session:
            session.add(
                VaultNoteModel(
                    note_uid="second",
                    vault_id="primary",
                    scope="global",
                    scope_id=VAULT_NOTE_SCOPE_ID_SENTINEL,
                    cao_key="glossary",
                    vault_relpath="Reference/Glossary-duplicate.md",
                    managed=False,
                    status="indexed",
                )
            )
            session.commit()


def test_source_kind_migration_failure_propagates(isolated_db):
    db_path, _ = isolated_db
    _create_legacy_memory_metadata(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE memory_metadata_new (id TEXT)")

    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        db_mod.init_db()

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_metadata)")}
    assert "source_kind" not in columns


def test_source_kind_migration_ignores_database_without_memory_metadata(isolated_db):
    db_path, _ = isolated_db
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE workflow_only (id TEXT PRIMARY KEY)")

    db_mod._migrate_memory_source_kind()


def _has_source_kind_equality(owner) -> bool:
    tree = ast.parse(textwrap.dedent(inspect.getsource(owner)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or not any(isinstance(op, ast.Eq) for op in node.ops):
            continue
        operands = [node.left, *node.comparators]
        if any(
            isinstance(operand, ast.Attribute) and operand.attr == "source_kind"
            for operand in operands
        ):
            return True
    return False


def test_all_key_and_scope_queries_filter_source_kind():
    from cli_agent_orchestrator.services import (
        memory_reconciliation,
        memory_relationship_service,
        memory_service,
        promotion_service,
        wiki_healer,
        wiki_lint,
    )

    query_owners = (
        memory_service.MemoryService._upsert_metadata,
        memory_service.MemoryService._delete_metadata,
        memory_service.MemoryService.compact,
        memory_service.MemoryService._candidate_keys_for_topic,
        memory_service.MemoryService._related_keys_lookup,
        memory_service.MemoryService._increment_access_count,
        memory_reconciliation.MemoryReconciliationService._load_rows,
        memory_reconciliation.MemoryReconciliationService._repair_metadata,
        memory_relationship_service.MemoryRelationshipService._assert_endpoint_exists,
        memory_relationship_service.MemoryRelationshipService._source_updated_map,
        wiki_healer._row_exists,
        wiki_healer._delete_row,
        wiki_healer._heal_contradiction,
        wiki_healer._heal_poison,
        promotion_service.PromotionService.plan,
        wiki_lint.run_lint,
    )

    for owner in query_owners:
        assert _has_source_kind_equality(owner), owner.__qualname__

    enrich_source = inspect.getsource(memory_service.MemoryService._enrich_access_counts)
    assert "r.source_kind" in enrich_source
    assert 'getattr(m, "source_kind", "native")' in inspect.getsource(
        memory_service.MemoryService._identity
    )


def test_binding_aware_query_helpers_default_to_native_without_call_site_changes():
    from cli_agent_orchestrator.services import (
        memory_relationship_service,
        memory_service,
    )

    helpers = (
        memory_service.MemoryService._upsert_metadata,
        memory_service.MemoryService._delete_metadata,
        memory_service.MemoryService.compact,
        memory_service.MemoryService._related_keys_lookup,
        memory_relationship_service.MemoryRelationshipService._assert_endpoint_exists,
        memory_relationship_service.MemoryRelationshipService._source_updated_map,
    )
    for helper in helpers:
        parameter = inspect.signature(helper).parameters["source_kind"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default == "native"

    for path, helper_names in {
        "src/cli_agent_orchestrator/services/memory_service.py": {
            "_upsert_metadata",
            "_delete_metadata",
            "_related_keys_lookup",
        },
        "src/cli_agent_orchestrator/services/memory_relationship_service.py": {
            "_assert_endpoint_exists",
            "_source_updated_map",
        },
    }.items():
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr not in helper_names:
                continue
            source_kind_keywords = [
                keyword for keyword in node.keywords if keyword.arg == "source_kind"
            ]
            if not source_kind_keywords:
                continue
            current = parents[node]
            while not isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                current = parents[current]
            kwonly_names = {argument.arg for argument in current.args.kwonlyargs}
            # A source-aware wrapper may forward its own keyword-only value.
            # A backend-matched consumer may instead derive the value from the
            # Memory row it is grouping. Literal or unrelated overrides would
            # weaken the native default at an existing call site. The
            # The deterministic renderer deliberately queries native and vault
            # relationship edges separately before expanding either source.
            # The vault deindex helper has already resolved a VaultBinding, so
            # its literal identifies the store it is removing from rather
            # than weakening a native-default query.
            assert len(source_kind_keywords) == 1
            source_kind_value = source_kind_keywords[0].value
            # Either injection entry point: PR #693 extracted
            # `get_memory_context(terminal_context)` out of
            # `get_memory_context_for_terminal(terminal_id)` for elastic worker
            # nodes, moving this dual-source query into the new name.
            explicit_renderer_source = (
                current.name in {"get_memory_context", "get_memory_context_for_terminal"}
                and isinstance(source_kind_value, ast.Constant)
                and source_kind_value.value in {"native", "vault"}
            )
            explicit_vault_deindex_source = (
                current.name == "_deindex_vault_memory"
                and isinstance(source_kind_value, ast.Constant)
                and source_kind_value.value in {"native", "vault"}
            )
            if explicit_renderer_source or explicit_vault_deindex_source:
                continue
            assert isinstance(source_kind_value, ast.Name), (
                "source_kind literals are allowed only for the deterministic "
                "renderer's dual-source query and VaultBinding-resolved deindexing"
            )
            source_kind_name = source_kind_value.id
            forwards_kwonly = source_kind_name == "source_kind" and "source_kind" in kwonly_names
            derives_from_memory = any(
                isinstance(assignment, (ast.Assign, ast.AnnAssign))
                and source_kind_name
                in {
                    target.id
                    for target in (
                        assignment.targets
                        if isinstance(assignment, ast.Assign)
                        else [assignment.target]
                    )
                    if isinstance(target, ast.Name)
                }
                and (
                    (
                        isinstance(assignment.value, ast.Attribute)
                        and assignment.value.attr == "source_kind"
                    )
                    or (
                        isinstance(assignment.value, ast.Call)
                        and isinstance(assignment.value.func, ast.Name)
                        and assignment.value.func.id == "getattr"
                        and len(assignment.value.args) >= 2
                        and isinstance(assignment.value.args[1], ast.Constant)
                        and assignment.value.args[1].value == "source_kind"
                        and (
                            len(assignment.value.args) < 3
                            or (
                                isinstance(assignment.value.args[2], ast.Constant)
                                and assignment.value.args[2].value == "native"
                            )
                        )
                    )
                )
                for assignment in ast.walk(current)
            )
            assert forwards_kwonly or derives_from_memory

    relationship_tree = ast.parse(
        Path("src/cli_agent_orchestrator/services/memory_relationship_service.py").read_text(
            encoding="utf-8"
        )
    )
    for node in ast.walk(relationship_tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "purge_for_key":
            continue
        assert not any(keyword.arg == "spare_origins" for keyword in node.keywords)
