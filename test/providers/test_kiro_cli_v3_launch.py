"""The Kiro v3 provider launch against a scripted fake pane (#1167 Phase 1, G8, G10, G12).

No Kiro runs. The fake pane shows the captured 2.35.1 screens in the order a
real launch would draw them, driven by the keys CAO sends. Every refusal must
leave ``identity_verified`` False and type no task text; the only keys ever
sent are the launch command and the two slash commands with their Enter and
Escape.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Iterable, List
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.kiro_attest import KiroAttestationError
from cli_agent_orchestrator.providers.kiro_capabilities import KiroV3LaunchError
from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider
from cli_agent_orchestrator.providers.kiro_identity import KiroAgentIdentityError
from cli_agent_orchestrator.providers.kiro_workspace import KiroWorkspaceError

FIXTURES = Path(__file__).parent / "fixtures"
_KC = "cli_agent_orchestrator.providers.kiro_cli"
GRANT = (["fs_read", "fs_list", "@cao-mcp-server"], ["cao-mcp-server"])


def _screen(name: str) -> str:
    """A captured screen by fixture name, or ``name`` itself when it is screen text."""
    if "\n" in name:
        return name
    return (FIXTURES / f"kiro_cli_tui_2_35_1_v3_{name}.txt").read_text(encoding="utf-8")


class FakePane:
    """A pane whose screen follows the keys typed into it."""

    def __init__(
        self,
        cwd: str,
        *,
        ready="idle",
        typed_tools="typed_tools",
        tools="tools_readonly_mcp",
        mcp=("mcp_running",),
    ):
        self.cwd = cwd
        self.ready = ready
        self.typed = {"/tools": typed_tools, "/mcp": "typed_mcp"}
        self.panels = {"/tools": [tools], "/mcp": list(mcp)}
        self.screen = _screen(ready)
        self.buffer = ""
        self.command = ""
        self.texts: List[str] = []
        self.keys: List[str] = []

    # backend API -----------------------------------------------------------
    def get_pane_current_command(self, *_):
        return "zsh"

    def get_pane_working_directory(self, *_):
        return self.cwd

    def send_keys(self, _session, _window, text, *args, **kwargs):
        self.texts.append(text)
        if not self.command:
            self.command = text

    def send_special_key(self, _session, _window, key):
        self.keys.append(key)
        if key == "Escape":
            self.buffer = ""
            self.screen = _screen("idle")
        elif key == "Enter":
            panels = self.panels[self.buffer]
            self.screen = _screen(panels.pop(0) if len(panels) > 1 else panels[0])
        else:
            self.buffer += key
            if self.buffer in self.typed:
                self.screen = _screen(self.typed[self.buffer])

    def get_history(self, *_args, **_kwargs):
        return self.screen


def _provider(agent="v3ro", allowed=GRANT[0]):
    return KiroCliProvider("t1234567", "sess", "win", agent, allowed, engine=KiroEngine.V3)


async def _launch(pane, *, status=TerminalStatus.IDLE, grant=GRANT, provider=None):
    provider = provider or _provider()
    with (
        patch(f"{_KC}.get_backend", return_value=pane),
        patch(f"{_KC}.wait_for_shell", return_value=True),
        patch(f"{_KC}.wait_until_status", return_value=True),
        patch(f"{_KC}.load_agent_profile", side_effect=FileNotFoundError("no profile")),
        patch(f"{_KC}.asyncio.sleep", return_value=None),
        patch("cli_agent_orchestrator.services.status_monitor.status_monitor") as monitor,
        patch(
            "cli_agent_orchestrator.services.install_service.installed_kiro_v3_grant",
            return_value=grant,
        ),
    ):
        monitor.get_status.return_value = status
        await provider.initialize()
    return provider


async def _launch_with_profile(pane):
    """``_launch`` with ``load_agent_profile`` left to the caller's patch."""
    provider = _provider()
    with (
        patch(f"{_KC}.get_backend", return_value=pane),
        patch(f"{_KC}.wait_for_shell", return_value=True),
        patch(f"{_KC}.wait_until_status", return_value=True),
        patch(f"{_KC}.asyncio.sleep", return_value=None),
        patch("cli_agent_orchestrator.services.status_monitor.status_monitor") as monitor,
        patch(
            "cli_agent_orchestrator.services.install_service.installed_kiro_v3_grant",
            return_value=GRANT,
        ),
    ):
        monitor.get_status.return_value = TerminalStatus.IDLE
        await provider.initialize()
    return provider


@pytest.fixture
def cwd(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


@pytest.mark.asyncio
async def test_a_v3_launch_attests_tools_and_mcp_before_recording_identity(cwd):
    pane = FakePane(str(cwd))
    provider = await _launch(pane)

    assert provider.identity_verified is True
    argv = shlex.split(pane.command)
    assert argv[:9] == [
        "/usr/bin/env",
        "-u",
        "ASBX_KIRO_MANDATORY_MCPS",
        "-u",
        "KIRO_AGENT_CONFIG_DIR",
        "-u",
        "KIRO_AGENT_ENGINE",
        "-u",
        "KIRO_HOME",
    ]
    assert argv[9:15] == [
        "kiro-cli",
        "chat",
        "--agent-engine",
        "v3",
        "--trust-workspace",
        "--trust-all-tools",
    ]
    assert argv[-2:] == ["--agent", "v3ro"]
    # Only the launch command is sent as text; the slash commands go key by
    # key, never with Tab, and each panel is closed.
    assert pane.texts == [pane.command]
    assert pane.keys == [*"/mcp", "Enter", "Escape", *"/tools", "Enter", "Escape"]
    assert "Tab" not in pane.keys


_MCP_LOADING = _screen("mcp_running").replace(
    "● running       34 tools", "⠋ loading        0 tools"
)
_MCP_ROW_LINE = re.compile(r"^\s*(@\S+)\s{2,}mcp\b")


def _tools_panel_with(mcp_tools: Iterable[str] = ()) -> str:
    """The captured ``/tools`` panel with the ``read`` tag and only ``mcp_tools``."""
    keep = set(mcp_tools)
    lines = []
    for line in _screen("tools_readonly_mcp").splitlines():
        row = _MCP_ROW_LINE.match(line)
        if row is None or row.group(1) in keep:
            lines.append(line)
    count = 1 + len(keep)
    screen = "\n".join(lines).replace(
        "/tools · 35 tags", f"/tools · {count} tag{'s' if count != 1 else ''}"
    )
    assert screen != _screen("tools_readonly_mcp")
    return screen


#: What ``/tools`` shows while ``cao-mcp-server`` is still loading: none of its tools.
_TOOLS_WHILE_LOADING = _tools_panel_with()


class LoadingPane(FakePane):
    """A pane whose MCP server is still loading when the attestation starts.

    The server finishes once the ``/mcp`` panel has been read more than
    ``loading_reads`` times (CAO's re-reads stand in for the time passing).
    Until then ``/mcp`` shows it loading and ``/tools`` shows none of its tools,
    as one real pane would; afterwards ``/mcp`` shows it running and ``/tools``
    shows ``loaded_tools``.
    """

    def __init__(self, cwd: str, *, loaded_tools: str, loading_reads: int = 2):
        super().__init__(cwd)
        self.loaded_tools = loaded_tools
        self.loading_reads = loading_reads
        self.mcp_reads = 0
        self.tools_shown: List[str] = []

    @property
    def loaded(self) -> bool:
        return self.mcp_reads > self.loading_reads

    def send_special_key(self, session, window, key):
        if key == "Enter" and self.buffer in ("/tools", "/mcp"):
            self.keys.append(key)
            if self.buffer == "/tools":
                self.tools_shown.append("loaded" if self.loaded else "loading")
                self.screen = _screen(self.loaded_tools if self.loaded else _TOOLS_WHILE_LOADING)
            else:
                self.screen = _screen("mcp_running") if self.loaded else _MCP_LOADING
            return
        super().send_special_key(session, window, key)

    def get_history(self, *args, **kwargs):
        if self.buffer == "/mcp" and "/mcp list" in self.screen:
            self.mcp_reads += 1
            if self.loaded:
                self.screen = _screen("mcp_running")
        return self.screen


#: A grant that names one tool of ``cao-mcp-server`` and not the whole server.
PER_TOOL = (["fs_read", "fs_list", "@cao-mcp-server/send_message"], ["cao-mcp-server"])


@pytest.mark.asyncio
async def test_mcp_servers_still_loading_are_read_again_before_tools(cwd):
    """``/mcp`` is read until no server is loading, and only then ``/tools``, so
    the tools panel CAO checks is the one with the server's tools on it."""
    pane = LoadingPane(str(cwd), loaded_tools="tools_readonly_mcp")
    provider = await _launch(pane)
    assert provider.identity_verified is True
    assert pane.mcp_reads >= 3
    assert pane.tools_shown == ["loaded"]
    assert pane.keys == [*"/mcp", "Enter", "Escape", *"/tools", "Enter", "Escape"]


@pytest.mark.asyncio
async def test_a_per_tool_grant_is_not_attested_on_a_tools_panel_read_while_loading(cwd):
    """The blind case (maintainer review on #1179): had ``/tools`` been read while
    the server was loading, it would have shown none of the server's tools and
    passed. Read after loading, it shows tools the grant does not name."""
    pane = LoadingPane(str(cwd), loaded_tools="tools_readonly_mcp")
    provider = _provider(allowed=PER_TOOL[0])
    with pytest.raises(KiroAttestationError) as excinfo:
        await _launch(pane, grant=PER_TOOL, provider=provider)
    assert excinfo.value.code == "kiro_v3_tools_mismatch"
    assert provider.identity_verified is False
    assert pane.tools_shown == ["loaded"]
    assert pane.texts == [pane.command]


@pytest.mark.asyncio
async def test_a_server_that_never_finishes_loading_refuses_before_tools_is_read(cwd):
    """The ``/mcp`` re-reads end in a refusal, never in a ``/tools`` read: with a
    per-tool grant, a ``/tools`` panel read while the server loads would show
    none of its tools and pass blind."""
    from cli_agent_orchestrator.providers import kiro_cli

    pane = LoadingPane(
        str(cwd),
        loaded_tools="tools_readonly_mcp",
        loading_reads=kiro_cli.V3_MCP_LOADING_READS + 1,
    )
    provider = _provider(allowed=PER_TOOL[0])
    with pytest.raises(KiroAttestationError) as excinfo:
        await _launch(pane, grant=PER_TOOL, provider=provider)
    assert excinfo.value.code == "kiro_v3_mcp_not_ready"
    assert provider.identity_verified is False
    assert pane.tools_shown == []
    assert pane.keys == [*"/mcp", "Enter", "Escape"]


@pytest.mark.asyncio
async def test_a_per_tool_grant_whose_pane_shows_only_that_tool_attests(cwd):
    """The server a per-tool ref names is a granted server here too (#1178 review)."""
    pane = LoadingPane(str(cwd), loaded_tools=_tools_panel_with(["@cao-mcp-server/send_message"]))
    provider = _provider(allowed=PER_TOOL[0])
    await _launch(pane, grant=PER_TOOL, provider=provider)
    assert provider.identity_verified is True
    assert pane.tools_shown == ["loaded"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ready", "code"),
    [("trust_dialog", "kiro_v3_trust_dialog"), ("v2_upsell", "kiro_v3_startup_dialog")],
)
async def test_a_startup_dialog_refuses_and_no_key_is_sent(cwd, ready, code):
    pane = FakePane(str(cwd), ready=ready)
    with pytest.raises(KiroV3LaunchError) as excinfo:
        await _launch(pane, status=TerminalStatus.WAITING_USER_ANSWER)
    assert excinfo.value.code == code
    assert pane.keys == []
    assert pane.texts == [pane.command]


def _unrecognised_selector() -> str:
    """The captured approval block retitled as a dialog CAO has no pattern for
    (shaped like v2's trust-all consent; v3's has not been captured)."""
    screen = (
        _screen("approval")
        .replace("Create marker file p10-shell requires approval", "Accept trust-all tools?")
        .replace("❯ Allow once", "❯ Yes, I accept")
        .replace("   Deny once", "   No, exit")
        .replace("Create deny rule to block future similar actions", "Yes, and don't ask again")
    )
    assert screen != _screen("approval")
    return screen


async def _launch_with_the_real_wait(pane, status):
    """``_launch`` with the real ``wait_until_status``, so its poll hook runs."""
    provider = _provider()
    with (
        patch(f"{_KC}.get_backend", return_value=pane),
        patch(f"{_KC}.wait_for_shell", return_value=True),
        patch(f"{_KC}.get_server_settings", return_value={"provider_init_timeout": 30}),
        patch(f"{_KC}.load_agent_profile", side_effect=FileNotFoundError("no profile")),
        patch("cli_agent_orchestrator.services.status_monitor.status_monitor") as monitor,
    ):
        monitor.get_status.return_value = status
        await provider.initialize()
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ready", "status", "code", "words"),
    [
        (_unrecognised_selector(), TerminalStatus.PROCESSING, "kiro_v3_startup_dialog", "not"),
        ("trust_declined_exit", TerminalStatus.ERROR, "kiro_v3_trust_dialog", "declined"),
    ],
    ids=["unrecognised_selector", "trust_declined"],
)
async def test_a_startup_dialog_the_status_cannot_express_refuses_at_once(
    cwd, ready, status, code, words
):
    """Neither waits out the 30 s init timeout: the wait's poll hook reads the
    pane and refuses on the first poll, with no key sent (#1167 review)."""
    import time

    pane = FakePane(str(cwd), ready=ready)
    start = time.monotonic()
    with pytest.raises(KiroV3LaunchError) as excinfo:
        await _launch_with_the_real_wait(pane, status)
    assert time.monotonic() - start < 5
    assert excinfo.value.code == code
    assert words in str(excinfo.value)
    assert pane.keys == []
    assert pane.texts == [pane.command]


def test_only_a_selector_with_no_prompt_below_is_a_startup_selector():
    from cli_agent_orchestrator.providers.kiro_cli import kiro_v3_startup_selector

    assert kiro_v3_startup_selector(_unrecognised_selector()) is True
    assert kiro_v3_startup_selector(_screen("trust_dialog")) is True
    assert kiro_v3_startup_selector(_screen("idle")) is False
    # A selector the user already answered, with the prompt back below it, is history.
    assert kiro_v3_startup_selector(_unrecognised_selector() + "\n" + _screen("idle")) is False
    # v2's own selector footer is not v3's.
    assert kiro_v3_startup_selector("x\n esc to cancel · ↑↓ to navigate · ↵ to select\n") is False
    # An answered selector, the prompt, then a new selector below it: the new one counts.
    answered_then_new = (
        _unrecognised_selector() + "\n" + _screen("idle") + "\n" + _unrecognised_selector()
    )
    assert kiro_v3_startup_selector(answered_then_new) is True


@pytest.mark.asyncio
async def test_waiting_for_an_answer_on_a_screen_the_hook_does_not_know_refuses(cwd):
    """The status says WAITING_USER_ANSWER, but no v3 dialog or selector is on screen
    (here v2's footer): refused after the wait, not left to the init timeout."""
    unknown = _screen("idle").rstrip("\n") + (
        "\n  Continue with the old engine?\n  ❯ Yes\n    No\n"
        " esc to cancel · ↑↓ to navigate · ↵ to select\n"
    )
    pane = FakePane(str(cwd), ready=unknown)
    with pytest.raises(KiroV3LaunchError) as excinfo:
        await _launch(pane, status=TerminalStatus.WAITING_USER_ANSWER)
    assert excinfo.value.code == "kiro_v3_startup_dialog"
    assert "waiting for an answer" in str(excinfo.value)
    assert pane.keys == []
    assert pane.texts == [pane.command]


@pytest.mark.asyncio
async def test_the_v3_fallback_agent_refuses_before_any_slash_command(cwd):
    pane = FakePane(str(cwd), ready="fallback_not_found")
    with pytest.raises(KiroAgentIdentityError):
        await _launch(pane)
    assert pane.keys == []


@pytest.mark.asyncio
async def test_a_workspace_entry_planted_after_the_first_scan_refuses_at_the_rescan(cwd):
    (cwd / ".kiro" / "hooks").mkdir(parents=True)
    pane = FakePane(str(cwd))
    with pytest.raises(KiroWorkspaceError) as excinfo:
        await _launch(pane)
    assert excinfo.value.code == "kiro_v3_workspace_config:hooks"
    assert pane.keys == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tools", "why"),
    [("tools_default", "shell"), ("tools_none", "no tools"), ("tools_read_tag_write", "write")],
)
async def test_a_tools_panel_beyond_the_grant_refuses_and_types_no_task(cwd, tools, why):
    pane = FakePane(str(cwd), tools=tools)
    provider = _provider()
    with pytest.raises(KiroAttestationError, match=why):
        await _launch(pane, provider=provider)
    assert provider.identity_verified is False
    assert pane.texts == [pane.command]
    # /mcp is read first; the /tools refusal follows it.
    assert pane.keys == [*"/mcp", "Enter", "Escape", *"/tools", "Enter", "Escape"]


@pytest.mark.asyncio
async def test_the_tab_palette_is_never_entered(cwd):
    pane = FakePane(str(cwd), typed_tools="palette_tools_tab")
    with pytest.raises(KiroAttestationError, match="palette did not appear"):
        await _launch(pane)
    assert pane.keys == [*"/mcp", "Enter", "Escape", *"/tools", "Escape"]


@pytest.mark.asyncio
async def test_decoy_mcp_servers_refuse(cwd):
    pane = FakePane(str(cwd), mcp=("mcp_decoys_shown",))
    with pytest.raises(KiroAttestationError, match="extra"):
        await _launch(pane)
    assert pane.keys == [*"/mcp", "Enter", "Escape"]  # /tools was never typed
    assert pane.texts == [pane.command]


@pytest.mark.asyncio
async def test_a_non_universal_installed_file_refuses_before_typing(cwd):
    pane = FakePane(str(cwd))
    with pytest.raises(KiroAttestationError, match="not a v3 universal file"):
        await _launch(pane, grant=None)
    assert pane.keys == []


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [None, ["*"], ["fs_read", "*"]])
async def test_an_unrestricted_launch_grant_refuses_before_typing(cwd, allowed):
    pane = FakePane(str(cwd))
    provider = _provider(allowed=allowed)
    with pytest.raises(KiroAttestationError, match="restricted allowlist"):
        await _launch(pane, provider=provider)
    assert provider.identity_verified is False
    assert pane.keys == []


@pytest.mark.asyncio
async def test_an_installed_grant_wider_than_the_launch_refuses_before_typing(cwd):
    """A file rewritten after the pre-allocation check cannot widen the comparison."""
    pane = FakePane(str(cwd))
    wider = (["fs_read", "fs_list", "fs_write", "@cao-mcp-server"], ["cao-mcp-server"])
    with pytest.raises(KiroAttestationError, match=r"exceeds this launch's: \['fs_write'\]"):
        await _launch(pane, grant=wider)
    assert pane.keys == []


@pytest.mark.asyncio
async def test_an_installed_file_configuring_an_ungranted_server_refuses_before_typing(cwd):
    pane = FakePane(str(cwd))
    extra = (["fs_read", "fs_list", "@cao-mcp-server"], ["cao-mcp-server", "decoy"])
    with pytest.raises(KiroAttestationError, match=r"ungranted servers \['decoy'\]"):
        await _launch(pane, grant=extra)
    assert pane.keys == []


@pytest.mark.asyncio
async def test_a_narrower_installed_grant_is_what_the_pane_is_held_to(cwd):
    """The launch allows writes, the file does not: a write tag still refuses."""
    pane = FakePane(str(cwd), tools="tools_read_tag_write")
    provider = _provider(allowed=["fs_read", "fs_list", "fs_write", "@cao-mcp-server"])
    with pytest.raises(KiroAttestationError, match="write"):
        await _launch(pane, provider=provider)
    assert provider.identity_verified is False


@pytest.mark.asyncio
async def test_kiro_effort_is_passed_on_v3_and_never_on_v2(cwd):
    profile = AgentProfile(name="v3ro", description="d", kiroConfig={"effort": "high"})
    pane = FakePane(str(cwd))
    with patch(f"{_KC}.load_agent_profile", return_value=profile):
        await _launch_with_profile(pane)
    argv = shlex.split(pane.command)
    assert argv[argv.index("--effort") + 1] == "high"

    v2_pane = FakePane(str(cwd), ready="plain")
    provider = KiroCliProvider("t1234567", "sess", "win", "v3ro", engine=KiroEngine.V2)
    with (
        patch(f"{_KC}.get_backend", return_value=v2_pane),
        patch(f"{_KC}.wait_for_shell", return_value=True),
        patch(f"{_KC}.wait_until_status", return_value=True),
        patch(f"{_KC}.load_agent_profile", return_value=profile),
    ):
        await provider.initialize()
    assert "--effort" not in shlex.split(v2_pane.command)


@pytest.mark.asyncio
async def test_the_v2_launch_is_unchanged(cwd):
    pane = FakePane(str(cwd), ready="plain")
    provider = KiroCliProvider("t1234567", "sess", "win", "v3ro", engine=KiroEngine.V2)
    with (
        patch(f"{_KC}.get_backend", return_value=pane),
        patch(f"{_KC}.wait_for_shell", return_value=True),
        patch(f"{_KC}.wait_until_status", return_value=True),
        patch(f"{_KC}.load_agent_profile", side_effect=FileNotFoundError("no profile")),
    ):
        await provider.initialize()
    assert pane.command == "kiro-cli chat --agent-engine v2 --trust-all-tools --agent v3ro"
    assert pane.keys == []
    assert provider.identity_verified is True
