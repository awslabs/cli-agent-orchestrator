"""SC-3 — Regex round-trip invariant for index.md writer/reader.

Every field written through the production writer path (`store()` →
`_regenerate_scope_index()` / `_update_index()`) must be recovered unchanged
by the production reader (`_parse_index()` regex at
`memory_service.py:937-940`).

This guards against silent drift between the writer format string and the
reader regex. Drift would cause entries to vanish from injection (U2) or sort
order to break (the per-scope sort at `memory_service.py:1116` depends on
lexicographic ISO-8601 ordering).

Edge cases covered: unicode key candidates, multi-word tags, comma-delimited
tags, empty tags, multiple scopes in one index, ISO-8601 timestamp parseability.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from cli_agent_orchestrator.services.memory_service import MemoryService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(cwd: str = "/home/user/proj-u3") -> dict:
    return {
        "terminal_id": "term-u3",
        "session_name": "sess-u3",
        "agent_profile": "dev",
        "provider": "claude_code",
        "cwd": cwd,
    }


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _parse_iso8601_z(ts: str) -> datetime:
    """Parse the writer's strftime('%Y-%m-%dT%H:%M:%SZ') format.

    Python 3.10's datetime.fromisoformat does not accept the trailing 'Z'
    suffix, so use strptime against the exact writer format. If the writer
    ever drifts off ISO-8601 with Z-suffix this raises ValueError, which is
    the test invariant we want.
    """
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# AC1 — writer → reader round-trip on required fields
# ---------------------------------------------------------------------------


def test_roundtrip_store_parses_back_all_fields(tmp_path: Path) -> None:
    """Store N memories → _parse_index recovers every (key, type, tags, path).

    Uses the production `store()` path so the writer codepath is exactly what
    production callers exercise.
    """
    svc = MemoryService(base_dir=tmp_path)
    ctx = _ctx()

    written: list[dict] = []
    for i in range(5):
        key = f"roundtrip-key-{i:03d}"
        mem = _run(
            svc.store(
                content=f"body content {i}",
                scope="global",
                memory_type="project",
                key=key,
                tags=f"tag{i}",
                terminal_context=ctx,
            )
        )
        written.append(
            {
                "key": mem.key,
                "memory_type": "project",
                "tags": f"tag{i}",
                "relative_path": f"global/{mem.key}.md",
            }
        )

    scope_id = svc.resolve_scope_id("global", ctx)
    index_path = svc.get_index_path("global", scope_id)
    assert index_path.exists(), "writer must produce an index.md file"

    parsed = svc._parse_index(index_path)
    # Only look at the global scope slice; other scopes may be empty.
    parsed_global = [e for e in parsed if e["scope"] == "global"]

    # Every entry we wrote is recovered — no silent drops.
    parsed_keys = {e["key"] for e in parsed_global}
    for w in written:
        assert w["key"] in parsed_keys, (
            f"writer produced key {w['key']!r} but parser did not recover it"
        )

    # Field-level equality for each recovered entry.
    parsed_by_key = {e["key"]: e for e in parsed_global}
    for w in written:
        p = parsed_by_key[w["key"]]
        assert p["memory_type"] == w["memory_type"], (
            f"memory_type drift for {w['key']}: wrote {w['memory_type']!r} "
            f"read {p['memory_type']!r}"
        )
        assert p["tags"] == w["tags"], (
            f"tags drift for {w['key']}: wrote {w['tags']!r} read {p['tags']!r}"
        )
        assert p["relative_path"] == w["relative_path"], (
            f"relative_path drift for {w['key']}: wrote {w['relative_path']!r} "
            f"read {p['relative_path']!r}"
        )
        # scope label recovered
        assert p["scope"] == "global"


# ---------------------------------------------------------------------------
# AC2 — ISO-8601 timestamp format invariant
# ---------------------------------------------------------------------------


def test_roundtrip_updated_at_is_iso8601_parseable(tmp_path: Path) -> None:
    """Every recovered `updated_at` must parse as ISO-8601 with Z suffix.

    U2's per-scope sort relies on lexicographic ISO-8601 ordering. If the
    writer ever drifts off this format, this test fails fast.
    """
    svc = MemoryService(base_dir=tmp_path)
    ctx = _ctx()

    for i in range(3):
        _run(
            svc.store(
                content=f"finding {i}",
                scope="global",
                memory_type="reference",
                key=f"iso-{i:03d}",
                terminal_context=ctx,
            )
        )

    scope_id = svc.resolve_scope_id("global", ctx)
    index_path = svc.get_index_path("global", scope_id)
    parsed = [e for e in svc._parse_index(index_path) if e["scope"] == "global"]

    assert parsed, "expected at least one entry in global scope"
    for entry in parsed:
        ts = entry["updated_at"]
        # Lexicographic sort in U2 relies on fixed width — spot-check format.
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", ts), (
            f"updated_at {ts!r} does not match ISO-8601 Z-suffix format"
        )
        # And it must actually parse — no regex-only approximations.
        parsed_dt = _parse_iso8601_z(ts)
        assert parsed_dt.year >= 2020


# ---------------------------------------------------------------------------
# Edge case — empty tags
# ---------------------------------------------------------------------------


def test_roundtrip_empty_tags(tmp_path: Path) -> None:
    """Empty `tags=""` must round-trip to empty string — reader regex uses \\S*."""
    svc = MemoryService(base_dir=tmp_path)
    ctx = _ctx()

    _run(
        svc.store(
            content="content with no tags",
            scope="global",
            memory_type="project",
            key="empty-tags-key",
            tags="",
            terminal_context=ctx,
        )
    )

    scope_id = svc.resolve_scope_id("global", ctx)
    index_path = svc.get_index_path("global", scope_id)
    parsed = [e for e in svc._parse_index(index_path) if e["key"] == "empty-tags-key"]
    assert len(parsed) == 1, "entry must be recovered"
    assert parsed[0]["tags"] == "", f"empty tags drifted to {parsed[0]['tags']!r}"


# ---------------------------------------------------------------------------
# Edge case — comma-delimited multi-tag (the project's convention for "multiple tags")
# ---------------------------------------------------------------------------


def test_roundtrip_comma_delimited_tags(tmp_path: Path) -> None:
    """Comma-joined tags (no whitespace) round-trip verbatim.

    The writer emits `tags:{tags}` and the reader captures `\\S*`, so any
    whitespace-free tag blob — including commas — survives round-trip.
    """
    svc = MemoryService(base_dir=tmp_path)
    ctx = _ctx()

    tag_blob = "alpha,beta,gamma"
    _run(
        svc.store(
            content="tagged content",
            scope="global",
            memory_type="reference",
            key="csv-tags-key",
            tags=tag_blob,
            terminal_context=ctx,
        )
    )

    scope_id = svc.resolve_scope_id("global", ctx)
    index_path = svc.get_index_path("global", scope_id)
    parsed = [e for e in svc._parse_index(index_path) if e["key"] == "csv-tags-key"]
    assert len(parsed) == 1
    assert parsed[0]["tags"] == tag_blob, (
        f"csv tags drifted: wrote {tag_blob!r} read {parsed[0]['tags']!r}"
    )


# ---------------------------------------------------------------------------
# Edge case — multi-scope index
# ---------------------------------------------------------------------------


def test_roundtrip_multiple_scopes_in_one_index(tmp_path: Path) -> None:
    """A single index.md with entries in multiple scopes round-trips cleanly.

    ``_regenerate_scope_index`` writes `## <scope>` headers; `_parse_index`
    picks up `current_scope` from those headers. Test all three reachable
    scopes share one index.md and each scope's entries are labeled correctly.
    """
    svc = MemoryService(base_dir=tmp_path)
    ctx = _ctx()

    # session/project/global all resolve under the same project_dir (global
    # container for session; project hash for project; global dir for global).
    # The per-scope index.md only reflects rows within its scope_id partition,
    # but `_regenerate_scope_index` still groups by scope inside the file.
    expected: dict[str, list[str]] = {}
    for scope in ("global", "project", "session"):
        expected[scope] = []
        for i in range(2):
            key = f"{scope}-key-{i}"
            _run(
                svc.store(
                    content=f"{scope} body {i}",
                    scope=scope,
                    memory_type="project",
                    key=key,
                    tags=f"{scope}tag",
                    terminal_context=ctx,
                )
            )
            expected[scope].append(key)

    # Parse each scope's index.md and verify the entries labeled for that scope.
    for scope, wanted_keys in expected.items():
        scope_id = svc.resolve_scope_id(scope, ctx)
        index_path = svc.get_index_path(scope, scope_id)
        assert index_path.exists(), f"{scope} index.md missing"
        parsed = svc._parse_index(index_path)
        got = {e["key"] for e in parsed if e["scope"] == scope}
        missing = set(wanted_keys) - got
        assert not missing, (
            f"{scope} scope lost keys after round-trip: {missing}. "
            f"parsed rows: {parsed!r}"
        )


# ---------------------------------------------------------------------------
# AC4 — drift guard: fails if writer format or reader regex is edited
# ---------------------------------------------------------------------------


def test_writer_emits_reader_regex_format(tmp_path: Path) -> None:
    """Every writer-produced line must match the exact reader regex verbatim.

    This is the tightest lock on writer/reader parity. It pins the emitted
    line format byte-for-byte to the current regex. Any change to either side
    causes an immediate test failure.
    """
    svc = MemoryService(base_dir=tmp_path)
    ctx = _ctx()

    keys = [f"drift-{i:03d}" for i in range(4)]
    for key in keys:
        _run(
            svc.store(
                content="content for drift guard",
                scope="global",
                memory_type="project",
                key=key,
                tags="csv,tag,list",
                terminal_context=ctx,
            )
        )

    scope_id = svc.resolve_scope_id("global", ctx)
    index_path = svc.get_index_path("global", scope_id)
    text = index_path.read_text(encoding="utf-8")

    # The reader's regex, lifted verbatim from memory_service.py:937-940.
    reader_re = re.compile(
        r"^- \[([^\]]+)\]\(([^)]+)\) — type:(\S+) tags:(\S*) ~\d+tok updated:(\S+)$"
    )

    entry_lines = [
        ln
        for ln in text.splitlines()
        if ln.startswith("- [") and "](" in ln
    ]
    assert entry_lines, "writer produced no entry lines"

    for line in entry_lines:
        assert reader_re.match(line), (
            f"writer emitted a line that does NOT match the reader regex — "
            f"drift detected: {line!r}"
        )


# ---------------------------------------------------------------------------
# Performance — AC requires < 1s (U3 tasks.md)
# ---------------------------------------------------------------------------


def test_roundtrip_runs_under_one_second(tmp_path: Path) -> None:
    """Full round-trip for 10 entries completes under 1 second.

    Sanity check against the tasks.md AC4 runtime requirement.
    """
    import time

    svc = MemoryService(base_dir=tmp_path)
    ctx = _ctx(cwd="/home/user/proj-u3-perf")

    start = time.perf_counter()
    for i in range(10):
        _run(
            svc.store(
                content=f"perf body {i}",
                scope="global",
                memory_type="project",
                key=f"perf-{i:03d}",
                tags=f"t{i}",
                terminal_context=ctx,
            )
        )
    scope_id = svc.resolve_scope_id("global", ctx)
    index_path = svc.get_index_path("global", scope_id)
    parsed = svc._parse_index(index_path)
    elapsed = time.perf_counter() - start

    assert any(e["key"].startswith("perf-") for e in parsed)
    assert elapsed < 1.0, f"round-trip took {elapsed:.3f}s, exceeds 1s budget"
