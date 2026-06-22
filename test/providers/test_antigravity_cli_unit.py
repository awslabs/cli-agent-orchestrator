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
    assert "▸" not in out  # thought-process lines filtered
    assert "●" not in out  # tool-call lines filtered
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
