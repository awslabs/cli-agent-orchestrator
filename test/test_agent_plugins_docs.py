"""Guard tests for the agent-plugin documentation (W10).

**Validates: Requirements 1.5, 13.1, 21.4, 22.1, 22.7**

This tier asserts the properties the requirements make *checkable* — the
warning being early enough to be seen, the prerequisites being facts rather than
guesses, the two plugin systems being named apart — rather than trying to review
prose.

The value of this tier is that documentation claims and code drift in opposite
directions and nothing normally notices. Two tests here are cross-checks rather
than string matches, and they are the ones worth keeping:
``test_the_documented_endpoint_matches_the_real_default`` reads
``constants.SERVER_HOST``/``SERVER_PORT``, and
``test_package_skill_lists_match_the_build_configuration`` reads
``scripts/build_agent_plugin.py``'s own ``PACKAGES``. A doc that falls behind
either fails here instead of misleading an operator.

**Adapted, not copied.** Six of the donor's assertions matched wording this branch
words differently, and in most cases better — the skills section is
``## Agent-Plugin-Provided Skills`` rather than ``## Plugin-provided skills``,
which is what the Requirement 21.4 vocabulary rule actually demands, and the
provisional gating is stated per surface in ``[!NOTE]`` blocks rather than as one
blanket ``PROVISIONAL PENDING M1-M4`` banner. Those assertions were re-pointed at
this branch's equivalents; the underlying requirement each checks is unchanged.
Complements ``test/agent_plugins/test_naming_migration.py``, which owns the
vocabulary rule itself; this file owns the operator-facing claims.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_PLUGINS_DOC = REPO_ROOT / "docs" / "agent-plugins.md"
EVENT_PLUGINS_DOC = REPO_ROOT / "docs" / "plugins.md"
SKILLS_DOC = REPO_ROOT / "docs" / "skills.md"

#: "Stated in the first screenful" made concrete. A reader must not have to
#: scroll to learn they are about to run untrusted code.
FIRST_SCREENFUL_LINES = 40


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _head(path: Path, lines: int = FIRST_SCREENFUL_LINES) -> str:
    return "\n".join(_text(path).splitlines()[:lines])


class TestUntrustedContentWarning:
    """Requirement 22.1 — stated at or before the point of install."""

    def test_the_doc_exists(self) -> None:
        assert AGENT_PLUGINS_DOC.is_file()

    def test_warning_is_in_the_first_screenful(self) -> None:
        assert "untrusted code and content" in _head(AGENT_PLUGINS_DOC)

    def test_warning_does_not_imply_a_trust_model_cao_lacks(self) -> None:
        """CAO implements no signing or provenance verification, so say so.

        The failure this prevents is a warning that reads as "we checked and it
        looks fine" — an operator who has been told CAO validates a plugin can
        reasonably infer it also verifies where it came from. It does not.
        """
        head = _head(AGENT_PLUGINS_DOC)

        assert "no signing" in head
        assert "no provenance" in head

    def test_the_warning_says_what_the_risk_concretely_is(self) -> None:
        """Naming the mechanism is what makes the warning actionable.

        "Runs untrusted code" is easy to skim past. Saying skills become prompt
        instructions and MCP servers become subprocesses tells a reader what they
        are actually consenting to.
        """
        head = _head(AGENT_PLUGINS_DOC).lower()

        assert "system prompt" in head
        assert "subprocess" in head


class TestPrerequisitesAndPosture:
    """Requirements 1.5, 22.7."""

    def test_states_the_uv_prerequisite(self) -> None:
        assert "`uv`" in _text(AGENT_PLUGINS_DOC)

    def test_states_the_local_api_server_prerequisite(self) -> None:
        text = _text(AGENT_PLUGINS_DOC)

        assert "127.0.0.1:9889" in text
        assert "cao-server" in text

    def test_the_documented_endpoint_matches_the_real_default(self) -> None:
        """A stated prerequisite must be a fact, not a guess.

        The cross-check, not the string match, is the point: if the default host
        or port ever changes, this fails rather than leaving the doc quietly wrong.
        """
        from cli_agent_orchestrator.constants import SERVER_HOST, SERVER_PORT

        assert f"{SERVER_HOST}:{SERVER_PORT}" in _text(AGENT_PLUGINS_DOC)

    def test_states_the_localhost_only_posture(self) -> None:
        """Requirement 22.7."""
        text = _text(AGENT_PLUGINS_DOC).lower()

        assert "localhost-only" in text
        # And that overriding it is the operator's decision, not a silent change.
        assert "cao_api_host" in text

    def test_documents_the_two_package_split(self) -> None:
        """Requirement 1.5 — which package an operator wants, versus a contributor."""
        text = _text(AGENT_PLUGINS_DOC)

        assert "agent-plugin/cao" in text
        assert "agent-plugin/cao-contributor" in text

    def test_the_split_is_explained_by_audience_not_just_named(self) -> None:
        """Two package names alone do not tell a reader which one to install.

        The donor asserted the literal phrases "Drive a CAO fleet" / "Extend CAO
        itself"; this branch carries the same distinction as a table keyed on
        "Install this if you are…", so the assertion is on the distinction rather
        than on either wording.
        """
        text = _text(AGENT_PLUGINS_DOC).lower()

        assert "an operator" in text and "a contributor" in text
        assert "extending cao itself" in text

    def test_package_skill_lists_match_the_build_configuration(self) -> None:
        """The doc must not drift from what the packages actually ship.

        Reads ``scripts/build_agent_plugin.py``'s own ``PACKAGES`` rather than a
        copy of the skill names, so adding a skill to a package without mentioning
        it in the docs fails here.
        """
        import importlib.util
        import sys

        spec = importlib.util.spec_from_file_location(
            "_build_agent_plugin_docs", REPO_ROOT / "scripts" / "build_agent_plugin.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        # Registered before `exec_module` on purpose: the script defines
        # dataclasses, and `@dataclass` resolves the module by name at class
        # creation time, so a module absent from `sys.modules` raises
        # `AttributeError: 'NoneType' object has no attribute '__dict__'`.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        text = _text(AGENT_PLUGINS_DOC)
        for config in module.PACKAGES:
            for skill in config.skills:
                assert skill in text, f"{config.name}: doc does not mention {skill!r}"


class TestEventPluginDisambiguation:
    """Requirement 21.4 — the two systems are always named apart."""

    def test_event_plugin_page_is_retitled(self) -> None:
        assert _text(EVENT_PLUGINS_DOC).splitlines()[0] == "# Event Plugins"

    def test_event_plugin_page_carries_a_disambiguation_banner(self) -> None:
        head = _head(EVENT_PLUGINS_DOC)

        assert "agent plugin" in head.lower()
        assert "agent-plugins.md" in head

    def test_agent_plugin_page_disambiguates_in_the_other_direction(self) -> None:
        text = _text(AGENT_PLUGINS_DOC)

        assert "event plugin" in text.lower()
        assert "plugins.md" in text

    def test_the_event_plugin_page_keeps_its_path(self) -> None:
        """Retitling must not break inbound links."""
        assert EVENT_PLUGINS_DOC.is_file()

    @pytest.mark.parametrize(
        "source,label",
        [
            ("README.md", "[Event plugins](docs/plugins.md)"),
            ("CODEBASE.md", "[Event Plugins](docs/plugins.md)"),
            ("docs/control-planes.md", "[Event Plugins](plugins.md)"),
        ],
    )
    def test_inbound_links_use_the_qualified_name(self, source: str, label: str) -> None:
        """A bare "Plugins" label outside a page that scopes it is ambiguous."""
        assert label in _text(REPO_ROOT / source)

    def test_no_bare_plugins_link_label_remains(self) -> None:
        """No maintained page still links to the event-plugin page as "Plugins".

        Kept as its own test rather than folded into the parametrized one above:
        that one asserts the *right* label is present, this one asserts the wrong
        one is absent, and a page could satisfy the first while still carrying a
        second, bare link.
        """
        offenders = [
            source
            for source in ("README.md", "CODEBASE.md", "docs/control-planes.md")
            if re.search(r"\[Plugins?\]\((?:docs/)?plugins\.md\)", _text(REPO_ROOT / source))
        ]

        assert offenders == []


class TestSkillsDocCoversProjection:
    """Requirement 13.1."""

    def test_has_an_agent_plugin_skills_section(self) -> None:
        """The heading is qualified, per the Requirement 21.4 vocabulary rule."""
        assert "## Agent-Plugin-Provided Skills" in _text(SKILLS_DOC)

    def test_explains_the_projection_and_the_collision_rules(self) -> None:
        """Both rules an operator actually hits, in the terms the code uses."""
        text = _text(SKILLS_DOC)

        assert "projected" in text
        assert "always wins" in text
        assert "lexicographically smallest" in text

    def test_documents_the_copy_mode_fallback(self) -> None:
        assert "projection_mode" in _text(SKILLS_DOC)

    def test_agent_plugins_doc_links_to_this_section(self) -> None:
        assert "skills.md#agent-plugin-provided-skills" in _text(AGENT_PLUGINS_DOC)

    def test_the_section_anchor_the_link_targets_actually_exists(self) -> None:
        """A link and a heading can drift apart silently; tie them together.

        GitHub derives the anchor by lowercasing and replacing spaces with
        hyphens, so the heading and the link are two encodings of one fact and
        this recomputes one from the other.
        """
        heading = next(
            line for line in _text(SKILLS_DOC).splitlines() if line.startswith("## Agent-Plugin")
        )
        anchor = heading.lstrip("# ").strip().lower().replace(" ", "-")

        assert f"skills.md#{anchor}" in _text(AGENT_PLUGINS_DOC)

    def test_the_extra_dirs_limitation_is_recorded(self) -> None:
        """The W6 finding, per the Requirement 13 amendment (Criterion 7)."""
        text = _text(SKILLS_DOC)

        assert "Known Limitations" in text
        assert "Kiro CLI or OpenCode CLI" in text


class TestProvisionalPendingDecisions:
    """The M1-M4 gate is recorded in the docs, not only in the commit message.

    The donor asserted a single ``PROVISIONAL PENDING M1-M4`` banner in each doc's
    first screenful. This branch states the gate **per surface** instead — a
    ``[!NOTE]`` on the command verb, another on the web tab — which is more useful
    to a reader and harder to leave stale, since each note sits beside the thing it
    qualifies. These assert that structure.
    """

    def test_the_command_verb_is_marked_provisional(self) -> None:
        text = _text(AGENT_PLUGINS_DOC)

        assert "**provisional**" in text
        assert "M1" in text

    def test_the_verb_note_explains_what_the_conflict_is(self) -> None:
        """A bare "provisional" tells a reader nothing about what might change."""
        text = _text(AGENT_PLUGINS_DOC)

        assert "docs/plugins.md" in text or "plugins.md" in text
        assert "retracts" in text or "roadmap" in text

    def test_the_unshipped_surfaces_are_named_with_their_gate(self) -> None:
        """Requirement 16.5 — a doc must not describe a surface as available.

        The gates are the shared ``CAO_AGENT_PLUGINS_ENABLED`` execution gate over
        the CLI group and the four HTTP routes, the web tab's build-time constant,
        and the TUI rows' policy. Naming the flag that holds each closed is what
        lets a reader verify the claim, and what makes flipping one a documentation
        change too. ``hidden=True`` is deliberately no longer required here: the
        review of revision 2 established that hiding help text is not a ship gate,
        so the doc must name the gate that actually refuses execution.
        """
        text = _text(AGENT_PLUGINS_DOC)

        assert "CAO_AGENT_PLUGINS_ENABLED" in text
        assert "PLUGINS_TAB_ENABLED" in text
        assert "Policy::Hidden" in text

    def test_the_event_plugin_page_records_the_verb_conflict(self) -> None:
        """M3/M1 land on this page too: its roadmap promised the same noun."""
        text = _text(EVENT_PLUGINS_DOC)

        assert "M1" in text
        assert "unsettled" in text.lower() or "open maintainer decision" in text.lower()
