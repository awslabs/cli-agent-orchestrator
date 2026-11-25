"""classify_reason on the raw pipe-pane bytes that ApprovalBridge actually passes.

The fixture is the raw output stream (``tmux pipe-pane``) of Claude Code 2.1.332
under ``--permission-mode default``, stopped at the Write permission dialog
"Do you want to create approved-by-dojo.txt?". It starts at Claude Code's own
output; the working directory is replaced by ``/home/user/project`` and the
account tier by ``Claude Sub``.

Versions: the raw bytes are from 2.1.332 only. The 2.1.331 report came as a
rendered screen with the same dialog text, which cannot show escape bytes. The
2.1.310 layout is covered by the capture-pane fixtures in
``test/providers/fixtures/claude_code_310_*``.

In those bytes Ink draws some word gaps as cursor moves, so the footer arrives
as ``Esc to cancel · Tab\\x1b[22Gto amend``. The status monitor renders the
stream through pyte and sees ``WAITING_USER_ANSWER``, but the classifier used
to search the raw bytes and returned ``claude-code:unknown_prompt``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cli_agent_orchestrator.services.agui.approval_bridge import ApprovalBridge
from cli_agent_orchestrator.services.agui.handoff_approval import (
    AgentHandoffWithApproval,
    classify_reason,
)

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "providers"
    / "fixtures"
    / "claude_code_2332_write_permission_prompt_raw.txt"
)


def _raw() -> str:
    return FIXTURE.read_bytes().decode("utf-8")


class _Emitter:
    def __init__(self) -> None:
        self.intents: list = []

    def emit_intent(self, component, props, terminal_id=None, session_name=None) -> None:
        self.intents.append((component, dict(props)))


def test_fixture_footer_is_split_by_a_cursor_move():
    """Guards the fixture itself: the bug needs the cursor move in the footer."""
    raw = _raw()
    assert "Tab\x1b[22Gto amend" in raw
    assert "Tab to amend" not in raw


def test_raw_tail_as_the_bridge_passes_it_is_permission_request():
    assert classify_reason("claude_code", _raw()[-1024:]) == "claude-code:permission_request"


def test_bridge_raises_a_permission_request_interrupt_from_the_raw_buffer():
    construct = AgentHandoffWithApproval(_Emitter())
    bridge = ApprovalBridge(
        construct,
        get_output_fn=lambda _tid: _raw(),
        get_provider_fn=lambda _tid: "claude_code",
        get_session_fn=lambda _tid: "s",
    )
    asyncio.run(bridge._on_waiting("t1"))
    (interrupt,) = construct.pending()
    assert interrupt.reason == "claude-code:permission_request"
    assert interrupt.options == ["approve", "deny"]


@pytest.mark.parametrize(
    "raw",
    [
        # The footer with every gap drawn as a cursor move.
        "\x1b[38;5;246mEsc\x1b[1Cto\x1b[1Ccancel\x1b[1C·\x1b[1CTab\x1b[22Gto\x1b[1Camend\x1b[39m",
        # A colour change around the separator is dropped, not matched on.
        "Esc to cancel \x1b[2m·\x1b[22m Tab\x1b[22Gto amend",
        # A real space next to a cursor move still reads as one gap.
        "Esc to cancel · Tab \x1b[1Cto amend",
        # Plain bytes, as in the 2.1.310 capture-pane fixtures.
        " Esc to cancel · Tab to amend",
    ],
)
def test_footer_with_cursor_gaps_or_spaces_is_permission_request(raw):
    assert classify_reason("claude_code", raw) == "claude-code:permission_request"


@pytest.mark.parametrize(
    "raw",
    [
        # The words without the "·" chrome separator stay unmatched, as before.
        "Press\x1b[1CEsc\x1b[1Cto\x1b[1Ccancel\x1b[1Cand\x1b[1CTab\x1b[1Cto\x1b[1Camend.",
        # A colour code between the words is removed without adding a gap.
        "Esc to cancel\x1b[2m·\x1b[22mTab to\x1b[1mamend",
        # Ordinary completed output from the same stream.
        "\x1b[38;5;231m⏺\x1b[39m\x1b[1CThe\x1b[5Gfile\x1b[10Gwas\x1b[14Gwritten.\r\n❯\x1b[1C",
    ],
)
def test_ordinary_output_is_not_a_permission_request(raw):
    assert classify_reason("claude_code", raw) == "claude-code:unknown_prompt"


def _raw_before_dialog() -> str:
    """The same recorded stream, cut at the escape that starts the dialog box.

    It holds the startup banner, the hook warning, the typed task and the
    tool-call row: ordinary raw output from the same session.
    """
    raw = _raw()
    return raw[: raw.rfind("\x1b", 0, raw.index("Create"))]


def test_recorded_raw_output_before_the_dialog_is_not_a_permission_request():
    before = _raw_before_dialog()
    assert "Esc to cancel" not in before and "Tab" not in before
    construct = AgentHandoffWithApproval(_Emitter())
    bridge = ApprovalBridge(
        construct,
        get_output_fn=lambda _tid: before,
        get_provider_fn=lambda _tid: "claude_code",
        get_session_fn=lambda _tid: "s",
    )
    asyncio.run(bridge._on_waiting("t1"))
    (interrupt,) = construct.pending()
    assert interrupt.reason == "claude-code:unknown_prompt"


def test_trust_prompt_with_cursor_gaps_is_still_trust_prompt():
    raw = "❯\x1b[1C1.\x1b[1CYes,\x1b[1CI\x1b[1Ctrust\x1b[1Cthis\x1b[1Cfolder\r\n Enter to confirm · Esc"
    assert classify_reason("claude_code", raw) == "claude-code:trust_prompt"
