"""The ONE Codex argument composer.

The ordinary ``CodexProvider``, the unmanaged pre-task bootstrap, and the
managed-v2 adapter must all consume the same pure composer so the bootstrap
and the resumed TUI cannot drift apart on profile/route/prompt/MCP/trust.
These tests pin the composer directly and prove bootstrap/TUI core equality
with route/TUI/resume suffixes as the only intentional differences.
"""

from __future__ import annotations

import os
import shlex
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.providers.codex import (
    CODEX_APP_SERVER_FLAGS,
    CODEX_TUI_FLAGS,
    CodexProvider,
    CodexRoute,
    codex_route_suffix,
    compose_codex_core_args,
    resolve_codex_mcp_material_entry,
)


def _mcp_server(
    *,
    name="context7",
    command="/usr/bin/env",
    args=("context7",),
    env=(),
    env_vars=("HOME", "PATH"),
    tool_timeout_sec=90,
):
    return {
        "name": name,
        "command": command,
        "args": list(args),
        "env": [{"name": k, "value": v} for k, v in env],
        "env_vars": list(env_vars),
        "tool_timeout_sec": tool_timeout_sec,
    }


# ---------------------------------------------------------------------------
# yolo/profile selection — unrestricted "*" wins over a named codexProfile
# ---------------------------------------------------------------------------


def test_unrestricted_tools_force_yolo_even_with_named_profile():
    """A named codexProfile must NOT win when unrestricted "*" tools make the
    TUI choose --yolo — the same choice in every path."""
    core = compose_codex_core_args(
        codex_profile="sealed",
        codex_config=None,
        system_prompt="",
        mcp_servers=[],
        allowed_tools=["*"],
        trusted_project_root=None,
    )
    assert core[0:1] == ["--yolo"]


def test_named_profile_selected_when_tools_restricted():
    core = compose_codex_core_args(
        codex_profile="sealed",
        codex_config=None,
        system_prompt="",
        mcp_servers=[],
        allowed_tools=["Read", "Write"],
        trusted_project_root=None,
    )
    assert core[0:2] == ["--profile", "sealed"]


def test_yolo_when_no_profile_and_no_tools():
    core = compose_codex_core_args(
        codex_profile=None,
        codex_config=None,
        system_prompt="",
        mcp_servers=[],
        allowed_tools=None,
        trusted_project_root=None,
    )
    assert core == ["--yolo"]


# ---------------------------------------------------------------------------
# restricted tools + empty base prompt + non-empty skill catalog
# ---------------------------------------------------------------------------


def test_restricted_tools_compose_security_and_tools_with_empty_base_prompt():
    """When the base profile body is empty but a skill catalog is present, the
    fully-composed developer instructions still carry the security prompt and
    the explicit tool list — the composer consumes the already-composed
    system_prompt verbatim."""
    composed_prompt = (
        "SECURITY...\nYou only have access to these tools: Read, Write\n\n"
        "## Available Skills\n- foo"
    )
    core = compose_codex_core_args(
        codex_profile=None,
        codex_config=None,
        system_prompt=composed_prompt,
        mcp_servers=[],
        allowed_tools=["Read", "Write"],
        trusted_project_root=None,
    )
    assert core[0] == "--yolo"
    # The composed prompt is emitted verbatim as developer_instructions (newlines
    # escaped to literal \n so tmux send_keys keeps it on one line).
    dev_idx = core.index("-c") + 1
    assert core[dev_idx].startswith("developer_instructions=")
    assert "Available Skills" in core[dev_idx]
    assert "Read, Write" in core[dev_idx]


# ---------------------------------------------------------------------------
# MCP command/args/env/env_vars/default and explicit timeout
# ---------------------------------------------------------------------------


def test_mcp_material_serialized_with_command_args_env_envvars_and_timeout():
    core = compose_codex_core_args(
        codex_profile=None,
        codex_config=None,
        system_prompt="",
        mcp_servers=[_mcp_server(env=(("API_KEY", "sk"),))],
        allowed_tools=None,
        trusted_project_root=None,
    )
    assert core == [
        "--yolo",
        "-c",
        'mcp_servers.context7.command="/usr/bin/env"',
        "-c",
        'mcp_servers.context7.args=["context7"]',
        "-c",
        'mcp_servers.context7.env.API_KEY="sk"',
        "-c",
        'mcp_servers.context7.env_vars=["HOME", "PATH", "CAO_TERMINAL_ID"]',
        "-c",
        "mcp_servers.context7.tool_timeout_sec=90.0",
    ]


def test_mcp_default_timeout_is_600_when_unspecified():
    server = _mcp_server()
    server["tool_timeout_sec"] = None
    core = compose_codex_core_args(
        codex_profile=None,
        codex_config=None,
        system_prompt="",
        mcp_servers=[server],
        allowed_tools=None,
        trusted_project_root=None,
    )
    assert "mcp_servers.context7.tool_timeout_sec=600.0" in " ".join(core)


# ---------------------------------------------------------------------------
# URL/streamable-HTTP MCP entries: url + optional bearer token env var
# ---------------------------------------------------------------------------


def test_url_mcp_entry_emits_url_and_bearer_only():
    """A command-less HTTP entry serializes exactly the Codex ``url`` and
    ``bearer_token_env_var`` keys — no command/args/env/env_vars, no
    CAO_TERMINAL_ID injection into a nonexistent subprocess, and no
    ``type`` key (that stays profile-side information)."""
    core = compose_codex_core_args(
        codex_profile=None,
        codex_config=None,
        system_prompt="",
        mcp_servers=[
            {
                "name": "web",
                "url": "https://example.invalid/mcp",
                "bearer_token_env_var": "TEST_TOKEN",
            }
        ],
        allowed_tools=None,
        trusted_project_root=None,
    )
    assert core == [
        "--yolo",
        "-c",
        'mcp_servers.web.url="https://example.invalid/mcp"',
        "-c",
        'mcp_servers.web.bearer_token_env_var="TEST_TOKEN"',
    ]
    joined = " ".join(core)
    assert "mcp_servers.web.command" not in joined
    assert "mcp_servers.web.args" not in joined
    assert "mcp_servers.web.env" not in joined
    assert "CAO_TERMINAL_ID" not in joined
    assert "type" not in joined
    assert "mcp_servers.web.tool_timeout_sec" not in joined


def test_url_mcp_entry_without_bearer_emits_url_only():
    core = compose_codex_core_args(
        codex_profile=None,
        codex_config=None,
        system_prompt="",
        mcp_servers=[{"name": "web", "url": "https://example.invalid/mcp"}],
        allowed_tools=None,
        trusted_project_root=None,
    )
    assert core == [
        "--yolo",
        "-c",
        'mcp_servers.web.url="https://example.invalid/mcp"',
    ]


def test_mcp_resolver_rejects_ambiguous_or_missing_transport():
    """Exactly one usable transport per entry: both, neither, or an
    empty-string transport is a typed refusal, never an invented one."""
    with pytest.raises(ValueError, match="exactly one usable transport"):
        resolve_codex_mcp_material_entry(
            name="web",
            config={"command": "/usr/bin/env", "url": "https://example.invalid/mcp"},
            terminal_id="t1",
        )
    with pytest.raises(ValueError, match="exactly one usable transport"):
        resolve_codex_mcp_material_entry(name="web", config={"type": "http"}, terminal_id="t1")
    with pytest.raises(ValueError, match="exactly one usable transport"):
        resolve_codex_mcp_material_entry(
            name="web", config={"command": "", "url": ""}, terminal_id="t1"
        )


def test_mcp_resolver_rejects_empty_url_and_empty_bearer():
    # An empty url is not a usable transport: neither transport is usable.
    with pytest.raises(ValueError, match="exactly one usable transport"):
        resolve_codex_mcp_material_entry(name="web", config={"url": ""}, terminal_id="t1")
    with pytest.raises(ValueError, match="non-empty string"):
        resolve_codex_mcp_material_entry(
            name="web",
            config={"url": "https://example.invalid/mcp", "bearer_token_env_var": ""},
            terminal_id="t1",
        )


def test_mcp_composer_rejects_no_transport_entry():
    """The composer itself fails closed on an entry with no usable
    transport — the same typed boundary every consumer maps."""
    with pytest.raises(ValueError, match="exactly one usable transport"):
        compose_codex_core_args(
            codex_profile=None,
            codex_config=None,
            system_prompt="",
            mcp_servers=[{"name": "web", "type": "http"}],
            allowed_tools=None,
            trusted_project_root=None,
        )


def test_command_mcp_material_entry_shape_is_unchanged():
    """The shared resolver keeps the established command/stdio shape
    byte-for-byte: sorted env with the CAO_TERMINAL_ID default, args,
    env_vars, and the tool timeout passthrough."""
    entry = resolve_codex_mcp_material_entry(
        name="context7",
        config={
            "command": "/usr/bin/env",
            "args": ["context7"],
            "env": {"API_KEY": "sk", "A": "1"},
            "env_vars": ["HOME"],
            "tool_timeout_sec": 90,
        },
        terminal_id="t1",
    )
    assert entry == {
        "name": "context7",
        "command": "/usr/bin/env",
        "args": ["context7"],
        "env": [
            {"name": "A", "value": "1"},
            {"name": "API_KEY", "value": "sk"},
            {"name": "CAO_TERMINAL_ID", "value": "t1"},
        ],
        "env_vars": ["HOME"],
        "tool_timeout_sec": 90,
    }
    # The serialized form matches the pre-existing command behavior exactly.
    core = compose_codex_core_args(
        codex_profile=None,
        codex_config=None,
        system_prompt="",
        mcp_servers=[entry],
        allowed_tools=None,
        trusted_project_root=None,
    )
    assert core == [
        "--yolo",
        "-c",
        'mcp_servers.context7.command="/usr/bin/env"',
        "-c",
        'mcp_servers.context7.args=["context7"]',
        "-c",
        'mcp_servers.context7.env.A="1"',
        "-c",
        'mcp_servers.context7.env.API_KEY="sk"',
        "-c",
        'mcp_servers.context7.env.CAO_TERMINAL_ID="t1"',
        "-c",
        'mcp_servers.context7.env_vars=["HOME", "CAO_TERMINAL_ID"]',
        "-c",
        "mcp_servers.context7.tool_timeout_sec=90.0",
    ]


def test_material_producers_agree_on_mixed_command_and_url_servers(monkeypatch):
    """The managed-v2 material builder and the ordinary provider fallback
    produce the SAME material entries for a mixed profile (command + URL)."""
    from cli_agent_orchestrator.models.agent_profile import AgentProfile
    from cli_agent_orchestrator.services.managed_provider_bridge import (
        _profile_material_from_profile,
    )

    profile = AgentProfile(
        name="developer",
        description="Developer",
        mcpServers={
            "local": {
                "command": "/usr/bin/env",
                "args": ["mcp-server"],
                "env": {"TOKEN": "abc"},
            },
            "web": {
                "type": "http",
                "url": "https://example.invalid/mcp",
                "bearer_token_env_var": "TEST_TOKEN",
            },
        },
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.codex.load_agent_profile", lambda _name: profile
    )
    managed = _profile_material_from_profile(profile, "term-1", allowed_tools=["Read"])
    fallback = CodexProvider("term-1", "s1", "w1", agent_profile="developer")
    assert fallback._resolve_codex_profile_material()["mcp_servers"] == managed["mcp_servers"]
    assert managed["mcp_servers"] == [
        {
            "name": "local",
            "command": "/usr/bin/env",
            "args": ["mcp-server"],
            "env": [
                {"name": "CAO_TERMINAL_ID", "value": "term-1"},
                {"name": "TOKEN", "value": "abc"},
            ],
            "env_vars": [],
            "tool_timeout_sec": None,
        },
        {
            "name": "web",
            "url": "https://example.invalid/mcp",
            "bearer_token_env_var": "TEST_TOKEN",
        },
    ]


def test_mcp_env_vars_must_be_strings():
    server = _mcp_server()
    server["env_vars"] = ["HOME", 42]
    with pytest.raises(ValueError) as error:
        compose_codex_core_args(
            codex_profile=None,
            codex_config=None,
            system_prompt="",
            mcp_servers=[server],
            allowed_tools=None,
            trusted_project_root=None,
        )
    assert str(error.value) == ("mcpServers 'context7' env_vars[1] must be a string, got int")


# ---------------------------------------------------------------------------
# codexConfig, canonical trust, explicit route, default route
# ---------------------------------------------------------------------------


def test_codexconfig_overrides_emitted_before_trust_and_route():
    core = compose_codex_core_args(
        codex_profile=None,
        codex_config={"features.fast_mode": True, "model_reasoning_effort": "high"},
        system_prompt="",
        mcp_servers=[],
        allowed_tools=None,
        trusted_project_root=None,
    )
    joined = " ".join(core)
    assert "features.fast_mode=true" in joined
    assert 'model_reasoning_effort="high"' in joined


def test_canonical_trust_rendered_via_projects_renderer(tmp_path):
    canonical = os.path.realpath(str(tmp_path))
    core = compose_codex_core_args(
        codex_profile=None,
        codex_config=None,
        system_prompt="",
        mcp_servers=[],
        allowed_tools=None,
        trusted_project_root=canonical,
    )
    assert core == [
        "--yolo",
        "-c",
        f'projects={{"{canonical}"={{trust_level="trusted"}}}}',
    ]


def test_explicit_route_suffix_emitted_last_wins():
    suffix = codex_route_suffix(CodexRoute(model="gpt-5.6-sol", effort="xhigh"))
    assert suffix == ["--model", "gpt-5.6-sol", "-c", 'model_reasoning_effort="xhigh"']


def test_default_route_suffix_is_empty():
    """A provider-default route (no model, no effort) emits nothing — never an
    empty-string or invented route."""
    assert codex_route_suffix(CodexRoute(model="", effort="")) == []
    assert codex_route_suffix(CodexRoute(model=None, effort=None)) == []
    assert codex_route_suffix(None) == []


def test_route_model_emitted_without_effort_when_effort_unreported():
    """On the ordinary path the provider may report a model but no effort."""
    suffix = codex_route_suffix(CodexRoute(model="gpt-5.6-sol", effort=None))
    assert suffix == ["--model", "gpt-5.6-sol"]


def test_route_suffix_after_core_so_codexconfig_cannot_override():
    """The route is emitted AFTER codexConfig so a named profile or codexConfig
    knob cannot silently select a different route (last-wins)."""
    core = compose_codex_core_args(
        codex_profile=None,
        codex_config={"model_reasoning_effort": "low"},
        system_prompt="",
        mcp_servers=[],
        allowed_tools=None,
        trusted_project_root=None,
    )
    full = core + codex_route_suffix(CodexRoute(model="m", effort="xhigh"))
    # The sealed effort appears after the codexConfig effort.
    assert full.index('model_reasoning_effort="xhigh"') > full.index('model_reasoning_effort="low"')


# ---------------------------------------------------------------------------
# bootstrap and TUI core argument equality
# ---------------------------------------------------------------------------


def _bootstrap_argv(core, pinned_route):
    return list(core) + codex_route_suffix(pinned_route) + list(CODEX_APP_SERVER_FLAGS)


def _tui_argv(core, observed_route, resume_id):
    # The resumed TUI places its TUI flags right after the yolo/profile choice
    # and appends the observed route then the exact resume id.
    head, tail = core[0:1], core[1:]
    return (
        head
        + list(CODEX_TUI_FLAGS)
        + tail
        + codex_route_suffix(observed_route)
        + (["resume", resume_id] if resume_id else [])
    )


def test_bootstrap_and_tui_core_args_equal_only_suffixes_differ(tmp_path):
    """The bootstrap and resumed TUI share every composer-produced argument."""
    canonical = os.path.realpath(str(tmp_path))
    core = compose_codex_core_args(
        codex_profile=None,
        codex_config={"features.fast_mode": True},
        system_prompt="composed developer instructions",
        mcp_servers=[_mcp_server()],
        allowed_tools=["*"],
        trusted_project_root=canonical,
    )

    pinned = CodexRoute(model="", effort="")  # ordinary default-route bootstrap
    observed = CodexRoute(model="gpt-5.6-sol", effort=None)  # fed back from thread/start
    boot = _bootstrap_argv(core, pinned)
    tui = _tui_argv(core, observed, "019fb17d-0c6d-7161-a408-6b1fa61c8f2d")

    assert boot == [
        *core,
        "app-server",
        "--stdio",
    ]
    assert tui == [
        core[0],
        *CODEX_TUI_FLAGS,
        *core[1:],
        "--model",
        "gpt-5.6-sol",
        "resume",
        "019fb17d-0c6d-7161-a408-6b1fa61c8f2d",
    ]


def test_provider_profile_command_has_exact_composer_core_and_trust_parity(tmp_path):
    """The real TUI command consumes the precomposed profile core unchanged."""
    canonical = os.path.realpath(str(tmp_path))
    profile = SimpleNamespace(
        codexProfile="sealed",
        codexConfig={"features.fast_mode": True},
        model=None,
    )
    material = {
        "profile": profile,
        "allowed_tools": ["Read"],
        "system_prompt": "instructions",
        "mcp_servers": [_mcp_server()],
    }
    core = compose_codex_core_args(
        codex_profile=profile.codexProfile,
        codex_config=profile.codexConfig,
        system_prompt=material["system_prompt"],
        mcp_servers=material["mcp_servers"],
        allowed_tools=material["allowed_tools"],
        trusted_project_root=canonical,
    )
    assert core == [
        "--profile",
        "sealed",
        "-c",
        'developer_instructions="instructions"',
        "-c",
        'mcp_servers.context7.command="/usr/bin/env"',
        "-c",
        'mcp_servers.context7.args=["context7"]',
        "-c",
        'mcp_servers.context7.env_vars=["HOME", "PATH", "CAO_TERMINAL_ID"]',
        "-c",
        "mcp_servers.context7.tool_timeout_sec=90.0",
        "-c",
        "features.fast_mode=true",
        "-c",
        f'projects={{"{canonical}"={{trust_level="trusted"}}}}',
    ]

    provider = CodexProvider(
        "terminal-1",
        "session-1",
        "window-1",
        trusted_project_root=canonical,
        expected_model="gpt-5.6-sol",
        expected_effort="xhigh",
        native_session_id="019fb17d-0c6d-7161-a408-6b1fa61c8f2d",
        codex_profile_material=material,
    )
    assert shlex.split(provider._build_codex_command()) == [
        "codex",
        "--profile",
        "sealed",
        *CODEX_TUI_FLAGS,
        *core[2:],
        "--model",
        "gpt-5.6-sol",
        "-c",
        'model_reasoning_effort="xhigh"',
        "resume",
        "019fb17d-0c6d-7161-a408-6b1fa61c8f2d",
    ]


def test_explicit_route_bootstrap_and_tui_share_core(tmp_path):
    canonical = os.path.realpath(str(tmp_path))
    core = compose_codex_core_args(
        codex_profile="sealed",
        codex_config=None,
        system_prompt="instructions",
        mcp_servers=[],
        allowed_tools=["Read"],
        trusted_project_root=canonical,
    )
    route = CodexRoute(model="gpt-5.6-sol", effort="xhigh")
    boot = _bootstrap_argv(core, route)
    tui = _tui_argv(core, route, "abc")
    # The route suffix is identical when the same route is pinned/observed.
    assert codex_route_suffix(route) == [
        "--model",
        "gpt-5.6-sol",
        "-c",
        'model_reasoning_effort="xhigh"',
    ]
    # Only the suffix-bearing tails differ.
    assert "app-server" in boot and "app-server" not in tui
    assert "resume" in tui and "resume" not in boot


# ---------------------------------------------------------------------------
# The resumed TUI launches the EXACT digest-verified executable
# ---------------------------------------------------------------------------


def test_resumed_tui_uses_pinned_verified_executable_not_bare_codex(tmp_path):
    """PATH-divergence regression: the pre-task bootstrap digest-verified one
    absolute executable, and the resumed TUI must launch THAT path, never a
    bare ``codex`` that an existing tmux session's different PATH could
    resolve to another build."""
    canonical = os.path.realpath(str(tmp_path))
    pinned = "/opt/codex-0.147.0/bin/codex"
    profile = SimpleNamespace(codexProfile=None, codexConfig=None, model=None)
    material = {
        "profile": profile,
        "allowed_tools": ["*"],
        "system_prompt": "",
        "mcp_servers": [],
    }
    provider = CodexProvider(
        "terminal-1",
        "session-1",
        "window-1",
        trusted_project_root=canonical,
        native_session_id="019fb17d-0c6d-7161-a408-6b1fa61c8f2d",
        codex_profile_material=material,
        codex_executable=pinned,
    )
    argv = shlex.split(provider._build_codex_command())
    assert argv[0] == pinned
    assert argv.count("codex") == 0  # the bare name never appears as an argv
    assert argv[-2:] == ["resume", "019fb17d-0c6d-7161-a408-6b1fa61c8f2d"]


def test_legacy_provider_without_pinned_executable_keeps_bare_codex(tmp_path):
    """Direct/unit construction without the pre-task seam still launches
    ``codex`` (legacy ambient form); the pin is additive, never guessed."""
    canonical = os.path.realpath(str(tmp_path))
    profile = SimpleNamespace(codexProfile=None, codexConfig=None, model=None)
    material = {
        "profile": profile,
        "allowed_tools": ["*"],
        "system_prompt": "",
        "mcp_servers": [],
    }
    provider = CodexProvider(
        "terminal-1",
        "session-1",
        "window-1",
        trusted_project_root=canonical,
        native_session_id="019fb17d-0c6d-7161-a408-6b1fa61c8f2d",
        codex_profile_material=material,
    )
    assert shlex.split(provider._build_codex_command())[0] == "codex"


# ---------------------------------------------------------------------------
# P2: codexConfig.model wins over profile.model; sealed expected wins both
# ---------------------------------------------------------------------------


def _route_provider(profile, *, expected_model=None, expected_effort=None, **kwargs):
    material = {
        "profile": profile,
        "allowed_tools": ["Read"],
        "system_prompt": "instructions",
        "mcp_servers": [],
    }
    return CodexProvider(
        "terminal-1",
        "session-1",
        "window-1",
        expected_model=expected_model,
        expected_effort=expected_effort,
        codex_profile_material=material,
        **kwargs,
    )


def test_codexconfig_model_beats_profile_model_for_ordinary_route():
    """The documented ordinary-route precedence: an explicit ``codexConfig
    model`` override wins over the profile's own ``model`` field."""
    profile = SimpleNamespace(
        codexProfile=None,
        codexConfig={"model": "config-model"},
        model="profile-model",
    )
    provider = _route_provider(profile)
    argv = shlex.split(provider._build_codex_command())
    assert argv[argv.index("--model") + 1] == "config-model"


def test_sealed_expected_model_beats_codexconfig_and_profile_models():
    """A caller-sealed expected_model (managed-v2 sealed route) still wins
    over both the codexConfig override and the profile model."""
    profile = SimpleNamespace(
        codexProfile=None,
        codexConfig={"model": "config-model"},
        model="profile-model",
    )
    provider = _route_provider(profile, expected_model="sealed-model")
    argv = shlex.split(provider._build_codex_command())
    assert argv[argv.index("--model") + 1] == "sealed-model"


def test_codexconfig_model_alone_selects_route_when_profile_model_empty():
    profile = SimpleNamespace(
        codexProfile=None,
        codexConfig={"model": "config-model"},
        model=None,
    )
    provider = _route_provider(profile)
    argv = shlex.split(provider._build_codex_command())
    assert argv[argv.index("--model") + 1] == "config-model"


def test_profile_model_still_selected_when_no_codexconfig_model():
    profile = SimpleNamespace(
        codexProfile=None,
        codexConfig={"model_reasoning_effort": "high"},
        model="profile-model",
    )
    provider = _route_provider(profile)
    argv = shlex.split(provider._build_codex_command())
    assert argv[argv.index("--model") + 1] == "profile-model"
