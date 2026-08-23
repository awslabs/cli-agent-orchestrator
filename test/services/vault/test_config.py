"""Table-driven configuration tests for the vault security boundary."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from cli_agent_orchestrator.services.vault import config as vault_config
from cli_agent_orchestrator.services.vault.config import (
    ALWAYS_EXCLUDED_PATTERNS,
    VaultConfig,
)


def _document(root: str) -> dict:
    return {
        "enabled": True,
        "max_recall_body_chars": 4096,
        "vaults": [
            {
                "id": "primary",
                "root": root,
                "managed_folder": "CAO",
                "exclude": ["Private/**"],
                "mappings": [
                    {
                        "folder": "CAO",
                        "scope": "agent",
                        "scope_id": "writer",
                        "writable": True,
                    },
                    {
                        "folder": "References",
                        "scope": "global",
                    },
                ],
            }
        ],
    }


def _load(document: dict) -> VaultConfig:
    return VaultConfig.model_validate(document)


def _set_invalid_managed_folder(document: dict, root: Path) -> None:
    document["vaults"][0]["managed_folder"] = "CAO Notes"
    document["vaults"][0]["mappings"][0]["folder"] = "CAO Notes"


def test_folder_accepts_real_vault_characters(tmp_path):
    document = _document(str(tmp_path))
    document["vaults"][0]["mappings"][1]["folder"] = "Références/Don't Panic, (v2)"

    config = _load(document)

    assert config.vaults[0].mappings[1].folder == "Références/Don't Panic, (v2)"


@pytest.mark.parametrize(
    ("rule", "mutate", "match"),
    [
        (
            1,
            lambda document, root: document.update(enabled=True, vaults=[]),
            "at least one vault",
        ),
        (
            2,
            lambda document, root: document["vaults"].append(
                deepcopy(document["vaults"][0])
            ),
            "only one vault",
        ),
        (
            3,
            lambda document, root: document["vaults"][0].update(id="Not valid"),
            "id must match",
        ),
        (
            4,
            lambda document, root: document["vaults"][0].update(
                root=str(root / "missing")
            ),
            "root",
        ),
        (
            6,
            lambda document, root: document["vaults"][0].update(root=str(Path.home())),
            "home directory",
        ),
        (
            7,
            lambda document, root: document["vaults"][0]["mappings"][1].update(
                folder="../escape"
            ),
            "folder",
        ),
        (
            8,
            _set_invalid_managed_folder,
            r"must match \^\[A-Za-z0-9._-\]\+\$",
        ),
        (
            9,
            lambda document, root: document["vaults"][0]["mappings"][1].update(
                folder="CAO/nested"
            ),
            "must not overlap",
        ),
        (
            10,
            lambda document, root: document["vaults"][0].update(
                managed_folder="Unmapped"
            ),
            "managed_folder",
        ),
        (
            11,
            lambda document, root: document["vaults"][0]["mappings"][1].update(
                scope="federated"
            ),
            "scope",
        ),
        (
            12,
            lambda document, root: document["vaults"][0]["mappings"][0].update(
                scope_id="..."
            ),
            "scope_id",
        ),
        (
            13,
            lambda document, root: document["vaults"][0]["mappings"][1].update(
                folder="Other", scope="agent", scope_id="writer"
            ),
            r"same \(scope, scope_id\)",
        ),
        (
            14,
            lambda document, root: document["vaults"][0].update(
                exclude=["../Private/**"]
            ),
            "exclude",
        ),
        (
            15,
            lambda document, root: document["vaults"][0]["mappings"][1].update(
                index=False, inject=True
            ),
            "requires index",
        ),
        (
            16,
            lambda document, root: document["vaults"][0]["mappings"][1].update(
                secret_gate="ignore"
            ),
            "secret_gate",
        ),
        (
            17,
            lambda document, root: document["vaults"][0]["mappings"][1].update(
                allow_hardlinks="false"
            ),
            "allow_hardlinks",
        ),
        (
            18,
            lambda document, root: document.update(max_recall_body_chars=0),
            "max_recall_body_chars",
        ),
    ],
    ids=lambda value: f"rule-{value}" if isinstance(value, int) else None,
)
def test_validation_rules_reject_invalid_documents(tmp_path, rule, mutate, match):
    document = _document(str(tmp_path))
    mutate(document, tmp_path)

    with pytest.raises(ValidationError, match=match):
        _load(document)


def test_rule_5_rejects_memory_base_overlap(tmp_path, monkeypatch):
    document = _document(str(tmp_path))
    monkeypatch.setattr(vault_config, "MEMORY_BASE_DIR", tmp_path)

    with pytest.raises(ValidationError, match="MEMORY_BASE_DIR"):
        _load(document)


@pytest.mark.parametrize("folder", [".", "./CAO"])
def test_rule_7_rejects_dot_segments_that_bypass_mapping_overlap(tmp_path, folder):
    document = _document(str(tmp_path))
    document["vaults"][0]["mappings"][1].update(folder=folder, inject=True)

    with pytest.raises(ValidationError, match=r"must not contain '\.' or '\.\.'"):
        _load(document)


def test_rule_9_casefolds_mapping_paths_before_prefix_comparison():
    assert vault_config._is_path_prefix("CAO", "cao")
    assert vault_config._is_path_prefix("Références", "Références")


@pytest.mark.parametrize("pattern", ["./Private/**", "Private/./**", "."])
def test_rule_14_rejects_dot_segments_in_exclude_patterns(tmp_path, pattern):
    document = _document(str(tmp_path))
    document["vaults"][0]["exclude"] = [pattern]

    with pytest.raises(ValidationError) as exc_info:
        _load(document)

    assert exc_info.value.errors()[0]["msg"] == (
        "Value error, exclude patterns must be relative and not contain '.' or '..' path segments"
    )


def test_rule_14_accepts_normal_relative_exclude_pattern(tmp_path):
    document = _document(str(tmp_path))
    document["vaults"][0]["exclude"] = ["Private/**"]

    config = _load(document)

    assert config.vaults[0].exclude == ["Private/**"]


def test_rule_6_rejects_graph_export_root_overlap(tmp_path, monkeypatch):
    document = _document(str(tmp_path))
    monkeypatch.setattr(vault_config, "graph_export_root", lambda: tmp_path)

    with pytest.raises(ValidationError, match="graph export root"):
        _load(document)


def test_rule_10_rejects_a_second_writable_mapping(tmp_path):
    document = _document(str(tmp_path))
    document["vaults"][0]["mappings"][1]["writable"] = True

    with pytest.raises(ValidationError, match="only the managed_folder mapping"):
        _load(document)


@pytest.mark.parametrize("scope", ["federated", "session"])
def test_rule_11_refuses_unmappable_scopes(tmp_path, scope):
    document = _document(str(tmp_path))
    document["vaults"][0]["mappings"][1]["scope"] = scope

    with pytest.raises(ValidationError, match="scope"):
        _load(document)


def test_rule_12_forbids_global_scope_id(tmp_path):
    document = _document(str(tmp_path))
    document["vaults"][0]["mappings"][1]["scope_id"] = "not-allowed"

    with pytest.raises(ValidationError, match="forbidden for global"):
        _load(document)


@pytest.mark.parametrize(
    "key",
    ["max_note_bytes", "max_notes", "max_frontmatter_bytes"],
)
def test_rule_18_rejects_disableable_vault_bounds(tmp_path, key):
    document = _document(str(tmp_path))
    document["vaults"][0][key] = 0

    with pytest.raises(ValidationError, match=key):
        _load(document)


@pytest.mark.parametrize(
    ("key", "limit", "target"),
    [
        ("max_note_bytes", 1048576, "vault"),
        ("max_notes", 100000, "vault"),
        ("max_frontmatter_bytes", 65536, "vault"),
        ("max_recall_body_chars", 65536, "config"),
    ],
)
def test_rule_18_rejects_values_over_every_ceiling(tmp_path, key, limit, target):
    document = _document(str(tmp_path))
    if target == "vault":
        document["vaults"][0][key] = limit + 1
    else:
        document[key] = limit + 1

    with pytest.raises(ValidationError, match=key):
        _load(document)


def test_rule_19_always_excluded_paths_are_fixed_data():
    assert ALWAYS_EXCLUDED_PATTERNS == (".obsidian/", ".trash/", ".git/", "_cao-*")


def test_secret_gate_defaults_to_reject(tmp_path):
    config = _load(_document(str(tmp_path)))

    assert config.vaults[0].mappings[0].secret_gate == "reject"


def test_rule_20_warn_and_inject_loads_with_persistent_warning(tmp_path, caplog):
    document = _document(str(tmp_path))
    document["vaults"][0]["mappings"][1].update(inject=True, secret_gate="warn")

    config = _load(document)

    assert config.warnings == (
        "mapping 'References' has secret_gate='warn' with inject=true",
    )
    assert "secret_gate='warn' with inject=true" in caplog.text


def test_recall_budget_warns_when_it_exceeds_the_injection_scope_budget(
    tmp_path, caplog
):
    _load(_document(str(tmp_path)))

    assert "exceeds the injection scope budget" in caplog.text
