"""Regression tests for native-only maintenance paths."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.services import (
    cleanup_service,
    settings_service,
    wiki_healer,
    wiki_lint,
)
from cli_agent_orchestrator.services.vault import binding
from cli_agent_orchestrator.services.vault.config import FolderMapping, VaultConfig
from cli_agent_orchestrator.services.wiki_lint import _make_issue


def _vault_binding(scope: str, scope_id: str | None) -> binding.VaultBinding:
    return binding.VaultBinding(
        scope=scope,
        scope_id=scope_id,
        vault_id="primary",
        root="/vault",
        mapping=FolderMapping(folder="Notes", scope=scope, scope_id=scope_id),
    )


def _resolve_vault_binding(
    scope: str,
    scope_id: str | None,
    *,
    vault_config: VaultConfig | None = None,
) -> binding.ScopeBinding:
    """Signature-bound vault resolver substitute for maintenance guard tests."""
    del vault_config
    return _vault_binding(scope, scope_id)


def test_wiki_lint_excludes_vault_metadata_before_detectors(tmp_path: Path, monkeypatch) -> None:
    base = tmp_path / "memory"
    wiki_dir = base / "global" / "wiki"
    wiki_dir.mkdir(parents=True)
    engine = create_engine(f"sqlite:///{tmp_path / 'metadata.db'}")
    Base.metadata.create_all(bind=engine)

    with engine.begin() as connection:
        for index in range(20):
            key = f"vault-{index}"
            path = wiki_dir / f"{key}.md"
            path.write_text("short vault note", encoding="utf-8")
            connection.execute(
                MemoryMetadataModel.__table__.insert().values(
                    key=key,
                    memory_type="reference",
                    scope="global",
                    scope_id=None,
                    source_kind="vault",
                    file_path=str(path),
                    tags="",
                    access_count=100 if index == 19 else 1,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )

    monkeypatch.setattr(wiki_lint, "_detect_orphan_pages", lambda *args: [])
    monkeypatch.setattr(wiki_lint, "_detect_stale_claims", lambda *args: [])
    monkeypatch.setattr(wiki_lint, "_detect_graph_density", lambda *args: [])
    monkeypatch.setattr(wiki_lint, "_detect_contradictions", lambda *args, **kwargs: [])

    issues = asyncio.run(
        wiki_lint.run_lint(
            "project",
            scope="global",
            base_dir=base,
            db_engine=engine,
            repo_root=str(tmp_path),
        )
    )

    assert [issue for issue in issues if issue.issue_type == "poison_frequency"] == []


def test_wiki_healer_refuses_vault_bound_scope_before_delete_row(monkeypatch) -> None:
    monkeypatch.setattr(wiki_healer, "_is_memory_enabled", lambda: True)
    monkeypatch.setattr(
        binding,
        "resolve",
        _resolve_vault_binding,
    )
    delete_row = MagicMock()
    monkeypatch.setattr(wiki_healer, "_delete_row", delete_row)

    with pytest.raises(
        wiki_healer.VaultBoundScopeError,
        match=r"^vault-bound scope cannot be healed; reconcile the vault instead$",
    ):
        asyncio.run(
            wiki_healer.heal(
                [_make_issue(issue_type="orphan_page", key="note", description="orphan")],
                scope="project",
                scope_id="project-id",
                apply=True,
            )
        )

    delete_row.assert_not_called()


def test_wiki_healer_refuses_when_vault_config_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(wiki_healer, "_is_memory_enabled", lambda: True)
    monkeypatch.setattr(
        settings_service,
        "get_vault_config",
        lambda: (_ for _ in ()).throw(ValueError("vault root 'missing' does not exist")),
    )
    svc = MagicMock()

    with pytest.raises(
        binding.VaultConfigUnavailableError,
        match=r"^vault configuration unavailable: vault root 'missing' does not exist$",
    ):
        asyncio.run(
            wiki_healer.heal(
                [],
                scope="project",
                scope_id="project-id",
                apply=True,
                svc=svc,
            )
        )

    svc._get_db_session.assert_not_called()


def test_cleanup_refuses_vault_bound_scope_before_forget(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    index = tmp_path / "project-id" / "wiki" / "index.md"
    index.parent.mkdir(parents=True)
    index.write_text(
        "## project\n"
        "- [old-note](project/old-note.md) — type:project tags:none ~1tok "
        "updated:2000-01-01T00:00:00Z\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cleanup_service, "MEMORY_BASE_DIR", tmp_path)
    monkeypatch.setattr(
        binding,
        "resolve",
        _resolve_vault_binding,
    )
    forget = MagicMock()
    monkeypatch.setattr(cleanup_service, "_forget_sync", forget)

    asyncio.run(cleanup_service.cleanup_expired_memories())

    forget.assert_not_called()
    assert (
        "vault-bound memory retention refused scope=project scope_id=project-id" in caplog.messages
    )


def test_cleanup_refuses_when_vault_config_is_unavailable(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    index = tmp_path / "project-id" / "wiki" / "index.md"
    index.parent.mkdir(parents=True)
    index.write_text(
        "## project\n"
        "- [old-note](project/old-note.md) — type:project tags:none ~1tok "
        "updated:2000-01-01T00:00:00Z\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cleanup_service, "MEMORY_BASE_DIR", tmp_path)
    monkeypatch.setattr(
        settings_service,
        "get_vault_config",
        lambda: (_ for _ in ()).throw(ValueError("vault root 'missing' does not exist")),
    )
    forget = MagicMock()
    monkeypatch.setattr(cleanup_service, "_forget_sync", forget)

    asyncio.run(cleanup_service.cleanup_expired_memories())

    forget.assert_not_called()
    assert (
        "vault-bound memory retention refused: vault configuration unavailable: "
        "vault root 'missing' does not exist" in caplog.messages
    )
