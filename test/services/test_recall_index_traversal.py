"""Traversal guard for the substring-recall (`_metadata_recall`) path.

`_metadata_recall` walks each scope's ``index.md`` and reads the wiki file
named by every entry's ``relative_path``. That path comes from an on-disk
index file, which can be corrupted or hand-crafted. A malicious entry such
as ``[x](../../../../etc/passwd)`` (or ``../<other-project>/wiki/...``)
resolves outside this scope's wiki directory, so an unguarded read would
return an arbitrary out-of-base file's bytes as a "memory"
(information disclosure / cross-scope leak).

This mirrors the guard already covered for
``get_memory_context_for_terminal`` in ``test_memory_injection_traversal.py``:
the resolved wiki file must stay under the per-scope wiki dir, and an escaping
entry must be silently skipped (not read, not returned).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from cli_agent_orchestrator.services.memory_service import MemoryService

VICTIM_CONTENT = "TOP SECRET recall cross-project content"


def _ctx(cwd: str, terminal_id: str = "term-recall-trav") -> dict:
    return {
        "terminal_id": terminal_id,
        "session_name": "sess-recall-trav",
        "agent_profile": "dev",
        "provider": "claude_code",
        "cwd": cwd,
    }


def _make_svc(tmp_path: Path, ctx: dict) -> MemoryService:
    svc = MemoryService(base_dir=tmp_path)
    svc._get_terminal_context = lambda terminal_id: ctx  # type: ignore[method-assign]
    return svc


def test_recall_skips_index_entry_escaping_scope_wiki_dir(tmp_path: Path) -> None:
    """An escaping ``relative_path`` must not be read or returned by recall.

    The escape target is a real, well-formed memory in a *sibling* project
    under the same global memory base, so an unguarded read would happily
    return it. The containment guard must skip the entry instead.
    """
    # 1. Create a real, well-formed victim memory in a separate project.
    victim_ctx = _ctx(cwd="/home/user/victim-proj")
    victim_svc = _make_svc(tmp_path, victim_ctx)
    asyncio.run(
        victim_svc.store(
            content=VICTIM_CONTENT,
            scope="project",
            memory_type="project",
            key="secret",
            terminal_context=victim_ctx,
        )
    )
    victim_hash = victim_svc.resolve_scope_id("project", victim_ctx)
    assert victim_hash, "project scope must resolve to a non-empty id"
    victim_file = tmp_path / victim_hash / "wiki" / "project" / "secret.md"
    assert victim_file.exists(), "victim memory file should have been created"
    assert VICTIM_CONTENT in victim_file.read_text(encoding="utf-8")

    # 2. Attacker terminal in a *different* project. Hand-craft its global
    #    index.md with one entry whose relative_path climbs out of the global
    #    wiki dir into the victim project's wiki file.
    attacker_ctx = _ctx(cwd="/home/user/attacker-proj")
    svc = _make_svc(tmp_path, attacker_ctx)
    scope_id = svc.resolve_scope_id("global", attacker_ctx)
    global_wiki = svc._get_project_dir("global", scope_id) / "wiki"
    global_wiki.mkdir(parents=True, exist_ok=True)

    hops = len(global_wiki.relative_to(tmp_path).parts)
    escape = "/".join([".."] * hops) + f"/{victim_hash}/wiki/project/secret.md"
    # Sanity: the escape really does resolve onto the victim file.
    assert (global_wiki / escape).resolve() == victim_file.resolve()

    (global_wiki / "index.md").write_text(
        "# Memory Index\n\n## global\n"
        f"- [secret]({escape}) — type:project tags: ~5tok updated:2026-05-29T00:00:00Z\n",
        encoding="utf-8",
    )

    results = asyncio.run(
        svc.recall(
            scope="global",
            terminal_context=attacker_ctx,
            search_mode="metadata",
        )
    )

    # The traversal entry is the only one present; it must be skipped, so no
    # memory is returned and the victim's content never leaks.
    assert results == []
    assert all(VICTIM_CONTENT not in m.content for m in results)


def test_recall_still_returns_legitimate_in_scope_entry(tmp_path: Path) -> None:
    """A normal entry within the scope's wiki dir is still returned.

    Guards against the fix over-tightening and dropping valid memories.
    """
    ctx = _ctx(cwd="/home/user/normal-proj")
    svc = _make_svc(tmp_path, ctx)

    asyncio.run(
        svc.store(
            content="a legitimate global recall reference",
            scope="global",
            memory_type="reference",
            key="legit-ref",
            terminal_context=ctx,
        )
    )

    results = asyncio.run(
        svc.recall(
            scope="global",
            terminal_context=ctx,
            search_mode="metadata",
        )
    )

    assert any("a legitimate global recall reference" in m.content for m in results)
