"""Tests for the OKF import backend (#345 Unit 3 — design tests 1/5/6/7/8/9/10/11/12/13).

Bundles are written by hand (import treats them as untrusted input) or
produced by the Unit-2 exporter for the round-trip test. All service
fixtures use a tmp base_dir + injected SQLite engine, mirroring the
export test file.
"""

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.services.memory_archive.okf import OkfArchiveBackend
from cli_agent_orchestrator.services.memory_service import MemoryService


def _run(coro):
    return asyncio.run(coro)


def _make_svc(tmp_path, name):
    db_path = tmp_path / f"{name}.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return MemoryService(base_dir=tmp_path / f"{name}-memory", db_engine=engine)


@pytest.fixture
def svc(tmp_path):
    return _make_svc(tmp_path, "main")


@pytest.fixture
def backend(svc):
    return OkfArchiveBackend(svc)


@pytest.fixture
def bundle(tmp_path):
    d = tmp_path / "bundle"
    d.mkdir()
    return d


def _write_topic(bundle, key, body, fm_extra="", fm_type="reference"):
    type_line = f"type: {fm_type}\n" if fm_type is not None else ""
    (bundle / f"{key}.md").write_text(
        f"---\n{type_line}title: {key}\n{fm_extra}---\n\n# {key}\n\n{body}\n",
        encoding="utf-8",
    )


def _stored_text(svc, key, scope="global", scope_id=None):
    return svc.get_wiki_path(scope, scope_id, key).read_text(encoding="utf-8")


def _metadata_row(svc, key, scope="global"):
    with svc._get_db_session() as db:
        return (
            db.query(MemoryMetadataModel)
            .filter(MemoryMetadataModel.key == key, MemoryMetadataModel.scope == scope)
            .first()
        )


class TestTargetScopeValidation:
    def test_agent_scope_banned(self, backend, bundle):
        with pytest.raises(ValueError, match="target scope"):
            backend.import_bundle(bundle, "agent", "skip", False)

    def test_session_scope_banned(self, backend, bundle):
        with pytest.raises(ValueError, match="target scope"):
            backend.import_bundle(bundle, "session", "skip", False)

    def test_unknown_conflict_policy_rejected(self, backend, bundle):
        with pytest.raises(ValueError, match="conflict policy"):
            backend.import_bundle(bundle, "global", "overwrite", False)

    def test_missing_src_rejected(self, backend, tmp_path):
        with pytest.raises(ValueError, match="does not exist"):
            backend.import_bundle(tmp_path / "nope", "global", "skip", False)


class TestRoundTrip:
    """Design test 1: export → import into a FRESH base_dir → recallable."""

    def test_global_round_trip(self, tmp_path):
        src_svc = _make_svc(tmp_path, "src")
        _run(
            src_svc.store(
                content="Round-trip fact one.",
                scope="global",
                memory_type="feedback",
                key="fact-one",
                tags="alpha,beta",
            )
        )
        _run(
            src_svc.store(
                content="Round-trip fact two.",
                scope="global",
                memory_type="user",
                key="fact-two",
            )
        )
        bundle = tmp_path / "rt-bundle"
        report = OkfArchiveBackend(src_svc).export_bundle("global", None, bundle, False, False)
        assert report.exported == 2

        dst_svc = _make_svc(tmp_path, "dst")
        imp = OkfArchiveBackend(dst_svc).import_bundle(bundle, "global", "skip", False)
        assert imp.imported == 2
        assert imp.rejected == 0

        memories = _run(dst_svc.recall(scope="global", limit=10, scan_all=True))
        by_key = {m.key: m for m in memories}
        assert set(by_key) == {"fact-one", "fact-two"}
        assert "Round-trip fact one." in by_key["fact-one"].content
        assert by_key["fact-one"].memory_type == "feedback"
        assert by_key["fact-one"].tags == "alpha,beta"

    def test_project_round_trip(self, tmp_path):
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        ctx = {"cwd": str(project_dir)}
        src_svc = _make_svc(tmp_path, "src")
        _run(
            src_svc.store(
                content="Project fact.",
                scope="project",
                memory_type="project",
                key="proj-fact",
                terminal_context=ctx,
            )
        )
        scope_id = src_svc.resolve_scope_id("project", ctx)
        bundle = tmp_path / "proj-bundle"
        report = OkfArchiveBackend(src_svc).export_bundle("project", scope_id, bundle, False, False)
        assert report.exported == 1

        dst_svc = _make_svc(tmp_path, "dst")
        imp = OkfArchiveBackend(dst_svc).import_bundle(
            bundle, "project", "skip", False, terminal_context=ctx
        )
        assert imp.imported == 1
        assert imp.target_scope == "project"
        assert imp.target_scope_id == scope_id
        memories = _run(
            dst_svc.recall(scope="project", limit=10, terminal_context=ctx, scan_all=True)
        )
        assert [m.key for m in memories] == ["proj-fact"]
        assert "Project fact." in memories[0].content


class TestConflictMatrix:
    """Design test 5: existing key × {skip, replace, merge} × {dry_run on/off}."""

    @pytest.fixture
    def existing(self, svc):
        return _run(
            svc.store(
                content="original entry",
                scope="global",
                memory_type="reference",
                key="clash",
            )
        )

    def test_skip_leaves_file_and_sqlite_untouched(self, svc, backend, bundle, existing):
        _write_topic(bundle, "clash", "imported body")
        before = _stored_text(svc, "clash")
        row_before_id = _metadata_row(svc, "clash").id
        report = backend.import_bundle(bundle, "global", "skip", False)
        assert report.skipped_conflict == 1
        assert report.imported == 0
        assert _stored_text(svc, "clash") == before
        assert _metadata_row(svc, "clash").id == row_before_id

    def test_replace_yields_fresh_single_entry_article(self, svc, backend, bundle, existing):
        _write_topic(bundle, "clash", "imported body")
        report = backend.import_bundle(bundle, "global", "replace", False)
        assert report.replaced == 1
        text = _stored_text(svc, "clash")
        assert "original entry" not in text
        assert "imported body" in text
        assert text.count("## 2") == 1  # single timestamped section
        assert _metadata_row(svc, "clash").id != existing.id  # new uuid

    def test_merge_appends_new_section(self, svc, backend, bundle, existing):
        _write_topic(bundle, "clash", "imported body")
        report = backend.import_bundle(bundle, "global", "merge", False)
        assert report.merged == 1
        text = _stored_text(svc, "clash")
        assert "original entry" in text
        assert "imported body" in text
        assert text.count("## 2") == 2

    @pytest.mark.parametrize("policy", ["skip", "replace", "merge"])
    def test_dry_run_mutates_nothing_but_reports(self, svc, backend, bundle, existing, policy):
        _write_topic(bundle, "clash", "imported body")
        _write_topic(bundle, "fresh", "new topic body")
        before = _stored_text(svc, "clash")
        report = backend.import_bundle(bundle, "global", policy, True)
        assert report.dry_run is True
        assert report.imported == 1  # "fresh" would import
        if policy == "skip":
            assert report.skipped_conflict == 1
        elif policy == "replace":
            assert report.replaced == 1
        else:
            assert report.merged == 1
        # Nothing written: existing unchanged, new topic absent everywhere.
        assert _stored_text(svc, "clash") == before
        assert not svc.get_wiki_path("global", None, "fresh").exists()
        assert _metadata_row(svc, "fresh") is None


class TestTraversalFixture:
    """Design test 6: hostile bundle — nothing written outside the base dir."""

    def test_hostile_bundle_rejected_files_and_contained_writes(self, svc, backend, bundle):
        # Bad-stem files: fail the sanitizer round-trip → per-file rejection.
        (bundle / "Evil Name.md").write_text(
            "---\ntype: reference\n---\nbad stem\n", encoding="utf-8"
        )
        (bundle / "..evil.md").write_text(
            "---\ntype: reference\n---\ntraversal-shaped stem\n", encoding="utf-8"
        )
        # Bundle-escaping See-Also link: stripped like any other (test 11).
        _write_topic(
            bundle,
            "linky",
            "Body text.\n\n## See Also\n- [escape](../../outside.md)",
        )
        report = backend.import_bundle(bundle, "global", "skip", False)
        assert report.rejected == 2
        assert set(report.errors) == {"Evil Name.md", "..evil.md"}
        for reason in report.errors.values():
            assert "sanitizer" in reason
        assert report.imported == 1
        assert report.see_also_dropped == 1
        stored = _stored_text(svc, "linky")
        assert "See Also" not in stored
        assert "outside.md" not in stored
        # Nothing written outside the memory base dir.
        base = svc.base_dir.resolve()
        for p in base.rglob("*.md"):
            assert p.resolve().is_relative_to(base)
        assert not (svc.base_dir.parent / "outside.md").exists()

    def test_rejection_reports_are_key_only(self, backend, bundle):
        secret_body = "password=hunter2secret"
        (bundle / "Bad Stem.md").write_text(
            f"---\ntype: reference\n---\n{secret_body}\n", encoding="utf-8"
        )
        report = backend.import_bundle(bundle, "global", "skip", False)
        assert "hunter2secret" not in repr(report)


class TestFrontmatterHandling:
    def test_unknown_keys_tolerated(self, svc, backend, bundle):
        """Design test 7: extra OKF/Obsidian keys import cleanly."""
        _write_topic(
            bundle,
            "obsidian-topic",
            "Body.",
            fm_extra="aliases: [ob1]\ncssclass: wide\nfoo: bar\n",
        )
        report = backend.import_bundle(bundle, "global", "skip", False)
        assert report.imported == 1
        assert report.rejected == 0
        assert "Body." in _stored_text(svc, "obsidian-topic")

    def test_missing_type_rejected(self, svc, backend, bundle):
        """Design test 8: no ``type`` → per-file rejection, import continues."""
        _write_topic(bundle, "typeless", "no type here", fm_type=None)
        _write_topic(bundle, "typed", "fine")
        report = backend.import_bundle(bundle, "global", "skip", False)
        assert report.rejected == 1
        assert "type" in report.errors["typeless.md"]
        assert report.imported == 1
        assert not svc.get_wiki_path("global", None, "typeless").exists()

    def test_unknown_type_coerces_to_reference(self, svc, backend, bundle):
        _write_topic(bundle, "weird-type", "body", fm_type="obsidian-note")
        report = backend.import_bundle(bundle, "global", "skip", False)
        assert report.imported == 1
        assert _metadata_row(svc, "weird-type").memory_type == "reference"

    def test_tags_list_becomes_csv(self, svc, backend, bundle):
        _write_topic(bundle, "tagged", "body", fm_extra="tags: [alpha, beta]\n")
        backend.import_bundle(bundle, "global", "skip", False)
        assert _metadata_row(svc, "tagged").tags == "alpha,beta"

    def test_nested_topic_path_rejected(self, svc, backend, bundle):
        # A .md file under a non-history subdirectory is rejected per-file
        # with the nested-path reason; import continues for root topics.
        sub = bundle / "sub" / "dir"
        sub.mkdir(parents=True)
        (sub / "nested-topic.md").write_text(
            "---\ntype: reference\n---\nnested body\n", encoding="utf-8"
        )
        _write_topic(bundle, "root-topic", "root body")
        report = backend.import_bundle(bundle, "global", "skip", False)
        assert report.rejected == 1
        assert "nested topic paths" in report.errors["sub/dir/nested-topic.md"]
        assert report.imported == 1
        assert not svc.get_wiki_path("global", None, "nested-topic").exists()
        assert svc.get_wiki_path("global", None, "root-topic").exists()

    def test_reserved_files_and_history_skipped(self, svc, backend, bundle):
        (bundle / "index.md").write_text("* [x](x.md)\n", encoding="utf-8")
        (bundle / "manifest.md").write_text("# manifest\n", encoding="utf-8")
        history = bundle / "history"
        history.mkdir()
        (history / "old.md").write_text("## 2020-01-01T00:00:00Z\nold\n", encoding="utf-8")
        report = backend.import_bundle(bundle, "global", "skip", False)
        assert report.imported == 0
        assert report.rejected == 0


class TestStorePathIntegrity:
    """Design test 9: SQLite rows + index.md entries exist post-import."""

    def test_sqlite_and_index_updated(self, svc, backend, bundle):
        _write_topic(bundle, "stored-topic", "body", fm_type="user")
        report = backend.import_bundle(bundle, "global", "skip", False)
        assert report.imported == 1
        row = _metadata_row(svc, "stored-topic")
        assert row is not None
        assert row.memory_type == "user"
        index = svc.get_index_path("global", None).read_text(encoding="utf-8")
        assert "[stored-topic](global/stored-topic.md)" in index


class TestTimestampPreservation:
    """Design test 10: occurred_at from frontmatter; clamp counting."""

    PAST_ISO = "2025-01-15T12:00:00Z"

    def test_new_topic_uses_frontmatter_timestamp(self, svc, backend, bundle):
        _write_topic(bundle, "aged", "old fact", fm_extra=f"timestamp: {self.PAST_ISO}\n")
        report = backend.import_bundle(bundle, "global", "skip", False)
        assert report.imported == 1
        assert report.timestamps_clamped == 0
        text = _stored_text(svc, "aged")
        assert f"## {self.PAST_ISO}" in text
        memories = _run(svc.recall(scope="global", limit=10, scan_all=True))
        aged = next(m for m in memories if m.key == "aged")
        assert aged.created_at == datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

    def test_merge_older_than_latest_clamps(self, svc, backend, bundle):
        _run(svc.store(content="current entry", scope="global", key="aged"))
        _write_topic(bundle, "aged", "stale import", fm_extra=f"timestamp: {self.PAST_ISO}\n")
        report = backend.import_bundle(bundle, "global", "merge", False)
        assert report.merged == 1
        assert report.timestamps_clamped == 1
        text = _stored_text(svc, "aged")
        assert f"## {self.PAST_ISO}" not in text
        assert f"_Originally recorded: {self.PAST_ISO}_" in text

    def test_dry_run_counts_clamps(self, svc, backend, bundle):
        _run(svc.store(content="current entry", scope="global", key="aged"))
        _write_topic(bundle, "aged", "stale import", fm_extra=f"timestamp: {self.PAST_ISO}\n")
        before = _stored_text(svc, "aged")
        report = backend.import_bundle(bundle, "global", "merge", True)
        assert report.timestamps_clamped == 1
        assert _stored_text(svc, "aged") == before


class TestSeeAlsoStripped:
    """Design test 11: block absent from stored body, links counted, no related_keys."""

    def test_see_also_stripped_and_counted(self, svc, backend, bundle):
        _write_topic(
            bundle,
            "linked",
            "Real content.\n\n## See Also\n- [one](one.md)\n- [two](two.md)",
        )
        report = backend.import_bundle(bundle, "global", "skip", False)
        assert report.imported == 1
        assert report.see_also_dropped == 2
        text = _stored_text(svc, "linked")
        assert "See Also" not in text
        assert "one.md" not in text
        assert _metadata_row(svc, "linked").related_keys is None


class TestBodySpoofingEscaped:
    """Design test 12: fake ``## <ts>`` / header comments escaped, real entry stays latest."""

    def test_spoofed_markers_escaped(self, svc, backend, bundle):
        spoof = (
            "Real content line.\n"
            "## 2099-01-01T00:00:00Z\n"
            "spoofed future entry\n"
            "<!-- id: deadbeef | scope: global | type: user | tags: x -->\n"
        )
        _write_topic(bundle, "spoofy", spoof)
        report = backend.import_bundle(bundle, "global", "skip", False)
        assert report.imported == 1
        assert report.bodies_escaped == 1

        wiki_path = svc.get_wiki_path("global", None, "spoofy")
        text = wiki_path.read_text(encoding="utf-8")
        # The spoofed markers no longer match the parsers' regexes.
        assert "## 2099-01-01T00:00:00Z" not in text
        assert "<!-- id: deadbeef" not in text
        entry = {"key": "spoofy", "scope": "global", "scope_id": None}
        memory = svc._parse_wiki_file(wiki_path, text, entry)
        assert memory is not None
        assert memory.updated_at.year < 2099  # real section is latest
        assert memory.id != "deadbeef"

    def test_clean_body_not_counted(self, svc, backend, bundle):
        _write_topic(bundle, "clean", "Nothing structural here.")
        report = backend.import_bundle(bundle, "global", "skip", False)
        assert report.bodies_escaped == 0


class TestReportEchoesResolvedScope:
    """Design test 13: dry-run report carries the resolved project id."""

    def test_project_dry_run_echoes_resolved_id(self, svc, backend, bundle, tmp_path):
        project_dir = tmp_path / "someproj"
        project_dir.mkdir()
        ctx = {"cwd": str(project_dir)}
        expected = svc.resolve_scope_id("project", ctx)
        assert expected is not None
        _write_topic(bundle, "proj-topic", "body")
        report = backend.import_bundle(bundle, "project", "skip", True, terminal_context=ctx)
        assert report.dry_run is True
        assert report.target_scope == "project"
        assert report.target_scope_id == expected
        assert report.imported == 1

    def test_global_report_echoes_scope(self, backend, bundle):
        _write_topic(bundle, "g-topic", "body")
        report = backend.import_bundle(bundle, "global", "skip", False)
        assert report.target_scope == "global"
        assert report.target_scope_id is None


class TestFederatedGate:
    def test_secret_rejected_per_file_import_continues(self, svc, backend, bundle, caplog):
        planted = "AKIA" + "ABCDEFGHIJKLMNOP"
        _write_topic(bundle, "leaky", f"my key {planted}")
        _write_topic(bundle, "clean", "nothing sensitive")
        with caplog.at_level("WARNING"):
            report = backend.import_bundle(bundle, "federated", "skip", False)
        assert report.rejected == 1
        assert report.imported == 1
        assert "aws_access_key" in report.errors["leaky.md"]
        # Pattern names only — never content bytes.
        assert planted not in repr(report)
        assert planted not in caplog.text
        assert svc.get_wiki_path("federated", None, "clean").exists()
        assert not svc.get_wiki_path("federated", None, "leaky").exists()

    def test_dry_run_reports_secret_rejection(self, svc, backend, bundle):
        _write_topic(bundle, "leaky", "my key AKIA" + "ABCDEFGHIJKLMNOP")
        report = backend.import_bundle(bundle, "federated", "skip", True)
        assert report.rejected == 1
        assert not svc.get_wiki_path("federated", None, "leaky").exists()


class TestExportReadPathContainment:
    """U2 carry-forward: tampered index relative_path cannot read outside the wiki dir."""

    def test_tampered_relative_path_skipped(self, svc, backend, tmp_path, caplog):
        _run(svc.store(content="real body", scope="global", key="real-topic"))
        # Plant a file OUTSIDE the wiki dir and point the index entry at it.
        outside = svc.base_dir.parent / "outside.md"
        outside.write_text(
            "# real-topic\n<!-- id: abc | scope: global | type: user | tags: -->\n"
            "\n## 2026-01-01T00:00:00Z\nexfil bait\n",
            encoding="utf-8",
        )
        index_path = svc.get_index_path("global", None)
        text = index_path.read_text(encoding="utf-8")
        tampered = text.replace("global/real-topic.md", "../../../outside.md")
        assert tampered != text
        index_path.write_text(tampered, encoding="utf-8")

        dest = tmp_path / "contained-bundle"
        with caplog.at_level("WARNING"):
            report = backend.export_bundle("global", None, dest, False, False)
        assert report.exported == 0
        assert "okf_export_unsafe_index_path" in caplog.text
        assert "exfil bait" not in caplog.text
        assert not (dest / "real-topic.md").exists()


class TestSymlinkContainment:
    """N6: bundle symlinks must never pull content from outside the bundle root."""

    def test_symlinked_md_outside_bundle_rejected(self, svc, backend, bundle, tmp_path):
        outside = tmp_path / "outside-secret.md"
        outside.write_text(
            "---\ntype: user\n---\n\n# sneaky\n\nexfiltrated content\n", encoding="utf-8"
        )
        (bundle / "sneaky.md").symlink_to(outside)
        report = backend.import_bundle(bundle, "global", "skip", False)
        assert report.imported == 0
        assert report.rejected == 1
        assert "symlink" in report.errors["sneaky.md"]
        assert not svc.get_wiki_path("global", None, "sneaky").exists()

    def test_regular_files_unaffected(self, svc, backend, bundle, tmp_path):
        outside = tmp_path / "elsewhere.md"
        outside.write_text("---\ntype: user\n---\n\n# evil\n\nbad\n", encoding="utf-8")
        (bundle / "evil.md").symlink_to(outside)
        _write_topic(bundle, "honest", "legit content")
        report = backend.import_bundle(bundle, "global", "skip", False)
        assert report.imported == 1
        assert report.rejected == 1
        assert svc.get_wiki_path("global", None, "honest").exists()


class TestTimestampCoercion:
    """I1: the quoted-string strptime branch and the unparseable fallback."""

    QUOTED_ISO = '"2026-01-02T03:04:05Z"'

    def test_quoted_string_timestamp_parsed(self, svc, backend, bundle):
        _write_topic(bundle, "quoted", "body", fm_extra=f"timestamp: {self.QUOTED_ISO}\n")
        report = backend.import_bundle(bundle, "global", "skip", False)
        assert report.imported == 1
        memories = _run(svc.recall(scope="global", limit=10, scan_all=True))
        quoted = next(m for m in memories if m.key == "quoted")
        assert quoted.created_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    def test_unparseable_timestamp_falls_back_to_now(self, svc, backend, bundle):
        _write_topic(bundle, "garbled", "body", fm_extra='timestamp: "not-a-date"\n')
        report = backend.import_bundle(bundle, "global", "skip", False)
        assert report.imported == 1
        assert report.rejected == 0
        text = _stored_text(svc, "garbled")
        assert "not-a-date" not in text
        memories = _run(svc.recall(scope="global", limit=10, scan_all=True))
        garbled = next(m for m in memories if m.key == "garbled")
        # Fallback is now(): the entry heading carries a fresh timestamp.
        assert garbled.created_at.year >= 2026


class TestHeaderCommentOnlySpoofEscaped:
    """I2: a body with ONLY a spoofed header comment (no fake heading) is escaped."""

    def test_header_comment_only_escaped(self, svc, backend, bundle):
        spoof = (
            "Real content line.\n" "<!-- id: deadbeef | scope: global | type: user | tags: x -->\n"
        )
        _write_topic(bundle, "comment-spoof", spoof)
        report = backend.import_bundle(bundle, "global", "skip", False)
        assert report.imported == 1
        assert report.bodies_escaped == 1

        wiki_path = svc.get_wiki_path("global", None, "comment-spoof")
        text = wiki_path.read_text(encoding="utf-8")
        assert "<!-- id: deadbeef" not in text
        entry = {"key": "comment-spoof", "scope": "global", "scope_id": None}
        memory = svc._parse_wiki_file(wiki_path, text, entry)
        assert memory is not None
        assert memory.id != "deadbeef"
