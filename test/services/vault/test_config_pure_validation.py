"""Vault configuration validation must be pure: it never consults the database.

Issue #644 follow-up. ``VaultConfig.model_validate`` used to resolve project
aliases through ``clients.database`` from inside a ``model_validator``, so
validating a document with a project mapping failed on a CAO home whose
database had never been initialised, and the outcome of validation depended on
runtime state. Alias canonicalisation now lives on the loading path
(``settings_service.get_vault_config``), which is where a database exists.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import cli_agent_orchestrator
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import config_service, settings_service
from cli_agent_orchestrator.services.vault import binding
from cli_agent_orchestrator.services.vault.config import VaultConfig


def _document(root: str) -> dict:
    return {
        "enabled": True,
        "vaults": [
            {
                "id": "primary",
                "root": root,
                "managed_folder": "CAO",
                "mappings": [
                    {"folder": "CAO", "scope": "agent", "scope_id": "writer", "writable": True},
                    {"folder": "Projects", "scope": "project", "scope_id": "old-project"},
                ],
            }
        ],
    }


def _unavailable_session_local():
    raise RuntimeError("project alias database unavailable")


# ---------------------------------------------------------------------------
# Reproduction: a clean CAO_HOME_DIR has no database and validation must not care
# ---------------------------------------------------------------------------


def test_vault_config_validates_on_a_clean_cao_home_without_a_database(tmp_path):
    """Validate a project-mapped document in a subprocess whose home has never run init_db()."""
    home = tmp_path / "home"
    vault_root = tmp_path / "vault"
    (vault_root / "CAO").mkdir(parents=True)
    (vault_root / "Projects").mkdir()
    script = (
        "import json, sys\n"
        "from cli_agent_orchestrator.services.vault.config import VaultConfig\n"
        "config = VaultConfig.model_validate(json.loads(sys.argv[1]))\n"
        "print(json.dumps([m.scope_id for m in config.vaults[0].mappings]))\n"
    )
    env = {**os.environ, "CAO_HOME_DIR": str(home)}
    env.pop("CAO_MEMORY_VAULT_ENABLED", None)
    result = subprocess.run(
        [sys.executable, "-c", script, json.dumps(_document(str(vault_root)))],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert json.loads(result.stdout.strip()) == ["writer", "old-project"]
    assert not (home / "db" / "cli-agent-orchestrator.db").exists() or (
        (home / "db" / "cli-agent-orchestrator.db").stat().st_size == 0
    ), "pure validation must not create or populate the database"


def test_vault_config_validation_never_opens_a_database_session(tmp_path, monkeypatch):
    (tmp_path / "CAO").mkdir()
    monkeypatch.setattr(database, "SessionLocal", _unavailable_session_local)

    config = VaultConfig.model_validate(_document(str(tmp_path)))

    assert [mapping.scope_id for mapping in config.vaults[0].mappings] == ["writer", "old-project"]


def test_literal_duplicate_mapping_identity_is_still_rejected_purely(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "SessionLocal", _unavailable_session_local)
    document = _document(str(tmp_path))
    document["vaults"][0]["mappings"].append(
        {"folder": "Elsewhere", "scope": "project", "scope_id": "OLD-PROJECT"}
    )

    with pytest.raises(ValueError, match=r"same \(scope, scope_id\)"):
        VaultConfig.model_validate(document)


def test_config_list_lookup_works_without_a_database(tmp_path, monkeypatch):
    """``cao config list`` is introspection and must not need the alias table."""
    (tmp_path / "CAO").mkdir()
    monkeypatch.setattr(database, "SessionLocal", _unavailable_session_local)
    monkeypatch.setattr(
        settings_service, "_load", lambda: {"memory": {"vault": _document(str(tmp_path))}}
    )

    value = config_service._get_value("memory.vault")

    assert value["enabled"] is True
    assert [m["scope_id"] for m in value["vaults"][0]["mappings"]] == ["writer", "old-project"]


# ---------------------------------------------------------------------------
# Behaviour with a database is unchanged, now on the loading path
# ---------------------------------------------------------------------------


def _settings(monkeypatch, document: dict) -> None:
    monkeypatch.setattr(settings_service, "_load", lambda: {"memory": {"vault": document}})


def test_loader_canonicalises_configured_project_aliases(tmp_path, monkeypatch):
    (tmp_path / "CAO").mkdir()
    _settings(monkeypatch, _document(str(tmp_path)))
    monkeypatch.setattr(
        binding,
        "get_project_id_by_alias",
        lambda scope_id, **_kwargs: "canonical-project" if scope_id == "old-project" else None,
    )

    config = settings_service.get_vault_config()

    assert [m.scope_id for m in config.vaults[0].mappings] == ["writer", "canonical-project"]
    assert config.enabled is True


def test_loader_rejects_alias_equivalent_project_mappings(tmp_path, monkeypatch):
    document = _document(str(tmp_path))
    document["vaults"][0]["mappings"].append(
        {"folder": "Elsewhere", "scope": "project", "scope_id": "renamed-project"}
    )
    _settings(monkeypatch, document)
    monkeypatch.setattr(
        binding,
        "get_project_id_by_alias",
        lambda scope_id, **_kwargs: (
            "canonical-project" if scope_id in {"old-project", "renamed-project"} else None
        ),
    )

    with pytest.raises(ValueError, match=r"same \(scope, scope_id\)"):
        settings_service.get_vault_config()


def test_loader_fails_closed_when_the_alias_table_is_unavailable(tmp_path, monkeypatch):
    _settings(monkeypatch, _document(str(tmp_path)))
    monkeypatch.setattr(database, "SessionLocal", _unavailable_session_local)

    with pytest.raises(
        database.ProjectAliasLookupUnavailableError,
        match=r"^project alias database unavailable$",
    ):
        settings_service.get_vault_config()


def test_loader_keeps_warnings_and_env_gate_after_canonicalisation(tmp_path, monkeypatch):
    document = _document(str(tmp_path))
    document["vaults"][0]["mappings"][1].update(secret_gate="warn", inject=True)
    _settings(monkeypatch, document)
    monkeypatch.setattr(binding, "get_project_id_by_alias", lambda *_a, **_k: "canonical-project")
    monkeypatch.setenv("CAO_MEMORY_VAULT_ENABLED", "false")

    config = settings_service.get_vault_config()

    assert config.enabled is False
    assert config.warnings == ("mapping 'Projects' has secret_gate='warn' with inject=true",)
    assert config.vaults[0].mappings[1].scope_id == "canonical-project"


def test_loader_can_skip_canonicalisation_for_pure_introspection(tmp_path, monkeypatch):
    _settings(monkeypatch, _document(str(tmp_path)))
    monkeypatch.setattr(database, "SessionLocal", _unavailable_session_local)

    config = settings_service.get_vault_config(canonicalize=False)

    assert config.vaults[0].mappings[1].scope_id == "old-project"


# ---------------------------------------------------------------------------
# Structural guard: no validator under services/vault may reach the database
# ---------------------------------------------------------------------------

_VAULT_DIR = Path(cli_agent_orchestrator.__file__).parent / "services" / "vault"
_DATABASE_MODULE_SUFFIX = "clients.database"
_VALIDATOR_DECORATORS = {"model_validator", "field_validator"}
# Names that reach the database regardless of how they were imported.
_ALWAYS_FORBIDDEN_NAMES = {"get_db", "SessionLocal", "init_db"}


def _decorator_name(node: ast.expr) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Name):
        return target.id
    return ""


def _database_bound_names(tree: ast.Module) -> set[str]:
    """Every local name that is (or is a member of) the database client module."""
    names: set[str] = set(_ALWAYS_FORBIDDEN_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.endswith(_DATABASE_MODULE_SUFFIX):
                names.update(alias.asname or alias.name for alias in node.names)
            elif module.endswith("clients") or module == "":
                names.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "database"
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith(_DATABASE_MODULE_SUFFIX):
                    names.add(alias.asname or alias.name.split(".")[0])
    return names


def _root_name(node: ast.expr) -> str:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def _module_functions(tree: ast.Module) -> dict[str, ast.AST]:
    functions: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions[f"{node.name}.{item.name}"] = item
    return functions


def _validators(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    found = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                _decorator_name(decorator) in _VALIDATOR_DECORATORS
                for decorator in item.decorator_list
            ):
                found.append((f"{node.name}.{item.name}", item))
    return found


def _callees(function: ast.AST, functions: dict[str, ast.AST], class_name: str) -> set[str]:
    """Module-level functions and same-class methods this function body calls."""
    callees: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name) and target.id in functions:
            callees.add(target.id)
        elif (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id in {"self", "cls"}
            and f"{class_name}.{target.attr}" in functions
        ):
            callees.add(f"{class_name}.{target.attr}")
    return callees


def _database_references(function: ast.AST, forbidden: set[str]) -> list[str]:
    hits = []
    for node in ast.walk(function):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", None) or ""
            targets = [module] + [alias.name for alias in node.names]
            if any(_DATABASE_MODULE_SUFFIX in target for target in targets):
                hits.append(f"import at line {node.lineno}")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in forbidden:
                hits.append(f"{node.id} at line {node.lineno}")
        elif isinstance(node, ast.Attribute):
            root = _root_name(node)
            if root in forbidden and root != node.attr:
                hits.append(f"{root}.{node.attr} at line {node.lineno}")
    return hits


def _validator_database_reaches(source: str) -> list[str]:
    """Return human-readable violations for one module's source text."""
    tree = ast.parse(source)
    forbidden = _database_bound_names(tree)
    functions = _module_functions(tree)
    violations = []
    for validator_name, validator in _validators(tree):
        class_name = validator_name.split(".")[0]
        reachable: list[tuple[str, ast.AST]] = [(validator_name, validator)]
        seen = {validator_name}
        queue = [(validator_name, validator)]
        while queue:
            name, function = queue.pop()
            for callee in _callees(function, functions, class_name):
                if callee not in seen:
                    seen.add(callee)
                    queue.append((callee, functions[callee]))
                    reachable.append((callee, functions[callee]))
        for name, function in reachable:
            for hit in _database_references(function, forbidden):
                via = "" if name == validator_name else f" via {name}"
                violations.append(f"{validator_name}{via}: {hit}")
    return violations


def test_no_vault_validator_reaches_the_database_client():
    modules = sorted(_VAULT_DIR.glob("*.py"))
    assert modules, "no vault modules found"
    violations = []
    for module in modules:
        for violation in _validator_database_reaches(module.read_text(encoding="utf-8")):
            violations.append(f"{module.name}: {violation}")

    assert not violations, "validators must not reach the database:\n" + "\n".join(violations)


_ORIGINAL_SHAPE = """
from pydantic import BaseModel, model_validator
from cli_agent_orchestrator.clients.database import get_project_id_by_alias

def canonical_scope_id(scope, scope_id):
    if scope != "project" or scope_id is None:
        return scope_id
    return get_project_id_by_alias(scope_id, fail_closed=True) or scope_id

def _mapping_identity(mapping):
    return mapping.scope, canonical_scope_id(mapping.scope, mapping.scope_id)

class VaultSpec(BaseModel):
    mappings: list = []

    @model_validator(mode="after")
    def validate_vault_rules(self):
        bindings = [_mapping_identity(mapping) for mapping in self.mappings]
        return self
"""

_LAZY_IMPORT_SHAPE = """
from pydantic import BaseModel, field_validator

class FolderMapping(BaseModel):
    scope_id: str

    @field_validator("scope_id")
    @classmethod
    def validate_scope_id(cls, value):
        from cli_agent_orchestrator.clients.database import get_db
        return value
"""

_MODULE_ALIAS_SHAPE = """
import cli_agent_orchestrator.clients.database as db
from pydantic import BaseModel, model_validator

class VaultConfig(BaseModel):
    @model_validator(mode="after")
    def check(self):
        return self._lookup()

    def _lookup(self):
        with db.SessionLocal():
            return self
"""

_CLEAN_SHAPE = """
from pydantic import BaseModel, model_validator
from cli_agent_orchestrator.clients.database import get_project_id_by_alias

def canonicalize(config):
    return get_project_id_by_alias(config, fail_closed=True)

class VaultSpec(BaseModel):
    mappings: list = []

    @model_validator(mode="after")
    def validate_vault_rules(self):
        if len(self.mappings) != len(set(self.mappings)):
            raise ValueError("duplicate")
        return self
"""


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (_ORIGINAL_SHAPE, "via canonical_scope_id"),
        (_ORIGINAL_SHAPE, "get_project_id_by_alias"),
        (_LAZY_IMPORT_SHAPE, "import at line"),
        (_MODULE_ALIAS_SHAPE, "db.SessionLocal"),
    ],
)
def test_guard_detects_transitive_lazy_and_aliased_database_reaches(source, expected):
    violations = _validator_database_reaches(source)

    assert any(expected in violation for violation in violations), violations


def test_guard_accepts_database_use_outside_validators():
    assert _validator_database_reaches(_CLEAN_SHAPE) == []
