"""Unit tests for mock_cli scripted-prompt mode.

Proves:
- When CAO_MOCK_CLI_SCRIPTED_PROMPTS=1 and buffer contains APPROVAL_REQUIRED:
  marker, get_status returns WAITING_USER_ANSWER.
- When answer is delivered (text after marker), status clears back to normal.
- Default behavior unchanged when env var not set.
"""

from __future__ import annotations

import os
import pty
import select
import subprocess
import time
from pathlib import Path

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.mock_cli import (
    SCRIPTED_PROMPT_MARKER,
    MockCliProvider,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def provider():
    """Create a MockCliProvider instance (does not launch binary)."""
    return MockCliProvider(
        terminal_id="t-test",
        session_name="test-session",
        window_name="test-window",
    )


# ---------------------------------------------------------------------------
# Default behavior (env not set)
# ---------------------------------------------------------------------------


class TestDefaultBehavior:
    """Scripted-prompt mode OFF: default behavior unchanged."""

    def test_marker_ignored_when_env_not_set(self, provider, monkeypatch):
        """APPROVAL_REQUIRED: marker is ignored without env var."""
        monkeypatch.delenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", raising=False)
        buffer = f"Some output\n{SCRIPTED_PROMPT_MARKER} Do you approve?\n"
        # Without idle prompt, returns PROCESSING
        status = provider.get_status(buffer)
        assert status == TerminalStatus.PROCESSING

    def test_normal_idle(self, provider, monkeypatch):
        monkeypatch.delenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", raising=False)
        buffer = "Welcome to mock_cli\n\u276f "
        status = provider.get_status(buffer)
        assert status == TerminalStatus.IDLE

    def test_normal_completed(self, provider, monkeypatch):
        monkeypatch.delenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", raising=False)
        buffer = "> MOCK: hello\n\u276f "
        status = provider.get_status(buffer)
        assert status == TerminalStatus.COMPLETED

    def test_empty_buffer(self, provider, monkeypatch):
        monkeypatch.delenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", raising=False)
        assert provider.get_status("") == TerminalStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Scripted-prompt mode ON
# ---------------------------------------------------------------------------


class TestScriptedPromptMode:
    """Scripted-prompt mode ON: APPROVAL_REQUIRED: triggers WAITING_USER_ANSWER."""

    def test_waiting_when_marker_present(self, provider, monkeypatch):
        """Buffer with marker and no answer -> WAITING_USER_ANSWER."""
        monkeypatch.setenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", "1")
        buffer = f"Processing...\n{SCRIPTED_PROMPT_MARKER} Allow file write?"
        status = provider.get_status(buffer)
        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_waiting_with_whitespace_after_marker(self, provider, monkeypatch):
        """Only whitespace after marker -> still WAITING_USER_ANSWER."""
        monkeypatch.setenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", "1")
        buffer = f"Output\n{SCRIPTED_PROMPT_MARKER}   \n  \n"
        status = provider.get_status(buffer)
        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_clears_after_answer(self, provider, monkeypatch):
        """Text after marker on subsequent lines -> answer delivered, status clears."""
        monkeypatch.setenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", "1")
        buffer = (
            f"Processing...\n"
            f"{SCRIPTED_PROMPT_MARKER} Allow?\n"
            f"y\n"  # Answer delivered
            f"\u276f "  # Idle prompt
        )
        status = provider.get_status(buffer)
        # Should return to normal (IDLE since it has the prompt)
        assert status == TerminalStatus.IDLE

    def test_clears_to_completed(self, provider, monkeypatch):
        """Answer + response marker -> COMPLETED."""
        monkeypatch.setenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", "1")
        buffer = (
            f"Processing...\n"
            f"{SCRIPTED_PROMPT_MARKER} Allow?\n"
            f"y\n"  # Answer
            f"> MOCK: done\n"
            f"\u276f "
        )
        status = provider.get_status(buffer)
        assert status == TerminalStatus.COMPLETED

    def test_env_true_variant(self, provider, monkeypatch):
        """'true' value also enables scripted mode."""
        monkeypatch.setenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", "true")
        buffer = f"{SCRIPTED_PROMPT_MARKER} Approve deletion?"
        status = provider.get_status(buffer)
        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_env_yes_variant(self, provider, monkeypatch):
        """'yes' value also enables scripted mode."""
        monkeypatch.setenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", "yes")
        buffer = f"{SCRIPTED_PROMPT_MARKER} Approve deletion?"
        status = provider.get_status(buffer)
        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_error_still_takes_priority(self, provider, monkeypatch):
        """ERROR indicator still takes priority over scripted prompt."""
        monkeypatch.setenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", "1")
        buffer = f"ERROR: mock failure injected\n{SCRIPTED_PROMPT_MARKER} Allow?"
        status = provider.get_status(buffer)
        assert status == TerminalStatus.ERROR


# ---------------------------------------------------------------------------
# Round-trip status transition
# ---------------------------------------------------------------------------


class TestStatusTransitionRoundTrip:
    """Full round-trip: IDLE -> WAITING -> answer -> IDLE/COMPLETED."""

    def test_full_round_trip(self, provider, monkeypatch):
        monkeypatch.setenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", "1")

        # Initial idle
        buffer_idle = "\u276f "
        assert provider.get_status(buffer_idle) == TerminalStatus.IDLE

        # Terminal shows approval prompt
        buffer_waiting = f"\u276f \n{SCRIPTED_PROMPT_MARKER} Execute rm -rf /tmp?"
        assert provider.get_status(buffer_waiting) == TerminalStatus.WAITING_USER_ANSWER

        # User delivers answer
        buffer_answered = (
            f"\u276f \n{SCRIPTED_PROMPT_MARKER} Execute rm -rf /tmp?\n"
            f"y\n"
            f"> MOCK: executed\n"
            f"\u276f "
        )
        assert provider.get_status(buffer_answered) == TerminalStatus.COMPLETED


# ---------------------------------------------------------------------------
# The real binary: the __mock_approval__ magic (#1116)
# ---------------------------------------------------------------------------
#
# Every test above fakes the buffer. These run the real ``mock_cli`` binary in
# a pty, so they prove the binary can actually hold the prompt state that
# scripted mode detects. Before the magic existed, the binary echoed the marker
# as ``> MOCK: APPROVAL_REQUIRED: ...`` and reprinted the prompt, so a
# non-blank line always followed the marker and the status could never be
# WAITING_USER_ANSWER.

MOCK_CLI_BIN = Path(__file__).resolve().parent / "fixtures" / "bin" / "mock_cli"


class _LiveMockCli:
    """The real binary on a pty, with bounded reads."""

    def __init__(self) -> None:
        self.parent_fd, child_fd = pty.openpty()
        self.proc = subprocess.Popen(
            [str(MOCK_CLI_BIN), "--delay-ms", "0"],
            stdin=child_fd,
            stdout=child_fd,
            stderr=child_fd,
            close_fds=True,
        )
        os.close(child_fd)
        self.buffer = ""

    def read_until(self, needle: str, timeout: float = 5.0) -> str:
        deadline = time.monotonic() + timeout
        while needle not in self.buffer and time.monotonic() < deadline:
            ready, _, _ = select.select([self.parent_fd], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(self.parent_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                self.buffer += chunk.decode("utf-8", "replace").replace("\r\n", "\n")
        return self.buffer

    def settle(self, quiet: float = 0.3) -> str:
        """Read until the binary has printed nothing for ``quiet`` seconds."""
        end = time.monotonic() + 5.0
        last = time.monotonic()
        while time.monotonic() < end and time.monotonic() - last < quiet:
            ready, _, _ = select.select([self.parent_fd], [], [], 0.05)
            if ready:
                chunk = os.read(self.parent_fd, 4096)
                if chunk:
                    self.buffer += chunk.decode("utf-8", "replace").replace("\r\n", "\n")
                    last = time.monotonic()
        return self.buffer

    def ask(self, text: str = "Allow?") -> str:
        """Raise the scripted prompt; wait for its marker, then for the output to settle.

        Waiting on the marker first bounds the read by content, not by the quiet
        window alone, so a slow CI runner cannot settle before the prompt prints.
        """
        self.send(f"__mock_approval__ {text}")
        self.read_until(f"{SCRIPTED_PROMPT_MARKER} {text}")
        return self.settle()

    def send(self, line: str) -> None:
        os.write(self.parent_fd, (line + "\n").encode("utf-8"))

    def close(self) -> None:
        try:
            # Answer a prompt a test left open, so /exit is not read as the answer
            # and the wait below does not stall. Outside a prompt it is one echo.
            self.send("n")
            self.send("/exit")
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
            self.proc.wait(timeout=5)
        finally:
            os.close(self.parent_fd)


@pytest.fixture
def live_mock_cli():
    cli = _LiveMockCli()
    cli.read_until("\u276f ")
    yield cli
    cli.close()


class TestMockApprovalMagicOnTheRealBinary:
    """``__mock_approval__ <text>`` holds WAITING_USER_ANSWER until it is answered y or n."""

    def test_the_magic_holds_the_waiting_state(self, provider, live_mock_cli, monkeypatch):
        monkeypatch.setenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", "1")
        buffer = live_mock_cli.ask()

        assert f"{SCRIPTED_PROMPT_MARKER} Allow?" in buffer
        assert provider.get_status(buffer) == TerminalStatus.WAITING_USER_ANSWER

    def test_an_empty_line_first_does_not_answer_it(self, provider, live_mock_cli, monkeypatch):
        """CAO's bracketed paste can deliver an empty line before the answer."""
        monkeypatch.setenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", "1")
        live_mock_cli.ask()
        live_mock_cli.send("")
        live_mock_cli.send("y")
        buffer = live_mock_cli.read_until("> MOCK: answered y")

        assert "> MOCK: answered y" in buffer

    @pytest.mark.parametrize("answer", ["y", "n"])
    def test_answering_clears_to_completed(self, provider, live_mock_cli, monkeypatch, answer):
        monkeypatch.setenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", "1")
        live_mock_cli.ask()
        live_mock_cli.send(answer)
        live_mock_cli.read_until(f"> MOCK: answered {answer}")
        buffer = live_mock_cli.settle()

        assert f"> MOCK: answered {answer}" in buffer
        assert provider.get_status(buffer) == TerminalStatus.COMPLETED

    def test_any_other_answer_is_reported_as_unexpected(self, live_mock_cli):
        """Only y and n are accepted, so a change in CAO's decision translation shows."""
        live_mock_cli.ask()
        live_mock_cli.send("x")
        buffer = live_mock_cli.read_until("> MOCK: unexpected answer x")

        assert "> MOCK: unexpected answer x" in buffer
        assert "answered" not in buffer

    def test_the_header_lists_the_magic(self):
        header = MOCK_CLI_BIN.read_text(encoding="utf-8").split("set -u", 1)[0]
        assert "__mock_approval__" in header
