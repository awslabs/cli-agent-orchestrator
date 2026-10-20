"""Lifecycle regressions for append-preserving managed vault stores."""

from __future__ import annotations

import asyncio
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from test.fixtures.vault_factory import build_vault_fixture

import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import memory_service, settings_service
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.vault import reconcile as reconcile_module
from cli_agent_orchestrator.services.vault import vault_lock
from cli_agent_orchestrator.services.vault import writer as writer_module
from cli_agent_orchestrator.services.vault.config import VaultConfig
from cli_agent_orchestrator.services.vault.parser import split_frontmatter
from cli_agent_orchestrator.utils import atomic_file

FIRST = datetime(2025, 1, 1, 1, 2, 3, tzinfo=timezone.utc)
SECOND = datetime(2025, 1, 2, 1, 2, 3, tzinfo=timezone.utc)
EARLIER = datetime(2024, 12, 31, 1, 2, 3, tzinfo=timezone.utc)
HEADING_RE = re.compile(r"(?m)^## (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)$")


def _vault_service(tmp_path, monkeypatch):
    lock_dir = tmp_path / "locks"
    monkeypatch.setattr(atomic_file, "LOCK_DIR", lock_dir)
    monkeypatch.setattr(vault_lock, "LOCK_DIR", lock_dir)
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(reconcile_module, "SessionLocal", Session)
    monkeypatch.setattr(reconcile_module, "_replace_vault_edges", lambda *_a, **_k: None)
    monkeypatch.setattr(reconcile_module, "_clear_stale_vault_edges", lambda *_a, **_k: None)
    monkeypatch.setattr(reconcile_module, "_emit_audit_events", lambda *_a, **_k: None)
    monkeypatch.setattr(memory_service, "_is_memory_enabled", lambda: True)
    fixture = build_vault_fixture(tmp_path)
    config = VaultConfig(enabled=True, vaults=[fixture.vault])
    monkeypatch.setattr(settings_service, "get_vault_config", lambda: config)
    return MemoryService(base_dir=tmp_path / "native", db_engine=engine), fixture, engine


def _store(service, body, *, key="append-history", memory_type="reference", occurred_at=None):
    return asyncio.run(
        service.store(
            content=body,
            scope="global",
            memory_type=memory_type,
            key=key,
            occurred_at=occurred_at,
        )
    )


def test_store_appends_preserving_human_body_and_cao_links(tmp_path, monkeypatch):
    service, fixture, engine = _vault_service(tmp_path, monkeypatch)
    first = _store(service, "first payload", occurred_at=FIRST)
    assert first.action == "created"
    assert first.created_at == FIRST
    target = fixture.root / "CAO" / "append-history.md"
    initial = target.read_text(encoding="utf-8")
    initial_body = split_frontmatter(initial, fixture.vault.max_frontmatter_bytes).body

    authored_frontmatter = (
        "---\n"
        'title: "Human title"\n'
        "unknown_field: keep-me\n"
        "cao:\n"
        "  key: append-history\n"
        "  type: reference\n"
        "  managed: true\n"
        "  links:\n"
        "  - to: design\n"
        "    type: relates_to\n"
        "    status: active\n"
        "    origin: human\n"
        "---\n"
    )
    target.write_text(
        authored_frontmatter + "Human prose between frontmatter and history.\n\n" + initial_body,
        encoding="utf-8",
    )
    reconcile_module.reconcile(fixture.vault, apply=True)

    second = _store(
        service,
        "second payload",
        memory_type="project",
        occurred_at=SECOND,
    )
    assert second.action == "updated"
    assert second.created_at == FIRST
    assert second.timestamp_clamped is False

    written = target.read_text(encoding="utf-8")
    region = split_frontmatter(written, fixture.vault.max_frontmatter_bytes)
    parsed = yaml.safe_load(region.raw)
    assert region.raw.index('title: "Human title"') < region.raw.index("unknown_field: keep-me")
    assert region.raw.index("unknown_field: keep-me") < region.raw.index("cao:")
    assert parsed["title"] == "Human title"
    assert parsed["unknown_field"] == "keep-me"
    assert parsed["cao"]["type"] == "project"
    assert parsed["cao"]["key"] == "append-history"
    assert parsed["cao"]["managed"] is True
    assert parsed["cao"]["links"] == [
        {
            "to": "design",
            "type": "relates_to",
            "status": "active",
            "origin": "human",
        }
    ]
    assert "Human prose between frontmatter and history." in region.body
    assert region.body.count("first payload") == 1
    assert region.body.count("second payload") == 1
    assert HEADING_RE.findall(region.body) == [
        "2025-01-01T01:02:03Z",
        "2025-01-02T01:02:03Z",
    ]

    clamped = _store(service, "earlier payload", occurred_at=EARLIER)
    final_body = split_frontmatter(
        target.read_text(encoding="utf-8"),
        fixture.vault.max_frontmatter_bytes,
    ).body
    headings = HEADING_RE.findall(final_body)
    parsed_headings = [
        datetime.strptime(item, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        for item in headings
    ]
    assert clamped.timestamp_clamped is True
    assert clamped.created_at == FIRST
    assert "_Originally recorded: 2024-12-31T01:02:03Z_\nearlier payload" in final_body
    assert parsed_headings == sorted(parsed_headings)
    assert final_body.count("first payload") == 1
    assert final_body.count("second payload") == 1
    assert final_body.count("earlier payload") == 1
    engine.dispose()


def test_same_key_concurrent_stores_append_both_sections(tmp_path, monkeypatch):
    service, fixture, engine = _vault_service(tmp_path, monkeypatch)
    _store(service, "seed payload", occurred_at=FIRST)

    first_lock_held = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()
    acquisition_guard = threading.Lock()
    acquisition_count = 0
    real_file_lock = writer_module._file_lock

    @contextmanager
    def controlled_file_lock(lock_path, timeout):
        nonlocal acquisition_count
        with real_file_lock(lock_path, timeout):
            with acquisition_guard:
                acquisition_count += 1
                ordinal = acquisition_count
            if ordinal == 1:
                first_lock_held.set()
                assert release_first.wait(10)
            yield

    monkeypatch.setattr(writer_module, "_file_lock", controlled_file_lock)
    errors = []
    results = []

    def store(body, occurred_at):
        try:
            results.append(_store(service, body, occurred_at=occurred_at))
        except BaseException as exc:
            errors.append(exc)
        finally:
            if body == "second concurrent payload":
                second_finished.set()

    first_thread = threading.Thread(
        target=store,
        args=("first concurrent payload", SECOND),
    )
    second_thread = threading.Thread(
        target=store,
        args=("second concurrent payload", SECOND + timedelta(seconds=1)),
    )
    first_thread.start()
    assert first_lock_held.wait(10)
    second_thread.start()
    time.sleep(0.1)
    assert not second_finished.is_set()
    release_first.set()
    first_thread.join(15)
    second_thread.join(15)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert len(results) == 2
    body = split_frontmatter(
        (fixture.root / "CAO" / "append-history.md").read_text(encoding="utf-8"),
        fixture.vault.max_frontmatter_bytes,
    ).body
    assert body.count("seed payload") == 1
    assert body.count("first concurrent payload") == 1
    assert body.count("second concurrent payload") == 1
    assert HEADING_RE.findall(body) == [
        "2025-01-01T01:02:03Z",
        "2025-01-02T01:02:03Z",
        "2025-01-02T01:02:04Z",
    ]
    engine.dispose()
