"""An ephemeral Kiro helper's provider launch on v3, against a scripted fake pane (#1167 Phase 2).

No Kiro runs. The per-launch home is real (under tmp_path), the pane is Phase
1's fake that draws the captured 2.35.1 screens, and the operator's
``~/.kiro`` is a fake HOME's that only ``stat`` may touch. Every refusal must
leave ``identity_verified`` False and type no task: refusals at stage C type
nothing at all, refusals after the header send no slash command (gates G18,
G20, G22, G23, G27).
"""

from __future__ import annotations

import os
import shlex
import stat
from pathlib import Path
from test.providers.kiro_helper_support import operator_home  # noqa: F401
from test.providers.test_kiro_cli_v3_launch import FakePane
from unittest.mock import Mock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers import kiro_launch_home as k
from cli_agent_orchestrator.providers.kiro_attest import KiroAttestationError
from cli_agent_orchestrator.providers.kiro_capabilities import KiroV3LaunchError
from cli_agent_orchestrator.providers.kiro_cli import KiroAttestation, KiroCliProvider
from cli_agent_orchestrator.providers.kiro_identity import KiroAgentIdentityError
from cli_agent_orchestrator.providers.kiro_workspace import KiroWorkspaceError
from cli_agent_orchestrator.services import ephemeral_service

_KC = "cli_agent_orchestrator.providers.kiro_cli"
TID = "0a1b2c3d"
AGENT = "v3ro"  # the agent the captured screens' header names
KIRO = "/opt/kiro/bin/kiro-cli"


@pytest.fixture
def helper(tmp_path, monkeypatch, operator_home):  # noqa: F811
    ephemeral = tmp_path / "cao" / "ephemeral"
    ephemeral.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(ephemeral_service, "EPHEMERAL_DIR", ephemeral)
    repo = tmp_path / "repo"
    repo.mkdir()
    grant = k.kiro_helper_grant(["fs_read", "fs_list"])
    profile = AgentProfile(name=AGENT, description="d", system_prompt="You are a helper.")
    snapshot = k.snapshot_kiro_global_home(k.server_home(), placeholder_listable=False)
    prepared = k.create_launch_home(
        TID, AGENT, k.render_helper_agent(AGENT, profile, grant, TID), snapshot
    )
    k.seal_launch_home(prepared)
    launch = k.KiroLaunch(
        home=prepared,
        kiro_cli=k.ResolvedKiroCli(path=KIRO, target=KIRO),
        grant=tuple(grant),
        working_directory=str(repo),
        model="claude-haiku-5.5",
        effort="low",
        version="2.35.1",
        schema="kas-0.66.70",
    )
    yield launch, repo, operator_home
    k.discard_launch_home(TID)
    for directory, dirnames, _ in os.walk(ephemeral):
        for name in dirnames:
            path = Path(directory) / name
            if not path.is_symlink() and stat.S_IMODE(os.lstat(path).st_mode) == 0:
                path.chmod(0o700)


def _provider(launch):
    return KiroCliProvider(
        TID, "sess", "win", AGENT, ["fs_read", "fs_list"], engine=KiroEngine.V3, launch=launch
    )


async def _launch(pane, provider, *, installed=None):
    """``initialize`` against ``pane``; the installed store must never be read."""
    installed = installed or Mock(side_effect=AssertionError("the installed store was read"))
    with (
        patch(f"{_KC}.get_backend", return_value=pane),
        patch(f"{_KC}.wait_for_shell", return_value=True),
        patch(f"{_KC}.wait_until_status", return_value=True),
        patch(f"{_KC}.load_agent_profile", side_effect=AssertionError("profile was loaded")),
        patch(f"{_KC}.asyncio.sleep", return_value=None),
        patch("cli_agent_orchestrator.services.status_monitor.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.install_service.installed_kiro_v3_grant", installed),
    ):
        monitor.get_status.return_value = TerminalStatus.IDLE
        await provider.initialize()
    return provider


class SideEffectPane(FakePane):
    """A fake pane that runs ``on_command`` once the launch command is typed (Kiro's start)."""

    def __init__(self, cwd, on_command=None, **kwargs):
        super().__init__(cwd, **kwargs)
        self.on_command = on_command

    def send_keys(self, session, window, text, *args, **kwargs):
        first = not self.command
        super().send_keys(session, window, text, *args, **kwargs)
        if first and self.on_command is not None:
            self.on_command()


@pytest.mark.asyncio
async def test_a_helper_launch_types_its_command_and_attests_the_per_launch_grant(helper):
    launch, repo, _ = helper
    pane = FakePane(str(repo))
    installed = Mock(side_effect=AssertionError("the installed store was read"))
    provider = await _launch(pane, _provider(launch), installed=installed)

    assert provider.identity_verified is True
    argv = shlex.split(pane.command)
    assert argv == [
        "/usr/bin/env",
        "-u",
        "ASBX_KIRO_MANDATORY_MCPS",
        "-u",
        "KIRO_AGENT_CONFIG_DIR",
        "-u",
        "KIRO_AGENT_ENGINE",
        f"KIRO_HOME={launch.home.home}",
        KIRO,
        "chat",
        "--agent-engine",
        "v3",
        "--trust-workspace",
        "--trust-all-tools",
        "--model",
        "claude-haiku-5.5",
        "--effort",
        "low",
        "--agent",
        AGENT,
    ]
    assert pane.texts == [pane.command]
    assert pane.keys == [*"/mcp", "Enter", "Escape", *"/tools", "Enter", "Escape"]
    installed.assert_not_called()
    assert provider.attestation == KiroAttestation(
        tags=("read",), mcp_tools=(("cao-mcp-server", 34),), model="auto"
    )


@pytest.mark.asyncio
async def test_a_home_changed_before_the_command_refuses_and_nothing_is_typed(helper):
    launch, repo, _ = helper
    agents = launch.home.home / "agents"
    agents.chmod(0o700)
    launch.home.agent_file.chmod(0o600)
    launch.home.agent_file.write_text(launch.home.agent_file.read_text().replace("glob", "shell"))
    launch.home.agent_file.chmod(0o400)
    agents.chmod(0o500)
    pane = FakePane(str(repo))
    provider = _provider(launch)
    with pytest.raises(k.KiroLaunchRefusal) as err:
        await _launch(pane, provider)
    assert err.value.rule == "kiro_home_invalid:modified"
    assert pane.texts == [] and pane.keys == []
    assert provider.identity_verified is False


def _chmod_placeholder(launch, operator):
    (launch.home.home / "powers").chmod(0o700)


def _replace_placeholder(launch, operator):
    powers = launch.home.home / "powers"
    powers.rmdir()
    powers.mkdir()
    (powers / "installed.json").write_text("{}")


def _staging_entry(launch, operator):
    (launch.home.home / ".powers-migrate-ab12").mkdir()


def _secrets_marker(launch, operator):
    operator.set_entry(".secrets-migrated", "file")


def _powers_marker_gone(launch, operator):
    operator.set_entry(".powers-migrated", "absent")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "migration",
    [
        _chmod_placeholder,
        _replace_placeholder,
        _staging_entry,
        _secrets_marker,
        _powers_marker_gone,
    ],
)
async def test_a_legacy_copy_seen_after_the_header_refuses_before_any_slash_command(
    helper, migration
):
    launch, repo, operator = helper
    pane = SideEffectPane(str(repo), on_command=lambda: migration(launch, operator))
    provider = _provider(launch)
    with pytest.raises(k.KiroLaunchRefusal) as err:
        await _launch(pane, provider)
    assert err.value.rule == "kiro_v3_global_home:changed"
    assert pane.keys == [] and pane.texts == [pane.command]
    assert provider.identity_verified is False


@pytest.mark.asyncio
async def test_kiros_own_files_and_a_rewritten_source_do_not_refuse(helper):
    launch, repo, operator = helper

    def session_start():
        (launch.home.home / "sessions").mkdir()
        (launch.home.home / "secrets.json").write_text('{"kiro": "state"}')
        operator.set_entry("powers", "absent")  # the operator's own Kiro, meanwhile

    pane = SideEffectPane(str(repo), on_command=session_start)
    provider = await _launch(pane, _provider(launch))
    assert provider.identity_verified is True


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["agent", "sentinel", "settings", "extra"])
async def test_a_home_rewritten_after_the_header_refuses_as_modified(helper, target):
    # The sentinel, the seed and agents/'s entries are verify_launch_home's alone:
    # the attestation's grant read checks only the helper's own file.
    launch, repo, _ = helper

    def rewrite():
        agents = launch.home.home / "agents"
        agents.chmod(0o700)
        if target == "extra":
            (agents / "planted.json").write_text("{}")
        else:
            path = {
                "agent": launch.home.agent_file,
                "sentinel": launch.home.sentinel_file,
                "settings": launch.home.home / "settings" / "cli.json",
            }[target]
            path.chmod(0o600)
            path.write_text("{}")
            path.chmod(0o400)
        agents.chmod(0o500)

    pane = SideEffectPane(str(repo), on_command=rewrite)
    provider = _provider(launch)
    with pytest.raises(k.KiroLaunchRefusal) as err:
        await _launch(pane, provider)
    assert err.value.rule == "kiro_home_invalid:modified"
    assert pane.keys == [] and provider.identity_verified is False


@pytest.mark.asyncio
async def test_a_pane_in_home_refuses_at_the_rescan(helper):
    launch, _, operator = helper
    pane = FakePane(str(operator.home))
    provider = _provider(launch)
    with pytest.raises(KiroWorkspaceError) as err:
        await _launch(pane, provider)
    assert err.value.code == "kiro_v3_workspace_config:home"
    assert pane.keys == [] and provider.identity_verified is False


@pytest.mark.asyncio
async def test_a_workspace_entry_planted_after_the_first_scan_refuses_at_the_rescan(helper):
    launch, repo, _ = helper
    pane = SideEffectPane(
        str(repo), on_command=lambda: (repo / ".kiro" / "hooks").mkdir(parents=True)
    )
    with pytest.raises(KiroWorkspaceError) as err:
        await _launch(pane, _provider(launch))
    assert err.value.code == "kiro_v3_workspace_config:hooks"
    assert pane.keys == []


@pytest.mark.asyncio
async def test_an_unreadable_pane_cwd_refuses_at_the_rescan(helper):
    launch, repo, _ = helper
    pane = FakePane("")
    with pytest.raises(KiroV3LaunchError) as err:
        await _launch(pane, _provider(launch))
    assert err.value.code == "kiro_v3_bad_cwd"
    assert pane.keys == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "panel, error",
    [
        ({"tools": "tools_default"}, "kiro_v3_tools_mismatch"),
        ({"tools": "tools_none"}, "kiro_v3_tools_mismatch"),
        ({"tools": "tools_read_tag_write"}, "kiro_v3_tools_mismatch"),
        ({"mcp": ("mcp_decoys_shown",)}, "kiro_v3_mcp_not_ready"),
        ({"typed_tools": "palette_tools_tab"}, "kiro_v3_tools_mismatch"),
    ],
)
async def test_panels_beyond_the_helpers_grant_refuse_and_type_no_task(helper, panel, error):
    launch, repo, _ = helper
    pane = FakePane(str(repo), **panel)
    provider = _provider(launch)
    with pytest.raises(KiroAttestationError) as err:
        await _launch(pane, provider)
    assert err.value.code == error
    assert provider.identity_verified is False and provider.attestation is None
    assert pane.texts == [pane.command]
    assert "Tab" not in pane.keys


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ready, status, code",
    [
        ("trust_dialog", TerminalStatus.WAITING_USER_ANSWER, "kiro_v3_trust_dialog"),
        ("trust_declined_exit", TerminalStatus.ERROR, "kiro_v3_trust_dialog"),
        ("v2_upsell", TerminalStatus.WAITING_USER_ANSWER, "kiro_v3_startup_dialog"),
    ],
)
async def test_a_startup_dialog_refuses_on_the_helper_path_and_no_key_is_sent(
    helper, ready, status, code
):
    """With the real wait, so its poll hook reads the pane (G23)."""
    launch, repo, _ = helper
    pane = FakePane(str(repo), ready=ready)
    provider = _provider(launch)
    with (
        patch(f"{_KC}.get_backend", return_value=pane),
        patch(f"{_KC}.wait_for_shell", return_value=True),
        patch(f"{_KC}.get_server_settings", return_value={"provider_init_timeout": 30}),
        patch("cli_agent_orchestrator.services.status_monitor.status_monitor") as monitor,
    ):
        monitor.get_status.return_value = status
        with pytest.raises(KiroV3LaunchError) as err:
            await provider.initialize()
    assert err.value.code == code
    assert pane.keys == [] and pane.texts == [pane.command]
    assert provider.identity_verified is False


@pytest.mark.asyncio
async def test_the_fallback_agent_refuses_before_any_slash_command(helper):
    launch, repo, _ = helper
    pane = FakePane(str(repo), ready="fallback_not_found")
    provider = _provider(launch)
    with pytest.raises(KiroAgentIdentityError):
        await _launch(pane, provider)
    assert pane.keys == [] and provider.identity_verified is False


def test_a_helper_runs_on_v3_only(helper):
    launch, _, _ = helper
    with pytest.raises(ValueError):
        KiroCliProvider(TID, "s", "w", AGENT, None, engine=KiroEngine.V2, launch=launch)
