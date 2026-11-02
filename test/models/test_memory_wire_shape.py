"""Model-level pin on the wire shape of ``Memory`` (#644, #968).

#968 pinned the default serialized recall payload per client surface in
``test/services/test_memory_861_default_parity_extended.py``. Those pins are
per-endpoint, and only the ``/internal/memory/recall`` one dumps the model
wholesale — the MCP ``memory_recall`` tool projects it down to the keys
declared in ``MCP_RECALL_KEYS``. So a field added to ``Memory`` widens the
wire on some surfaces and not others, and the endpoint pins report it as a
diff in a 22-key dict rather than as "you added a field to the model".

This module pins the invariant at the model instead, where the decision is
actually made:

* the serialized field list is enumerated once, so any new field is a
  deliberate, reviewable wire change on every surface at once; and
* the ``exclude=True`` convention the model already uses for in-process labels
  is self-enforcing, rather than depending on whoever adds the next field
  noticing that four of their neighbours have it.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest

from cli_agent_orchestrator.models.memory import Memory

# Every field ``Memory`` puts on the wire, in serialization order. This is the
# shape #968 pinned at ``/internal/memory/recall``. Adding a name here is a
# client-visible contract change: say so in the PR body and update the #968
# pins in the same commit. A field that is an in-process label rather than part
# of the contract belongs in NEVER_SERIALIZED below.
SERIALIZED_FIELDS = (
    "id",
    "key",
    "memory_type",
    "scope",
    "scope_id",
    "file_path",
    "tags",
    "source_provider",
    "source_terminal_id",
    "created_at",
    "updated_at",
    "access_count",
    "last_compiled_at",
    "related_keys",
    "content",
    "source_kind",
    "source_path",
    "indexed_at",
    "index_freshness",
    "content_truncated",
    "token_estimate",
)

# Declared fields that are producer-to-consumer labels within one process and
# must never reach a client. ``redacted_patterns`` (#644) is the vault reader's
# content-free receipt, read by the injection renderer to emit the
# ``injection_redaction.*`` counters; no recall surface consumes it.
NEVER_SERIALIZED = (
    "action",
    "is_related",
    "lifecycle_status",
    "redacted_patterns",
    "timestamp_clamped",
)

# The phrase each of those fields' descriptions carries, so the promise in the
# docstring and the ``exclude=True`` that enforces it cannot drift apart.
_NEVER_SERIALIZED_PROMISE = re.compile(r"never persisted or seriali[sz]ed", re.IGNORECASE)


def _memory(**overrides: object) -> Memory:
    at = datetime(2025, 12, 30, tzinfo=timezone.utc)
    fields: dict[str, object] = {
        "id": "00000000-0000-0000-0000-000000000001",
        "key": "pin",
        "memory_type": "project",
        "scope": "global",
        "file_path": "/vault/global/wiki/global/pin.md",
        "created_at": at,
        "updated_at": at,
        "content": "body",
    }
    fields.update(overrides)
    return Memory(**fields)  # type: ignore[arg-type]


def test_declared_fields_are_either_serialized_or_never_serialized() -> None:
    """No third category: every field is on the wire or declared off it."""
    assert set(Memory.model_fields) == set(SERIALIZED_FIELDS) | set(NEVER_SERIALIZED)
    assert not set(SERIALIZED_FIELDS) & set(NEVER_SERIALIZED)


def test_serialized_field_names_and_order_are_pinned() -> None:
    assert tuple(_memory().model_dump()) == SERIALIZED_FIELDS


def test_serialized_json_keys_match_the_dict_keys() -> None:
    """``model_dump_json`` is the path the internal API actually takes."""
    assert tuple(json.loads(_memory().model_dump_json())) == SERIALIZED_FIELDS


@pytest.mark.parametrize("name", NEVER_SERIALIZED)
def test_never_serialized_fields_are_excluded(name: str) -> None:
    assert Memory.model_fields[name].exclude is True


@pytest.mark.parametrize("name", NEVER_SERIALIZED)
def test_never_serialized_fields_document_the_promise(name: str) -> None:
    description = Memory.model_fields[name].description or ""
    assert _NEVER_SERIALIZED_PROMISE.search(description), (
        f"{name} is excluded from serialization; say so in its description so the "
        "next reader does not put it back on the wire"
    )


@pytest.mark.parametrize("name", SERIALIZED_FIELDS)
def test_serialized_fields_do_not_claim_to_be_excluded(name: str) -> None:
    """The converse, so the promise is a reliable signal in both directions."""
    description = Memory.model_fields[name].description or ""
    assert not _NEVER_SERIALIZED_PROMISE.search(description), (
        f"{name} says it is never serialised but is on the wire; either give it "
        "exclude=True and move it to NEVER_SERIALIZED, or drop the claim"
    )


def test_redacted_patterns_is_readable_in_process_but_never_on_the_wire() -> None:
    """#644: the receipt reaches the injection renderer, not the client."""
    memory = _memory(redacted_patterns=["github_pat", "bearer_token"])

    assert memory.redacted_patterns == ["github_pat", "bearer_token"]
    assert "redacted_patterns" not in memory.model_dump()
    assert "redacted_patterns" not in memory.model_dump_json()


def test_redacted_patterns_defaults_to_an_independent_empty_list() -> None:
    """A shared default would let one memory's receipt leak into another's."""
    first, second = _memory(), _memory()

    assert first.redacted_patterns == []
    first.redacted_patterns.append("github_pat")

    assert second.redacted_patterns == []
