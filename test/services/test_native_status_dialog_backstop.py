"""#896 case 2: a dialog the backend's native status did not report.

On the herdr backends CAO does not read the screen to decide status: the
provider returns herdr's own agent state. herdr has reported ``idle`` and
``done`` with a Claude Code dialog open (herdrdev/herdr#4573, #5741), so the
#973 guard, which tests the status, saw a ready terminal and the inbox message
was typed into the dialog: the paste was swallowed and the trailing Enter
selected the highlighted option.

The backstop under test reads the pane once before typing orchestrated input
and asks the provider whether a user-answer dialog is on screen
(``BaseProvider.shows_user_answer_dialog``).

Frames. The permission prompts, idle panes and finished turn are the real
``tmux capture-pane`` fixtures in test/providers/fixtures. There is no captured
AskUserQuestion or plan-approval frame in the tree, so those two are the frames
the #405 tests in test/providers/test_claude_code_unit.py already use, quoted
unchanged. None of them is a ``herdr pane read`` capture; whether herdr's text
matches is a live-validation item, not something these tests can show.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator, List, Optional
from unittest.mock import MagicMock, call, patch

import pytest

from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend
from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.terminal import (
    TerminalInputBlockedError,
    TerminalInputBlockedReason,
    TerminalStatus,
)
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.services import terminal_service as ts
from cli_agent_orchestrator.services.inbox_service import InboxService

FIXTURES = Path(__file__).parent.parent / "providers" / "fixtures"
MESSAGE = "MSG1: refactor module A so the retry policy lives in one place"


def _fixture(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text(encoding="utf-8")


# test_claude_code_unit.py::TestClaudeCodeDialogDetection::
# test_ask_user_question_with_notes_hint_and_error_banner
ASK_USER_QUESTION = (
    "❯ 1. Option one\n"
    "  2. Option two\n"
    "  3. Option three\n"
    "n to add notes\n"
    "⚠ Please select a valid option\n"
    "Enter to select · ↑/↓ to navigate · Esc to cancel"
)
# ...::test_plan_approval_active_with_option_markers_is_waiting. Nothing in
# WAITING_USER_ANSWER_PATTERN matches this frame, so only the plan-approval
# branch can catch it; get_status_from_screen has no such branch (#405).
PLAN_APPROVAL = (
    "Claude has a plan. Would you like to proceed?\n"
    "❯ 1. Yes, and bypass permissions\n"
    "  2. Yes, manually approve edits\n"
    "  3. No, refine\n"
    "  4. Tell Claude what to change\n"
    "     shift+tab to approve with feedback"
)
# ...::test_agent_prose_with_nav_text_in_footer_false_waiting (strict xfail there).
PROSE_IN_FOOTER_WINDOW = (
    "⏺ The dialog shows:\n"
    "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
    "────────────────────────\n"
    "❯ "
)


def _on_a_real_screen(dialog: str) -> str:
    """``dialog`` as a pane read would hold it: transcript above, blank rows below.

    The transcript is the top of a real finished-turn capture. The blank rows
    are what a rendered pane has under an inline TUI that has not reached the
    bottom of the window; the buffer path the detector was written for never
    sees them, and they are what would push a footer out of its 6-line window.
    """
    transcript = "\n".join(_fixture("claude_code_865_turn_ended").split("\n")[:30])
    return transcript + "\n\n" + dialog + "\n" + "\n" * 12


DIALOG_PANES = {
    "bash_permission_prompt": _fixture("claude_code_310_bash_permission_prompt"),
    "write_permission_prompt": _fixture("claude_code_310_write_permission_prompt"),
    "ask_user_question": _on_a_real_screen(ASK_USER_QUESTION),
    "plan_approval": _on_a_real_screen(PLAN_APPROVAL),
}

# The dialog wording sits in the transcript, well above the bottom region of a
# real idle pane, the way an agent that had just described a dialog would leave it.
_IDLE_ROWS = _fixture("claude_code_310_permission_idle_after").split("\n")
PROSE_ABOVE_IDLE_PROMPT = "\n".join(
    _IDLE_ROWS[:20]
    + [
        "⏺ The dialog shows:",
        "  Enter to select · ↑/↓ to navigate · Esc to cancel",
        "  Would you like to proceed?",
        "  Esc to cancel · Tab to amend",
    ]
    + _IDLE_ROWS[20:]
)

READY_PANES = {
    "startup_idle": _fixture("claude_code_865_startup_idle"),
    "idle_after_permission": _fixture("claude_code_310_permission_idle_after"),
    "turn_ended": _fixture("claude_code_865_turn_ended"),
    "dialog_wording_in_transcript": PROSE_ABOVE_IDLE_PROMPT,
}


def _claude() -> ClaudeCodeProvider:
    return ClaudeCodeProvider("term-1", "cao-session", "developer-abcd")


def _backend(pane: Any, *, native: Optional[TerminalStatus], event_inbox: bool = True) -> MagicMock:
    """A backend double. ``event_inbox=True`` with a native status is herdr's shape."""
    backend = MagicMock()
    backend.supports_event_inbox.return_value = event_inbox
    backend.event_inbox_confirms_submit.return_value = False
    backend.get_native_status.return_value = native
    if isinstance(pane, BaseException):
        backend.get_history.side_effect = pane
    else:
        backend.get_history.return_value = pane
    return backend


def _screen_reads(backend: MagicMock) -> List[Any]:
    """The backstop's reads: the only caller here that asks for the visible screen."""
    return [c for c in backend.get_history.call_args_list if c.kwargs.get("visible_only")]


@contextmanager
def _terminal(
    provider: Any, backend: MagicMock, status: Optional[TerminalStatus] = None
) -> Iterator[MagicMock]:
    """Patch ``dispatch_input``'s collaborators around one provider and one backend.

    With ``status`` None the monitor answers the way StatusMonitor.get_status
    does on an event-inbox backend: by asking the provider, which returns the
    backend's native state without looking at the pane. That is the path the bug
    lives on, so the tests go through it and not through a canned status.
    """
    monitor = MagicMock()
    monitor.output_generation.return_value = 7
    if status is None:
        monitor.get_status.side_effect = lambda _tid: provider.get_status("")
    else:
        monitor.get_status.return_value = status
    with ExitStack() as stack:
        stack.enter_context(patch("cli_agent_orchestrator.backends.registry._backend", backend))
        stack.enter_context(patch.object(ts, "status_monitor", monitor))
        stack.enter_context(
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={
                    "tmux_session": "cao-session",
                    "tmux_window": "developer-abcd",
                    "provider": "claude_code",
                },
            )
        )
        stack.enter_context(
            patch.object(ts.provider_manager, "get_provider", return_value=provider)
        )
        stack.enter_context(patch.object(ts, "inject_memory_context", side_effect=lambda m, *a: m))
        stack.enter_context(patch.object(ts, "update_last_active"))
        yield monitor


def _assert_refused(backend: MagicMock, **dispatch: Any) -> None:
    with pytest.raises(TerminalInputBlockedError, match="waiting for a user answer") as exc:
        ts.dispatch_input("term-1", MESSAGE, **dispatch)
    assert exc.value.reason is TerminalInputBlockedReason.WAITING_USER_ANSWER
    backend.send_keys.assert_not_called()
    backend.send_special_key.assert_not_called()


class TestProviderDialogCheck:
    """``shows_user_answer_dialog``: the provider's own detection, on a pane read."""

    def test_default_is_cannot_tell(self):
        assert BaseProvider.shows_user_answer_dialog(
            MagicMock(), DIALOG_PANES["plan_approval"]
        ) is (False)

    @pytest.mark.parametrize("name", sorted(DIALOG_PANES))
    def test_claude_code_sees_each_dialog(self, name):
        assert _claude().shows_user_answer_dialog(DIALOG_PANES[name]) is True

    @pytest.mark.parametrize("name", sorted(READY_PANES))
    def test_claude_code_does_not_see_a_dialog_on_a_ready_pane(self, name):
        assert _claude().shows_user_answer_dialog(READY_PANES[name]) is False

    @pytest.mark.parametrize("pane", ["", "\n\n   \n"])
    def test_blank_pane_is_not_a_dialog(self, pane):
        assert _claude().shows_user_answer_dialog(pane) is False

    def test_plan_approval_is_seen_only_by_the_buffer_path_detection(self):
        """Why the method reuses get_status's detection, not get_status_from_screen (#405)."""
        provider = _claude()
        rows = DIALOG_PANES["plan_approval"].split("\n")
        assert provider.get_status_from_screen(rows) != TerminalStatus.WAITING_USER_ANSWER
        assert provider.shows_user_answer_dialog(DIALOG_PANES["plan_approval"]) is True

    @pytest.mark.parametrize("name", ["bash_permission_prompt", "write_permission_prompt"])
    def test_agrees_with_get_status_on_the_real_dialog_captures(self, name):
        """One definition: the method and the tmux buffer path cannot disagree."""
        backend = _backend("", native=None, event_inbox=False)
        with patch("cli_agent_orchestrator.backends.registry._backend", backend):
            assert _claude().get_status(DIALOG_PANES[name]) == TerminalStatus.WAITING_USER_ANSWER

    def test_startup_trust_dialog_is_excluded_as_in_get_status(self):
        """Parity with the status guard: ``initialize`` answers this one itself."""
        pane = _fixture("claude_code_1277_resume_trust_dialog")
        assert _claude().shows_user_answer_dialog(pane) is False

    @pytest.mark.xfail(
        reason="Inherited from get_status (#405 residual, strict xfail in "
        "test_claude_code_unit.py): prose quoting the footer inside the last 6 "
        "rows reads as a dialog. Here that holds a message; it does not lose one.",
        strict=True,
    )
    def test_prose_quoting_the_footer_in_the_last_six_rows(self):
        assert _claude().shows_user_answer_dialog(PROSE_IN_FOOTER_WINDOW) is False


class TestDispatchRefusesIntoUnreportedDialog:
    """``dispatch_input`` on a native-status backend that reports a ready pane."""

    @pytest.mark.parametrize("name", sorted(DIALOG_PANES))
    def test_inbox_delivery_is_refused_when_herdr_reports_idle(self, name):
        """THE REGRESSION (#896 case 2). Fails on main: the message is typed."""
        provider = _claude()
        backend = _backend(DIALOG_PANES[name], native=TerminalStatus.IDLE)
        with _terminal(provider, backend) as monitor:
            assert monitor.get_status("term-1") == TerminalStatus.IDLE
            _assert_refused(backend, orchestration_type=OrchestrationType.SEND_MESSAGE)
        assert len(_screen_reads(backend)) == 1

    def test_inbox_delivery_is_refused_when_herdr_reports_done(self):
        """The issue's log: ``done`` is PROCESSING for the flush wait, and still delivered."""
        provider = _claude()
        backend = _backend(DIALOG_PANES["ask_user_question"], native=TerminalStatus.COMPLETED)
        with _terminal(provider, backend) as monitor:
            provider.mark_input_received()  # an earlier turn: "done" is now post-dispatch
            assert monitor.get_status("term-1") == TerminalStatus.PROCESSING
            _assert_refused(backend, orchestration_type=OrchestrationType.SEND_MESSAGE)

    @pytest.mark.parametrize("orchestration_type", list(OrchestrationType), ids=lambda t: t.value)
    def test_every_orchestration_type_is_refused(self, orchestration_type):
        backend = _backend(DIALOG_PANES["bash_permission_prompt"], native=TerminalStatus.IDLE)
        with _terminal(_claude(), backend):
            _assert_refused(backend, orchestration_type=orchestration_type)

    def test_refusal_does_not_spend_the_memory_block(self):
        backend = _backend(DIALOG_PANES["ask_user_question"], native=TerminalStatus.IDLE)
        with _terminal(_claude(), backend):
            _assert_refused(backend, orchestration_type=OrchestrationType.SEND_MESSAGE)
            ts.inject_memory_context.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.parametrize("name", sorted(READY_PANES))
    def test_ready_pane_is_delivered_after_exactly_one_screen_read(self, name):
        backend = _backend(READY_PANES[name], native=TerminalStatus.IDLE)
        with _terminal(_claude(), backend):
            ts.dispatch_input("term-1", MESSAGE, orchestration_type=OrchestrationType.SEND_MESSAGE)
        backend.send_keys.assert_called_once()
        assert _screen_reads(backend) == [
            call("cao-session", "developer-abcd", strip_escapes=True, visible_only=True)
        ]

    def test_operator_input_reaches_the_dialog(self):
        """No orchestration type: a human, or answer_user_prompt. Must not be read or held."""
        backend = _backend(DIALOG_PANES["ask_user_question"], native=TerminalStatus.IDLE)
        with _terminal(_claude(), backend):
            ts.dispatch_input("term-1", "1")
        backend.send_keys.assert_called_once()
        assert _screen_reads(backend) == []

    def test_tmux_backend_is_never_read(self):
        """Not a native-status backend: no capture at all, and nothing changes."""
        backend = _backend(DIALOG_PANES["ask_user_question"], native=None, event_inbox=False)
        with _terminal(_claude(), backend, status=TerminalStatus.IDLE):
            ts.dispatch_input("term-1", MESSAGE, orchestration_type=OrchestrationType.SEND_MESSAGE)
        backend.send_keys.assert_called_once()
        assert _screen_reads(backend) == []

    def test_provider_that_does_not_block_at_dialogs_is_never_read(self):
        provider = MagicMock()
        provider.blocks_orchestrated_input_while_waiting_user_answer = False
        provider.shows_user_answer_dialog.return_value = True
        provider.paste_enter_count = 1
        backend = _backend(DIALOG_PANES["ask_user_question"], native=TerminalStatus.IDLE)
        with _terminal(provider, backend, status=TerminalStatus.IDLE):
            ts.dispatch_input("term-1", MESSAGE, orchestration_type=OrchestrationType.SEND_MESSAGE)
        backend.send_keys.assert_called_once()
        backend.get_history.assert_not_called()

    def test_blocking_provider_without_the_method_is_unaffected(self):
        """codex blocks at dialogs but inherits the default: cannot tell, so send."""
        assert CodexProvider.shows_user_answer_dialog is BaseProvider.shows_user_answer_dialog
        provider = MagicMock()
        provider.blocks_orchestrated_input_while_waiting_user_answer = True
        provider.shows_user_answer_dialog.side_effect = lambda text: (
            BaseProvider.shows_user_answer_dialog(provider, text)
        )
        provider.paste_enter_count = 1
        backend = _backend(DIALOG_PANES["ask_user_question"], native=TerminalStatus.IDLE)
        with _terminal(provider, backend, status=TerminalStatus.IDLE):
            ts.dispatch_input("term-1", MESSAGE, orchestration_type=OrchestrationType.SEND_MESSAGE)
        backend.send_keys.assert_called_once()

    @pytest.mark.parametrize(
        "pane",
        [RuntimeError("herdr pane read: socket closed"), "", "   \n\n", None],
        ids=["raises", "empty", "blank", "not-text"],
    )
    def test_failed_pane_read_fails_open_and_warns(self, pane, caplog):
        """A backstop must not turn a herdr CLI hiccup into an inbox that never drains."""
        backend = _backend(pane, native=TerminalStatus.IDLE)
        provider = MagicMock()
        provider.blocks_orchestrated_input_while_waiting_user_answer = True
        provider.paste_enter_count = 1
        with _terminal(provider, backend, status=TerminalStatus.IDLE), caplog.at_level("WARNING"):
            ts.dispatch_input("term-1", MESSAGE, orchestration_type=OrchestrationType.SEND_MESSAGE)
        backend.send_keys.assert_called_once()
        provider.shows_user_answer_dialog.assert_not_called()
        assert "Could not read the pane of term-1" in caplog.text

    def test_provider_check_raising_fails_open(self, caplog):
        backend = _backend(DIALOG_PANES["ask_user_question"], native=TerminalStatus.IDLE)
        provider = MagicMock()
        provider.blocks_orchestrated_input_while_waiting_user_answer = True
        provider.shows_user_answer_dialog.side_effect = ValueError("bad frame")
        provider.paste_enter_count = 1
        with _terminal(provider, backend, status=TerminalStatus.IDLE), caplog.at_level("WARNING"):
            ts.dispatch_input("term-1", MESSAGE, orchestration_type=OrchestrationType.SEND_MESSAGE)
        backend.send_keys.assert_called_once()
        assert "Could not read the pane of term-1" in caplog.text

    def test_status_guard_refuses_without_a_pane_read(self):
        """Case 1 is unchanged: a status that already says so costs no read."""
        backend = _backend(DIALOG_PANES["ask_user_question"], native=TerminalStatus.IDLE)
        with _terminal(_claude(), backend, status=TerminalStatus.WAITING_USER_ANSWER):
            _assert_refused(backend, orchestration_type=OrchestrationType.SEND_MESSAGE)
        backend.get_history.assert_not_called()


class TestReadOnRealHerdrBackend:
    """What the backstop asks herdr for, through the real backend's ``get_history``."""

    def test_one_bounded_plain_text_pane_read(self):
        # As test/backends/test_herdr_backend.py builds it: the socket "exists".
        with patch(
            "cli_agent_orchestrator.backends.herdr_backend.os.path.exists", return_value=True
        ):
            backend = HerdrBackend(send_delay_ms=0)
        result = MagicMock(returncode=0, stdout=DIALOG_PANES["bash_permission_prompt"], stderr="")
        with (
            patch.object(backend, "_resolve_pane_id_from_window", return_value="p_1"),
            patch.object(backend, "_run_herdr", return_value=result) as run,
            patch.object(ts, "get_backend", return_value=backend),
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={"tmux_session": "cao-session", "tmux_window": "developer-abcd"},
            ),
        ):
            assert backend.supports_event_inbox() is True
            assert (
                ts._pane_shows_dialog_native_status_missed(
                    "term-1", _claude(), OrchestrationType.SEND_MESSAGE
                )
                is True
            )
        assert run.call_args_list == [
            call(
                ["pane", "read", "p_1", "--source", "recent", "--lines", "50", "--format", "text"],
                check=False,
            )
        ]


def _inbox_message() -> InboxMessage:
    from datetime import datetime

    return InboxMessage(
        id=1,
        sender_id="sender-1",
        receiver_id="term-1",
        message=MESSAGE,
        status=MessageStatus.PENDING,
        created_at=datetime.now(),
    )


class TestInboxHoldsThenDelivers:
    """Through InboxService: the refused message stays PENDING and is delivered later."""

    def test_message_is_held_while_the_dialog_is_open_and_delivered_once_it_is_gone(self):
        provider = _claude()
        backend = _backend(DIALOG_PANES["ask_user_question"], native=TerminalStatus.IDLE)
        with (
            _terminal(provider, backend) as monitor,
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_pending_messages",
                return_value=[_inbox_message()],
            ),
            patch("cli_agent_orchestrator.services.inbox_service.update_message_status") as update,
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor", monitor),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_backend", return_value=backend
            ),
        ):
            service = InboxService()

            service.deliver_pending("term-1")

            backend.send_keys.assert_not_called()
            assert update.call_args_list == [
                call(1, MessageStatus.DELIVERED),
                call(1, MessageStatus.PENDING),
            ]

            # The human answers; the pane is an ordinary idle prompt again.
            update.reset_mock()
            backend.get_history.return_value = READY_PANES["idle_after_permission"]

            service.deliver_pending("term-1")

            backend.send_keys.assert_called_once()
            assert MESSAGE in backend.send_keys.call_args.args[2]
            assert update.call_args_list == [call(1, MessageStatus.DELIVERED)]


class TestBareEnterRecovery:
    """The one submit that never reaches ``dispatch_input`` gets the same pane check.

    It sends an Enter and nothing else, which is exactly what answers a dialog.
    """

    def _recover(self, pane: Any, *, event_inbox: bool, orchestration_type: Any) -> List[str]:
        provider = _claude()
        backend = _backend(
            pane, native=TerminalStatus.IDLE if event_inbox else None, event_inbox=event_inbox
        )
        enters: List[str] = []
        with (
            _terminal(provider, backend, status=TerminalStatus.IDLE),
            patch.object(ts, "_capture_current_composer_region", return_value=MESSAGE),
            patch.object(ts, "send_special_key", side_effect=lambda _tid, key: enters.append(key)),
        ):
            ts.redeliver_dropped_message_with_boundary(
                "term-1",
                MESSAGE,
                1,
                provider,
                full_resend_requires_composer_evidence=True,
                orchestration_type=orchestration_type,
            )
        return enters

    def test_no_enter_into_a_dialog_herdr_did_not_report(self):
        """Fails on main: the Enter is sent."""
        assert (
            self._recover(
                DIALOG_PANES["bash_permission_prompt"],
                event_inbox=True,
                orchestration_type=OrchestrationType.SEND_MESSAGE,
            )
            == []
        )

    def test_enter_is_still_resent_on_a_ready_pane(self):
        assert self._recover(
            READY_PANES["idle_after_permission"],
            event_inbox=True,
            orchestration_type=OrchestrationType.SEND_MESSAGE,
        ) == ["Enter"]

    def test_enter_is_still_resent_on_tmux(self):
        assert self._recover(
            DIALOG_PANES["bash_permission_prompt"],
            event_inbox=False,
            orchestration_type=OrchestrationType.SEND_MESSAGE,
        ) == ["Enter"]

    def test_enter_is_still_resent_for_untyped_input(self):
        assert self._recover(
            DIALOG_PANES["bash_permission_prompt"], event_inbox=True, orchestration_type=None
        ) == ["Enter"]
