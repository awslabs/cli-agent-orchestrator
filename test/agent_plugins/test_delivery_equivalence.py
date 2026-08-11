"""W6 / Property 10 — cross-provider delivery equivalence.

**Property 10: Cross-provider delivery equivalence**
**Validates: Requirements 13.2, 13.6, 13.7**

For every one of the seven providers, the set of skill names reachable by an
agent equals ``builtin ∪ projected(valid plugin skills)``, plus ``extra_dirs``
for the providers Requirement 13.6 names — asserted against each provider's
*real* delivery artifact, not against ``list_skills()`` standing in for all of
them. Two of the seven (Kiro, OpenCode) never call ``list_skills()`` at all, so a
test that only checked it would prove nothing about them.

The ``extra_dirs`` term is provider-dependent because Kiro's glob and OpenCode's
symlink are both rooted at ``SKILLS_DIR``. That asymmetry predates agent plugins;
Requirement 13 was amended to record it (Criteria 6 and 7, with Criteria 3-5
keeping their numbering), and it is documented under "Known Limitations" in
``docs/skills.md``.
"""

from __future__ import annotations

import glob
import json
import re
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Set
from unittest import mock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cli_agent_orchestrator.agent_plugins.installer import install
from cli_agent_orchestrator.agent_plugins.models import PluginSource
from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore
from cli_agent_orchestrator.models.provider import ProviderType

from .conftest import build_plugin, write_skill

ALL_PROVIDERS = [
    ProviderType.CLAUDE_CODE.value,
    ProviderType.CODEX.value,
    ProviderType.KIMI_CLI.value,
    ProviderType.ANTIGRAVITY_CLI.value,
    ProviderType.COPILOT_CLI.value,
    ProviderType.KIRO_CLI.value,
    ProviderType.OPENCODE_CLI.value,
]

_CATALOG_LINE = re.compile(r"^- \*\*(?P<name>[^*]+)\*\*:", re.MULTILINE)


def reachable_skill_names(provider: str, skills_dir: Path, opencode_dir: Path) -> Set[str]:
    """Skill names an agent on ``provider`` can actually reach.

    Each branch reads the artifact that provider genuinely consumes:

    * runtime-catalog providers → the catalog text ``terminal_service`` builds;
    * Copilot → the composed ``.agent.md`` body;
    * Kiro CLI → expansion of the ``skill://`` resource glob it is handed;
    * OpenCode → traversal of its ``skills`` config symlink.
    """
    if provider in {
        ProviderType.CLAUDE_CODE.value,
        ProviderType.CODEX.value,
        ProviderType.KIMI_CLI.value,
        ProviderType.ANTIGRAVITY_CLI.value,
    }:
        from cli_agent_orchestrator.utils.skills import build_skill_catalog

        return {m.group("name") for m in _CATALOG_LINE.finditer(build_skill_catalog())}

    if provider == ProviderType.COPILOT_CLI.value:
        from cli_agent_orchestrator.models.agent_profile import AgentProfile
        from cli_agent_orchestrator.utils.skill_injection import compose_agent_prompt

        composed = compose_agent_prompt(AgentProfile(name="w", description="d", prompt="p")) or ""
        return {m.group("name") for m in _CATALOG_LINE.finditer(composed)}

    if provider == ProviderType.KIRO_CLI.value:
        # The literal resources Kiro receives are skill://{SKILLS_DIR}/**/SKILL.md
        # and skill://{SKILLS_DIR}/*/SKILL.md. The union is what Kiro sees, so the
        # union is what the reachable set has to be computed from — the two-glob
        # rationale lives in TestKiroFilesystemGlob and at the emission site.
        matches = glob.glob(f"{skills_dir}/**/SKILL.md", recursive=True)
        matches += glob.glob(f"{skills_dir}/*/SKILL.md")
        return {Path(m).parent.name for m in matches}

    if provider == ProviderType.OPENCODE_CLI.value:
        skills_link = opencode_dir / "skills"
        if not skills_link.exists():
            return set()
        return {
            item.name
            for item in skills_link.iterdir()
            if item.is_dir() and (item / "SKILL.md").is_file()
        }

    raise AssertionError(f"unhandled provider: {provider}")


@contextmanager
def delivery_sandbox(base: Path):
    """Build and wire a complete, isolated skill world under ``base``.

    A context manager rather than only a fixture because the property test below
    needs a **fresh** world per Hypothesis example — a function-scoped fixture is
    created once for the whole test, so state from one example would leak into
    the next and the second install would collide on the plugin name.
    """
    skills_dir = base / "skills"
    skills_dir.mkdir(parents=True)
    extra_dir = base / "extra"
    extra_dir.mkdir()
    opencode_dir = base / "opencode"
    opencode_dir.mkdir()

    with ExitStack() as stack:
        stack.enter_context(
            mock.patch("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skills_dir)
        )
        stack.enter_context(
            mock.patch(
                "cli_agent_orchestrator.services.settings_service.get_extra_skill_dirs",
                lambda: [str(extra_dir)],
            )
        )
        stack.enter_context(
            mock.patch(
                "cli_agent_orchestrator.utils.opencode_config.OPENCODE_CONFIG_DIR", opencode_dir
            )
        )
        stack.enter_context(
            mock.patch("cli_agent_orchestrator.utils.opencode_config.SKILLS_DIR", skills_dir)
        )
        yield {
            "skills_dir": skills_dir,
            "extra_dir": extra_dir,
            "opencode_dir": opencode_dir,
            "store": InstalledPluginStore(base / "agent-plugins", base / "agent-plugin-data"),
            "tmp_path": base,
        }


@pytest.fixture
def delivery_world(tmp_path):
    """A fully wired skill world: built-ins, an extra dir, and a plugin store."""
    with delivery_sandbox(tmp_path / "world") as world:
        yield world


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_every_provider_reaches_the_full_union(delivery_world, provider):
    """The named, concrete case for all seven providers."""
    from cli_agent_orchestrator.utils.opencode_config import ensure_skills_symlink

    world = delivery_world
    write_skill(world["skills_dir"] / "builtin-skill", "builtin-skill")
    write_skill(world["extra_dir"] / "extra-skill", "extra-skill")

    source = build_plugin(
        world["tmp_path"] / "src", "delivery", skills=["plugin-skill-one", "plugin-skill-two"]
    )
    install(
        PluginSource(kind="path", location=str(source)),
        store=world["store"],
        skills_dir=world["skills_dir"],
        refresh_agents=False,
    )
    ensure_skills_symlink()

    expected = {"builtin-skill", "plugin-skill-one", "plugin-skill-two"}
    if provider not in (ProviderType.KIRO_CLI.value, ProviderType.OPENCODE_CLI.value):
        # Requirement 13.6: the catalog-scan providers include extra_dirs.
        # Requirement 13.7 excludes exactly Kiro and OpenCode, whose glob and
        # symlink are both rooted at SKILLS_DIR — pre-existing CAO behaviour for
        # every skill, plugin-provided or not, and not something projection
        # changes. This branch is the assertion Criterion 7 points back to.
        expected |= {"extra-skill"}

    assert reachable_skill_names(provider, world["skills_dir"], world["opencode_dir"]) == expected


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_invalid_plugin_skills_reach_no_provider(delivery_world, provider):
    """The union is over *valid* plugin skills; a skipped one reaches nobody."""
    from cli_agent_orchestrator.utils.opencode_config import ensure_skills_symlink

    world = delivery_world
    source = build_plugin(world["tmp_path"] / "src", "delivery", skills=["good-skill"])
    broken = source / "skills" / "broken-skill"
    broken.mkdir()
    (broken / "SKILL.md").write_text(
        "---\nname: mismatched\ndescription: d\n---\n\nx\n", encoding="utf-8"
    )

    install(
        PluginSource(kind="path", location=str(source)),
        store=world["store"],
        skills_dir=world["skills_dir"],
        refresh_agents=False,
    )
    ensure_skills_symlink()

    reachable = reachable_skill_names(provider, world["skills_dir"], world["opencode_dir"])
    assert "good-skill" in reachable
    assert "broken-skill" not in reachable
    assert "mismatched" not in reachable


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_removal_withdraws_the_skill_from_every_provider(delivery_world, provider):
    from cli_agent_orchestrator.agent_plugins.installer import uninstall
    from cli_agent_orchestrator.utils.opencode_config import ensure_skills_symlink

    world = delivery_world
    source = build_plugin(world["tmp_path"] / "src", "delivery", skills=["temporary-skill"])
    install(
        PluginSource(kind="path", location=str(source)),
        store=world["store"],
        skills_dir=world["skills_dir"],
        refresh_agents=False,
    )
    ensure_skills_symlink()
    assert "temporary-skill" in reachable_skill_names(
        provider, world["skills_dir"], world["opencode_dir"]
    )

    uninstall(
        "delivery", store=world["store"], skills_dir=world["skills_dir"], refresh_agents=False
    )

    assert "temporary-skill" not in reachable_skill_names(
        provider, world["skills_dir"], world["opencode_dir"]
    )


# --- Property 10 ------------------------------------------------------------


@given(
    builtins=st.integers(min_value=0, max_value=2),
    plugin_skills=st.integers(min_value=0, max_value=3),
    invalid_skills=st.integers(min_value=0, max_value=2),
)
@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_delivery_equivalence_across_providers(
    tmp_path_factory, builtins, plugin_skills, invalid_skills
):
    """Every provider's reachable set equals the same union, for any world.

    Note the `extra_dirs` term of the union is exercised by the concrete test
    above rather than here, because Requirements 13.6 and 13.7 make it
    provider-dependent: Kiro's glob and OpenCode's symlink are both rooted at
    ``SKILLS_DIR`` and so never see extra dirs — pre-existing CAO behaviour for
    every skill, not something projection changes. This property is about the
    term Criterion 13.2 makes universal, which is the term projection controls.
    """
    from cli_agent_orchestrator.utils.opencode_config import ensure_skills_symlink

    base = tmp_path_factory.mktemp("equivalence")
    with delivery_sandbox(base / "world") as world:
        builtin_names = {f"builtin-{i}" for i in range(builtins)}
        for name in builtin_names:
            write_skill(world["skills_dir"] / name, name)

        plugin_names = {f"plugin-{i}" for i in range(plugin_skills)}
        source = build_plugin(base / "src", "delivery", skills=sorted(plugin_names))
        for index in range(invalid_skills):
            broken = source / "skills" / f"broken-{index}"
            broken.mkdir(parents=True, exist_ok=True)
            (broken / "SKILL.md").write_text(
                "---\nname: nope\ndescription: d\n---\n\nx\n", encoding="utf-8"
            )

        install(
            PluginSource(kind="path", location=str(source)),
            store=world["store"],
            skills_dir=world["skills_dir"],
            refresh_agents=False,
        )
        ensure_skills_symlink()

        expected = builtin_names | plugin_names

        for provider in ALL_PROVIDERS:
            reachable = reachable_skill_names(provider, world["skills_dir"], world["opencode_dir"])
            assert reachable == expected, provider


# --- MCP delivery equivalence (W11) -----------------------------------------
#
# The skill half above asks "does every provider reach the same set of skill
# names". This half asks the same question of MCP servers, and it is a genuinely
# different question rather than a copy: skills are delivered by *projection into
# a shared store* every provider already reads, so the expected set is
# provider-independent by construction. MCP servers are delivered by *merging
# into each profile's `mcpServers`* with a provider-dependent transport matrix, so
# the expected set is a function of the provider — and the union has to be
# asserted per transport rather than once.


def mcp_document(**servers) -> str:
    from .conftest import MCP_SCHEMA_ID

    return json.dumps({"$schema": MCP_SCHEMA_ID, "mcpServers": servers}, indent=2)


def delivered_server_names(provider: str, store) -> Set[str]:
    """MCP server names an agent on ``provider`` would be configured with.

    Reads through ``mcp_delivery``, which is the single seam ``install_agent``
    uses, so this cannot drift from what a provider config actually receives the
    way a re-implemented merge would.
    """
    from cli_agent_orchestrator.agent_plugins.mcp_delivery import collect_plugin_mcp_servers

    return set(collect_plugin_mcp_servers(provider=provider, store=store).servers)


STDIO_SERVER = {"type": "stdio", "command": "demo-server"}
HTTP_SERVER = {"type": "streamable-http", "url": "https://example.test/mcp"}

#: The providers whose native MCP shape cannot carry a url-based entry, audited
#: against each serializer rather than assumed (review on #584 caught the
#: original single-entry set as wrong):
#:
#: * ``opencode_cli`` — `utils/opencode_config.translate_mcp_server_config`
#:   flattens an entry into `{"type": "local", "command": [...]}`.
#: * ``codex`` — the serializer emits only `command`/`args`/`env`/`env_vars` TOML
#:   overrides and has no `url` branch.
#: * ``antigravity_cli`` — `_register_mcp_servers` always writes a local
#:   `command`/`args`/`env` entry into `mcp_config.json`.
#:
#: In every case an HTTP server would arrive with an empty command — worse than
#: absent, because it would look configured.
STDIO_ONLY_PROVIDERS = {
    ProviderType.OPENCODE_CLI.value,
    ProviderType.CODEX.value,
    ProviderType.ANTIGRAVITY_CLI.value,
}


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_every_provider_receives_every_stdio_plugin_server(delivery_world, provider):
    """A stdio server is deliverable everywhere, so every provider gets it."""
    world = delivery_world
    source = build_plugin(
        world["tmp_path"] / "src",
        "delivery",
        skills=["plugin-skill"],
        mcp_text=mcp_document(**{"tools-one": STDIO_SERVER, "tools-two": STDIO_SERVER}),
    )
    install(
        PluginSource(kind="path", location=str(source)),
        store=world["store"],
        skills_dir=world["skills_dir"],
        refresh_agents=False,
    )

    assert delivered_server_names(provider, world["store"]) == {"tools-one", "tools-two"}


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_an_http_server_reaches_exactly_the_providers_that_can_carry_it(delivery_world, provider):
    """Requirement 18.7 — skipped for the narrowed provider, delivered elsewhere.

    This is the asymmetry that makes MCP delivery different from skill delivery,
    asserted rather than described: the same installed plugin yields a different
    delivered set depending on the provider, and that is correct.
    """
    world = delivery_world
    source = build_plugin(
        world["tmp_path"] / "src",
        "delivery",
        skills=["plugin-skill"],
        mcp_text=mcp_document(local=STDIO_SERVER, remote=HTTP_SERVER),
    )
    install(
        PluginSource(kind="path", location=str(source)),
        store=world["store"],
        skills_dir=world["skills_dir"],
        refresh_agents=False,
    )

    expected = {"local"} if provider in STDIO_ONLY_PROVIDERS else {"local", "remote"}
    assert delivered_server_names(provider, world["store"]) == expected


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_an_unusable_mcp_json_delivers_nothing_to_any_provider(delivery_world, provider):
    """§7.2.2.2 — MCP off for that plugin, uniformly, and skills unaffected."""
    from cli_agent_orchestrator.utils.opencode_config import ensure_skills_symlink

    world = delivery_world
    source = build_plugin(
        world["tmp_path"] / "src",
        "delivery",
        skills=["plugin-skill"],
        mcp_text="{ not json",
    )
    install(
        PluginSource(kind="path", location=str(source)),
        store=world["store"],
        skills_dir=world["skills_dir"],
        refresh_agents=False,
    )
    # OpenCode reaches skills through its own config symlink, which the install
    # path does not create; the skill-side tests above call this for the same
    # reason. Without it this test would "prove" skills were lost.
    ensure_skills_symlink()

    assert delivered_server_names(provider, world["store"]) == set()
    # The skill half of the same plugin is untouched by the MCP failure.
    assert "plugin-skill" in reachable_skill_names(
        provider, world["skills_dir"], world["opencode_dir"]
    )


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_removal_withdraws_the_server_from_every_provider(delivery_world, provider):
    """The MCP mirror of ``test_removal_withdraws_the_skill_from_every_provider``."""
    from cli_agent_orchestrator.agent_plugins.installer import uninstall

    world = delivery_world
    source = build_plugin(
        world["tmp_path"] / "src",
        "delivery",
        skills=["plugin-skill"],
        mcp_text=mcp_document(temporary=STDIO_SERVER),
    )
    install(
        PluginSource(kind="path", location=str(source)),
        store=world["store"],
        skills_dir=world["skills_dir"],
        refresh_agents=False,
    )
    assert "temporary" in delivered_server_names(provider, world["store"])

    uninstall(
        "delivery", store=world["store"], skills_dir=world["skills_dir"], refresh_agents=False
    )

    assert delivered_server_names(provider, world["store"]) == set()
