"""Discriminated absent-lint enrichment causes for the memory graph provider."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cli_agent_orchestrator.graph.providers.memory import MemoryGraphProvider
from cli_agent_orchestrator.services import wiki_lint
from cli_agent_orchestrator.services.vault.binding import NativeBinding, VaultBinding
from cli_agent_orchestrator.services.vault.config import FolderMapping
from cli_agent_orchestrator.services.vault.reader import VaultCandidateResolution


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
        binding_resolver=lambda scope, scope_id: NativeBinding(scope, scope_id),
    )

    view = await provider._build("global", None, lint_enabled=False)

    run_lint.assert_not_called()
    assert view.meta["lint_enrichment"] == "disabled_by_setting"


@pytest.mark.asyncio
async def test_lint_enrichment_cause_unavailable_vault(tmp_path, monkeypatch) -> None:
    run_lint = AsyncMock(return_value=[])
    monkeypatch.setattr(wiki_lint, "run_lint", run_lint)
    monkeypatch.setattr(
        "cli_agent_orchestrator.graph.providers.memory.resolve_candidates",
        lambda *_args, **_kwargs: VaultCandidateResolution((), "explicit_recall", "candidates"),
    )
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


def _graph_candidate(key: str):
    return SimpleNamespace(metadata=SimpleNamespace(key=key))


@pytest.mark.asyncio
async def test_vault_nodes_use_candidates_without_reading_note_content(
    tmp_path, monkeypatch
) -> None:
    """The graph receives indexed metadata only; it never needs a note body."""
    run_lint = AsyncMock(return_value=[])
    monkeypatch.setattr(wiki_lint, "run_lint", run_lint)
    binding = _vault_binding(tmp_path)
    service = _IndexService(tmp_path / "index.md")
    provider = MemoryGraphProvider(
        memory_service=service,
        lint_enabled=lambda: True,
        binding_resolver=lambda _scope, _scope_id: binding,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.graph.providers.memory.resolve_candidates",
        lambda *_args, **_kwargs: VaultCandidateResolution(
            (_graph_candidate("design"),),
            "explicit_recall",
            "candidates",
        ),
    )

    view = await provider._build("global", None, lint_enabled=True)

    assert [(node.id, node.label, node.attrs) for node in view.nodes] == [
        ("design", "design", {"is_vault": True})
    ]
    assert view.meta["lint_enrichment"] == "unavailable_vault"
    run_lint.assert_not_called()
    assert not view.edges


@pytest.mark.asyncio
async def test_agent_scope_omission_is_visible_in_graph_metadata(tmp_path, monkeypatch) -> None:
    """Unknown server identity refuses agent nodes and says the graph is partial."""
    run_lint = AsyncMock(return_value=[])
    monkeypatch.setattr(wiki_lint, "run_lint", run_lint)
    binding = VaultBinding(
        scope="agent",
        scope_id="fixture-agent",
        vault_id="vault",
        root=str(tmp_path),
        mapping=FolderMapping(folder="Agents", scope="agent", scope_id="fixture-agent"),
    )
    service = _IndexService(tmp_path / "index.md")
    provider = MemoryGraphProvider(
        memory_service=service,
        lint_enabled=lambda: True,
        binding_resolver=lambda _scope, _scope_id: binding,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.graph.providers.memory.resolve_candidates",
        lambda *_args, **_kwargs: VaultCandidateResolution(
            (),
            "memory_manager",
            "curator_agent_scope_refused",
        ),
    )

    view = await provider._build("agent", "fixture-agent", lint_enabled=True)

    assert view.nodes == []
    assert view.meta["agent_scope_omitted"] is True
    assert view.meta["lint_enrichment"] == "unavailable_vault"
    run_lint.assert_not_called()
