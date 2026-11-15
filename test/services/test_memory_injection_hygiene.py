"""Regression tests for memory injection hygiene (#1329 P1b)."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest

from cli_agent_orchestrator.services.memory_injection import (
    memory_blocks_have_same_body,
    uniquely_delimited_block,
)
from cli_agent_orchestrator.services.memory_service import MemoryService

pytestmark = pytest.mark.usefixtures("isolated_memory_db")


def _ctx(cwd: Path) -> dict:
    return {
        "terminal_id": "worker",
        "session_name": "injection-hygiene",
        "agent_profile": "developer",
        "provider": "claude_code",
        "cwd": str(cwd),
    }


def test_project_injection_reads_recorded_cwd_hash_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_dir = tmp_path / "memory"
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.com/acme/widgets.git"],
        cwd=repo,
        check=True,
    )
    ctx = _ctx(repo)
    service = MemoryService(base_dir=memory_dir)
    canonical = service.resolve_scope_id("project", ctx)
    cwd_hash = hashlib.sha256(os.path.realpath(repo).encode()).hexdigest()[:12]
    assert canonical != cwd_hash

    legacy_service = MemoryService(base_dir=memory_dir)
    legacy_service.resolve_scope_id = (  # type: ignore[method-assign]
        lambda scope, terminal_context: cwd_hash if scope == "project" else None
    )
    asyncio.run(
        legacy_service.store(
            content="memory retained in the cwd-hash folder",
            scope="project",
            memory_type="project",
            key="legacy-project-note",
            terminal_context=ctx,
        )
    )

    block = service.get_memory_context(ctx, budget_chars=9000)

    assert "memory retained in the cwd-hash folder" in block
    assert re.search(r'^<cao-memory generated="[^"]+">', block)

    from cli_agent_orchestrator.services import settings_service

    original_get_memory_settings = settings_service.get_memory_settings

    def legacy_settings() -> dict:
        settings = original_get_memory_settings()
        settings["injection"] = {"clear_on_empty": True, "v1_fixes": False}
        return settings

    monkeypatch.setattr(settings_service, "get_memory_settings", legacy_settings)
    assert service.get_memory_context(ctx, budget_chars=9000) == ""


def test_injection_settings_default_to_hygiene_fixes() -> None:
    from cli_agent_orchestrator.services.settings_service import get_memory_settings

    assert get_memory_settings()["injection"] == {
        "clear_on_empty": True,
        "v1_fixes": True,
    }


def test_idempotency_compares_only_the_uniquely_delimited_managed_block() -> None:
    begin_marker = "<!-- cao-memory:begin -->"
    end_marker = "<!-- cao-memory:end -->"
    content = (
        "# User example\n"
        "<cao-memory>SAME</cao-memory>\n"
        f"{begin_marker}\n"
        '<cao-memory generated="2000-01-01T00:00:00Z">OLD</cao-memory>\n'
        f"{end_marker}\n"
    )

    managed = uniquely_delimited_block(content, begin_marker, end_marker)

    assert managed is not None
    assert not memory_blocks_have_same_body(managed, "<cao-memory>SAME</cao-memory>")
