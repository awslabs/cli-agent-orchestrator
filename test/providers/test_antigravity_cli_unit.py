"""Unit tests for the Antigravity CLI (``agy``) provider."""

from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.antigravity_cli import (
    IDLE_FOOTER_PATTERN,
    PROCESSING_FOOTER_PATTERN,
    AntigravityCliProvider,
    ProviderError,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _skip_startup_dialog():
    # initialize() polls the pane to accept agy's workspace-trust dialog; that
    # path is exercised separately. Stub it so the init tests stay fast and
    # independent of the (mocked) get_history return type.
    with patch.object(AntigravityCliProvider, "_handle_startup_dialog", return_value=None):
        yield


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_provider(
    agent_profile=None, allowed_tools=None, model=None, skill_prompt=None
) -> AntigravityCliProvider:
    return AntigravityCliProvider(
        terminal_id="test-tid",
        session_name="test-session",
        window_name="window-0",
        agent_profile=agent_profile,
        allowed_tools=allowed_tools,
        model=model,
        skill_prompt=skill_prompt,
    )


# --------------------------------------------------------------------------- #
# Status detection (against captured live agy TUI fixtures)
# --------------------------------------------------------------------------- #


def test_status_idle_fixture():
    assert make_provider().get_status(load_fixture("agy_idle.txt")) == TerminalStatus.IDLE


def test_status_processing_fixture():
    assert (
        make_provider().get_status(load_fixture("agy_processing.txt")) == TerminalStatus.PROCESSING
    )


def test_status_completed_after_turn():
    p = make_provider()
    p.mark_input_received()  # _turns -> 1
    assert p.get_status(load_fixture("agy_completed.txt")) == TerminalStatus.COMPLETED


def test_status_idle_vs_completed_split_on_turns():
    # Same completed-looking footer is IDLE before the first delivered turn.
    p = make_provider()
    assert p.get_status(load_fixture("agy_completed.txt")) == TerminalStatus.IDLE


def test_status_empty_is_unknown():
    # native=None always falls through (no dispatch-timing guess); on tmux the
    # live-read fallback is a pass-through, so an empty buffer hits agy's own
    # no-output default (UNKNOWN) directly.
    assert make_provider().get_status("") == TerminalStatus.UNKNOWN
    assert make_provider().get_status(None) == TerminalStatus.UNKNOWN


def test_status_processing_takes_priority_over_idle_footer():
    # If both footers appear in the buffer, the live "esc to cancel" tail wins.
    buf = (
        "? for shortcuts\n" * 5
        + ("x" * 3000)
        + "\n⣽  Working...\nesc to cancel   Gemini 3.1 Pro (High)"
    )
    assert make_provider().get_status(buf) == TerminalStatus.PROCESSING


def test_status_waiting_user_answer():
    buf = "Do you want to allow this action? [y/n]\n"
    assert make_provider().get_status(buf) == TerminalStatus.WAITING_USER_ANSWER


def test_status_error():
    assert make_provider().get_status("Error: something exploded\n") == TerminalStatus.ERROR


# --------------------------------------------------------------------------- #
# pyte rendered-screen status detection (get_status_from_screen)
# --------------------------------------------------------------------------- #


def test_provider_opts_into_screen_detection():
    assert make_provider().supports_screen_detection is True


def test_screen_status_idle_when_only_ready_footer():
    # A composited viewport resolves in-place redraws: only the live footer
    # remains. The bottom row carries "? for shortcuts" ⇒ IDLE pre-first-turn.
    screen = [
        "> ",
        "─" * 80,
        "? for shortcuts                                  Gemini 3.1 Pro (High)",
    ]
    assert make_provider().get_status_from_screen(screen) == TerminalStatus.IDLE


def test_screen_status_completed_after_turn():
    p = make_provider()
    p.mark_input_received()
    screen = ["> hi", "  done", "─" * 80, "? for shortcuts        Gemini 3.1 Pro (High)"]
    assert p.get_status_from_screen(screen) == TerminalStatus.COMPLETED


def test_screen_status_processing_footer():
    screen = ["⣽  Working...", "esc to cancel        Gemini 3.1 Pro (High)"]
    assert make_provider().get_status_from_screen(screen) == TerminalStatus.PROCESSING


def test_screen_status_waiting_user_answer():
    screen = ["Do you want to allow this action? [y/n]"]
    assert make_provider().get_status_from_screen(screen) == TerminalStatus.WAITING_USER_ANSWER


def test_screen_status_empty_is_unknown():
    assert make_provider().get_status_from_screen([]) == TerminalStatus.UNKNOWN
    assert make_provider().get_status_from_screen(["   ", ""]) == TerminalStatus.UNKNOWN


def test_screen_resolves_stale_processing_footer_regression():
    """Regression for the init-timeout bug: the append-only stream keeps both
    footers when agy overwrites "esc to cancel" with "? for shortcuts", so the
    raw get_status() latches PROCESSING. A composited pyte viewport (the screen
    path) shows only the final ready footer ⇒ IDLE, so the session reaches
    ready and POST /sessions returns instead of timing out.
    """
    p = make_provider()
    # Raw append-only stream: stale "esc to cancel" survives below the response,
    # with the live ready footer rendered last — raw path wrongly says PROCESSING.
    raw = (
        "> analyze this\n  here is the analysis\n"
        + "esc to cancel   Gemini 3.1 Pro (High)\n"
        + ("\n" * 3)
        + "? for shortcuts   Gemini 3.1 Pro (High)\n"
    )
    assert p.get_status(raw) == TerminalStatus.PROCESSING

    # Composited viewport: the in-place rewrite is resolved, leaving only the
    # ready footer ⇒ IDLE.
    screen = [
        "> analyze this",
        "  here is the analysis",
        "─" * 80,
        "> ",
        "─" * 80,
        "? for shortcuts   Gemini 3.1 Pro (High)",
    ]
    assert p.get_status_from_screen(screen) == TerminalStatus.IDLE


# --------------------------------------------------------------------------- #
# Response extraction
# --------------------------------------------------------------------------- #


def test_extract_completed_response():
    assert (
        make_provider().extract_last_message_from_script(load_fixture("agy_completed.txt"))
        == "PONG"
    )


def test_extract_raises_without_query():
    with pytest.raises(ValueError):
        make_provider().extract_last_message_from_script("no query here\njust text\n")


def test_extract_filters_thought_and_tool_chrome():
    # Captured from a live agy reviewer turn that called cao-mcp-server.
    out = make_provider().extract_last_message_from_script(load_fixture("agy_review_completed.txt"))
    assert "▸" not in out  # thought-process header lines filtered
    assert "●" not in out  # tool-call lines filtered
    # The collapsed-thought title line that follows each "▸ Thought" header
    # ("Prioritizing Tool Usage" / "Prioritizing Tool Specificity") is chrome,
    # not response content, and must not leak into the extracted message.
    assert "Prioritizing" not in out
    assert "CRITICAL BUG" in out  # actual review content preserved
    assert "CHANGES_REQUESTED" in out


# --------------------------------------------------------------------------- #
# Command building
# --------------------------------------------------------------------------- #


def test_build_command_raises_when_binary_missing():
    with patch("cli_agent_orchestrator.providers.antigravity_cli.shutil.which", return_value=None):
        with pytest.raises(ProviderError, match="not found"):
            make_provider()._build_agy_command()


def test_build_command_includes_skip_permissions_and_model():
    with patch(
        "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
        return_value="/usr/local/bin/agy",
    ):
        cmd = make_provider(model="Gemini 3.1 Pro (High)")._build_agy_command()
    assert cmd.startswith("agy --dangerously-skip-permissions")
    assert "--model" in cmd and "Gemini 3.1 Pro (High)" in cmd


def test_build_command_injects_system_prompt_via_i(tmp_path, monkeypatch):
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    profile = AgentProfile(
        name="reviewer_gemini", description="Reviewer", system_prompt="You review code."
    )
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
    ):
        cmd = make_provider(agent_profile="reviewer_gemini")._build_agy_command()
    assert "-i" in cmd
    assert "You review code." in cmd
    assert "Acknowledge your role" in cmd


def test_mcp_registration_writes_config(tmp_path, monkeypatch):
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    cfg = tmp_path / "mcp_config.json"
    profile = AgentProfile(
        name="reviewer_gemini",
        description="Reviewer",
        system_prompt="You review code.",
        mcpServers={"cao-mcp-server": {"command": "uvx", "args": ["cao-mcp-server"]}},
    )
    p = make_provider(agent_profile="reviewer_gemini")
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
        patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
    ):
        p._build_agy_command()
        import json

        data = json.loads(cfg.read_text())
        assert "cao-mcp-server" in data["mcpServers"]
        # CAO_TERMINAL_ID forwarded so cao-mcp-server can resolve the terminal.
        assert data["mcpServers"]["cao-mcp-server"]["env"]["CAO_TERMINAL_ID"] == "test-tid"
        # cleanup removes our entry without clobbering the file.
        p.cleanup()
        data2 = json.loads(cfg.read_text())
        assert "cao-mcp-server" not in data2.get("mcpServers", {})


def test_mcp_registration_resolves_bundled_command(tmp_path, monkeypatch):
    """The bundled bare `cao-mcp-server` command is resolved to a PATH-
    independent invocation before it is written to mcp_config.json. agy reads
    that file at a later launch (persisted=True), so the stable PATH launcher
    is preferred over the versioned interpreter-sibling path."""
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    cfg = tmp_path / "mcp_config.json"
    profile = AgentProfile(
        name="reviewer_gemini",
        description="Reviewer",
        system_prompt="You review code.",
        mcpServers={"cao-mcp-server": {"command": "cao-mcp-server", "args": []}},
    )
    p = make_provider(agent_profile="reviewer_gemini")
    MOD = "cli_agent_orchestrator.utils.mcp_resolution"
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
        patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
        patch(f"{MOD}._sibling_script", return_value="/versioned/venv/bin/cao-mcp-server"),
        patch(f"{MOD}.shutil.which", return_value="/home/u/.local/bin/cao-mcp-server"),
    ):
        p._build_agy_command()
        import json

        entry = json.loads(cfg.read_text())["mcpServers"]["cao-mcp-server"]
        # persisted=True prefers the stable PATH launcher, not the bare command
        # or the versioned sibling.
        assert entry["command"] == "/home/u/.local/bin/cao-mcp-server"
        assert entry["args"] == []


# --------------------------------------------------------------------------- #
# Misc lifecycle
# --------------------------------------------------------------------------- #


def test_exit_cli_is_quit():
    assert make_provider().exit_cli() == "/quit"


def test_mark_input_received_increments_turns():
    p = make_provider()
    assert p._turns == 0
    p.mark_input_received()
    assert p._turns == 1


def test_footer_patterns_smoke():
    import re

    assert re.search(PROCESSING_FOOTER_PATTERN, "esc to cancel")
    assert re.search(IDLE_FOOTER_PATTERN, "? for shortcuts")


def test_blocks_orchestrated_input_while_waiting_user_answer():
    # agy approval/picker prompts consume pasted text, so the provider opts in.
    assert make_provider().blocks_orchestrated_input_while_waiting_user_answer is True


def test_get_idle_pattern_for_log():
    pat = make_provider().get_idle_pattern_for_log()
    import re

    assert re.search(pat, "? for shortcuts")


def test_mcp_config_path_default_location():
    # Exercises the real (un-patched) path resolution.
    path = make_provider()._mcp_config_path()
    assert path.name == "mcp_config.json"
    assert path.parent.name == "config"
    assert path.parent.parent.name == ".gemini"


# --------------------------------------------------------------------------- #
# Command building — skill prompt + soft tool restriction
# --------------------------------------------------------------------------- #


def test_build_command_appends_security_prompt_when_tool_restricted():
    from cli_agent_orchestrator.constants import SECURITY_PROMPT
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    profile = AgentProfile(
        name="reviewer_gemini", description="Reviewer", system_prompt="You review code."
    )
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
    ):
        # Read-only reviewer: restricted tools -> security prompt is appended.
        cmd = make_provider(
            agent_profile="reviewer_gemini", allowed_tools=["fs_read", "fs_list"]
        )._build_agy_command()
    assert SECURITY_PROMPT.split("\n", 1)[0] in cmd


def test_build_command_includes_skill_catalog():
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    profile = AgentProfile(
        name="developer_claude", description="Dev", system_prompt="You write code."
    )
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
    ):
        cmd = make_provider(
            agent_profile="developer_claude", skill_prompt="## Available Skills\n- foo: bar"
        )._build_agy_command()
    assert "Available Skills" in cmd


def test_mcp_registration_accepts_pydantic_mcpserver(tmp_path):
    import json

    from cli_agent_orchestrator.models.agent_profile import AgentProfile, McpServer

    cfg = tmp_path / "mcp_config.json"
    # Pre-existing, non-CAO server must be preserved across merge.
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "keep"}}}))
    profile = AgentProfile(
        name="reviewer_gemini",
        description="Reviewer",
        system_prompt="You review code.",
        mcpServers={"cao-mcp-server": McpServer(command="uvx", args=["cao-mcp-server"])},
    )
    p = make_provider(agent_profile="reviewer_gemini")
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
        patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
    ):
        p._build_agy_command()
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["cao-mcp-server"]["env"]["CAO_TERMINAL_ID"] == "test-tid"
    assert data["mcpServers"]["other"]["command"] == "keep"  # untouched


def test_mcp_registration_recovers_from_corrupt_config(tmp_path):
    import json

    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    cfg = tmp_path / "mcp_config.json"
    cfg.write_text("{ this is not valid json")
    profile = AgentProfile(
        name="reviewer_gemini",
        description="Reviewer",
        mcpServers={"cao-mcp-server": {"command": "uvx", "args": ["cao-mcp-server"]}},
    )
    p = make_provider(agent_profile="reviewer_gemini")
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
        patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
    ):
        p._build_agy_command()  # should not raise; starts fresh
    data = json.loads(cfg.read_text())
    assert "cao-mcp-server" in data["mcpServers"]


@pytest.mark.parametrize("payload", ["[1, 2, 3]", '"just a string"', "42"])
def test_mcp_registration_recovers_from_non_dict_config(tmp_path, payload):
    # The shared ~/.gemini/config/mcp_config.json may hold valid JSON of an
    # unexpected shape (list/string/number). Registration must normalize it
    # rather than raise AttributeError/TypeError on setdefault()/mutation.
    import json

    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    cfg = tmp_path / "mcp_config.json"
    cfg.write_text(payload)
    profile = AgentProfile(
        name="reviewer_gemini",
        description="Reviewer",
        mcpServers={"cao-mcp-server": {"command": "uvx", "args": ["cao-mcp-server"]}},
    )
    p = make_provider(agent_profile="reviewer_gemini")
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
        patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
    ):
        p._build_agy_command()  # must not raise
    data = json.loads(cfg.read_text())
    assert isinstance(data, dict)
    assert "cao-mcp-server" in data["mcpServers"]


def test_mcp_registration_replaces_non_dict_mcpservers(tmp_path):
    # A dict root but a non-dict "mcpServers" value must be replaced, not mutated.
    import json

    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    cfg = tmp_path / "mcp_config.json"
    cfg.write_text(json.dumps({"mcpServers": ["unexpected", "list"]}))
    profile = AgentProfile(
        name="reviewer_gemini",
        description="Reviewer",
        mcpServers={"cao-mcp-server": {"command": "uvx", "args": ["cao-mcp-server"]}},
    )
    p = make_provider(agent_profile="reviewer_gemini")
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
        patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
    ):
        p._build_agy_command()  # must not raise
    data = json.loads(cfg.read_text())
    assert isinstance(data["mcpServers"], dict)
    assert "cao-mcp-server" in data["mcpServers"]


def test_unregister_noop_when_nothing_registered():
    # No servers registered -> cleanup is a no-op, never touches the filesystem.
    p = make_provider()
    p.cleanup()  # must not raise
    assert p._mcp_server_names == []


def test_unregister_handles_missing_file(tmp_path):
    p = make_provider()
    p._mcp_server_names = ["cao-mcp-server"]
    missing = tmp_path / "does_not_exist.json"
    with patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=missing):
        p.cleanup()  # file gone -> just resets state
    assert p._mcp_server_names == []


# --------------------------------------------------------------------------- #
# Extraction — chrome filtering + error paths
# --------------------------------------------------------------------------- #


_RULE = "─" * 30  # input-box separator (SEPARATOR_PATTERN needs >= 20)


def test_extract_filters_survey_banner_and_tip():
    script = (
        f"{_RULE}\n"
        "> what is 2+2\n"
        "  Antigravity CLI 1.0.10\n"  # banner chrome
        "  └ Tip: press something\n"  # tip chrome
        "  ⣽ Working...\n"  # spinner chrome
        "  How's the CLI experience?\n"  # survey chrome
        "  The answer is 4.\n"  # real content
        f"{_RULE}\n"
        ">\n"
    )
    out = make_provider().extract_last_message_from_script(script)
    assert out == "The answer is 4."


def test_extract_raises_on_empty_response():
    # A query followed only by chrome -> no content -> ValueError.
    script = f"{_RULE}\n> hello\n  ⣽ Working...\n{_RULE}\n>\n"
    with pytest.raises(ValueError, match="Empty"):
        make_provider().extract_last_message_from_script(script)


def test_extract_filters_stray_footer_in_body():
    script = f"{_RULE}\n> hi\n  ? for shortcuts\n  Hello there.\n{_RULE}\n>\n"
    assert make_provider().extract_last_message_from_script(script) == "Hello there."


def test_status_unknown_when_no_markers():
    # Non-empty buffer with no footer / spinner / error markers -> UNKNOWN.
    assert make_provider().get_status("just some banner text\n") == TerminalStatus.UNKNOWN


def test_build_command_raises_on_bad_profile():
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            side_effect=RuntimeError("boom"),
        ),
    ):
        with pytest.raises(ProviderError, match="Failed to load agent profile"):
            make_provider(agent_profile="missing")._build_agy_command()


def test_profile_model_overrides_constructor_model():
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    profile = AgentProfile(
        name="reviewer_gemini",
        description="Reviewer",
        system_prompt="You review code.",
        model="Gemini 3.5 Flash (High)",
    )
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
    ):
        cmd = make_provider(
            agent_profile="reviewer_gemini", model="Gemini 3.1 Pro (High)"
        )._build_agy_command()
    assert "Gemini 3.5 Flash (High)" in cmd  # profile wins
    assert "Gemini 3.1 Pro (High)" not in cmd


def test_unregister_warns_on_corrupt_config(tmp_path):
    cfg = tmp_path / "mcp_config.json"
    cfg.write_text("{ not valid json")
    p = make_provider()
    p._mcp_server_names = ["cao-mcp-server"]
    with patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg):
        p.cleanup()  # corrupt file -> logged warning, state reset, no raise
    assert p._mcp_server_names == []


@pytest.mark.parametrize("payload", ["[1, 2, 3]", '"just a string"', "42"])
def test_unregister_handles_non_dict_config(tmp_path, payload):
    # Valid-but-unexpected JSON shape must not raise during teardown, and the
    # internal state must always be cleared (finally block).
    cfg = tmp_path / "mcp_config.json"
    cfg.write_text(payload)
    p = make_provider()
    p._mcp_server_names = ["cao-mcp-server"]
    with patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg):
        p.cleanup()  # non-dict config -> no raise, state reset
    assert p._mcp_server_names == []


# --------------------------------------------------------------------------- #
# launch.py PROVIDERS_REQUIRING_WORKSPACE_ACCESS
# --------------------------------------------------------------------------- #


def test_antigravity_cli_in_workspace_access_set():
    # agy launches with --dangerously-skip-permissions, so it must trigger the
    # unrestricted-launch warning like the other full-access providers.
    from cli_agent_orchestrator.cli.commands.launch import (
        PROVIDERS_REQUIRING_WORKSPACE_ACCESS,
    )

    assert "antigravity_cli" in PROVIDERS_REQUIRING_WORKSPACE_ACCESS


# --------------------------------------------------------------------------- #
# initialize() lifecycle
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_initialize_success(monkeypatch):
    p = make_provider(model="Gemini 3.1 Pro (High)")

    sent = {}

    class FakeBackend:
        def send_keys(self, session, window, command):
            sent["command"] = command

    async def fake_wait_for_shell(tid, timeout):
        return True

    async def fake_wait_until_status(tid, statuses, timeout):
        return True

    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
        lambda b: "/usr/local/bin/agy",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.get_backend", lambda: FakeBackend()
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.wait_for_shell", fake_wait_for_shell
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.wait_until_status",
        fake_wait_until_status,
    )

    assert await p.initialize() is True
    assert p._initialized is True
    assert sent["command"].startswith("agy --dangerously-skip-permissions")


@pytest.mark.asyncio
async def test_initialize_raises_when_shell_times_out(monkeypatch):
    async def fake_wait_for_shell(tid, timeout):
        return False

    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.wait_for_shell", fake_wait_for_shell
    )
    with pytest.raises(TimeoutError, match="Shell"):
        await make_provider().initialize()


@pytest.mark.asyncio
async def test_initialize_raises_when_agy_times_out(monkeypatch):
    class FakeBackend:
        def send_keys(self, session, window, command):
            pass

    async def fake_wait_for_shell(tid, timeout):
        return True

    async def fake_wait_until_status(tid, statuses, timeout):
        return False

    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
        lambda b: "/usr/local/bin/agy",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.get_backend", lambda: FakeBackend()
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.wait_for_shell", fake_wait_for_shell
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.wait_until_status",
        fake_wait_until_status,
    )
    with pytest.raises(TimeoutError, match="initialization timed out"):
        await make_provider().initialize()
