"""Tests for issue #407: content-based staleness guard after send_input.

The real bug: after send_input pastes into tmux, the buffer still returns
the previous turn's response + idle prompt. get_status() would re-derive
COMPLETED from that stale buffer, causing wait_until_status(COMPLETED) to
return immediately on the OLD turn's output.

The fix: mark_input_received() captures a tail-hash (ANSI-stripped hash of
the last N lines) and the extracted last-response text. The buffer-path
get_status() returns PROCESSING while the tail-hash matches the snapshot
(screen unchanged). Once the tail differs, normal derivation runs but a
secondary guard prevents COMPLETED when the derived last-response is still
the old turn's text (handles paste-echo growing the buffer without new output).

Critical property: buffer length is NOT monotonic (Ink composer-collapse,
sliding -S -200 window), so the guard uses content hashing, not length.
"""

from unittest.mock import patch

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider


class TestContentBasedStalenessGuard:
    """Test the content-based staleness check on the buffer path."""

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_stale_buffer_after_send_input_returns_processing(self, mock_backend):
        """Right after mark_input_received, unchanged buffer → PROCESSING."""
        mock_backend.get_native_status.return_value = None
        mock_backend.supports_event_inbox.return_value = False
        stale_buffer = "⏺ Previous response\n────────────────────────\n❯ "
        mock_backend.get_history.return_value = stale_buffer

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")

        assert provider.get_status(stale_buffer) == TerminalStatus.COMPLETED

        provider.mark_input_received()

        assert provider.get_status(stale_buffer) == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_shorter_buffer_after_input_still_processing(self, mock_backend):
        """Buffer SHRINKS after input (Ink composer-collapse) → still PROCESSING
        while tail content unchanged. This is the key regression the length-based
        guard got wrong."""
        mock_backend.get_native_status.return_value = None
        mock_backend.supports_event_inbox.return_value = False
        initial_buffer = (
            "Some long preamble\n" * 10 + "⏺ Previous response\n────────────────────────\n❯ "
        )
        mock_backend.get_history.return_value = initial_buffer

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider.mark_input_received()

        # Buffer shrinks (Ink collapse) but tail content is identical
        shorter_buffer = "⏺ Previous response\n────────────────────────\n❯ "
        assert provider.get_status(shorter_buffer) == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_new_response_completes_even_with_shorter_buffer(self, mock_backend):
        """New turn produces SHORTER total buffer than snapshot but with different
        content → must reach COMPLETED (the hang case from the review)."""
        mock_backend.get_native_status.return_value = None
        mock_backend.supports_event_inbox.return_value = False
        initial_buffer = (
            "A" * 500
            + "\n"
            + "⏺ Original long response text here\n"
            + "────────────────────────\n❯ "
        )
        mock_backend.get_history.return_value = initial_buffer

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider.mark_input_received()

        # New response is shorter total but different content
        new_buffer = "⏺ Short new reply\n────────────────────────\n❯ "
        assert provider.get_status(new_buffer) == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_identical_response_text_across_turns_still_completes(self, mock_backend):
        """New turn produces IDENTICAL response text to previous turn → must
        reach COMPLETED because the tail hash differs (echo of new input)."""
        mock_backend.get_native_status.return_value = None
        mock_backend.supports_event_inbox.return_value = False
        initial_buffer = "⏺ Done.\n────────────────────────\n❯ "
        mock_backend.get_history.return_value = initial_buffer

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider.mark_input_received()

        # New turn: user input echo + same response text → tail hash differs
        new_buffer = (
            "⏺ Done.\n────────────────────────\n"
            "❯ do it again\n"
            "⏺ Done.\n────────────────────────\n❯ "
        )
        assert provider.get_status(new_buffer) == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_paste_echo_with_old_response_not_completed(self, mock_backend):
        """Immediately after paste, buffer shows paste echo but old response
        still visible → NOT COMPLETED (the original stale-COMPLETED bug).

        Realistic scenario: tmux sliding window captures old response + separator
        + pasted user input at the new ❯ prompt. The pasted text changes the
        tail hash but the last ⏺ marker is still the old response. The prompt
        character in the pasted line satisfies last_idle; old ⏺ satisfies
        last_response → would falsely derive COMPLETED without the guard.
        """
        mock_backend.get_native_status.return_value = None
        mock_backend.supports_event_inbox.return_value = False
        initial_buffer = "⏺ Old answer\n────────────────────────\n❯ "
        mock_backend.get_history.return_value = initial_buffer

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider.mark_input_received()

        # Paste echo: user text appended after the prompt. The ❯ with text after
        # it still matches IDLE_PROMPT_PATTERN. The last response marker is still
        # "Old answer" with the same count as at snapshot time.
        paste_echo_buffer = (
            "⏺ Old answer\n────────────────────────\n" "❯ this is my new task that I pasted in"
        )
        # Tail hash differs from snapshot (new text) but last-response is still
        # "Old answer" with same response count → PROCESSING
        assert provider.get_status(paste_echo_buffer) == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_buffer_grows_after_input_resumes_normal_detection(self, mock_backend):
        """Once buffer shows new content with different response, normal derivation resumes."""
        mock_backend.get_native_status.return_value = None
        mock_backend.supports_event_inbox.return_value = False
        stale_buffer = "⏺ Previous response\n────────────────────────\n❯ "
        mock_backend.get_history.return_value = stale_buffer

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider.mark_input_received()

        assert provider.get_status(stale_buffer) == TerminalStatus.PROCESSING

        # Agent finishes with new response text
        done_buffer = (
            stale_buffer + "\n❯ new task text\n⏺ New response\n" "────────────────────────\n❯ "
        )
        assert provider.get_status(done_buffer) == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_staleness_guard_inactive_before_first_input(self, mock_backend):
        """Before any mark_input_received, guard is inactive (generation=0)."""
        mock_backend.get_native_status.return_value = None
        mock_backend.supports_event_inbox.return_value = False

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider._input_generation == 0
        assert provider.get_status("⏺ Response\n❯ ") == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_two_turns_staleness_guard_resets(self, mock_backend):
        """Second mark_input_received resets the snapshot for the new turn."""
        mock_backend.get_native_status.return_value = None
        mock_backend.supports_event_inbox.return_value = False

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")

        # Turn 1
        turn1_buffer = "⏺ Turn 1 response\n────────────────────────\n❯ "
        mock_backend.get_history.return_value = turn1_buffer
        provider.mark_input_received()
        assert provider.get_status(turn1_buffer) == TerminalStatus.PROCESSING

        # Turn 1 completes
        turn1_done = turn1_buffer + "\n❯ task1\n⏺ Done task 1\n────────────────────────\n❯ "
        assert provider.get_status(turn1_done) == TerminalStatus.COMPLETED

        # Turn 2: mark_input_received with current buffer
        mock_backend.get_history.return_value = turn1_done
        provider.mark_input_received()
        assert provider.get_status(turn1_done) == TerminalStatus.PROCESSING

        # Turn 2 completes
        turn2_done = turn1_done + "\n❯ task2\n⏺ Done task 2\n────────────────────────\n❯ "
        assert provider.get_status(turn2_done) == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_native_path_unaffected_by_staleness_guard(self, mock_backend):
        """Native path (herdr) bypasses the staleness guard entirely."""
        mock_backend.get_history.return_value = "⏺ Previous\n❯ "
        mock_backend.get_native_status.return_value = TerminalStatus.COMPLETED
        mock_backend.supports_event_inbox.return_value = True

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider.mark_input_received()

        # Native returns COMPLETED + _task_dispatched=True → flush-wait logic
        result = provider.get_status("")
        assert result == TerminalStatus.PROCESSING  # flush wait, not staleness guard

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_mark_input_received_increments_generation(self, mock_backend):
        """Each mark_input_received call increments _input_generation."""
        mock_backend.get_native_status.return_value = None
        mock_backend.supports_event_inbox.return_value = False
        mock_backend.get_history.return_value = "❯ "

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        assert provider._input_generation == 0

        provider.mark_input_received()
        assert provider._input_generation == 1

        provider.mark_input_received()
        assert provider._input_generation == 2

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_no_snapshot_response_allows_any_completed(self, mock_backend):
        """When snapshot had no response (e.g. first turn from IDLE), any
        COMPLETED with a response is accepted immediately."""
        mock_backend.get_native_status.return_value = None
        mock_backend.supports_event_inbox.return_value = False
        # Initial buffer: just idle prompt, no response marker
        idle_buffer = "────────────────────────\n❯ "
        mock_backend.get_history.return_value = idle_buffer

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider.mark_input_received()

        # Agent responds
        response_buffer = "────────────────────────\n❯ task\n⏺ Here is the answer\n❯ "
        assert provider.get_status(response_buffer) == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_marker_count_decrease_via_eviction_reaches_completed(self, mock_backend):
        """Sliding window evicts old markers → count DECREASES below snapshot.
        Identical response text but fewer markers means the window slid (new
        activity pushed old markers out) — must NOT hang in PROCESSING.

        Repro scenario: snapshot has 2 markers (old response A + earlier one);
        new turn completes with identical text "Response A" while intervening
        output evicts the earlier marker → current_count=1 < snapshot_count=2.
        """
        mock_backend.get_native_status.return_value = None
        mock_backend.supports_event_inbox.return_value = False
        # Snapshot: 2 response markers visible in window
        initial_buffer = (
            "⏺ Earlier response\n────────────────────────\n"
            "❯ task A\n"
            "⏺ Response A\n────────────────────────\n❯ "
        )
        mock_backend.get_history.return_value = initial_buffer

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        # Snapshot captures: last_response="Response A", count=2
        provider.mark_input_received()

        # New turn: window slid, earlier marker evicted; new identical response
        evicted_buffer = "⏺ Response A\n────────────────────────\n❯ "
        # current_count=1 < snapshot_count=2 → must be COMPLETED (not stuck)
        assert provider.get_status(evicted_buffer) == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_paste_echo_same_count_still_processing(self, mock_backend):
        """Paste-echo case: text matches AND count unchanged → PROCESSING.
        This is the legitimate hold case the guard protects against."""
        mock_backend.get_native_status.return_value = None
        mock_backend.supports_event_inbox.return_value = False
        initial_buffer = "⏺ Old answer\n────────────────────────\n❯ "
        mock_backend.get_history.return_value = initial_buffer

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        # Snapshot: last_response="Old answer", count=1
        provider.mark_input_received()

        # Paste echo: same response marker, same count, same text
        paste_buffer = "⏺ Old answer\n────────────────────────\n" "❯ some new pasted input here"
        # count=1 == snapshot_count=1, text matches → PROCESSING
        assert provider.get_status(paste_buffer) == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_identical_response_count_increased_reaches_completed(self, mock_backend):
        """New turn produces identical response text with MORE markers →
        COMPLETED (a new response was emitted, count proves it)."""
        mock_backend.get_native_status.return_value = None
        mock_backend.supports_event_inbox.return_value = False
        initial_buffer = "⏺ Done.\n────────────────────────\n❯ "
        mock_backend.get_history.return_value = initial_buffer

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        # Snapshot: last_response="Done.", count=1
        provider.mark_input_received()

        # New turn: same response text, but 2 markers now
        new_buffer = (
            "⏺ Done.\n────────────────────────\n"
            "❯ repeat\n"
            "⏺ Done.\n────────────────────────\n❯ "
        )
        # current_count=2 > snapshot_count=1 → COMPLETED
        assert provider.get_status(new_buffer) == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_effort_footer_does_not_perturb_marker_count(self, mock_backend):
        """Own-line effort footer ("● high · /effort", GH #459) appearing after
        the snapshot must not increment the marker count or hijack last-response
        extraction — the guard must keep holding PROCESSING, not release early."""
        mock_backend.get_native_status.return_value = None
        mock_backend.supports_event_inbox.return_value = False
        initial_buffer = "⏺ Old answer\n────────────────────────\n❯ "
        mock_backend.get_history.return_value = initial_buffer

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        # Snapshot: last_response="Old answer", count=1 (footer absent)
        provider.mark_input_received()

        # Footer renders on a later poll while the old response is still the
        # only real response on screen. Without footer exclusion this counted
        # as a second marker AND became the extracted "last response",
        # releasing the guard into a stale COMPLETED.
        footer_buffer = "⏺ Old answer\n────────────────────────\n" "● high · /effort\n" "❯ "
        assert provider.get_status(footer_buffer) == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.backends.registry._backend")
    def test_effort_footer_present_at_snapshot_and_poll_holds(self, mock_backend):
        """Footer visible at both snapshot and poll: counts match with the
        footer excluded on both sides — guard holds PROCESSING."""
        mock_backend.get_native_status.return_value = None
        mock_backend.supports_event_inbox.return_value = False
        initial_buffer = "⏺ Old answer\n────────────────────────\n" "● high · /effort\n" "❯ "
        mock_backend.get_history.return_value = initial_buffer

        provider = ClaudeCodeProvider("test123", "test-session", "window-0")
        provider.mark_input_received()
        assert provider.get_status(initial_buffer) == TerminalStatus.PROCESSING
