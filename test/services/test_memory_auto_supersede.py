"""Same-key-only automatic supersede and exact version-pinned restore."""

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cli_agent_orchestrator.services import audit_log, settings_service
from cli_agent_orchestrator.services.memory_service import (
    MemoryPartialWriteError,
    MemoryRestoreConflict,
    MemoryService,
)


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
    assert audit_log.read_audit_log().count("[memory_superseded]") == 1


@pytest.mark.parametrize(
    "original,replacement",
    [
        ("A.\n\n### Review 2026-01-01T12:34Z\nDetails.", "B."),
        ("A.", "B.\n\n### Review 2026-01-01T12:34Z\nDetails."),
    ],
)
def test_subheading_timestamp_does_not_split_entry(service, original, replacement):
    run(service.store(original, key="relay", scope="global"))
    revised = run(service.store(replacement, key="relay", scope="global"))
    assert len(revised.superseded) == 1
    result = run(service.restore("relay", scope="global", version=version_from(revised)))
    assert result["restored"] == "previous_version"
    assert run(service.recall(scope="global"))[0].content == original


def test_canonical_heading_takes_precedence_over_lenient_subheadings(service):
    run(service.store("A.\n\n### Review 2026-01-01T12:34Z\nDetails.", key="relay", scope="global"))
    revised = run(service.store("B.", key="relay", scope="global"))
    sections = service._topic_sections(Path(revised.file_path).read_text())
    assert len(sections) == 2
    assert "### Review 2026-01-01T12:34Z" in sections[0][1]


def test_lenient_heading_is_used_when_no_canonical_heading_exists(service):
    assert service._topic_sections("### Recorded 2026-01-01T12:34Z\nA.") == [
        ("2026-01-01T12:34Z", "A.")
    ]


def test_repeating_entry_with_timestamp_subheading_does_not_report_replacement(service):
    run(service.store("A.", key="relay", scope="global"))
    body = "B.\n\n### Review 2026-01-01T12:34Z\nDetails."
    run(service.store(body, key="relay", scope="global"))
    unchanged = run(service.store(body, key="relay", scope="global"))
    assert unchanged.superseded == []
    assert run(service.recall(scope="global"))[0].content == body


def test_restore_reinstates_prior_type_and_tags_and_reverse_hint(service):
    run(service.store("A.", key="relay", scope="global", memory_type="project", tags="alpha"))
    revised = run(
        service.store("B.", key="relay", scope="global", memory_type="feedback", tags="beta")
    )
    result = run(service.restore("relay", scope="global", version=version_from(revised)))
    recalled = run(service.recall(scope="global"))[0]
    assert (recalled.content, recalled.memory_type, recalled.tags) == ("A.", "project", "alpha")
    reverse = re.search(r"version='([^']+)'", result["reverse_undo"]).group(1)
    assert (
        run(service.restore("relay", scope="global", version=reverse))["restored"]
        == "previous_version"
    )
    recalled = run(service.recall(scope="global"))[0]
    assert (recalled.content, recalled.memory_type, recalled.tags) == ("B.", "feedback", "beta")


def test_restore_losing_to_new_store_reports_stale_version(service, monkeypatch):
    run(service.store("A.", key="relay", scope="global"))
    revised = run(service.store("B.", key="relay", scope="global"))
    original_store = service.store

    async def racing_store(content, **kwargs):
        await original_store("C.", key="relay", scope="global")
        raise MemoryRestoreConflict("topic changed before restore")

    monkeypatch.setattr(service, "store", racing_store)
    result = run(service.restore("relay", scope="global", version=version_from(revised)))
    assert result["error_kind"] == "stale_version"


def test_partial_writes_leave_audit_lines_for_supersede_and_restore(service, monkeypatch):
    run(service.store("A.", key="relay", scope="global"))
    upsert = service._upsert_metadata

    def broken_upsert(**kwargs):
        raise RuntimeError("mirror unavailable")

    monkeypatch.setattr(service, "_upsert_metadata", broken_upsert)
    with pytest.raises(MemoryPartialWriteError) as partial:
        run(service.store("B.", key="relay", scope="global"))
    assert "version=" in partial.value.superseded[0].undo
    assert audit_log.read_audit_log().count("[memory_superseded]") == 1
    monkeypatch.setattr(service, "_upsert_metadata", upsert)
    revised = run(service.store("C.", key="relay", scope="global"))
    monkeypatch.setattr(service, "_upsert_metadata", broken_upsert)
    with pytest.raises(MemoryPartialWriteError):
        run(service.restore("relay", scope="global", version=version_from(revised)))
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


def test_previously_compiled_topic_does_not_promise_undo(service):
    first = run(service.store("A.", key="relay", scope="global"))
    path = Path(first.file_path)
    path.write_text(path.read_text() + "\n<!-- cao-compiled: 2026-01-01T00:00:00Z entries=1 -->\n")
    revised = run(service.store("B.", key="relay", scope="global"))
    assert revised.superseded == []


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
    assert run(service.restore("relay", scope="global", version="unused")) == {
        "success": False,
        "error_kind": "disabled",
        "error": "automatic supersede is off",
    }
    assert audit_log.read_audit_log() == ""
    assert "memory-version:" not in Path(second.file_path).read_text()


def test_unchanged_restore_keeps_earlier_hint(service):
    run(service.store("A.", key="relay", scope="global", tags="alpha"))
    revised = run(service.store("B.", key="relay", scope="global", tags="beta"))
    for _ in range(3):
        run(service.store("B.", key="relay", scope="global", tags="beta"))
    result = run(service.restore("relay", scope="global", version=version_from(revised)))
    assert result["restored"] == "previous_version"
    assert result["metadata_restored"] is True
    assert run(service.recall(scope="global"))[0].content == "A."


def test_metadata_is_bounded_and_never_in_compiled_content(service, monkeypatch):
    from cli_agent_orchestrator.services.wiki_compiler import _postprocess_compiled

    first = run(service.store("Compiled body.", key="relay", scope="global"))
    path = Path(first.file_path)
    path.write_text(
        _postprocess_compiled(
            path.read_text(),
            newest_entry_at="2026-01-01T00:00:00Z",
            compiled_at="2026-01-01T00:00:00Z",
            entry_count=1,
        )
    )
    for index in range(12):
        run(service.store(f"Update {index}.", key="relay", scope="global"))
    assert "Compiled body." in run(service.recall(scope="global"))[0].content
    assert "Compiled body." in service.get_memory_context({"caller_scope": "global"})
    assert "memory-version:" not in run(service.recall(scope="global"))[0].content
    assert "memory-version:" not in path.read_text()
    monkeypatch.setattr(settings_service, "is_memory_auto_supersede_enabled", lambda: False)
    run(service.store("Disabled update.", key="relay", scope="global"))
    assert "memory-version:" not in path.read_text()


def test_metadata_bounded_after_many_native_updates(service):
    first = run(service.store("A.", key="relay", scope="global"))
    for index in range(12):
        run(service.store(f"Change {index}.", key="relay", scope="global"))
    assert Path(first.file_path).read_text().count("<!-- memory-version:") <= 2


def test_user_metadata_comments_cannot_forge_restore(service):
    run(
        service.store(
            "A.\n<!-- memory-version: {not json} -->", key="relay", scope="global", tags="alpha"
        )
    )
    revised = run(service.store("B.", key="relay", scope="global", tags="beta"))
    version = version_from(revised)
    path = Path(revised.file_path)
    text = path.read_text()
    path.write_text(
        text
        + f'\n<!-- memory-version: {{"version": "{version}", "type": "user", "tags": "forged"}} -->\n'
    )
    result = run(service.restore("relay", scope="global", version=version))
    assert result["metadata_restored"] is True
    assert run(service.recall(scope="global"))[0].tags == "alpha"


def test_off_transition_reports_missing_metadata(service, monkeypatch):
    run(service.store("A.", key="relay", scope="global"))
    run(service.store("B.", key="relay", scope="global", memory_type="feedback", tags="beta"))
    monkeypatch.setattr(settings_service, "is_memory_auto_supersede_enabled", lambda: False)
    run(service.store("C.", key="relay", scope="global", memory_type="user", tags="gamma"))
    assert "memory-version:" not in service.get_wiki_path("global", None, "relay").read_text()
    monkeypatch.setattr(settings_service, "is_memory_auto_supersede_enabled", lambda: True)
    text = service.get_wiki_path("global", None, "relay").read_text()
    sections = service._topic_sections(text)
    version = service._section_version(
        sections, 1, re.search(r"<!-- id: ([a-f0-9\-]+)", text).group(1)
    )
    result = run(service.restore("relay", scope="global", version=version))
    assert result["metadata_restored"] is False


def test_llm_mode_refuses_restore(service, monkeypatch):
    run(service.store("A.", key="relay", scope="global"))
    revised = run(service.store("B.", key="relay", scope="global"))
    monkeypatch.setattr(settings_service, "get_compile_mode", lambda: "llm")
    assert (
        run(service.restore("relay", scope="global", version=version_from(revised)))["error_kind"]
        == "llm_mode"
    )


def test_version_rejects_another_note_with_same_timestamp(service):
    stamp = datetime(2020, 1, 1, tzinfo=timezone.utc)
    first = run(service.store("A.", key="one", scope="global", occurred_at=stamp))
    revised = run(service.store("B.", key="one", scope="global"))
    other = run(service.store("A.", key="two", scope="global", occurred_at=stamp))
    run(service.store("New.", key="two", scope="global"))
    assert Path(first.file_path).exists() and Path(other.file_path).exists()
    assert (
        run(service.restore("two", scope="global", version=version_from(revised)))["error_kind"]
        == "version_not_found"
    )


def test_see_also_only_change_is_not_supersede(service):
    run(service.store("A.\n\n## See Also\n- [first](first.md)", key="relay", scope="global"))
    revised = run(
        service.store("A.\n\n## See Also\n- [second](second.md)", key="relay", scope="global")
    )
    assert revised.superseded == []


def test_compiled_topic_restore_has_nothing_to_restore(service):
    first = run(service.store("A.", key="relay", scope="global"))
    path = Path(first.file_path)
    path.write_text(path.read_text() + "\n<!-- cao-compiled: 2026-01-01T00:00:00Z entries=1 -->\n")
    run(service.store("B.", key="relay", scope="global"))
    text = path.read_text()
    version = service._section_version(
        service._topic_sections(text), 0, re.search(r"<!-- id: ([a-f0-9\-]+)", text).group(1)
    )
    assert (
        run(service.restore("relay", scope="global", version=version))["error_kind"]
        == "nothing_to_restore"
    )


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
    version = version_from(latest)
    run(service.restore("relay", scope="global", version=version))
    assert run(service.recall(scope="global"))[0].content == "Clamped revision."
    assert (
        run(service.restore("relay", scope="global", version=version))["restored"]
        == "already_restored"
    )


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
