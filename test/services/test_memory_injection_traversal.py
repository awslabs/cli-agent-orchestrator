"""Cross-project traversal guard for memory injection.

`get_memory_context_for_terminal` reads each scope's ``index.md`` and loads
the wiki file named by every entry's ``relative_path``. That path comes from
an on-disk index file, which can be corrupted or hand-crafted. A malicious
entry such as ``../<other-project>/wiki/project/secret.md`` resolves to a
real, well-formed wiki file *under the global memory base* but *outside this
scope's wiki directory* — leaking another project's memory into this
terminal's context.

The guard must validate each resolved wiki file against the per-scope wiki
directory (``project_dir / "wiki"``), not the global base directory. The
victim file here is created via ``store`` so it is a fully-formed memory the
parser accepts; the traversal guard is therefore the *only* thing standing
between it and injection.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cli_agent_orchestrator.services.memory_service import MemoryService

pytestmark = pytest.mark.usefixtures("isolated_memory_db")

VICTIM_CONTENT = "TOP SECRET cross-project content"


def _ctx(cwd: str, terminal_id: str = "term-trav") -> dict:
    return {
        "terminal_id": terminal_id,
        "session_name": "sess-trav",
        "agent_profile": "dev",
        "provider": "claude_code",
        "cwd": cwd,
    }


def _make_svc(tmp_path: Path, ctx: dict) -> MemoryService:
    svc = MemoryService(base_dir=tmp_path)
    svc._get_terminal_context = lambda terminal_id: ctx  # type: ignore[method-assign]
    return svc


def test_index_entry_escaping_scope_wiki_dir_is_rejected(tmp_path: Path) -> None:
    """A relative_path escaping this scope's wiki dir must not be injected.

    The escape target is a real memory in a *sibling* project under the same
    global memory base, so the old base-dir guard would have admitted it. The
    tightened per-scope guard must reject it.
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

    block = svc.get_memory_context_for_terminal("term-trav", budget_chars=100_000)

    # The traversal entry is the only one present; it must be rejected, so the
    # context is empty and never contains the victim's content.
    assert VICTIM_CONTENT not in block
    assert block == ""


def test_legitimate_in_scope_entry_still_injected(tmp_path: Path) -> None:
    """A normal entry within the scope's wiki dir is still admitted.

    Guards against the fix over-tightening and dropping valid memories.
    """
    ctx = _ctx(cwd="/home/user/normal-proj")
    svc = _make_svc(tmp_path, ctx)

    asyncio.run(
        svc.store(
            content="a legitimate global reference",
            scope="global",
            memory_type="reference",
            key="legit-ref",
            terminal_context=ctx,
        )
    )

    block = svc.get_memory_context_for_terminal("term-trav", budget_chars=100_000)
    assert "a legitimate global reference" in block
