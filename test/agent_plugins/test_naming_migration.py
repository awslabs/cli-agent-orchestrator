"""Docs vocabulary and the prepared skill-rename retirement step (W10).

**Validates: Requirements 21.4, 21.5**

Two things are asserted here, and neither resolves a maintainer decision.

The **docs vocabulary rule** — "event plugin" and "agent plugin" are always
qualified, bare "plugin" only inside a document whose title already scopes it —
is checked mechanically, because a rule enforced by review alone decays.

The **retirement step** for renaming ``cao-plugin`` to ``cao-event-plugin`` is
built and tested while its rename map stays empty. M4 is unresolved, so
activating it must be a data edit rather than a migration project; the tests
below drive the mechanism through a synthetic rename to prove that.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cli_agent_orchestrator.cli.commands import init as init_module

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"


class TestDocsVocabulary:
    """Requirement 21.4."""

    def test_agent_plugins_doc_exists(self):
        assert (DOCS / "agent-plugins.md").is_file()

    def test_event_plugin_doc_keeps_its_path(self):
        """Renaming the file would break inbound links for no comprehension gain."""
        assert (DOCS / "plugins.md").is_file()

    def test_event_plugin_doc_is_retitled(self):
        first_line = (DOCS / "plugins.md").read_text(encoding="utf-8").splitlines()[0]
        assert first_line.strip() == "# Event Plugins"

    def test_each_doc_banners_the_other(self):
        """A reader who lands on either page learns the other exists."""
        event_doc = (DOCS / "plugins.md").read_text(encoding="utf-8")
        agent_doc = (DOCS / "agent-plugins.md").read_text(encoding="utf-8")

        assert "agent-plugins.md" in event_doc
        assert "Agent Plugins" in event_doc.split("## ")[0]  # in the banner, before any section
        assert "plugins.md" in agent_doc
        assert "Event Plugins" in agent_doc.split("## ")[0]

    def test_the_retracted_roadmap_promise_is_recorded_not_deleted(self):
        """The `cao plugin` verb conflict is called out where it was promised."""
        event_doc = (DOCS / "plugins.md").read_text(encoding="utf-8")
        assert "M1" in event_doc


def _prose_of(doc: str) -> str:
    """``doc``'s prose, with code, links and paths removed.

    A bare ``plugin`` inside ``cao plugin add``, or inside a link to
    ``docs/plugins.md``, is not prose making a claim about the noun — it is a
    command or a filename, and neither can be "qualified" without breaking it.
    """
    text = (DOCS / doc).read_text(encoding="utf-8")
    prose = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    prose = re.sub(r"`[^`]*`", "", prose)
    return re.sub(r"\[[^\]]*\]\([^)]*\)", "", prose)


#: Matches "plugin"/"plugins" not already preceded by a qualifier.
_UNQUALIFIED_PLUGIN = re.compile(r"(?<![\w-])(?<!agent )(?<!event )plugins?\b", re.I)

#: Docs whose H1 does **not** scope the noun. Every prose occurrence must be
#: qualified. ``control-planes.md`` earns its place by listing the event plugin
#: system as an outbound control plane — exactly the context where a bare
#: "plugin" now reads ambiguously. The five docs after it are the former
#: vocabulary backlog, brought in line: every occurrence was an event-plugin
#: reference and now says so.
_UNSCOPED_DOCS = [
    "skills.md",
    "control-planes.md",
    "codex-cli.md",
    "knowledge-graph-viewing.md",
    "mcp-apps.md",
    "memory.md",
    "otel-deployment.md",
]

#: Docs whose H1 scopes the noun ("# Event Plugins", "# Agent Plugins"), so bare
#: "plugin" is legitimate *after* the reader has been oriented. They are NOT
#: exempt — see ``TestDocsVocabularyInScopedDocs``.
_SCOPED_DOCS = ["agent-plugins.md", "plugins.md"]

#: Docs where the bare noun names a **third party's own** plugin concept, which
#: Requirement 21.4's event/agent qualifiers would misdescribe. Not a backlog —
#: a permanent, justified exemption, listed rather than silently skipped.
#:
#: The point of the list is that it is a **list**: the coverage test below fails
#: for any *new* plugin-mentioning doc, so this cannot quietly grow. The former
#: backlog entries (codex-cli, knowledge-graph-viewing, mcp-apps, memory,
#: otel-deployment) were all event-plugin references and were qualified and
#: promoted into ``_UNSCOPED_DOCS``.
_VOCABULARY_BACKLOG_DOCS = [
    "cursor-cli.md",  # a Cursor plugin manifest — third-party, qualifying it would be wrong
    "opencode-cli.md",  # OpenCode's own plugin system (its Node.js prerequisite) — same reason
]


class TestDocsVocabularyInUnscopedDocs:
    """Requirement 21.4 — the strict rule, for docs whose title scopes nothing."""

    @pytest.mark.parametrize("doc", _UNSCOPED_DOCS)
    def test_every_prose_occurrence_is_qualified(self, doc):
        unqualified = [match.group(0) for match in _UNQUALIFIED_PLUGIN.finditer(_prose_of(doc))]
        assert unqualified == [], f"{doc}: unqualified 'plugin' in prose: {unqualified}"

    @pytest.mark.parametrize("doc", _UNSCOPED_DOCS)
    def test_the_doc_really_has_no_scoping_title(self, doc):
        """The strict rule must apply for the stated reason, not by accident.

        If one of these docs were retitled to something containing "Plugin", the
        strict check above would become the wrong rule for it, and this test is
        what makes that a visible failure instead of a silent mismatch between the
        list and the reason for the list.
        """
        title = (DOCS / doc).read_text(encoding="utf-8").splitlines()[0]
        assert "Plugin" not in title, f"{doc} now has a scoping title; move it to _SCOPED_DOCS"


class TestDocsVocabularyInScopedDocs:
    """Requirement 21.4 — the rule that actually applies to a scoped document.

    The previous version of this guard parametrized over all four docs and then
    early-returned for any title containing "Plugin". That exempted
    ``agent-plugins.md`` and ``plugins.md`` — the two documents where confusing the
    two plugin systems does the most damage, and the two the commit message
    claimed the guard covered. Only ``skills.md`` and ``control-planes.md`` were
    ever checked.

    A scoped doc cannot sensibly obey the strict rule: "# Event Plugins" exists
    precisely so the body can say "plugin" without repeating the qualifier in every
    sentence. The rule that *does* apply is about orientation — the noun must be
    qualified where the reader first meets it, and the other system must be named
    so a reader who arrived at the wrong page finds out.
    """

    @pytest.mark.parametrize("doc", _SCOPED_DOCS)
    def test_the_doc_has_a_scoping_title(self, doc):
        """The exemption from the strict rule is earned by the title, so assert it."""
        title = (DOCS / doc).read_text(encoding="utf-8").splitlines()[0]
        assert "Plugin" in title, f"{doc} has no scoping H1; move it to _UNSCOPED_DOCS"

    @pytest.mark.parametrize("doc", _SCOPED_DOCS)
    def test_the_noun_is_qualified_on_first_reference(self, doc):
        """The first prose mention of the noun carries its qualifier.

        Checked in the **banner region** — everything before the first ``## ``
        heading — because that is what a reader sees before deciding whether they
        are on the right page, and it is where both docs already disambiguate.
        """
        banner = _prose_of(doc).split("\n## ")[0]

        assert _UNQUALIFIED_PLUGIN.search(banner) is None or any(
            phrase in banner.lower() for phrase in ("agent plugin", "event plugin")
        ), f"{doc}: the banner uses bare 'plugin' without ever qualifying it"

    @pytest.mark.parametrize("doc", _SCOPED_DOCS)
    def test_the_banner_names_the_other_system(self, doc):
        """A reader on the wrong page must be told the other one exists.

        Overlaps ``test_each_doc_banners_the_other`` deliberately: that one asserts
        the cross-link, this one asserts the *vocabulary* — that the other system is
        named in words, not merely hyperlinked, since a link's URL is stripped from
        prose and a reader skimming headings would miss it.
        """
        banner = _prose_of(doc).split("\n## ")[0].lower()
        other = "event plugin" if doc == "agent-plugins.md" else "agent plugin"
        assert other in banner, f"{doc}: banner never names the other system ('{other}')"


class TestNoPluginDocIsSilentlyExempt:
    """The guard's coverage is itself asserted, because it silently lost half before.

    The original defect was not a wrong rule, it was an **invisible** one: a
    ``return`` inside a parametrized test meant two of four docs were never
    checked, and nothing said so. Every doc that mentions plugins is now in
    exactly one of three named lists, and adding a doc to none of them fails.
    """

    _ALL_LISTS = {
        "_UNSCOPED_DOCS": _UNSCOPED_DOCS,
        "_SCOPED_DOCS": _SCOPED_DOCS,
        "_VOCABULARY_BACKLOG_DOCS": _VOCABULARY_BACKLOG_DOCS,
    }

    def test_the_lists_do_not_overlap(self):
        """A doc in two lists would be checked under two rules, one of them wrong."""
        seen: dict = {}
        for list_name, docs in self._ALL_LISTS.items():
            for doc in docs:
                assert doc not in seen, f"{doc} is in both {seen[doc]} and {list_name}"
                seen[doc] = list_name

    def test_every_plugin_mentioning_doc_is_classified(self):
        """No doc that talks about plugins may sit outside all three lists.

        This is the test that would have caught the original defect. A new doc that
        discusses either plugin system must declare which rule it lives under,
        rather than being covered by none while looking covered.
        """
        classified = {doc for docs in self._ALL_LISTS.values() for doc in docs}

        mentions_plugins = {
            path.name
            for path in sorted(DOCS.glob("*.md"))
            if "plugin" in path.read_text(encoding="utf-8").lower()
        }

        unclassified = sorted(mentions_plugins - classified)
        assert not unclassified, (
            "doc(s) mention plugins but are in none of "
            + ", ".join(self._ALL_LISTS)
            + ": "
            + ", ".join(unclassified)
            + ". Add each to the list matching its situation, so the rule applied is explicit."
        )

    def test_every_listed_doc_exists(self):
        """A renamed or deleted doc must leave the list, or its rule silently stops."""
        missing = [
            f"{list_name}:{doc}"
            for list_name, docs in self._ALL_LISTS.items()
            for doc in docs
            if not (DOCS / doc).is_file()
        ]
        assert not missing, "listed doc(s) do not exist: " + ", ".join(missing)

    @pytest.mark.parametrize("doc", _VOCABULARY_BACKLOG_DOCS)
    def test_a_backlog_doc_does_not_discuss_agent_plugins(self, doc):
        """The backlog is only defensible while these docs are about the *other* system.

        A bare "plugin" in a doc that predates agent plugins is ambiguous but
        harmless as long as the doc never discusses agent plugins. The moment one
        does, its unqualified prose becomes actively misleading and the doc has to
        graduate to a real rule. This test is what forces that.
        """
        text = (DOCS / doc).read_text(encoding="utf-8").lower()
        assert "agent plugin" not in text and "agent-plugins.md" not in text, (
            f"{doc} now references agent plugins, so bare 'plugin' in it is ambiguous. "
            f"Qualify its prose and move it to _UNSCOPED_DOCS."
        )


class TestRenameRetirementIsPreparedNotActivated:
    """Requirement 21.5 — the mechanism exists; the decision does not."""

    def test_the_rename_map_is_empty_pending_m4(self):
        """This document does not settle M4, so nothing is renamed yet."""
        assert init_module.SKILL_RENAMES == {}

    def test_sync_skills_still_ships_the_current_name(self):
        """No rename has been applied to the shipped allowlist either."""
        import sys

        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import sync_skills

        assert "cao-plugin" in sync_skills.SHIPPED_SKILLS
        assert "cao-event-plugin" not in sync_skills.SHIPPED_SKILLS

    def test_the_coupling_is_documented_where_a_renamer_would_look(self):
        """A future renamer reads the allowlist; the warning has to be there."""
        source = (REPO_ROOT / "scripts" / "sync_skills.py").read_text(encoding="utf-8")
        assert "SKILL_RENAMES" in source


class TestRetirementMechanism:
    """Drive the mechanism through a synthetic rename, so M4 is a data edit."""

    @pytest.fixture
    def skills_dir(self, tmp_path, monkeypatch):
        path = tmp_path / "skills"
        path.mkdir()
        monkeypatch.setattr(init_module, "SKILLS_DIR", path)
        return path

    def _write(self, folder: Path, name: str, body: str = "Shared body.") -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Authoring guide.\n---\n\n{body}\n",
            encoding="utf-8",
        )
        return folder

    def test_an_unmodified_old_folder_is_retired(self, skills_dir, monkeypatch):
        self._write(skills_dir / "old-name", "old-name")
        self._write(skills_dir / "new-name", "new-name")
        monkeypatch.setattr(init_module, "SKILL_RENAMES", {"old-name": "new-name"})

        retired = init_module._retire_renamed_skills()

        assert retired == ["old-name"]
        assert not (skills_dir / "old-name").exists()
        assert (skills_dir / "new-name" / "SKILL.md").is_file()

    def test_a_locally_modified_old_folder_is_kept_with_a_warning(
        self, skills_dir, monkeypatch, caplog
    ):
        """Deleting an operator's edits to tidy a catalog is the worse outcome."""
        self._write(skills_dir / "old-name", "old-name", body="I customized this.")
        self._write(skills_dir / "new-name", "new-name")
        monkeypatch.setattr(init_module, "SKILL_RENAMES", {"old-name": "new-name"})

        with caplog.at_level("WARNING"):
            retired = init_module._retire_renamed_skills()

        assert retired == []
        assert (skills_dir / "old-name").exists()
        assert "local modifications" in caplog.text

    def test_nothing_is_retired_before_the_replacement_exists(self, skills_dir, monkeypatch):
        self._write(skills_dir / "old-name", "old-name")
        monkeypatch.setattr(init_module, "SKILL_RENAMES", {"old-name": "new-name"})

        assert init_module._retire_renamed_skills() == []
        assert (skills_dir / "old-name").exists()

    def test_an_extra_file_counts_as_a_modification(self, skills_dir, monkeypatch):
        self._write(skills_dir / "old-name", "old-name")
        (skills_dir / "old-name" / "notes.md").write_text("mine", encoding="utf-8")
        self._write(skills_dir / "new-name", "new-name")
        monkeypatch.setattr(init_module, "SKILL_RENAMES", {"old-name": "new-name"})

        assert init_module._retire_renamed_skills() == []
        assert (skills_dir / "old-name").exists()

    def test_reference_files_are_compared_too(self, skills_dir, monkeypatch):
        old = self._write(skills_dir / "old-name", "old-name")
        new = self._write(skills_dir / "new-name", "new-name")
        (old / "references").mkdir()
        (new / "references").mkdir()
        (old / "references" / "guide.md").write_text("v1", encoding="utf-8")
        (new / "references" / "guide.md").write_text("v2", encoding="utf-8")
        monkeypatch.setattr(init_module, "SKILL_RENAMES", {"old-name": "new-name"})

        assert init_module._retire_renamed_skills() == []

    def test_the_frontmatter_name_line_is_not_treated_as_a_modification(
        self, skills_dir, monkeypatch
    ):
        """A rename necessarily changes it, so comparing it would retire nothing."""
        self._write(skills_dir / "old-name", "old-name")
        self._write(skills_dir / "new-name", "new-name")
        monkeypatch.setattr(init_module, "SKILL_RENAMES", {"old-name": "new-name"})

        assert init_module._retire_renamed_skills() == ["old-name"]

    def test_it_never_raises_on_an_unremovable_folder(self, skills_dir, monkeypatch):
        self._write(skills_dir / "old-name", "old-name")
        self._write(skills_dir / "new-name", "new-name")
        monkeypatch.setattr(init_module, "SKILL_RENAMES", {"old-name": "new-name"})

        def boom(*args, **kwargs):
            raise PermissionError("read-only filesystem")

        monkeypatch.setattr(init_module.shutil, "rmtree", boom)

        assert init_module._retire_renamed_skills() == []  # logged, not raised

    def test_an_empty_map_is_a_no_op(self, skills_dir):
        self._write(skills_dir / "cao-plugin", "cao-plugin")

        assert init_module._retire_renamed_skills() == []
        assert (skills_dir / "cao-plugin").exists()
