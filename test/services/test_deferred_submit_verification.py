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
            patch.object(ts.status_monitor, "get_buffer", return_value="• Working (1s)"),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            mgr.get_provider.return_value = provider
            started = ts.redeliver_dropped_message("t1", "Analyze the logs", 1)
        assert started is True
        mgr.get_provider.assert_called_once_with("t1")
        # The post-dispatch bytes ride along: they are what binds the verdict
        # to THIS submission, so the probe must receive them, not the message.
        probe.assert_called_once_with("t1", provider, "• Working (1s)")
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
            assert ts._worker_is_started_direct("t1", MagicMock(), "evidence") is False

    def test_returns_false_when_session_key_missing(self):
        with patch.object(ts, "get_terminal_metadata", return_value={"tmux_window": "w1"}):
            assert ts._worker_is_started_direct("t1", MagicMock(), "evidence") is False

    def test_returns_false_when_window_key_missing(self):
        with patch.object(ts, "get_terminal_metadata", return_value={"tmux_session": "s1"}):
            assert ts._worker_is_started_direct("t1", MagicMock(), "evidence") is False

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
            assert ts._worker_is_started_direct("t1", MagicMock(), "evidence") is False

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
            assert ts._worker_is_started_direct("t1", provider, "evidence") is False

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
            assert ts._worker_is_started_direct("t1", provider, "evidence") is True

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
            assert ts._worker_is_started_direct("t1", provider, "evidence") is False


class TestCodexDirectProbeOptIn:
    """#659: Codex deferred assign — the cached status can sit IDLE for the whole
    confirm window while the real pane already shows the TUI Working spinner
    (detection fires only at rising-edge/quiescence, and a repainting spinner
    defers quiescence). Without the direct-probe opt-in the confirm loop
    re-delivers the task into the working pane up to three times and then tears
    the worker down.

    The opt-in alone is not safe, though: ``get_status`` classifies a rendered
    frame as a whole, and Codex's startup chrome renders like its task activity
    (the ``Starting MCP servers`` spinner IS ``TUI_PROGRESS_PATTERN``; any
    startup bullet is an assistant marker). So the verdict is bound to
    POST-DISPATCH BYTES: ``send_input`` empties the StatusMonitor rolling buffer
    immediately before sending keystrokes, so a spinner that stopped before the
    dispatch contributes nothing there no matter where it still sits on screen,
    while a live one repaints its counter and necessarily does.

    These drive the REAL provider through the real redelivery decision, so a
    regression in either half goes red here — not just in a unit assert.
    """

    _MESSAGE = "[CAO Handoff] Supervisor terminal ID: sup-123. Do the task."
    _FOOTER = "  gpt-5.6-sol medium · Context 100% left\n"
    _SPINNER = "• Working (3s • esc to interrupt)\n"

    # The shape from the issue report: handoff prompt in the transcript, live
    # Working spinner, TUI footer.
    _WORKING_FRAME = (
        "› [CAO Handoff] Supervisor terminal ID: sup-123. Do the task.\n"
        "\n"
        "• Working (3s • esc to interrupt)\n"
        "\n"
        "› Use /skills to list available skills\n"
        "\n"
        "  ? for shortcuts                     100% context left\n"
    )

    # Startup chrome as Codex renders it before any task exists.
    _STARTUP_BANNER = (
        "╭──────────────────────────────────────────────╮\n"
        "│ >_ OpenAI Codex (v0.145.0)                   │\n"
        "│ permissions: YOLO mode                       │\n"
        "╰──────────────────────────────────────────────╯\n"
        "\n"
        "• Starting MCP servers (4s • esc to interrupt)\n"
    )
    _IDLE_COMPOSER = "\n› Write tests for @filename\n\n" + _FOOTER

    @classmethod
    def _residue_frame(cls, gap: int, composer: str) -> str:
        """Startup spinner ``gap`` blank lines above the composer: outside the
        bottom-15 window initialize() vetoes activity in, so the provider
        reports ready, yet inside (or above) the wider tail get_status scans."""
        return cls._STARTUP_BANNER + "\n" * gap + composer

    @staticmethod
    def _provider():
        from cli_agent_orchestrator.providers.codex import CodexProvider

        return CodexProvider("t1", "s1", "w0")

    @classmethod
    def _redeliver(cls, frame: str, message: str, post_dispatch: str):
        """One redelivery decision against a rendered ``frame`` and the bytes
        that arrived since the dispatch. Returns (started, enter, full_resend)."""
        provider = cls._provider()
        backend = MagicMock()
        backend.get_history.return_value = frame
        with (
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={"tmux_session": "s1", "tmux_window": "w0"},
            ),
            patch.object(ts, "get_backend", return_value=backend),
            patch.object(ts, "get_output", return_value=frame),
            patch.object(ts.status_monitor, "get_buffer", return_value=post_dispatch),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            started = ts.redeliver_dropped_message("t1", message, 1, provider)
        return started, key.called, send.called

    def test_codex_opts_into_direct_status_probe(self):
        from cli_agent_orchestrator.providers.codex import CodexProvider

        assert CodexProvider.supports_direct_status_probe is True

    @pytest.mark.asyncio
    async def test_codex_confirm_succeeds_from_live_frame_without_redelivery(self):
        """End to end through the confirm loop: cached status stays IDLE past
        every poll, the pane shows a live turn, nothing is typed into it."""
        provider = self._provider()
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
            patch.object(ts.status_monitor, "get_buffer", return_value=self._SPINNER * 3),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1", self._MESSAGE, None, "sup", None, provider=provider
            )

        # Started: the caller must not classify this as a dropped submit, so the
        # delete_worker teardown arm never fires...
        assert ok is True
        # ...and nothing was typed into the already-working pane: no full
        # re-delivery (which would run the task twice) and no blind Enter.
        send.assert_not_called()
        key.assert_not_called()

    # --- the verdict is bound to post-dispatch bytes, not to the frame -------

    @pytest.mark.parametrize(
        "gap, expected_status",
        [
            (12, "processing"),  # spinner inside get_status's 25-line tail
            (20, "processing"),  # ...at its far edge
            (28, "completed"),  # spinner out of the tail: the bullet is a marker
        ],
    )
    def test_startup_residue_on_a_dropped_task_still_redelivers(self, gap, expected_status):
        """A task-less pane reads STARTED on the frame at every distance, yet
        emitted nothing since the dispatch — so the task really was dropped."""
        from cli_agent_orchestrator.providers.codex import _has_startup_idle_composer

        frame = self._residue_frame(gap, self._IDLE_COMPOSER)
        # Precondition — the frame alone is genuinely misleading: the provider
        # reports ready (initialize() would have returned) while the whole-frame
        # status says started, and the message is nowhere.
        assert _has_startup_idle_composer(frame) is True
        assert self._provider().get_status(frame).value == expected_status
        assert "sup-123" not in frame

        started, enter, full_resend = self._redeliver(frame, self._MESSAGE, "")

        assert started is False
        assert full_resend is True
        assert enter is False

    def test_stale_activity_of_a_previous_turn_does_not_confirm_a_new_message(self):
        """A completed EARLIER handoff, still on screen, must not vouch for a
        message that was never delivered. Its bullet predates the dispatch, so
        it contributes no post-dispatch bytes."""
        frame = (
            "› [CAO Handoff] Supervisor terminal ID: OLD-999. Do the old task.\n"
            "• Finished the old task\n"
            "\n› Write tests for @filename\n\n" + self._FOOTER
        )
        assert self._provider().get_status(frame).value == "completed"

        provider = self._provider()
        assert provider.direct_probe_confirms_dispatch("") is False
        assert ts._worker_is_started_direct("t1", provider, "") is False

    def test_accepted_turn_is_confirmed_even_after_its_echo_scrolls_away(self):
        """An accepted turn can out-scroll its own echo while a spinner remains.
        The frame no longer contains the message, but the live spinner is
        post-dispatch output, so the turn is confirmed and NOT re-sent."""
        frame = (
            "\n".join(f"  output line {i}" for i in range(190))
            + "\n"
            + self._SPINNER
            + self._FOOTER
        )
        assert self._provider().get_status(frame).value == "processing"
        assert "sup-123" not in frame

        started, enter, full_resend = self._redeliver(frame, self._MESSAGE, self._SPINNER * 4)

        assert started is True
        assert full_resend is False, "re-pasting here would run the task twice"
        assert enter is False

    def test_reply_wording_that_echoes_the_prompt_still_confirms(self):
        """A valid fast reply whose words appear in the prompt must not be
        discarded — the verdict never inspects the message text."""
        message = "Reply with Done"
        frame = "› Reply with Done\n• Done\n\n› Write tests for @filename\n\n" + self._FOOTER
        assert self._provider().get_status(frame).value == "completed"

        started, enter, full_resend = self._redeliver(frame, message, self._SPINNER + "• Done\n")

        assert started is True
        assert enter is False and full_resend is False

    def test_pasted_bullets_cannot_confirm_their_own_delivery(self):
        """The composer echoes a paste as it renders, so a multi-line message
        carrying its own bullet emits one without any turn starting. Only the
        spinner's ``(<n>s • esc to interrupt)`` shape counts as evidence."""
        provider = self._provider()
        echoed_paste = "› Review these findings:\n  • first finding\n  • second finding\n"

        assert provider.direct_probe_confirms_dispatch(echoed_paste) is False
        assert provider.direct_probe_confirms_dispatch(echoed_paste + self._SPINNER) is True

    def test_unicode_and_empty_post_dispatch_output_are_unproven(self):
        provider = self._provider()
        assert provider.direct_probe_confirms_dispatch("") is False
        assert provider.direct_probe_confirms_dispatch("› Review this:\n  • 日本語\n") is False

    def test_spinner_is_recognized_through_terminal_escapes(self):
        """The buffer holds the RAW byte stream, so the evidence must survive
        the SGR/cursor sequences a repainting TUI interleaves."""
        provider = self._provider()
        raw = "\x1b[2K\x1b[1;32m• Working (12s • esc to interrupt)\x1b[0m\r\n"
        assert provider.direct_probe_confirms_dispatch(raw) is True

    def test_unproven_delivery_with_post_dispatch_output_withholds_the_resend(self):
        """Absence of proof is not proof of a dropped paste: when the terminal
        emitted output after the dispatch and our text is not on screen, the
        full re-send (the only branch that can duplicate work) is withheld."""
        frame = "\n".join(f"  output line {i}" for i in range(190)) + "\n" + self._FOOTER

        started, enter, full_resend = self._redeliver(frame, self._MESSAGE, "  a line of output\n")

        assert started is False
        assert full_resend is False
        assert enter is False

    def test_probe_declines_when_the_backend_feeds_no_buffer(self):
        """Event-inbox backends (herdr) never push a byte buffer. The probe then
        vouches for nothing and the caller keeps its pre-existing behavior."""
        provider = self._provider()
        assert ts._worker_is_started_direct("t1", provider, "") is False
