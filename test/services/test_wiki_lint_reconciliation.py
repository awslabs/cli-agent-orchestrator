"""Regression test for metadata-independent wiki lint discovery."""

import asyncio
import uuid
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.services import audit_log, wiki_lint


def test_lint_finds_surviving_agent_topic_with_zero_metadata_rows(
    tmp_path: Path, monkeypatch
) -> None:
    base = tmp_path / "memory"
    topic = base / "global" / "wiki" / "agent" / "code_supervisor" / "survivor.md"
    topic.parent.mkdir(parents=True)
    topic.write_text(
        "# survivor\n"
        f"<!-- id: {uuid.uuid4()} | scope: agent | type: reference | tags: lint -->\n\n"
        "## 2026-07-16T01:00:00Z\n"
        "surviving content\n",
        encoding="utf-8",
    )
    before = topic.read_bytes()
    engine = create_engine(
        f"sqlite:///{tmp_path / 'metadata.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)

    monkeypatch.setattr(wiki_lint, "_build_llm_client", lambda: None)

    async def no_audit(*args, **kwargs) -> None:
        del args, kwargs

    monkeypatch.setattr(audit_log, "write_audit", no_audit)
    issues = asyncio.run(
        wiki_lint.run_lint(
            "project",
            scope="agent",
            base_dir=base,
            db_engine=engine,
            repo_root=str(tmp_path),
        )
    )

    assert any(issue.issue_type == "orphan_page" and issue.key == "survivor" for issue in issues)
    assert topic.read_bytes() == before
    with sessionmaker(bind=engine)() as db:
        assert db.query(MemoryMetadataModel).count() == 0
