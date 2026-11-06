"""The Kiro v3 provider launch against a scripted fake pane (#1167 Phase 1, G8, G10, G12).

No Kiro runs. The fake pane shows the captured 2.35.1 screens in the order a
real launch would draw them, driven by the keys CAO sends. Every refusal must
leave ``identity_verified`` False and type no task text; the only keys ever
sent are the launch command and the two slash commands with their Enter and
Escape (``/mcp`` once per open), and Down while a ``/tools`` list longer
than the pane scrolls. ``SnapshotPane`` freezes an open ``/mcp`` panel, as
Kiro v3 does, ``ScrollingToolsPane`` scrolls ``/tools`` as Kiro v3 does, and
the provider runs on a fake clock.
"""

from __future__ import annotations

import logging
import re
import shlex
from pathlib import Path
from test.providers.kiro_tools_panel import (
    LIVE_ROWS,
    KiroPane,
    frame,
    last_offset,
    render,
    visible_rows,
)
from typing import Iterable, List, Optional, Sequence
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


class FakeClock:
    """The provider's clock: seconds since the launch, moved only by its own sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: List[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds, *_args, **_kwargs) -> None:
        self.sleeps.append(float(seconds))
        self.now += max(0.0, float(seconds))


async def _launch(
    pane,
    *,
    status=TerminalStatus.IDLE,
    grant=GRANT,
    provider=None,
    clock=None,
    init_timeout=60,
    status_wait=None,
):
    """``initialize`` against ``pane``, on a fake clock and a fixed ``provider_init_timeout``.

    ``status_wait`` replaces ``wait_until_status`` (by default it returns True at once).
    """
    provider = provider or _provider()
    clock = clock or getattr(pane, "clock", None) or FakeClock()
    with (
        patch(f"{_KC}.get_backend", return_value=pane),
        patch(f"{_KC}.wait_for_shell", return_value=True),
        patch(
            f"{_KC}.wait_until_status",
            **({"new": status_wait} if status_wait else {"return_value": True}),
        ),
        patch(f"{_KC}.load_agent_profile", side_effect=FileNotFoundError("no profile")),
        patch(f"{_KC}.get_server_settings", return_value={"provider_init_timeout": init_timeout}),
        patch(f"{_KC}.asyncio.sleep", new=clock.sleep),
        patch(f"{_KC}.time", new=clock),
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


#: The captured ``/mcp`` panel with ``cao-mcp-server`` loading. Kiro's bundled TUI
#: draws ``◌`` for it; no live capture of a loading row exists yet.
_MCP_LOADING = _screen("mcp_running").replace(
    "● running       34 tools", "◌ loading        0 tools"
)
#: ... and with it failed, drawn as the captured decoys are.
_MCP_FAILED = _screen("mcp_running").replace("● running       34 tools", "✗ failed         0 tools")
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

#: The ``/mcp`` panel an open shows, by the server's state at that open.
_MCP_PANELS = {
    "running": _screen("mcp_running"),
    "loading": _MCP_LOADING,
    "failed": _MCP_FAILED,
    "decoys": _screen("mcp_decoys_shown"),
}


class SnapshotPane(FakePane):
    """A pane whose ``/mcp`` panel is a snapshot, as Kiro v3's is (#1167).

    Kiro v3's TUI copies its server list once, when ``/mcp`` opens, and never
    updates an open panel: the engine's status notices update only that list,
    and the panel's own updater is fed by a v2-engine notice alone. So an open
    ``/mcp`` panel shows the server's state at the Enter that opened it, however
    often it is read and however much time passes; only closing it and opening
    it again shows a new state. ``/tools`` is live: it shows the server's tools
    once the server is running.

    The server's state at the n-th open is ``states[n - 1]`` (the last one
    repeats); with ``ready_at`` it is ``loading`` until the clock reaches that
    second, then ``running``. Keys typed while a panel is open go to its search
    box, never to the prompt; a dialog takes every key typed while it is on
    screen. ``palette_lag``, ``open_lag`` and ``close_lag`` reads still show the
    old frame after the last key of a slash command, an Enter or an Escape, as a
    render can trail the key; ``close_frame`` replaces the old frame after an
    Escape (a blank redraw, or a screen that is neither the panel nor the prompt).
    The provider waits ``V3_ATTEST_READ_INTERVAL_S`` between reads, so on the
    fake clock each lagging read costs time, as it would live.
    """

    def __init__(
        self,
        cwd: str,
        *,
        states: Sequence[str] = ("loading", "running"),
        ready_at: Optional[float] = None,
        loaded_tools: str = "tools_readonly_mcp",
        palette_fails_at: Optional[int] = None,
        dialog_after_close: Optional[int] = None,
        dialog: str = "approval",
        palette_lag: int = 0,
        open_lag: int = 0,
        close_lag: int = 0,
        close_frame: Optional[str] = None,
        max_opens: int = 200,
    ):
        super().__init__(cwd)
        self.clock = FakeClock()
        self.states = list(states)
        self.ready_at = ready_at
        self.loaded_tools = loaded_tools
        self.palette_fails_at = palette_fails_at
        self.dialog_after_close = dialog_after_close
        self.dialog = dialog
        self.palette_lag = palette_lag
        self.open_lag = open_lag
        self.close_lag = close_lag
        self.close_frame = close_frame
        self.max_opens = max_opens
        self.open_panel: Optional[str] = None
        self.dialog_shown = False
        #: Frames a read shows before ``screen``: a render trailing a key.
        self.frames: List[str] = []
        #: The server's state each ``/mcp`` open showed, taken at its Enter.
        self.snapshots: List[str] = []
        self.closes = 0
        #: Keys typed into an open panel's search box.
        self.search = ""
        self.keys_into_dialog: List[str] = []
        self.tools_shown: List[str] = []

    def _state_at_open(self, number: int) -> str:
        if self.ready_at is not None:
            return "running" if self.clock.now >= self.ready_at else "loading"
        return self.states[min(number, len(self.states)) - 1]

    def _server_running(self) -> bool:
        if self.ready_at is not None:
            return self.clock.now >= self.ready_at
        return bool(self.snapshots) and self.snapshots[-1] == "running"

    def _open(self, panel: str, screen: str) -> None:
        self.frames = [self.screen] * self.open_lag
        self.open_panel = panel
        self.screen = screen

    def send_special_key(self, _session, _window, key):
        self.keys.append(key)
        if self.dialog_shown:
            self.keys_into_dialog.append(key)
            return
        if self.open_panel is not None:
            if key != "Escape":
                self.search += key
                return
            if self.open_panel == "/mcp":
                self.closes += 1
            trailing = self.screen if self.close_frame is None else _screen(self.close_frame)
            self.frames = [trailing] * self.close_lag
            self.open_panel, self.buffer, self.search = None, "", ""
            self.screen = _screen("idle")
            if self.closes == self.dialog_after_close:
                self.dialog_shown = True
                self.screen = _screen(self.dialog)
            return
        if key == "Escape":
            self.buffer = ""
            self.screen = _screen("idle")
        elif key == "Enter":
            if self.buffer == "/mcp" and self.screen == _screen("typed_mcp"):
                number = len(self.snapshots) + 1
                assert number <= self.max_opens, f"/mcp opened {number} times: no deadline"
                state = self._state_at_open(number)
                self.snapshots.append(state)
                self._open("/mcp", _MCP_PANELS[state])
            elif self.buffer == "/tools" and self.screen == _screen(self.typed["/tools"]):
                running = self._server_running()
                self.tools_shown.append("loaded" if running else "loading")
                self._open(
                    "/tools", _screen(self.loaded_tools if running else _TOOLS_WHILE_LOADING)
                )
        else:
            self.buffer += key
            if self.buffer == "/mcp" and self.palette_fails_at == len(self.snapshots) + 1:
                self.screen = _screen("palette_slash_menu")
            elif self.buffer in self.typed:
                self.frames = [self.screen] * self.palette_lag
                self.screen = _screen(self.typed[self.buffer])

    def get_history(self, *_args, **_kwargs):
        if self.frames:
            return self.frames.pop(0)
        return self.screen


def _keys(mcp_opens: int, *, tools: bool = True) -> List[str]:
    """The keys of ``mcp_opens`` opens of ``/mcp``, each closed, then of ``/tools``."""
    return [*"/mcp", "Enter", "Escape"] * mcp_opens + (
        [*"/tools", "Enter", "Escape"] if tools else []
    )


#: A grant that names one tool of ``cao-mcp-server`` and not the whole server.
PER_TOOL = (["fs_read", "fs_list", "@cao-mcp-server/send_message"], ["cao-mcp-server"])


def _type(pane, keys) -> None:
    for key in keys:
        pane.send_special_key("sess", "win", key)


def test_the_snapshot_pane_never_changes_an_open_mcp_panel(cwd):
    """The double itself (the old one turned an open panel to running on a re-read)."""
    pane = SnapshotPane(str(cwd), ready_at=2.0)
    _type(pane, [*"/mcp", "Enter"])
    assert "◌ loading" in pane.get_history()
    pane.clock.now = 30.0
    assert all("◌ loading" in pane.get_history() for _ in range(50))
    _type(pane, ["Escape", *"/mcp", "Enter"])
    assert "● running" in pane.get_history()
    assert pane.snapshots == ["loading", "running"]
    # Keys typed into an open panel never reach the prompt's palette.
    _type(pane, [*"/mcp", "Enter"])
    assert pane.search == "/mcpEnter"
    assert pane.snapshots == ["loading", "running"]


@pytest.mark.asyncio
async def test_a_server_loading_at_the_first_open_is_seen_running_on_a_reopen(cwd):
    """``/mcp`` is opened again until no server is loading, and only then ``/tools``,
    so the tools panel CAO checks is the one with the server's tools on it."""
    pane = SnapshotPane(str(cwd), states=("loading", "loading", "running"))
    provider = await _launch(pane)
    assert provider.identity_verified is True
    assert pane.snapshots == ["loading", "loading", "running"]
    assert pane.keys == _keys(3)
    assert pane.tools_shown == ["loaded"]
    assert pane.texts == [pane.command]
    assert pane.search == "" and pane.open_panel is None
    assert provider.attestation.mcp_tools == (("cao-mcp-server", 34),)
    # 1 s before the second open, 2 s before the third; no other wait.
    assert pane.clock.sleeps == [1.0, 2.0]


@pytest.mark.asyncio
async def test_regression_mcp_is_opened_again_not_read_again(cwd):
    """The bug a live kiro-cli 3.0.0 run found: CAO opened ``/mcp`` before
    cao-mcp-server had connected, read that frozen panel 40 times, and refused
    every v3 helper with ``kiro_v3_mcp_not_ready: MCP servers still loading``.
    Here the server is up 2 s after the first open; opening the panel again
    shows it."""
    pane = SnapshotPane(str(cwd), ready_at=2.0)
    provider = await _launch(pane)
    assert provider.identity_verified is True
    # Opened at 0 s and 1 s (loading), then at 3 s (running).
    assert pane.snapshots == ["loading", "loading", "running"]
    assert pane.keys == _keys(3)
    assert pane.tools_shown == ["loaded"]


@pytest.mark.asyncio
async def test_a_server_slower_than_the_old_20_s_wait_attests_within_provider_init_timeout(cwd):
    pane = SnapshotPane(str(cwd), ready_at=40.0)
    provider = await _launch(pane, init_timeout=60)
    assert provider.identity_verified is True
    assert pane.snapshots.count("running") == 1 and pane.snapshots[-1] == "running"
    assert pane.clock.now == 42.0  # opens every 3 s after the first two waits
    assert pane.keys == _keys(len(pane.snapshots))


@pytest.mark.asyncio
@pytest.mark.parametrize(("init_timeout", "opens"), [(10, 6), (45, 17), (60, 22)])
async def test_a_server_that_never_finishes_loading_refuses_before_tools_is_read(
    cwd, init_timeout, opens
):
    """The reopens end at ``provider_init_timeout`` in a refusal, never in a
    ``/tools`` read: with a per-tool grant, a ``/tools`` panel read while the
    server loads would show none of its tools and pass blind. 45 s is past the
    old fixed wait of about 20 s; 60 s is the default. Reads cost no time here; for
    opens that do, see ``test_the_deadline_counts_the_time_spent_inside_each_open``."""
    pane = SnapshotPane(str(cwd), states=("loading",))
    provider = _provider(allowed=PER_TOOL[0])
    with pytest.raises(KiroAttestationError) as excinfo:
        await _launch(pane, grant=PER_TOOL, provider=provider, init_timeout=init_timeout)
    assert excinfo.value.code == "kiro_v3_mcp_not_ready"
    assert str(excinfo.value) == (
        f"kiro_v3_mcp_not_ready: MCP servers still loading after {init_timeout:.1f} s "
        f"({opens} opens of /mcp): ['cao-mcp-server']"
    )
    assert pane.clock.now == init_timeout
    assert pane.snapshots == ["loading"] * opens
    assert pane.keys == _keys(opens, tools=False)
    assert pane.open_panel is None
    assert pane.tools_shown == []
    assert provider.identity_verified is False
    assert pane.texts == [pane.command]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "words"),
    [
        ("failed", "cao-mcp-server is '✗ failed' with 0 tools"),
        ("decoys", "extra ['kv3-user-decoy', 'kv3-ws-decoy']"),
    ],
)
async def test_a_failed_server_or_new_servers_on_a_reopen_refuse_at_once(cwd, state, words):
    pane = SnapshotPane(str(cwd), states=("loading", state, "running"))
    provider = _provider()
    with pytest.raises(KiroAttestationError) as excinfo:
        await _launch(pane, provider=provider)
    assert excinfo.value.code == "kiro_v3_mcp_not_ready"
    assert words in str(excinfo.value)
    assert pane.snapshots == ["loading", state]  # no further open
    assert pane.keys == _keys(2, tools=False)
    assert pane.open_panel is None
    assert provider.identity_verified is False


@pytest.mark.asyncio
async def test_a_palette_that_does_not_appear_on_a_reopen_refuses(cwd):
    pane = SnapshotPane(str(cwd), states=("loading", "running"), palette_fails_at=2)
    provider = _provider()
    with pytest.raises(KiroAttestationError) as excinfo:
        await _launch(pane, provider=provider)
    assert excinfo.value.code == "kiro_v3_mcp_not_ready"
    assert str(excinfo.value).endswith("the /mcp palette did not appear; nothing was selected")
    assert pane.keys == [*_keys(1, tools=False), *"/mcp", "Escape"]
    assert pane.snapshots == ["loading"]
    assert provider.identity_verified is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dialog",
    ["approval", "trust_dialog", "unrecognised_selector", "approval_below_the_prompt"],
)
@pytest.mark.parametrize(
    "states", [("loading", "running"), ("running",)], ids=["between_opens", "before_tools"]
)
async def test_a_dialog_once_mcp_closes_refuses_and_nothing_is_typed_into_it(cwd, dialog, states):
    """``approval_below_the_prompt`` is drawn under a visible prompt: a dialog on
    screen refuses even when the prompt's placeholder shows above it."""
    screens = {
        "unrecognised_selector": _unrecognised_selector(),
        "approval_below_the_prompt": _screen("idle").rstrip("\n") + "\n" + _screen("approval"),
    }
    screen = screens.get(dialog, dialog)
    pane = SnapshotPane(str(cwd), states=states, dialog_after_close=1, dialog=screen)
    provider = _provider()
    with pytest.raises(KiroAttestationError) as excinfo:
        await _launch(pane, provider=provider)
    assert excinfo.value.code == "kiro_v3_mcp_not_ready"
    assert "a dialog appeared after /mcp closed" in str(excinfo.value)
    assert pane.keys == _keys(1, tools=False)
    assert pane.keys_into_dialog == []
    assert pane.clock.now == 0.0  # refused at once, not at the deadline
    assert provider.identity_verified is False


@pytest.mark.asyncio
async def test_frames_trailing_an_enter_or_an_escape_are_read_again(cwd):
    from cli_agent_orchestrator.providers import kiro_cli

    lag = kiro_cli.V3_ATTEST_READS - 1
    pane = SnapshotPane(str(cwd), states=("loading", "running"), open_lag=lag, close_lag=lag)
    provider = await _launch(pane)
    assert provider.identity_verified is True
    # The snapshot is the state at the Enter, whenever its frame is drawn.
    assert pane.snapshots == ["loading", "running"]
    assert pane.keys == _keys(2)


@pytest.mark.asyncio
async def test_an_mcp_panel_that_is_never_drawn_refuses_after_the_render_wait(cwd):
    from cli_agent_orchestrator.providers import kiro_cli

    pane = SnapshotPane(str(cwd), states=("loading", "running"), open_lag=kiro_cli.V3_ATTEST_READS)
    with pytest.raises(KiroAttestationError) as excinfo:
        await _launch(pane)
    assert excinfo.value.code == "kiro_v3_mcp_not_ready"
    assert "no complete /mcp list panel on screen" in str(excinfo.value)
    assert pane.keys == _keys(1, tools=False)


@pytest.mark.asyncio
async def test_a_prompt_that_does_not_come_back_after_mcp_closes_refuses(cwd):
    from cli_agent_orchestrator.providers import kiro_cli

    pane = SnapshotPane(str(cwd), states=("loading", "running"), close_lag=kiro_cli.V3_ATTEST_READS)
    with pytest.raises(KiroAttestationError) as excinfo:
        await _launch(pane)
    assert str(excinfo.value) == "kiro_v3_mcp_not_ready: the prompt did not return after /mcp"
    assert pane.keys == _keys(1, tools=False)


#: Screens after Escape that are neither the ``/mcp`` panel nor the empty prompt.
_NEITHER_PANEL_NOR_PROMPT = {"busy_screen": "busy_1", "blank_redraw": "\n" * 40}


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", list(_NEITHER_PANEL_NOR_PROMPT))
@pytest.mark.parametrize(
    "states", [("loading", "running"), ("running",)], ids=["between_opens", "before_tools"]
)
async def test_a_screen_that_is_neither_the_panel_nor_the_prompt_after_escape_refuses(
    cwd, frame, states
):
    """Escape must bring back the empty prompt, not merely take the panel away: a
    blank redraw, or a screen with no prompt, held for the whole wait refuses,
    and nothing more is typed into it (not ``/mcp``, not ``/tools``)."""
    from cli_agent_orchestrator.providers import kiro_cli

    pane = SnapshotPane(
        str(cwd),
        states=states,
        close_lag=kiro_cli.V3_ATTEST_READS,
        close_frame=_NEITHER_PANEL_NOR_PROMPT[frame],
    )
    provider = _provider()
    with pytest.raises(KiroAttestationError) as excinfo:
        await _launch(pane, provider=provider)
    assert str(excinfo.value) == "kiro_v3_mcp_not_ready: the prompt did not return after /mcp"
    assert pane.keys == _keys(1, tools=False)
    assert pane.tools_shown == []
    assert provider.identity_verified is False


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", list(_NEITHER_PANEL_NOR_PROMPT))
async def test_a_screen_without_the_prompt_is_waited_out_within_the_render_wait(cwd, frame):
    from cli_agent_orchestrator.providers import kiro_cli

    pane = SnapshotPane(
        str(cwd),
        states=("loading", "running"),
        close_lag=kiro_cli.V3_ATTEST_READS - 1,
        close_frame=_NEITHER_PANEL_NOR_PROMPT[frame],
    )
    provider = await _launch(pane)
    assert provider.identity_verified is True
    assert pane.snapshots == ["loading", "running"]
    assert pane.keys == _keys(2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("init_timeout", "opens", "reported", "ends_at"),
    [(10, 4, "12.0", 12.0), (60, 13, "61.5", 61.5)],
)
async def test_the_deadline_counts_the_time_spent_inside_each_open(
    cwd, init_timeout, opens, reported, ends_at
):
    """Live, typing ``/mcp``, drawing its panel and getting the prompt back take
    time. Here each open spends 0.5 s on the palette, 1 s on the panel's frame
    and 1 s on the prompt after Escape. The deadline counts that time too, from
    the first ``/mcp`` key, and the refusal comes at the first check after it
    has passed: never more than one open's waits late. The 60 s case is the
    example in docs/kiro-cli.md."""
    from cli_agent_orchestrator.providers import kiro_cli

    pane = SnapshotPane(str(cwd), states=("loading",), palette_lag=1, open_lag=2, close_lag=2)
    with pytest.raises(KiroAttestationError) as excinfo:
        await _launch(pane, init_timeout=init_timeout)
    assert str(excinfo.value) == (
        f"kiro_v3_mcp_not_ready: MCP servers still loading after {reported} s "
        f"({opens} opens of /mcp): ['cao-mcp-server']"
    )
    assert pane.clock.now == ends_at
    # One open's waits: the palette, the panel's frame and the prompt, each at most
    # V3_ATTEST_READS reads apart by V3_ATTEST_READ_INTERVAL_S.
    one_open = 3 * (kiro_cli.V3_ATTEST_READS - 1) * kiro_cli.V3_ATTEST_READ_INTERVAL_S
    assert init_timeout <= pane.clock.now <= init_timeout + one_open
    assert pane.snapshots == ["loading"] * opens
    assert pane.keys == _keys(opens, tools=False)


@pytest.mark.asyncio
async def test_each_mcp_open_is_logged_at_debug(cwd, caplog):
    pane = SnapshotPane(str(cwd), states=("loading", "running"))
    with caplog.at_level(logging.DEBUG, logger=_KC):
        await _launch(pane)
    opens = [r for r in caplog.records if r.name == _KC and "/mcp open" in r.getMessage()]
    assert [r.getMessage() for r in opens] == [
        "kiro_cli: terminal t1234567 /mcp open 1 at 0.0 s: cao-mcp-server loading (0 tools)",
        "kiro_cli: terminal t1234567 /mcp open 2 at 1.0 s: cao-mcp-server running (34 tools)",
    ]
    assert {r.levelno for r in opens} == {logging.DEBUG}


#: Every message CAO may log while a v3 launch is attested in these tests. Any
#: other record, such as a panel row or title, could carry screen text.
_ATTESTATION_LOG_SHAPES = (
    re.compile(r"Profile 'v3ro' not loadable by CAO; skipping --model resolution: no profile"),
    re.compile(r"Profile 'v3ro' sets no Kiro effort: no profile"),
    re.compile(
        r"kiro_cli: terminal t1234567 /mcp open \d+ at \d+\.\d s: "
        r"[\w-]+ (?:loading|running) \(\d+ tools\)(?:, [\w-]+ (?:loading|running) \(\d+ tools\))*"
    ),
    re.compile(
        r"kiro_cli: terminal t1234567 /tools read: \d+ tags, \d+ on screen at a time, \d+ Down"
    ),
)
#: Text that only a pane's screen holds: panel titles, headings, columns and glyphs.
_SCREEN_ONLY = (
    "/mcp list",
    "Configured Servers",
    "Source",
    "local",
    "◌",
    "●",
    "✗",
    "esc to",
    "/tools ·",
    "↑",
    "↓",
    "@cao-mcp-server/",
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spec",
    [
        {"states": ("loading", "running")},
        {"states": ("loading",)},
        {"states": ("loading", "failed")},
        {"states": ("loading", "decoys")},
        {"states": ("loading", "running"), "dialog_after_close": 1},
        {"states": ("loading", "running"), "palette_fails_at": 2},
        {"states": ("loading", "running"), "close_lag": 6, "close_frame": "busy_1"},
    ],
    ids=["attests", "deadline", "failed", "new_servers", "dialog", "palette", "no_prompt"],
)
async def test_nothing_but_known_messages_is_logged_while_attesting(cwd, caplog, spec):
    """No screen text reaches the log on any path: each record from CAO is one of
    the known messages, and none holds a panel's title, heading, columns or glyphs."""
    pane = SnapshotPane(str(cwd), **spec)
    with caplog.at_level(logging.DEBUG):
        try:
            await _launch(pane, init_timeout=10)
        except KiroAttestationError:
            pass
    records = [r for r in caplog.records if r.name.startswith("cli_agent_orchestrator")]
    assert records
    for record in records:
        message = record.getMessage()
        assert any(shape.fullmatch(message) for shape in _ATTESTATION_LOG_SHAPES), message
        assert not any(text in message for text in _SCREEN_ONLY), message


@pytest.mark.asyncio
@pytest.mark.parametrize(("tools", "why"), [("tools_default", "shell"), ("tools_none", "no tools")])
async def test_tools_is_still_checked_after_mcp_is_opened_again(cwd, tools, why):
    pane = SnapshotPane(str(cwd), states=("loading", "running"), loaded_tools=tools)
    provider = _provider()
    with pytest.raises(KiroAttestationError) as excinfo:
        await _launch(pane, provider=provider)
    assert excinfo.value.code == "kiro_v3_tools_mismatch"
    assert why in str(excinfo.value)
    assert pane.keys == _keys(2)
    assert provider.identity_verified is False


@pytest.mark.asyncio
async def test_a_per_tool_grant_is_not_attested_on_a_tools_panel_read_while_loading(cwd):
    """The blind case (maintainer review on #1179): had ``/tools`` been read while
    the server was loading, it would have shown none of the server's tools and
    passed. Read after loading, it shows tools the grant does not name."""
    pane = SnapshotPane(str(cwd), states=("loading", "running"))
    provider = _provider(allowed=PER_TOOL[0])
    with pytest.raises(KiroAttestationError) as excinfo:
        await _launch(pane, grant=PER_TOOL, provider=provider)
    assert excinfo.value.code == "kiro_v3_tools_mismatch"
    assert provider.identity_verified is False
    assert pane.tools_shown == ["loaded"]
    assert pane.texts == [pane.command]


@pytest.mark.asyncio
async def test_a_per_tool_grant_whose_pane_shows_only_that_tool_attests(cwd):
    """The server a per-tool ref names is a granted server here too (#1178 review)."""
    pane = SnapshotPane(
        str(cwd),
        states=("loading", "running"),
        loaded_tools=_tools_panel_with(["@cao-mcp-server/send_message"]),
    )
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


# A /tools list longer than the pane (kiro-cli 3.0.0) --------------------------

#: The live 3.0.0 helper's grant: 36 tags on ``/tools``, more than fit.
HELPER = (["fs_read", "@cao-mcp-server"], ["cao-mcp-server"])
BACKENDS = ("tmux", "herdr")


def _extra_rows(count: int) -> tuple:
    return tuple(
        (f"@cao-mcp-server/zz_extra_{n:03d}", "mcp", "An extra tool.") for n in range(count)
    )


class ScrollingToolsPane(SnapshotPane):
    """A pane too short for its ``/tools`` list, as on the live 3.0.0 helper run.

    ``/tools`` opens at its first row, and Down and Up move it by one row,
    stopping at its ends. Each frame is drawn as Kiro's TUI draws it
    (``kiro_tools_panel.frame``: chrome, panel, three reserved rows) into a
    ``KiroPane``, so a scrolling frame is taller than the pane and its title
    sits above the pane's top row. Reads follow ``backend``: tmux returns the
    visible pane, or it plus N lines of scrollback; herdr returns the last N
    lines (50 for a "visible" read). The panel is live, so a list that changes
    is redrawn. ``cao-mcp-server`` runs from the first ``/mcp`` open.

    Faults, by the number of the Down they come at: ``skip_at`` moves the list
    by two rows, ``stuck_at`` not at all, ``change_at`` adds a tool to the list
    (and to its title), ``dialog_at`` puts a dialog on screen,
    ``dialog_below_at`` draws one below the panel, ``dialog_above_at`` draws one
    in the chrome above it, and ``notice_below_at`` draws a line of text below
    it. ``dialog_when_done`` puts a dialog up once the last screen has been
    read. ``reachable`` stops Down short of the last screen, drawn with no
    ``↓ more`` (its last rows are never shown), and ``declared`` overrides the
    title's count. ``scroll_lag`` reads still show the old pane after a Down,
    and ``tools_first_frames`` are what reads show after the ``/tools`` Enter.
    ``redraw`` is how ``KiroPane`` draws each frame: Kiro's default
    ``viewport_tail`` (old rows above the pane keep their text), ``clear`` or
    ``keep_old``.
    After the ``/tools`` Escape, ``tools_close_lag`` reads show
    ``tools_close_frame`` (or the old panel), and ``dialog_after_tools_close``
    shows a dialog instead of the prompt.
    """

    def __init__(
        self,
        cwd: str,
        *,
        backend: str = "tmux",
        rows: Sequence[tuple] = LIVE_ROWS,
        height: int = 30,
        declared: Optional[int] = None,
        skip_at: Optional[int] = None,
        stuck_at: Optional[int] = None,
        change_at: Optional[int] = None,
        dialog_at: Optional[int] = None,
        dialog_below_at: Optional[int] = None,
        dialog_above_at: Optional[int] = None,
        notice_below_at: Optional[int] = None,
        dialog_when_done: bool = False,
        reachable: Optional[int] = None,
        scroll_lag: int = 0,
        tools_first_frames: Sequence[str] = (),
        redraw: str = "viewport_tail",
        tools_close_lag: int = 0,
        tools_close_frame: Optional[str] = None,
        dialog_after_tools_close: bool = False,
        **kwargs,
    ):
        kwargs.setdefault("states", ("running",))
        super().__init__(cwd, **kwargs)
        self.backend = backend
        self.rows = list(rows)
        self.height = height
        self.declared = declared
        self.skip_at = skip_at
        self.stuck_at = stuck_at
        self.change_at = change_at
        self.dialog_at = dialog_at
        self.dialog_below_at = dialog_below_at
        self.dialog_above_at = dialog_above_at
        self.notice_below_at = notice_below_at
        self.dialog_when_done = dialog_when_done
        self.reachable = reachable
        self.scroll_lag = scroll_lag
        self.tools_first_frames = list(tools_first_frames)
        self.tools_close_lag = tools_close_lag
        self.tools_close_frame = tools_close_frame
        self.dialog_after_tools_close = dialog_after_tools_close
        self.term = KiroPane(height, redraw=redraw)
        #: Old panes a read shows before ``term``: a redraw trailing a Down.
        self.stale: List[KiroPane] = []
        self.offset = 0
        self.downs = 0
        self.tools_closed = False
        #: Keys sent once Escape had closed ``/tools``.
        self.keys_after_tools_closed: List[str] = []
        #: Down or Up sent while ``/tools`` was not the open panel.
        self.scroll_keys_elsewhere: List[str] = []
        #: The (visible_only, tail_lines) of every read while ``/tools`` was open.
        self.tools_reads: List[tuple] = []

    def _last(self) -> int:
        last = last_offset(len(self.rows), self.height)
        return last if self.reachable is None else min(last, self.reachable)

    def _draw(self) -> None:
        cut_short = self.reachable is not None and self.offset >= self._last()
        lines = frame(
            self.rows,
            height=self.height,
            offset=self.offset,
            declared=self.declared,
            more_below=False if cut_short else None,
        )
        if self.downs and self.downs == self.dialog_above_at:
            lines = [line for line in _screen("approval").splitlines() if line.strip()] + lines
        if self.downs and self.downs == self.dialog_below_at:
            lines += _screen("approval").splitlines()
        if self.downs and self.downs == self.notice_below_at:
            lines += ["• Kiro is checking for updates"]
        self.term.draw(lines)

    def _open(self, panel: str, screen: str) -> None:
        if panel == "/tools":
            self.offset = 0
            self._draw()
        super()._open(panel, screen)
        if panel == "/tools" and self.tools_first_frames:
            self.frames = list(self.tools_first_frames)

    def send_special_key(self, _session, _window, key):
        if self.tools_closed:
            self.keys_after_tools_closed.append(key)
        if key not in ("Down", "Up") or self.dialog_shown:
            was_tools = self.open_panel == "/tools"
            super().send_special_key(_session, _window, key)
            if was_tools and self.open_panel is None:
                self.tools_closed = True
                trailing = self.tools_close_frame or self.term.read(
                    "tmux", tail_lines=0, visible_only=True
                )
                self.frames = [trailing] * self.tools_close_lag
                if self.dialog_after_tools_close:
                    self.dialog_shown = True
                    self.screen = _screen(self.dialog)
            return
        self.keys.append(key)
        if self.open_panel != "/tools":
            self.scroll_keys_elsewhere.append(key)
            return
        self.stale = [self.term.copy() for _ in range(self.scroll_lag)]
        if key == "Down":
            self.downs += 1
            step = {self.skip_at: 2, self.stuck_at: 0}.get(self.downs, 1)
            self.offset = min(self._last(), self.offset + step)
            if self.downs == self.change_at:
                self.rows.append(("@cao-mcp-server/zz_new", "mcp", "A tool that just appeared."))
        else:
            self.offset = max(0, self.offset - 1)
        self._draw()
        if self.downs == self.dialog_at:
            self.dialog_shown = True
            self.screen = _screen(self.dialog)

    def read_as(
        self, backend: str, *, tail_lines: Optional[int] = None, visible_only: bool = False
    ):
        """What ``backend``'s ``get_history`` returns now."""
        if self.frames:
            return self.frames.pop(0)
        if self.open_panel != "/tools" or self.dialog_shown:
            return self.screen
        self.tools_reads.append((visible_only, tail_lines))
        pane = self.stale.pop(0) if self.stale else self.term
        text = pane.read(backend, tail_lines=tail_lines, visible_only=visible_only)
        if (
            self.dialog_when_done
            and not self.stale
            and self.offset == self._last()
            and pane is self.term
            and not visible_only
        ):
            # The last screen has been read; a dialog is up before the next read.
            self.dialog_shown = True
            self.screen = _screen(self.dialog)
        return text

    def get_history(self, _session=None, _window=None, tail_lines=None, **kwargs):
        return self.read_as(
            self.backend, tail_lines=tail_lines, visible_only=bool(kwargs.get("visible_only"))
        )


def _scroll_keys(downs: int, *, escape: bool = True) -> List[str]:
    """One ``/mcp`` open, then ``/tools`` scrolled ``downs`` times and closed."""
    return [*_keys(1, tools=False), *"/tools", "Enter", *["Down"] * downs] + (
        ["Escape"] if escape else []
    )


async def _launch_helper_grant(pane, **kwargs):
    provider = _provider(allowed=HELPER[0])
    try:
        await _launch(pane, grant=HELPER, provider=provider, **kwargs)
    finally:
        pane.provider = provider
    return provider


def test_the_scrolling_pane_moves_by_one_row_and_stops_at_the_ends(cwd):
    """The double itself, against the TUI's rules (``kiro_tools_panel``)."""
    pane = ScrollingToolsPane(str(cwd), height=24)
    _type(pane, [*"/mcp", "Enter", "Escape", *"/tools", "Enter"])
    whole = pane.read_as("tmux", tail_lines=100)
    assert "↑ more" not in whole and "   ↓ more" in whole.splitlines()
    _type(pane, ["Up"])
    assert pane.offset == 0
    _type(pane, ["Down"] * 30)
    whole = pane.read_as("tmux", tail_lines=100)
    assert pane.offset == 21 and "   ↓ more" not in whole.splitlines()
    assert "/tools · 36 tags" in whole
    _type(pane, ["Escape", "Down"])
    assert pane.scroll_keys_elsewhere == ["Down"]
    assert pane.keys_after_tools_closed == ["Down"]


@pytest.mark.parametrize(
    ("redraw", "height"),
    [("viewport_tail", h) for h in (12, 13, 14, 24, 30, 44)]
    + [("clear", h) for h in (5, 9, 13, 14, 24, 30, 44)],
)
def test_a_scrolling_frame_is_taller_than_the_pane_so_its_title_is_above_it(cwd, redraw, height):
    """What 3.0.0's layout means for a read: the frame is the chrome, the panel
    and three reserved rows; with the list scrolling it is taller than the pane,
    so the pane's own rows never hold the title. tmux's visible read misses it;
    the pane plus its recent scrollback holds it once, and on Kiro's default
    redraw (12 rows or more) or the clearing one it reads as the whole frame."""
    from cli_agent_orchestrator.providers.kiro_attest import parse_tools_page

    pane = ScrollingToolsPane(str(cwd), height=height, redraw=redraw)
    _type(pane, [*"/mcp", "Enter", "Escape", *"/tools", "Enter"])
    shown = visible_rows(height)
    for offset in range(last_offset(36, height) + 1):
        assert pane.offset == offset
        visible = pane.read_as("tmux", visible_only=True)
        assert len(visible.split("\n")) == height
        assert "/tools · 36 tags" not in visible
        for backend in BACKENDS:
            whole = pane.read_as(backend, tail_lines=100)
            assert whole.count("/tools · 36 tags") == 1
            page = parse_tools_page(whole)
            assert page.rows == tuple((n, src) for n, src, _ in LIVE_ROWS[offset : offset + shown])
        _type(pane, ["Down"])


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(("height", "downs"), [(30, 15), (24, 21), (14, 31), (13, 31), (12, 31)])
async def test_regression_a_tools_list_longer_than_the_pane_is_read_screen_by_screen(
    cwd, backend, height, downs
):
    """The live 3.0.0 refusal: a helper with ``fs_read`` and ``@cao-mcp-server``
    has 36 tags, more than fit, and Kiro drew ``↓ more`` below the rows that
    did. CAO refused it as ``unrecognized row '↓ more'``. Now it presses Down
    until no row is left below, one key per screen read, and checks all 36.
    Kiro draws the scrolling panel taller than the pane, so the title is read
    from the pane's recent scrollback, on tmux as on herdr, on any pane of 12
    rows or more under Kiro's default redraw."""
    from cli_agent_orchestrator.providers import kiro_cli

    pane = ScrollingToolsPane(str(cwd), backend=backend, height=height)
    provider = await _launch_helper_grant(pane)
    assert provider.identity_verified is True
    assert pane.keys == _scroll_keys(downs)
    assert pane.offset == last_offset(36, height)
    assert pane.keys_after_tools_closed == [] and pane.scroll_keys_elsewhere == []
    assert pane.search == "" and pane.open_panel is None
    assert pane.texts == [pane.command]
    assert provider.attestation.tags == ("read",)
    # Every read of the open panel reached back into the scrollback.
    assert set(pane.tools_reads) == {(False, kiro_cli.V3_TOOLS_READ_LINES)}


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("height", [11, 9, 5])
async def test_a_pane_under_12_rows_refuses_after_one_down(cwd, backend, height):
    """Kiro's default redraw keeps the scrollback: below 12 rows the list's own
    rows sit above the pane, and the first Down's change (the ``↑ more`` row)
    is above it too, so Kiro repaints the pane only and the rows above keep
    their old text. CAO reads one title over a mix of old and new rows, and the
    one-row check refuses it after one Down; Escape is the last key."""
    pane = ScrollingToolsPane(str(cwd), backend=backend, height=height)
    with pytest.raises(KiroAttestationError) as excinfo:
        await _launch_helper_grant(pane)
    assert (
        str(excinfo.value)
        == "kiro_v3_tools_mismatch: the /tools panel did not move down by one row"
    )
    assert pane.keys == _scroll_keys(1)
    assert pane.keys_after_tools_closed == [] and pane.keys_into_dialog == []
    assert pane.provider.identity_verified is False


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("height", [13, 9, 5])
async def test_with_kiro_clearing_its_scrollback_small_panes_attest(cwd, backend, height):
    """The non-default redraw (``chat.preserveScrollback`` off): Kiro clears the
    scrollback and writes the whole frame again, so every read is one coherent
    frame and even a 5-row pane attests."""
    pane = ScrollingToolsPane(str(cwd), backend=backend, height=height, redraw="clear")
    provider = await _launch_helper_grant(pane)
    assert provider.identity_verified is True
    assert pane.keys == _scroll_keys(31)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_every_tag_on_every_screen_is_held_to_the_grant(cwd, backend):
    """A tool that only the last screen shows still refuses, after Escape."""
    names = [name for name, source, _ in LIVE_ROWS if source == "mcp"]
    assert names[-1] == "@cao-mcp-server/workflow_wait"
    grant = (["fs_read", *names[:-1]], ["cao-mcp-server"])
    pane = ScrollingToolsPane(str(cwd), backend=backend)
    provider = _provider(allowed=grant[0])
    with pytest.raises(KiroAttestationError) as excinfo:
        await _launch(pane, grant=grant, provider=provider)
    assert str(excinfo.value) == (
        "kiro_v3_tools_mismatch: MCP tool @cao-mcp-server/workflow_wait is not granted"
    )
    assert pane.keys == _scroll_keys(15)
    assert provider.identity_verified is False
    # The same list with that tool granted attests.
    pane = ScrollingToolsPane(str(cwd), backend=backend)
    provider = _provider(allowed=["fs_read", *names])
    await _launch(pane, grant=(["fs_read", *names], ["cao-mcp-server"]), provider=provider)
    assert provider.identity_verified is True


def _duplicated() -> List[tuple]:
    rows = list(LIVE_ROWS)
    rows[30] = rows[2]
    return rows


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    ("fault", "downs", "detail"),
    [
        ({"skip_at": 4}, 4, "the /tools panel did not move down by one row"),
        ({"stuck_at": 4}, 4, "the /tools panel did not move down by one row"),
        # Kiro's default redraw leaves the old title (36) above the pane: the
        # bound catches the 37th row. A clearing redraw shows the new count at once.
        ({"change_at": 4}, 15, "the /tools panel shows more rows than the 36 it declares"),
        ({"change_at": 4, "redraw": "clear"}, 4, "the /tools panel did not move down by one row"),
        ({"reachable": 14}, 14, "the panel declares 36 tags but 35 are visible"),
        ({"declared": 37}, 15, "the panel declares 37 tags but 36 are visible"),
        (
            {"rows": _duplicated()},
            15,
            "the /tools panel shows ['@cao-mcp-server/assign'] more than once",
        ),
        (
            {"rows": LIVE_ROWS + _extra_rows(4), "declared": 36},
            15,
            "the /tools panel shows more rows than the 36 it declares",
        ),
        # A terminal that keeps every frame's overflow: two titles after a Down.
        ({"redraw": "keep_old"}, 1, "no complete /tools panel on screen"),
        ({"notice_below_at": 3}, 3, "something is drawn below the /tools panel"),
    ],
    ids=[
        "row_skipped",
        "did_not_scroll",
        "list_changed",
        "list_changed_clear",
        "row_hidden",
        "count",
        "twice",
        "endless",
        "old_overflow_kept",
        "text_below",
    ],
)
async def test_a_missing_screen_a_duplicated_or_hidden_tag_or_a_wrong_count_refuses(
    cwd, backend, fault, downs, detail
):
    pane = ScrollingToolsPane(str(cwd), backend=backend, **fault)
    with pytest.raises(KiroAttestationError) as excinfo:
        await _launch_helper_grant(pane)
    assert str(excinfo.value) == f"kiro_v3_tools_mismatch: {detail}"
    # The panel is closed with one Escape, and nothing follows it.
    assert pane.keys == _scroll_keys(downs)
    assert pane.keys_after_tools_closed == []
    assert pane.provider.identity_verified is False


@pytest.mark.asyncio
async def test_a_herdr_pane_too_tall_for_the_read_refuses_and_tmux_reads_it_whole(cwd):
    """The read's bound. A 120-tag list on a 100-row pane: the frame's title is
    110 lines from the bottom. herdr's read is the last 100 lines, so the title
    is outside it and the launch refuses with no Down; tmux's is the pane plus
    100 lines of scrollback, so it is read whole."""
    rows = LIVE_ROWS + _extra_rows(84)
    herdr = ScrollingToolsPane(str(cwd), backend="herdr", rows=rows, height=100)
    with pytest.raises(KiroAttestationError) as excinfo:
        await _launch_helper_grant(herdr)
    assert str(excinfo.value) == "kiro_v3_tools_mismatch: no complete /tools panel on screen"
    assert herdr.keys == _scroll_keys(0)
    tmux = ScrollingToolsPane(str(cwd), backend="tmux", rows=rows, height=100)
    provider = await _launch_helper_grant(tmux)
    assert provider.identity_verified is True
    assert tmux.keys == _scroll_keys(120 - 91)


@pytest.mark.asyncio
@pytest.mark.parametrize(("lag", "attests"), [(5, True), (6, False)])
async def test_a_screen_trailing_a_down_is_read_again_within_the_render_wait(cwd, lag, attests):
    from cli_agent_orchestrator.providers import kiro_cli

    assert lag in (kiro_cli.V3_ATTEST_READS - 1, kiro_cli.V3_ATTEST_READS)
    pane = ScrollingToolsPane(str(cwd), scroll_lag=lag)
    if attests:
        provider = await _launch_helper_grant(pane)
        assert provider.identity_verified is True
        assert pane.keys == _scroll_keys(15)
        return
    with pytest.raises(KiroAttestationError) as excinfo:
        await _launch_helper_grant(pane)
    assert (
        str(excinfo.value)
        == "kiro_v3_tools_mismatch: the /tools panel did not move down by one row"
    )
    assert pane.keys == _scroll_keys(1)


@pytest.mark.asyncio
async def test_a_tools_list_still_scrolling_at_provider_init_timeout_refuses(cwd):
    """Live, each screen takes time to draw. Here the palette trails the typed
    ``/tools`` by one read (0.5 s), the panel's first frame its Enter by two
    (1 s) and each later screen its Down by one (0.5 s). On a 14-row pane 36
    tags need 31 Downs; with a 5 s timeout, counted from the first ``/tools``
    key, CAO stops before the first Down after it and closes the panel. It is
    never more than one screen's render wait late."""
    from cli_agent_orchestrator.providers import kiro_cli

    pane = ScrollingToolsPane(str(cwd), height=14, scroll_lag=1, palette_lag=1, open_lag=2)
    with pytest.raises(KiroAttestationError) as excinfo:
        await _launch_helper_grant(pane, init_timeout=5)
    assert str(excinfo.value) == (
        "kiro_v3_tools_mismatch: the /tools panel was still scrolling after 5 s "
        "(12 of 36 tags read)"
    )
    assert pane.keys == _scroll_keys(7)
    # The one /mcp open took 1.5 s (its palette and its panel's frame) before /tools.
    tools_time = pane.clock.now - 1.5
    assert 5 <= tools_time <= 5 + kiro_cli.V3_ATTEST_RENDER_WAIT_S
    assert tools_time == 5.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spec", "init_timeout", "waited"),
    [
        # A list that fits, read at once: the prompt wait gets the rest of the T.
        ({"height": 50}, 60, 60.0),
        # 31 Downs at 0.5 s each end 0.5 s before a 16 s T: the floor applies.
        ({"height": 14, "scroll_lag": 1}, 16, 2.5),
        # 15 Downs at 0.5 s each: 7.5 s of a 20 s T are gone.
        ({"height": 30, "scroll_lag": 1}, 20, 12.5),
    ],
    ids=["fits", "floor", "remainder"],
)
async def test_the_tools_read_and_the_final_prompt_wait_share_one_timeout(
    cwd, spec, init_timeout, waited
):
    """So a long scroll can't add a fifth ``provider_init_timeout`` to a v3 init:
    the final status wait gets what is left of the T the ``/tools`` read began,
    and never less than one render wait (``utils/init_allowance.py``)."""
    from unittest.mock import AsyncMock

    pane = ScrollingToolsPane(str(cwd), **spec)
    status_wait = AsyncMock(return_value=True)
    await _launch_helper_grant(pane, init_timeout=init_timeout, status_wait=status_wait)
    assert pane.provider.identity_verified is True
    assert status_wait.await_args.kwargs["timeout"] == waited


@pytest.mark.asyncio
@pytest.mark.parametrize(("short_frames", "attests"), [(5, True), (6, False)])
async def test_a_panel_that_fits_but_is_first_drawn_short_is_read_again(cwd, short_frames, attests):
    """A list that fits needs no Down, and its first frame is held to its title as
    before: a frame drawn one row short is read again within the render wait."""
    full = render(LIVE_ROWS, height=50)
    short = "\n".join(line for line in full.splitlines() if "/workflow_wait " not in line)
    assert "↓ more" not in full and short != full
    pane = ScrollingToolsPane(str(cwd), height=50, tools_first_frames=[short] * short_frames)
    if attests:
        provider = await _launch_helper_grant(pane)
        assert provider.identity_verified is True
        assert pane.keys == _scroll_keys(0)
        return
    with pytest.raises(KiroAttestationError) as excinfo:
        await _launch_helper_grant(pane)
    assert str(excinfo.value) == (
        "kiro_v3_tools_mismatch: the panel declares 36 tags but 35 are visible"
    )
    assert pane.keys == _scroll_keys(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_a_dialog_while_tools_scrolls_refuses_and_no_key_is_sent_into_it(cwd, backend):
    pane = ScrollingToolsPane(str(cwd), backend=backend, dialog_at=3)
    with pytest.raises(KiroAttestationError) as excinfo:
        await _launch_helper_grant(pane)
    assert str(excinfo.value) == "kiro_v3_tools_mismatch: no complete /tools panel on screen"
    assert pane.keys == _scroll_keys(3, escape=False)
    assert pane.keys_into_dialog == []
    assert pane.provider.identity_verified is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "detail"),
    [
        ({"dialog_below_at": 2}, "something is drawn below the /tools panel"),
        (
            {"dialog_above_at": 2, "height": 14, "redraw": "clear"},
            "a dialog appeared on /tools; it was not answered and nothing more was typed",
        ),
    ],
    ids=["below", "above"],
)
async def test_a_dialog_drawn_with_the_panel_stops_the_scroll_and_is_not_answered(
    cwd, fault, detail
):
    """The panel still reads cleanly. Below it, the panel is no longer the last
    thing drawn; above it (written into the read by a clearing redraw), only
    the dialog check stops CAO. Either way no Down and no Escape is sent."""
    from cli_agent_orchestrator.providers.kiro_cli import kiro_v3_dialog

    pane = ScrollingToolsPane(str(cwd), **fault)
    with pytest.raises(KiroAttestationError) as excinfo:
        await _launch_helper_grant(pane)
    assert kiro_v3_dialog(pane.read_as("tmux", tail_lines=100)) == "approval"
    assert str(excinfo.value) == f"kiro_v3_tools_mismatch: {detail}"
    assert pane.keys == _scroll_keys(2, escape=False)
    assert pane.provider.identity_verified is False


@pytest.mark.asyncio
async def test_a_dialog_after_the_last_screen_refuses_and_is_not_answered(cwd):
    """The last screen read cleanly, but by the time Escape is due a dialog is up."""
    pane = ScrollingToolsPane(str(cwd), dialog_when_done=True)
    with pytest.raises(KiroAttestationError) as excinfo:
        await _launch_helper_grant(pane)
    assert str(excinfo.value) == (
        "kiro_v3_tools_mismatch: a dialog appeared on /tools; it was not answered "
        "and nothing more was typed"
    )
    assert pane.keys == _scroll_keys(15, escape=False)
    assert pane.keys_into_dialog == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spec", "detail"),
    [
        ({"tools_close_lag": 5}, None),
        ({"tools_close_lag": 5, "tools_close_frame": "busy_1"}, None),
        ({"tools_close_lag": 6}, "the prompt did not return after /tools"),
        (
            {"tools_close_lag": 6, "tools_close_frame": "\n" * 40},
            "the prompt did not return after /tools",
        ),
        (
            {"dialog_after_tools_close": True},
            "a dialog appeared after /tools closed; it was not answered and nothing more was typed",
        ),
    ],
    ids=["trailing_panel", "busy_then_prompt", "panel_held", "blank_held", "dialog"],
)
async def test_the_prompt_must_be_back_on_screen_after_tools_closes(cwd, spec, detail):
    """Read from the pane, as after ``/mcp``, not only through the status monitor:
    after Escape the empty prompt must be back within the render wait. A frame
    that trails the Escape is read again; one that is neither the prompt nor
    the panel, held for the whole wait, refuses, as does a dialog, which is not
    answered. No key follows the Escape."""
    if spec.get("tools_close_frame", "").startswith("busy"):
        spec = {**spec, "tools_close_frame": _screen(spec["tools_close_frame"])}
    pane = ScrollingToolsPane(str(cwd), **spec)
    if detail is None:
        provider = await _launch_helper_grant(pane)
        assert provider.identity_verified is True
    else:
        with pytest.raises(KiroAttestationError) as excinfo:
            await _launch_helper_grant(pane)
        assert str(excinfo.value) == f"kiro_v3_tools_mismatch: {detail}"
        assert pane.provider.identity_verified is False
    assert pane.keys == _scroll_keys(15)
    assert pane.keys_after_tools_closed == []
    assert pane.keys_into_dialog == []


@pytest.mark.parametrize("height", [5, 14, 24, 30])
def test_no_screen_of_a_scrolling_tools_panel_reads_as_a_dialog(height):
    from cli_agent_orchestrator.providers.kiro_cli import kiro_v3_dialog, kiro_v3_startup_selector

    for offset in range(last_offset(len(LIVE_ROWS), height) + 1):
        pane = KiroPane(height)
        pane.draw(frame(LIVE_ROWS, height=height, offset=offset))
        for screen in (pane.capture(0), pane.capture(-100), pane.recent(100)):
            assert kiro_v3_dialog(screen) is None
            assert not kiro_v3_startup_selector(screen)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [{}, {"skip_at": 4}, {"reachable": 14}, {"height": 14, "scroll_lag": 1}],
    ids=["attests", "row_skipped", "row_hidden", "deadline"],
)
async def test_reading_a_scrolling_tools_panel_logs_counts_only(cwd, caplog, fault):
    pane = ScrollingToolsPane(str(cwd), **fault)
    with caplog.at_level(logging.DEBUG):
        try:
            await _launch_helper_grant(pane, init_timeout=5)
        except KiroAttestationError:
            pass
    records = [r for r in caplog.records if r.name.startswith("cli_agent_orchestrator")]
    assert records
    for record in records:
        message = record.getMessage()
        assert any(shape.fullmatch(message) for shape in _ATTESTATION_LOG_SHAPES), message
        assert not any(text in message for text in _SCREEN_ONLY), message
    read = [r.getMessage() for r in records if "/tools read" in r.getMessage()]
    if not fault:
        assert read == [
            "kiro_cli: terminal t1234567 /tools read: 36 tags, 21 on screen at a time, 15 Down"
        ]
    else:
        assert read == []


# The scrolled read through the real backends, over a fake transport ------------


class _BackendSeam:
    """``get_backend()`` for the provider: the real backend's ``get_history`` and
    ``send_special_key``, over ``pane``'s model; everything else from ``pane``."""

    def __init__(self, real, pane: ScrollingToolsPane) -> None:
        self._real = real
        self._pane = pane

    def get_history(self, *args, **kwargs):
        return self._real.get_history(*args, **kwargs)

    def send_special_key(self, *args, **kwargs):
        return self._real.send_special_key(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._pane, name)


class _LibtmuxPane:
    """A libtmux pane: ``capture-pane`` and ``send-keys`` against the Kiro model."""

    def __init__(self, pane: ScrollingToolsPane) -> None:
        self.pane = pane
        self.captures: List[tuple] = []

    def cmd(self, name, *flags):
        from types import SimpleNamespace

        if name == "send-keys":
            assert flags == ("-X", "cancel")
            return SimpleNamespace(stdout=[], stderr=[], returncode=0)
        assert name == "capture-pane"
        self.captures.append(flags)
        assert flags[:2] == ("-p", "-S") and len(flags) == 3, flags
        start = int(flags[2])
        if start == 0:
            text = self.pane.read_as("tmux", visible_only=True)
        else:
            text = self.pane.read_as("tmux", tail_lines=-start)
        return SimpleNamespace(stdout=text.split("\n"), stderr=[], returncode=0)

    def send_keys(self, key, enter=True):
        assert enter is False
        self.pane.send_special_key("sess", "win", key)


@pytest.mark.asyncio
async def test_the_scrolled_read_through_the_tmux_backend(cwd):
    """``TmuxBackend`` → ``TmuxClient``: the ``/tools`` reads are
    ``capture-pane -p -S -100`` (the pane plus 100 lines of scrollback), every
    other read ``-S 0``, and each key a tmux ``send-keys`` of its name."""
    from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend
    from cli_agent_orchestrator.clients.tmux import TmuxClient

    pane = ScrollingToolsPane(str(cwd), backend="tmux", height=24)
    libtmux_pane = _LibtmuxPane(pane)
    with patch("cli_agent_orchestrator.clients.tmux.libtmux"):
        client = TmuxClient()
    with (
        patch.object(client, "_find_session", return_value=object()),
        patch.object(client, "_resolve_pane", return_value=libtmux_pane),
    ):
        provider = await _launch_helper_grant(_BackendSeam(TmuxBackend(client=client), pane))
    assert provider.identity_verified is True
    assert pane.keys == _scroll_keys(21)
    tools_reads = [flags for flags in libtmux_pane.captures if flags != ("-p", "-S", "0")]
    assert set(tools_reads) == {("-p", "-S", "-100")}
    # One per screen (the first, then one after each Down), and the close's dialog check.
    assert len(tools_reads) == 22 + 1


@pytest.mark.asyncio
async def test_the_scrolled_read_through_the_herdr_backend(cwd):
    """``HerdrBackend``: the ``/tools`` reads are ``pane read --source recent
    --lines 100 --format text``, every other read the 50-line "visible" one, and
    each key a ``pane send-keys`` of its name."""
    import subprocess

    from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend

    pane = ScrollingToolsPane(str(cwd), backend="herdr", height=24)
    calls: List[list] = []

    def run_herdr(args, check=True):
        calls.append(list(args))
        if args[:2] == ["pane", "send-keys"]:
            assert args[2] == "p1" and len(args) == 4, args
            pane.send_special_key("sess", "win", args[3])
            return subprocess.CompletedProcess(args, 0, "", "")
        assert args[:5] == ["pane", "read", "p1", "--source", "recent"], args
        assert args[5] == "--lines" and args[7:] == ["--format", "text"], args
        text = pane.read_as("herdr", tail_lines=int(args[6]))
        return subprocess.CompletedProcess(args, 0, text, "")

    with patch.object(HerdrBackend, "_ensure_session_running"):
        herdr = HerdrBackend(send_delay_ms=0)
    with (
        patch.object(herdr, "_resolve_pane_id_from_window", return_value="p1"),
        patch.object(herdr, "_run_herdr", side_effect=run_herdr),
    ):
        provider = await _launch_helper_grant(_BackendSeam(herdr, pane))
    assert provider.identity_verified is True
    assert pane.keys == _scroll_keys(21)
    reads = [call[6] for call in calls if call[:2] == ["pane", "read"]]
    assert set(reads) == {"50", "100"}
    assert reads.count("100") == 22 + 1
    assert ["pane", "send-keys", "p1", "Down"] in calls
