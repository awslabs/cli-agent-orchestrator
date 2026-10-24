"""Opt-in supersedes exclusion, explicit-recall demotion and transient labels."""

from datetime import datetime, timedelta, timezone

from test.services.test_scoring import (  # isort: skip  (per-name noqa below; see the comment)
    _ctx,
    _run,
    _stamp_updated_at,
    db_engine,  # noqa: F401  # pytest fixture, injected by name
    svc,  # noqa: F401  # pytest fixture, injected by name
)
from test.services.vault.test_vault_injection_renderer import _injectable_renderer

import pytest
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import MemoryMetadataModel, MemoryRelationshipModel
from cli_agent_orchestrator.services import memory_relationship_service, settings_service


@pytest.fixture
def lifecycle(svc, db_engine, monkeypatch):
    factory = sessionmaker(bind=db_engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(memory_relationship_service, "SessionLocal", factory)
    monkeypatch.setenv("CAO_MEMORY_LIFECYCLE_ENABLED", "true")
    ctx = _ctx(caller_scope="global")
    now = datetime.now(timezone.utc)
    for key, days in [("blocked", 0), ("active", 1), ("replacement", 2)]:
        _run(
            svc.store(key=key, content="kafka streams " + key, scope="global", terminal_context=ctx)
        )
        when = now - timedelta(days=days)
        _stamp_updated_at(db_engine, key, "global", when)
        svc._update_index(
            "global",
            None,
            key,
            "reference",
            "",
            "kafka streams " + key,
            when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "update",
        )
    _edge(factory, "blocked")
    return svc, factory, ctx


def _edge(factory, target, scope="global", scope_id="", status="active"):
    with factory() as db:
        db.add(
            MemoryRelationshipModel(
                scope=scope,
                scope_id=scope_id,
                source_key="replacement",
                target_key=target,
                type="supersedes",
                status=status,
                origin="human",
            )
        )
        db.commit()


@pytest.mark.parametrize("path", ["native", "vault", "related", "curated"])
def test_superseded_excluded_from_injection(
    path, lifecycle, tmp_path, monkeypatch, isolated_memory_db
):
    service, factory, ctx = lifecycle
    if path == "vault":
        (tmp_path / "renderer").mkdir()
        service, factory, root, config = _injectable_renderer(tmp_path / "renderer", monkeypatch)
        monkeypatch.setattr(database, "SessionLocal", factory)
        monkeypatch.setattr(memory_relationship_service, "SessionLocal", factory)
        _edge(factory, "design", "project", "fixture-project")
        assert "design: Design" not in service.get_memory_context_for_terminal("worker")
    elif path == "curated":
        rows = _run(
            service.recall(
                query="kafka streams",
                scope="global",
                limit=1,
                terminal_context=dict(ctx, agent_profile="memory_manager"),
            )
        )
        assert [m.key for m in rows] == ["active"]
    else:
        if path == "related":
            with factory() as db:
                db.query(MemoryMetadataModel).filter_by(key="active").update(
                    {"related_keys": "blocked"}
                )
                db.commit()
            index = service.get_index_path("global", None)
            index.write_text(
                "\n".join(
                    line for line in index.read_text().splitlines() if "[blocked]" not in line
                )
                + "\n"
            )
        block = service.get_memory_context(ctx)
        assert "] blocked" not in block
        assert "active: kafka streams active" in block


def test_vault_frontmatter_supersedes_excludes_target_note(
    tmp_path, monkeypatch, isolated_memory_db
):

    from cli_agent_orchestrator.services.vault import reconcile as module

    replace = module._replace_vault_edges
    service, factory, root, config = _injectable_renderer(tmp_path, monkeypatch)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(memory_relationship_service, "SessionLocal", factory)
    monkeypatch.setattr(module, "_replace_vault_edges", replace)
    monkeypatch.setenv("CAO_MEMORY_LIFECYCLE_ENABLED", "true")
    note = root / "Projects" / "CAO Design" / "Replacement.md"
    note.write_text(
        "---\ncao:\n  key: replacement\n  links:\n    - type: supersedes\n      to: design\n---\nReplacement"
    )
    module.reconcile(config.vaults[0], apply=True, run_id="lifecycle-links")
    block = service.get_memory_context_for_terminal("worker")
    assert "design: Design" not in block
    assert "replacement: Replacement" in block


def test_same_key_other_project_not_excluded(lifecycle):
    service, factory, ctx = lifecycle
    contexts = [dict(ctx, cwd="/projects/one"), dict(ctx, cwd="/projects/two")]
    for context in contexts:
        _run(
            service.store(
                key="same-key", content="same body", scope="project", terminal_context=context
            )
        )
    sid = service.resolve_scope_id("project", contexts[0])
    _edge(factory, "same-key", "project", sid)
    assert "same-key: same body" not in service.get_memory_context(contexts[0])
    assert "same-key: same body" in service.get_memory_context(contexts[1])


@pytest.mark.parametrize("status", ["proposal", "rejected", "superseded", "deleted"])
def test_proposal_rejected_deleted_edges_do_not_exclude(lifecycle, status):
    service, factory, ctx = lifecycle
    with factory() as db:
        db.query(MemoryRelationshipModel).update({"status": status})
        db.commit()
    assert "blocked: kafka streams blocked" in service.get_memory_context(ctx)


def test_relationship_store_failure_fails_open(lifecycle, monkeypatch, caplog):
    service, factory, ctx = lifecycle

    def broken(*args, **kwargs):
        raise OSError("blocked secret body")

    monkeypatch.setattr(
        memory_relationship_service.MemoryRelationshipService, "superseded_targets", broken
    )
    assert "blocked: kafka streams blocked" in service.get_memory_context(ctx)
    assert "memory_eligibility_failed_open" in caplog.text
    assert "secret body" not in caplog.text


def test_recency_moves_superseded_last_membership_unchanged(lifecycle, monkeypatch):
    service, factory, ctx = lifecycle
    monkeypatch.setenv("CAO_MEMORY_LIFECYCLE_ENABLED", "false")
    before = _run(service.recall(scope="global", limit=2, terminal_context=ctx))
    monkeypatch.setenv("CAO_MEMORY_LIFECYCLE_ENABLED", "true")
    after = _run(service.recall(scope="global", limit=2, terminal_context=ctx))
    assert [m.key for m in before] == ["blocked", "active"]
    assert [m.key for m in after] == ["active", "blocked"]
    assert {service._identity(m) for m in before} == {service._identity(m) for m in after}
    assert after[-1].lifecycle_status == "superseded"


def test_usage_demotes_before_scope_pass_and_slice(lifecycle):
    service, factory, ctx = lifecycle
    for key in ["session-sup", "session-active"]:
        _run(service.store(key=key, content="kafka streams", scope="session", terminal_context=ctx))
    sid = service.resolve_scope_id("session", ctx)
    _edge(factory, "session-sup", "session", sid)
    with factory() as db:
        db.query(MemoryMetadataModel).filter_by(key="blocked").update({"access_count": 1000})
        db.query(MemoryMetadataModel).filter_by(key="session-sup").update({"access_count": 900})
        db.commit()
    rows = _run(
        service.recall(query="kafka streams", terminal_context=ctx, sort_by="usage", limit=2)
    )
    assert [m.key for m in rows] == ["session-active", "session-sup"]
    assert rows[1].lifecycle_status == "superseded"


@pytest.mark.parametrize("sort_by", ["score", "relevance"])
def test_score_order_identical_to_flag_off_plus_annotation(lifecycle, monkeypatch, sort_by):
    service, factory, ctx = lifecycle
    monkeypatch.setenv("CAO_MEMORY_LIFECYCLE_ENABLED", "false")
    before = _run(
        service.recall(query="kafka streams", scope="global", sort_by=sort_by, terminal_context=ctx)
    )
    monkeypatch.setenv("CAO_MEMORY_LIFECYCLE_ENABLED", "true")
    after = _run(
        service.recall(query="kafka streams", scope="global", sort_by=sort_by, terminal_context=ctx)
    )
    assert [m.key for m in before] == [m.key for m in after]
    assert all(m.lifecycle_status is None for m in before)
    assert next(m for m in after if m.key == "blocked").lifecycle_status == "superseded"


def test_model_dump_never_contains_lifecycle_status(lifecycle):
    service, factory, ctx = lifecycle
    rows = _run(service.recall(scope="global", terminal_context=ctx))
    assert next(m for m in rows if m.key == "blocked").lifecycle_status == "superseded"
    assert all("lifecycle_status" not in m.model_dump() for m in rows)


@pytest.mark.parametrize("enabled", ["true", "false"])
def test_cli_show_status_line_only_when_on(lifecycle, monkeypatch, enabled):
    from click.testing import CliRunner

    from cli_agent_orchestrator.cli.commands import memory

    service, _, ctx = lifecycle
    monkeypatch.setenv("CAO_MEMORY_LIFECYCLE_ENABLED", enabled)
    monkeypatch.setattr(memory, "_get_memory_service", lambda: service)
    result = CliRunner().invoke(memory.show, ["blocked", "--scope", "global"])
    assert result.exit_code == 0, result.output
    assert ("Status:  superseded" in result.output) == (enabled == "true")


@pytest.mark.parametrize("enabled", ["true", "false"])
def test_cli_list_marker_only_when_on(lifecycle, monkeypatch, enabled):
    from click.testing import CliRunner

    from cli_agent_orchestrator.cli.commands import memory

    service, _, ctx = lifecycle
    monkeypatch.setenv("CAO_MEMORY_LIFECYCLE_ENABLED", enabled)
    monkeypatch.setattr(memory, "_get_memory_service", lambda: service)
    result = CliRunner().invoke(memory.list_memories, ["--scope", "global"])
    assert result.exit_code == 0, result.output
    assert ("[superseded]" in result.output) == (enabled == "true")


@pytest.mark.parametrize("enabled", ["true", "false"])
def test_mcp_entry_has_lifecycle_status_only_when_set(lifecycle, monkeypatch, enabled):
    from cli_agent_orchestrator.mcp_server import server

    service, _, ctx = lifecycle
    monkeypatch.setenv("CAO_MEMORY_LIFECYCLE_ENABLED", enabled)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.memory_service.MemoryService", lambda *a, **kw: service
    )
    tool = getattr(server.memory_recall, "fn", server.memory_recall)
    result = _run(
        tool(
            query="kafka streams",
            scope="global",
            memory_type=None,
            limit=10,
            sort_by="recency",
            include_related=False,
        )
    )
    assert result["success"], result
    target = next(m for m in result["memories"] if m["key"] == "blocked")
    assert (target.get("lifecycle_status") == "superseded") == (enabled == "true")
    if enabled == "false":
        assert "lifecycle_status" not in target


def test_internal_recall_round_trips_label_through_gateway(lifecycle, monkeypatch):
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api import main
    from cli_agent_orchestrator.services import memory_gateway

    service, _, ctx = lifecycle
    monkeypatch.setattr(main, "_get_memory_service", lambda: service)
    monkeypatch.setenv("CAO_MEMORY_API_URL", "http://memory-owner.invalid")
    client = TestClient(main.app, base_url="http://localhost")

    def post(path, body):
        response = client.post(path, json=body)
        assert response.status_code == 200, response.text
        target = next(m for m in response.json()["memories"] if m["key"] == "blocked")
        assert target["lifecycle_status"] == "superseded"
        return response.json()

    monkeypatch.setattr(memory_gateway, "_post", post)
    rows = _run(
        memory_gateway.recall_memory(
            query="kafka streams",
            scope="global",
            terminal_context=ctx,
            memory_type=None,
            limit=10,
            search_mode="hybrid",
            sort_by="recency",
            include_related=False,
        )
    )
    target = next(m for m in rows if m.key == "blocked")
    assert target.lifecycle_status == "superseded"
    assert "lifecycle_status" not in target.model_dump()


def test_related_extras_annotated_without_reordering(lifecycle):
    service, factory, ctx = lifecycle
    with factory() as db:
        db.query(MemoryMetadataModel).filter_by(key="active").update(
            {"related_keys": "replacement,blocked"}
        )
        db.commit()
    rows = _run(
        service.recall(
            query="active", scope="global", terminal_context=ctx, limit=1, include_related=True
        )
    )
    assert [m.key for m in rows] == ["active", "replacement", "blocked"]
    assert [m.is_related for m in rows] == [False, True, True]
    assert rows[-1].lifecycle_status == "superseded"


def test_lifecycle_setting_precedence_and_nested_config_set(tmp_path, monkeypatch):
    import json

    from click.testing import CliRunner

    from cli_agent_orchestrator.cli.commands.config import config
    from cli_agent_orchestrator.services.config_service import ConfigService, MemoryConfig

    monkeypatch.delenv("CAO_MEMORY_LIFECYCLE_ENABLED", raising=False)
    assert settings_service.is_memory_lifecycle_enabled() is False
    assert MemoryConfig().lifecycle.enabled is False
    settings_service.SETTINGS_FILE.write_text(
        json.dumps({"memory": {"lifecycle": {"enabled": False, "future": "reserved"}}})
    )
    result = CliRunner().invoke(config, ["set", "memory.lifecycle.enabled", "true"])
    assert result.exit_code == 0, result.output
    assert ConfigService.get("memory.lifecycle.enabled") is True
    assert ConfigService.list_all()["memory.lifecycle.enabled"] is True
    assert json.loads(settings_service.SETTINGS_FILE.read_text())["memory"]["lifecycle"] == {
        "enabled": True,
        "future": "reserved",
    }
    monkeypatch.setenv("CAO_MEMORY_LIFECYCLE_ENABLED", "false")
    assert ConfigService.get("memory.lifecycle.enabled") is False
    monkeypatch.setenv("CAO_MEMORY_LIFECYCLE_ENABLED", "true")
    settings_service.set_memory_setting("lifecycle.enabled", False)
    assert settings_service.is_memory_lifecycle_enabled() is True
    assert settings_service.get_memory_settings()["lifecycle"]["enabled"] is True


@pytest.mark.parametrize("value", [None, "bad", {"enabled": "false"}, {"enabled": 1}])
def test_invalid_lifecycle_settings_fail_to_default(value, monkeypatch, caplog):
    import json

    monkeypatch.delenv("CAO_MEMORY_LIFECYCLE_ENABLED", raising=False)
    settings_service.SETTINGS_FILE.write_text(json.dumps({"memory": {"lifecycle": value}}))
    assert settings_service.is_memory_lifecycle_enabled() is False
    assert "memory.lifecycle.enabled" in caplog.text


def test_lifecycle_read_error_is_off(monkeypatch):
    monkeypatch.delenv("CAO_MEMORY_LIFECYCLE_ENABLED", raising=False)

    def broken():
        raise OSError("unavailable")

    monkeypatch.setattr(settings_service, "_load", broken)
    assert settings_service.is_memory_lifecycle_enabled() is False


@pytest.mark.parametrize("enabled", ["true", "false"])
def test_relevance_demotion_independent_of_flag(lifecycle, monkeypatch, enabled):
    service, _, ctx = lifecycle
    monkeypatch.setenv("CAO_MEMORY_LIFECYCLE_ENABLED", enabled)
    monkeypatch.setattr(
        service,
        "_bm25_relevance",
        lambda *a, **kw: {
            ("blocked", "global", None, "native"): 10,
            ("active", "global", None, "native"): 1,
            ("replacement", "global", None, "native"): 0.5,
        },
    )
    rows = _run(
        service.recall(
            query="kafka streams", scope="global", sort_by="relevance", terminal_context=ctx
        )
    )
    assert [m.key for m in rows] == ["active", "replacement", "blocked"]


def test_recall_reuses_snapshot_for_primary_and_related_labels(lifecycle, monkeypatch):
    service, factory, ctx = lifecycle
    with factory() as db:
        db.query(MemoryMetadataModel).filter_by(key="active").update(
            {"related_keys": "replacement,blocked"}
        )
        db.commit()
    original = service._superseded_keys
    calls = []

    def lookup(rows):
        calls.append({row.key for row in rows})
        return original(rows) if len(calls) == 1 else set()

    monkeypatch.setattr(service, "_superseded_keys", lookup)
    rows = _run(
        service.recall(
            query="active", scope="global", terminal_context=ctx, limit=1, include_related=True
        )
    )
    assert len(calls) == 1
    assert calls[0] == {"active", "replacement", "blocked"}
    assert [row.key for row in rows] == ["active", "replacement", "blocked"]
    assert rows[-1].lifecycle_status == "superseded"


@pytest.mark.parametrize("late_failure", [False, True])
def test_curated_store_failure_is_observed(lifecycle, monkeypatch, caplog, late_failure):
    from cli_agent_orchestrator.services import memory_eligibility

    service, _, ctx = lifecycle
    begin = memory_eligibility.begin_check
    checks = []

    def capture(request):
        check = begin(request)
        checks.append(check)
        return check

    monkeypatch.setattr(memory_eligibility, "begin_check", capture)
    calls = []

    def broken(self, scope, scope_id, keys):
        calls.append(set(keys))
        if late_failure and len(calls) == 1:
            return {"blocked"}
        raise OSError("/PATH_SENTINEL/KEY_SENTINEL.md")

    monkeypatch.setattr(
        memory_relationship_service.MemoryRelationshipService, "superseded_targets", broken
    )
    caplog.clear()
    rows = _run(
        service.recall(
            scope="global",
            search_mode="metadata",
            limit=10,
            terminal_context=dict(ctx, agent_profile="memory_manager"),
        )
    )
    assert [row.key for row in rows] == (
        ["active", "replacement"] if late_failure else ["blocked", "active", "replacement"]
    )
    assert checks[0].errors == ["lifecycle.supersedes"]
    warnings = [
        record.getMessage()
        for record in caplog.records
        if "memory_eligibility_failed_open" in record.getMessage()
    ]
    assert warnings
    assert all("purpose=curated_recall" in warning for warning in warnings)
    assert all(
        "PATH_SENTINEL" not in warning and "KEY_SENTINEL" not in warning for warning in warnings
    )
    assert len(calls) == 2
    assert checks[0].exclusion_summary_for_scope("global") == (
        (1, ["lifecycle.supersedes"]) if late_failure else (0, [])
    )
    assert checks[0].exclusion_summary("global", None) == (
        (1, ["lifecycle.supersedes"]) if late_failure else (0, [])
    )


@pytest.fixture
def partial_graph(svc, db_engine, monkeypatch):
    factory = sessionmaker(bind=db_engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(memory_relationship_service, "SessionLocal", factory)
    ctx = _ctx(caller_scope="global")
    now = datetime.now(timezone.utc)
    for i, (key, scope) in enumerate(
        [
            ("g-new", "global"),
            ("g-old", "global"),
            ("p-new", "project"),
            ("p-old", "project"),
            ("g-rel", "global"),
        ]
    ):
        _run(svc.store(key=key, content="kafka streams " + key, scope=scope, terminal_context=ctx))
        _stamp_updated_at(db_engine, key, scope, now - timedelta(days=i))
    project_id = svc.resolve_scope_id("project", ctx)
    with factory() as db:
        for scope, sid, source, target in [
            ("global", "", "g-new", "g-old"),
            ("global", "", "g-new", "g-rel"),
            ("project", project_id, "p-new", "p-old"),
        ]:
            db.add(
                MemoryRelationshipModel(
                    scope=scope,
                    scope_id=sid,
                    source_key=source,
                    target_key=target,
                    type="supersedes",
                    status="active",
                    origin="human",
                )
            )
        db.query(MemoryMetadataModel).filter_by(key="g-new").update({"related_keys": "g-rel"})
        db.commit()
    monkeypatch.setenv("CAO_MEMORY_LIFECYCLE_ENABLED", "true")
    return svc, ctx


def test_partial_explain_matches_real_injection(partial_graph, monkeypatch, caplog):
    service, ctx = partial_graph
    original = memory_relationship_service.MemoryRelationshipService.superseded_targets
    calls = []

    def fail_project(self, scope, scope_id, keys):
        calls.append((scope, scope_id))
        if scope == "project":
            raise OSError("/PATH_SENTINEL/KEY_SENTINEL.md")
        return original(self, scope, scope_id, keys)

    monkeypatch.setattr(
        memory_relationship_service.MemoryRelationshipService, "superseded_targets", fail_project
    )
    result = _run(service.recall_explain(query="kafka streams", limit=5, terminal_context=ctx))
    assert len(calls) == 2 and len(set(calls)) == 2
    assert result["eligibility_errors"] == ["lifecycle.supersedes"]
    block = service.get_memory_context(ctx)
    for row in result["candidates"]:
        if row["scope"] == "global":
            expected = "eligible" if f"] {row['key']}:" in block else "excluded"
            assert row["injection"] == expected
        else:
            assert row["injection"] == "unknown"
    assert "] g-old:" not in block
    assert "] p-old:" in block
    warnings = [
        record.getMessage()
        for record in caplog.records
        if "memory_eligibility_failed_open" in record.getMessage()
    ]
    assert warnings
    assert all(
        "PATH_SENTINEL" not in warning and "KEY_SENTINEL" not in warning for warning in warnings
    )


def test_curator_snapshot_failure_still_checks_related(partial_graph, monkeypatch, caplog):
    service, ctx = partial_graph
    original = memory_relationship_service.MemoryRelationshipService.superseded_targets
    calls = []

    def fail_second(self, scope, scope_id, keys):
        calls.append(set(keys))
        if len(calls) == 2:
            raise OSError("/PATH_SENTINEL/KEY_SENTINEL.md")
        return original(self, scope, scope_id, keys)

    monkeypatch.setattr(
        memory_relationship_service.MemoryRelationshipService, "superseded_targets", fail_second
    )
    rows = _run(
        service.recall(
            query="kafka streams",
            scope="global",
            limit=1,
            include_related=True,
            terminal_context=dict(ctx, agent_profile="memory_manager"),
        )
    )
    assert "g-rel" not in [row.key for row in rows]
    assert [row.key for row in rows] == ["g-new"]
    assert len(calls) == 3 and calls[-1] == {"g-rel"}
    assert "memory_eligibility_failed_open" in caplog.text
