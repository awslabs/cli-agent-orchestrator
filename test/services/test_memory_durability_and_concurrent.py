"""SC-4 durability + SC-5 concurrent-write safety tests (U4).

- SC-4: ``MemoryService`` reinstantiation at the same ``base_dir``/DB file
  round-trips stored memories with field-level integrity.
- SC-5: Two concurrent writers to the same scope produce a parseable
  ``index.md`` with both entries present, under ``fcntl.flock``-guarded
  index writes.

Concurrency uses ``multiprocessing`` (not threads) to exercise the OS-level
flock across separate file descriptors AND separate SQLite connections.
Tests that require ``fcntl`` are skipped on platforms without it (Windows).

No wall-clock ``sleep`` is used for synchronization — every rendezvous is a
``multiprocessing.Barrier``.
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine

from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.constants import MEMORY_MAX_PER_SCOPE
from cli_agent_orchestrator.services.memory_service import MemoryService


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
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
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
    db_path = tmp_path / "u4.db"
    base_dir = tmp_path / "memory"
    base_dir.mkdir()

    ctx = _ctx()
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

    svc_a._db_engine.dispose()  # type: ignore[attr-defined]
    del svc_a

    svc_b = _make_svc(base_dir, db_path)
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
        assert (
            s["key"] in recalled_by_key
        ), f"durability violation — {s['key']!r} missing after reinstantiation"
        got = recalled_by_key[s["key"]]
        assert got.memory_type == s["memory_type"]
        assert got.tags == s["tags"]
        assert got.scope == s["scope"]
        assert s["content"] in got.content

    index_path = svc_b.get_index_path("global", None)
    assert index_path.exists(), "index.md must exist after reinstantiation"
    parsed = svc_b._parse_index(index_path)
    parsed_keys = {e["key"] for e in parsed if e["scope"] == "global"}
    for s in seeds:
        assert s["key"] in parsed_keys


# ---------------------------------------------------------------------------
# AC2 — concurrent writers
# ---------------------------------------------------------------------------


def _worker_store(
    db_path_str: str,
    base_dir_str: str,
    key: str,
    barrier: Any,
    result_queue: Any,
) -> None:
    """Subprocess worker: barrier-gated call to ``store()`` with its own key."""
    try:
        engine = create_engine(
            f"sqlite:///{db_path_str}", connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(bind=engine)

        svc = MemoryService(base_dir=Path(base_dir_str), db_engine=engine)
        svc._get_terminal_context = lambda terminal_id: _ctx()  # type: ignore[method-assign]
        ctx = _ctx()

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
    except Exception as exc:
        result_queue.put(("err", f"{key}: {type(exc).__name__}: {exc}"))


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl not available on Windows")
def test_concurrent_writers_both_present(tmp_path: Path) -> None:
    pytest.importorskip("fcntl")

    db_path = tmp_path / "u4-concurrent.db"
    base_dir = tmp_path / "memory"
    base_dir.mkdir()

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

    results: list[tuple[str, str]] = []
    while not result_queue.empty():
        results.append(result_queue.get_nowait())
    errs = [r for r in results if r[0] == "err"]
    assert not errs, f"worker errors: {errs}"
    ok_keys = {r[1] for r in results if r[0] == "ok"}
    assert ok_keys == {"worker-0", "worker-1"}, f"not all workers reported ok: {results}"

    svc = _make_svc(base_dir, db_path)
    index_path = svc.get_index_path("global", None)
    assert index_path.exists(), "index.md must exist after concurrent writes"

    parsed = svc._parse_index(index_path)
    parsed_keys = {e["key"] for e in parsed if e["scope"] == "global"}
    assert "worker-0" in parsed_keys, "worker-0 missing from index.md"
    assert "worker-1" in parsed_keys, "worker-1 missing from index.md"

    text = index_path.read_text(encoding="utf-8")
    assert text.count("## global") == 1, (
        f"index.md must contain exactly one '## global' header; got " f"{text.count('## global')}"
    )

    reader_re = re.compile(
        r"^- \[([^\]]+)\]\(([^)]+)\) — type:(\S+) tags:(\S*) ~\d+tok updated:(\S+)$"
    )
    entry_lines = [ln for ln in text.splitlines() if ln.startswith("- [") and "](" in ln]
    assert (
        len(entry_lines) >= 2
    ), f"expected ≥2 entry lines after 2 concurrent writes, got {len(entry_lines)}"
    for ln in entry_lines:
        assert reader_re.match(ln), f"corrupt entry line: {ln!r}"


# ---------------------------------------------------------------------------
# AC3 — fcntl gating
# ---------------------------------------------------------------------------


def test_concurrent_writers_skipped_without_fcntl() -> None:
    fcntl = pytest.importorskip("fcntl")
    assert hasattr(fcntl, "LOCK_EX"), "fcntl on this platform lacks LOCK_EX"


# ---------------------------------------------------------------------------
# AC4 — newest-N sort invariant (lexicographic ISO-8601 Z order)
# ---------------------------------------------------------------------------


def test_per_scope_sort_returns_newest_n(tmp_path: Path) -> None:
    """Store 12 entries with monotonically increasing index timestamps →
    ``get_memory_context_for_terminal`` returns exactly the newest 10.

    The sort key in ``get_memory_context_for_terminal`` is the ``updated``
    field parsed out of ``index.md`` via lexicographic string compare. This
    test pins that contract: when ISO-8601 Z timestamps are monotonically
    ordered they must lexicographically sort to the same order, and the cap
    must take the top-N.
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

    # Patch index.md with monotonically increasing ISO-8601 Z timestamps so
    # the sort is unambiguous (within-second store calls collapse otherwise).
    index_path = svc.get_index_path("global", None)
    text = index_path.read_text(encoding="utf-8")
    ts_re = re.compile(r"updated:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
    new_lines = []
    counter = 0
    for line in text.splitlines():
        m = re.match(r"^- \[(sort-\d{2})\]", line)
        if m:
            idx = int(m.group(1).split("-")[1])
            stamped = f"updated:2026-04-20T12:{idx:02d}:00Z"
            new_lines.append(ts_re.sub(stamped, line))
            counter += 1
        else:
            new_lines.append(line)
    assert counter == total, f"expected {total} sort-NN lines, rewrote {counter}"
    index_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    block = svc.get_memory_context_for_terminal("term-u4", budget_chars=100_000)
    assert block, "context block must not be empty"

    inner = block.split("<cao-memory>")[1].split("</cao-memory>")[0]
    global_line_keys: list[str] = []
    for line in inner.splitlines():
        if not line.startswith("- [global] "):
            continue
        match = re.match(r"^- \[global\] ([^:]+): ", line)
        assert match, f"malformed global context line: {line!r}"
        global_line_keys.append(match.group(1))

    assert len(global_line_keys) == MEMORY_MAX_PER_SCOPE, (
        f"expected exactly {MEMORY_MAX_PER_SCOPE} entries after sort+cap, "
        f"got {len(global_line_keys)}: {global_line_keys}"
    )

    expected_newest = {f"sort-{i:02d}" for i in range(total - MEMORY_MAX_PER_SCOPE, total)}
    got = set(global_line_keys)
    assert got == expected_newest, (
        f"sort invariant broken: expected newest-{MEMORY_MAX_PER_SCOPE} keys "
        f"{sorted(expected_newest)}, got {sorted(got)}"
    )
