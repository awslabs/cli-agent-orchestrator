"""End-to-end regression: the repair path the #657 migrator advertises.

The migration added by issue #657 warns ``Run `cao memory repair`` when a
legacy database holds duplicate NULL-scope rows, and the PR review called out
that this pointer was dead: reconciliation reported the duplicates as an
unresolvable conflict and the federated container was never scanned, so the
advertised repair could never clear the state the index needs.

These tests drive the REAL CLI over a REAL isolated ``CAO_HOME_DIR`` in a
subprocess — no in-process service patching — mirroring the subprocess
isolation pattern of ``test/fixtures/cao_server.py``: seed the legacy state
through raw SQLite exactly as an external edit would leave it, run
``python -m cli_agent_orchestrator.cli.main memory repair --apply``, then
prove the reviewer's contract: canonical content survives, the index exists
afterwards and rejects a new duplicate, and ambiguous rows fail actionably
without data loss.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

_TOPIC_BODY = (
    "# shared\n"
    "<!-- id: 11111111-1111-1111-1111-111111111111 | scope: global | "
    "type: reference | tags:  -->\n\n"
    "## 2026-07-15T01:00:00Z\ndurable body\n"
)


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "cao-home"
    (home / "db").mkdir(parents=True)
    (home / "memory" / "global" / "wiki" / "global").mkdir(parents=True)
    return home


def _db_path(home: Path) -> Path:
    from cli_agent_orchestrator.constants import CAO_HOME_DIR, DATABASE_FILE

    if str(CAO_HOME_DIR) == str(home):
        return Path(DATABASE_FILE)
    return home / "db" / DATABASE_FILE.name


def _seed_legacy_schema(db_file: Path) -> None:
    """Create the schema WITHOUT the #657 partial index, as old databases have."""
    from sqlalchemy import create_engine

    from cli_agent_orchestrator.clients.database import Base

    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(bind=engine)
    engine.dispose()
    with sqlite3.connect(str(db_file)) as conn:
        conn.execute("DROP INDEX IF EXISTS uq_memory_key_scope_null")
        conn.commit()


def _seed_duplicate_rows(
    db_file: Path, key: str, paths: list[str], *, scope: str = "global"
) -> None:
    with sqlite3.connect(str(db_file)) as conn:
        for path in paths:
            conn.execute(
                "INSERT INTO memory_metadata (id, key, memory_type, scope, scope_id, "
                "file_path, tags, access_count) "
                "VALUES (?, ?, 'reference', ?, NULL, ?, '', 0)",
                (str(uuid.uuid4()), key, scope, path),
            )
        conn.commit()


def _run_repair(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        "CAO_HOME_DIR": str(home),
        "PATH": "",
    }
    return subprocess.run(
        [sys.executable, "-m", "cli_agent_orchestrator.cli.main", "memory", "repair", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def _index_names(db_file: Path) -> set[str]:
    with sqlite3.connect(str(db_file)) as conn:
        return {row[1] for row in conn.execute("PRAGMA index_list('memory_metadata')")}


def test_repair_dedupes_and_lands_index_end_to_end(tmp_path: Path) -> None:
    """Seed legacy duplicates, run the real ``cao memory repair --apply``.

    Proves the reviewer's unambiguous contract: the canonical-path row
    survives with its identity intact, the partial unique index exists after
    the same repair run, and a fresh duplicate INSERT is rejected.
    """
    home = _home(tmp_path)
    db_file = _db_path(home)
    _seed_legacy_schema(db_file)
    topic = home / "memory" / "global" / "wiki" / "global" / "shared.md"
    topic.write_text(_TOPIC_BODY, encoding="utf-8")
    _seed_duplicate_rows(db_file, "shared", [str(topic), str(topic.with_name("stale.md"))])

    with sqlite3.connect(str(db_file)) as conn:
        survivor_id = conn.execute(
            "SELECT id FROM memory_metadata WHERE file_path = ?", (str(topic),)
        ).fetchone()[0]

    result = _run_repair(home, "--apply")

    assert result.returncode == 0, result.stdout + result.stderr
    with sqlite3.connect(str(db_file)) as conn:
        rows = conn.execute("SELECT id, file_path FROM memory_metadata").fetchall()
    assert rows == [(survivor_id, str(topic))], "canonical row survives with its id"
    assert "uq_memory_key_scope_null" in _index_names(db_file)
    with sqlite3.connect(str(db_file)) as conn:
        try:
            conn.execute(
                "INSERT INTO memory_metadata (id, key, memory_type, scope, scope_id, "
                "file_path, tags, access_count) "
                "VALUES ('dup-after-repair', 'shared', 'reference', 'global', NULL, '/x.md', '', 0)"
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("a new duplicate NULL-scope insert must be rejected")


def test_repair_reports_ambiguous_duplicates_without_data_loss(tmp_path: Path) -> None:
    """Neither row anchors the canonical path: actionable failure, zero loss."""
    home = _home(tmp_path)
    db_file = _db_path(home)
    _seed_legacy_schema(db_file)
    topic = home / "memory" / "global" / "wiki" / "global" / "shared.md"
    topic.write_text(_TOPIC_BODY, encoding="utf-8")
    _seed_duplicate_rows(
        db_file, "shared", [str(topic.with_name("a.md")), str(topic.with_name("b.md"))]
    )

    result = _run_repair(home, "--apply")

    assert result.returncode == 1, "ambiguous duplicates must fail the apply exit code"
    assert "skipped" in result.stdout and "conflict" in result.stdout
    assert "none is uniquely anchored to the canonical topic path" in result.stdout
    assert "manually" in result.stdout, "manual-resolution guidance must be actionable"
    with sqlite3.connect(str(db_file)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM memory_metadata").fetchone()[0]
    assert count == 2, "ambiguous duplicates must be retained without data loss"
    assert "uq_memory_key_scope_null" not in _index_names(db_file)


def test_repair_reports_no_topic_duplicates_via_the_real_cli(tmp_path: Path) -> None:
    """Duplicates with no surviving canonical topic fail actionably (P2-1).

    Review finding: reconciliation only emitted records for discovered
    filesystem topics, so NULL-scope duplicate identities absent from the
    scan made the real CLI exit 0 with ``total=0`` while both rows stayed
    and the migrator kept skipping the index. The repair must report every
    such identity as a conflict, exit 1, and delete nothing.
    """
    home = _home(tmp_path)
    db_file = _db_path(home)
    _seed_legacy_schema(db_file)
    # No topic files exist at all: both duplicate rows point at deleted paths.
    _seed_duplicate_rows(db_file, "shared", ["/gone/a.md", "/gone/b.md"])
    # A federated duplicate pair, equally unmapped, must not be invisible.
    _seed_duplicate_rows(db_file, "fed", ["/fed/gone/a.md", "/fed/gone/b.md"], scope="federated")

    dry = _run_repair(home)
    assert dry.returncode == 1, "dry-run must already exit nonzero on the conflict"
    assert "total=2" in dry.stdout, f"dry-run must see both identities: {dry.stdout}"
    assert "federated:-:fed" in dry.stdout, "the federated duplicate is reported too"
    assert "no canonical topic maps to it" in dry.stdout

    result = _run_repair(home, "--apply")
    assert result.returncode == 1, "apply must fail actionably, not report success"
    assert "total=2" in result.stdout
    assert "no canonical topic maps to it" in result.stdout
    with sqlite3.connect(str(db_file)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM memory_metadata").fetchone()[0]
    assert count == 4, "no row may be deleted without a topic anchor"
    assert "uq_memory_key_scope_null" not in _index_names(db_file)


def test_repair_dedupes_mixed_timestamp_rows_without_crashing(tmp_path: Path) -> None:
    """Mixed naive/NULL ``updated_at`` dedupes instead of TypeError (P2-2).

    The apply-time survivor pick compared raw legacy timestamps with a
    timezone-aware floor, so a NULL/naive mix crashed the record mid-repair.
    The real CLI must complete the dedupe, keep the newest row, and land the
    index in the same run.
    """
    home = _home(tmp_path)
    db_file = _db_path(home)
    _seed_legacy_schema(db_file)
    topic = home / "memory" / "global" / "wiki" / "global" / "shared.md"
    topic.write_text(_TOPIC_BODY, encoding="utf-8")
    _seed_duplicate_rows(db_file, "shared", [str(topic), str(topic.with_name("stale.md"))])
    with sqlite3.connect(str(db_file)) as conn:
        conn.execute(
            "UPDATE memory_metadata SET updated_at = '2026-07-01 10:00:00' " "WHERE file_path = ?",
            (str(topic),),
        )
        conn.execute(
            "UPDATE memory_metadata SET updated_at = NULL " "WHERE file_path = ?",
            (str(topic.with_name("stale.md")),),
        )
        survivor_id = conn.execute(
            "SELECT id FROM memory_metadata WHERE file_path = ?", (str(topic),)
        ).fetchone()[0]
        conn.commit()

    result = _run_repair(home, "--apply")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "TypeError" not in result.stderr
    with sqlite3.connect(str(db_file)) as conn:
        rows = conn.execute("SELECT id, file_path FROM memory_metadata").fetchall()
    assert rows == [(survivor_id, str(topic))], "newest (only dated) row survives"
    assert "uq_memory_key_scope_null" in _index_names(db_file)


def test_repair_surfaces_lock_failed_index_creation_and_recovers(tmp_path: Path) -> None:
    """A competing write lock must not yield silent success (P2-3).

    The migrator swallowed every exception at debug, so a concurrent SQLite
    writer could make ``cao memory repair --apply`` exit 0 while the unique
    index stayed absent. Under lock the CLI must fail naming the index; once
    the lock is released a rerun must succeed and the index must exist.
    """
    import subprocess
    import textwrap

    home = _home(tmp_path)
    db_file = _db_path(home)
    _seed_legacy_schema(db_file)
    topic = home / "memory" / "global" / "wiki" / "global" / "shared.md"
    topic.write_text(_TOPIC_BODY, encoding="utf-8")
    _seed_duplicate_rows(db_file, "shared", [str(topic), str(topic.with_name("stale.md"))])

    # An external process holds a write lock for the whole repair window.
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
                import sqlite3, sys, time
                conn = sqlite3.connect(sys.argv[1], timeout=30)
                conn.execute("BEGIN EXCLUSIVE")
                print("locked", flush=True)
                time.sleep(30)
                conn.rollback()
                """),
            str(db_file),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        result = _run_repair(home, "--apply")
        assert result.returncode != 0, "repair under lock must not report success"
    finally:
        holder.kill()
        holder.wait(timeout=10)

    # BEGIN EXCLUSIVE blocks readers too, so probe only after the release.
    assert "uq_memory_key_scope_null" not in _index_names(db_file)

    retry = _run_repair(home, "--apply")
    assert retry.returncode == 0, retry.stdout + retry.stderr
    assert "uq_memory_key_scope_null" in _index_names(db_file)
    with sqlite3.connect(str(db_file)) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM memory_metadata").fetchone()[0]
    assert rows == 1, "the retry completes the dedupe"
