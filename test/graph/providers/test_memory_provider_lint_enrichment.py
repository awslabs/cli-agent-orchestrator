"""Discriminated absent-lint enrichment causes for the memory graph provider."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cli_agent_orchestrator.graph.providers.memory import MemoryGraphProvider
from cli_agent_orchestrator.services import wiki_lint
from cli_agent_orchestrator.services.vault.binding import NativeBinding, VaultBinding
from cli_agent_orchestrator.services.vault.config import FolderMapping


class _IndexService:
    """Minimal native-index surface: all tests reach the enrichment branch."""

    def __init__(self, index_path: Path) -> None:
        self._index_path = index_path
        index_path.write_text("# Memory Index\n", encoding="utf-8")

    def get_index_path(self, scope: str, scope_id: str | None) -> Path:
        assert (scope, scope_id) == ("global", None)
        return self._index_path

    def _parse_index(self, index_path: Path) -> list[dict[str, str]]:
        assert index_path == self._index_path
        return []


def _vault_binding(tmp_path: Path) -> VaultBinding:
    return VaultBinding(
        scope="global",
        scope_id=None,
        vault_id="vault",
        root=str(tmp_path),
        mapping=FolderMapping(folder="Notes", scope="global"),
    )


@pytest.mark.asyncio
async def test_lint_enrichment_cause_disabled_by_setting(tmp_path, monkeypatch) -> None:
    run_lint = AsyncMock(return_value=[])
    monkeypatch.setattr(wiki_lint, "run_lint", run_lint)
    provider = MemoryGraphProvider(
        memory_service=_IndexService(tmp_path / "index.md"),
        lint_enabled=lambda: False,
        binding_resolver=lambda scope, scope_id: pytest.fail("binding must not be resolved"),
    )

    view = await provider._build("global", None, lint_enabled=False)

    run_lint.assert_not_called()
    assert view.meta["lint_enrichment"] == "disabled_by_setting"


@pytest.mark.asyncio
async def test_lint_enrichment_cause_unavailable_vault(tmp_path, monkeypatch) -> None:
    run_lint = AsyncMock(return_value=[])
    monkeypatch.setattr(wiki_lint, "run_lint", run_lint)
    provider = MemoryGraphProvider(
        memory_service=_IndexService(tmp_path / "index.md"),
        lint_enabled=lambda: True,
        binding_resolver=lambda scope, scope_id: _vault_binding(tmp_path),
    )

    view = await provider._build("global", None, lint_enabled=True)

    run_lint.assert_not_called()
    assert view.meta["lint_enrichment"] == "unavailable_vault"


@pytest.mark.asyncio
async def test_lint_enrichment_cause_failed_for_native_scope(tmp_path, monkeypatch) -> None:
    async def fail_lint(project_hash, *, scope=None, **kwargs):
        raise RuntimeError("lint backend unavailable")

    monkeypatch.setattr(wiki_lint, "run_lint", fail_lint)
    provider = MemoryGraphProvider(
        memory_service=_IndexService(tmp_path / "index.md"),
        lint_enabled=lambda: True,
        binding_resolver=lambda scope, scope_id: NativeBinding(scope, scope_id),
    )

    view = await provider._build("global", None, lint_enabled=True)

    assert view.meta["lint_enrichment"] == "failed"
    assert view.meta["lint_error"] == "RuntimeError"
