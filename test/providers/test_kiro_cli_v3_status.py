"""Kiro v3 status parsing on captured 2.35.1 panes (#1167 Phase 1, gate G6).

The approval picker and the workspace-trust dialog wait for an answer, never
PROCESSING; v2's upsell waits too; a declined trust dialog means Kiro exited.
The patterns apply to v3 terminals only: a v2 provider reads the same screens
exactly as before.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider, kiro_v3_dialog

FIXTURES = Path(__file__).parent / "fixtures"
W = TerminalStatus.WAITING_USER_ANSWER


def _screen(name: str) -> str:
    return (FIXTURES / f"kiro_cli_tui_2_35_1_v3_{name}.txt").read_text(encoding="utf-8")


def _provider(engine=KiroEngine.V3, agent="v3ro"):
    return KiroCliProvider("t1234567", "s", "w", agent, engine=engine)


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("idle", {TerminalStatus.IDLE}),
        ("idle_universal", {TerminalStatus.IDLE}),
        *[(f"busy_{i}", {TerminalStatus.PROCESSING}) for i in range(1, 7)],
        ("done_no_shell", {TerminalStatus.COMPLETED}),
        ("done_deny_at_call", {TerminalStatus.COMPLETED}),
        ("approval", {W}),
        ("approval_deny_selected", {W}),
        ("after_approval_denied", {TerminalStatus.COMPLETED, TerminalStatus.IDLE}),
        ("trust_dialog", {W}),
        ("trust_dialog_down", {W}),
        ("v2_upsell", {W}),
    ],
)
def test_each_d13_fixture_gives_its_status(fixture, expected):
    assert _provider().get_status(_screen(fixture)) in expected


def test_the_trust_dialog_is_never_processing_even_with_startup_text_above():
    screen = "● Initializing...\nThinking... (esc to cancel)\n" + _screen("trust_dialog")
    assert _provider().get_status(screen) == W


def test_a_declined_trust_dialog_is_not_processing():
    assert _provider().get_status(_screen("trust_declined_exit")) == TerminalStatus.ERROR


def test_the_reply_is_extracted_from_a_v3_completion():
    assert "NO_SHELL" in _provider().extract_last_message_from_script(_screen("done_no_shell"))


@pytest.mark.parametrize("fixture", ["exit_after", "quit_after"])
def test_exit_to_the_shell_is_detected(fixture):
    provider = _provider()
    provider._initialized = True
    provider.shell_baseline = "zsh"
    with patch("cli_agent_orchestrator.providers.kiro_cli.get_backend") as backend:
        backend.return_value.get_pane_current_command.return_value = "zsh"
        assert provider.get_status(_screen(fixture)) == TerminalStatus.IDLE


@pytest.mark.parametrize(
    "fixture", ["approval", "trust_dialog", "v2_upsell", "trust_declined_exit"]
)
def test_v2_parsing_of_the_same_screens_is_unchanged(fixture):
    """The v3 dialogs are not v2 states: a v2 provider keeps today's reading."""
    assert _provider(KiroEngine.V2).get_status(_screen(fixture)) == TerminalStatus.PROCESSING


def test_a_quoted_dialog_with_an_idle_prompt_below_is_history():
    screen = _screen("approval") + "\n" + _screen("idle")
    assert kiro_v3_dialog(screen) is None


@pytest.mark.parametrize("missing", ["Allow once", "Deny once", "Create deny rule", "esc to close"])
def test_the_approval_needs_every_option_and_its_footer(missing):
    screen = "\n".join(line for line in _screen("approval").splitlines() if missing not in line)
    assert kiro_v3_dialog(screen) is None


@pytest.mark.parametrize("missing", ["Trust, continue", "Don't trust, exit", "esc to exit"])
def test_the_trust_dialog_needs_every_option_and_its_footer(missing):
    screen = "\n".join(line for line in _screen("trust_dialog").splitlines() if missing not in line)
    assert kiro_v3_dialog(screen) is None


def test_the_generic_list_footer_alone_is_not_a_dialog():
    assert (
        kiro_v3_dialog(" ❯ Something\n   Else\n esc to close · ↑↓ to navigate · ↵ to select\n")
        is None
    )


@pytest.mark.parametrize("engine", [KiroEngine.V2, KiroEngine.V3])
@pytest.mark.parametrize("terminator", ["\x07", "\x1b\\"])
@pytest.mark.parametrize("prefix", ["7501;", " 7501 ; "])
def test_program_status_permission_block(engine, terminator, prefix):
    screen = f"\x1b]{prefix}state=blocked:kind=permission:app=kiro-cli{terminator}"
    assert _provider(engine).get_status(screen) == W


@pytest.mark.parametrize("engine", [KiroEngine.V2, KiroEngine.V3])
def test_program_status_working_supersedes_permission_block(engine):
    screen = (
        "\x1b]7501;state=blocked:kind=permission:app=kiro-cli\x07"
        "\x1b]7501;state=working:app=kiro-cli\x07"
    )
    assert _provider(engine).get_status(screen) == TerminalStatus.PROCESSING


@pytest.mark.parametrize("engine", [KiroEngine.V2, KiroEngine.V3])
@pytest.mark.parametrize("state,fixture", [("idle", "idle"), ("done", "done_no_shell")])
def test_program_status_answered_permission_preserves_existing_verdict(engine, state, fixture):
    provider = _provider(engine)
    screen = _screen(fixture)
    expected = provider.get_status(screen)
    assert expected in {TerminalStatus.IDLE, TerminalStatus.COMPLETED}
    history = (
        "\x1b]7501;state=blocked:kind=permission:app=kiro-cli\x07"
        f"\x1b]7501;state={state}:app=kiro-cli\x07\n"
    )
    assert provider.get_status(history + screen) == expected


@pytest.mark.parametrize("engine", [KiroEngine.V2, KiroEngine.V3])
def test_program_status_other_block_kind_falls_through(engine):
    screen = "\x1b]7501;state=blocked:kind=something-else:app=kiro-cli\x07"
    assert _provider(engine).get_status(screen) == TerminalStatus.PROCESSING


@pytest.mark.parametrize("engine", [KiroEngine.V2, KiroEngine.V3])
def test_program_status_permission_block_overrides_thinking_ghost_text(engine):
    screen = (
        "Thinking... (esc to cancel)\n" "\x1b]7501;state=blocked:kind=permission:app=kiro-cli\x07"
    )
    assert _provider(engine).get_status(screen) == W


@pytest.mark.parametrize("engine", [KiroEngine.V2, KiroEngine.V3])
def test_program_status_exit_to_shell_ends_permission_block(engine):
    provider = _provider(engine)
    provider._initialized = True
    provider.shell_baseline = "zsh"
    history = "\x1b]7501;state=blocked:kind=permission:app=kiro-cli\x07\n"
    with patch("cli_agent_orchestrator.providers.kiro_cli.get_backend") as backend:
        backend.return_value.get_pane_current_command.return_value = "zsh"
        assert provider.get_status(history + _screen("exit_after")) == TerminalStatus.IDLE


@pytest.mark.parametrize("engine", [KiroEngine.V2, KiroEngine.V3])
@pytest.mark.parametrize("fixture", ["idle", "after_approval_denied"])
def test_program_status_later_idle_ends_permission_block(engine, fixture):
    provider = _provider(engine)
    screen = _screen(fixture)
    expected = provider.get_status(screen)
    history = "\x1b]7501;state=blocked:kind=permission:app=kiro-cli\x07\n"
    assert expected != W
    assert provider.get_status(history + screen) == expected


@pytest.mark.parametrize("engine", [KiroEngine.V2, KiroEngine.V3])
def test_program_status_permission_key_order(engine):
    screen = "\x1b]7501;app=kiro-cli:state=blocked:kind=permission\x07"
    assert _provider(engine).get_status(screen) == W


@pytest.mark.parametrize("engine", [KiroEngine.V2, KiroEngine.V3])
def test_program_status_permission_kind_is_exact(engine):
    screen = "\x1b]7501;state=blocked:kind=permissionx:app=kiro-cli\x07"
    assert _provider(engine).get_status(screen) == TerminalStatus.PROCESSING


@pytest.mark.parametrize("engine", [KiroEngine.V2, KiroEngine.V3])
@pytest.mark.parametrize("app", ["omp", "kiro-cli2"])
def test_program_status_permission_app_is_exact(engine, app):
    screen = f"\x1b]7501;state=blocked:kind=permission:app={app}\x07"
    assert _provider(engine).get_status(screen) == TerminalStatus.PROCESSING


@pytest.mark.parametrize("engine", [KiroEngine.V2, KiroEngine.V3])
@pytest.mark.parametrize(
    "fixture,agent",
    [
        ("kiro_cli_permission_stale_answered.txt", "cao-workspace-expert"),
        ("kiro_cli_permission_stale_long_response.txt", "cao-query-decomposer-supervisor"),
    ],
)
def test_program_status_legacy_answered_permission_preserves_existing_verdict(
    engine, fixture, agent
):
    provider = _provider(engine, agent=agent)
    screen = (FIXTURES / fixture).read_text(encoding="utf-8")
    expected = provider.get_status(screen)
    history = "\x1b]7501;state=blocked:kind=permission:app=kiro-cli\x07\n"
    assert expected != W
    assert provider.get_status(history + screen) == expected


@pytest.mark.parametrize("engine", [KiroEngine.V2, KiroEngine.V3])
@pytest.mark.parametrize(
    "payload,expected",
    [
        ("state=working:state=blocked:kind=permission:app=kiro-cli", W),
        ("state=blocked:state=working:kind=permission:app=kiro-cli", TerminalStatus.PROCESSING),
        ("kind=permission:app=kiro-cli", W),
    ],
)
def test_program_status_duplicate_keys_and_missing_state(engine, payload, expected):
    screen = "\x1b]7501;state=blocked:kind=permission:app=kiro-cli\x07" f"\x1b]7501;{payload}\x07"
    assert _provider(engine).get_status(screen) == expected
