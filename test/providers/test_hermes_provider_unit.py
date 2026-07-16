"""Unit tests for Hermes provider."""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.hermes import HermesProvider, ProviderError

HERMES_IDLE_OUTPUT = """
Hermes Agent v0.15.1
Profile: test-worker

custom-worker ❯
"""

HERMES_IDLE_CUSTOM_SYMBOL_OUTPUT = """
Beatriz Agent online. ੨ੱ ──── ✦ ──── ੨ੱ
✦ Tip: The terminal tool supports 6 backends.

test-worker ✦
"""

HERMES_IDLE_UNKNOWN_SYMBOL_OUTPUT = """
model │ 17.1K/1M │ [░░░░░░░░░░] │ 20s │ ⏲ 4s │ YOLO
───────────────────────────────────────────────────────────────────────────────
any-profile 🜁
───────────────────────────────────────────────────────────────────────────────
"""

HERMES_PROCESSING_OUTPUT = """
● Summarize this

deepseek-v4-flash-free │ 17.1K/1M │ [░░░░░░░░░░] │ ⏱ 2s │ YOLO
⚕ ❯ msg=interrupt · /queue · /bg · /steer · Ctrl+C cancel
"""

HERMES_PROCESSING_WITH_STALE_IDLE_TIMER_OUTPUT = """
● Summarize this

model │ 17.1K/1M │ [░░░░░░░░░░] │ 20s │ ⏲ 4s │ YOLO
⚕ ❯ msg=interrupt · /queue · /bg · /steer · Ctrl+C cancel
"""

HERMES_WAITING_APPROVAL_OUTPUT = """
● Run a dangerous command

  ⚠️  DANGEROUS COMMAND: overwrite project env/config file
      cp .env.example .env

      [o]nce  |  [s]ession  |  [a]lways  |  [d]eny

      Choice [o/s/a/D]:
"""

HERMES_WAITING_APPROVAL_WITH_INTERRUPT_OUTPUT = """
● Run a dangerous command

  ⚠️  DANGEROUS COMMAND: overwrite project env/config file
      cp .env.example .env

      [o]nce  |  [s]ession  |  [a]lways  |  [d]eny

      Choice [o/s/a/D]:

⚕ ❯ msg=interrupt · /queue · /bg · /steer · Ctrl+C cancel
"""

HERMES_WAITING_APPROVAL_ZH_OUTPUT = """
● 运行一个危险命令

  ⚠️  危险命令： overwrite project env/config file
      cp .env.example .env

      [o]仅此一次  |  [s]本次会话  |  [a]永久允许  |  [d]拒绝

      选择 [o/s/a/D]:
"""

HERMES_WAITING_BUTTON_STYLE_OUTPUT = """
● Run a dangerous command

Command approval required
Allow once   Allow always   Reject
ctrl+f fullscreen  ⇆ select  enter confirm
"""

HERMES_WAITING_CLARIFY_OUTPUT = """
● Please ask a clarification question.

╭─ Hermes needs your input ──────────────────────╮
│                                                │
│ 请选择实现方案                                        │
│                                                │
│ ❯ 1. 方案 A：保持现状                                 │
│   2. 方案 B：补充检测                                 │
│   3. 方案 C：重构状态机                                │
│   4. Other (type your answer)                  │
│                                                │
╰────────────────────────────────────────────────╯

  ❓ 请选择实现方案  (  5.7s)
  ↑/↓ to select, Enter to confirm  (114s)
 ⚕ deepseek-v4-flash-free │ 17.2K/1M │ [░░░░░░░░░░] 2% │ 29s │ ⏱ 9s │ ⚠ YOLO
────────────────────────────────────────────────────────────────────────────────
? ❯
────────────────────────────────────────────────────────────────────────────────
"""

HERMES_STALE_CLARIFY_COMPLETED_OUTPUT = """
● Please ask a clarification question.

╭─ Hermes needs your input ──────────────────────╮
│ 请选择实现方案                                        │
│ ❯ 1. 方案 A：保持现状                                 │
│   2. 方案 B：补充检测                                 │
│   3. 方案 C：重构状态机                                │
│   4. Other (type your answer)                  │
╰────────────────────────────────────────────────╯

 ─  ⚕ Hermes  ─────────────────────────────────────────────────────────────────

     我会采用方案 B。

 ──────────────────────────────────────────────────────────────────────────────
model │ 17.1K/1M │ [░░░░░░░░░░] 2% │ 7s │ ⏲ 3s
───────────────────────────────────────────────────────────────────────────────
any-profile 🜁
───────────────────────────────────────────────────────────────────────────────
"""

HERMES_STALE_APPROVAL_COMPLETED_OUTPUT = """
● Run a dangerous command

  ⚠️  DANGEROUS COMMAND: overwrite project env/config file
      cp .env.example .env

      [o]nce  |  [s]ession  |  [a]lways  |  [d]eny

      Choice [o/s/a/D]: o
      ✓ Allowed once

 ─  ⚕ Hermes  ─────────────────────────────────────────────────────────────────

     Done

 ──────────────────────────────────────────────────────────────────────────────
model │ 17.1K/1M │ [░░░░░░░░░░] 2% │ 7s │ ⏲ 3s
───────────────────────────────────────────────────────────────────────────────
any-profile 🜁
───────────────────────────────────────────────────────────────────────────────
"""

HERMES_COMPLETED_OUTPUT = """
● Only output OK

 ─  ⚕ Hermes  ─────────────────────────────────────────────────────────────────

     OK

 ──────────────────────────────────────────────────────────────────────────────

custom-worker ❯
"""

HERMES_COMPLETED_CUSTOM_OUTPUT = """
● Only output OK

 ━  Worker Reply  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

     OK

 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

my-themed-prompt ❯
"""

HERMES_COMPLETED_WITH_STALE_INITIALIZING_OUTPUT = """
● Only output OK
Initializing agent...

 ─  ੨ੱ Beatriz ੨ੱ  ───────────────────────────────────────────────────────────

     OK

 ──────────────────────────────────────────────────────────────────────────────
model │ 17.1K/1M │ [░░░░░░░░░░] 2% │ 7s │ ⏲ 3s │ YOLO
───────────────────────────────────────────────────────────────────────────────
any-profile 🜁
───────────────────────────────────────────────────────────────────────────────
"""

HERMES_NO_RESPONSE_OUTPUT = """
● Only output OK

my-themed-prompt ❯
"""


class TestHermesBuildCommand:
    def _profile(self, hermes_profile: str | None = "test-worker"):
        mock_profile = MagicMock()
        mock_profile.name = "developer"
        mock_profile.hermesProfile = hermes_profile
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        return mock_profile

    def test_build_command_without_agent_profile_uses_default_hermes(self):
        provider = HermesProvider("tid", "sess", "win", None)

        assert provider._build_hermes_command() == "hermes chat --yolo --accept-hooks --source cao"

    @patch("cli_agent_orchestrator.providers.hermes.load_agent_profile")
    def test_build_command_uses_hermes_profile_from_agent_profile(self, mock_load):
        mock_load.return_value = self._profile("test-worker")

        provider = HermesProvider("tid", "sess", "win", "developer")
        assert (
            provider._build_hermes_command()
            == "test-worker chat --yolo --accept-hooks --source cao"
        )

    @patch("cli_agent_orchestrator.providers.hermes.load_agent_profile")
    def test_build_command_uses_default_hermes_when_profile_has_no_hermes_profile(self, mock_load):
        mock_load.return_value = self._profile(None)

        provider = HermesProvider("tid", "sess", "win", "developer")

        assert provider._build_hermes_command() == "hermes chat --yolo --accept-hooks --source cao"

    @patch("cli_agent_orchestrator.providers.hermes.load_agent_profile")
    def test_build_command_appends_model_when_profile_sets_model(self, mock_load):
        mock_profile = self._profile("test-worker")
        mock_profile.model = "deepseek-v4-flash-free"
        mock_load.return_value = mock_profile

        provider = HermesProvider("tid", "sess", "win", "developer")
        command = provider._build_hermes_command()

        assert "--model deepseek-v4-flash-free" in command

    @patch("cli_agent_orchestrator.providers.hermes.load_agent_profile")
    def test_build_command_quotes_profile_with_spaces_or_shell_metacharacters(self, mock_load):
        mock_load.return_value = self._profile("test worker; rm -rf /")

        provider = HermesProvider("tid", "sess", "win", "developer")

        assert (
            provider._build_hermes_command()
            == "'test worker; rm -rf /' chat --yolo --accept-hooks --source cao"
        )

    @patch("cli_agent_orchestrator.providers.hermes.load_agent_profile")
    def test_build_command_profile_load_failure(self, mock_load):
        mock_load.side_effect = RuntimeError("Profile not found")
        provider = HermesProvider("tid", "sess", "win", "missing")

        with pytest.raises(ProviderError, match="Failed to load agent profile"):
            provider._build_hermes_command()


class TestHermesInitialization:
    def _profile(self):
        mock_profile = MagicMock()
        mock_profile.hermesProfile = "test-worker"
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        return mock_profile

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.hermes.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.hermes.wait_until_status")
    @patch("cli_agent_orchestrator.providers.hermes.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.hermes.tmux_client")
    async def test_initialize_success(
        self, mock_tmux, mock_wait_shell, mock_wait_status, mock_load
    ):
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_load.return_value = self._profile()

        provider = HermesProvider("tid", "sess", "win", "developer")
        result = await provider.initialize()

        assert result is True
        mock_tmux.send_keys.assert_called_once_with(
            "sess", "win", "test-worker chat --yolo --accept-hooks --source cao"
        )

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.hermes.wait_for_shell")
    async def test_initialize_shell_timeout(self, mock_wait_shell):
        mock_wait_shell.return_value = False
        provider = HermesProvider("tid", "sess", "win", "developer")

        with pytest.raises(TimeoutError, match="Shell initialization timed out"):
            await provider.initialize()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.hermes.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.hermes.wait_until_status")
    @patch("cli_agent_orchestrator.providers.hermes.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.hermes.tmux_client")
    async def test_initialize_hermes_timeout(
        self, mock_tmux, mock_wait_shell, mock_wait_status, mock_load
    ):
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = False
        mock_load.return_value = self._profile()

        provider = HermesProvider("tid", "sess", "win", "developer")

        with pytest.raises(TimeoutError, match="Hermes initialization timed out"):
            await provider.initialize()


class TestHermesStatusDetection:
    def test_get_status_idle_with_custom_prompt_prefix(self):
        provider = HermesProvider("tid", "sess", "win", None)
        assert provider.get_status(HERMES_IDLE_OUTPUT) == TerminalStatus.IDLE

    def test_get_status_idle_with_custom_prompt_symbol(self):
        provider = HermesProvider("tid", "sess", "win", None)
        assert provider.get_status(HERMES_IDLE_CUSTOM_SYMBOL_OUTPUT) == TerminalStatus.IDLE

    def test_get_status_idle_with_stable_timer_and_unknown_prompt_symbol(self):
        provider = HermesProvider("tid", "sess", "win", None)
        assert provider.get_status(HERMES_IDLE_UNKNOWN_SYMBOL_OUTPUT) == TerminalStatus.PROCESSING
        assert provider.get_status(HERMES_IDLE_UNKNOWN_SYMBOL_OUTPUT) == TerminalStatus.IDLE

    def test_get_status_frozen_idle_timer_does_not_pin_idle_forever(self):
        provider = HermesProvider("tid", "sess", "win", None)
        statuses = [provider.get_status(HERMES_IDLE_UNKNOWN_SYMBOL_OUTPUT) for _ in range(10)]

        assert TerminalStatus.IDLE in statuses
        assert statuses[-1] == TerminalStatus.PROCESSING

    def test_get_status_processing_excludes_interrupt_prompt(self):
        provider = HermesProvider("tid", "sess", "win", None)
        assert provider.get_status(HERMES_PROCESSING_OUTPUT) == TerminalStatus.PROCESSING

    def test_get_status_processing_placeholder_overrides_stable_timer(self):
        provider = HermesProvider("tid", "sess", "win", None)
        assert (
            provider.get_status(HERMES_PROCESSING_WITH_STALE_IDLE_TIMER_OUTPUT)
            == TerminalStatus.PROCESSING
        )
        assert (
            provider.get_status(HERMES_PROCESSING_WITH_STALE_IDLE_TIMER_OUTPUT)
            == TerminalStatus.PROCESSING
        )

    def test_get_status_waiting_user_answer_for_hermes_approval_menu(self):
        provider = HermesProvider("tid", "sess", "win", None)
        assert (
            provider.get_status(HERMES_WAITING_APPROVAL_OUTPUT)
            == TerminalStatus.WAITING_USER_ANSWER
        )

    def test_get_status_waiting_prompt_wins_over_interrupt_marker(self):
        provider = HermesProvider("tid", "sess", "win", None)
        assert (
            provider.get_status(HERMES_WAITING_APPROVAL_WITH_INTERRUPT_OUTPUT)
            == TerminalStatus.WAITING_USER_ANSWER
        )

    def test_get_status_waiting_user_answer_for_localized_approval_menu(self):
        provider = HermesProvider("tid", "sess", "win", None)
        assert (
            provider.get_status(HERMES_WAITING_APPROVAL_ZH_OUTPUT)
            == TerminalStatus.WAITING_USER_ANSWER
        )

    def test_get_status_waiting_user_answer_for_button_style_approval(self):
        provider = HermesProvider("tid", "sess", "win", None)
        assert (
            provider.get_status(HERMES_WAITING_BUTTON_STYLE_OUTPUT)
            == TerminalStatus.WAITING_USER_ANSWER
        )

    def test_get_status_waiting_user_answer_for_clarify_picker(self):
        provider = HermesProvider("tid", "sess", "win", None)
        assert (
            provider.get_status(HERMES_WAITING_CLARIFY_OUTPUT) == TerminalStatus.WAITING_USER_ANSWER
        )

    def test_get_status_does_not_treat_stale_approval_as_waiting(self):
        provider = HermesProvider("tid", "sess", "win", None)
        assert (
            provider.get_status(HERMES_STALE_APPROVAL_COMPLETED_OUTPUT) == TerminalStatus.PROCESSING
        )
        assert (
            provider.get_status(HERMES_STALE_APPROVAL_COMPLETED_OUTPUT) == TerminalStatus.COMPLETED
        )

    def test_get_status_does_not_treat_stale_clarify_picker_as_waiting(self):
        provider = HermesProvider("tid", "sess", "win", None)
        assert (
            provider.get_status(HERMES_STALE_CLARIFY_COMPLETED_OUTPUT) == TerminalStatus.PROCESSING
        )
        assert (
            provider.get_status(HERMES_STALE_CLARIFY_COMPLETED_OUTPUT) == TerminalStatus.COMPLETED
        )

    def test_get_status_completed(self):
        provider = HermesProvider("tid", "sess", "win", None)
        assert provider.get_status(HERMES_COMPLETED_OUTPUT) == TerminalStatus.COMPLETED

    def test_get_status_completed_without_default_header(self):
        provider = HermesProvider("tid", "sess", "win", None)
        assert provider.get_status(HERMES_COMPLETED_CUSTOM_OUTPUT) == TerminalStatus.COMPLETED

    def test_get_status_completed_ignores_stale_initializing_text(self):
        provider = HermesProvider("tid", "sess", "win", None)
        assert (
            provider.get_status(HERMES_COMPLETED_WITH_STALE_INITIALIZING_OUTPUT)
            == TerminalStatus.PROCESSING
        )
        assert (
            provider.get_status(HERMES_COMPLETED_WITH_STALE_INITIALIZING_OUTPUT)
            == TerminalStatus.COMPLETED
        )

    def test_get_status_error(self):
        provider = HermesProvider("tid", "sess", "win", None)
        assert provider.get_status("Error: failed\n") == TerminalStatus.ERROR

    def test_get_status_empty_output(self):
        # native=None always falls through (no dispatch-timing guess); on tmux
        # the live-read fallback is a pass-through, so an empty buffer hits
        # Hermes's own no-output default (ERROR) directly.
        provider = HermesProvider("tid", "sess", "win", None)
        assert provider.get_status("") == TerminalStatus.ERROR


class TestHermesExtraction:
    def test_extract_last_message_with_default_header(self):
        provider = HermesProvider("tid", "sess", "win", None)
        assert provider.extract_last_message_from_script(HERMES_COMPLETED_OUTPUT) == "OK"

    def test_extract_last_message_without_default_header(self):
        provider = HermesProvider("tid", "sess", "win", None)
        assert provider.extract_last_message_from_script(HERMES_COMPLETED_CUSTOM_OUTPUT) == "OK"

    def test_extract_last_message_missing_response_raises(self):
        provider = HermesProvider("tid", "sess", "win", None)

        with pytest.raises(
            ValueError,
            match="Empty Hermes response|No Hermes idle prompt|No Hermes response found",
        ):
            provider.extract_last_message_from_script(HERMES_IDLE_OUTPUT)

    def test_extract_last_message_does_not_return_user_text(self):
        provider = HermesProvider("tid", "sess", "win", None)

        with pytest.raises(ValueError, match="Empty Hermes response"):
            provider.extract_last_message_from_script(HERMES_NO_RESPONSE_OUTPUT)


def test_exit_cli():
    provider = HermesProvider("tid", "sess", "win", None)
    assert provider.exit_cli() == "/exit"


def test_blocks_orchestrated_input_while_waiting_user_answer():
    provider = HermesProvider("tid", "sess", "win", None)
    assert provider.blocks_orchestrated_input_while_waiting_user_answer is True
