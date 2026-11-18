"""Visibility regressions for native memory topics without canonical headings."""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.services import (
    memory_reconciliation,
    memory_service,
    settings_service,
    wiki_compiler,
)
from cli_agent_orchestrator.services.memory_reconciliation import (
    MemoryReconciliationService,
    RepairAction,
)
from cli_agent_orchestrator.services.memory_service import MemoryService


@pytest.fixture
def engine(tmp_path: Path) -> Any:
    db_engine = create_engine(
        f"sqlite:///{tmp_path / 'metadata.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=db_engine)
    return db_engine


def _topic(
    base: Path,
    key: str,
    body: str,
    *,
    timestamp: str = "",
) -> Path:
    path = base / "global" / "wiki" / "global" / f"{key}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# {key}\n"
        f"<!-- id: {uuid.uuid4()} | scope: global | type: reference | tags: visibility -->\n"
        f"\n{timestamp}{body}\n",
        encoding="utf-8",
    )
    return path


def _entry(key: str, *, updated_at: str = "2026-11-14T05:54:00Z") -> dict[str, Any]:
    return {
        "key": key,
        "scope": "global",
        "scope_id": None,
        "memory_type": "reference",
        "tags": "visibility",
        "updated_at": updated_at,
        "relative_path": f"global/{key}.md",
    }


def test_compiled_article_without_entry_heading_parses_whole_body(
    tmp_path: Path, engine: Any
) -> None:
    path = _topic(
        tmp_path,
        "compiled-note",
        "## Overview\nPreviously invisible detail.\n\n## Decision\nKeep the whole article.",
    )
    memory = MemoryService(tmp_path, engine)._parse_wiki_file(
        path, path.read_text(encoding="utf-8"), _entry("compiled-note")
    )

    assert memory is not None
    assert memory.content == (
        "## Overview\nPreviously invisible detail.\n\n## Decision\nKeep the whole article."
    )


def test_minute_precision_heading_supplies_fact_date(tmp_path: Path, engine: Any) -> None:
    path = _topic(
        tmp_path,
        "minute-note",
        "Minute-precision content.",
        timestamp="## 2026-11-14T05:53Z\n",
    )
    memory = MemoryService(tmp_path, engine)._parse_wiki_file(
        path, path.read_text(encoding="utf-8"), _entry("minute-note")
    )

    assert memory is not None
    assert memory.created_at == datetime(2026, 11, 14, 5, 53, tzinfo=timezone.utc)
    assert memory.content == "Minute-precision content."


def test_header_only_topic_stays_invisible(tmp_path: Path, engine: Any) -> None:
    path = _topic(tmp_path, "header-only", "")

    memory = MemoryService(tmp_path, engine)._parse_wiki_file(
        path, path.read_text(encoding="utf-8"), _entry("header-only")
    )

    assert memory is None


def test_timestamped_see_also_only_topic_stays_visible(tmp_path: Path, engine: Any) -> None:
    path = _topic(
        tmp_path,
        "link-only",
        "## See Also\n- [target-topic](../global/target-topic.md)",
        timestamp="## 2026-11-14T05:53:00Z\n",
    )

    memory = MemoryService(tmp_path, engine)._parse_wiki_file(
        path, path.read_text(encoding="utf-8"), _entry("link-only")
    )

    assert memory is not None
    assert memory.content == ""


def test_lenient_false_restores_legacy_invisibility(
    tmp_path: Path, engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _topic(tmp_path, "legacy-note", "No canonical heading.")
    monkeypatch.setattr(
        settings_service,
        "get_memory_settings",
        lambda: {"parser": {"lenient": False}},
    )

    memory = MemoryService(tmp_path, engine)._parse_wiki_file(
        path, path.read_text(encoding="utf-8"), _entry("legacy-note")
    )

    assert memory is None


@pytest.mark.parametrize("lenient", [True, False])
@pytest.mark.parametrize(
    ("timestamp", "expected_created", "expected_updated", "expected_content"),
    [
        (
            "### 2026-01-01T00:00:00Z\n",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            "body one",
        ),
        (
            "## 2026-01-01T00:00:00Z \n",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            "body one",
        ),
        (
            "## 2026-01-01T00:00:00Z - Summary\n",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            "- Summary\nbody one",
        ),
        (
            "## 2026-01-01T00:00:00Z\nold body\n\n## 2026-02-01T00:00:00Z (edited)\n",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 2, 1, tzinfo=timezone.utc),
            "(edited)\nbody one",
        ),
    ],
)
def test_legacy_heading_shapes_preserve_dates_and_latest_content(
    tmp_path: Path,
    engine: Any,
    monkeypatch: pytest.MonkeyPatch,
    lenient: bool,
    timestamp: str,
    expected_created: datetime,
    expected_updated: datetime,
    expected_content: str,
) -> None:
    monkeypatch.setattr(
        settings_service,
        "get_memory_settings",
        lambda: {"parser": {"lenient": lenient}},
    )
    path = _topic(tmp_path, "legacy-shape", "body one", timestamp=timestamp)

    memory = MemoryService(tmp_path, engine)._parse_wiki_file(
        path, path.read_text(encoding="utf-8"), _entry("legacy-shape")
    )

    assert memory is not None
    assert memory.created_at == expected_created
    assert memory.updated_at == expected_updated
    assert memory.content == expected_content


def test_canonical_topic_ignores_timestamped_subheading(tmp_path: Path, engine: Any) -> None:
    path = _topic(
        tmp_path,
        "canonical-subheading",
        "Incident summary\n### Timeline 2026-01-15T10:00:00Z\ndetails",
        timestamp="## 2026-02-01T00:00:00Z\n",
    )

    memory = MemoryService(tmp_path, engine)._parse_wiki_file(
        path, path.read_text(encoding="utf-8"), _entry("canonical-subheading")
    )

    expected = datetime(2026, 2, 1, tzinfo=timezone.utc)
    assert memory is not None
    assert memory.created_at == expected
    assert memory.updated_at == expected
    assert memory.content == ("Incident summary\n### Timeline 2026-01-15T10:00:00Z\ndetails")


@pytest.mark.parametrize(
    "body_heading",
    [
        "## 2027-06-01T00:00Z",
        "### Notes 2027-06-01T00:00:00Z",
    ],
)
def test_stored_body_heading_does_not_change_topic(
    tmp_path: Path,
    engine: Any,
    body_heading: str,
) -> None:
    body = f"Body line.\n{body_heading}\ntail text"
    service = MemoryService(tmp_path, engine)

    asyncio.run(
        service.store(
            content=body,
            scope="global",
            key="body-heading",
            occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    memories = asyncio.run(service.recall(scope="global", scan_all=True))

    assert len(memories) == 1
    assert memories[0].updated_at == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert memories[0].content == body


@pytest.mark.parametrize("lenient", [True, False])
def test_compiled_topic_uses_cao_owned_dates(
    tmp_path: Path,
    engine: Any,
    monkeypatch: pytest.MonkeyPatch,
    lenient: bool,
) -> None:
    monkeypatch.setattr(
        settings_service,
        "get_memory_settings",
        lambda: {"parser": {"lenient": lenient}},
    )
    path = _topic(
        tmp_path,
        "compiled-dates",
        ("## 2026-01-01T00:00:00Z\nfirst fact\n\n" "## 2026-02-01T00:00:00Z\nsecond fact"),
    )
    compiled = wiki_compiler._postprocess_compiled(
        path.read_text(encoding="utf-8"),
        newest_entry_at="2026-03-01T00:00:00Z",
        entry_count=3,
        compiled_at="2026-03-02T00:00:00Z",
    )
    path.write_text(compiled, encoding="utf-8")
    entry = _entry("compiled-dates", updated_at="2026-03-01T00:00:00Z")
    entry["created_at"] = "2026-01-01T00:00:00Z"

    memory = MemoryService(tmp_path, engine)._parse_wiki_file(path, compiled, entry)

    assert memory is not None
    assert memory.created_at == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert memory.updated_at == datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert memory.created_at <= memory.updated_at


def test_parser_checks_only_timestamp_boundaries_and_first_body_marker(
    tmp_path: Path,
    engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TrackingDateTime(datetime):
        parse_calls = 0

        @classmethod
        def strptime(cls, value: str, date_format: str) -> datetime:
            cls.parse_calls += 1
            return super().strptime(value, date_format)

    class TrackingMarker:
        def __init__(self, pattern: re.Pattern[str]) -> None:
            self.calls = 0
            self.pattern = pattern

        def fullmatch(self, value: str) -> re.Match[str] | None:
            self.calls += 1
            return self.pattern.fullmatch(value)

    marker = TrackingMarker(memory_service._COMPILED_MARKER_RE)
    monkeypatch.setattr(memory_service, "datetime", TrackingDateTime)
    monkeypatch.setattr(memory_service, "_COMPILED_MARKER_RE", marker)
    sections = "\n\n".join(f"## 2026-01-{day:02d}T00:00:00Z\nbody {day}" for day in range(1, 21))
    path = _topic(tmp_path, "parse-cost", sections)

    parsed = MemoryService(tmp_path, engine)._parse_wiki_file(
        path, path.read_text(encoding="utf-8"), _entry("parse-cost")
    )

    assert parsed is not None
    assert TrackingDateTime.parse_calls == 2
    assert marker.calls == 1


def test_recall_returns_previously_invisible_topic(tmp_path: Path, engine: Any) -> None:
    path = _topic(tmp_path, "visible-now", "The rarevisibilitytoken is searchable.")
    index = tmp_path / "global" / "wiki" / "index.md"
    index.write_text(
        "# CAO Memory Index\n"
        "<!-- Updated: 2026-11-14T05:54:00Z -->\n\n"
        "## global\n"
        "- [visible-now](global/visible-now.md) — type:reference "
        "tags:visibility ~8tok updated:2026-11-14T05:54:00Z\n",
        encoding="utf-8",
    )

    results = asyncio.run(
        MemoryService(tmp_path, engine).recall(
            query="rarevisibilitytoken",
            scope="global",
            search_mode="metadata",
        )
    )

    assert [memory.key for memory in results] == ["visible-now"]
    assert results[0].file_path == str(path)


def test_repair_indexes_and_reports_fallback_dates(tmp_path: Path, engine: Any) -> None:
    path = _topic(tmp_path, "repair-visible", "Repair should index this body.")
    expected_mtime = datetime(2026, 11, 14, 5, 55, tzinfo=timezone.utc)
    os.utime(path, (expected_mtime.timestamp(), expected_mtime.timestamp()))

    report = MemoryReconciliationService(tmp_path, engine).reconcile(apply=True)

    assert report.counts["repaired"] == 1
    assert report.counts["index_with_fallback_dates"] == 1
    assert RepairAction.INDEX_WITH_FALLBACK_DATES in report.records[0].actions
    assert report.records[0].finding is not None
    assert report.records[0].finding.kind == "fallback_dates"
    with sessionmaker(bind=engine)() as db:
        row = db.query(MemoryMetadataModel).one()
        assert row.key == "repair-visible"
        assert row.created_at == expected_mtime.replace(tzinfo=None)
        assert row.updated_at == expected_mtime.replace(tzinfo=None)


def test_repair_honours_disabled_lenient_parser(
    tmp_path: Path,
    engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _topic(tmp_path, "strict-repair", "A headingless topic.")
    monkeypatch.setattr(
        settings_service,
        "get_memory_settings",
        lambda: {"parser": {"lenient": False}},
    )

    report = MemoryReconciliationService(tmp_path, engine).reconcile(apply=True)

    assert report.counts["malformed"] == 1
    assert report.records[0].actions == (RepairAction.MALFORMED,)
    assert report.records[0].finding is not None
    assert report.records[0].finding.kind == "malformed_timestamp"
    with sessionmaker(bind=engine)() as db:
        assert db.query(MemoryMetadataModel).count() == 0


def test_compact_headingless_topic_uses_metadata_updated_at(
    tmp_path: Path,
    engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _topic(tmp_path, "compact-headingless", "A headingless compact candidate.")
    article = path.read_text(encoding="utf-8")
    updated_at = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    with sessionmaker(bind=engine)() as db:
        db.add(
            MemoryMetadataModel(
                key="compact-headingless",
                memory_type="reference",
                scope="global",
                scope_id=None,
                file_path=str(path),
                tags="visibility",
                created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                updated_at=updated_at,
            )
        )
        db.commit()

    async def return_article(*args: Any, **kwargs: Any) -> str:
        return article

    monkeypatch.setattr(wiki_compiler, "_build_llm_client", lambda *args, **kwargs: object())
    monkeypatch.setattr(wiki_compiler, "_llm_call", return_article)

    result = asyncio.run(MemoryService(tmp_path, engine).compact(key="compact-headingless"))

    assert result["compact-headingless"] == "applied"
    assert "## 2025-01-02T03:04:05Z" in path.read_text(encoding="utf-8")


def test_add_headings_backs_up_before_write_and_is_idempotent(
    tmp_path: Path,
    engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _topic(tmp_path, "add-heading", "Repairable article body.")
    before = path.read_bytes()
    backup = path.with_suffix(".md.bak")
    service = MemoryReconciliationService(tmp_path, engine)
    real_replace = os.replace

    def assert_backup_precedes_topic_replace(source: str, destination: str) -> None:
        if Path(destination) == path:
            assert backup.read_bytes() == before
        real_replace(source, destination)

    monkeypatch.setattr(
        memory_reconciliation.os,
        "replace",
        assert_backup_precedes_topic_replace,
    )

    first = service.reconcile(apply=True, add_headings=True)

    assert first.counts["repaired"] == 1
    assert backup.read_bytes() == before
    after_first = path.read_bytes()
    assert after_first != before
    assert (
        len(re.findall(rb"^## \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", after_first, re.MULTILINE))
        == 1
    )

    second = service.reconcile(apply=True, add_headings=True)

    assert second.counts["unchanged"] == 1
    assert path.read_bytes() == after_first
    assert backup.read_bytes() == before


def test_parser_lenient_setting_defaults_true_and_round_trips() -> None:
    from cli_agent_orchestrator.services.config_service import ConfigService

    assert settings_service.is_memory_parser_lenient() is True
    assert ConfigService.get("memory.parser.lenient") is True

    ConfigService.set("memory.parser.lenient", False)

    assert settings_service.is_memory_parser_lenient() is False
    assert settings_service.get_memory_settings()["parser"]["lenient"] is False
    assert ConfigService.get("memory.parser.lenient") is False
    assert ConfigService.get_config().memory.parser.lenient is False
