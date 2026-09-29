"""Plugin MCP servers must appear in each provider's REAL launch artifact.

Reproduced by review on #584, and the reason this file exists at all:

> "the all-provider tests need to inspect real launch commands rather than
> treating ``collect_plugin_mcp_servers()`` as delivery."

The pre-existing equivalence suite asked ``mcp_delivery`` what it *would*
deliver. That is a tautology with respect to the actual defect: the merge ran in
``install_service`` against an in-memory profile, ``_write_context_file``
persisted the untouched raw text, and Claude Code, Codex, Kimi, Antigravity and
Cursor each called ``load_agent_profile()`` **again** at launch — so the merged
entry was gone by the time the command was built. Copilot never consulted the
profile for MCP at all. Every assertion here therefore reads the command string
or config file the provider really produces.

Mutation-verified: removing the ``with_plugin_mcp`` wrapper from a provider makes
that provider's case fail.
"""

from __future__ import annotations

import ast
import json
import shlex
from pathlib import Path
from typing import NamedTuple

import pytest

from cli_agent_orchestrator.agent_plugins.installer import install
from cli_agent_orchestrator.agent_plugins.mcp_mapping import PROVIDER_TRANSPORTS
from cli_agent_orchestrator.agent_plugins.models import PluginSource
from cli_agent_orchestrator.models.provider import ProviderType

from .conftest import build_plugin

PLUGIN_SERVER = "plugin-tools"
MCP_DOC = json.dumps(
    {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
        "mcpServers": {
            PLUGIN_SERVER: {"type": "stdio", "command": "demo-server", "args": ["--serve"]}
        },
    }
)


@pytest.fixture
def installed_plugin(store, skills_dir, tmp_path, monkeypatch):
    """Install a plugin declaring one stdio MCP server, with stores redirected."""
    monkeypatch.setattr("cli_agent_orchestrator.agent_plugins.projection.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGINS_DIR", tmp_path)

    source = build_plugin(
        tmp_path / "plugin-src", "mcpdonor", skills=["donor-skill"], mcp_text=MCP_DOC
    )
    install(
        PluginSource(kind="path", location=str(source)),
        store=store,
        skills_dir=skills_dir,
        refresh_agents=False,
    )
    # The delivery seam resolves the store from module state, so point it at ours.
    monkeypatch.setattr(
        "cli_agent_orchestrator.agent_plugins.mcp_delivery.InstalledPluginStore",
        lambda *a, **k: store,
    )
    return store


def _profile_stub(name: str = "worker"):
    """A minimal real AgentProfile — not a MagicMock, so serializers behave."""
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    return AgentProfile(name=name, description="d", system_prompt="p")


def _token_after(command: str, flag: str) -> str | None:
    """The argument following ``flag`` in ``command``, or ``None``."""
    tokens = shlex.split(command)
    for index, token in enumerate(tokens[:-1]):
        if token == flag:
            return tokens[index + 1]
    return None


def _tree_text(root: Path) -> str:
    """Every file under ``root`` concatenated, for artifact assertions."""
    return "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(root.rglob("*"))
        if p.is_file()
    )


def _cursor_artifact(provider, command: str) -> str:
    """Cursor injects MCP config as a *directory* passed with ``--plugin-dir``.

    Not a ``.json`` token, so the generic command/JSON search never looked inside
    it — this case only appeared to pass because the old blanket ``pytest.skip``
    swallowed the environment error before any assertion ran.
    """
    plugin_dir = _token_after(command, "--plugin-dir")
    assert plugin_dir, f"cursor built no --plugin-dir argument: {command}"
    return _tree_text(Path(plugin_dir))


def _omp_artifact(provider, command: str) -> str:
    """OMP writes an extension root and passes it with ``--extension``."""
    extension_dir = _token_after(command, "--extension")
    assert extension_dir, f"omp built no --extension argument: {command}"
    return (Path(extension_dir) / ".mcp.json").read_text(encoding="utf-8")


def _grok_artifact(provider, command: str) -> str:
    """Grok writes a TOML config into its CAO-managed private home."""
    assert provider.grok_home is not None, "grok built no managed home"
    return (provider.grok_home / "config.toml").read_text(encoding="utf-8")


def _minimax_artifact(provider, command: str) -> str:
    """MiniMax writes a plugin manifest plus a servers document under its data dir."""
    data_dir = provider._data_dir_path()
    return (data_dir / "plugins" / "cao-orchestrator" / "servers.mcp.json").read_text(
        encoding="utf-8"
    )


def _delivered_somewhere(command: str, server_name: str) -> bool:
    """Whether ``server_name`` reaches the provider, inline or by referenced file.

    Providers split two ways and both count as a real launch artifact: some
    inline the MCP config into the command (Codex's ``-c`` overrides, Kimi's
    ``--mcp-config <json>``), others write a file and pass its path (Claude
    Code's ``--mcp-config <path>``). Asserting only on the command string would
    give a false negative for the second group, so any referenced ``.json`` the
    command names is read and searched too.
    """
    if server_name in command:
        return True
    for token in shlex.split(command):
        if not token.endswith(".json"):
            continue
        candidate = Path(token)
        if candidate.is_file() and server_name in candidate.read_text(encoding="utf-8"):
            return True
    return False


class _LaunchCase(NamedTuple):
    """How to drive one provider's launch-artifact build.

    ``artifact`` reads the file the provider actually wrote, for providers whose
    delivery is a side effect of building the command rather than something
    visible in the command string. ``None`` falls back to searching the command
    and any ``.json`` it names.
    """

    module: str
    cls: str
    builder: str
    artifact: object = None


#: Providers whose plugin delivery is asserted by the parametrized case below.
#:
#: Keyed by ``ProviderType`` **value**, which is what
#: ``PROVIDER_TRANSPORTS`` and every ``_with_plugin_mcp(...)`` call site use —
#: note ``mcode``, not ``minimax_code``: the enum value and the module name differ
#: for that one provider, and getting it wrong silently wires a provider to a
#: transport row that does not exist (it would fall through to the stdio-only
#: default). The module name is therefore carried explicitly.
LAUNCH_CASES: dict[str, _LaunchCase] = {
    "claude_code": _LaunchCase("claude_code", "ClaudeCodeProvider", "_build_claude_command"),
    "codex": _LaunchCase("codex", "CodexProvider", "_build_codex_command"),
    "kimi_cli": _LaunchCase("kimi_cli", "KimiCliProvider", "_build_kimi_command"),
    "cursor_cli": _LaunchCase(
        "cursor_cli", "CursorCliProvider", "_build_cursor_command", _cursor_artifact
    ),
    "omp": _LaunchCase("omp", "OmpProvider", "_build_omp_command", _omp_artifact),
    "grok_cli": _LaunchCase("grok_cli", "GrokCliProvider", "_build_grok_command", _grok_artifact),
    "mcode": _LaunchCase(
        "minimax_code", "MiniMaxCodeProvider", "_build_command", _minimax_artifact
    ),
}

#: Delivering providers covered by a bespoke test in this file instead, because
#: their artifact is not a command string.
BESPOKE_LAUNCH_CASES = frozenset({"copilot_cli", "antigravity_cli"})

#: Delivering providers that persist MCP config at ``cao install`` time and never
#: re-read the profile at launch, so there is no launch artifact to inspect.
#: Covered by ``test_mcp_delivery.py`` against the real written config file.
INSTALL_TIME_DELIVERY = frozenset({"kiro_cli", "opencode_cli"})


class TestEveryDeliveringProviderIsCovered:
    """The matrix is *derived*, so a new provider cannot be added and forgotten.

    Reproduced by review 3 on #584: ``omp``, ``grok_cli`` and ``mcode`` build MCP
    configuration from a profile they re-read at launch, exactly like the five
    providers the previous review fixed, but were never wired to
    ``with_plugin_mcp`` — and nothing failed, because the hand-written case list
    simply did not mention them. Enumerating from ``PROVIDER_TRANSPORTS`` turns
    that silence into a failure.
    """

    def test_no_delivering_provider_lacks_a_case(self):
        delivering = {name for name, transports in PROVIDER_TRANSPORTS.items() if transports}
        covered = set(LAUNCH_CASES) | BESPOKE_LAUNCH_CASES | INSTALL_TIME_DELIVERY
        assert delivering <= covered, (
            f"provider(s) {sorted(delivering - covered)} declare deliverable MCP "
            f"transports but no test asserts a plugin server reaches them"
        )

    def test_no_case_names_a_provider_that_delivers_nothing(self):
        """The converse: a case for a provider with an empty transport row is dead."""
        covered = set(LAUNCH_CASES) | BESPOKE_LAUNCH_CASES | INSTALL_TIME_DELIVERY
        for name in sorted(covered):
            assert PROVIDER_TRANSPORTS.get(name), (
                f"{name} is covered by a delivery test but its PROVIDER_TRANSPORTS "
                f"row is empty/absent, so the test cannot be asserting delivery"
            )

    def test_every_provider_type_has_a_transport_row(self):
        """No provider may inherit the stdio-only default by accident."""
        missing = [p.value for p in ProviderType if p.value not in PROVIDER_TRANSPORTS]
        assert not missing, (
            f"provider(s) {missing} have no PROVIDER_TRANSPORTS row and would "
            f"silently inherit DEFAULT_TRANSPORTS"
        )

    def test_every_provider_type_is_classified_for_launch_delivery(self):
        """Every provider is in **exactly** one bucket, so none can be forgotten."""
        no_mcp_path = {name for name, transports in PROVIDER_TRANSPORTS.items() if not transports}
        buckets = {
            "LAUNCH_CASES": set(LAUNCH_CASES),
            "BESPOKE_LAUNCH_CASES": set(BESPOKE_LAUNCH_CASES),
            "INSTALL_TIME_DELIVERY": set(INSTALL_TIME_DELIVERY),
            "no MCP path": no_mcp_path,
        }
        # Union: nothing unclassified, nothing stale.
        classified: set = set().union(*buckets.values())
        assert classified == {p.value for p in ProviderType}

        # Disjointness, asserted rather than assumed. "Exactly one" is what this
        # test's name claims and union equality alone does not establish it -- a
        # provider in two buckets would be checked under two contradictory rules.
        names = sorted(buckets)
        for index, left in enumerate(names):
            for right in names[index + 1 :]:
                overlap = buckets[left] & buckets[right]
                assert not overlap, f"{sorted(overlap)} is in both {left} and {right}"


#: Module name → the ``ProviderType`` value that module must pass to
#: ``_with_plugin_mcp``. ``mcode``/``minimax_code`` is the one place these differ,
#: which is exactly the mistake the guard below exists to catch.
_PROVIDER_MODULE_VALUES = {
    "antigravity_cli": "antigravity_cli",
    "claude_code": "claude_code",
    "codex": "codex",
    "copilot_cli": "copilot_cli",
    "cursor_cli": "cursor_cli",
    "grok_cli": "grok_cli",
    "kimi_cli": "kimi_cli",
    "minimax_code": "mcode",
    "omp": "omp",
}


def _provider_key_of(node: ast.expr | None) -> str | None:
    """The provider key a ``_with_plugin_mcp`` second argument denotes, statically.

    Two spellings are accepted and both are resolved to the same string, because
    both appear in the tree and both are correct:

    * a literal — ``"kimi_cli"`` (what the six providers wired by the previous
      review use), and
    * the enum — ``ProviderType.MINIMAX_CODE.value``, which is the safer spelling
      precisely where it matters: that member's value is ``"mcode"``, so a
      hand-typed ``"minimax_code"`` would silently miss its transport row.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    # ProviderType.<MEMBER>.value
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "value"
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "ProviderType"
    ):
        member = getattr(ProviderType, node.value.attr, None)
        return member.value if member is not None else None
    return None


class TestEveryProfileLoadIsWrapped:
    """An AST guard: a provider that reads ``mcpServers`` must wrap its profile load.

    Reproduced by review 3 on #584. The previous review wired six providers by
    hand; three more with the same shape (``omp``, ``grok_cli``, ``minimax_code``)
    were added to the tree and nothing noticed, because "did you remember to wrap
    it" was a convention rather than a rule. Parsed rather than grepped: the
    providers' docstrings discuss ``load_agent_profile`` and ``mcpServers`` in
    prose, and a substring match would flag the very comments that explain the
    seam.

    Modules that never mention ``mcpServers`` are exempt automatically — they
    build no MCP configuration, so there is nothing for the merge to feed.
    """

    def _violations(self) -> list[str]:
        providers_dir = (
            Path(__file__).resolve().parents[2] / "src" / "cli_agent_orchestrator" / "providers"
        )
        problems: list[str] = []

        for module_path in sorted(providers_dir.glob("*.py")):
            source = module_path.read_text(encoding="utf-8")
            if "mcpServers" not in source:
                continue
            stem = module_path.stem
            expected = _PROVIDER_MODULE_VALUES.get(stem)
            tree = ast.parse(source, filename=str(module_path))

            # Every `_with_plugin_mcp(load_agent_profile(...), "<value>")` call,
            # recorded by the position of its inner load.
            wrapped: dict[tuple[int, int], str | None] = {}
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                    continue
                if node.func.id != "_with_plugin_mcp" or not node.args:
                    continue
                inner = node.args[0]
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name):
                    if inner.func.id == "load_agent_profile":
                        second = node.args[1] if len(node.args) > 1 else None
                        wrapped[(inner.lineno, inner.col_offset)] = _provider_key_of(second)

            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                    continue
                if node.func.id != "load_agent_profile":
                    continue
                key = (node.lineno, node.col_offset)
                if key not in wrapped:
                    problems.append(
                        f"{module_path.name}:{node.lineno} load_agent_profile() is not "
                        f"wrapped in _with_plugin_mcp()"
                    )
                elif expected is None:
                    problems.append(
                        f"{module_path.name}:{node.lineno} module is not in "
                        f"_PROVIDER_MODULE_VALUES, so its provider key cannot be checked"
                    )
                elif wrapped[key] != expected:
                    problems.append(
                        f"{module_path.name}:{node.lineno} passes "
                        f"{wrapped[key]!r} to _with_plugin_mcp, expected {expected!r} "
                        f"(the ProviderType value)"
                    )
        return problems

    def test_no_provider_reads_mcp_servers_from_an_unwrapped_profile(self):
        problems = self._violations()
        assert not problems, "unwrapped or mis-keyed profile loads:\n" + "\n".join(problems)


class TestTheLaunchCommandCarriesThePluginServer:
    @pytest.mark.parametrize("provider_key", sorted(LAUNCH_CASES))
    def test_the_built_command_mentions_the_plugin_server(
        self, installed_plugin, monkeypatch, tmp_path, provider_key
    ):
        import importlib

        case = LAUNCH_CASES[provider_key]
        mod = importlib.import_module(f"cli_agent_orchestrator.providers.{case.module}")
        provider_cls = getattr(mod, case.cls)

        monkeypatch.setattr(mod, "load_agent_profile", lambda _name: _profile_stub())
        # A synthetic environment rather than a skip. The old blanket
        # `except Exception: pytest.skip(...)` meant a provider whose build raised
        # for ANY reason — including a genuine delivery defect — reported green,
        # which is how three unwired providers stayed hidden. Give the provider a
        # private home and a resolvable binary; anything still raising is a real
        # failure and is reported as one.
        home = tmp_path / "cao-home" / provider_key
        home.mkdir(parents=True)
        if hasattr(mod, "CAO_HOME_DIR"):
            monkeypatch.setattr(mod, "CAO_HOME_DIR", home)
        if hasattr(mod, "shutil"):
            monkeypatch.setattr(mod.shutil, "which", lambda _name: f"/usr/bin/{_name}")

        # MiniMax seeds its private data dir from the user's real ~/.minimax
        # unless told otherwise; point it at an empty directory.
        minimax_seed = tmp_path / "minimax-seed"
        minimax_seed.mkdir()
        monkeypatch.setenv("MINIMAX_DATA_DIR", str(minimax_seed))

        provider = provider_cls("tid-1", "sess", "win", "worker")
        try:
            command = getattr(provider, case.builder)()
            if case.artifact is not None:
                written = case.artifact(provider, command)
                assert PLUGIN_SERVER in written, (
                    f"{provider_key} wrote a launch artifact without the plugin MCP "
                    f"server; plugin delivery does not reach this provider.\n{written}"
                )
            else:
                assert _delivered_somewhere(command, PLUGIN_SERVER), (
                    f"{provider_key} built a launch command without the plugin MCP "
                    f"server; plugin delivery does not reach this provider"
                )
        finally:
            cleanup = getattr(provider, "cleanup", None)
            if callable(cleanup):
                try:
                    cleanup()
                except Exception:  # pragma: no cover - best-effort teardown
                    pass

    def test_copilot_runtime_mcp_config_includes_the_plugin_server(
        self, installed_plugin, monkeypatch
    ):
        """Copilot's runtime config is the only MCP config it reads."""
        from cli_agent_orchestrator.providers import copilot_cli as mod

        monkeypatch.setattr(mod, "load_agent_profile", lambda _name: _profile_stub())
        provider = mod.CopilotCliProvider("tid-2", "sess", "win", "worker")

        raw = provider._build_runtime_mcp_config()
        servers = json.loads(raw)["mcpServers"]

        assert "cao-mcp-server" in servers, "CAO's own in-session server must remain"
        assert PLUGIN_SERVER in servers, (
            "Copilot's runtime MCP config omitted the plugin server, so plugin "
            "MCP delivery never reaches Copilot"
        )

    def test_antigravity_writes_the_plugin_server_into_its_shared_config(
        self, installed_plugin, monkeypatch, tmp_path
    ):
        """Antigravity delivers via a config file rather than the command line."""
        from cli_agent_orchestrator.providers import antigravity_cli as mod

        config_path = tmp_path / "gemini" / "config" / "mcp_config.json"
        monkeypatch.setattr(mod, "load_agent_profile", lambda _name: _profile_stub())
        monkeypatch.setattr(
            mod.AntigravityCliProvider, "_mcp_config_path", lambda self: config_path
        )

        provider = mod.AntigravityCliProvider("tid-3", "sess", "win", "worker")
        profile = mod._with_plugin_mcp(_profile_stub(), "antigravity_cli")
        assert profile.mcpServers and PLUGIN_SERVER in profile.mcpServers

        provider._register_mcp_servers(profile.mcpServers)

        written = json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"]
        assert any(key.startswith(PLUGIN_SERVER) for key in written), (
            f"antigravity wrote {sorted(written)} — no plugin server reached " f"mcp_config.json"
        )


class TestGrokCarriesAnHttpPluginServer:
    """The transport-vocabulary mismatch, at the real artifact.

    Listing ``grok_cli`` as carrying all transports is only safe because
    ``GROK_URL_TRANSPORTS`` translates CAO's ``streamable-http`` into Grok's
    ``http``. Without it, a schema-valid plugin server raises ``ProviderError``
    out of ``_render_mcp_config`` — during terminal creation, so the agent does
    not launch at all. The stdio fixture used by the matrix above cannot see this,
    because it never takes the url branch.
    """

    def test_a_streamable_http_plugin_server_reaches_grok_as_http(
        self, store, skills_dir, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "cli_agent_orchestrator.agent_plugins.projection.SKILLS_DIR", skills_dir
        )
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skills_dir)
        monkeypatch.setattr(
            "cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGINS_DIR", tmp_path
        )

        http_doc = json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                "mcpServers": {
                    PLUGIN_SERVER: {
                        "type": "streamable-http",
                        "url": "https://mcp.example.invalid/x",
                    }
                },
            }
        )
        source = build_plugin(
            tmp_path / "http-src", "httpdonor", skills=["donor-skill"], mcp_text=http_doc
        )
        install(
            PluginSource(kind="path", location=str(source)),
            store=store,
            skills_dir=skills_dir,
            refresh_agents=False,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.agent_plugins.mcp_delivery.InstalledPluginStore",
            lambda *a, **k: store,
        )

        from cli_agent_orchestrator.providers import grok_cli as mod

        monkeypatch.setattr(mod, "CAO_HOME_DIR", tmp_path / "cao-home")
        monkeypatch.setattr(mod, "load_agent_profile", lambda _name: _profile_stub())
        monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/grok")

        provider = mod.GrokCliProvider("tid-grok", "sess", "win", "worker")
        try:
            provider._build_grok_command()
            written = (provider.grok_home / "config.toml").read_text(encoding="utf-8")
        finally:
            provider.cleanup()

        assert f'[mcp_servers."{PLUGIN_SERVER}"]' in written, written
        assert 'type = "http"' in written, written
        assert "streamable-http" not in written, written


class TestKimiCarriesTheDeclaredTransport:
    """Reported by review 5222539218 on #584 (item 5), at the real artifact.

    The matrix fixture is stdio, so it cannot see the remote-transport swap. This
    publishes an SSE server on a path that does NOT end in ``/sse``, which is
    precisely the case FastMCP's ``infer_transport_type_from_url`` gets wrong when
    ``transport`` is absent: it would start a declared SSE server as Streamable
    HTTP.
    """

    def test_an_sse_plugin_server_reaches_kimi_as_transport_sse(
        self, store, skills_dir, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "cli_agent_orchestrator.agent_plugins.projection.SKILLS_DIR", skills_dir
        )
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skills_dir)
        monkeypatch.setattr(
            "cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGINS_DIR", tmp_path
        )

        sse_doc = json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                "mcpServers": {
                    PLUGIN_SERVER: {"type": "sse", "url": "https://mcp.example.invalid/events"}
                },
            }
        )
        source = build_plugin(
            tmp_path / "sse-src", "ssedonor", skills=["donor-skill"], mcp_text=sse_doc
        )
        install(
            PluginSource(kind="path", location=str(source)),
            store=store,
            skills_dir=skills_dir,
            refresh_agents=False,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.agent_plugins.mcp_delivery.InstalledPluginStore",
            lambda *a, **k: store,
        )

        from cli_agent_orchestrator.providers import kimi_cli as mod

        monkeypatch.setattr(mod, "load_agent_profile", lambda _name: _profile_stub())
        provider = mod.KimiCliProvider("tid-kimi", "sess", "win", "worker")
        monkeypatch.setattr(mod.Path, "home", classmethod(lambda cls: tmp_path / "kimi-home"))
        if True:
            command = provider._build_kimi_command()

        tokens = shlex.split(command)
        doc = json.loads(tokens[tokens.index("--mcp-config") + 1])
        assert doc[PLUGIN_SERVER]["transport"] == "sse", doc
        assert "type" not in doc[PLUGIN_SERVER], doc


class TestTheProfileItselfIsUnchangedOnDisk:
    def test_delivery_is_recomputed_not_persisted(self, installed_plugin, monkeypatch):
        """The profile source must not gain the expanded absolute paths.

        The whole reason delivery is applied on read: a persisted copy of the
        expanded ``${PLUGIN_ROOT}`` paths goes stale when the store moves.
        """
        from cli_agent_orchestrator.agent_plugins.mcp_delivery import with_plugin_mcp

        profile = with_plugin_mcp(_profile_stub(), "claude_code")
        assert PLUGIN_SERVER in (profile.mcpServers or {})

        # A second, independent load must produce the server again from disk
        # state alone — not from anything the first call wrote down.
        again = with_plugin_mcp(_profile_stub(), "claude_code")
        assert (again.mcpServers or {}).keys() == (profile.mcpServers or {}).keys()


class TestCopilotCarriesAnHttpPluginServer:
    """Copilot's ``type`` vocabulary is not the specification's, at the real artifact.

    Found by self-audit on #584, in the same class as review 5222539218's item 5
    (Kimi) and review 3's ``GROK_URL_TRANSPORTS``: ``_map_entry`` emits the
    Agent Plugins / MCP spelling verbatim, and a provider whose own config format
    names the same transport differently receives a value it does not use.

    Copilot's documented vocabulary is ``local``/``stdio``, ``http`` and ``sse``
    (`Adding MCP servers for GitHub Copilot CLI
    <https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers>`_).
    Two consequences, and only the second is a defect:

    * ``stdio`` needs **no** translation. The vendor documents ``Local`` and
      ``STDIO`` as working the same way and recommends ``stdio`` precisely for
      cross-client portability, so the common case was never broken. An earlier
      draft of this finding claimed otherwise; the documentation does not support
      that and the claim is withdrawn.
    * ``streamable-http`` **is** foreign. Copilot names that transport ``http``,
      so a schema-valid remote plugin server reached ``--additional-mcp-config``
      with a ``type`` Copilot has no case for.

    The matrix fixture above is stdio, so it cannot see this: it never takes the
    url branch. That is the same blind spot that hid the Grok and Kimi defects.
    """

    def test_a_streamable_http_plugin_server_reaches_copilot_as_http(
        self, store, skills_dir, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "cli_agent_orchestrator.agent_plugins.projection.SKILLS_DIR", skills_dir
        )
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skills_dir)
        monkeypatch.setattr(
            "cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGINS_DIR", tmp_path
        )

        http_doc = json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                "mcpServers": {
                    PLUGIN_SERVER: {
                        "type": "streamable-http",
                        "url": "https://mcp.example.invalid/x",
                    }
                },
            }
        )
        source = build_plugin(
            tmp_path / "copilot-http-src",
            "copilothttpdonor",
            skills=["donor-skill"],
            mcp_text=http_doc,
        )
        install(
            PluginSource(kind="path", location=str(source)),
            store=store,
            skills_dir=skills_dir,
            refresh_agents=False,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.agent_plugins.mcp_delivery.InstalledPluginStore",
            lambda *a, **k: store,
        )

        from cli_agent_orchestrator.providers import copilot_cli as mod

        monkeypatch.setattr(mod, "load_agent_profile", lambda _name: _profile_stub())
        provider = mod.CopilotCliProvider("tid-copilot-http", "sess", "win", "worker")

        servers = json.loads(provider._build_runtime_mcp_config())["mcpServers"]

        assert PLUGIN_SERVER in servers, (
            f"copilot wrote {sorted(servers)} — the remote plugin server never "
            f"reached --additional-mcp-config"
        )
        entry = servers[PLUGIN_SERVER]
        assert entry.get("type") == "http", (
            f"copilot received type={entry.get('type')!r}; its documented "
            f"vocabulary is local/stdio, http, sse — 'streamable-http' is not a "
            f"value it has a case for"
        )
        assert entry.get("url") == "https://mcp.example.invalid/x", entry

    def test_a_stdio_plugin_server_keeps_the_portable_spelling(self, installed_plugin, monkeypatch):
        """``stdio`` must be passed through, NOT rewritten to ``local``.

        The vendor documents the two as equivalent and recommends ``stdio`` for
        configurations shared with VS Code, the cloud agent, and other MCP
        clients. Rewriting it would trade a portable spelling for a Copilot-only
        one and gain nothing, so this pins the pass-through as deliberate rather
        than as an oversight of the ``streamable-http`` fix.
        """
        from cli_agent_orchestrator.providers import copilot_cli as mod

        monkeypatch.setattr(mod, "load_agent_profile", lambda _name: _profile_stub())
        provider = mod.CopilotCliProvider("tid-copilot-stdio", "sess", "win", "worker")

        servers = json.loads(provider._build_runtime_mcp_config())["mcpServers"]

        assert PLUGIN_SERVER in servers, sorted(servers)
        assert servers[PLUGIN_SERVER].get("type") == "stdio", servers[PLUGIN_SERVER]

    def test_cao_own_server_is_not_given_a_transport_it_never_had(
        self, installed_plugin, monkeypatch
    ):
        """CAO's in-session entry carries no ``type`` and must keep carrying none.

        It works today precisely because Copilot infers a command-based server
        from ``command``. A translation pass that invented a ``type`` for a
        type-less entry would be a behaviour change beyond this finding — the
        same boundary ``KIMI_TRANSPORTS`` draws.
        """
        from cli_agent_orchestrator.providers import copilot_cli as mod

        monkeypatch.setattr(mod, "load_agent_profile", lambda _name: _profile_stub())
        provider = mod.CopilotCliProvider("tid-copilot-own", "sess", "win", "worker")

        servers = json.loads(provider._build_runtime_mcp_config())["mcpServers"]

        assert "cao-mcp-server" in servers
        assert "type" not in servers["cao-mcp-server"], servers["cao-mcp-server"]
