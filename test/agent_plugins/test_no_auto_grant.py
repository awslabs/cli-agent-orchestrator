"""A plugin install must not widen any agent's tool allowlist.

Issue #573 AC7, raised as a P2 by gutosantos82 in review 5074181308 on #584:

    Plugin MCP servers merge into EVERY managed agent's profile and
    `resolve_allowed_tools` appends `@<server>` to any non-`"*"` profile,
    silently widening deliberately-restricted roles (reviewer/supervisor) to
    include arbitrary plugin code.

The mechanism predates the plugin work -- it is how ``@cao-mcp-server`` reaches a
profile -- but its INPUT is new: ``apply_plugin_mcp_servers`` merges plugin servers
into ``profile.mcpServers`` before the grant site reads it.

The rule is fail-closed and deliberately not special-cased: role defaults already
enumerate what they trust **by name**, a plugin appears in no role default, so a
plugin-provided server is untrusted until a profile names it. Servers the profile
declares itself are unaffected.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from cli_agent_orchestrator.agent_plugins.installer import install as install_plugin
from cli_agent_orchestrator.agent_plugins.mcp_delivery import (
    apply_plugin_mcp_servers,
    grantable_server_names,
)
from cli_agent_orchestrator.agent_plugins.mcp_mapping import PRE_EXPANDED_KEY
from cli_agent_orchestrator.agent_plugins.models import PluginSource
from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "cli_agent_orchestrator"

#: CAO's own operator package, whose ``mcp.json`` declares exactly one server.
#: Used rather than a synthetic fixture because the defect is about what the
#: *real* install path does with a *real* plugin's servers.
OPERATOR_PACKAGE = REPO_ROOT / "agent-plugin" / "cao"
PLUGIN_SERVER = "cao-ops"


class _Profile:
    """Minimal stand-in: the grant sites read only these two attributes."""

    def __init__(self, mcp_servers, allowed=None, role=None):
        self.mcpServers = mcp_servers
        self.allowedTools = allowed
        self.role = role


def _plugin_entry(command="plugin-server"):
    return {"type": "stdio", "command": command, PRE_EXPANDED_KEY: True}


def _profile_entry(command="my-server"):
    return {"type": "stdio", "command": command}


class TestPluginServersAreNeverAutoGranted:
    """The reported defect, stated as the contract."""

    @pytest.mark.parametrize("role", ["supervisor", "reviewer", "developer"])
    def test_a_plugin_server_does_not_reach_a_role_default(self, role):
        """No role default gains a plugin tool, restricted or not.

        ``developer`` is included on purpose: the rule is "a plugin is untrusted
        until named", not "restricted roles are special". Exempting ``developer``
        would make the boundary depend on which role you happened to pick.
        """
        profile = _Profile({"plugin-tools": _plugin_entry()}, allowed=None, role=role)

        granted = resolve_allowed_tools(
            profile.allowedTools, profile.role, grantable_server_names(profile)
        )

        assert "@plugin-tools" not in granted

    def test_the_restricted_roles_keep_exactly_their_designed_posture(self):
        """The supervisor default omits fs_write and execute_bash by design.

        Its whole purpose is that the seat cannot do the work itself and must
        delegate; a plugin tool would invert that.
        """
        from cli_agent_orchestrator.constants import ROLE_TOOL_DEFAULTS

        for role in ("supervisor", "reviewer"):
            profile = _Profile({"plugin-tools": _plugin_entry()}, allowed=None, role=role)
            granted = resolve_allowed_tools(
                profile.allowedTools, profile.role, grantable_server_names(profile)
            )
            assert granted == list(ROLE_TOOL_DEFAULTS[role]), role

    def test_an_explicitly_authored_allowlist_is_not_appended_to(self):
        """The worst case of the defect: overriding a precise human decision.

        A profile that enumerated its tools stated exactly what it permits.
        """
        profile = _Profile(
            {"plugin-tools": _plugin_entry()}, allowed=["fs_read", "@cao-mcp-server"], role=None
        )

        granted = resolve_allowed_tools(
            profile.allowedTools, profile.role, grantable_server_names(profile)
        )

        assert granted == ["fs_read", "@cao-mcp-server"]


class TestNamingItExplicitlyStillWorks:
    """Fail-closed, not unusable: the operator retains a way to say yes."""

    def test_an_explicit_server_reference_grants_it(self):
        profile = _Profile(
            {"plugin-tools": _plugin_entry()},
            allowed=["fs_read", "@plugin-tools"],
            role=None,
        )
        granted = resolve_allowed_tools(
            profile.allowedTools, profile.role, grantable_server_names(profile)
        )
        assert "@plugin-tools" in granted

    def test_a_star_allowlist_still_grants_everything(self):
        """``"*"`` already means everything; the rule must not narrow it."""
        profile = _Profile({"plugin-tools": _plugin_entry()}, allowed=["*"], role=None)
        granted = resolve_allowed_tools(
            profile.allowedTools, profile.role, grantable_server_names(profile)
        )
        assert granted == ["*"]


class TestProfileAuthoredServersAreUnaffected:
    """Pre-existing behaviour, preserved exactly."""

    def test_a_profile_declared_server_is_still_auto_granted(self):
        """The operator wrote this server into their own profile.

        Granting it honours their instruction; that is not the defect.
        """
        profile = _Profile({"my-server": _profile_entry()}, allowed=None, role="developer")

        granted = resolve_allowed_tools(
            profile.allowedTools, profile.role, grantable_server_names(profile)
        )

        assert "@my-server" in granted

    def test_a_mixed_profile_grants_only_the_declared_one(self):
        """The discriminating case: both kinds present, one granted."""
        profile = _Profile(
            {"my-server": _profile_entry(), "plugin-tools": _plugin_entry()},
            allowed=None,
            role="developer",
        )

        granted = resolve_allowed_tools(
            profile.allowedTools, profile.role, grantable_server_names(profile)
        )

        assert "@my-server" in granted
        assert "@plugin-tools" not in granted

    def test_no_servers_at_all_returns_none(self):
        """Preserves the existing ``None`` idiom the grant sites relied on."""
        assert grantable_server_names(_Profile({})) is None
        assert grantable_server_names(_Profile(None)) is None

    def test_only_plugin_servers_also_returns_none(self):
        """Not an empty list: ``None`` is what "nothing to append" already meant."""
        assert grantable_server_names(_Profile({"plugin-tools": _plugin_entry()})) is None


class TestEveryGrantSiteUsesTheHelper:
    """The guard that stops this being reintroduced.

    Five call sites shared one hand-written idiom
    (``list(profile.mcpServers.keys()) if profile.mcpServers else None``). A sixth
    would silently reinstate the widening, and nothing would fail -- the same
    hand-maintained-duplication hazard that let the original defect ship. So the
    idiom is banned by scan rather than by convention.
    """

    #: Modules that resolve an allowlist and therefore must use the helper.
    GRANT_SITES = (
        "services/install_service.py",
        "services/terminal_service.py",
        "mcp_server/server.py",
        "utils/orchestration.py",
        "cli/commands/launch.py",
    )

    def test_no_grant_site_derives_server_names_by_hand(self):
        offenders = []
        for rel in self.GRANT_SITES:
            text = (SRC / rel).read_text(encoding="utf-8")
            if ".mcpServers.keys()" in text:
                offenders.append(rel)
        assert not offenders, (
            f"{offenders} derive MCP server names directly from profile.mcpServers, which "
            f"re-includes plugin-provided servers in the automatic allowlist append. Use "
            f"grantable_server_names(profile) instead (issue #573 AC7)."
        )

    def test_every_grant_site_imports_and_calls_the_helper(self):
        """Both, because calling without importing is a latent ``NameError``.

        A substring check for the call alone passed while one site had the call and
        no import -- caught here rather than at runtime on ``cao launch``. The
        import is verified through the AST so a name in a comment or docstring
        cannot satisfy it.
        """
        broken = []
        for rel in self.GRANT_SITES:
            text = (SRC / rel).read_text(encoding="utf-8")
            imported = any(
                isinstance(node, ast.ImportFrom)
                and any(alias.name == "grantable_server_names" for alias in node.names)
                for node in ast.walk(ast.parse(text))
            )
            called = "grantable_server_names(" in text
            if not (imported and called):
                broken.append(f"{rel} (imported={imported}, called={called})")
        assert not broken, f"AC7 helper not properly wired: {broken}"

    def test_the_helper_is_in_scope_at_every_call(self):
        """Bound where it is called, not merely imported somewhere in the file.

        ``terminal_service`` resolves an allowlist in two branches. Importing in
        only the first left the second raising ``UnboundLocalError`` at runtime
        while a whole-file import check still passed -- these modules import
        ``resolve_allowed_tools`` function-locally, so file-level presence proves
        nothing about scope.
        """
        broken = []
        for rel in self.GRANT_SITES:
            tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
            module_level = any(
                isinstance(node, ast.ImportFrom)
                and any(alias.name == "grantable_server_names" for alias in node.names)
                for node in tree.body
            )
            for func in ast.walk(tree):
                if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                calls = [
                    node
                    for node in ast.walk(func)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "grantable_server_names"
                ]
                if not calls:
                    continue
                local_import = any(
                    isinstance(node, ast.ImportFrom)
                    and any(alias.name == "grantable_server_names" for alias in node.names)
                    for node in ast.walk(func)
                )
                if not (module_level or local_import):
                    broken.append(f"{rel}::{func.name}")
        assert not broken, (
            f"grantable_server_names is called without being in scope in {broken} -- "
            f"an UnboundLocalError at runtime."
        )

    def test_the_scan_covers_every_resolve_allowed_tools_caller(self):
        """The site list is derived, so a NEW caller cannot escape the scan.

        Without this the two tests above would silently pass while a sixth grant
        site went unchecked -- which is precisely how the defect reached review.
        """
        callers = set()
        for path in SRC.rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "resolve_allowed_tools"
                    and node.args
                ):
                    callers.add(str(path.relative_to(SRC)))

        # `tool_mapping.py` defines it; every other caller must be a declared site.
        callers.discard("utils/tool_mapping.py")
        assert callers == set(self.GRANT_SITES), (
            f"resolve_allowed_tools callers {sorted(callers)} do not match the declared "
            f"grant sites {sorted(self.GRANT_SITES)}. Add the new site to GRANT_SITES and "
            f"make it use grantable_server_names()."
        )


# ---------------------------------------------------------------------------
# The integration half (R1.1, R1.2). Everything above tests the HELPER; these
# drive the real install path, which is the only place the defect lives.
# ---------------------------------------------------------------------------


class _Installer:
    """Handle onto one redirected install workspace."""

    def __init__(self, run, kiro_dir: Path, opencode_config_file: Path):
        self.run = run
        self._kiro_dir = kiro_dir
        self.opencode_config_file = opencode_config_file

    def kiro_json(self, name: str) -> dict:
        return json.loads((self._kiro_dir / f"{name}.json").read_text(encoding="utf-8"))

    def opencode_json(self) -> dict:
        return json.loads(self.opencode_config_file.read_text(encoding="utf-8"))

    def opencode_sidecar(self) -> dict:
        path = self.opencode_config_file.with_name("cao-grants.json")
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


@pytest.fixture
def plugin_in_default_store(skills_dir) -> InstalledPluginStore:
    """Install the real ``cao`` package into the **default** store.

    ``install_agent`` calls ``apply_plugin_mcp_servers`` with no ``store=``
    override, so the default-constructed store is the only one on the real path —
    installing into the ``store`` fixture's tree would leave the install path
    seeing no plugins at all and the test asserting nothing. conftest's autouse
    ``_never_touch_the_real_plugin_store`` has already pointed that default at a
    scratch tree, which is what makes constructing it here safe.
    """
    store = InstalledPluginStore()
    outcome = install_plugin(
        PluginSource(kind="path", location=str(OPERATOR_PACKAGE)),
        store=store,
        skills_dir=skills_dir,
        refresh_agents=False,
    )
    assert outcome.installed, outcome.report.findings
    return store


@pytest.fixture
def installer(plugin_in_default_store, skills_dir, tmp_path, monkeypatch):
    """Run a real ``install_agent`` against fully redirected paths.

    Every sink ``install_agent`` writes to is relocated under ``tmp_path``:
    without this the call reaches the operator's real ``~/.aws`` (the local
    profile store, the Kiro agent dir, and OpenCode's shared ``opencode.json``
    plus its grant sidecar).
    """
    local_store = tmp_path / "agent-store"
    context_dir = tmp_path / "agent-context"
    kiro_dir = tmp_path / "kiro-agents"
    opencode_dir = tmp_path / "opencode"
    for directory in (local_store, context_dir, kiro_dir, opencode_dir):
        directory.mkdir(parents=True, exist_ok=True)

    for target, value in (
        ("cli_agent_orchestrator.services.profile_store.LOCAL_AGENT_STORE_DIR", local_store),
        ("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", local_store),
        ("cli_agent_orchestrator.services.install_service.AGENT_CONTEXT_DIR", context_dir),
        ("cli_agent_orchestrator.services.install_service.KIRO_AGENTS_DIR", kiro_dir),
        ("cli_agent_orchestrator.services.install_service.SKILLS_DIR", skills_dir),
        (
            "cli_agent_orchestrator.services.install_service.OPENCODE_AGENTS_DIR",
            opencode_dir / "agents",
        ),
        (
            "cli_agent_orchestrator.utils.opencode_config.OPENCODE_CONFIG_FILE",
            opencode_dir / "opencode.json",
        ),
    ):
        monkeypatch.setattr(target, value)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.ensure_skills_symlink", lambda: None
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
    )

    from cli_agent_orchestrator.services.install_service import install_agent

    def _install(name: str, provider: str, *, allowed=None, role="developer"):
        allowed_block = ""
        if allowed is not None:
            # ``@cao-ops`` must be quoted: ``@`` cannot start a YAML token.
            allowed_block = "allowedTools:\n" + "".join(
                f"  - {json.dumps(tool)}\n" for tool in allowed
            )
        (local_store / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: Agent {name}\nrole: {role}\n"
            f"{allowed_block}---\nPrompt body.\n",
            encoding="utf-8",
        )
        result = install_agent(name, provider)
        assert result.success, result.message
        return result

    return _Installer(
        run=_install,
        kiro_dir=kiro_dir,
        opencode_config_file=opencode_dir / "opencode.json",
    )


class TestTheInstallPathActuallyExcludesThem:
    """R1.1 / R1.2 against the artifact ``install_agent`` writes.

    The 15 unit tests above pass on the broken tree because they construct
    ``_Profile`` objects whose plugin entries still carry ``PRE_EXPANDED_KEY`` —
    the one input shape the real path cannot produce at the grant site, because
    ``apply_plugin_mcp_servers`` strips the marker 35 lines earlier. Only an
    assertion on the emitted config can see the defect.
    """

    def test_kiro_delivers_the_plugin_server_without_granting_it(self, installer):
        """The discriminating pair: delivered, not granted."""
        installer.run("unnamed-agent", ProviderType.KIRO_CLI.value)

        emitted = installer.kiro_json("unnamed-agent")

        assert PLUGIN_SERVER in emitted["mcpServers"], "the server must still be DELIVERED"
        assert f"@{PLUGIN_SERVER}" not in emitted["allowedTools"], (
            f"@{PLUGIN_SERVER} was auto-granted into allowedTools "
            f"{emitted['allowedTools']} — the no-auto-grant rule is a no-op on the "
            f"install path (issue #573 AC7, G0)."
        )

    def test_kiro_still_grants_a_server_the_profile_names(self, installer):
        """R1.2 — the escape hatch has to survive the fix."""
        installer.run(
            "naming-agent",
            ProviderType.KIRO_CLI.value,
            allowed=["fs_read", f"@{PLUGIN_SERVER}"],
        )

        emitted = installer.kiro_json("naming-agent")

        assert PLUGIN_SERVER in emitted["mcpServers"]
        assert f"@{PLUGIN_SERVER}" in emitted["allowedTools"]

    def test_the_opencode_grant_sidecar_records_no_plugin_grant(self, installer):
        """The same rule on the provider that persists what it granted.

        OpenCode's per-agent ``tools`` map is its ``@<server>`` equivalent, and
        ``cao-grants.json`` is CAO's record of the keys it wrote there. A rule
        that holds for Kiro and not for OpenCode is not a rule.
        """
        installer.run("unnamed-agent", ProviderType.OPENCODE_CLI.value)

        config = installer.opencode_json()
        sidecar = installer.opencode_sidecar()

        assert PLUGIN_SERVER in config["mcp"], "the server must still be DELIVERED"
        recorded = sidecar.get("agents", {}).get("unnamed-agent", [])
        assert f"{PLUGIN_SERVER}*" not in recorded, (
            f"cao-grants.json records {recorded} for an agent that never named "
            f"{PLUGIN_SERVER} — the plugin tool was granted through OpenCode's "
            f"agent.<id>.tools map."
        )
        agent_tools = config.get("agent", {}).get("unnamed-agent", {}).get("tools", {})
        assert f"{PLUGIN_SERVER}*" not in agent_tools, agent_tools

    def test_opencode_still_grants_a_server_the_profile_names(self, installer):
        """R1.2 on the sidecar side."""
        installer.run(
            "naming-agent",
            ProviderType.OPENCODE_CLI.value,
            allowed=["fs_read", f"@{PLUGIN_SERVER}"],
        )

        config = installer.opencode_json()
        sidecar = installer.opencode_sidecar()

        assert f"{PLUGIN_SERVER}*" in sidecar["agents"]["naming-agent"]
        assert config["agent"]["naming-agent"]["tools"][f"{PLUGIN_SERVER}*"] is True


class TestADocumentedGlobGrantIsHonored:
    """``docs/agent-plugins.md:207`` promises ``"*"``, ``@server-name``, **or a
    matching glob**, and shows ``@plugin-*`` at line 225.

    Both grant sites implemented exact membership only, so the glob an operator
    is told to write authorized nothing: OpenCode wrote the server into the
    shared ``mcp`` section and enabled it, then granted the agent no tool alias
    for it.

    This does NOT restore automatic granting (issue #573 AC7). A pattern still
    has to be written into the profile by a human, and it is expanded against
    the concrete DELIVERED names -- a plugin install on its own still widens
    nothing, which is what the ``TestTheInstallPathActuallyExcludesThem`` cases
    above continue to assert.
    """

    def test_opencode_grants_a_plugin_server_a_glob_names(self, installer):
        """The reported defect, on the artifact OpenCode is launched from."""
        installer.run(
            "glob-agent",
            ProviderType.OPENCODE_CLI.value,
            allowed=["fs_read", "@cao-op*"],
        )

        config = installer.opencode_json()

        # Precondition: delivery is not what is broken, the grant is.
        assert PLUGIN_SERVER in config["mcp"]
        agent_tools = config.get("agent", {}).get("glob-agent", {}).get("tools", {})
        assert agent_tools.get(f"{PLUGIN_SERVER}*") is True, (
            f"the documented @cao-op* grant authorized nothing: agent.glob-agent.tools is "
            f"{agent_tools} while {PLUGIN_SERVER} was written and enabled "
            f"(docs/agent-plugins.md:207)"
        )
        assert f"{PLUGIN_SERVER}*" in installer.opencode_sidecar()["agents"]["glob-agent"]

    def test_kiro_grants_a_plugin_server_a_glob_names(self, installer):
        """The same rule on the provider whose grant is the allowlist itself.

        Kiro's emitted ``allowedTools`` carries the pattern verbatim, so the
        claim here is that the pattern reaches the artifact alongside the
        delivered server -- the concrete expansion is the launching provider's,
        and Grok's is asserted in ``test/providers/test_grok_cli_unit.py``.
        """
        installer.run(
            "glob-agent-kiro",
            ProviderType.KIRO_CLI.value,
            allowed=["fs_read", "@cao-op*"],
        )

        emitted = installer.kiro_json("glob-agent-kiro")

        assert PLUGIN_SERVER in emitted["mcpServers"]
        assert "@cao-op*" in emitted["allowedTools"]

    def test_a_non_matching_glob_still_denies(self, installer):
        """The control that keeps the fix from being "grant on any pattern"."""
        installer.run(
            "other-glob-agent",
            ProviderType.OPENCODE_CLI.value,
            allowed=["fs_read", "@other-*"],
        )

        config = installer.opencode_json()

        assert PLUGIN_SERVER in config["mcp"], "the server must still be DELIVERED"
        agent_tools = config.get("agent", {}).get("other-glob-agent", {}).get("tools", {})
        assert f"{PLUGIN_SERVER}*" not in agent_tools, agent_tools
        assert f"{PLUGIN_SERVER}*" not in installer.opencode_sidecar().get("agents", {}).get(
            "other-glob-agent", []
        )

    def test_the_glob_grant_is_case_sensitive(self, installer):
        """``@CAO-OP*`` must not match ``cao-ops`` on any platform.

        ``fnmatch.fnmatch`` case-folds wherever ``os.path.normcase`` does, so a
        case-insensitive host would widen this grant. The rule uses
        ``fnmatchcase``.
        """
        installer.run(
            "shouty-agent",
            ProviderType.OPENCODE_CLI.value,
            allowed=["fs_read", "@CAO-OP*"],
        )

        config = installer.opencode_json()
        agent_tools = config.get("agent", {}).get("shouty-agent", {}).get("tools", {})

        assert f"{PLUGIN_SERVER}*" not in agent_tools, agent_tools

    def test_a_glob_never_grants_a_server_that_was_not_delivered(self, installer):
        """The negative case that matters: expansion is over concrete names.

        A grant derived from the pattern rather than from the delivered set
        would write a tool key for a server that does not exist -- pre-authorizing
        whatever later answered to that name.
        """
        installer.run(
            "ghost-agent",
            ProviderType.OPENCODE_CLI.value,
            allowed=["fs_read", "@ghost-*"],
        )

        config = installer.opencode_json()
        written = set(config.get("mcp", {}))
        granted = set(config.get("agent", {}).get("ghost-agent", {}).get("tools", {}))

        assert not any(key.startswith("ghost-") for key in granted), granted
        # Every granted key must name a server that was actually written.
        assert {key.rstrip("*") for key in granted} <= written, (granted, written)


class TestTheHelperCannotBeDefeatedByOrdering:
    """R1.3 / R1.4 — the direct regression guard for G0.

    The defect was not "the helper is wrong". The helper was correct and asked a
    witness the preceding call had deleted. So the guard has to assert the helper
    answers correctly *on post-strip entries*, which is the only shape the
    install path can hand it.
    """

    def test_delivered_is_the_authority_when_every_marker_is_gone(self, plugin_in_default_store):
        profile = _Profile({"my-server": _profile_entry()}, allowed=None, role="developer")

        delivered = apply_plugin_mcp_servers(profile, provider=ProviderType.KIRO_CLI.value)

        # Precondition, not a side assertion: if the marker survived, this test
        # would pass for the wrong reason and G0 would be invisible again.
        assert PLUGIN_SERVER in profile.mcpServers
        assert not any(
            PRE_EXPANDED_KEY in entry for entry in profile.mcpServers.values()
        ), "apply_plugin_mcp_servers is expected to have stripped every marker"

        assert grantable_server_names(profile, delivered=delivered) == ["my-server"]

    def test_omitting_delivered_degrades_the_helper_to_the_defect(self, plugin_in_default_store):
        """Pins *why* the parameter is required rather than optional-in-practice.

        A caller that drops ``delivered=`` degrades to the broken behaviour, so
        the exclusion cannot be presented as unconditional. Asserting the
        degradation keeps the next reader from assuming the marker check alone
        covers the install path.

        What this does NOT cover: it calls the helper directly with the argument
        already omitted and never executes ``install_service``, so it cannot
        detect ``delivered=`` going missing at a real call site -- it passes
        unchanged if the kwarg is dropped at ``install_service.py:590``. The two
        cases in ``TestTheInstallPathActuallyExcludesThem`` --
        ``test_kiro_delivers_the_plugin_server_without_granting_it`` and
        ``test_the_opencode_grant_sidecar_records_no_plugin_grant`` -- are what
        fail on that, verified by mutation. Named for the helper for that reason:
        a reader greps this file asking what protects the grant site, and the
        earlier name answered that question wrongly.
        """
        profile = _Profile({"my-server": _profile_entry()}, allowed=None, role="developer")
        apply_plugin_mcp_servers(profile, provider=ProviderType.KIRO_CLI.value)

        assert PLUGIN_SERVER in (grantable_server_names(profile) or [])

    def test_owners_and_server_names_stay_in_parity(self, plugin_in_default_store):
        """R1.5 — the exclusion set is derived from one of a paired assignment.

        ``servers`` and ``owners`` are written together at
        ``mcp_delivery.py:192-193``, so they cannot differ today. If they ever
        do, the fix silently *under*-excludes and the auto-grant returns with
        nothing failing — so the pairing is pinned rather than trusted.
        """
        profile = _Profile({}, allowed=None, role="developer")

        delivered = apply_plugin_mcp_servers(profile, provider=ProviderType.KIRO_CLI.value)

        assert set(delivered.owners) == set(delivered.server_names)
        assert set(delivered.servers) == set(delivered.server_names)
