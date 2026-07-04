"""Tests for the OKF export backend (#345 Unit 2 — design tests 1/2/3/4 export side).

Fixtures are built by calling ``store()`` into a tmp ``base_dir`` — the
exporter is exercised against real wiki files, index.md, and SQLite
metadata, never hand-written store internals (except the See-Also block,
which only the compile pipeline writes and is appended file-side here).
"""

import asyncio
import os
import tarfile
from datetime import datetime

import frontmatter
import pytest
from sqlalchemy import create_engine

from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services.memory_archive import get_backend
from cli_agent_orchestrator.services.memory_archive.okf import (
    OkfArchiveBackend,
    export_bundle_to_tar,
)
from cli_agent_orchestrator.services.memory_service import MemoryService

PLANTED_AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def svc(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return MemoryService(base_dir=tmp_path / "memory", db_engine=engine)


@pytest.fixture
def backend(svc):
    return OkfArchiveBackend(svc)


@pytest.fixture
def dest(tmp_path):
    return tmp_path / "bundle"


def _store(svc, key, content, scope="global", memory_type="reference", tags="", ctx=None):
    return _run(
        svc.store(
            content=content,
            scope=scope,
            memory_type=memory_type,
            key=key,
            tags=tags,
            terminal_context=ctx,
        )
    )


def _frontmatter(path):
    # Parsed with python-frontmatter (already a declared dependency) rather
    # than importing yaml, which the project only pulls in transitively.
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.name}: missing frontmatter open"
    post = frontmatter.loads(text)
    return post.metadata, post.content


def _bundle_files(dest):
    return sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*.md"))


class TestRegistration:
    def test_okf_registered(self):
        assert get_backend("okf") is OkfArchiveBackend


class TestConformance:
    """Design test 1, export side: OKF §9 — every non-reserved *.md is a topic."""

    def test_every_non_reserved_md_has_frontmatter_with_type(self, svc, backend, dest):
        _store(svc, "topic-one", "First topic. More detail here.", memory_type="reference")
        _store(svc, "topic-two", "Second topic body.", memory_type="project", tags="ci,deploy")
        report = backend.export_bundle("global", None, dest, False, False)
        assert report.exported == 2

        non_reserved = [
            p
            for p in dest.rglob("*.md")
            if p.name not in ("index.md", "manifest.md")
            and "history" not in p.relative_to(dest).parts
        ]
        assert len(non_reserved) == 2
        for path in non_reserved:
            fm, body = _frontmatter(path)
            assert fm["type"], f"{path.name}: empty type"
            assert fm["title"] == path.stem
            # H1 kept at body top per the mapping table.
            assert body.lstrip("\n").startswith(f"# {path.stem}")

    def test_frontmatter_fixed_key_order(self, svc, backend, dest):
        _store(svc, "ordered", "A sentence. Another.", memory_type="user", tags="a,b")
        backend.export_bundle("global", None, dest, False, False)
        lines = (dest / "ordered.md").read_text(encoding="utf-8").splitlines()
        keys = [ln.split(":")[0] for ln in lines[1 : lines.index("---", 1)]]
        assert keys == ["type", "title", "description", "tags", "timestamp", "created"]

    def test_tags_and_description(self, svc, backend, dest):
        _store(svc, "tagged", "Short fact. Extra prose.", tags="alpha,beta")
        backend.export_bundle("global", None, dest, False, False)
        fm, _ = _frontmatter(dest / "tagged.md")
        assert fm["tags"] == ["alpha", "beta"]
        assert fm["description"] == "Short fact."

    def test_special_char_tags_are_yaml_escaped(self, svc, backend, dest):
        # Quotes, colons, '#', and leading-special chars must not break the
        # frontmatter flow sequence or inject keys.
        special_tags = ['qu"ote', "co:lon", "#lead", "-dash"]
        _store(svc, "spiky", "Body sentence.", tags=",".join(special_tags))
        backend.export_bundle("global", None, dest, False, False)
        fm, _ = _frontmatter(dest / "spiky.md")  # asserts frontmatter still parses
        assert fm["tags"] == special_tags  # round-trips exact values
        assert set(fm.keys()) == {"type", "title", "description", "tags", "timestamp", "created"}

    def test_multi_entry_topic_timestamps(self, svc, backend, dest):
        _run(
            svc.store(
                content="first entry",
                scope="global",
                memory_type="reference",
                key="aged",
                occurred_at=datetime(2024, 3, 1, 10, 0, 0),
            )
        )
        _run(
            svc.store(
                content="second entry",
                scope="global",
                memory_type="reference",
                key="aged",
                occurred_at=datetime(2025, 6, 2, 11, 30, 0),
            )
        )
        backend.export_bundle("global", None, dest, False, False)
        lines = (dest / "aged.md").read_text(encoding="utf-8").splitlines()
        # created == first section ts, timestamp == latest section ts, values differ.
        assert "created: 2024-03-01T10:00:00Z" in lines
        assert "timestamp: 2025-06-02T11:30:00Z" in lines

    def test_index_and_manifest_have_no_frontmatter(self, svc, backend, dest):
        _store(svc, "topic-one", "Body.")
        backend.export_bundle("global", None, dest, False, False)
        index = (dest / "index.md").read_text(encoding="utf-8")
        assert not index.startswith("---")
        assert "* [topic-one](topic-one.md)" in index
        manifest = (dest / "manifest.md").read_text(encoding="utf-8")
        assert not manifest.startswith("---")
        assert "edits here are not synced back" in manifest


class TestSecretGate:
    """Design test 2: skip by default, redact opt-in, pattern names only."""

    def test_default_skips_topic_with_pattern_name_only(self, svc, backend, dest, caplog):
        _store(svc, "clean-topic", "Nothing sensitive here.")
        _store(svc, "leaky-topic", f"My key is {PLANTED_AWS_KEY} do not share.")
        with caplog.at_level("DEBUG"):
            report = backend.export_bundle("global", None, dest, False, False)
        assert report.exported == 1
        assert report.skipped_secret == 1
        assert report.skip_reasons == {"leaky-topic": ["aws_access_key"]}
        assert not (dest / "leaky-topic.md").exists()
        # NEVER content bytes in report or logs.
        assert PLANTED_AWS_KEY not in repr(report)
        assert PLANTED_AWS_KEY not in caplog.text

    def test_redact_exports_with_marker(self, svc, backend, dest):
        _store(svc, "leaky-topic", f"My key is {PLANTED_AWS_KEY} do not share.")
        report = backend.export_bundle("global", None, dest, False, True)
        assert report.redacted == 1
        assert report.skipped_secret == 0
        text = (dest / "leaky-topic.md").read_text(encoding="utf-8")
        assert PLANTED_AWS_KEY not in text
        assert "[REDACTED:aws_access_key]" in text

    def test_skipped_topic_absent_from_index(self, svc, backend, dest):
        _store(svc, "leaky-topic", f"key {PLANTED_AWS_KEY}")
        _store(svc, "clean-topic", "fine")
        backend.export_bundle("global", None, dest, False, False)
        index = (dest / "index.md").read_text(encoding="utf-8")
        assert "leaky-topic" not in index
        assert "clean-topic" in index

    def test_history_sections_gated_when_included(self, svc, backend, dest):
        _store(svc, "old-leak", f"old entry with {PLANTED_AWS_KEY}")
        _store(svc, "old-leak", "latest entry is clean")
        # Without history the latest-only content is clean → exports.
        report = backend.export_bundle("global", None, dest, False, False)
        assert report.exported == 1
        # With history the older leaky section trips the gate → skipped.
        dest2 = dest.parent / "bundle2"
        report2 = backend.export_bundle("global", None, dest2, True, False)
        assert report2.skipped_secret == 1
        assert report2.skip_reasons["old-leak"] == ["aws_access_key"]


class TestScopeLayout:
    """Design test 3, layout half: session/agent nest under scope_id."""

    def test_session_topics_nest_per_scope_id(self, svc, backend, dest):
        _store(svc, "note", "session one note", scope="session", ctx={"session_name": "sess-one"})
        _store(svc, "note", "session two note", scope="session", ctx={"session_name": "sess-two"})
        report = backend.export_bundle("session", None, dest, False, False)
        assert report.exported == 2
        assert (dest / "sess-one" / "note.md").exists()
        assert (dest / "sess-two" / "note.md").exists()
        index = (dest / "index.md").read_text(encoding="utf-8")
        assert "* [note](sess-one/note.md)" in index
        assert "* [note](sess-two/note.md)" in index

    def test_session_scope_id_filter(self, svc, backend, dest):
        _store(svc, "note", "one", scope="session", ctx={"session_name": "sess-one"})
        _store(svc, "note", "two", scope="session", ctx={"session_name": "sess-two"})
        report = backend.export_bundle("session", "sess-one", dest, False, False)
        assert report.exported == 1
        assert (dest / "sess-one" / "note.md").exists()
        assert not (dest / "sess-two").exists()

    def test_global_scope_is_flat(self, svc, backend, dest):
        _store(svc, "flat-topic", "body")
        backend.export_bundle("global", None, dest, False, False)
        assert (dest / "flat-topic.md").exists()

    def test_export_is_strictly_per_scope(self, svc, backend, dest):
        # Session topics share the global container index — a global export
        # must not sweep them in.
        _store(svc, "global-topic", "global body")
        _store(svc, "session-topic", "session body", scope="session", ctx={"session_name": "s1"})
        report = backend.export_bundle("global", None, dest, False, False)
        assert report.exported == 1
        assert _bundle_files(dest) == ["global-topic.md", "index.md", "manifest.md"]

    def test_history_mirrors_nesting(self, svc, backend, dest):
        ctx = {"session_name": "sess-one"}
        _store(svc, "note", "first entry", scope="session", ctx=ctx)
        _store(svc, "note", "second entry", scope="session", ctx=ctx)
        backend.export_bundle("session", None, dest, True, False)
        history = dest / "history" / "sess-one" / "note.md"
        assert history.exists()
        text = history.read_text(encoding="utf-8")
        assert "first entry" in text
        assert not text.startswith("---")  # verbatim, no frontmatter


class TestIdempotencyAndPrune:
    """Design test 4: re-export rewrites zero files; prune removes exactly the deleted topic."""

    def test_second_export_rewrites_zero_files(self, svc, backend, dest):
        _store(svc, "topic-one", "Body one.")
        _store(svc, "topic-two", "Body two.")
        first = backend.export_bundle("global", None, dest, False, False)
        assert first.exported == 2
        before = {p: os.stat(p).st_mtime_ns for p in dest.rglob("*.md")}
        second = backend.export_bundle("global", None, dest, False, False)
        assert second.exported == 0
        assert second.unchanged == 2
        after = {p: os.stat(p).st_mtime_ns for p in dest.rglob("*.md")}
        assert before == after

    def test_prune_removes_exactly_the_deleted_topic(self, svc, backend, dest):
        _store(svc, "keep-me", "stays")
        _store(svc, "drop-me", "goes")
        backend.export_bundle("global", None, dest, True, False)
        _run(svc.forget("drop-me", scope="global"))
        report = backend.export_bundle("global", None, dest, True, False, prune=True)
        assert report.pruned == 1
        assert not (dest / "drop-me.md").exists()
        assert (dest / "keep-me.md").exists()
        assert (dest / "index.md").exists()
        assert (dest / "manifest.md").exists()

    def test_prune_removes_history_with_its_topic(self, svc, backend, dest):
        _store(svc, "drop-me", "v1")
        _store(svc, "drop-me", "v2")
        backend.export_bundle("global", None, dest, True, False)
        assert (dest / "history" / "drop-me.md").exists()
        _run(svc.forget("drop-me", scope="global"))
        report = backend.export_bundle("global", None, dest, True, False, prune=True)
        assert report.pruned == 2  # topic + its history file
        assert not (dest / "drop-me.md").exists()
        assert not (dest / "history" / "drop-me.md").exists()

    def test_prune_keeps_history_of_still_exported_topic(self, svc, backend, dest):
        # Locks the documented deviation: a prune run without
        # include_history keeps history/<key>.md for a surviving topic.
        _store(svc, "keep-me", "v1")
        _store(svc, "keep-me", "v2")
        backend.export_bundle("global", None, dest, True, False)
        assert (dest / "history" / "keep-me.md").exists()
        report = backend.export_bundle("global", None, dest, False, False, prune=True)
        assert report.pruned == 0
        assert (dest / "history" / "keep-me.md").exists()

    def test_prune_default_off(self, svc, backend, dest):
        _store(svc, "drop-me", "goes")
        backend.export_bundle("global", None, dest, False, False)
        _run(svc.forget("drop-me", scope="global"))
        report = backend.export_bundle("global", None, dest, False, False)
        assert report.pruned == 0
        assert (dest / "drop-me.md").exists()


class TestEmptyScope:
    def test_empty_scope_yields_valid_bundle(self, backend, dest):
        report = backend.export_bundle("global", None, dest, False, False)
        assert report.exported == 0
        assert _bundle_files(dest) == ["index.md", "manifest.md"]


class TestSeeAlso:
    def _append_see_also(self, svc, key, target):
        path = svc.get_wiki_path("global", None, key)
        text = path.read_text(encoding="utf-8").rstrip("\n")
        path.write_text(
            text + f"\n\n## See Also\n- [{target}](../global/{target}.md)\n", encoding="utf-8"
        )

    def test_link_normalized_when_target_in_bundle(self, svc, backend, dest):
        _store(svc, "source-topic", "links out")
        _store(svc, "target-topic", "linked to")
        self._append_see_also(svc, "source-topic", "target-topic")
        report = backend.export_bundle("global", None, dest, False, False)
        assert report.links_dropped == 0
        text = (dest / "source-topic.md").read_text(encoding="utf-8")
        assert "- [target-topic](target-topic.md)" in text
        assert "../global/" not in text

    def test_link_to_skipped_topic_degrades_to_text(self, svc, backend, dest):
        _store(svc, "source-topic", "links out")
        _store(svc, "secret-topic", f"key {PLANTED_AWS_KEY}")
        self._append_see_also(svc, "source-topic", "secret-topic")
        report = backend.export_bundle("global", None, dest, False, False)
        assert report.links_dropped == 1
        text = (dest / "source-topic.md").read_text(encoding="utf-8")
        assert "- secret-topic" in text
        assert "](" not in text.rsplit("## See Also", 1)[-1]

    def test_see_also_only_section_yields_no_description(self, svc, backend, dest):
        # A topic whose latest section is only a See-Also block must not
        # leak raw ../<scope_id>/ paths into frontmatter or index.md.
        _store(svc, "target-topic", "linked to")
        _store(svc, "link-only", "placeholder")
        path = svc.get_wiki_path("global", None, "link-only")
        text = path.read_text(encoding="utf-8")
        text = text.replace(
            "placeholder", "## See Also\n- [target-topic](../global/target-topic.md)"
        )
        path.write_text(text, encoding="utf-8")
        backend.export_bundle("global", None, dest, False, False)
        fm, _ = _frontmatter(dest / "link-only.md")
        assert "description" not in fm
        index = (dest / "index.md").read_text(encoding="utf-8")
        assert "../" not in index


class TestTamperedIndex:
    def test_tampered_index_entry_cannot_escape_dest(self, svc, backend, dest, caplog):
        # A tampered index.md (scope_id "..", traversal key) must not steer
        # writes above dest — the entry is skipped, key-only logged.
        _store(svc, "note", "session body", scope="session", ctx={"session_name": "sess-one"})
        index_path = svc.get_index_path("session", "sess-one")
        text = index_path.read_text(encoding="utf-8")
        tampered = text.replace("session/sess-one/note.md", "session/../escape.md").replace(
            "[note]", "[escape]"
        )
        assert tampered != text
        index_path.write_text(tampered, encoding="utf-8")

        with caplog.at_level("WARNING"):
            report = backend.export_bundle("session", None, dest, False, False)
        assert report.exported == 0
        assert "okf_export_unsafe_index_entry" in caplog.text
        # Nothing written outside dest, and no topic files inside it.
        assert not (dest.parent / "escape.md").exists()
        assert _bundle_files(dest) == ["index.md", "manifest.md"]


class TestDestValidation:
    def test_blocked_system_dest_rejected(self, backend):
        from pathlib import Path

        with pytest.raises(ValueError, match="not allowed"):
            backend.export_bundle("global", None, Path("/etc/new-bundle"), False, False)

    def test_unknown_scope_rejected(self, backend, dest):
        with pytest.raises(ValueError):
            backend.export_bundle("nope", None, dest, False, False)

    def test_project_scope_requires_scope_id(self, backend, dest):
        with pytest.raises(ValueError, match="scope_id"):
            backend.export_bundle("project", None, dest, False, False)


class TestTarHelper:
    def test_tar_output_contains_bundle(self, svc, backend, tmp_path):
        _store(svc, "topic-one", "Body one.")
        tar_path = tmp_path / "out.tar.gz"
        report = export_bundle_to_tar(backend, "global", None, tar_path)
        assert report.exported == 1
        assert tar_path.exists()
        with tarfile.open(tar_path, "r:gz") as tar:
            names = sorted(tar.getnames())
        assert names == ["index.md", "manifest.md", "topic-one.md"]

    def test_tar_target_must_not_be_directory(self, backend, tmp_path):
        with pytest.raises(ValueError, match="directory"):
            export_bundle_to_tar(backend, "global", None, tmp_path)

    def test_tar_output_is_deterministic(self, svc, backend, tmp_path):
        # Sorted members, zeroed uid/gid/mtime, gzip mtime=0 → exporting the
        # same bundle twice yields byte-identical archives.
        import hashlib

        _store(svc, "topic-one", "Body one.")
        _store(svc, "topic-two", "Body two.")
        digests = []
        # Same basename both times: gzip embeds the target's basename in
        # its header, so determinism is per target name.
        for subdir in ("first", "second"):
            tar_path = tmp_path / subdir / "out.tar.gz"
            tar_path.parent.mkdir()
            export_bundle_to_tar(backend, "global", None, tar_path)
            digests.append(hashlib.sha256(tar_path.read_bytes()).hexdigest())
        assert digests[0] == digests[1]


class TestImportImplemented:
    # Replaces Unit 2's NotImplementedError assertion: import_bundle now
    # ships (Unit 3); full coverage lives in test_memory_archive_okf_import.py.
    def test_import_bundle_runs_on_empty_dir(self, backend, tmp_path):
        report = backend.import_bundle(tmp_path, "global", "skip", False)
        assert report.imported == 0
        assert report.rejected == 0
        assert report.target_scope == "global"
