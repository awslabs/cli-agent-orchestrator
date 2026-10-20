"""Vault scopes cannot silently export empty native OKF bundles."""

import pytest

from cli_agent_orchestrator.services.memory_archive.okf import OkfArchiveBackend
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.vault.binding import VaultBinding
from cli_agent_orchestrator.services.vault.config import FolderMapping


def test_okf_export_refuses_a_vault_bound_scope_before_native_collection(monkeypatch, tmp_path):
    """The exact refusal prevents a misleading empty native archive."""
    from cli_agent_orchestrator.services.vault import binding

    vault_binding = VaultBinding(
        scope="global",
        scope_id=None,
        vault_id="fixture",
        root=str(tmp_path / "vault"),
        mapping=FolderMapping(folder="CAO", scope="global", writable=True),
    )
    monkeypatch.setattr(binding, "resolve", lambda *_args, **_kwargs: vault_binding)

    with pytest.raises(
        ValueError, match=r"^vault-bound scopes cannot be exported as native OKF bundles$"
    ):
        OkfArchiveBackend(MemoryService()).export_bundle(
            "global", None, tmp_path / "out", False, False
        )
