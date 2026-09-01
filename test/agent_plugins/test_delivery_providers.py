"""W6 — cross-provider skill delivery verification.

**This is not optional test scaffolding.** design.md names W6 "the integration
gate for Increment 1": the whole point of projecting into ``SKILLS_DIR`` rather
than registering a new search directory is that *every* provider picks plugin
skills up through the pathway it already uses. If that claim is wrong for even
one provider, the central design decision is wrong, and only a test against each
provider's **real delivery artifact** can tell us.

The seven providers split three ways:

* **Runtime catalog** (Claude Code, Codex, Kimi, Antigravity) — the catalog text
  ``terminal_service`` builds at launch.
* **Baked catalog** (Copilot) — the ``.agent.md`` body written at install time.
* **Filesystem-direct** (Kiro CLI, OpenCode) — the ``skill://`` resource glob and
  the ``OPENCODE_CONFIG_DIR/skills`` symlink, neither of which goes anywhere near
  ``list_skills()``. These two are the reason the "extra search directory"
  alternative was rejected.
"""

from __future__ import annotations

import glob
from pathlib import Path

import pytest

from cli_agent_orchestrator.agent_plugins.installer import install
from cli_agent_orchestrator.agent_plugins.models import PluginSource
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.services.terminal_service import RUNTIME_SKILL_PROMPT_PROVIDERS

from .conftest import build_plugin, write_skill

PROJECTED_SKILL = "plugin-provided-skill"
PROJECTED_DESCRIPTION = "Delivered to every provider through the existing pathway."


@pytest.fixture
def delivered(store, skills_dir, tmp_path, monkeypatch):
    """Install a plugin and point the skill machinery at the isolated store.

    ``utils/skills.py`` reads the module-level ``SKILLS_DIR``, so the patch has
    to land there — that module is what every one of the three delivery
    mechanisms ultimately consults (directly, or by reading the same path).
    """
    source = build_plugin(tmp_path / "src", "delivery-demo", skills=[PROJECTED_SKILL])
    write_skill(source / "skills" / PROJECTED_SKILL, PROJECTED_SKILL, PROJECTED_DESCRIPTION)

    outcome = install(
        PluginSource(kind="path", location=str(source)),
        store=store,
        skills_dir=skills_dir,
        refresh_agents=False,
    )
    assert outcome.installed

    monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skills_dir)
    return skills_dir


@pytest.fixture
def kiro_install(delivered, tmp_path, monkeypatch):
    """Run a real Kiro agent install and hand back the JSON it wrote.

    Kiro is the only provider whose skill delivery ships as a **pattern** rather
    than resolved content, so it is the only one where the emitted artifact has to
    be read to know what was delivered. ``test/services/test_install_service.py``
    already asserts this array's shape for the no-plugin case; this fixture exists
    so the plugin-delivery suite can assert it against a store that has a plugin
    projected into it.
    """
    import json

    local_store = tmp_path / "agent-store"
    context_dir = tmp_path / "agent-context"
    kiro_dir = tmp_path / "kiro-agents"
    for directory in (local_store, context_dir, kiro_dir):
        directory.mkdir(parents=True, exist_ok=True)

    for target, value in (
        ("cli_agent_orchestrator.services.profile_store.LOCAL_AGENT_STORE_DIR", local_store),
        ("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", local_store),
        ("cli_agent_orchestrator.services.install_service.AGENT_CONTEXT_DIR", context_dir),
        ("cli_agent_orchestrator.services.install_service.KIRO_AGENTS_DIR", kiro_dir),
        # The globs are built from `install_service`'s own SKILLS_DIR, so this is
        # the patch that makes the emitted paths point at the projected store.
        ("cli_agent_orchestrator.services.install_service.SKILLS_DIR", delivered),
    ):
        monkeypatch.setattr(target, value)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
    )

    (local_store / "delivery-agent.md").write_text(
        "---\nname: delivery-agent\ndescription: Delivery agent\n---\nPrompt.\n",
        encoding="utf-8",
    )

    from cli_agent_orchestrator.services.install_service import install_agent

    result = install_agent("delivery-agent", ProviderType.KIRO_CLI.value)
    assert result.success, result.message

    return {
        "agent_json": json.loads((kiro_dir / "delivery-agent.json").read_text(encoding="utf-8")),
        "skills_root": delivered,
    }


class TestRuntimeCatalogProviders:
    """Claude Code, Codex, Kimi, Antigravity — Requirement 13.2, Task 7.1."""

    @pytest.mark.parametrize(
        "provider",
        [
            ProviderType.CLAUDE_CODE.value,
            ProviderType.CODEX.value,
            ProviderType.KIMI_CLI.value,
            ProviderType.ANTIGRAVITY_CLI.value,
        ],
    )
    def test_projected_skill_appears_in_the_runtime_catalog(self, delivered, provider):
        from cli_agent_orchestrator.utils.skills import build_skill_catalog

        assert provider in RUNTIME_SKILL_PROMPT_PROVIDERS

        catalog = build_skill_catalog()

        assert PROJECTED_SKILL in catalog
        assert PROJECTED_DESCRIPTION in catalog

    def test_a_profile_skill_filter_still_applies_to_projected_skills(self, delivered):
        """Projection adds skills to the catalog; it does not bypass its scoping."""
        from cli_agent_orchestrator.utils.skills import build_skill_catalog

        assert PROJECTED_SKILL in build_skill_catalog([PROJECTED_SKILL])
        assert PROJECTED_SKILL not in build_skill_catalog(["something-else"])

    def test_the_skill_is_loadable_by_name_through_the_normal_resolver(self, delivered):
        """The ``load_skill`` MCP tool's path, unmodified."""
        from cli_agent_orchestrator.utils.skills import load_skill_content, load_skill_metadata

        assert load_skill_metadata(PROJECTED_SKILL).description == PROJECTED_DESCRIPTION
        assert load_skill_content(PROJECTED_SKILL)


class TestCopilotBakedCatalog:
    """Copilot — Requirement 13.2, Task 7.2."""

    def test_projected_skill_appears_in_the_baked_agent_prompt(self, delivered):
        from cli_agent_orchestrator.models.agent_profile import AgentProfile
        from cli_agent_orchestrator.utils.skill_injection import compose_agent_prompt

        profile = AgentProfile(name="worker", description="d", prompt="Base prompt.")
        composed = compose_agent_prompt(profile)

        assert composed is not None
        assert PROJECTED_SKILL in composed
        assert "Base prompt." in composed

    def test_install_time_refresh_is_what_keeps_the_baked_catalog_current(
        self, store, skills_dir, tmp_path, monkeypatch
    ):
        """Requirement 13.1 / design step 6 — the one path baked at install time.

        Skipping the refresh call would leave Copilot ``.agent.md`` catalogs
        stale, which is exactly why the installer makes it.
        """
        calls = []
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.skill_injection.refresh_all_cao_managed_agents",
            lambda: calls.append(True) or [],
        )

        source = build_plugin(tmp_path / "src", "refresh-demo", skills=["alpha"])
        install(
            PluginSource(kind="path", location=str(source)),
            store=store,
            skills_dir=skills_dir,
            refresh_agents=True,
        )

        assert calls, "installer must refresh baked provider artifacts"


class TestKiroFilesystemGlob:
    """Kiro CLI — Requirement 13.2, Task 7.3.

    Kiro is the one provider CAO hands a *pattern* rather than a resolved list, so
    it is the one provider where delivery depends on someone else's glob
    implementation. That makes ``**`` a liability: its treatment of directory
    symlinks is genuinely implementation-dependent, and projected plugin skills
    are directory symlinks. Hence two globs — see the comment at the emission
    site in ``install_service``.
    """

    def test_the_skill_resource_glob_traverses_the_projected_link(self, delivered):
        """The recursive glob reaches the projected skill under stdlib semantics.

        No CAO code walks this glob — Kiro does, natively. So the assertion has
        to be that the glob *as written* expands through the projected symlink.
        """
        pattern = f"{delivered}/**/SKILL.md"
        matches = glob.glob(pattern, recursive=True)

        assert any(
            Path(match).parent.name == PROJECTED_SKILL for match in matches
        ), f"projected skill not reachable through {pattern}: {matches}"

    def test_the_recursive_glob_alone_is_not_sufficient_under_strict_semantics(
        self, delivered
    ) -> None:
        """Why a second pattern exists, demonstrated rather than asserted in prose.

        ``pathlib.Path.glob`` is a real, stdlib implementation of ``**`` that does
        **not** descend into directory symlinks. Under that reading the projected
        skill is invisible — which is exactly what would happen inside Kiro if its
        glob agreed with pathlib rather than with ``glob.glob``.

        If a future Python makes ``pathlib`` follow symlinks here, this test fails
        loudly. That is the correct outcome: it means the premise changed and the
        second glob should be re-justified, not that the code broke.
        """
        recursive = list(Path(delivered).glob("**/SKILL.md"))

        assert not any(p.parent.name == PROJECTED_SKILL for p in recursive), (
            "pathlib's `**` now follows directory symlinks; re-examine whether the "
            f"`*/SKILL.md` companion glob is still needed. Matches: {recursive}"
        )

    def test_the_single_level_glob_reaches_the_projected_link_either_way(self, delivered) -> None:
        """The hardening itself: ``*/SKILL.md`` works under BOTH implementations.

        A single-level match names the symlink as a directory entry and resolves
        through it, so there is no recursive descent for an implementation to opt
        out of. This is what makes projected skills reachable by Kiro regardless
        of how it reads ``**``.
        """
        stdlib_matches = glob.glob(f"{delivered}/*/SKILL.md")
        pathlib_matches = list(Path(delivered).glob("*/SKILL.md"))

        assert any(Path(m).parent.name == PROJECTED_SKILL for m in stdlib_matches)
        assert any(p.parent.name == PROJECTED_SKILL for p in pathlib_matches)

    def test_the_installed_kiro_profile_carries_both_globs(self, kiro_install):
        """Both globs reach the agent JSON Kiro actually loads.

        Asserts the **emitted artifact**, not `install_service`'s source text. An
        earlier version of this test read the module with `Path(...).read_text()`
        and asserted two f-string literals appeared in it, which would have kept
        passing if the list were built and then dropped, and ignored the
        `skills_root` fixture it went to the trouble of setting up.
        """
        resources = kiro_install["agent_json"]["resources"]
        skill_globs = [r for r in resources if r.startswith("skill://")]

        assert len(skill_globs) == 2, f"expected both skill globs, got {skill_globs}"
        assert [glob.rsplit("/skills", 1)[-1] for glob in skill_globs] == [
            "/**/SKILL.md",
            "/*/SKILL.md",
        ]

    def test_both_globs_are_rooted_at_the_real_skill_store(self, kiro_install):
        """The globs point at `SKILLS_DIR`, which is what makes them find anything.

        Separated from the shape assertion above because the two fail for different
        reasons: a wrong *pattern* is a glob-semantics mistake, while a wrong *root*
        means Kiro is handed a path with no skills under it at all.
        """
        skills_root = kiro_install["skills_root"]
        skill_globs = [
            r for r in kiro_install["agent_json"]["resources"] if r.startswith("skill://")
        ]

        assert all(glob.startswith(f"skill://{skills_root}/") for glob in skill_globs)


class TestOpenCodeSymlink:
    """OpenCode — Requirement 13.2, Task 7.4."""

    def test_the_config_symlink_traversal_reaches_the_projected_skill(
        self, delivered, tmp_path, monkeypatch
    ):
        """OpenCode reads ``OPENCODE_CONFIG_DIR/skills`` → ``SKILLS_DIR``.

        Two links in a chain: OpenCode's own into the skill store, then CAO's
        projection into the plugin root. Both must traverse.
        """
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.opencode_config.OPENCODE_CONFIG_DIR", config_dir
        )
        monkeypatch.setattr("cli_agent_orchestrator.utils.opencode_config.SKILLS_DIR", delivered)

        from cli_agent_orchestrator.utils.opencode_config import ensure_skills_symlink

        ensure_skills_symlink()

        through_symlink = config_dir / "skills" / PROJECTED_SKILL / "SKILL.md"
        assert through_symlink.is_file()
        assert PROJECTED_DESCRIPTION in through_symlink.read_text(encoding="utf-8")


class TestNoNewLaunchTimeCost:
    """Requirement 13.5 — projection adds entries to a scan, not a new scan."""

    def test_search_dirs_are_unchanged_by_installing_a_plugin(
        self, store, skills_dir, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skills_dir)
        from cli_agent_orchestrator.utils.skills import _skill_search_dirs

        before = list(_skill_search_dirs())

        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        install(
            PluginSource(kind="path", location=str(source)),
            store=store,
            skills_dir=skills_dir,
            refresh_agents=False,
        )

        assert list(_skill_search_dirs()) == before


class TestEventPluginSystemUntouched:
    """Decision D7 regression guard — the *other* plugin system stays put."""

    def test_event_plugin_registry_still_imports_from_its_own_package(self):
        from cli_agent_orchestrator.plugins.registry import PluginRegistry

        assert PluginRegistry.__module__ == "cli_agent_orchestrator.plugins.registry"

    def test_entry_point_group_is_unchanged(self):
        """Asserted against the installed metadata, not against pyproject.toml.

        ``tomllib`` is stdlib only from 3.11 and this project supports 3.10, but
        the stronger reason is that reading the declaration proves only that
        someone wrote it down. Reading the *installed* entry points proves the
        group is really discoverable, which is what decision D7 actually
        promises.
        """
        from importlib.metadata import entry_points

        registered = list(entry_points(group="cao.plugins"))
        assert registered, "the cao.plugins entry-point group resolves to nothing"

    def test_no_symbol_in_agent_plugins_shadows_one_in_plugins(self):
        """The two packages are siblings; a shared public name would defeat that."""
        import cli_agent_orchestrator.agent_plugins as agent_plugins
        import cli_agent_orchestrator.plugins as event_plugins

        agent_names = {n for n in dir(agent_plugins) if not n.startswith("_")}
        event_names = {n for n in dir(event_plugins) if not n.startswith("_")}

        assert not (agent_names & event_names)

    def test_agent_plugins_is_not_nested_under_plugins(self):
        import cli_agent_orchestrator.agent_plugins as agent_plugins

        assert agent_plugins.__name__ == "cli_agent_orchestrator.agent_plugins"
        assert not agent_plugins.__name__.startswith("cli_agent_orchestrator.plugins.")

    def test_agent_plugins_imports_nothing_from_the_event_plugin_package(self):
        """A stated non-dependency, asserted rather than assumed.

        Parsed rather than grepped: the package docstrings discuss the
        event-plugin import path by name, and a substring match would flag the
        prose that exists precisely to keep the two systems distinct.
        """
        import ast

        package_dir = (
            Path(__file__).resolve().parents[2] / "src" / "cli_agent_orchestrator" / "agent_plugins"
        )
        event_package = "cli_agent_orchestrator.plugins"

        for module in sorted(package_dir.glob("*.py")):
            tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert not node.module.startswith(
                        event_package
                    ), f"{module.name}: {node.module}"
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        assert not alias.name.startswith(
                            event_package
                        ), f"{module.name}: {alias.name}"
