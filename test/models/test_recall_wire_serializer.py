"""Structural guard: one serializer builds every recalled-memory payload (#968, #1034).

Two client surfaces return recalled memories -- the MCP ``memory_recall`` tool
and ``POST /internal/memory/recall`` -- and until this change each decided its
own wire shape at its own call site: the route dumped the model wholesale, the
tool hand-built a dict from a key allowlist. #1034 is what that costs. A new
``Memory.redacted_patterns`` field widened the route and was invisible on the
tool, so only the route's #968 pin reported it, and the shape had to be fixed
in one place and re-checked in another.

``test/models/test_memory_wire_shape.py`` (#1034) pins *which fields the model
serializes*. This module pins *that both surfaces go through one function to
serialize them*, so a hand-built dict cannot come back:

* ``recall_wire_dict`` is the full payload the internal route returns;
* ``mcp_recall_wire_dict`` is a projection of that same dict through the
  declared ``MCP_RECALL_KEYS``, and the keys it deviates on are enumerated here
  rather than left to whoever reads the two call sites.

The MCP surface stays narrower than the route's -- #968 pins them as different
shapes and this is a refactor, not a contract change. What changed is that the
narrowing is now one declared tuple next to the model instead of a second
hand-written dict in another module.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from test._support import callsite_lint as cl
from typing import Dict, Optional, Tuple

import pytest

from cli_agent_orchestrator.models import memory as memory_model
from cli_agent_orchestrator.models.memory import (
    MCP_RECALL_KEYS,
    Memory,
    mcp_recall_wire_dict,
    recall_wire_dict,
)

# The two recall surfaces, as (module relpath, function name, serializer name).
# Both entries are load-bearing: dropping one is how this guard would quietly
# stop guarding, so the parametrization is pinned by count below.
RECALL_SURFACES: Tuple[Tuple[str, str, str], ...] = (
    ("cli_agent_orchestrator/mcp_server/server.py", "memory_recall", "mcp_recall_wire_dict"),
    ("cli_agent_orchestrator/api/main.py", "internal_memory_recall", "recall_wire_dict"),
)

# Keys whose value the MCP projection deliberately renders differently from the
# shared dict. Everything else must come through untouched. Widening this set is
# a client-visible change to the MCP recall contract pinned in
# ``test/services/test_memory_861_default_parity_extended.py`` (``MCP_PIN``).
MCP_VALUE_DEVIATIONS = frozenset({"file_path", "indexed_at", "updated_at"})

# A dict display inside a recall handler that names this many recall keys is a
# hand-built memory payload. Three, because the only dict literal either
# handler legitimately builds is the ``recall()`` kwargs, which names two
# (``scope``, ``memory_type``); a reintroduced payload names twelve.
_HAND_BUILT_KEY_OVERLAP = 3


def _memory(**overrides: object) -> Memory:
    """A fully populated recalled memory: every optional field set, so a
    projection that drops or mangles one shows up as a value diff."""
    fields: Dict[str, object] = {
        "id": "00000000-0000-0000-0000-000000000001",
        "key": "pin-key",
        "memory_type": "project",
        "scope": "project",
        "scope_id": "pin-project",
        "file_path": "/abs/root/pin-project/wiki/project/pin-key.md",
        "tags": "a,b",
        "source_provider": "claude_code",
        "source_terminal_id": "term-1",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc),
        "access_count": 3,
        "last_compiled_at": datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
        "related_keys": "other-key",
        "content": "body",
        "source_kind": "native",
        "source_path": None,
        "indexed_at": None,
        "index_freshness": None,
        "content_truncated": True,
        "token_estimate": 7,
    }
    fields.update(overrides)
    return Memory(**fields)  # type: ignore[arg-type]


def _vault_memory(**overrides: object) -> Memory:
    return _memory(
        source_kind="vault",
        source_path="Projects/CAO Design/Note.md",
        file_path="/abs/vault/Projects/CAO Design/Note.md",
        indexed_at=datetime(2026, 1, 3, tzinfo=timezone(timedelta(hours=-5))),
        index_freshness="stale",
        **overrides,
    )


# ---------------------------------------------------------------------------
# The projection is a projection
# ---------------------------------------------------------------------------


def test_mcp_keys_are_a_subset_of_the_shared_dict() -> None:
    """Every projected key exists in the shared dict, so the projection cannot
    invent a field the model does not serialize."""
    shared = set(recall_wire_dict(_memory()))
    assert set(MCP_RECALL_KEYS) <= shared, set(MCP_RECALL_KEYS) - shared


def test_mcp_keys_are_declared_once_each() -> None:
    assert len(MCP_RECALL_KEYS) == len(set(MCP_RECALL_KEYS))


@pytest.mark.parametrize("builder", [_memory, _vault_memory], ids=["native", "vault"])
def test_mcp_dict_is_the_declared_keys_in_declared_order(builder) -> None:
    assert tuple(mcp_recall_wire_dict(builder())) == MCP_RECALL_KEYS


@pytest.mark.parametrize("builder", [_memory, _vault_memory], ids=["native", "vault"])
def test_mcp_values_come_from_the_shared_dict_except_the_declared_deviations(builder) -> None:
    memory = builder()
    shared = recall_wire_dict(memory)
    projected = mcp_recall_wire_dict(memory)
    differing = {key for key, value in projected.items() if shared[key] != value}
    assert differing <= MCP_VALUE_DEVIATIONS, differing - MCP_VALUE_DEVIATIONS


def test_the_declared_deviations_all_actually_deviate() -> None:
    """A deviation that no longer deviates means the override is dead code and
    the key should be taken off this list -- the set shrinks, like the
    call-site lint allowlists."""
    memory = _vault_memory()
    shared = recall_wire_dict(memory)
    projected = mcp_recall_wire_dict(memory)
    assert {key for key in MCP_VALUE_DEVIATIONS if shared[key] != projected[key]} == (
        MCP_VALUE_DEVIATIONS
    )


@pytest.mark.parametrize("builder", [_memory, _vault_memory], ids=["native", "vault"])
def test_lifecycle_status_appears_on_both_surfaces_only_when_set(builder) -> None:
    """``lifecycle_status`` is ``exclude=True`` on the model (it is never
    persisted) and recall reports it anyway, at opposite ends of the two
    payloads. #968 pins both positions."""
    unset = builder()
    assert "lifecycle_status" not in recall_wire_dict(unset)
    assert "lifecycle_status" not in mcp_recall_wire_dict(unset)

    labelled = builder(lifecycle_status="superseded")
    shared = recall_wire_dict(labelled)
    projected = mcp_recall_wire_dict(labelled)
    assert list(shared)[-1] == "lifecycle_status"
    assert list(projected)[0] == "lifecycle_status"
    assert shared["lifecycle_status"] == projected["lifecycle_status"] == "superseded"


def test_shared_dict_round_trips_through_the_model() -> None:
    """The memory gateway rebuilds a ``Memory`` from the internal route's
    payload (``memory_gateway.recall_memory``), so the shared dict has to stay
    ``model_validate``-able -- including the excluded lifecycle label."""
    memory = _vault_memory(lifecycle_status="superseded")
    restored = Memory.model_validate(recall_wire_dict(memory))
    assert recall_wire_dict(restored) == recall_wire_dict(memory)
    assert restored.lifecycle_status == "superseded"


# ---------------------------------------------------------------------------
# Both surfaces go through the shared serializer
# ---------------------------------------------------------------------------


def test_both_recall_surfaces_are_covered() -> None:
    """Guard on the guard: two surfaces exist, so two rows are checked."""
    assert len(RECALL_SURFACES) == 2


@pytest.mark.parametrize("relpath,func,serializer", RECALL_SURFACES)
def test_surface_imports_the_shared_serializer(relpath: str, func: str, serializer: str) -> None:
    """Module-attribute half: the name each handler calls is the function in
    ``models/memory.py``, not a local reimplementation of it."""
    module = _import_module(relpath)
    assert getattr(module, serializer) is getattr(memory_model, serializer)


@pytest.mark.parametrize("relpath,func,serializer", RECALL_SURFACES)
def test_surface_builds_memories_by_calling_the_shared_serializer(
    relpath: str, func: str, serializer: str
) -> None:
    """AST half: the ``"memories"`` payload is a comprehension over a direct
    call to the shared serializer.

    Pinning the shape of the expression, not just the presence of a call,
    is what stops ``{**memory.model_dump(mode="json"), ...}`` -- the #1034
    shape, which names no keys of its own -- from coming back next to it.
    """
    node = _function(relpath, func)
    payloads = [
        value
        for dict_node in _dicts(node)
        for key, value in zip(dict_node.keys, dict_node.values)
        if isinstance(key, ast.Constant) and key.value == "memories"
    ]
    assert payloads, f"{relpath}:{node.lineno} {func} returns no 'memories' payload"
    built = [value for value in payloads if not _is_empty_list(value)]
    assert built, f"{relpath}:{node.lineno} {func} never builds a non-empty 'memories' payload"
    for value in built:
        assert isinstance(value, ast.ListComp), (
            f"{relpath}:{value.lineno} 'memories' must be a comprehension over "
            f"{serializer}(); serialize through models/memory.py, not here"
        )
        assert _called_name(value.elt) == serializer, (
            f"{relpath}:{value.lineno} 'memories' elements must come from "
            f"{serializer}(); a hand-built dict is the #1034 defect"
        )


@pytest.mark.parametrize("relpath,func,serializer", RECALL_SURFACES)
def test_surface_builds_no_memory_payload_dict_of_its_own(
    relpath: str, func: str, serializer: str
) -> None:
    """No dict literal in either handler may name a quorum of recall keys.

    The second net: it catches a partial hand-build (a few keys patched on top
    of the shared call) that the comprehension check above would accept.
    """
    node = _function(relpath, func)
    for dict_node in _dicts(node):
        literal = {
            key.value
            for key in dict_node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        overlap = literal & set(MCP_RECALL_KEYS)
        assert len(overlap) < _HAND_BUILT_KEY_OVERLAP, (
            f"{relpath}:{dict_node.lineno} hand-builds a recalled memory "
            f"({sorted(overlap)}); project it in models/memory.py instead"
        )


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _import_module(relpath: str):
    module_name = relpath.removesuffix(".py").replace("/", ".")
    return __import__(module_name, fromlist=["_"])


def _function(relpath: str, name: str) -> ast.AST:
    """The named top-level function of a module under ``src/``, parsed."""
    module = next(m for m in cl.parse_sources() if m.relpath == relpath)
    for node in ast.walk(module.tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{relpath} no longer defines {name}")


def _dicts(node: ast.AST):
    return [child for child in ast.walk(node) if isinstance(child, ast.Dict)]


def _is_empty_list(node: ast.AST) -> bool:
    return isinstance(node, ast.List) and not node.elts


def _called_name(node: ast.AST) -> Optional[str]:
    """The bare name of a call's callee (``a.b()`` -> ``b``), else None."""
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None
