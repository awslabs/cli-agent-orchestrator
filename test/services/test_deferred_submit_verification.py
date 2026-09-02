"""Tests for the deferred-init submit-verification guard.

The deferred-init delivery (send_input: paste -> fixed sleep -> Enter) can drop
the Enter (message left in the box) or the whole paste (TUI not input-ready).
Nothing blocks on completion in that path, so a dropped submit would leave the
worker idle forever. These cover the confirm + re-submit logic that closes it.
"""

from unittest.mock import AsyncMock, MagicMock, patch

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


class TestRedeliverDroppedMessageHelper:
    """The shared one-attempt helper: a caller without a provider instance
    (the synchronous step path, #562) gets it resolved from the registry,
    best-effort — a resolution failure means no probe, never a lost
    redelivery."""

    def test_resolves_provider_from_registry_for_direct_probe(self):
        # Provider without explicit pass + direct probe True → started, no send.
        provider = MagicMock(supports_direct_status_probe=True)
        with (
            patch.object(ts, "provider_manager") as mgr,
            patch.object(ts, "_worker_is_started_direct", return_value=True) as probe,
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            mgr.get_provider.return_value = provider
            started = ts.redeliver_dropped_message("t1", "Analyze the logs", 1)
        assert started is True
        mgr.get_provider.assert_called_once_with("t1")
        # The message rides along: the probe binds its verdict to it.
        probe.assert_called_once_with("t1", provider, "Analyze the logs")
        key.assert_not_called()
        send.assert_not_called()

    def test_provider_resolution_failure_falls_through_to_box_check(self):
        # Registry blowup must not lose the redelivery — box check still runs.
        with (
            patch.object(ts, "provider_manager") as mgr,
            patch.object(ts, "_message_visible_in_box", return_value=True) as box,
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            mgr.get_provider.side_effect = ValueError("Terminal t1 not found")
            started = ts.redeliver_dropped_message("t1", "Analyze the logs", 1)
        assert started is False
        box.assert_called_once_with("t1", "Analyze the logs")
        key.assert_called_once_with("t1", "Enter")
        send.assert_not_called()

    def test_gate_on_probe_capable_still_full_resends_when_box_empty(self):
        # Gated step path: probe ran and said not-started, text absent → the
        # probe ruled out a working worker, so the full re-send is safe.
        provider = MagicMock(supports_direct_status_probe=True)
        with (
            patch.object(ts, "_worker_is_started_direct", return_value=False),
            patch.object(ts, "_message_visible_in_box", return_value=False),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            started = ts.redeliver_dropped_message(
                "t1", "Analyze the logs", 1, provider, full_resend_requires_probe=True
            )
        assert started is False
        key.assert_not_called()
        send.assert_called_once()

    def test_gate_on_skips_full_resend_without_probe(self):
        # Gated step path + non-probe provider + text absent: cannot tell
        # "paste dropped" from "worker running, prompt scrolled off" — the
        # full re-send would risk a duplicate task, so nothing is sent.
        provider = MagicMock(supports_direct_status_probe=False)
        with (
            patch.object(ts, "_worker_is_started_direct") as probe,
            patch.object(ts, "_message_visible_in_box", return_value=False),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            started = ts.redeliver_dropped_message(
                "t1", "Analyze the logs", 1, provider, full_resend_requires_probe=True
            )
        assert started is False
        probe.assert_not_called()
        key.assert_not_called()
        send.assert_not_called()

    def test_gate_on_still_sends_bare_enter_without_probe(self):
        # Gated step path + non-probe provider + text VISIBLE: a bare Enter
        # cannot duplicate a task, so the Enter-swallowed recovery survives
        # the gate.
        provider = MagicMock(supports_direct_status_probe=False)
        with (
            patch.object(ts, "_message_visible_in_box", return_value=True),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            started = ts.redeliver_dropped_message(
                "t1", "Analyze the logs", 1, provider, full_resend_requires_probe=True
            )
        assert started is False
        key.assert_called_once_with("t1", "Enter")
        send.assert_not_called()

    def test_gate_off_default_keeps_deferred_init_behavior(self):
        # Deferred-init path (default): non-probe provider + text absent →
        # full re-send, exactly as before the helper was extracted.
        provider = MagicMock(supports_direct_status_probe=False)
        with (
            patch.object(ts, "_message_visible_in_box", return_value=False),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            started = ts.redeliver_dropped_message("t1", "Analyze the logs", 1, provider)
        assert started is False
        key.assert_not_called()
        send.assert_called_once()


@pytest.mark.asyncio
class TestConfirmWorkerStartedOrResubmit:
    async def test_started_on_first_confirm_no_resubmit(self):
        with (
            patch.object(ts, "wait_until_status", new=AsyncMock(return_value=True)),
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
            patch.object(ts, "wait_until_status", new=AsyncMock(side_effect=[False, True])),
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
            patch.object(ts, "wait_until_status", new=AsyncMock(side_effect=[False, True])),
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
            patch.object(ts, "wait_until_status", new=AsyncMock(return_value=False)),
            patch.object(ts, "_message_visible_in_box", return_value=True),
            patch.object(ts, "send_special_key"),
            patch.object(ts, "send_input"),
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1", "Analyze the logs", None, "sup", None
            )
        assert ok is False

    async def test_direct_probe_short_circuits_when_worker_started(self):
        # Provider with supports_direct_status_probe=True + direct probe True →
        # returns True without calling send_input or send_special_key.
        provider = MagicMock(supports_direct_status_probe=True)
        with (
            patch.object(ts, "wait_until_status", new=AsyncMock(return_value=False)),
            patch.object(ts, "_worker_is_started_direct", return_value=True),
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
        key.assert_not_called()
        send.assert_not_called()

    async def test_direct_probe_falls_through_when_worker_not_started(self):
        # Direct probe returns False → continues to existing resubmit logic.
        provider = MagicMock(supports_direct_status_probe=True)
        with (
            patch.object(ts, "wait_until_status", new=AsyncMock(side_effect=[False, True])),
            patch.object(ts, "_worker_is_started_direct", return_value=False),
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
        # Provider without supports_direct_status_probe → direct probe never
        # invoked; falls through to existing resubmit logic.
        provider = MagicMock(supports_direct_status_probe=False)
        with (
            patch.object(ts, "wait_until_status", new=AsyncMock(side_effect=[False, True])),
            patch.object(ts, "_worker_is_started_direct") as probe,
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
        probe.assert_not_called()

    async def test_provider_none_skips_direct_probe(self):
        # The existing None-provider path still works unchanged.
        with (
            patch.object(ts, "wait_until_status", new=AsyncMock(side_effect=[False, True])),
            patch.object(ts, "_worker_is_started_direct") as probe,
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
        probe.assert_not_called()


class TestWorkerIsStartedDirect:
    """Unit tests for the capture-pane direct status probe."""

    def test_returns_false_when_metadata_is_none(self):
        with patch.object(ts, "get_terminal_metadata", return_value=None):
            assert ts._worker_is_started_direct("t1", MagicMock()) is False

    def test_returns_false_when_session_key_missing(self):
        with patch.object(ts, "get_terminal_metadata", return_value={"tmux_window": "w1"}):
            assert ts._worker_is_started_direct("t1", MagicMock()) is False

    def test_returns_false_when_window_key_missing(self):
        with patch.object(ts, "get_terminal_metadata", return_value={"tmux_session": "s1"}):
            assert ts._worker_is_started_direct("t1", MagicMock()) is False

    def test_returns_false_when_get_history_raises(self):
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
            assert ts._worker_is_started_direct("t1", MagicMock()) is False

    def test_returns_false_when_get_status_raises(self):
        provider = MagicMock()
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

        provider = MagicMock()
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

        provider = MagicMock()
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


class TestCodexDirectProbeOptIn:
    """#659: Codex deferred assign — the cached status can sit IDLE for the whole
    confirm window while the real pane already shows the TUI Working spinner
    (detection fires only at rising-edge/quiescence, and a repainting spinner
    defers quiescence). Without the direct-probe opt-in the confirm loop
    re-delivers the task into the working pane up to three times and then tears
    the worker down. These pin the opt-in end to end with the REAL provider and
    a real rendered frame, so removing the flag (or breaking the detector on
    this shape) goes red here — not just in a unit assert on the attribute.
    """

    _MESSAGE = "[CAO Handoff] Supervisor terminal ID: sup-123. Do the task."

    # The shape from the issue report: handoff prompt in the transcript, live
    # Working spinner, TUI footer. Same frame family the codex provider unit
    # tests pin as PROCESSING.
    _WORKING_FRAME = (
        "› [CAO Handoff] Supervisor terminal ID: sup-123. Do the task.\n"
        "\n"
        "• Working (3s • esc to interrupt)\n"
        "\n"
        "› Use /skills to list available skills\n"
        "\n"
        "  ? for shortcuts                     100% context left\n"
    )

    # Startup chrome as Codex renders it before any task exists. The MCP
    # startup spinner IS the TUI progress pattern, and any startup bullet is
    # an assistant marker.
    _STARTUP_BANNER = (
        "╭──────────────────────────────────────────────╮\n"
        "│ >_ OpenAI Codex (v0.145.0)                   │\n"
        "│                                              │\n"
        "│ model:       gpt-5.6-sol medium              │\n"
        "│ directory:   ~/project                       │\n"
        "│ permissions: YOLO mode                       │\n"
        "╰──────────────────────────────────────────────╯\n"
        "\n"
        "• Starting MCP servers (4s • esc to interrupt)\n"
    )
    _IDLE_COMPOSER = (
        "\n" "› Write tests for @filename\n" "\n" "  gpt-5.6-sol medium · Context 100% left\n"
    )

    @staticmethod
    def _residue_frame(gap: int, composer: str) -> str:
        """Startup spinner ``gap`` blank lines above the composer: outside the
        bottom-15 window initialize() vetoes activity in, so the provider
        reports ready, yet inside (or above) the wider tail get_status scans."""
        return TestCodexDirectProbeOptIn._STARTUP_BANNER + "\n" * gap + composer

    @staticmethod
    async def _run_confirm(frame: str, message: str):
        from cli_agent_orchestrator.providers.codex import CodexProvider

        provider = CodexProvider("t1", "s1", "w0")
        backend = MagicMock()
        backend.get_history.return_value = frame
        with (
            # Cached status stays IDLE past every poll — the #496-class lag.
            patch.object(ts, "wait_until_status", new=AsyncMock(return_value=False)),
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={"tmux_session": "s1", "tmux_window": "w0"},
            ),
            patch.object(ts, "get_backend", return_value=backend),
            patch.object(ts, "get_output", return_value=frame),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1", message, None, "sup", None, provider=provider
            )
        return ok, key, send

    def test_codex_opts_into_direct_status_probe(self):
        from cli_agent_orchestrator.providers.codex import CodexProvider

        assert CodexProvider.supports_direct_status_probe is True

    @pytest.mark.asyncio
    async def test_codex_confirm_succeeds_from_live_frame_without_redelivery(self):
        from cli_agent_orchestrator.providers.codex import CodexProvider

        provider = CodexProvider("t1", "s1", "w0")
        backend = MagicMock()
        backend.get_history.return_value = self._WORKING_FRAME
        with (
            # Cached status stays IDLE past every poll — the #496-class lag.
            patch.object(ts, "wait_until_status", new=AsyncMock(return_value=False)),
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={"tmux_session": "s1", "tmux_window": "w0"},
            ),
            patch.object(ts, "get_backend", return_value=backend),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1",
                self._MESSAGE,
                None,
                "sup",
                None,
                provider=provider,
            )

        # Started: the caller must not classify this as a dropped submit, so the
        # delete_worker teardown arm never fires...
        assert ok is True
        # ...and nothing was typed into the already-working pane: no full
        # re-delivery (which would run the task twice) and no blind Enter.
        send.assert_not_called()
        key.assert_not_called()

    # --- the verdict is bound to the submission, not to the pane's status -----
    # get_status classifies the frame as a whole, so startup residue reads as
    # started for a pane whose task paste was dropped. The probe must not take
    # that as acceptance: it would skip the redelivery this path exists for and
    # the task would be silently lost with the supervisor waiting forever.

    @pytest.mark.parametrize(
        "gap, expected_status",
        [
            (12, "processing"),  # spinner inside get_status's 25-line spinner tail
            (20, "processing"),  # ...at its far edge
            (28, "completed"),  # spinner out of the tail: the bullet is an assistant marker
        ],
    )
    @pytest.mark.asyncio
    async def test_startup_residue_on_a_dropped_task_still_redelivers(self, gap, expected_status):
        from cli_agent_orchestrator.providers.codex import CodexProvider, _has_startup_idle_composer

        frame = self._residue_frame(gap, self._IDLE_COMPOSER)
        # Precondition — this is exactly the frame the finding describes: the
        # provider reports ready (initialize() would have returned) while the
        # whole-frame status says started, and the message is nowhere.
        assert _has_startup_idle_composer(frame) is True
        assert CodexProvider("t1", "s1", "w0").get_status(frame).value == expected_status
        assert self._MESSAGE[:12] not in frame

        ok, key, send = await self._run_confirm(frame, self._MESSAGE)

        # Not started: the paste was dropped, so every attempt re-delivers the
        # full message and the caller gets to classify the outcome.
        assert ok is False
        assert send.call_count == ts._DEFERRED_SUBMIT_MAX_RESUBMITS
        key.assert_not_called()

    @pytest.mark.asyncio
    async def test_startup_residue_with_unsubmitted_text_sends_enter(self):
        # Paste landed, Enter was swallowed: the message sits in the composer
        # under the residue. The bare-Enter recovery must still fire.
        composer = "\n› " + self._MESSAGE + "\n\n  gpt-5.6-sol medium · Context 100% left\n"
        frame = self._residue_frame(18, composer)

        ok, key, send = await self._run_confirm(frame, self._MESSAGE)

        assert ok is False
        assert key.call_count == ts._DEFERRED_SUBMIT_MAX_RESUBMITS
        send.assert_not_called()

    @pytest.mark.asyncio
    async def test_startup_residue_above_an_accepted_task_is_started(self):
        # Residue AND a real accepted turn: the echo of our message with the
        # turn's activity below it is the causal evidence; residue above it
        # neither adds nor subtracts.
        frame = self._residue_frame(18, self._WORKING_FRAME)

        ok, key, send = await self._run_confirm(frame, self._MESSAGE)

        assert ok is True
        send.assert_not_called()
        key.assert_not_called()

    @pytest.mark.asyncio
    async def test_accepted_turn_on_an_approval_prompt_is_started(self):
        # WAITING_USER_ANSWER after our turn (codex 0.147 approval menu, as in
        # test/providers/fixtures/codex_approval_modal_raw.txt): the activity
        # bullet below the echo binds it; the probe must not blind-Enter into
        # the menu (that would select "Yes, proceed").
        frame = (
            "› " + self._MESSAGE + "\n"
            "• Running mkdir -p /tmp/work/subdir\n"
            "  Would you like to run the following command?\n"
            "  $ mkdir -p /tmp/work/subdir\n"
            "› 1. Yes, proceed (y)\n"
            "  2. Yes, and don't ask again for commands that start with `mkdir` (p)\n"
            "  3. No, and tell Codex what to do differently (esc)\n"
            "  Press enter to confirm or esc to cancel\n"
        )
        ok, key, send = await self._run_confirm(frame, self._MESSAGE)

        assert ok is True
        send.assert_not_called()
        key.assert_not_called()

    def test_bullets_inside_the_pasted_message_do_not_self_attribute(self):
        from cli_agent_orchestrator.providers.codex import CodexProvider

        # An unsubmitted multi-line paste whose own lines are bullets: the
        # bullets below the echo line belong to the message, not to a reply.
        message = "Review these findings for me:\n• first finding\n• second finding"
        frame = self._residue_frame(
            18,
            "\n› Review these findings for me:\n  • first finding\n  • second finding\n"
            "\n  gpt-5.6-sol medium · Context 100% left\n",
        )
        provider = CodexProvider("t1", "s1", "w0")
        assert provider.direct_probe_confirms_submission(frame, message) is False
        # ...while a reply bullet under the same paste does bind it.
        assert (
            provider.direct_probe_confirms_submission(
                frame.replace(
                    "  • second finding\n", "  • second finding\n• Reviewing the findings\n"
                ),
                message,
            )
            is True
        )

    def test_short_message_cannot_bind(self):
        from cli_agent_orchestrator.providers.codex import CodexProvider

        # Below the 8-character floor the collapse cannot match reliably; the
        # hook must refuse rather than guess (same floor as the box check).
        assert (
            CodexProvider("t1", "s1", "w0").direct_probe_confirms_submission(
                "› hi\n• Working (3s • esc to interrupt)\n", "hi"
            )
            is False
        )
