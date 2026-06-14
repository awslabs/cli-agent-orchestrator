"""Cross-reference (See-Also) tests for the memory wiki.

Covers:
- Acceptance: after a successful LLM compile + find_related round-trip, the
  article gains a `## See Also` block and SQLite ``related_keys`` is populated.
- Link injection / path traversal defences on every read and render path.
- Fanout, dedup, cycle and depth-1 guarantees of recall expansion.
- Length caps (parse-side truncation + fresh-DB CHECK constraint).
- Markdown smuggling defences in the rendered See-Also block.
- Cross-scope leakage and silent-skip semantics for missing/invalid keys.
- JSON-parse hardening of find_related output.
- Log hygiene: no content bytes in WARNING/INFO records.
- Transient ``is_related`` flag freshness across recalls.
- Compile-mode bypass: append mode and compile fallback never run the
  second pass.
- Critical regression: ``recall(include_related=False)`` is byte-identical
  to the non-expanded behaviour.

The compiler is stubbed; integration paths exercise ``MemoryService.store()``
(with its deferred background compile drained explicitly) and ``recall()``
against a per-test SQLite DB.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.services import wiki_compiler
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.wiki_compiler import (
    SEE_ALSO_LINK_RE,
    RelatedResult,
    find_related,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _ctx(
    *,
    cwd: str = "/home/user/project",
    session: str = "test-session",
    agent: str = "developer",
    provider: str = "claude_code",
    terminal: str = "term-001",
    caller_scope: str = "global",
) -> dict:
    return {
        "terminal_id": terminal,
        "session_name": session,
        "agent_profile": agent,
        "provider": provider,
        "cwd": cwd,
        "caller_scope": caller_scope,
    }


@pytest.fixture
def db_engine(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture
def svc(tmp_path, db_engine):
    return MemoryService(base_dir=tmp_path, db_engine=db_engine)


@pytest.fixture
def stubbed_llm(monkeypatch):
    """Replace ``_build_llm_client`` with a sentinel + ``_llm_call`` with a
    user-controlled response. Returns a state dict with ``calls`` and a
    settable ``response``.
    """
    state = {"response": "[]", "calls": []}

    monkeypatch.setattr(wiki_compiler, "_build_llm_client", lambda *a, **k: object())

    async def _fake(_client, system, user, *, timeout_s):
        state["calls"].append({"system": system, "user": user, "timeout_s": timeout_s})
        v = state["response"]
        if isinstance(v, BaseException):
            raise v
        return v

    monkeypatch.setattr(wiki_compiler, "_llm_call", _fake)
    return state


def _set_related_keys(db_engine, key: str, scope: str, raw: Optional[str]) -> None:
    Session = sessionmaker(bind=db_engine)
    with Session() as db:
        row = db.query(MemoryMetadataModel).filter_by(key=key, scope=scope).first()
        assert row is not None, f"row {key!r} scope={scope!r} not found"
        row.related_keys = raw
        db.commit()


def _extract_id_line(prompt: str) -> str:
    """Pull the existing ``<!-- id: ... -->`` line out of a compile prompt so
    a stub can echo it verbatim (output rule 4b requires byte-for-byte
    preservation of the existing id header)."""
    for line in prompt.splitlines():
        if wiki_compiler._ID_HEADER_RE.match(line.strip()):
            return line.strip()
    raise AssertionError("no id header line found in compile prompt")


async def _drain_compiles(svc: MemoryService) -> None:
    """Await every scheduled background compile task to completion."""
    pending = list(svc._compile_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# ===========================================================================
# Acceptance — See-Also added to article + related_keys populated
# ===========================================================================


class TestAcceptanceSeeAlso:
    def test_see_also_added_to_article(self, svc, db_engine, stubbed_llm, monkeypatch):
        """Two stores on the same key route the update through the deferred
        LLM compile. After the background task completes, the article carries
        a `## See Also` block referencing the candidate key AND the SQLite
        ``related_keys`` cell is populated.
        """
        ctx = _ctx()

        async def _seq(_client, system, user, *, timeout_s):
            stubbed_llm["calls"].append({"system": system, "user": user, "timeout_s": timeout_s})
            # First call is the compile pass; second is find_related.
            if "wiki compiler" in system.lower():
                # A compliant LLM preserves the existing id header byte-for-byte
                # (output rule 4b); echo the real id line from the prompt.
                id_line = _extract_id_line(user)
                return (
                    "# testing-conventions\n"
                    f"{id_line}\n"
                    "\n"
                    "Prefer pytest with pytest-asyncio for async tests. Add coverage\n"
                    "for retry logic.\n"
                )
            return '["auth-issues"]'

        monkeypatch.setattr(wiki_compiler, "_llm_call", _seq)

        async def _scenario():
            # Seed the candidate target.
            await svc.store(
                content="auth issues are tracked in this article",
                scope="global",
                memory_type="reference",
                key="auth-issues",
                tags="auth",
                terminal_context=ctx,
            )
            # First store of the topic is brand-new — bypasses compile.
            await svc.store(
                content="prefer pytest",
                scope="global",
                memory_type="reference",
                key="testing-conventions",
                tags="testing",
                terminal_context=ctx,
            )
            # Second store enters the is_update branch and schedules the
            # deferred compile on this running loop.
            monkeypatch.setenv("CAO_MEMORY_COMPILE_MODE", "llm")
            await svc.store(
                content="add coverage for retry logic",
                scope="global",
                memory_type="reference",
                key="testing-conventions",
                tags="testing",
                terminal_context=ctx,
            )
            await _drain_compiles(svc)

        _run(_scenario())

        # SQLite related_keys populated by the second pass.
        Session = sessionmaker(bind=db_engine)
        with Session() as db:
            row = (
                db.query(MemoryMetadataModel)
                .filter_by(key="testing-conventions", scope="global")
                .first()
            )
            assert row is not None
            assert row.related_keys == "auth-issues"

        # Article on disk has the `## See Also` block in canonical shape.
        wiki_path = svc.get_wiki_path("global", None, "testing-conventions")
        body = wiki_path.read_text(encoding="utf-8")
        assert "## See Also" in body
        link_lines = [
            ln for ln in body.splitlines() if ln.startswith("- [") and "auth-issues" in ln
        ]
        assert len(link_lines) == 1
        assert SEE_ALSO_LINK_RE.match(link_lines[0]), f"link line shape: {link_lines[0]!r}"

    def test_compile_writeback_preserves_provenance(self, svc, db_engine, stubbed_llm, monkeypatch):
        """The background compile rewrite is not a new store — it must not
        erase ``source_provider``/``source_terminal_id`` stamped by the
        store() that scheduled it.
        """
        ctx = _ctx(provider="kimi_cli", terminal="term-prov")

        async def _seq(_client, system, user, *, timeout_s):
            stubbed_llm["calls"].append({"system": system, "user": user, "timeout_s": timeout_s})
            if "wiki compiler" in system.lower():
                # Preserve the real id header (output rule 4b).
                return f"# prov-topic\n{_extract_id_line(user)}\n\nmerged body\n"
            return "[]"

        monkeypatch.setattr(wiki_compiler, "_llm_call", _seq)

        async def _scenario():
            await svc.store(
                content="first body",
                scope="global",
                memory_type="reference",
                key="prov-topic",
                tags="t",
                terminal_context=ctx,
            )
            monkeypatch.setenv("CAO_MEMORY_COMPILE_MODE", "llm")
            await svc.store(
                content="second body",
                scope="global",
                memory_type="reference",
                key="prov-topic",
                tags="t",
                terminal_context=ctx,
            )
            await _drain_compiles(svc)

        _run(_scenario())

        Session = sessionmaker(bind=db_engine)
        with Session() as db:
            row = db.query(MemoryMetadataModel).filter_by(key="prov-topic", scope="global").first()
            assert row is not None
            assert row.last_compiled_at is not None, "compile write-back did not land"
            assert row.source_provider == "kimi_cli"
            assert row.source_terminal_id == "term-prov"


# ===========================================================================
# Link injection / path traversal
# ===========================================================================


class TestPathTraversal:
    def test_find_related_rejects_key_not_in_candidates(self, stubbed_llm):
        stubbed_llm["response"] = '["../../etc/passwd"]'
        result = _run(
            find_related(
                "<!-- id: x -->\n# x\nbody",
                candidate_keys=["good-key"],
                topic_key="x",
                timeout_s=2.0,
            )
        )
        assert result.related_keys == []
        assert result.used_llm is True  # JSON parsed; per-element drop is success

    def test_find_related_rejects_key_failing_sanitiser(self, stubbed_llm):
        stubbed_llm["response"] = '["valid-key/"]'  # slash fails the key regex
        result = _run(
            find_related(
                "body",
                candidate_keys=["good-key"],
                topic_key="x",
                timeout_s=2.0,
            )
        )
        assert result.related_keys == []
        assert result.used_llm is True

    def test_parse_related_keys_drops_handedited_traversal(self, caplog):
        """A hand-edited DB row containing ``../../evil`` after a good key →
        good key returned, evil entry dropped, WARNING logged.

        ``_sanitize_key('../../evil')`` returns ``'evil'`` (basename), so the
        round-trip ``canonical != p`` check fires and the entry is dropped.
        """
        with caplog.at_level(
            logging.WARNING, logger="cli_agent_orchestrator.services.memory_service"
        ):
            out = MemoryService._parse_related_keys("good-key,../../evil", scope="project")
        assert out == ["good-key"]
        assert any("related_keys_unsanitised" in r.getMessage() for r in caplog.records)

    def test_load_related_memory_rejects_symlink_escape(self, svc, tmp_path, db_engine):
        """A symlink in the wiki tree pointing outside ``base_dir`` is
        rejected by the containment guard. ``_load_related_memory`` returns
        None — silent skip, no exception.
        """
        ctx = _ctx()
        _run(
            svc.store(
                content="legit body",
                scope="global",
                memory_type="reference",
                key="legit-key",
                tags="t",
                terminal_context=ctx,
            )
        )
        # Replace the legit wiki file with a symlink escaping base_dir.
        wiki_path = svc.get_wiki_path("global", None, "legit-key")
        outside = tmp_path.parent / "outside-of-base-dir.md"
        outside.write_text("escaped\n", encoding="utf-8")
        wiki_path.unlink()
        os.symlink(str(outside), str(wiki_path))

        result = svc._load_related_memory("legit-key", "global", None)
        assert result is None

    def test_load_related_memory_rejects_symlink_within_base(self, svc, db_engine):
        """A symlink whose target stays under ``base_dir`` but in ANOTHER
        scope's tree is rejected — the guard validates against the scope
        dir the key legitimately lives in, not the global memory base.

        The victim is created via real ``store()`` so the wiki parser
        accepts it: the containment guard is the only thing that can stop
        the leak.
        """
        ctx = _ctx()
        _run(
            svc.store(
                content="session secret",
                scope="session",
                memory_type="reference",
                key="secret-topic",
                tags="t",
                terminal_context=ctx,
            )
        )
        victim_path = svc.get_wiki_path("session", "test-session", "secret-topic")
        assert victim_path.exists()
        # Sanity: the victim IS loadable through its own scope.
        assert svc._load_related_memory("secret-topic", "session", "test-session") is not None

        link_path = svc.get_wiki_path("global", None, "leak-key")
        link_path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(victim_path), str(link_path))

        assert svc._load_related_memory("leak-key", "global", None) is None

    def test_parse_related_keys_drops_absolute_path_components(self):
        """Absolute path component as key → sanitiser round-trip rejects."""
        out = MemoryService._parse_related_keys("good-key,/etc/passwd", scope="global")
        assert out == ["good-key"]


# ===========================================================================
# Fanout cap / dedup
# ===========================================================================


class TestFanoutCap:
    def test_recall_include_related_not_capped_globally(self, svc, db_engine):
        """``recall(include_related=True)`` is NOT subject to the global
        per-build fanout cap (that applies to context injection only). The
        per-primary parse cap of 3 still applies.
        """
        ctx = _ctx()
        # Seed 3 primaries, each with 3 distinct related keys → 9 candidates.
        related_targets = [f"target-{i}" for i in range(9)]
        for k in related_targets:
            _run(
                svc.store(
                    content=f"target body {k}",
                    scope="global",
                    memory_type="reference",
                    key=k,
                    tags="t",
                    terminal_context=ctx,
                )
            )
        for i in range(3):
            primary = f"primary-{i}"
            _run(
                svc.store(
                    content=f"primary body {i} target",
                    scope="global",
                    memory_type="reference",
                    key=primary,
                    tags="primarytag",
                    terminal_context=ctx,
                )
            )
            picks = ",".join(related_targets[i * 3 : i * 3 + 3])
            _set_related_keys(db_engine, primary, "global", picks)

        results = _run(
            svc.recall(
                query="primary",
                scope="global",
                terminal_context=ctx,
                include_related=True,
                sort_by="recency",
            )
        )
        primaries = [m for m in results if not getattr(m, "is_related", False)]
        related = [m for m in results if getattr(m, "is_related", False)]
        assert len(primaries) >= 3
        assert len(related) > 5, f"got {len(related)} related (cap should not apply)"

    def test_recall_include_related_still_dedups(self, svc, db_engine):
        """Overlapping related → dedup against the visited set."""
        ctx = _ctx()
        _run(
            svc.store(
                content="shared target body",
                scope="global",
                memory_type="reference",
                key="shared-tgt",
                tags="t",
                terminal_context=ctx,
            )
        )
        for i in range(3):
            primary = f"primary-{i}"
            _run(
                svc.store(
                    content=f"primary content {i}",
                    scope="global",
                    memory_type="reference",
                    key=primary,
                    tags="primarytag",
                    terminal_context=ctx,
                )
            )
            _set_related_keys(db_engine, primary, "global", "shared-tgt")

        results = _run(
            svc.recall(
                query="primary",
                scope="global",
                terminal_context=ctx,
                include_related=True,
                sort_by="recency",
            )
        )
        related_keys = [m.key for m in results if getattr(m, "is_related", False)]
        assert related_keys.count("shared-tgt") == 1


# ===========================================================================
# Cycles / depth-1 traversal
# ===========================================================================


class TestCycle:
    def test_cycle_a_b_a_blocked(self, svc, db_engine):
        """A→B, B→A → recall surfaces A and B once, no infinite loop."""
        ctx = _ctx()
        for k in ("cyc-a", "cyc-b"):
            _run(
                svc.store(
                    content=f"body {k}",
                    scope="global",
                    memory_type="reference",
                    key=k,
                    tags="t",
                    terminal_context=ctx,
                )
            )
        _set_related_keys(db_engine, "cyc-a", "global", "cyc-b")
        _set_related_keys(db_engine, "cyc-b", "global", "cyc-a")

        results = _run(
            svc.recall(
                query="body",
                scope="global",
                terminal_context=ctx,
                include_related=True,
                sort_by="recency",
            )
        )
        keys = [m.key for m in results]
        assert keys.count("cyc-a") == 1
        assert keys.count("cyc-b") == 1

    def test_self_reference_blocked_by_visited(self, svc, db_engine):
        """A in its own related_keys → primary A appended once, no
        ``[related] A`` (visited prevents re-add).
        """
        ctx = _ctx()
        _run(
            svc.store(
                content="solo body",
                scope="global",
                memory_type="reference",
                key="solo",
                tags="t",
                terminal_context=ctx,
            )
        )
        _set_related_keys(db_engine, "solo", "global", "solo")
        results = _run(
            svc.recall(
                query="solo",
                scope="global",
                terminal_context=ctx,
                include_related=True,
                sort_by="recency",
            )
        )
        related = [m for m in results if getattr(m, "is_related", False)]
        assert related == []
        assert sum(1 for m in results if m.key == "solo") == 1

    def test_recall_include_related_does_not_recurse(self, svc, db_engine):
        """Primary A → A links B → B links C: result is [A, B], NOT [A, B, C]
        (depth = 1).
        """
        ctx = _ctx()
        for k in ("a", "b", "c"):
            _run(
                svc.store(
                    content=f"chain body {k}",
                    scope="global",
                    memory_type="reference",
                    key=f"chain-{k}",
                    tags="chaintag",
                    terminal_context=ctx,
                )
            )
        _set_related_keys(db_engine, "chain-a", "global", "chain-b")
        _set_related_keys(db_engine, "chain-b", "global", "chain-c")

        results = _run(
            svc.recall(
                query="chain-a",  # narrow to primary A
                scope="global",
                terminal_context=ctx,
                include_related=True,
                sort_by="recency",
                limit=1,
            )
        )
        keys = [m.key for m in results]
        assert "chain-a" in keys
        assert "chain-b" in keys
        assert "chain-c" not in keys, "depth=1 violated; recursion happened"


# ===========================================================================
# Length caps
# ===========================================================================


class TestLengthCap:
    def test_parse_related_keys_truncates_oversized_input(self, caplog):
        """Raw of ~100 KiB → returns ≤ 3 keys, no exception, WARNING with len."""
        raw = ",".join([f"k-{i}" for i in range(20_000)])
        with caplog.at_level(
            logging.WARNING, logger="cli_agent_orchestrator.services.memory_service"
        ):
            out = MemoryService._parse_related_keys(raw, scope="global")
        assert len(out) <= 3
        msgs = [r.getMessage() for r in caplog.records]
        assert any("related_keys_oversized" in m for m in msgs)
        assert any("len=" in m for m in msgs)

    def test_parse_related_keys_oversized_log_no_value_bytes(self, caplog):
        """WARNING log contains scope + length only — never the raw bytes."""
        raw = "EVIL-LITERAL-DO-NOT-LOG," + ("k," * 20_000)
        with caplog.at_level(
            logging.WARNING, logger="cli_agent_orchestrator.services.memory_service"
        ):
            MemoryService._parse_related_keys(raw, scope="project")
        for rec in caplog.records:
            assert "EVIL-LITERAL-DO-NOT-LOG" not in rec.getMessage()

    def test_check_constraint_blocks_oversized_write_on_fresh_db(self, db_engine):
        """Fresh DBs created via ``Base.metadata.create_all`` carry the
        CHECK constraint. A 2 KiB ``related_keys`` cell raises IntegrityError.
        """
        Session = sessionmaker(bind=db_engine)
        with Session() as db:
            row = MemoryMetadataModel(
                key="oversize",
                memory_type="reference",
                scope="global",
                scope_id=None,
                file_path="/tmp/x",
                tags="",
                token_estimate=0,
                related_keys="x" * 2048,
            )
            db.add(row)
            with pytest.raises(IntegrityError):
                db.commit()


# ===========================================================================
# Markdown smuggling in `## See Also`
# ===========================================================================


class TestMarkdownSmuggling:
    def test_see_also_link_text_is_sanitised_key_only(self, svc):
        """``_render_see_also`` produces lines whose link text is the
        sanitised key only (no markdown specials).
        """
        out = svc._render_see_also(["evil-key"], topic_scope="global", topic_scope_id=None)
        assert "evil-key" in out
        for forbidden in ("<", ">", "javascript:", "&lt;", "!["):
            assert forbidden not in out

    def test_see_also_target_matches_strict_regex(self, svc):
        """Every emitted link line matches SEE_ALSO_LINK_RE."""
        out = svc._render_see_also(
            ["a-key", "b-key", "c-key"], topic_scope="project", topic_scope_id="proj-1"
        )
        link_lines = [ln for ln in out.splitlines() if ln.startswith("- [")]
        assert link_lines
        for ln in link_lines:
            assert SEE_ALSO_LINK_RE.match(ln), f"non-canonical: {ln!r}"

    def test_see_also_drops_unsanitisable_key(self, svc, caplog):
        """A key the sanitiser rejects (empty) is skipped with a WARNING."""
        with caplog.at_level(
            logging.WARNING, logger="cli_agent_orchestrator.services.memory_service"
        ):
            out = svc._render_see_also(["ok-key", ""], topic_scope="global", topic_scope_id=None)
        link_lines = [ln for ln in out.splitlines() if ln.startswith("- [")]
        assert len(link_lines) == 1

    def test_render_see_also_idempotent_strips_old_block(self, svc):
        """Strip is idempotent: stripping twice equals stripping once."""
        body = "<!-- id: t -->\n# t\n\nbody.\n\n" "## See Also\n- [old-key](../global/old-key.md)\n"
        once = svc._strip_existing_see_also(body)
        twice = svc._strip_existing_see_also(once)
        assert "## See Also" not in once
        assert once == twice


# ===========================================================================
# Cross-scope leakage
# ===========================================================================


class TestCrossScopeLeakage:
    def test_related_load_silent_on_missing_entry(self, svc, db_engine):
        """Stale related_keys referencing a deleted key → silently skipped."""
        ctx = _ctx()
        _run(
            svc.store(
                content="primary body",
                scope="global",
                memory_type="reference",
                key="orphan-primary",
                tags="t",
                terminal_context=ctx,
            )
        )
        _set_related_keys(db_engine, "orphan-primary", "global", "ghost-key")
        results = _run(
            svc.recall(
                query="orphan",
                scope="global",
                terminal_context=ctx,
                include_related=True,
                sort_by="recency",
            )
        )
        keys = [m.key for m in results]
        assert "orphan-primary" in keys
        assert "ghost-key" not in keys

    def test_related_load_returns_none_for_invalid_key(self, svc):
        """``_load_related_memory`` returns None for a key that fails
        sanitisation — defence in depth even if the DB row is hand-edited.
        """
        out = svc._load_related_memory("../../evil", "global", None)
        assert out is None


# ===========================================================================
# JSON parse hardening in find_related output
# ===========================================================================


class TestJsonParse:
    def test_find_related_rejects_non_json_response(self, stubbed_llm):
        stubbed_llm["response"] = "sure thing!"
        result = _run(find_related("body", candidate_keys=["good"], topic_key="x", timeout_s=1.0))
        assert result.used_llm is False
        assert result.fallback_reason == "llm_error"
        assert result.related_keys == []

    def test_find_related_rejects_markdown_fence_wrap(self, stubbed_llm):
        """Wrapped JSON in a fence → fallback. We do NOT try to be clever."""
        stubbed_llm["response"] = '```json\n["a"]\n```'
        result = _run(find_related("body", candidate_keys=["a"], topic_key="x", timeout_s=1.0))
        assert result.used_llm is False
        assert result.fallback_reason == "llm_error"

    def test_find_related_drops_non_string_elements(self, stubbed_llm, caplog):
        stubbed_llm["response"] = '["good", 42, null, {"x":1}]'
        with caplog.at_level(logging.DEBUG, logger="cli_agent_orchestrator.services.wiki_compiler"):
            result = _run(
                find_related(
                    "body",
                    candidate_keys=["good"],
                    topic_key="x",
                    timeout_s=1.0,
                )
            )
        assert result.related_keys == ["good"]
        assert result.used_llm is True
        # DEBUG log mentions type names, not raw values.
        type_msgs = [
            r.getMessage() for r in caplog.records if "find_related_dropped" in r.getMessage()
        ]
        assert any("type=int" in m for m in type_msgs)
        assert any("type=NoneType" in m for m in type_msgs)
        assert any("type=dict" in m for m in type_msgs)
        for m in type_msgs:
            assert "42" not in m  # raw int value never logged

    def test_find_related_empty_array_is_success(self, stubbed_llm):
        stubbed_llm["response"] = "[]"
        result = _run(find_related("body", candidate_keys=["good"], topic_key="x", timeout_s=1.0))
        assert result.used_llm is True
        assert result.fallback_reason is None
        assert result.related_keys == []


# ===========================================================================
# Log injection from related-load events
# ===========================================================================


class TestLogInjection:
    def test_oversize_warning_no_value_bytes(self, caplog):
        raw = ("EVIL-LITERAL-1," * 200) + ("k," * 20_000)
        with caplog.at_level(
            logging.WARNING, logger="cli_agent_orchestrator.services.memory_service"
        ):
            MemoryService._parse_related_keys(raw, scope="global")
        for rec in caplog.records:
            assert "EVIL-LITERAL-1" not in rec.getMessage()

    def test_dropped_key_debug_log_sanitised(self, stubbed_llm, caplog):
        """LLM returns a key with a newline → dropped, and the log line never
        contains a raw (unescaped) newline inside the key field.
        """
        stubbed_llm["response"] = '["bad\\nkey"]'  # JSON-escaped newline
        with caplog.at_level(logging.DEBUG, logger="cli_agent_orchestrator.services.wiki_compiler"):
            _run(
                find_related(
                    "body",
                    candidate_keys=["good"],
                    topic_key="x",
                    timeout_s=1.0,
                )
            )
        msgs = [r.getMessage() for r in caplog.records]
        for m in msgs:
            assert "bad\nkey" not in m

    def test_no_info_log_includes_related_value(self, svc, db_engine, caplog):
        """Full happy-path traversal → no INFO record contains row content."""
        ctx = _ctx()
        secret = "ULTRA-SECRET-CONTENT-DO-NOT-LOG"
        _run(
            svc.store(
                content="primary body",
                scope="global",
                memory_type="reference",
                key="primary",
                tags="t",
                terminal_context=ctx,
            )
        )
        _run(
            svc.store(
                content=secret,
                scope="global",
                memory_type="reference",
                key="related-target",
                tags="t",
                terminal_context=ctx,
            )
        )
        _set_related_keys(db_engine, "primary", "global", "related-target")
        with caplog.at_level(logging.INFO, logger="cli_agent_orchestrator.services.memory_service"):
            _run(
                svc.recall(
                    query="primary",
                    scope="global",
                    terminal_context=ctx,
                    include_related=True,
                    sort_by="recency",
                )
            )
        for rec in caplog.records:
            if rec.levelno >= logging.INFO:
                assert secret not in rec.getMessage()


# ===========================================================================
# Transient is_related flag (fresh-instance guarantees)
# ===========================================================================


class TestTransientFlagFreshness:
    def test_is_related_default_false_on_primary(self, svc, db_engine):
        """Primary results must have ``is_related == False``."""
        ctx = _ctx()
        _run(
            svc.store(
                content="primary body",
                scope="global",
                memory_type="reference",
                key="prim",
                tags="t",
                terminal_context=ctx,
            )
        )
        results = _run(
            svc.recall(
                query="primary",
                scope="global",
                terminal_context=ctx,
                sort_by="recency",
            )
        )
        for m in results:
            assert getattr(m, "is_related", False) is False

    def test_load_related_memory_returns_fresh_instance(self, svc, db_engine):
        """Two calls with same key → ``id(a) != id(b)`` (no cache)."""
        ctx = _ctx()
        _run(
            svc.store(
                content="body",
                scope="global",
                memory_type="reference",
                key="freshie",
                tags="t",
                terminal_context=ctx,
            )
        )
        a = svc._load_related_memory("freshie", "global", None)
        b = svc._load_related_memory("freshie", "global", None)
        assert a is not None and b is not None
        assert id(a) != id(b), "Memory instance reused across recalls (cache leak)"

    def test_is_related_flag_does_not_leak_across_recalls(self, svc, db_engine):
        """Two consecutive recalls; second recall's primaries must NOT carry
        ``is_related=True`` from the first.
        """
        ctx = _ctx()
        _run(
            svc.store(
                content="primary body",
                scope="global",
                memory_type="reference",
                key="p1",
                tags="t",
                terminal_context=ctx,
            )
        )
        _run(
            svc.store(
                content="related body",
                scope="global",
                memory_type="reference",
                key="r1",
                tags="t",
                terminal_context=ctx,
            )
        )
        _set_related_keys(db_engine, "p1", "global", "r1")
        _run(
            svc.recall(
                query="primary",
                scope="global",
                terminal_context=ctx,
                include_related=True,
                sort_by="recency",
            )
        )
        second = _run(
            svc.recall(
                query="primary",
                scope="global",
                terminal_context=ctx,
                include_related=False,
                sort_by="recency",
            )
        )
        for m in second:
            assert (
                getattr(m, "is_related", False) is False
            ), f"is_related leaked from prior recall onto {m.key}"


# ===========================================================================
# Compile-mode bypass at second pass
# ===========================================================================


class TestCompileModeBypass:
    def test_append_mode_skips_find_related(self, svc, db_engine, monkeypatch):
        """compile_mode=append → find_related call count = 0; SQL stays NULL."""
        monkeypatch.setenv("CAO_MEMORY_COMPILE_MODE", "append")
        called = {"n": 0}

        async def _tracker(*a, **kw):
            called["n"] += 1
            return RelatedResult(related_keys=[], used_llm=True, fallback_reason=None, elapsed_ms=0)

        monkeypatch.setattr(wiki_compiler, "find_related", _tracker)
        ctx = _ctx()

        async def _scenario():
            # First store seeds the topic; second store is the is_update branch.
            await svc.store(
                content="alpha",
                scope="global",
                memory_type="reference",
                key="append-mode",
                tags="t",
                terminal_context=ctx,
            )
            await svc.store(
                content="beta",
                scope="global",
                memory_type="reference",
                key="append-mode",
                tags="t",
                terminal_context=ctx,
            )
            await _drain_compiles(svc)

        _run(_scenario())
        assert called["n"] == 0
        # SQL row stays NULL (never attempted under append mode).
        Session = sessionmaker(bind=db_engine)
        with Session() as db:
            row = db.query(MemoryMetadataModel).filter_by(key="append-mode", scope="global").first()
            assert row is not None
            assert row.related_keys is None

    def test_compile_used_llm_false_skips_find_related(self, svc, db_engine, monkeypatch):
        """compile_mode=llm but compile fell back to append → find_related not
        called; SQL stays NULL.
        """
        monkeypatch.setenv("CAO_MEMORY_COMPILE_MODE", "llm")
        # Force compile to fall back (no CLI available → "disabled").
        monkeypatch.setattr(wiki_compiler, "_build_llm_client", lambda *a, **k: None)

        called = {"n": 0}

        async def _tracker(*a, **kw):
            called["n"] += 1
            return RelatedResult(related_keys=[], used_llm=True, fallback_reason=None, elapsed_ms=0)

        monkeypatch.setattr(wiki_compiler, "find_related", _tracker)
        ctx = _ctx()

        async def _scenario():
            await svc.store(
                content="alpha",
                scope="global",
                memory_type="reference",
                key="fallback-skip",
                tags="t",
                terminal_context=ctx,
            )
            await svc.store(
                content="beta",
                scope="global",
                memory_type="reference",
                key="fallback-skip",
                tags="t",
                terminal_context=ctx,
            )
            await _drain_compiles(svc)

        _run(_scenario())
        assert called["n"] == 0
        Session = sessionmaker(bind=db_engine)
        with Session() as db:
            row = (
                db.query(MemoryMetadataModel).filter_by(key="fallback-skip", scope="global").first()
            )
            assert row is not None
            assert row.related_keys is None

    def test_find_related_no_cli_returns_disabled_fallback(self, monkeypatch):
        """No CLI on PATH → ``RelatedResult(used_llm=False,
        fallback_reason="disabled")`` and no LLM call attempted.
        """
        monkeypatch.setattr(wiki_compiler, "_build_llm_client", lambda *a, **k: None)
        called = {"n": 0}

        async def _boom(*a, **kw):
            called["n"] += 1
            raise AssertionError("LLM call should not be attempted")

        monkeypatch.setattr(wiki_compiler, "_llm_call", _boom)

        result = _run(
            find_related(
                "body",
                candidate_keys=["good"],
                topic_key="x",
                timeout_s=1.0,
            )
        )
        assert result.used_llm is False
        assert result.fallback_reason == "disabled"
        assert called["n"] == 0


# ===========================================================================
# Edge cases (NULL vs "" semantics, strip behaviour)
# ===========================================================================


class TestEdgeCases:
    def test_null_related_keys_returns_empty(self):
        """NULL → []. ``_parse_related_keys`` must short-circuit."""
        assert MemoryService._parse_related_keys(None, scope="global") == []

    def test_empty_string_related_keys_returns_empty(self):
        """``""`` → []. Distinct from NULL semantically (computed-but-empty)
        but both decode to no traversal.
        """
        assert MemoryService._parse_related_keys("", scope="global") == []

    def test_render_see_also_empty_input_returns_empty_string(self, svc):
        """No ``## See Also`` section for empty related_keys."""
        assert svc._render_see_also([], topic_scope="global", topic_scope_id=None) == ""

    def test_strip_see_also_preserves_body(self, svc):
        body = (
            "<!-- id: t -->\n# t\n\nimportant body content.\n\n"
            "## See Also\n- [x](../global/x.md)\n"
        )
        out = svc._strip_existing_see_also(body)
        assert "important body content" in out
        assert "## See Also" not in out
        assert "[x]" not in out

    def test_strip_see_also_preserves_subsequent_section(self, svc):
        """Strip terminates at the next ``## `` heading."""
        body = (
            "<!-- id: t -->\n# t\n\nbody.\n\n"
            "## See Also\n- [x](../global/x.md)\n"
            "\n## Another Section\nsurvives.\n"
        )
        out = svc._strip_existing_see_also(body)
        assert "## Another Section" in out
        assert "survives." in out
        assert "## See Also" not in out
        assert "[x]" not in out


# ===========================================================================
# Critical regression — recall(include_related=False) byte-identical
# ===========================================================================


class TestRecallNoExpansionRegression:
    def test_default_include_related_false(self, svc, db_engine):
        """Default ``include_related=False`` → no related rows appended."""
        ctx = _ctx()
        for k in ("p", "r"):
            _run(
                svc.store(
                    content=f"body {k}",
                    scope="global",
                    memory_type="reference",
                    key=f"reg-{k}",
                    tags="t",
                    terminal_context=ctx,
                )
            )
        _set_related_keys(db_engine, "reg-p", "global", "reg-r")
        results = _run(svc.recall(query="body", scope="global", terminal_context=ctx))
        assert all(getattr(m, "is_related", False) is False for m in results)
        keys = {m.key for m in results}
        assert keys == {"reg-p", "reg-r"}

    def test_include_related_off_byte_identical_under_recency(self, svc, db_engine):
        """Two recalls with identical inputs and ``include_related=False`` must
        produce identical key sequences (no extras, no reorder).
        """
        ctx = _ctx()
        for k in ("a", "b", "c"):
            _run(
                svc.store(
                    content=f"body {k}",
                    scope="global",
                    memory_type="reference",
                    key=f"reg2-{k}",
                    tags="t",
                    terminal_context=ctx,
                )
            )
        _set_related_keys(db_engine, "reg2-a", "global", "reg2-b")

        first = [
            m.key
            for m in _run(
                svc.recall(
                    query="body",
                    scope="global",
                    terminal_context=ctx,
                    sort_by="recency",
                )
            )
        ]
        second = [
            m.key
            for m in _run(
                svc.recall(
                    query="body",
                    scope="global",
                    terminal_context=ctx,
                    sort_by="recency",
                )
            )
        ]
        assert first == second
