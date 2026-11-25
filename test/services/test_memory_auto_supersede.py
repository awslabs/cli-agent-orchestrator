"""Same-key-only automatic supersede and exact version-pinned restore."""

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cli_agent_orchestrator.services import audit_log, settings_service
from cli_agent_orchestrator.services.memory_service import MemoryService


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def service(tmp_path: Path, isolated_memory_db, monkeypatch):
    monkeypatch.setattr(audit_log, "MEMORY_BASE_DIR", tmp_path / "audit")
    monkeypatch.setattr(settings_service, "is_memory_auto_supersede_enabled", lambda: True)
    monkeypatch.setattr(settings_service, "get_compile_mode", lambda: "append")
    return MemoryService(base_dir=tmp_path / "memory", db_engine=isolated_memory_db)


def version_from(memory):
    return re.search(r"version='([^']+)'", memory.superseded[0].undo).group(1)


def test_exact_restore_retains_authored_links_and_is_idempotent(service):
    authored = "Manual relay.\n\n## See Also\n- [other](other-note.md)"
    run(service.store(authored, key="relay", scope="global"))
    revised = run(service.store("Automatic relay.", key="relay", scope="global"))
    version = version_from(revised)
    assert revised.superseded[0].reason == "same_key"
    assert (
        run(service.restore("relay", scope="global", version=version))["restored_from"] == version
    )
    assert run(service.recall(scope="global"))[0].content == "Manual relay."
    assert (
        run(service.restore("relay", scope="global", version=version))["restored"]
        == "already_restored"
    )
    text = Path(revised.file_path).read_text()
    assert text.count("Manual relay.") == 2
    assert text.count("## See Also\n- [other](other-note.md)") == 2
    assert audit_log.read_audit_log().count("[memory_supersede_undone]") == 1


def test_stale_hint_refuses_to_restore_wrong_revision(service):
    run(service.store("First.", key="relay", scope="global"))
    second = run(service.store("Second.", key="relay", scope="global"))
    run(service.store("Third.", key="relay", scope="global"))
    assert (
        run(service.restore("relay", scope="global", version=version_from(second)))["error_kind"]
        == "stale_version"
    )
    assert run(service.recall(scope="global"))[0].content == "Third."


def test_links_and_similar_siblings_never_supersede(service, monkeypatch):
    monkeypatch.setattr(settings_service, "is_memory_content_links_enabled", lambda: True)
    run(service.store("Host alpha02 runs nightly backup.", key="host-alpha02", scope="global"))
    sibling = run(
        service.store(
            "Host alpha01 runs nightly backup. Sibling: [[host-alpha02]].",
            key="host-alpha01",
            scope="global",
        )
    )
    assert sibling.superseded == []
    assert sorted(memory.key for memory in run(service.recall(scope="global"))) == [
        "host-alpha01",
        "host-alpha02",
    ]


def test_restore_scope_guard_and_missing_version(service):
    run(service.store("Original.", key="relay", scope="global"))
    run(service.store("Revised.", key="relay", scope="global"))
    with pytest.raises(PermissionError):
        run(
            service.restore(
                "relay",
                scope="global",
                terminal_context={"caller_scope": "session", "session_name": "s"},
            )
        )
    with pytest.raises(PermissionError):
        run(
            service.restore(
                "missing",
                scope="global",
                terminal_context={"caller_scope": "session", "session_name": "s"},
            )
        )
    assert run(service.restore("relay", scope="global"))["error_kind"] == "version_required"
    assert (
        run(service.restore("relay", scope="global", version="2099-01-01T00:00:00Z"))["error_kind"]
        == "version_not_found"
    )


def test_llm_mode_does_not_promise_undo(service, monkeypatch):
    monkeypatch.setattr(settings_service, "get_compile_mode", lambda: "llm")
    run(service.store("Original.", key="relay", scope="global"))
    revised = run(service.store("Revised.", key="relay", scope="global"))
    assert revised.superseded == []
    assert "[memory_superseded]" not in audit_log.read_audit_log()


def test_global_audit_omits_literal_none_scope_id(service):
    run(service.store("Original.", key="relay", scope="global"))
    run(service.store("Revised.", key="relay", scope="global"))
    history = audit_log.read_audit_log()
    assert "[memory_superseded]" in history
    assert "scope_id=None" not in history


def test_default_off_is_unchanged(service, monkeypatch):
    monkeypatch.setattr(settings_service, "is_memory_auto_supersede_enabled", lambda: False)
    first = run(service.store("First.", key="relay", scope="global"))
    second = run(service.store("Second.", key="relay", scope="global"))
    assert first.superseded == second.superseded == []
    assert "superseded" not in second.model_dump()
    assert "lifecycle_status" not in second.model_dump()
    assert audit_log.read_audit_log() == ""


def test_concurrent_same_key_stores_leave_a_visible_revision(service):
    run(service.store("Original.", key="relay", scope="global"))

    def write(content):
        return run(service.store(content, key="relay", scope="global"))

    with ThreadPoolExecutor(max_workers=2) as executor:
        revisions = list(executor.map(write, ["Revision one.", "Revision two."]))
    assert all(len(revision.superseded) == 1 for revision in revisions)
    assert run(service.recall(scope="global"))[0].content in {"Revision one.", "Revision two."}
    assert audit_log.read_audit_log().count("[memory_superseded]") == 2


def test_concurrent_restore_retry_does_not_append_twice(service):
    run(service.store("Original.", key="relay", scope="global"))
    revised = run(service.store("Revised.", key="relay", scope="global"))
    version = version_from(revised)

    def undo(_index):
        return run(service.restore("relay", scope="global", version=version))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(undo, range(2)))
    assert {result["restored"] for result in results} == {"previous_version", "already_restored"}
    assert Path(revised.file_path).read_text().count("Original.") == 2


def test_legacy_edges_label_exact_reads_and_curator_include(service):
    run(service.store("Original.", key="old", scope="global"))
    run(service.store("New.", key="new", scope="global"))
    service._relationship_service().create(
        "global", None, "new", "old", "supersedes", "auto_supersede"
    )
    assert [memory.key for memory in run(service.recall(scope="global"))] == ["new"]
    labelled = run(service.read_exact_key("old", scope="global"))
    assert labelled.lifecycle_status == "superseded"
    visible = run(service.recall(scope="global", include_superseded=True))
    assert sorted(memory.key for memory in visible) == ["new", "old"]
    curated = run(
        service.recall(
            scope="global",
            include_superseded=True,
            terminal_context={"agent_profile": "memory-manager"},
        )
    )
    assert sorted(memory.key for memory in curated) == ["new", "old"]
    related = run(service.related("new", scope="global"))
    assert any(
        item["key"] == "old" and item["status"] == "superseded" for item in related["neighbours"]
    )


def test_only_native_auto_edges_hide_and_label(service):
    from cli_agent_orchestrator.services.memory_eligibility import (
        AutoSupersedeSource,
        EligibilityRequest,
        MemoryIdentity,
    )

    for key in ("source", "auto-old", "human-old"):
        run(service.store(key, key=key, scope="global"))
    relationships = service._relationship_service()
    relationships.create("global", None, "source", "auto-old", "supersedes", "auto_supersede")
    relationships.create("global", None, "source", "human-old", "supersedes", "human")
    assert sorted(memory.key for memory in run(service.recall(scope="global"))) == [
        "human-old",
        "source",
    ]
    listed = run(service.recall(scope="global", include_superseded=True))
    assert {memory.key: memory.lifecycle_status for memory in listed} == {
        "auto-old": "superseded",
        "human-old": None,
        "source": None,
    }
    candidates = [
        MemoryIdentity("auto-old", "global", None, "native"),
        MemoryIdentity("auto-old", "global", None, "vault"),
        MemoryIdentity("human-old", "global", None, "native"),
    ]
    verdicts = AutoSupersedeSource(service._db_session_factory).excluded(
        EligibilityRequest.from_context("curated_recall", None), candidates
    )
    assert verdicts == {candidates[0]: "superseded"}


def test_setting_default_off(monkeypatch):
    from cli_agent_orchestrator.services.config_service import MemoryConfig

    monkeypatch.setattr(settings_service, "get_memory_settings", lambda: {})
    assert MemoryConfig().supersede.auto is False
    assert settings_service.is_memory_auto_supersede_enabled() is False
    monkeypatch.setenv("CAO_MEMORY_SUPERSEDE_AUTO", "true")
    assert settings_service.is_memory_auto_supersede_enabled() is True


def test_restore_clamped_entry_removes_provenance_from_content(service):
    run(
        service.store(
            "Original.",
            key="relay",
            scope="global",
            occurred_at=datetime(2020, 1, 2, tzinfo=timezone.utc),
        )
    )
    run(
        service.store(
            "Clamped revision.",
            key="relay",
            scope="global",
            occurred_at=datetime(2019, 1, 2, tzinfo=timezone.utc),
        )
    )
    latest = run(service.store("Newest.", key="relay", scope="global"))
    run(service.restore("relay", scope="global", version=version_from(latest)))
    assert run(service.recall(scope="global"))[0].content == "Clamped revision."


def test_restore_resolves_project_alias_before_native_lookup(service, tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import binding as vault_binding

    context = {"cwd": str(tmp_path / "real-project")}
    run(service.store("Original.", key="relay", scope="project", terminal_context=context))
    revised = run(service.store("Revised.", key="relay", scope="project", terminal_context=context))
    actual_scope_id = revised.scope_id
    real_resolve = vault_binding.resolve
    resolved_ids = iter(["old-alias", actual_scope_id])
    monkeypatch.setattr(service, "resolve_scope_id", lambda scope, context: next(resolved_ids))

    def resolve_alias(scope, scope_id, *, vault_config=None):
        if scope_id == "old-alias":
            return vault_binding.NativeBinding(scope=scope, scope_id=actual_scope_id)
        return real_resolve(scope, scope_id, vault_config=vault_config)

    monkeypatch.setattr(vault_binding, "resolve", resolve_alias)
    result = run(
        service.restore(
            "relay", scope="project", version=version_from(revised), terminal_context=context
        )
    )
    assert result["restored"] == "previous_version"


def test_restore_refuses_vault_mapped_scope(service, tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import binding as vault_binding
    from cli_agent_orchestrator.services.vault.config import FolderMapping

    mapped = vault_binding.VaultBinding(
        scope="global",
        scope_id=None,
        vault_id="test-vault",
        root=str(tmp_path),
        mapping=FolderMapping(folder="notes", scope="global"),
    )
    monkeypatch.setattr(vault_binding, "resolve", lambda *args, **kwargs: mapped)
    result = run(service.restore("relay", scope="global", version="2020-01-01T00:00:00Z#0"))
    assert result["error_kind"] == "vault_mapped"
