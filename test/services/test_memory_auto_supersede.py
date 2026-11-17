"""Automatic supersede with undo (#1335 change 5).

Every test runs the real MemoryService, the real relationship store and the
real audit log against per-test files and a per-test SQLite database.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import MemoryRelationshipModel
from cli_agent_orchestrator.services import audit_log, memory_relationship_service
from cli_agent_orchestrator.services import memory_service as memory_service_module
from cli_agent_orchestrator.services import settings_service
from cli_agent_orchestrator.services.memory_service import MemoryService

STALE = "Retired revision one says the cobalt bridge opens at dawn."
CURRENT = "Current revision one says the cobalt bridge opens at noon. Replaces [[bridge-stale]]."


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def audit_base(tmp_path: Path, monkeypatch) -> Path:
    base = tmp_path / "audit-base"
    base.mkdir()
    monkeypatch.setattr(audit_log, "MEMORY_BASE_DIR", base)
    return base


@pytest.fixture
def service(tmp_path: Path, isolated_memory_db, audit_base, monkeypatch) -> MemoryService:
    # One database for every path, including forget's relationship purge.
    monkeypatch.setattr(settings_service, "is_memory_content_links_enabled", lambda: True)
    monkeypatch.setattr(
        memory_relationship_service, "SessionLocal", sessionmaker(bind=isolated_memory_db)
    )
    return MemoryService(base_dir=tmp_path / "memory", db_engine=isolated_memory_db)


def _edges(engine) -> list[tuple[str, str, str, str, str]]:
    with sessionmaker(bind=engine)() as db:
        return sorted(
            (row.source_key, row.target_key, row.type, row.origin, row.status)
            for row in db.query(MemoryRelationshipModel).all()
            if row.type == "supersedes"
        )


def _history() -> list[str]:
    return [
        line
        for line in audit_log.read_audit_log().splitlines()
        if "[memory_superseded]" in line or "[memory_supersede_undone]" in line
    ]


def _keys(rows) -> list[str]:
    return [row.key for row in rows]


def _store_pair(service: MemoryService, *, current: str = CURRENT):
    stale = _run(service.store(STALE, scope="global", key="bridge-stale"))
    new = _run(service.store(current, scope="global", key="bridge-current"))
    return stale, new


# --------------------------------------------------------------------------- #
# The similarity path: a linked, highly similar note replaces the old one
# --------------------------------------------------------------------------- #


def test_linked_similar_note_supersedes_old_note_at_once(service, isolated_memory_db) -> None:
    stale, new = _store_pair(service)

    assert stale.superseded == []
    assert [item.model_dump() for item in new.superseded] == [
        {
            "key": "bridge-stale",
            "scope": "global",
            "reason": "similar",
            "undo": "memory_restore(key='bridge-stale', scope='global')",
        }
    ]
    assert _edges(isolated_memory_db) == [
        ("bridge-current", "bridge-stale", "supersedes", "auto_supersede", "active")
    ]
    with sessionmaker(bind=isolated_memory_db)() as db:
        row = db.query(MemoryRelationshipModel).filter_by(type="supersedes").one()
        assert row.confidence is not None
        floor = memory_service_module.AUTO_SUPERSEDE_SIMILARITY_FLOOR
        assert floor <= row.confidence <= 1.0

    # Recall prefers the new note; the old one is hidden from default recall.
    assert _keys(_run(service.recall(query="cobalt bridge", scope="global"))) == ["bridge-current"]
    assert _keys(_run(service.recall(scope="global"))) == ["bridge-current"]

    # ...but it still exists and can be listed, labelled.
    listed = _run(service.recall(query="cobalt bridge", scope="global", include_superseded=True))
    assert sorted(_keys(listed)) == ["bridge-current", "bridge-stale"]
    labels = {row.key: row.lifecycle_status for row in listed}
    assert labels == {"bridge-current": None, "bridge-stale": "superseded"}
    assert Path(stale.file_path).read_text(encoding="utf-8").count(STALE) == 1

    history = _history()
    assert len(history) == 1
    assert "reason=similar" in history[0]
    assert "key=bridge-stale" in history[0]
    assert "superseded_by=bridge-current" in history[0]
    # History is content-free.
    assert "cobalt" not in history[0]


def test_linked_supersede_requires_p4_link_opt_in(service, isolated_memory_db, monkeypatch) -> None:
    monkeypatch.setattr(settings_service, "is_memory_content_links_enabled", lambda: False)
    stale, new = _store_pair(service)

    assert stale.superseded == []
    assert new.superseded == []
    assert _edges(isolated_memory_db) == []
    assert sorted(_keys(_run(service.recall(scope="global")))) == [
        "bridge-current",
        "bridge-stale",
    ]


def test_dissimilar_linked_note_does_not_supersede(service, isolated_memory_db) -> None:
    """The false-positive guard: a link alone is not a replacement."""
    _run(service.store(STALE, scope="global", key="bridge-stale"))
    hub = _run(
        service.store(
            "Harbour navigation index for the night shift; see [[bridge-stale]] too.",
            scope="global",
            key="harbour-index",
        )
    )

    assert hub.superseded == []
    assert _edges(isolated_memory_db) == []
    assert sorted(_keys(_run(service.recall(scope="global")))) == [
        "bridge-stale",
        "harbour-index",
    ]
    assert _history() == []


def test_link_markup_alone_does_not_make_a_note_similar(service, isolated_memory_db) -> None:
    """A link names its target; a short note that only cites it is not a restatement."""
    _run(service.store("deleted-from-text", scope="global", key="deleted-from-text"))
    citing = _run(service.store("[[deleted-from-text]]", scope="global", key="source"))

    assert citing.superseded == []
    assert _edges(isolated_memory_db) == []


def test_similar_but_unlinked_sibling_does_not_supersede(service, isolated_memory_db) -> None:
    """Templated siblings score higher than real revisions; without a link they stay."""
    _run(
        service.store(
            "The archive briefly compared beacon alpha01 during maintenance.",
            scope="global",
            key="archive-decoy-01",
        )
    )
    sibling = _run(
        service.store(
            "The archive briefly compared beacon alpha02 during maintenance.",
            scope="global",
            key="archive-decoy-02",
        )
    )

    assert sibling.superseded == []
    assert _edges(isolated_memory_db) == []
    assert len(_run(service.recall(scope="global"))) == 2


def test_cross_scope_similar_note_does_not_supersede(service, isolated_memory_db, tmp_path) -> None:
    _run(service.store(STALE, scope="global", key="bridge-stale"))
    project = _run(
        service.store(
            CURRENT,
            scope="project",
            key="bridge-current",
            terminal_context={"cwd": str(tmp_path / "project")},
        )
    )

    assert project.superseded == []
    assert _edges(isolated_memory_db) == []


def test_no_cycle_when_old_note_already_supersedes_new_one(service, isolated_memory_db) -> None:
    _run(service.store(STALE, scope="global", key="bridge-stale"))
    _run(service.store("Placeholder.", scope="global", key="bridge-current"))
    with sessionmaker(bind=isolated_memory_db)() as db:
        db.add(
            MemoryRelationshipModel(
                scope="global",
                scope_id="",
                source_key="bridge-stale",
                target_key="bridge-current",
                type="supersedes",
                status="active",
                origin="human",
            )
        )
        db.commit()

    updated = _run(service.store(CURRENT, scope="global", key="bridge-current"))

    assert [item.reason for item in updated.superseded] == ["same_key"]
    assert _edges(isolated_memory_db) == [
        ("bridge-stale", "bridge-current", "supersedes", "human", "active")
    ]


def test_restore_undoes_similarity_supersede_and_undo_sticks(service, isolated_memory_db) -> None:
    _store_pair(service)

    result = _run(service.restore("bridge-stale", scope="global"))

    assert result == {
        "success": True,
        "key": "bridge-stale",
        "scope": "global",
        "restored": "superseded_note",
        "superseded_by": ["bridge-current"],
    }
    assert sorted(_keys(_run(service.recall(query="cobalt bridge", scope="global")))) == [
        "bridge-current",
        "bridge-stale",
    ]
    # Nothing is deleted: the edge stays, as rejected.
    assert _edges(isolated_memory_db) == [
        ("bridge-current", "bridge-stale", "supersedes", "auto_supersede", "rejected")
    ]
    # Storing the same replacement again does not undo the undo.
    again = _run(service.store(CURRENT, scope="global", key="bridge-current"))
    assert again.superseded == []
    assert _edges(isolated_memory_db) == [
        ("bridge-current", "bridge-stale", "supersedes", "auto_supersede", "rejected")
    ]
    assert len(_run(service.recall(query="cobalt bridge", scope="global"))) == 2

    history = _history()
    assert [re.search(r"\[(memory_[a-z_]+)\]", line).group(1) for line in history] == [
        "memory_superseded",
        "memory_supersede_undone",
    ]
    assert "reason=similar" in history[1]
    assert "superseded_by=bridge-current" in history[1]


def test_forgetting_the_replacement_brings_the_old_note_back(service, isolated_memory_db) -> None:
    _store_pair(service)

    _run(service.forget("bridge-current", scope="global"))

    assert _keys(_run(service.recall(scope="global"))) == ["bridge-stale"]


# --------------------------------------------------------------------------- #
# The same-key path: a new entry under the same key replaces the previous one
# --------------------------------------------------------------------------- #


def test_same_key_update_supersedes_previous_version_and_restore_brings_it_back(
    service,
) -> None:
    first = _run(service.store("The relay is manual.", scope="global", key="relay-mode"))
    second = _run(service.store("The relay is automatic.", scope="global", key="relay-mode"))

    assert first.superseded == []
    assert [item.model_dump() for item in second.superseded] == [
        {
            "key": "relay-mode",
            "scope": "global",
            "reason": "same_key",
            "undo": "memory_restore(key='relay-mode', scope='global')",
        }
    ]
    assert [m.content for m in _run(service.recall(scope="global"))] == ["The relay is automatic."]

    result = _run(service.restore("relay-mode", scope="global"))

    assert result["success"] is True
    assert result["restored"] == "previous_version"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", result["restored_from"])
    assert [m.content for m in _run(service.recall(scope="global"))] == ["The relay is manual."]
    # Nothing is deleted: all three versions stay in the topic file.
    text = Path(second.file_path).read_text(encoding="utf-8")
    assert text.count("The relay is manual.") == 2
    assert text.count("The relay is automatic.") == 1

    history = _history()
    assert [re.search(r"\[(memory_[a-z_]+)\]", line).group(1) for line in history] == [
        "memory_superseded",
        "memory_supersede_undone",
    ]
    assert all("reason=same_key" in line for line in history)
    assert all("relay is" not in line for line in history)


def test_same_key_identical_content_is_not_a_supersede(service) -> None:
    _run(service.store("The relay is manual.", scope="global", key="relay-mode"))
    again = _run(service.store("The relay is manual.", scope="global", key="relay-mode"))

    assert again.superseded == []
    assert _history() == []


def test_restore_errors(service) -> None:
    assert _run(service.restore("missing-note", scope="global")) == {
        "success": False,
        "error_kind": "not_found",
        "error": "memory not found: missing-note",
    }
    _run(service.store("Only one version.", scope="global", key="single"))
    assert _run(service.restore("single", scope="global")) == {
        "success": False,
        "error_kind": "nothing_to_restore",
        "error": "memory has no superseded version to restore: single",
    }


def test_restore_respects_the_scope_write_guard(service, tmp_path) -> None:
    _run(service.store("The relay is manual.", scope="global", key="relay-mode"))
    _run(service.store("The relay is automatic.", scope="global", key="relay-mode"))

    with pytest.raises(PermissionError):
        _run(
            service.restore(
                "relay-mode",
                scope="global",
                terminal_context={"caller_scope": "session", "session_name": "s1"},
            )
        )


# --------------------------------------------------------------------------- #
# Injection and the setting
# --------------------------------------------------------------------------- #


def test_injection_excludes_auto_superseded_note(service, tmp_path) -> None:
    _store_pair(service)
    ctx = {"terminal_id": "t-1", "cwd": str(tmp_path / "project")}

    block = service.get_memory_context(ctx)

    assert "[global] bridge-current:" in block
    assert "[global] bridge-stale:" not in block


def test_setting_off_restores_todays_behaviour(service, isolated_memory_db, tmp_path) -> None:
    settings_service.set_memory_setting("supersede.auto", False)

    first = _run(service.store("The relay is manual.", scope="global", key="relay-mode"))
    second = _run(service.store("The relay is automatic.", scope="global", key="relay-mode"))
    stale, new = _store_pair(service)

    assert first.superseded == second.superseded == stale.superseded == new.superseded == []
    assert _edges(isolated_memory_db) == []
    assert sorted(_keys(_run(service.recall(scope="global")))) == [
        "bridge-current",
        "bridge-stale",
        "relay-mode",
    ]
    assert "[global] bridge-stale:" in service.get_memory_context(
        {"terminal_id": "t-1", "cwd": str(tmp_path / "project")}
    )
    assert _history() == []


def test_setting_off_unhides_existing_auto_supersedes(service) -> None:
    _store_pair(service)
    settings_service.set_memory_setting("supersede.auto", False)

    assert sorted(_keys(_run(service.recall(scope="global")))) == [
        "bridge-current",
        "bridge-stale",
    ]


def test_supersede_setting_defaults_on_and_round_trips() -> None:
    from cli_agent_orchestrator.services.config_service import ConfigService, MemoryConfig

    assert MemoryConfig().supersede.auto is True
    assert settings_service.is_memory_auto_supersede_enabled() is True
    assert ConfigService.get("memory.supersede.auto") is True
    ConfigService.set("memory.supersede.auto", False)
    assert ConfigService.get("memory.supersede.auto") is False
    assert settings_service.is_memory_auto_supersede_enabled() is False
    with pytest.raises(ValueError):
        settings_service.set_memory_setting("supersede.auto", "yes")
