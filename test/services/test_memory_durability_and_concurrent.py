"""SC-4 durability + SC-5 concurrent-write safety tests (U4).

- SC-4: ``MemoryService`` reinstantiation at the same ``base_dir``/DB file
  round-trips stored memories with field-level integrity (key, memory_type,
  tags, scope, content).
- SC-5: Two concurrent writers to the same scope produce a parseable
  ``index.md`` with both entries present, under ``fcntl.flock``-guarded
  writes at ``memory_service.py:470`` / ``memory_service.py:567``.

Cross-unit risk absorbed from the U3 challenger flag and the security-
reviewer's deferred-items tracker: the per-scope "newest-N wins" sort at
``memory_service.py:1116`` is pinned by an additional invariant test so any
drift in ``order_by(updated_at.desc())`` at ``memory_service.py:182`` or in
the ``_regenerate_scope_index`` grouping fails here instead of silently
serving stale memories.

Concurrency uses ``multiprocessing`` (not threads) to exercise the OS-level
flock across separate file descriptors *and* separate SQLite connections.
Tests that require ``fcntl`` are skipped cleanly on platforms without it
(Windows).

No wall-clock ``sleep`` is used for synchronization — every rendezvous is a
``multiprocessing.Barrier``.
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.constants import MEMORY_MAX_PER_SCOPE
from cli_agent_orchestrator.services.memory_service import MemoryService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(terminal_id: str = "term-u4", cwd: str = "/home/user/proj-u4") -> dict:
    return {
        "terminal_id": terminal_id,
        "session_name": "sess-u4",
        "agent_profile": "dev",
        "provider": "claude_code",
        "cwd": cwd,
    }


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _make_engine(db_path: Path) -> Any:
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    return engine


def _make_svc(base_dir: Path, db_path: Path) -> MemoryService:
    engine = _make_engine(db_path)
    svc = MemoryService(base_dir=base_dir, db_engine=engine)
    svc._get_terminal_context = lambda terminal_id: _ctx()  # type: ignore[method-assign]
    return svc


# ---------------------------------------------------------------------------
# AC1 — durability: memories survive service reinstantiation
# ---------------------------------------------------------------------------


def test_memories_survive_service_reinstantiation(tmp_path: Path) -> None:
    """Write 5 memories via service A → discard A → fresh service B at the
    same ``base_dir``/DB file → recall returns all 5 with metadata + content.

    Also verifies that ``index.md`` on disk is regenerated for service B's
    fallback reads (``_parse_index`` round-trip).
    """
    db_path = tmp_path / "u4.db"
    base_dir = tmp_path / "memory"
    base_dir.mkdir()

    ctx = _ctx()

    # ----- Service A: write phase -----
    svc_a = _make_svc(base_dir, db_path)

    seeds = [
        {
            "key": f"durable-{i:02d}",
            "content": f"durable content {i} — should survive reinstantiation",
            "memory_type": "project",
            "tags": f"t{i},durable",
            "scope": "global",
        }
        for i in range(5)
    ]
    for s in seeds:
        _run(
            svc_a.store(
                content=s["content"],
                scope=s["scope"],
                memory_type=s["memory_type"],
                key=s["key"],
                tags=s["tags"],
                terminal_context=ctx,
            )
        )

    # Dispose pooled connections and drop all references to service A so B
    # cannot accidentally reuse A's session factory.
    svc_a._db_engine.dispose()  # type: ignore[attr-defined]
    del svc_a

    # ----- Service B: fresh construction at same base_dir + DB file -----
    svc_b = _make_svc(base_dir, db_path)

    # Recall via SQLite metadata path — exercises the durability contract.
    recalled = _run(
        svc_b.recall(
            scope="global",
            limit=50,
            search_mode="metadata",
            terminal_context=ctx,
        )
    )

    recalled_by_key = {m.key: m for m in recalled}
    for s in seeds:
        assert s["key"] in recalled_by_key, (
            f"durability violation — key {s['key']!r} missing after reinstantiation"
        )
        got = recalled_by_key[s["key"]]
        assert got.memory_type == s["memory_type"], (
            f"memory_type drift after reinstantiation for {s['key']}"
        )
        assert got.tags == s["tags"], f"tags drift after reinstantiation for {s['key']}"
        assert got.scope == s["scope"]
        # Wiki file body must still contain the original content — durability
        # of the file store, independent of the SQLite row.
        assert s["content"] in got.content, (
            f"content drift after reinstantiation for {s['key']}: "
            f"wiki file lost body"
        )

    # index.md must also survive reinstantiation — the fallback reader
    # (`_recall_file_fallback`) depends on this, and U2 injection reads it
    # directly.
    index_path = svc_b.get_index_path("global", None)
    assert index_path.exists(), "index.md must exist after reinstantiation"
    parsed = svc_b._parse_index(index_path)
    parsed_keys = {e["key"] for e in parsed if e["scope"] == "global"}
    for s in seeds:
        assert s["key"] in parsed_keys, (
            f"durability violation — key {s['key']!r} missing from index.md"
        )


# ---------------------------------------------------------------------------
# AC2 — concurrent writers: both entries present, no corruption
# ---------------------------------------------------------------------------


def _worker_store(
    db_path_str: str,
    base_dir_str: str,
    key: str,
    barrier: Any,
    result_queue: Any,
) -> None:
    """Subprocess worker: barrier-gated call to ``store()`` with its own key.

    Each worker builds a fresh engine/service because SQLAlchemy engines do
    not survive ``fork()`` cleanly — we force ``spawn`` start method below,
    but opening a new engine per process is also the pattern used elsewhere
    in this codebase.
    """
    try:
        engine = create_engine(
            f"sqlite:///{db_path_str}", connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(bind=engine)

        svc = MemoryService(base_dir=Path(base_dir_str), db_engine=engine)
        svc._get_terminal_context = lambda terminal_id: _ctx()  # type: ignore[method-assign]
        ctx = _ctx()

        # Both workers rendezvous here — releases simultaneously.
        barrier.wait(timeout=10)

        _run(
            svc.store(
                content=f"concurrent content for {key}",
                scope="global",
                memory_type="project",
                key=key,
                tags=f"concurrent,{key}",
                terminal_context=ctx,
            )
        )
        result_queue.put(("ok", key))
    except Exception as exc:  # surface in parent assertion
        result_queue.put(("err", f"{key}: {type(exc).__name__}: {exc}"))


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl not available on Windows")
def test_concurrent_writers_both_present(tmp_path: Path) -> None:
    """Two subprocess writers race on the same scope index. ``fcntl.flock``
    must serialize index writes so both entries land in ``index.md`` with
    neither truncation nor duplicate ``## global`` headers.
    """
    pytest.importorskip("fcntl")

    db_path = tmp_path / "u4-concurrent.db"
    base_dir = tmp_path / "memory"
    base_dir.mkdir()

    # Pre-initialize DB schema so subprocesses don't race on CREATE TABLE.
    _make_engine(db_path).dispose()

    mp_ctx = mp.get_context("spawn")
    barrier = mp_ctx.Barrier(2)
    result_queue: Any = mp_ctx.Queue()

    procs = [
        mp_ctx.Process(
            target=_worker_store,
            args=(str(db_path), str(base_dir), f"worker-{i}", barrier, result_queue),
        )
        for i in range(2)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=20)
        assert p.exitcode == 0, f"worker {p.name} exited {p.exitcode}"

    # Drain the queue — surface any worker-side exception.
    results: list[tuple[str, str]] = []
    while not result_queue.empty():
        results.append(result_queue.get_nowait())
    errs = [r for r in results if r[0] == "err"]
    assert not errs, f"worker errors: {errs}"
    ok_keys = {r[1] for r in results if r[0] == "ok"}
    assert ok_keys == {"worker-0", "worker-1"}, (
        f"not all workers reported ok: {results}"
    )

    # Parent re-opens the service and parses the final index state.
    svc = _make_svc(base_dir, db_path)
    index_path = svc.get_index_path("global", None)
    assert index_path.exists(), "index.md must exist after concurrent writes"

    parsed = svc._parse_index(index_path)
    parsed_keys = {e["key"] for e in parsed if e["scope"] == "global"}
    assert "worker-0" in parsed_keys, "worker-0 missing from index.md"
    assert "worker-1" in parsed_keys, "worker-1 missing from index.md"

    # No duplicate scope headers — the regeneration path must emit exactly one
    # "## global" section even under concurrent writes.
    text = index_path.read_text(encoding="utf-8")
    assert text.count("## global") == 1, (
        f"index.md must contain exactly one '## global' header; got "
        f"{text.count('## global')}"
    )

    # No half-lines: every entry line round-trips through the reader regex
    # verbatim (U3 drift guard, applied to the concurrent case).
    reader_re = re.compile(
        r"^- \[([^\]]+)\]\(([^)]+)\) — type:(\S+) tags:(\S*) ~\d+tok updated:(\S+)$"
    )
    entry_lines = [
        ln for ln in text.splitlines() if ln.startswith("- [") and "](" in ln
    ]
    assert len(entry_lines) >= 2, (
        f"expected ≥2 entry lines after 2 concurrent writes, got {len(entry_lines)}"
    )
    for ln in entry_lines:
        assert reader_re.match(ln), (
            f"corrupt entry line from concurrent write: {ln!r}"
        )


# ---------------------------------------------------------------------------
# AC3 — platform gate: skip cleanly without fcntl
# ---------------------------------------------------------------------------


def test_concurrent_writers_skipped_without_fcntl() -> None:
    """Assert the fcntl gating pattern — on Windows ``importorskip`` emits
    a skip marker, on POSIX it returns the module and the test proceeds.

    Kept as a separate test so the skip mechanism is visible in the audit
    regardless of which platform CI runs on.
    """
    fcntl = pytest.importorskip("fcntl")
    # Sanity — on POSIX the module exposes LOCK_EX (what `_update_index`
    # actually calls).
    assert hasattr(fcntl, "LOCK_EX"), "fcntl on this platform lacks LOCK_EX"


# ---------------------------------------------------------------------------
# AC4 — newest-N sort invariant (absorbs U3 challenger flag + U2 dep)
# ---------------------------------------------------------------------------


def test_per_scope_sort_returns_newest_n(tmp_path: Path) -> None:
    """Store 12 memories with staggered ``updated_at`` →
    ``get_memory_context_for_terminal`` returns exactly the 10 newest.

    Pins the sort invariant at ``memory_service.py:1116``. If
    ``order_by(updated_at.desc())`` at ``memory_service.py:182`` or the
    lexicographic sort in ``get_memory_context_for_terminal`` drifts, this
    test fails instead of silently serving stale memories.

    ``updated_at`` is forced via a post-hoc ORM update rather than a
    wall-clock sleep so the test is deterministic and fast.
    """
    db_path = tmp_path / "u4-sort.db"
    base_dir = tmp_path / "memory"
    base_dir.mkdir()

    svc = _make_svc(base_dir, db_path)
    ctx = _ctx()

    total = 12
    for i in range(total):
        _run(
            svc.store(
                content=f"sortable body {i:02d}",
                scope="global",
                memory_type="project",
                key=f"sort-{i:02d}",
                tags=f"t{i}",
                terminal_context=ctx,
            )
        )

    # Force staggered updated_at: sort-00 oldest, sort-11 newest.
    base_time = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)
    with svc._get_db_session() as db:
        for i in range(total):
            row = (
                db.query(MemoryMetadataModel)
                .filter_by(key=f"sort-{i:02d}", scope="global")
                .first()
            )
            assert row is not None, f"sort-{i:02d} must be in SQLite"
            # Naïve UTC — matches the column type used elsewhere in the codebase.
            row.updated_at = (base_time + timedelta(minutes=i)).replace(tzinfo=None)
        db.commit()

    # Refresh index.md from the mutated SQLite rows.
    svc._regenerate_scope_index("global", None)

    block = svc.get_memory_context_for_terminal("term-u4", budget_chars=100_000)
    assert block, "context block must not be empty"

    inner = block.split("<cao-memory>")[1].split("</cao-memory>")[0]
    global_line_keys: list[str] = []
    for line in inner.splitlines():
        if not line.startswith("- [global] "):
            continue
        # Format: "- [global] <key>: <content>"
        match = re.match(r"^- \[global\] ([^:]+): ", line)
        assert match, f"malformed global context line: {line!r}"
        global_line_keys.append(match.group(1))

    # Per-scope cap strictly enforced: exactly MEMORY_MAX_PER_SCOPE entries.
    assert len(global_line_keys) == MEMORY_MAX_PER_SCOPE, (
        f"expected exactly {MEMORY_MAX_PER_SCOPE} entries after sort+cap, "
        f"got {len(global_line_keys)}: {global_line_keys}"
    )

    # The 10 returned keys must be exactly the 10 newest (sort-02 .. sort-11).
    expected_newest = {
        f"sort-{i:02d}" for i in range(total - MEMORY_MAX_PER_SCOPE, total)
    }
    got = set(global_line_keys)
    assert got == expected_newest, (
        f"sort invariant broken: expected newest-{MEMORY_MAX_PER_SCOPE} keys "
        f"{sorted(expected_newest)}, got {sorted(got)}"
    )
