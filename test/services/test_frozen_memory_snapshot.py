from __future__ import annotations

import asyncio
import builtins
import hashlib
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

import pytest

from cli_agent_orchestrator.clients.database import MemoryMetadataModel
from cli_agent_orchestrator.constants import MEMORY_SCOPE_BUDGET_CHARS
from cli_agent_orchestrator.services import memory_service as memory_service_module
from cli_agent_orchestrator.services.frozen_memory_snapshot import (
    InheritedMemoryScope,
    MemoryFreezeError,
    MemoryFreezeRequest,
    freeze_memory_snapshot,
)
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.plan_identifier import digest_bytes
from cli_agent_orchestrator.services.private_plan_snapshot import (
    decode_material,
    structured_material_bytes,
)

pytestmark = pytest.mark.usefixtures("isolated_memory_db")


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _context(
    cwd: Path,
    *,
    session_name: str = "session-a",
    agent_profile: str = "developer",
) -> dict[str, str]:
    return {
        "cwd": str(cwd),
        "working_directory": str(cwd),
        "session_name": session_name,
        "agent_profile": agent_profile,
    }


def _request(
    cwd: Path,
    *,
    target_key: str = "repo",
    agent_profile: str = "developer",
    memory_mode: Literal["exact-snapshot", "off"] = "exact-snapshot",
    inherited_scope: InheritedMemoryScope | None = None,
) -> MemoryFreezeRequest:
    return MemoryFreezeRequest(
        target_key=target_key,
        agent_profile=agent_profile,
        context=_context(cwd, agent_profile=agent_profile),
        memory_mode=memory_mode,
        inherited_scope=inherited_scope,
    )


def _store(
    service: MemoryService,
    context: dict[str, str],
    *,
    key: str,
    content: str,
    scope: str,
) -> None:
    _run(
        service.store(
            content=content,
            key=key,
            memory_type="project",
            scope=scope,
            terminal_context=context,
        )
    )


def _set_related(
    service: MemoryService,
    *,
    key: str,
    related_keys: str,
    scope: str = "global",
    scope_id: str | None = None,
) -> None:
    with service._get_db_session() as db:
        query = db.query(MemoryMetadataModel).filter(
            MemoryMetadataModel.key == key,
            MemoryMetadataModel.scope == scope,
        )
        if scope_id is None:
            query = query.filter(MemoryMetadataModel.scope_id.is_(None))
        else:
            query = query.filter(MemoryMetadataModel.scope_id == scope_id)
        row = query.one()
        row.related_keys = related_keys
        db.commit()


def _db_state(service: MemoryService) -> list[tuple[Any, ...]]:
    with service._get_db_session() as db:
        rows = (
            db.query(MemoryMetadataModel)
            .order_by(
                MemoryMetadataModel.scope,
                MemoryMetadataModel.scope_id,
                MemoryMetadataModel.key,
            )
            .all()
        )
        return [
            (
                row.key,
                row.scope,
                row.scope_id,
                row.file_path,
                row.tags,
                row.access_count,
                row.related_keys,
                row.created_at,
                row.updated_at,
                row.last_accessed_at,
            )
            for row in rows
        ]


def _file_state(base_dir: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(base_dir)): path.read_bytes()
        for path in sorted(base_dir.rglob("*"))
        if path.is_file()
    }


def test_roundtrip_is_immutable_exact_and_deterministic_with_related_rows(
    tmp_path: Path,
    isolated_memory_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAO_PROJECT_ID", "project-a")
    service = MemoryService(base_dir=tmp_path / "memory", db_engine=isolated_memory_db)
    context = _context(tmp_path / "checkout")
    _store(service, context, key="alpha", content="alpha body", scope="global")
    for key in ("bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet"):
        _store(service, context, key=key, content=f"{key} body", scope="global")
    _store(service, context, key="zulu", content="zulu body", scope="global")
    _set_related(service, key="zulu", related_keys="alpha")

    index_path = service.get_index_path("global", None)
    lines = index_path.read_text(encoding="utf-8").splitlines()
    entry_lines = [line for line in lines if line.startswith("- [")]
    fixed_entries = [
        line.rsplit("updated:", 1)[0] + "updated:2026-09-16T00:00:00Z" for line in entry_lines
    ]
    prefix = [line for line in lines if not line.startswith("- [")]
    index_path.write_text(
        "\n".join(prefix + list(reversed(fixed_entries))) + "\n", encoding="utf-8"
    )

    before_files = _file_state(service.base_dir)
    before_rows = _db_state(service)
    first = freeze_memory_snapshot([_request(tmp_path / "checkout")], memory_service=service)

    index_path.write_text("\n".join(prefix + fixed_entries) + "\n", encoding="utf-8")
    second = freeze_memory_snapshot([_request(tmp_path / "checkout")], memory_service=service)

    assert first.material == second.material
    assert first.material == structured_material_bytes(json.loads(first.material))
    assert first.memory_digest == second.memory_digest == digest_bytes(first.material)
    assert first.block_for("repo", "developer").encode("utf-8") == (
        first.binding_for("repo", "developer").block_bytes
    )
    assert "[related]:" in first.block_for("repo", "developer")
    with pytest.raises(Exception):
        first.bindings[0].scope_selection += (("global", None),)  # type: ignore[misc]
    assert _db_state(service) == before_rows
    assert {
        path: content
        for path, content in _file_state(service.base_dir).items()
        if path != str(index_path.relative_to(service.base_dir))
    } == {
        path: content
        for path, content in before_files.items()
        if path != str(index_path.relative_to(service.base_dir))
    }


@pytest.mark.parametrize(
    ("source", "expected_value"),
    [
        ("override-env", "env-project"),
        ("override-settings", "settings-project"),
        ("git-remote", "example-com-org-repo"),
        ("cwd-hash", None),
    ],
)
def test_project_scope_resolution_records_exact_provenance(
    source: str,
    expected_value: str | None,
    tmp_path: Path,
    isolated_memory_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / source
    checkout.mkdir()
    monkeypatch.delenv("CAO_PROJECT_ID", raising=False)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_memory_settings",
        lambda: {"project_id": "settings-project"} if source == "override-settings" else {},
    )
    if source == "override-env":
        monkeypatch.setenv("CAO_PROJECT_ID", "env-project")
    elif source == "git-remote":
        subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://example.com/org/repo.git"],
            cwd=checkout,
            check=True,
        )

    service = MemoryService(base_dir=tmp_path / "memory", db_engine=isolated_memory_db)
    snapshot = freeze_memory_snapshot([_request(checkout)], memory_service=service)
    binding = snapshot.binding_for("repo", "developer")

    assert binding.project_id_source == source
    if expected_value is None:
        expected_value = hashlib.sha256(os.path.realpath(checkout).encode()).hexdigest()[:12]
    assert binding.project_id_value == expected_value
    assert binding.scope_selection == (
        ("session", "session-a"),
        ("project", expected_value),
        ("global", None),
    )
    material_binding = json.loads(snapshot.material)["bindings"][0]
    assert material_binding["project_id_source"] == source
    assert material_binding["project_id_value"] == expected_value
    assert material_binding["context"] == _context(checkout)


def test_pairs_bind_independent_inherited_project_scopes_and_modes(
    tmp_path: Path,
    isolated_memory_db: Any,
) -> None:
    service = MemoryService(base_dir=tmp_path / "memory", db_engine=isolated_memory_db)
    first_scope = InheritedMemoryScope.for_project(
        project_id_source="git-remote",
        project_id_value="project-one",
        session_scope_id="session-a",
    )
    second_scope = InheritedMemoryScope.for_project(
        project_id_source="override-settings",
        project_id_value="project-two",
        session_scope_id="session-b",
    )
    requests = [
        _request(
            tmp_path / "generated-one",
            target_key="one",
            inherited_scope=first_scope,
        ),
        _request(
            tmp_path / "generated-two",
            target_key="two",
            agent_profile="reviewer",
            inherited_scope=second_scope,
        ),
        _request(
            tmp_path / "disabled",
            target_key="off",
            memory_mode="off",
        ),
    ]

    snapshot = freeze_memory_snapshot(reversed(requests), memory_service=service)

    assert [(b.target_key, b.agent_profile) for b in snapshot.bindings] == [
        ("off", "developer"),
        ("one", "developer"),
        ("two", "reviewer"),
    ]
    assert snapshot.binding_for("one", "developer").project_id_value == "project-one"
    assert snapshot.binding_for("two", "reviewer").project_id_value == "project-two"
    off = snapshot.binding_for("off", "developer")
    assert off.memory_mode == "off"
    assert off.block_bytes == b""
    assert off.scope_selection == ()


def test_selected_memory_change_changes_digest_but_unselected_project_does_not(
    tmp_path: Path,
    isolated_memory_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MemoryService(base_dir=tmp_path / "memory", db_engine=isolated_memory_db)
    monkeypatch.setenv("CAO_PROJECT_ID", "selected")
    selected_context = _context(tmp_path / "selected")
    _store(service, selected_context, key="selected", content="v1", scope="project")
    request = _request(tmp_path / "selected")
    first = freeze_memory_snapshot([request], memory_service=service)

    monkeypatch.setenv("CAO_PROJECT_ID", "outside")
    _store(
        service,
        _context(tmp_path / "outside"),
        key="outside",
        content="must not bind",
        scope="project",
    )
    inherited = InheritedMemoryScope.from_binding(first.binding_for("repo", "developer"))
    second = freeze_memory_snapshot(
        [replace(request, inherited_scope=inherited)],
        memory_service=service,
    )
    assert second.memory_digest == first.memory_digest

    monkeypatch.setenv("CAO_PROJECT_ID", "selected")
    _store(service, selected_context, key="selected", content="v2", scope="project")
    third = freeze_memory_snapshot([request], memory_service=service)
    assert third.memory_digest != first.memory_digest


def test_off_mode_performs_no_resolution_or_memory_query(tmp_path: Path) -> None:
    class NoReads:
        def get_memory_context_strict(self, *args: Any, **kwargs: Any) -> str:
            raise AssertionError("off must not read memory")

    snapshot = freeze_memory_snapshot(
        [_request(tmp_path, memory_mode="off")],
        memory_service=NoReads(),  # type: ignore[arg-type]
    )

    assert snapshot.block_for("repo", "developer") == ""
    assert json.loads(snapshot.material)["bindings"][0]["memory_mode"] == "off"


def test_related_lookup_failure_refuses_without_leaking_exception_text(
    tmp_path: Path,
    isolated_memory_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "SECRET-RELATED-QUERY"
    monkeypatch.setenv("CAO_PROJECT_ID", "project-a")
    service = MemoryService(base_dir=tmp_path / "memory", db_engine=isolated_memory_db)
    context = _context(tmp_path / "checkout")
    _store(service, context, key="alpha", content="body", scope="global")

    def fail() -> Any:
        raise RuntimeError(secret)

    monkeypatch.setattr(service, "_get_db_session", fail)
    with pytest.raises(MemoryFreezeError) as caught:
        freeze_memory_snapshot([_request(tmp_path / "checkout")], memory_service=service)

    message = str(caught.value)
    assert "related metadata" in message
    assert secret not in message
    assert str(tmp_path) not in message


@pytest.mark.parametrize("disabled_by", ["env", "settings"])
def test_disabled_memory_refuses_strict_freeze_before_any_material_read(
    disabled_by: str,
    tmp_path: Path,
    isolated_memory_db: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from cli_agent_orchestrator.services import settings_service

    secret = "TOPSECRETMEMORYBODY"
    monkeypatch.setenv("CAO_PROJECT_ID", "project-a")
    service = MemoryService(base_dir=tmp_path / "memory", db_engine=isolated_memory_db)
    context = _context(tmp_path / "checkout")
    _store(service, context, key="alpha", content=secret, scope="global")

    if disabled_by == "env":
        monkeypatch.setenv("CAO_MEMORY_ENABLED", "0")
    else:
        monkeypatch.delenv("CAO_MEMORY_ENABLED", raising=False)
        monkeypatch.setattr(
            settings_service,
            "get_memory_settings",
            lambda: {"enabled": False},
        )
    assert settings_service.is_memory_enabled() is False

    reads: list[str] = []

    def forbidden_read(*args: Any, **kwargs: Any) -> str:
        reads.append("material")
        raise AssertionError("wiki/index/query read attempted")

    monkeypatch.setattr(service, "_get_memory_context_from_scope_selection", forbidden_read)
    assert service.get_memory_context(context) == ""

    with pytest.raises(MemoryFreezeError) as caught:
        freeze_memory_snapshot([_request(tmp_path / "checkout")], memory_service=service)

    assert caught.value.component == "memory disabled"
    assert caught.value.declaration_key == "memory_read"
    assert reads == []
    assert secret not in str(caught.value)
    assert secret not in caplog.text


def test_related_metadata_import_failure_is_legacy_permissive_but_strict_refuses(
    tmp_path: Path,
    isolated_memory_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MemoryService(base_dir=tmp_path / "memory", db_engine=isolated_memory_db)
    context = _context(tmp_path / "checkout")
    _store(service, context, key="alpha", content="primary body", scope="global")
    real_import = builtins.__import__

    def import_with_database_failure(
        name: str,
        globals: Any = None,
        locals: Any = None,
        fromlist: Any = (),
        level: int = 0,
    ) -> Any:
        if name == "cli_agent_orchestrator.clients.database" and "MemoryMetadataModel" in fromlist:
            raise ImportError("PRIVATE IMPORT DETAIL")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_with_database_failure)

    assert "primary body" in service.get_memory_context(context)
    with pytest.raises(memory_service_module.MemoryStrictReadError) as caught:
        service.get_memory_context_strict(
            (("session", "session-a"), ("project", "project-a"), ("global", None))
        )
    assert caught.value.component == "related metadata"
    assert "PRIVATE IMPORT DETAIL" not in str(caught.value)


def test_dangling_related_article_is_a_normal_strict_miss(
    tmp_path: Path,
    isolated_memory_db: Any,
) -> None:
    service = MemoryService(base_dir=tmp_path / "memory", db_engine=isolated_memory_db)
    context = _context(tmp_path / "checkout")
    _store(service, context, key="zulu", content="primary body", scope="global")
    _set_related(service, key="zulu", related_keys="ghost")

    block = service.get_memory_context_strict(
        (("session", "session-a"), ("project", "project-a"), ("global", None))
    )
    assert "primary body" in block
    assert "ghost" not in block


def test_existing_malformed_related_article_refuses_strict_read(
    tmp_path: Path,
    isolated_memory_db: Any,
) -> None:
    service = MemoryService(base_dir=tmp_path / "memory", db_engine=isolated_memory_db)
    context = _context(tmp_path / "checkout")
    _store(service, context, key="alpha", content="related body", scope="global")
    for key in ("bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet"):
        _store(service, context, key=key, content=f"{key} body", scope="global")
    _store(service, context, key="zulu", content="primary body", scope="global")
    _set_related(service, key="zulu", related_keys="alpha")
    service.get_wiki_path("global", None, "alpha").write_text(
        "existing but malformed",
        encoding="utf-8",
    )

    with pytest.raises(memory_service_module.MemoryStrictReadError) as caught:
        service.get_memory_context_strict(
            (("session", "session-a"), ("project", "project-a"), ("global", None))
        )
    assert caught.value.component == "related article"


def test_strict_path_never_calls_curator_terminal_or_provider_discovery(
    tmp_path: Path,
    isolated_memory_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli_agent_orchestrator.services import terminal_service

    monkeypatch.setenv("CAO_PROJECT_ID", "project-a")
    service = MemoryService(base_dir=tmp_path / "memory", db_engine=isolated_memory_db)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("live discovery was called")

    monkeypatch.setattr(service, "get_curated_memory_context", forbidden)
    monkeypatch.setattr(service, "_get_terminal_context", forbidden)
    monkeypatch.setattr(service, "_find_context_manager_terminal", forbidden)
    monkeypatch.setattr(terminal_service, "send_input", forbidden)
    monkeypatch.setattr(terminal_service.provider_manager, "get_provider", forbidden)

    snapshot = freeze_memory_snapshot([_request(tmp_path / "checkout")], memory_service=service)
    assert snapshot.block_for("repo", "developer") == ""


def test_inherited_scope_reads_parent_without_generated_cwd_resolution(
    tmp_path: Path,
    isolated_memory_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MemoryService(base_dir=tmp_path / "memory", db_engine=isolated_memory_db)
    parent = InheritedMemoryScope.for_project(
        project_id_source="git-remote",
        project_id_value="parent-project",
        session_scope_id="parent-session",
    )

    monkeypatch.setattr(
        memory_service_module,
        "_git_remote_identity",
        lambda _cwd: (_ for _ in ()).throw(AssertionError("generated cwd queried")),
    )
    snapshot = freeze_memory_snapshot(
        [_request(tmp_path / "generated-worktree", inherited_scope=parent)],
        memory_service=service,
    )

    binding = snapshot.binding_for("repo", "developer")
    assert binding.project_id_value == "parent-project"
    assert binding.scope_selection[1] == ("project", "parent-project")


def test_per_scope_budget_counts_unicode_characters_not_utf8_bytes_and_is_per_pair(
    tmp_path: Path,
    isolated_memory_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAO_PROJECT_ID", "project-a")
    service = MemoryService(base_dir=tmp_path / "memory", db_engine=isolated_memory_db)
    context = _context(tmp_path / "checkout")
    for index in range(8):
        _store(
            service,
            context,
            key=f"unicode-{index}",
            content=("界" * 90) + str(index),
            scope="global",
        )

    snapshot = freeze_memory_snapshot(
        [
            _request(tmp_path / "checkout", target_key="a"),
            _request(tmp_path / "checkout", target_key="b", agent_profile="reviewer"),
        ],
        memory_service=service,
    )

    for binding in snapshot.bindings:
        scope_lines = [line for line in binding.block.splitlines() if line.startswith("- [global]")]
        assert sum(len(line) + 1 for line in scope_lines) <= MEMORY_SCOPE_BUDGET_CHARS
        assert len(binding.block_bytes) > MEMORY_SCOPE_BUDGET_CHARS
    assert sum(len(binding.block) for binding in snapshot.bindings) > MEMORY_SCOPE_BUDGET_CHARS

    decoded = decode_material("memory", snapshot.material)
    decoded_bindings = {
        (item["target_key"], item["agent_profile"]): item for item in decoded["bindings"]
    }
    for binding in snapshot.bindings:
        decoded_block = decoded_bindings[(binding.target_key, binding.agent_profile)][
            "snapshot_block"
        ]
        assert decoded_block.encode("utf-8") == binding.block_bytes
        assert (
            decoded_bindings[(binding.target_key, binding.agent_profile)]["content_hash"]
            == digest_bytes(binding.block_bytes)
            == binding.content_hash
        )


def test_freeze_reads_leave_wiki_index_and_database_rows_byte_for_byte_unchanged(
    tmp_path: Path,
    isolated_memory_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAO_PROJECT_ID", "project-a")
    service = MemoryService(base_dir=tmp_path / "memory", db_engine=isolated_memory_db)
    context = _context(tmp_path / "checkout")
    _store(service, context, key="alpha", content="body", scope="global")
    before_files = _file_state(service.base_dir)
    before_rows = _db_state(service)

    freeze_memory_snapshot([_request(tmp_path / "checkout")], memory_service=service)

    assert _file_state(service.base_dir) == before_files
    assert _db_state(service) == before_rows


@pytest.mark.parametrize(
    "scope_selection",
    [
        (),
        [("session", "s"), ("project", "p"), ("global", None)],
        (("project", "p"), ("session", "s"), ("global", None)),
        (("session", "s"), ("project", "p")),
    ],
)
def test_strict_scope_selection_requires_exact_tuple_shape_and_order(
    scope_selection: Any,
    tmp_path: Path,
    isolated_memory_db: Any,
) -> None:
    service = MemoryService(base_dir=tmp_path / "memory", db_engine=isolated_memory_db)
    with pytest.raises(memory_service_module.MemoryStrictReadError) as caught:
        service.get_memory_context_strict(scope_selection)
    assert caught.value.component == "scope selection"


def test_strict_index_traversal_refuses_without_disclosing_path(
    tmp_path: Path,
    isolated_memory_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAO_PROJECT_ID", "project-a")
    service = MemoryService(base_dir=tmp_path / "memory", db_engine=isolated_memory_db)
    context = _context(tmp_path / "checkout")
    _store(service, context, key="alpha", content="body", scope="global")
    index_path = service.get_index_path("global", None)
    index_path.write_text(
        index_path.read_text(encoding="utf-8").replace(
            "(global/alpha.md)",
            "(../../private-target.md)",
        ),
        encoding="utf-8",
    )

    with pytest.raises(MemoryFreezeError) as caught:
        freeze_memory_snapshot([_request(tmp_path / "checkout")], memory_service=service)
    assert caught.value.component == "memory index"
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize("article_state", ["missing", "malformed"])
def test_strict_primary_article_missing_or_malformed_refuses(
    article_state: str,
    tmp_path: Path,
    isolated_memory_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAO_PROJECT_ID", "project-a")
    service = MemoryService(base_dir=tmp_path / "memory", db_engine=isolated_memory_db)
    context = _context(tmp_path / "checkout")
    _store(service, context, key="alpha", content="body", scope="global")
    article = service.get_wiki_path("global", None, "alpha")
    if article_state == "missing":
        article.unlink()
    else:
        article.write_text("existing but malformed", encoding="utf-8")

    with pytest.raises(MemoryFreezeError) as caught:
        freeze_memory_snapshot([_request(tmp_path / "checkout")], memory_service=service)
    assert caught.value.component == "memory article"


def test_duplicate_target_agent_pair_refuses(
    tmp_path: Path,
    isolated_memory_db: Any,
) -> None:
    service = MemoryService(base_dir=tmp_path / "memory", db_engine=isolated_memory_db)
    request = _request(tmp_path, memory_mode="off")
    with pytest.raises(MemoryFreezeError) as caught:
        freeze_memory_snapshot([request, request], memory_service=service)
    assert caught.value.component == "binding"
    assert caught.value.declaration_key == "duplicate_pair"


@pytest.mark.parametrize(
    "tamper",
    ["source", "empty", "project", "global", "order"],
)
def test_tampered_inherited_scope_refuses(
    tamper: str,
    tmp_path: Path,
    isolated_memory_db: Any,
) -> None:
    service = MemoryService(base_dir=tmp_path / "memory", db_engine=isolated_memory_db)
    valid = InheritedMemoryScope.for_project(
        project_id_source="git-remote",
        project_id_value="project-a",
        session_scope_id="session-a",
    )
    if tamper == "source":
        invalid = replace(valid, project_id_source="untrusted")
    elif tamper == "empty":
        invalid = replace(valid, project_id_value="")
    elif tamper == "project":
        invalid = replace(
            valid,
            scope_selection=(
                ("session", "session-a"),
                ("project", "different"),
                ("global", None),
            ),
        )
    elif tamper == "global":
        invalid = replace(
            valid,
            scope_selection=(
                ("session", "session-a"),
                ("project", "project-a"),
                ("global", "unexpected"),
            ),
        )
    else:
        invalid = replace(
            valid,
            scope_selection=(
                ("project", "project-a"),
                ("session", "session-a"),
                ("global", None),
            ),
        )

    with pytest.raises(MemoryFreezeError) as caught:
        freeze_memory_snapshot(
            [_request(tmp_path, inherited_scope=invalid)],
            memory_service=service,
        )
    assert caught.value.component == "inherited scope"


@pytest.mark.parametrize(
    ("target_key", "agent_profile", "memory_mode", "component"),
    [
        ("", "developer", "off", "target key"),
        ("repo", "", "off", "agent profile"),
        ("repo", "developer", "enabled", "memory mode"),
    ],
)
def test_invalid_pair_declaration_refuses(
    target_key: str,
    agent_profile: str,
    memory_mode: Any,
    component: str,
    tmp_path: Path,
    isolated_memory_db: Any,
) -> None:
    service = MemoryService(base_dir=tmp_path / "memory", db_engine=isolated_memory_db)
    request = MemoryFreezeRequest(
        target_key=target_key,
        agent_profile=agent_profile,
        context=_context(tmp_path),
        memory_mode=memory_mode,
    )
    with pytest.raises(MemoryFreezeError) as caught:
        freeze_memory_snapshot([request], memory_service=service)
    assert caught.value.component == component


def test_nul_cwd_refuses_with_fixed_secret_safe_error(
    tmp_path: Path,
    isolated_memory_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CAO_PROJECT_ID", raising=False)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_memory_settings",
        lambda: {},
    )
    service = MemoryService(base_dir=tmp_path / "memory", db_engine=isolated_memory_db)
    request = MemoryFreezeRequest(
        target_key="repo",
        agent_profile="developer",
        context={"cwd": "PRIVATE\x00PATH", "session_name": "session-a"},
        memory_mode="exact-snapshot",
    )
    with pytest.raises(MemoryFreezeError) as caught:
        freeze_memory_snapshot([request], memory_service=service)
    assert caught.value.component == "project identity"
    assert caught.value.declaration_key == "cwd"
    assert "PRIVATE" not in str(caught.value)


def test_non_json_context_path_refuses_without_disclosing_value(
    tmp_path: Path,
    isolated_memory_db: Any,
) -> None:
    service = MemoryService(base_dir=tmp_path / "memory", db_engine=isolated_memory_db)
    request = MemoryFreezeRequest(
        target_key="repo",
        agent_profile="developer",
        context={"cwd": tmp_path / "PRIVATE-CONTEXT"},
        memory_mode="off",
    )
    with pytest.raises(MemoryFreezeError) as caught:
        freeze_memory_snapshot([request], memory_service=service)
    assert caught.value.component == "context"
    assert "PRIVATE-CONTEXT" not in str(caught.value)
