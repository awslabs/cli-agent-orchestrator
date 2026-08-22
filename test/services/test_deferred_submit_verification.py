"""Tests for the deferred-init submit-verification guard.

The deferred-init delivery (send_input: paste -> fixed sleep -> Enter) can drop
the Enter (message left in the box) or the whole paste (TUI not input-ready).
Nothing blocks on completion in that path, so a dropped submit would leave the
worker idle forever. These cover the confirm + re-submit logic that closes it.
"""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.services import terminal_service as ts


class TestMessageVisibleInBox:
    def test_true_when_probe_present(self):
        with patch.object(ts, "get_output", return_value="❯ Analyze the logs now"):
            assert ts._message_visible_in_box("t1", "Analyze the logs") is True

    def test_false_when_absent(self):
        with patch.object(ts, "get_output", return_value="❯ (empty prompt)"):
            assert ts._message_visible_in_box("t1", "Analyze the logs") is False

    def test_false_when_message_too_short(self):
        # < 8 alnum chars → don't risk a blank submit; report not-shown.
        with patch.object(ts, "get_output", return_value="go go go") as mock_out:
            assert ts._message_visible_in_box("t1", "go") is False
            mock_out.assert_not_called()

    def test_false_when_output_fetch_raises(self):
        with patch.object(ts, "get_output", side_effect=Exception("boom")):
            assert ts._message_visible_in_box("t1", "Analyze the logs") is False

    def test_match_survives_wrapping_and_whitespace(self):
        # Rendered box wraps the text across lines / pads with spaces.
        with patch.object(ts, "get_output", return_value="❯ Analyze the\n  logs carefully"):
            assert ts._message_visible_in_box("t1", "Analyze the logs") is True


@pytest.mark.asyncio
class TestConfirmWorkerStartedOrResubmit:
    async def test_started_on_first_confirm_no_resubmit(self):
        with (
            patch.object(ts, "_wait_for_input_acceptance", return_value=True),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1", "Analyze the logs", None, "sup", None
            )
        assert ok is True
        key.assert_not_called()
        send.assert_not_called()

    async def test_enter_resubmit_when_message_in_box(self):
        # First confirm fails, box shows our text (Enter swallowed) → bare Enter,
        # second confirm succeeds.
        with (
            patch.object(ts, "_wait_for_input_acceptance", side_effect=[False, True]),
            patch.object(ts, "_message_visible_in_box", return_value=True),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1", "Analyze the logs", None, "sup", None
            )
        assert ok is True
        key.assert_called_once_with("t1", "Enter")
        send.assert_not_called()

    async def test_full_redeliver_when_box_empty(self):
        # First confirm fails, box empty (paste dropped) → re-deliver full msg.
        with (
            patch.object(ts, "_wait_for_input_acceptance", side_effect=[False, True]),
            patch.object(ts, "_message_visible_in_box", return_value=False),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1", "Analyze the logs", "reg", "sup", None
            )
        assert ok is True
        key.assert_not_called()
        send.assert_called_once()
        assert send.call_args.args[0] == "t1"
        assert send.call_args.args[1] == "Analyze the logs"

    async def test_returns_false_when_worker_never_starts(self):
        # Every confirm fails through all resubmit attempts.
        with (
            patch.object(ts, "_wait_for_input_acceptance", return_value=False),
            patch.object(ts, "_message_visible_in_box", return_value=True),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input"),
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1", "Analyze the logs", None, "sup", None
            )
        assert ok is False
        assert key.call_count == ts._DEFERRED_SUBMIT_MAX_RESUBMITS

    async def test_direct_probe_short_circuits_when_worker_started(self):
        # Shared acceptance wait reports the live rendered status as started.
        provider = MagicMock(supports_direct_status_probe=True)
        with (
            patch.object(ts, "_wait_for_input_acceptance", return_value=True) as wait,
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1",
                "Analyze the logs",
                None,
                "sup",
                None,
                provider=provider,
            )
        assert ok is True
        wait.assert_called_once_with("t1", provider, None, None)
        key.assert_not_called()
        send.assert_not_called()

    async def test_direct_probe_falls_through_when_worker_not_started(self):
        # A failed acceptance wait continues to the existing Enter retry.
        provider = MagicMock(supports_direct_status_probe=True)
        with (
            patch.object(ts, "_wait_for_input_acceptance", side_effect=[False, True]),
            patch.object(ts, "_message_visible_in_box", return_value=True),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1",
                "Analyze the logs",
                None,
                "sup",
                None,
                provider=provider,
            )
        assert ok is True
        key.assert_called_once()
        send.assert_not_called()

    async def test_direct_probe_skipped_when_provider_not_opted_in(self):
        # Unsupported providers still use the same cached acceptance wait.
        provider = MagicMock(supports_direct_status_probe=False)
        with (
            patch.object(ts, "_wait_for_input_acceptance", side_effect=[False, True]) as wait,
            patch.object(ts, "_message_visible_in_box", return_value=True),
            patch.object(ts, "send_special_key"),
            patch.object(ts, "send_input"),
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1",
                "Analyze the logs",
                None,
                "sup",
                None,
                provider=provider,
            )
        assert ok is True
        assert wait.call_count == 2

    async def test_provider_none_skips_direct_probe(self):
        # The existing None-provider path still works unchanged.
        with (
            patch.object(ts, "_wait_for_input_acceptance", side_effect=[False, True]) as wait,
            patch.object(ts, "_message_visible_in_box", return_value=True),
            patch.object(ts, "send_special_key"),
            patch.object(ts, "send_input"),
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1",
                "Analyze the logs",
                None,
                "sup",
                None,
                provider=None,
            )
        assert ok is True
        assert wait.call_count == 2


class TestWorkerIsStartedDirect:
    """Unit tests for the capture-pane direct status probe."""

    def test_returns_false_when_metadata_is_none(self):
        provider = MagicMock(
            supports_screen_detection=False,
            supports_direct_status_probe=True,
        )
        with patch.object(ts, "get_terminal_metadata", return_value=None):
            assert ts._worker_is_started_direct("t1", provider) is False

    def test_returns_false_when_session_key_missing(self):
        provider = MagicMock(
            supports_screen_detection=False,
            supports_direct_status_probe=True,
        )
        with patch.object(ts, "get_terminal_metadata", return_value={"tmux_window": "w1"}):
            assert ts._worker_is_started_direct("t1", provider) is False

    def test_returns_false_when_window_key_missing(self):
        provider = MagicMock(
            supports_screen_detection=False,
            supports_direct_status_probe=True,
        )
        with patch.object(ts, "get_terminal_metadata", return_value={"tmux_session": "s1"}):
            assert ts._worker_is_started_direct("t1", provider) is False

    def test_returns_false_when_get_history_raises(self):
        provider = MagicMock(
            supports_screen_detection=False,
            supports_direct_status_probe=True,
        )
        with (
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={
                    "tmux_session": "s1",
                    "tmux_window": "w1",
                },
            ),
            patch.object(ts, "get_backend") as mock_be,
        ):
            mock_be.return_value.get_history.side_effect = Exception("capture failed")
            assert ts._worker_is_started_direct("t1", provider) is False

    def test_returns_false_when_get_status_raises(self):
        provider = MagicMock(
            supports_screen_detection=False,
            supports_direct_status_probe=True,
        )
        provider.get_status.side_effect = Exception("parse failure")
        with (
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={
                    "tmux_session": "s1",
                    "tmux_window": "w1",
                },
            ),
            patch.object(ts, "get_backend") as mock_be,
        ):
            assert ts._worker_is_started_direct("t1", provider) is False

    def test_returns_true_when_status_is_processing(self):
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        provider = MagicMock(
            supports_screen_detection=False,
            supports_direct_status_probe=True,
        )
        provider.get_status.return_value = TerminalStatus.PROCESSING
        with (
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={
                    "tmux_session": "s1",
                    "tmux_window": "w1",
                },
            ),
            patch.object(ts, "get_backend") as mock_be,
        ):
            assert ts._worker_is_started_direct("t1", provider) is True

    def test_returns_false_when_status_is_idle(self):
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        provider = MagicMock(
            supports_screen_detection=False,
            supports_direct_status_probe=True,
        )
        provider.get_status.return_value = TerminalStatus.IDLE
        with (
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={
                    "tmux_session": "s1",
                    "tmux_window": "w1",
                },
            ),
            patch.object(ts, "get_backend") as mock_be,
        ):
            assert ts._worker_is_started_direct("t1", provider) is False

    def test_screen_detector_receives_rendered_lines(self):
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        provider = MagicMock(
            supports_screen_detection=True,
            supports_direct_status_probe=False,
        )
        provider.get_status_from_screen.return_value = TerminalStatus.COMPLETED
        with (
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={
                    "tmux_session": "s1",
                    "tmux_window": "w1",
                },
            ),
            patch.object(ts, "get_backend") as mock_be,
        ):
            mock_be.return_value.get_history.return_value = "response\n❯"
            assert ts._get_direct_rendered_status("t1", provider) == TerminalStatus.COMPLETED

        provider.get_status_from_screen.assert_called_once_with(["response", "❯"])


class TestWaitForInputAcceptance:
    def test_does_not_accept_same_rendered_completed_status_from_before_send(self):
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        provider = MagicMock()
        with (
            patch.object(ts.status_monitor, "get_status", return_value=TerminalStatus.IDLE),
            patch.object(
                ts,
                "_get_direct_rendered_status",
                return_value=TerminalStatus.COMPLETED,
            ),
            patch.object(ts.time, "monotonic", side_effect=[0.0, 0.0, 9.0]),
            patch.object(ts.time, "sleep"),
        ):
            accepted = ts._wait_for_input_acceptance(
                "t1",
                provider,
                initial_cached_status=TerminalStatus.IDLE,
                initial_rendered_status=TerminalStatus.COMPLETED,
            )

        assert accepted is False

    def test_does_not_accept_stale_cached_processing_status(self):
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        provider = MagicMock()
        with (
            patch.object(
                ts.status_monitor,
                "get_status",
                return_value=TerminalStatus.PROCESSING,
            ),
            patch.object(
                ts,
                "_get_direct_rendered_status",
                return_value=TerminalStatus.IDLE,
            ),
            patch.object(ts.time, "monotonic", side_effect=[0.0, 0.0, 9.0]),
            patch.object(ts.time, "sleep"),
        ):
            accepted = ts._wait_for_input_acceptance(
                "t1",
                provider,
                initial_cached_status=TerminalStatus.PROCESSING,
                initial_rendered_status=TerminalStatus.IDLE,
            )

        assert accepted is False
