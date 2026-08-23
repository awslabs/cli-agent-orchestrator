"""Tests for vault scope binding resolution."""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.models.relationship import VALID_ORIGINS
from cli_agent_orchestrator.services import settings_service
from cli_agent_orchestrator.services.vault import binding
from cli_agent_orchestrator.services.vault.config import (
    FolderMapping,
    VaultConfig,
    VaultSpec,
)


def _config(tmp_path) -> VaultConfig:
    root = tmp_path / "vault"
    root.mkdir()
    return VaultConfig(
        enabled=True,
        vaults=[
            VaultSpec(
                id="primary",
                root=str(root),
                managed_folder="CAO",
                mappings=[
                    FolderMapping(
                        folder="Projects",
                        scope="project",
                        scope_id="canonical-project",
                    ),
                    FolderMapping(folder="CAO", scope="global", writable=True),
                ],
            )
        ],
    )


def test_resolve_canonicalises_project_alias_before_matching(
    tmp_path, monkeypatch
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(
        binding,
        "get_project_id_by_alias",
        lambda scope_id: "canonical-project" if scope_id == "old-project" else None,
    )

    resolved = binding.resolve("project", "old-project", vault_config=config)

    assert isinstance(resolved, binding.VaultBinding)
    assert resolved.scope_id == "canonical-project"
    assert resolved.vault_id == "primary"


def test_resolve_canonicalises_configured_project_alias_and_case_variant(
    tmp_path, monkeypatch
) -> None:
    config = _config(tmp_path)
    stale_mapping = (
        config.vaults[0].mappings[0].model_copy(update={"scope_id": "old-project"})
    )
    config.vaults[0].mappings[0] = stale_mapping
    monkeypatch.setattr(
        binding,
        "get_project_id_by_alias",
        lambda scope_id: "canonical-project" if scope_id == "old-project" else None,
    )

    resolved = binding.resolve("project", "CANONICAL-PROJECT", vault_config=config)

    assert isinstance(resolved, binding.VaultBinding)
    assert resolved.scope_id == "canonical-project"
    assert resolved.mapping.scope_id == "old-project"


def test_load_vault_config_refuses_when_configuration_is_unavailable(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(
        settings_service,
        "get_vault_config",
        lambda: (_ for _ in ()).throw(
            ValueError("vault root 'missing' does not exist")
        ),
    )

    with pytest.raises(
        binding.VaultConfigUnavailableError,
        match=r"^vault configuration unavailable: vault root 'missing' does not exist$",
    ):
        binding._load_vault_config()

    assert (
        "vault binding configuration unavailable: vault root 'missing' does not exist"
        in caplog.messages
    )


def test_unmapped_project_write_warns_and_increments_counter(tmp_path, caplog) -> None:
    binding._reset_unmapped_project_write_count()
    config = _config(tmp_path)

    binding.record_unmapped_project_write("unmapped-project", vault_config=config)

    assert binding.unmapped_project_write_count() == 1
    assert binding.unmapped_project_write_count("unmapped-project") == 1
    assert (
        "unmapped_project_write scope_id='unmapped-project' count=1; "
        "writing native wiki while project vault mappings exist"
    ) in caplog.messages


def test_valid_origins_are_the_full_closed_vault_vocabulary() -> None:
    assert VALID_ORIGINS == frozenset(
        {
            "compiler",
            "wiki_lint",
            "human",
            "legacy_related_keys",
            "external_import",
            "vault",
        }
    )


def test_collect_binding_warnings_are_complete_deterministic_and_content_free(
    tmp_path, monkeypatch
) -> None:
    binding._reset_unmapped_project_write_count()
    root = tmp_path / "vault-warnings"
    root.mkdir()
    config = VaultConfig(
        enabled=True,
        vaults=[
            VaultSpec(
                id="primary",
                root=str(root),
                managed_folder="CAO",
                mappings=[
                    FolderMapping(
                        folder="Orphaned",
                        scope="project",
                        scope_id="missing-project",
                    ),
                    FolderMapping(
                        folder="Cwd Hash",
                        scope="project",
                        scope_id="deadbeefcafe",
                    ),
                    FolderMapping(folder="CAO", scope="global", writable=True),
                ],
            )
        ],
    )
    monkeypatch.setattr(
        binding,
        "get_project_id_by_alias",
        lambda scope_id: (
            "github-com-acme-widgets" if scope_id == "deadbeefcafe" else None
        ),
    )
    monkeypatch.setattr(
        binding,
        "list_aliases_for_project",
        lambda project_id: (
            [
                {
                    "project_id": "github-com-acme-widgets",
                    "alias": "deadbeefcafe",
                    "kind": "cwd_hash",
                }
            ]
            if project_id == "github-com-acme-widgets"
            else []
        ),
    )
    note_content = "credential: secret-note-body-must-not-surface"
    binding.record_unmapped_project_write("native-project", vault_config=config)

    warnings = binding.collect_binding_warnings(config)

    assert [warning.kind for warning in warnings] == [
        "cwd_hash_scope_id",
        "orphaned_mapping",
        "unmapped_project_write",
    ]
    assert warnings[0].mapping == "Cwd Hash"
    assert warnings[1].mapping == "Orphaned"
    assert warnings[2].mapping == ""
    assert [warning.detail for warning in warnings] == [
        "project scope_id 'deadbeefcafe' is a cwd-hash alias and may change after a folder rename",
        "project scope_id 'missing-project' is not a known project id or alias",
        "1 native project write(s) missed configured vault mappings",
    ]
    assert all(note_content not in warning.detail for warning in warnings)
    assert all("secret-note-body" not in warning.detail for warning in warnings)
    binding._reset_unmapped_project_write_count()
