"""Tests for ``MemoryService.store(occurred_at=...)`` (#345 D5 clamp rule).

- ``occurred_at=None`` → byte-identical to today's behavior.
- In-order, non-future value → used verbatim for the ``## <ts>`` heading
  (and ``created_at`` when the topic is new).
- Future value (new topics AND merges) or older than the existing topic's
  latest section → heading clamps to now(), original ts preserved in the
  body as ``_Originally recorded: <ISO-ts>_``, ``timestamp_clamped=True``
  on the returned Memory.
"""

import asyncio
import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine

from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services.memory_service import MemoryService


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def svc(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return MemoryService(base_dir=tmp_path / "memory", db_engine=engine)


def _headings(svc, key):
    content = svc.get_wiki_path("global", None, key).read_text(encoding="utf-8")
    return re.findall(r"## (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", content), content


PAST = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
PAST_ISO = "2025-01-15T12:00:00Z"


class TestOccurredAtNone:
    def test_default_behavior_unchanged(self, svc):
        mem = _run(svc.store(content="note", scope="global", key="topic-a"))
        headings, content = _headings(svc, "topic-a")
        assert len(headings) == 1
        assert mem.timestamp_clamped is False
        assert "_Originally recorded:" not in content
        # Heading is now(), not some historical value.
        heading_ts = datetime.strptime(headings[0], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        assert abs((heading_ts - datetime.now(timezone.utc)).total_seconds()) < 60

    def test_merge_default_behavior_unchanged(self, svc):
        # occurred_at=None on the MERGE path: append to an existing topic
        # must keep the exact pre-#345 byte shape.
        _run(svc.store(content="first", scope="global", key="topic-a2"))
        before = svc.get_wiki_path("global", None, "topic-a2").read_text(encoding="utf-8")
        mem = _run(svc.store(content="second", scope="global", key="topic-a2"))
        headings, content = _headings(svc, "topic-a2")
        assert len(headings) == 2
        assert mem.timestamp_clamped is False
        assert "_Originally recorded:" not in content
        # Heading is now-based, not historical.
        heading_ts = datetime.strptime(headings[-1], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        assert abs((heading_ts - datetime.now(timezone.utc)).total_seconds()) < 60
        # Byte-shape identical to the pre-change append: existing content
        # (sans trailing newlines) + blank line + heading + raw body.
        assert content == before.rstrip("\n") + f"\n\n## {headings[-1]}\nsecond\n"


class TestOccurredAtInOrder:
    def test_new_topic_uses_verbatim_heading_and_created_at(self, svc):
        mem = _run(svc.store(content="note", scope="global", key="topic-b", occurred_at=PAST))
        headings, content = _headings(svc, "topic-b")
        assert headings == [PAST_ISO]
        assert mem.created_at == PAST
        assert mem.timestamp_clamped is False
        assert "_Originally recorded:" not in content

    def test_merge_in_order_uses_verbatim_heading(self, svc):
        _run(svc.store(content="first", scope="global", key="topic-c", occurred_at=PAST))
        later = PAST + timedelta(days=1)
        mem = _run(svc.store(content="second", scope="global", key="topic-c", occurred_at=later))
        headings, content = _headings(svc, "topic-c")
        assert headings == [PAST_ISO, "2025-01-16T12:00:00Z"]
        assert mem.timestamp_clamped is False
        # Original created_at (first section) preserved on merge.
        assert mem.created_at == PAST

    def test_naive_datetime_treated_as_utc(self, svc):
        naive = PAST.replace(tzinfo=None)
        mem = _run(svc.store(content="note", scope="global", key="topic-d", occurred_at=naive))
        headings, _ = _headings(svc, "topic-d")
        assert headings == [PAST_ISO]
        assert mem.timestamp_clamped is False

    def test_non_utc_tz_aware_converted_to_utc(self, svc):
        # Same instant as PAST expressed at UTC+5: 17:00+05:00 == 12:00Z.
        # The heading must reflect the UTC instant, and since the instant
        # is genuinely in the past no clamp occurs.
        plus_five = PAST.astimezone(timezone(timedelta(hours=5)))
        assert plus_five.hour == 17
        mem = _run(svc.store(content="note", scope="global", key="topic-tz", occurred_at=plus_five))
        headings, content = _headings(svc, "topic-tz")
        assert headings == [PAST_ISO]
        assert mem.created_at == PAST
        assert mem.timestamp_clamped is False
        assert "_Originally recorded:" not in content


class TestOccurredAtClamped:
    def test_future_clamps_on_new_topic(self, svc):
        future = datetime.now(timezone.utc) + timedelta(days=365)
        mem = _run(svc.store(content="note", scope="global", key="topic-e", occurred_at=future))
        headings, content = _headings(svc, "topic-e")
        future_iso = future.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert headings != [future_iso]
        assert mem.timestamp_clamped is True
        assert f"_Originally recorded: {future_iso}_" in content
        # created_at clamps too — a new topic cannot be created in the future.
        assert mem.created_at <= datetime.now(timezone.utc)

    def test_future_clamps_on_merge(self, svc):
        _run(svc.store(content="first", scope="global", key="topic-f"))
        future = datetime.now(timezone.utc) + timedelta(days=365)
        mem = _run(svc.store(content="second", scope="global", key="topic-f", occurred_at=future))
        assert mem.timestamp_clamped is True

    def test_older_than_latest_section_clamps_on_merge(self, svc):
        # Existing topic's latest section is now(); import an older entry.
        _run(svc.store(content="current", scope="global", key="topic-g"))
        mem = _run(svc.store(content="old note", scope="global", key="topic-g", occurred_at=PAST))
        headings, content = _headings(svc, "topic-g")
        assert mem.timestamp_clamped is True
        assert f"_Originally recorded: {PAST_ISO}_" in content
        # Append-only contract: section timestamps remain in order, so the
        # LAST section (what readers treat as latest) is the new entry.
        assert headings[-1] != PAST_ISO
        parsed = [
            datetime.strptime(h, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            for h in headings
        ]
        assert parsed == sorted(parsed)
        assert "old note" in content.rsplit(f"## {headings[-1]}", 1)[-1]

    def test_clamped_entry_body_carries_original_ts_first_line(self, svc):
        _run(svc.store(content="current", scope="global", key="topic-h"))
        _run(svc.store(content="old note", scope="global", key="topic-h", occurred_at=PAST))
        _, content = _headings(svc, "topic-h")
        last_section = content.rsplit("## ", 1)[-1]
        body_lines = last_section.splitlines()
        assert body_lines[1] == f"_Originally recorded: {PAST_ISO}_"
        assert body_lines[2] == "old note"


class TestClampHelperEquivalence:
    """store()'s clamp outcome must equal _occurred_at_would_clamp's
    prediction — the same helper backs the OKF import dry-run report."""

    def _predict(self, svc, key, occurred_at):
        wiki_path = svc.get_wiki_path("global", None, key)
        latest = None
        if wiki_path.exists():
            ts = re.findall(
                r"## (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)",
                wiki_path.read_text(encoding="utf-8"),
            )
            if ts:
                latest = datetime.strptime(ts[-1], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
        return MemoryService._occurred_at_would_clamp(
            occurred_at, latest, datetime.now(timezone.utc)
        )

    def test_store_matches_helper_on_boundary_cases(self, svc):
        key = "topic-eq"
        _run(svc.store(content="seed", scope="global", key=key, occurred_at=PAST))
        cases = [
            PAST,  # equal to latest section ts — in order, no clamp
            PAST - timedelta(seconds=1),  # just-older than latest — clamps
            datetime.now(timezone.utc) + timedelta(days=1),  # future — clamps
        ]
        for occurred_at in cases:
            predicted = self._predict(svc, key, occurred_at)
            mem = _run(svc.store(content="entry", scope="global", key=key, occurred_at=occurred_at))
            assert mem.timestamp_clamped is predicted, occurred_at
