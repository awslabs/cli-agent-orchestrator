"""Unit tests for Copilot CLI provider."""

from __future__ import annotations

import asyncio
import json
import shlex
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider


class TestCopilotBoxedComposer:
    box = "╻▄▄▄▄▄▄▄▄\n┃\n╹▀▀▀▀▀▀▀▀\n"
    hint = "← open sidebar · Autopilot · Allow All · / commands · tab next tab"

    @pytest.mark.parametrize("update", ["", "v1.0.96-2 downloaded · next launch or /restart · "])
    @pytest.mark.parametrize(
        "model", ["", "            reviewer · Claude Sonnet 5", "\nreviewer · Claude Sonnet 5"]
    )
    @pytest.mark.parametrize("reply", ["", "❯ review\n● Done.\n"])
    @pytest.mark.parametrize("layout", ["prompt", "box"])
    @pytest.mark.parametrize("mode", ["Autopilot · Allow All", "Interactive · Manual Approval"])
    @pytest.mark.parametrize("width", [80, 220, 239])
    @pytest.mark.parametrize("ansi", [False, True])
    def test_captured_layouts_are_ready(self, update, model, reply, layout, mode, width, ansi):
        if layout == "box":
            composer = "╻" + "▄" * (width - 1) + "\n┃\n╹" + "▀" * (width - 1) + "\n"
        else:
            rule = "─" * width
            composer = f"{rule}\n❯\n{rule}\n"
        hint = self.hint.replace("Autopilot · Allow All", mode)
        output = reply + composer + update + hint + model + "\n\n"
        if ansi:
            output = "\x1b[38;2;120;120;120m" + output + "\x1b[0m"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        expected = TerminalStatus.COMPLETED if reply else TerminalStatus.IDLE
        with patch.object(provider, "_resolve_native_status", return_value=None):
            assert provider.get_status(output) == expected
            assert provider.probe_stale_processing_capture(output) == expected
            assert provider.commit_stale_processing_capture(output, expected)
        assert provider._unrecognized_rows_after_prompt(provider._clean(output).splitlines()) == []
        if reply:
            assert provider.extract_last_message_from_script(output) == "● Done."
        else:
            with pytest.raises(ValueError):
                provider.extract_last_message_from_script(output)

    @pytest.mark.parametrize(
        "row, expected",
        [
            ("∙ Thinking (Esc to cancel)", TerminalStatus.PROCESSING),
            ("⠋ Working · esc interrupt · enqueue", TerminalStatus.PROCESSING),
            ("Allow this tool? [y/n]", TerminalStatus.WAITING_USER_ANSWER),
            ("Allow this tool [y/n]", TerminalStatus.WAITING_USER_ANSWER),
            ("Confirm folder trust", TerminalStatus.WAITING_USER_ANSWER),
            ("Do you trust the files in this folder? [y/n]", TerminalStatus.WAITING_USER_ANSWER),
        ],
    )
    @pytest.mark.parametrize("placement", ["before", "after"])
    def test_box_does_not_hide_active_or_waiting_rows(self, row, expected, placement):
        frame = self.box + self.hint + "\n"
        output = "❯ review\n● Partial reply\n" + (
            row + "\n" + frame if placement == "before" else frame + row + "\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        with patch.object(provider, "_resolve_native_status", return_value=None):
            assert provider.get_status(output) == expected
            assert not provider.commit_stale_processing_capture(output, TerminalStatus.COMPLETED)

    @pytest.mark.parametrize(
        "busy_row",
        [
            "∙ Thinking (Esc to cancel)",
            "● Thinking (Esc to cancel)",
            "◉ Working · esc interrupt · enqueue",
            "◐ Thinking (Esc to cancel)",
        ],
    )
    @pytest.mark.parametrize("detail_count", [0, 1, 45])
    def test_box_does_not_hide_busy_row_in_tall_pane(self, busy_row, detail_count):
        output = (
            f"❯ review\n● Partial reply\n{busy_row}\n"
            + "detail\n" * detail_count
            + self.box
            + self.hint
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        with patch.object(provider, "_resolve_native_status", return_value=None):
            assert provider.get_status(output) == TerminalStatus.PROCESSING
            assert provider.probe_stale_processing_capture(output) == TerminalStatus.PROCESSING
            assert not provider.commit_stale_processing_capture(output, TerminalStatus.COMPLETED)

    @pytest.mark.parametrize("layout", ["prompt", "box"])
    @pytest.mark.parametrize(
        "answer",
        [
            "● The utility asks before deleting: [y/n].",
            "● The utility asks before deleting: [y/n]",
            "● The utility asks before deleting:\n  Continue? [y/n] is the prompt it shows.",
        ],
    )
    def test_reported_choice_is_not_a_live_dialog(self, layout, answer):
        composer = "❯\n" if layout == "prompt" else self.box
        output = "❯ explain the confirmation prompt\n" + answer + "\n" + composer + self.hint
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        with patch.object(provider, "_resolve_native_status", return_value=None):
            assert provider.get_status(output) == TerminalStatus.COMPLETED
            assert provider.probe_stale_processing_capture(output) == TerminalStatus.COMPLETED
            assert provider.commit_stale_processing_capture(output, TerminalStatus.COMPLETED)

    @pytest.mark.parametrize("layout", ["prompt", "box"])
    @pytest.mark.parametrize(
        "answer",
        ["● Confirm folder trust is the UI label to look for.", "● Allow this tool? [y/n]"],
    )
    def test_assistant_dialog_description_is_a_response(self, layout, answer):
        composer = "❯\n" if layout == "prompt" else self.box
        output = "❯ describe the dialog\n" + answer + "\n" + composer + self.hint
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        with patch.object(provider, "_resolve_native_status", return_value=None):
            assert provider.get_status(output) == TerminalStatus.COMPLETED
            assert provider.probe_stale_processing_capture(output) == TerminalStatus.COMPLETED

    @pytest.mark.parametrize("layout", ["prompt", "box"])
    @pytest.mark.parametrize("final_line", ["● Done.\n", ""])
    def test_assistant_description_of_busy_hint_is_a_response(self, layout, final_line):
        composer = "❯\n" if layout == "prompt" else self.box
        output = (
            "❯ explain the UI\n"
            "● The UI says Esc to cancel while working.\n" + final_line + composer + self.hint
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        with patch.object(provider, "_resolve_native_status", return_value=None):
            assert provider.get_status(output) == TerminalStatus.COMPLETED
            assert provider.probe_stale_processing_capture(output) == TerminalStatus.COMPLETED
        assert (
            provider.extract_last_message_from_script(output)
            == ("● The UI says Esc to cancel while working.\n" + final_line).strip()
        )

    @pytest.mark.parametrize("layout", ["prompt", "box"])
    @pytest.mark.parametrize(
        "answer",
        [
            "● Example prompt:\n  Continue? [y/n]",
            "● For instance:\n  Continue? [y/n]",
            "● apt then asks:\n  Do you want to continue? [Y/n]",
            "● The installer shows:\n  Press enter to continue",
            "● Example prompt:\n  The tool shows\n  Continue? [y/n]",
        ],
    )
    def test_indented_dialog_in_answer_is_a_response(self, layout, answer):
        composer = "❯\n" if layout == "prompt" else self.box
        output = "❯ explain the dialog\n" + answer + "\n" + composer + self.hint
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        with patch.object(provider, "_resolve_native_status", return_value=None):
            assert provider.get_status(output) == TerminalStatus.COMPLETED
            assert provider.probe_stale_processing_capture(output) == TerminalStatus.COMPLETED
            assert provider.commit_stale_processing_capture(output, TerminalStatus.COMPLETED)
        assert provider.extract_last_message_from_script(output) == answer

    @pytest.mark.parametrize("layout", ["prompt", "box"])
    def test_answer_paragraph_and_wrapped_user_prompt_are_not_dialogs(self, layout):
        composer = "❯\n" if layout == "prompt" else self.box
        output = (
            "❯ Quote the APT prompt\n"
            "   and include [y/n]\n"
            " ● APT asks for confirmation.\n\n"
            "   Do you want to continue? [Y/n]\n\n"
            " ● Task complete\n"
            "   └ Quoted the requested prompt.\n" + composer + self.hint
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        with patch.object(provider, "_resolve_native_status", return_value=None):
            assert provider.get_status(output) == TerminalStatus.COMPLETED
            assert provider.probe_stale_processing_capture(output) == TerminalStatus.COMPLETED
            assert provider.commit_stale_processing_capture(output, TerminalStatus.COMPLETED)

    @pytest.mark.parametrize("layout", ["none", "prompt", "box"])
    def test_framed_folder_trust_stays_waiting(self, layout):
        frame = (
            "╭────────────╮\n"
            "│ Confirm folder trust │\n"
            "│ Do you trust the files in this folder? │\n"
            "│ ❯ 1. Yes │\n"
            "╰────────────╯\n"
        )
        composer = self.box if layout == "box" else "❯\n" if layout == "prompt" else ""
        output = "❯ review\n● Partial reply\n" + frame + composer + self.hint
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        with patch.object(provider, "_resolve_native_status", return_value=None):
            assert provider.get_status(output) == TerminalStatus.WAITING_USER_ANSWER
            assert (
                provider.probe_stale_processing_capture(output)
                == TerminalStatus.WAITING_USER_ANSWER
            )
            assert not provider.commit_stale_processing_capture(output, TerminalStatus.COMPLETED)

    def test_raw_framed_folder_trust_row_is_waiting(self):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        with patch.object(provider, "_resolve_native_status", return_value=None):
            assert (
                provider.get_status("│ Do you trust the files in this folder?│")
                == TerminalStatus.WAITING_USER_ANSWER
            )

    @pytest.mark.parametrize("layout", ["prompt", "box"])
    @pytest.mark.parametrize(
        "question", ["Do you want to continue? [Y/n]", "Press Enter to continue"]
    )
    def test_answer_table_is_not_a_framed_dialog(self, layout, question):
        composer = "────────\n❯\n────────\n" if layout == "prompt" else self.box
        answer = (
            " ● ┌──────┬────────────────────────────────┐\n"
            "   │ Tool │ Question                       │\n"
            "   ├──────┼────────────────────────────────┤\n"
            f"   │ apt  │ {question:<30} │\n"
            "   └──────┴────────────────────────────────┘"
        )
        output = "❯ show the confirmation table\n" + answer + "\n" + composer + self.hint
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        with patch.object(provider, "_resolve_native_status", return_value=None):
            assert provider.get_status(output) == TerminalStatus.COMPLETED
            assert provider.probe_stale_processing_capture(output) == TerminalStatus.COMPLETED
            assert provider.commit_stale_processing_capture(output, TerminalStatus.COMPLETED)
        assert provider.extract_last_message_from_script(output) == answer.strip()

    def test_indented_busy_row_after_example_heading_stays_processing(self):
        output = (
            "❯ review\n● Example:\n  ∙ Thinking (Esc to cancel)\nMore output\n"
            + self.box
            + self.hint
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        with patch.object(provider, "_resolve_native_status", return_value=None):
            assert provider.get_status(output) == TerminalStatus.PROCESSING

    @pytest.mark.parametrize("reply", ["", "❯ previous\n● Old reply\n"])
    def test_nonempty_box_does_not_extract_draft_or_footer(self, reply):
        output = reply + "╻▄▄▄▄▄▄▄▄\n┃ draft\n╹▀▀▀▀▀▀▀▀\n" + self.hint
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        with patch.object(provider, "_resolve_native_status", return_value=None):
            assert provider.get_status(output) == TerminalStatus.PROCESSING
        if reply:
            assert provider.extract_last_message_from_script(output) == "● Old reply"
        else:
            with pytest.raises(ValueError, match="No provider response content found"):
                provider.extract_last_message_from_script(output)

    @pytest.mark.parametrize(
        "frame",
        [
            "╻▄▄▄▄▄▄▄▄\n┃\n",  # missing bottom
            "┃\n╹▀▀▀▀▀▀▀▀\n",  # missing top
            "╻▄▄▄▄▄▄▄▄\n╹▀▀▀▀▀▀▀▀\n",  # missing interior
            "╻▄▄▄▄▄▄▄▄\n┃ draft\n╹▀▀▀▀▀▀▀▀\n",  # nonempty
            "╻▄▄▄▄▄▄▄▄\n┃\n╹▀▀▀▀\n",  # torn widths
            "╭────────╮\n│        │\n╰────────╯\n",  # ordinary box
        ],
    )
    def test_incomplete_or_other_boxes_are_not_ready(self, frame):
        output = "❯ review\n● Partial reply\n" + frame + self.hint + "\n"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        with patch.object(provider, "_resolve_native_status", return_value=None):
            assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_box_requires_hint_and_preserves_boxed_reply(self):
        reply = "● Example:\n" + self.box + "ordinary boxed output"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        with patch.object(provider, "_resolve_native_status", return_value=None):
            assert provider.get_status(self.box) == TerminalStatus.PROCESSING
        output = "❯ explain\n" + reply + "\n" + self.box + self.hint
        assert provider.extract_last_message_from_script(output) == reply

    @pytest.mark.asyncio
    async def test_trust_handler_returns_without_sending_keys_for_ready_box(self):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        with (
            patch.object(provider, "_history", return_value=self.box + self.hint),
            patch.object(provider, "_send_enter") as enter,
            patch.object(provider, "_send_key") as key,
        ):
            await provider._accept_trust_prompts(timeout=1)
        enter.assert_not_called()
        key.assert_not_called()

    def test_assistant_fallback_excludes_boxed_composer(self):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        output = "assistant: Done.\n" + self.box + self.hint
        assert provider.extract_last_message_from_script(output) == "Done."


class TestCopilotDownloadedUpdateFooter:
    hint = (
        "v1.0.94 downloaded · next launch or /restart · "
        "← open sidebar · Autopilot · Allow All · / commands · tab next tab"
    )
    model = "reviewer · Claude Haiku 5.5"

    @pytest.mark.parametrize("version", ["v1.0.94", "v1.0.96-2", "v1.0.95-0"])
    @pytest.mark.parametrize("shared_row", [True, False])
    @pytest.mark.parametrize("reply", ["", "❯ review the change\n● FINDINGS: NO\n"])
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_downloaded_update_footer_is_ready(self, mock_backend, shared_row, reply, version):
        hint = self.hint.replace("v1.0.94", version)
        footer = f"{hint}            {self.model}" if shared_row else f"{hint}\n{self.model}"
        rule = "─" * 160
        output = f"{reply}{rule}\n❯\n{rule}\n {footer}\n"
        mock_backend.return_value.get_native_status.return_value = None
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        expected = TerminalStatus.COMPLETED if reply else TerminalStatus.IDLE
        assert provider.get_status(output) == expected
        assert provider.probe_stale_processing_capture(output) == expected
        assert provider.commit_stale_processing_capture(output, expected) is True
        if reply:
            assert provider.extract_last_message_from_script(output) == "● FINDINGS: NO"
        else:
            with pytest.raises(ValueError, match="No provider response content found"):
                provider.extract_last_message_from_script(output)

    @pytest.mark.parametrize(
        "row, expected",
        [
            ("∙ Thinking (Esc to cancel)", TerminalStatus.PROCESSING),
            ("⠋ Working · esc interrupt · enqueue", TerminalStatus.PROCESSING),
            ("Allow this tool to run? [y/n]", TerminalStatus.WAITING_USER_ANSWER),
            ("Error: connection failed", TerminalStatus.ERROR),
            ("Queued", TerminalStatus.PROCESSING),
        ],
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_downloaded_update_does_not_hide_blocking_rows(self, mock_backend, row, expected):
        output = f"❯ review\n● Partial reply\n❯\n{self.hint}\n{self.model}\n{row}\n"
        mock_backend.return_value.get_native_status.return_value = None
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == expected
        assert provider.probe_stale_processing_capture(output) == expected
        assert provider.commit_stale_processing_capture(output, TerminalStatus.IDLE) is False
        assert provider.commit_stale_processing_capture(output, TerminalStatus.COMPLETED) is False

    @pytest.mark.parametrize(
        "prefix",
        [
            "v1.0.94 downloading · next launch or /restart · ",
            "v1.0.94 downloaded · restart failed · ",
            "unknown notification · ",
        ],
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_unknown_update_notice_is_not_idle_chrome(self, mock_backend, prefix):
        hint = "← open sidebar · Autopilot · Allow All · / commands · tab next tab"
        output = f"❯\n{prefix}{hint}            {self.model}\n"
        mock_backend.return_value.get_native_status.return_value = None
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_downloaded_update_text_in_reply_is_preserved(self):
        reply = f"● Footer example:\n{self.hint}\nDo not restart this session."
        output = f"❯ explain the footer\n{reply}\n❯\n{self.hint}            {self.model}\n"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.extract_last_message_from_script(output) == reply


class TestCopilotCliProviderCommand:
    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._supports_flag")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._build_runtime_mcp_config"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.dict("os.environ", {}, clear=True)
    def test_command_builds_default(self, mock_tmux, mock_build_mcp, mock_supports_flag):
        mock_supports_flag.return_value = True
        mock_build_mcp.return_value = '{"mcpServers":{"cao-mcp-server":{"command":"x"}}}'
        mock_tmux.return_value.get_pane_working_directory.return_value = "/tmp/project"

        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        command = provider._command()
        parts = shlex.split(command)

        assert parts[0] == "copilot"
        assert "--allow-all" in parts
        assert "--model" not in parts
        assert "--config-dir" in parts
        assert "--add-dir" in parts
        assert parts[parts.index("--add-dir") + 1] == "/tmp/project"
        assert "--additional-mcp-config" in parts
        assert "--autopilot" in parts
        mock_build_mcp.assert_called_once_with()

    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._supports_flag")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._build_runtime_mcp_config"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.dict("os.environ", {}, clear=True)
    def test_empty_allowlist_emits_deny_tool(self, mock_tmux, mock_build_mcp, mock_supports_flag):
        """allowed_tools=[] must pass --deny-tool, not skip restrictions."""
        mock_supports_flag.return_value = True
        mock_build_mcp.return_value = '{"mcpServers":{"cao-mcp-server":{"command":"x"}}}'
        mock_tmux.return_value.get_pane_working_directory.return_value = "/tmp/project"

        provider = CopilotCliProvider("test1234", "test-session", "window-0", allowed_tools=[])
        parts = shlex.split(provider._command())

        assert "--deny-tool" in parts
        assert "shell" in parts

    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._supports_flag")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._build_runtime_mcp_config"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.dict("os.environ", {}, clear=True)
    def test_command_does_not_use_model_env(self, mock_tmux, mock_build_mcp, mock_supports_flag):
        mock_supports_flag.return_value = True
        mock_build_mcp.return_value = '{"mcpServers":{"cao-mcp-server":{"command":"x"}}}'
        mock_tmux.return_value.get_pane_working_directory.return_value = "/tmp/project"

        with patch.dict("os.environ", {"CAO_COPILOT_MODEL": "gpt-4.1"}, clear=False):
            provider = CopilotCliProvider("test1234", "test-session", "window-0")
            parts = shlex.split(provider._command())

        assert "--model" not in parts
        assert "--allow-all" in parts
        assert "--autopilot" in parts
        assert "--config-dir" in parts
        assert "--add-dir" in parts
        assert parts[parts.index("--add-dir") + 1] == "/tmp/project"
        assert "--additional-mcp-config" in parts
        mock_build_mcp.assert_called_once_with()

    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._supports_flag")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._build_runtime_mcp_config"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.dict("os.environ", {}, clear=True)
    def test_command_passes_agent_name_directly(
        self,
        mock_tmux,
        mock_build_mcp,
        mock_supports_flag,
    ):
        mock_supports_flag.return_value = True
        mock_build_mcp.return_value = '{"mcpServers":{"cao-mcp-server":{"command":"x"}}}'
        mock_tmux.return_value.get_pane_working_directory.return_value = "/tmp/project"

        provider = CopilotCliProvider(
            "test1234", "test-session", "window-0", agent_profile="repo-agent"
        )
        parts = shlex.split(provider._command())
        assert parts[parts.index("--agent") + 1] == "repo-agent"

    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._supports_flag")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._build_runtime_mcp_config"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.dict("os.environ", {}, clear=True)
    def test_command_skips_mcp_flag_if_unsupported(
        self,
        mock_tmux,
        mock_build_mcp,
        mock_supports_flag,
    ):
        mock_supports_flag.return_value = False
        mock_tmux.return_value.get_pane_working_directory.return_value = "/tmp/project"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        command = provider._command()
        assert "--additional-mcp-config" not in command
        mock_build_mcp.assert_not_called()

    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._supports_flag")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._build_runtime_mcp_config"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.dict("os.environ", {}, clear=True)
    def test_command_falls_back_to_process_cwd_when_pane_dir_missing(
        self,
        mock_tmux,
        mock_build_mcp,
        mock_supports_flag,
    ):
        mock_supports_flag.return_value = True
        mock_build_mcp.return_value = '{"mcpServers":{"cao-mcp-server":{"command":"x"}}}'
        mock_tmux.return_value.get_pane_working_directory.return_value = None

        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        parts = shlex.split(provider._command())

        assert "--add-dir" in parts
        assert parts[parts.index("--add-dir") + 1]


class TestCopilotCliProviderModelFlag:
    """Tests that the model kwarg is forwarded to Copilot CLI via --model.

    The Copilot provider takes the model value directly from the constructor
    (populated by terminal_service from the already-loaded AgentProfile) to
    avoid re-loading the profile.
    """

    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._supports_flag")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._build_runtime_mcp_config"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.dict("os.environ", {}, clear=True)
    def test_command_appends_model_when_set(self, mock_tmux, mock_build_mcp, mock_supports_flag):
        mock_supports_flag.return_value = True
        mock_build_mcp.return_value = '{"mcpServers":{"cao-mcp-server":{"command":"x"}}}'
        mock_tmux.return_value.get_pane_working_directory.return_value = "/tmp/project"

        provider = CopilotCliProvider(
            "test1234",
            "test-session",
            "window-0",
            agent_profile="repo-agent",
            model="claude-sonnet-4.5",
        )
        parts = shlex.split(provider._command())

        assert "--model" in parts
        assert parts[parts.index("--model") + 1] == "claude-sonnet-4.5"

    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._supports_flag")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._build_runtime_mcp_config"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.dict("os.environ", {}, clear=True)
    def test_command_omits_model_when_unset(self, mock_tmux, mock_build_mcp, mock_supports_flag):
        mock_supports_flag.return_value = True
        mock_build_mcp.return_value = '{"mcpServers":{"cao-mcp-server":{"command":"x"}}}'
        mock_tmux.return_value.get_pane_working_directory.return_value = "/tmp/project"

        provider = CopilotCliProvider(
            "test1234", "test-session", "window-0", agent_profile="repo-agent"
        )
        parts = shlex.split(provider._command())

        assert "--model" not in parts

    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._supports_flag")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._build_runtime_mcp_config"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.dict("os.environ", {}, clear=True)
    def test_command_omits_model_when_no_agent_profile(
        self, mock_tmux, mock_build_mcp, mock_supports_flag
    ):
        # --model is only meaningful alongside --agent; without an agent
        # profile the flag is not emitted even if a model is passed.
        mock_supports_flag.return_value = True
        mock_build_mcp.return_value = '{"mcpServers":{"cao-mcp-server":{"command":"x"}}}'
        mock_tmux.return_value.get_pane_working_directory.return_value = "/tmp/project"

        provider = CopilotCliProvider(
            "test1234", "test-session", "window-0", model="claude-sonnet-4.5"
        )
        parts = shlex.split(provider._command())

        assert "--model" not in parts


class TestCopilotCliProviderInitialization:
    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_for_shell")
    async def test_initialize_shell_timeout(self, mock_wait_shell):
        mock_wait_shell.return_value = False
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        with pytest.raises(TimeoutError, match="Shell initialization timed out"):
            await provider.initialize()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    @patch("cli_agent_orchestrator.providers.copilot_cli.asyncio.sleep")
    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.object(CopilotCliProvider, "_accept_trust_prompts")
    async def test_initialize_success(
        self,
        mock_accept,
        mock_tmux,
        mock_wait_shell,
        _mock_async_sleep,
        mock_status_monitor,
    ):
        """initialize() launches the CLI and returns once the terminal is IDLE."""
        mock_wait_shell.return_value = True
        mock_status_monitor.get_status.return_value = TerminalStatus.IDLE

        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        result = await provider.initialize()

        assert result is True
        assert provider._initialized is True
        # The CLI command is typed into the pane after the shell is ready.
        mock_tmux.return_value.send_keys.assert_called_once()
        mock_accept.assert_called_once()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    @patch("cli_agent_orchestrator.providers.copilot_cli.asyncio.sleep")
    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.object(CopilotCliProvider, "_accept_trust_prompts")
    async def test_initialize_handles_trust_prompt_then_idle(
        self,
        mock_accept,
        mock_tmux,
        mock_wait_shell,
        _mock_async_sleep,
        mock_status_monitor,
    ):
        """A WAITING_USER_ANSWER trust prompt is handled before reaching IDLE."""
        mock_wait_shell.return_value = True
        mock_status_monitor.get_status.side_effect = [
            TerminalStatus.WAITING_USER_ANSWER,
            TerminalStatus.IDLE,
        ]

        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        result = await provider.initialize()

        assert result is True
        # Initial trust handling + the in-loop WAITING_USER_ANSWER handling.
        assert mock_accept.call_count >= 2

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.object(CopilotCliProvider, "_accept_trust_prompts")
    async def test_initialize_polls_get_status_via_to_thread(
        self,
        mock_accept,
        mock_tmux,
        mock_wait_shell,
        mock_status_monitor,
    ):
        """#558: status_monitor.get_status() is no longer in-memory only -- for a
        PROCESSING terminal it can fork a real tmux capture-pane subprocess (the
        stale-PROCESSING fallback), and copilot init is exactly the regime that trips it
        (cached PROCESSING, pane quiet during auth/MCP boot). Pin the asyncio.to_thread
        dispatch of the init poll -- the other init tests mock the status monitor and
        cannot see HOW it was called."""
        mock_wait_shell.return_value = True
        mock_status_monitor.get_status.return_value = TerminalStatus.IDLE

        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        with patch(
            "cli_agent_orchestrator.providers.copilot_cli.asyncio.to_thread",
            wraps=asyncio.to_thread,
        ) as mock_to_thread:
            result = await provider.initialize()
            # initialize() also offloads _command and send_keys -- count only the
            # get_status dispatches.
            get_status_calls = [
                c
                for c in mock_to_thread.call_args_list
                if c.args[0] == mock_status_monitor.get_status
            ]

        assert result is True
        assert get_status_calls, "status_monitor.get_status was never dispatched via to_thread"
        assert all(c.args[1] == "test1234" for c in get_status_calls)

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.copilot_cli.logger")
    @patch("cli_agent_orchestrator.providers.copilot_cli.time.time")
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    @patch("cli_agent_orchestrator.providers.copilot_cli.asyncio.sleep")
    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.object(CopilotCliProvider, "_accept_trust_prompts")
    async def test_initialize_timeout_logs_unrecognized_row(
        self,
        _mock_accept,
        _mock_tmux,
        mock_wait_shell,
        _mock_async_sleep,
        mock_status_monitor,
        mock_time,
        mock_logger,
    ):
        mock_wait_shell.return_value = True
        mock_status_monitor.get_status.return_value = TerminalStatus.PROCESSING
        mock_time.side_effect = [0.0, 61.0]
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        with patch.object(provider, "_history", return_value="● Ready\n❯\n\nNEW ROW alpha\n"):
            with pytest.raises(TimeoutError, match="Copilot initialization timed out"):
                await provider.initialize()

        mock_logger.warning.assert_called_once_with(
            "Copilot idle prompt for %s:%s is followed by unrecognized row %r",
            "test-session",
            "window-0",
            "NEW ROW alpha",
        )

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.copilot_cli.logger")
    @patch("cli_agent_orchestrator.providers.copilot_cli.time.time")
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    @patch("cli_agent_orchestrator.providers.copilot_cli.asyncio.sleep")
    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.object(CopilotCliProvider, "_accept_trust_prompts")
    async def test_initialize_timeout_names_footer_fragment_from_narrow_pane(
        self,
        _mock_accept,
        _mock_tmux,
        mock_wait_shell,
        _mock_async_sleep,
        mock_status_monitor,
        mock_time,
        mock_logger,
    ):
        mock_wait_shell.return_value = True
        mock_status_monitor.get_status.return_value = TerminalStatus.PROCESSING
        mock_time.side_effect = [0.0, 61.0]
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        narrow_screen = (
            "❯\n"
            "← open · Autopilot · Allow · / commands · tab next\n"
            "sidebar              All                  tab\n"
        )

        with patch.object(provider, "_history", return_value=narrow_screen):
            with pytest.raises(TimeoutError, match="Copilot initialization timed out"):
                await provider.initialize()

        mock_logger.warning.assert_called_once_with(
            "Copilot idle prompt for %s:%s is followed by unrecognized row %r",
            "test-session",
            "window-0",
            "sidebar              All                  tab",
        )

    @pytest.mark.parametrize("screen", ["Loading environment...\n", "", "\n  \n"])
    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.copilot_cli.logger")
    @patch("cli_agent_orchestrator.providers.copilot_cli.time.time")
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    @patch("cli_agent_orchestrator.providers.copilot_cli.asyncio.sleep")
    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.object(CopilotCliProvider, "_accept_trust_prompts")
    async def test_initialize_timeout_logs_missing_prompt(
        self,
        _mock_accept,
        _mock_tmux,
        mock_wait_shell,
        _mock_async_sleep,
        mock_status_monitor,
        mock_time,
        mock_logger,
        screen,
    ):
        mock_wait_shell.return_value = True
        mock_status_monitor.get_status.return_value = TerminalStatus.PROCESSING
        mock_time.side_effect = [0.0, 61.0]
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        with patch.object(provider, "_history", return_value=screen):
            with pytest.raises(TimeoutError, match="Copilot initialization timed out"):
                await provider.initialize()

        mock_logger.warning.assert_called_once_with(
            "Copilot idle prompt not found for %s:%s", "test-session", "window-0"
        )

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.copilot_cli.logger")
    @patch("cli_agent_orchestrator.providers.copilot_cli.time.time")
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    @patch("cli_agent_orchestrator.providers.copilot_cli.asyncio.sleep")
    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.object(CopilotCliProvider, "_accept_trust_prompts")
    async def test_initialize_timeout_logs_recognized_prompt_that_did_not_settle(
        self,
        _mock_accept,
        _mock_tmux,
        mock_wait_shell,
        _mock_async_sleep,
        mock_status_monitor,
        mock_time,
        mock_logger,
    ):
        mock_wait_shell.return_value = True
        mock_status_monitor.get_status.return_value = TerminalStatus.PROCESSING
        mock_time.side_effect = [0.0, 61.0]
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        with patch.object(provider, "_history", return_value="● Ready\n❯\n"):
            with pytest.raises(TimeoutError, match="Copilot initialization timed out"):
                await provider.initialize()

        mock_logger.warning.assert_called_once_with(
            "Copilot idle prompt recognized for %s:%s but status did not settle to idle",
            "test-session",
            "window-0",
        )


class TestCopilotCliProviderTrustPrompts:
    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.copilot_cli.asyncio.sleep")
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    async def test_accept_trust_prompts_answers_yes_then_returns(self, mock_tmux, _mock_sleep):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        with (
            patch.object(
                provider,
                "_history",
                side_effect=[
                    "Do you trust all the actions in this folder? [y/N]",
                    "GitHub Copilot v0.0.415\n❯ Type @ to mention files",
                ],
            ),
            patch.object(provider, "_send_enter") as mock_enter,
        ):
            await provider._accept_trust_prompts(timeout=2.0)

        mock_tmux.return_value.send_special_key.assert_any_call("test-session", "window-0", "y")
        assert mock_enter.call_count >= 1

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.copilot_cli.logger")
    @patch("cli_agent_orchestrator.providers.copilot_cli.asyncio.sleep")
    @patch("cli_agent_orchestrator.providers.copilot_cli.time.time")
    async def test_accept_trust_prompts_logs_warning_on_timeout(
        self, mock_time, _mock_sleep, mock_logger
    ):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        mock_time.side_effect = [0.0, 0.0, 3.0]

        with patch.object(provider, "_history", return_value="still waiting"):
            await provider._accept_trust_prompts(timeout=2.0)

        mock_logger.warning.assert_called_once_with(
            "Trust prompt handler timed out for %s:%s",
            "test-session",
            "window-0",
        )

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.copilot_cli.asyncio.sleep")
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    async def test_accept_trust_prompts_answers_yes_for_spaced_yes_no_prompt(
        self, mock_tmux, _mock_sleep
    ):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        with (
            patch.object(
                provider,
                "_history",
                side_effect=[
                    "Do you trust all the actions in this folder? [y / N]",
                    "GitHub Copilot v0.0.415\n❯ Type @ to mention files",
                ],
            ),
            patch.object(provider, "_send_enter") as mock_enter,
        ):
            await provider._accept_trust_prompts(timeout=2.0)

        mock_tmux.return_value.send_special_key.assert_any_call("test-session", "window-0", "y")
        assert mock_enter.call_count >= 1

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.copilot_cli.logger")
    @patch("cli_agent_orchestrator.providers.copilot_cli.asyncio.sleep")
    async def test_accept_trust_prompts_returns_at_v1091_idle_prompt(
        self, _mock_sleep, mock_logger
    ):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        idle_screen = (
            "● MCP Servers reloaded: 2 servers connected\n"
            "\n"
            "❯\n"
            "\n"
            "← open sidebar · Autopilot · Allow All · / commands · tab next tab\n"
            "data_analyst · GitHub Copilot • GPT-5.6 Terra\n"
        )

        with (
            patch.object(provider, "_history", return_value=idle_screen) as mock_history,
            patch.object(provider, "_send_enter") as mock_enter,
        ):
            await provider._accept_trust_prompts(timeout=2.0)

        mock_history.assert_called_once()
        mock_enter.assert_not_called()
        mock_logger.warning.assert_not_called()


class TestCopilotCliProviderStatusDetection:
    @pytest.mark.parametrize(
        "busy_row",
        [
            "∙ Thinking (Esc to cancel)",
            "● Thinking (Esc to cancel)",
            "⠋ Working · esc interrupt · enqueue",
        ],
    )
    @pytest.mark.parametrize("composer", ["❯", "❯ unsent text"])
    @pytest.mark.parametrize("submitted_prompt", ["", "❯ do the task\n"])
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_busy_above_composer_stays_processing(
        self, mock_tmux, busy_row, composer, submitted_prompt
    ):
        mock_tmux.return_value.get_native_status.return_value = None
        rule = "─" * 100
        output = (
            f"{submitted_prompt}● Partial reply\n{busy_row}\n{rule}\n{composer}\n{rule}\n"
            "← open sidebar · Autopilot · Allow All · / commands · tab next tab\n"
            "data_analyst · Claude Sonnet 5\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        assert provider.get_status(output) == TerminalStatus.PROCESSING
        assert provider.probe_stale_processing_capture(output) == TerminalStatus.PROCESSING
        assert provider.commit_stale_processing_capture(output, TerminalStatus.IDLE) is False
        assert provider.commit_stale_processing_capture(output, TerminalStatus.COMPLETED) is False

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_assistant_prose_mentioning_cancel_remains_completed(self, mock_tmux):
        mock_tmux.return_value.get_native_status.return_value = None
        output = (
            "❯ explain the status text\n"
            "● The status text says Esc to cancel.\n"
            "────────\n❯\n────────\n"
            "← open sidebar · Autopilot · Allow All · / commands · tab next tab\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED
        assert provider.probe_stale_processing_capture(output) == TerminalStatus.COMPLETED

    @pytest.mark.parametrize(
        "busy_row", ["∙ Thinking (Esc to cancel)", "⠋ Working · esc interrupt · enqueue"]
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_old_busy_row_before_completed_reply_does_not_block_readiness(
        self, mock_tmux, busy_row
    ):
        mock_tmux.return_value.get_native_status.return_value = None
        output = (
            f"❯ do the task\n{busy_row}\n● Finished reply\n❯\n"
            "← open sidebar · Autopilot · Allow All · / commands · tab next tab\n"
            "data_analyst · Claude Sonnet 5\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        assert provider.get_status(output) == TerminalStatus.COMPLETED
        assert provider.probe_stale_processing_capture(output) == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_waiting_user_answer(self, mock_tmux):
        output = "confirm folder trust [y/n]"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_idle_with_no_user_message(self, mock_tmux):
        output = "GitHub Copilot v0.0.415\n❯ Type @ to mention files"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_idle_with_wrapped_prompt_helper(self, mock_tmux):
        output = (
            "● Environment loaded: 2 MCP servers, 3 agents\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯  Type @ to mention files, # for issues/PRs, / for commands, or ? for \n"
            "  shortcuts\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            " shift+tab switch mode                     Remaining reqs.: 99.66666666666667%\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_processing_when_no_idle_prompt(self, mock_tmux):
        output = "Working on edits..."
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_error_while_processing_if_error_after_user(self, mock_tmux):
        output = "❯ refactor this\nError: failed to parse"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_completed_when_response_present_and_idle(self, mock_tmux):
        output = "❯ refactor this\n● Edit file.py (+1 -1)\n❯ "
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_processing_when_spinner_visible_with_idle_prompt(self, mock_tmux):
        output = (
            "❯ Analyze the dataset and calculate standard \n"
            "  deviation.\n"
            "∙ Thinking (Esc to cancel)\n"
            " ~/repo [⎇ branch*] gpt-5-mini (medium) (0x) data_analyst\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯ Type @ to mention files, # for issues/PRs, / for commands, or ? for \n"
            "  shortcuts\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_error_when_idle_and_error_without_assistant_marker(self, mock_tmux):
        output = "❯ refactor this\nError: failed to parse\n❯ "
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_completed_when_idle_and_error_with_assistant_marker(self, mock_tmux):
        output = "❯ refactor this\nassistant: note\nError: sample\n❯ "
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    # ------------------------------------------------------------------
    # Copilot v1.0.31+ layout: bare ❯ followed by the status bar line
    # ------------------------------------------------------------------

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_idle_with_v1031_autopilot_status_bar(self, mock_tmux):
        """Bare ❯ + 'autopilot · / commands ...' status bar → IDLE (no prior user turn)."""
        output = (
            "● Selected custom agent: developer\n"
            "\n"
            "● Environment loaded: 3 custom instructions, 1 hook, 2 MCP servers\n"
            "\n"
            " ~/repo [⎇ main*%]\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            " autopilot · / commands \u200b                        Claude Sonnet 4.6 · (0%)\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_idle_with_v1031_plan_status_bar(self, mock_tmux):
        """Bare ❯ + 'plan · / commands ...' status bar → IDLE."""
        output = (
            " ~/repo [⎇ main]\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            " plan · / commands \u200b                             Claude Sonnet 4.6 · (0%)\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_idle_with_v1031_interactive_status_bar(self, mock_tmux):
        """Bare ❯ + 'interactive · / commands ...' status bar → IDLE."""
        output = (
            " ~/repo [⎇ main]\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            " interactive · / commands \u200b                      Claude Sonnet 4.6 · (0%)\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_completed_with_v1031_status_bar_after_user_turn(self, mock_tmux):
        """User turn + agent response + bare ❯ + status bar → COMPLETED."""
        output = (
            "❯ fix the bug\n"
            "● Edit src/main.py (+3 -1)\n"
            " ~/repo [⎇ main*%]\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            " autopilot · / commands \u200b                        Claude Sonnet 4.6 · (0%)\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_processing_with_spinner_and_v1031_status_bar(self, mock_tmux):
        """Spinner line present alongside status bar → still PROCESSING."""
        output = (
            "❯ refactor utils.py\n"
            "∙ Thinking (Esc to cancel)\n"
            " ~/repo [⎇ main*%]\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            " autopilot · / commands \u200b                        Claude Sonnet 4.6 · (0%)\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_idle_with_v1031_absolute_path_breadcrumb(self, mock_tmux):
        """Bare ❯ + absolute-path breadcrumb (CWD outside $HOME) → IDLE."""
        output = (
            " /tmp/pr184-e2e [⎇ pr-184]\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            " autopilot · / commands \u200b                        Claude Sonnet 4.6 · (0%)\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE

    # ------------------------------------------------------------------
    # Copilot v1.0.91 layout: bare ❯ followed by hint bar and agent/model bar
    # ------------------------------------------------------------------

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_idle_with_v1091_footer(self, mock_tmux):
        """Bare ❯ + hint bar + agent/model bar → IDLE (no prior user turn)."""
        output = (
            "Copilot v1.0.91 uses AI.\n"
            "\n"
            "● Selected custom agent: data_analyst\n"
            "\n"
            "● MCP Servers reloaded: 2 servers connected\n"
            "\n"
            "/Users/me/work\n"
            "\n"
            "❯\n"
            "\n"
            "← open sidebar · Autopilot · Allow All · / commands · tab next tab\n"
            "data_analyst · GitHub Copilot • GPT-5.6 Terra\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_completed_with_v1091_footer_after_user_turn(self, mock_tmux):
        """User turn + agent response + bare ❯ + v1.0.91 footer → COMPLETED."""
        output = (
            "❯ fix the bug\n"
            "● Edit src/main.py (+3 -1)\n"
            "\n"
            "❯\n"
            "\n"
            "← open sidebar · Autopilot · Allow All · / commands · tab next tab\n"
            "data_analyst · GitHub Copilot • GPT-5.6 Terra\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    @pytest.mark.parametrize(
        "busy_row", ["∙ Thinking (Esc to cancel)", "⠋ Working · esc interrupt · enqueue"]
    )
    @pytest.mark.parametrize("reply", ["", "● Partial reply\n"])
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_processing_with_spinner_and_v1091_footer(self, mock_tmux, busy_row, reply):
        output = (
            "❯ refactor utils.py\n"
            f"{reply}{busy_row}\n"
            "\n"
            "❯\n"
            "\n"
            "← open sidebar · Autopilot · Allow All · / commands · tab next tab\n"
            "data_analyst · GitHub Copilot • GPT-5.6 Terra\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        mock_tmux.return_value.get_native_status.return_value = None
        assert provider.get_status(output) == TerminalStatus.PROCESSING
        assert provider.probe_stale_processing_capture(output) == TerminalStatus.PROCESSING
        assert provider.commit_stale_processing_capture(output, TerminalStatus.COMPLETED) is False
        if reply:
            assert provider.extract_last_message_from_script(output) == reply.strip()
        else:
            with pytest.raises(ValueError, match="No provider response content found"):
                provider.extract_last_message_from_script(output)


class TestCopilotCliProviderMessageExtraction:
    def test_extract_last_message_from_post_user_lines(self):
        output = "❯ refactor\n● Edit x.py (+2 -1)\nDONE\n❯ "
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)
        assert "Edit x.py" in message
        assert "DONE" in message

    def test_extract_last_message_from_assistant_fallback(self):
        output = "assistant: Completed task successfully."
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.extract_last_message_from_script(output) == "Completed task successfully."

    def test_extract_last_message_raises_when_not_found(self):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        with pytest.raises(ValueError, match="No provider response content found"):
            provider.extract_last_message_from_script("just ui chrome")

    def test_extract_last_message_ignores_processing_spinner_tail(self):
        output = (
            "❯ Analyze the dataset and calculate standard \n"
            "  deviation.\n"
            "∙ Thinking (Esc to cancel)\n"
            " ~/repo [⎇ branch*] gpt-5-mini (medium) (0x) data_analyst\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯ Type @ to mention files, # for issues/PRs, / for commands, or ? for \n"
            "  shortcuts\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        with pytest.raises(ValueError, match="No provider response content found"):
            provider.extract_last_message_from_script(output)

    def test_extract_last_message_excludes_v1091_footer(self):
        output = (
            "❯ fix the bug\n"
            "● Fixed the off-by-one in main.py\n"
            "\n"
            "❯\n"
            "\n"
            "← open sidebar · Autopilot · Allow All · / commands · tab next tab\n"
            "data_analyst · GitHub Copilot • GPT-5.6 Terra\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.extract_last_message_from_script(output) == (
            "● Fixed the off-by-one in main.py"
        )

    def test_extract_last_message_keeps_reply_lines_resembling_v1091_footer(self):
        output = (
            "❯ explain\n"
            "● Here is the plan\n"
            "Use the menu · / commands are listed\n"
            "GitHub Copilot · supports MCP\n"
            "Done.\n"
            "\n"
            "❯\n"
            "\n"
            "← open sidebar · Autopilot · Allow All · / commands · tab next tab\n"
            "data_analyst · GitHub Copilot • GPT-5.6 Terra\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.extract_last_message_from_script(output) == (
            "● Here is the plan\n"
            "Use the menu · / commands are listed\n"
            "GitHub Copilot · supports MCP\n"
            "Done."
        )


class TestCopilotCliProviderMisc:
    @pytest.mark.parametrize(
        "reply",
        [
            "vscode · GitHub Copilot • AI assistant",
            "GitHub Copilot • GPT-5.6 Terra",
            "Claude Sonnet 5",
        ],
    )
    @pytest.mark.parametrize("trailing_prompt", ["", "\n❯\n"])
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_model_like_reply_without_hint_context_is_preserved(
        self, mock_tmux, reply, trailing_prompt
    ):
        mock_tmux.return_value.get_native_status.return_value = None
        output = f"❯ who are you\n{reply}{trailing_prompt}"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        expected = TerminalStatus.COMPLETED if trailing_prompt else TerminalStatus.PROCESSING

        assert provider.get_status(output) == expected
        assert provider.probe_stale_processing_capture(output) == expected
        assert provider.extract_last_message_from_script(output) == reply

    @pytest.mark.parametrize("prior_turn", ["", "❯ previous task\n● Previous reply\n"])
    @pytest.mark.parametrize(
        "footer",
        [
            pytest.param(
                "← open sidebar · Autopilot · Allow All · / commands · tab next tab"
                "            data_analyst · Claude Sonnet 5\n",
                id="wide",
            ),
            pytest.param(
                "← open sidebar · Autopilot · Allow All · / commands · tab next tab\n"
                "data_analyst · Claude Sonnet 5\n",
                id="separate-model-row",
            ),
            pytest.param(
                "autopilot · / commands    Claude Sonnet 4.6 · (0%)\n",
                id="old-status-bar",
            ),
        ],
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_unsent_composer_text_is_idle_without_response(self, mock_tmux, footer, prior_turn):
        mock_tmux.return_value.get_native_status.return_value = None
        rule = "─" * 100
        output = f"{prior_turn}{rule}\n❯ summarize the repo\n{rule}\n{footer}"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        assert provider.get_status(output) == TerminalStatus.IDLE
        assert provider.probe_stale_processing_capture(output) == TerminalStatus.IDLE
        assert provider.commit_stale_processing_capture(output, TerminalStatus.IDLE) is True
        assert provider.commit_stale_processing_capture(output, TerminalStatus.COMPLETED) is False
        with pytest.raises(ValueError, match="No provider response content found"):
            provider.extract_last_message_from_script(output)

    def test_opts_into_stale_capture_but_not_direct_status_probe(self):
        assert CopilotCliProvider.supports_stale_processing_capture is True
        assert CopilotCliProvider.supports_direct_status_probe is False

    def test_stale_capture_probe_classifies_without_side_effects(self):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        output = "❯\n← open sidebar · Autopilot · / commands · tab next tab\n"
        assert provider.probe_stale_processing_capture(output) == TerminalStatus.IDLE
        assert provider.commit_stale_processing_capture(output, TerminalStatus.IDLE) is True

    @pytest.mark.parametrize("snapshot", ["", " \n  \n", "\x1b[2J\x1b[H\n"])
    def test_blank_stale_capture_never_reads_history(self, snapshot):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        old_ready_transcript = (
            "❯ previous task\n● Previous reply\n❯\n"
            "← open sidebar · Autopilot · / commands · tab next tab\n"
        )
        with (
            patch.object(provider, "_history", return_value=old_ready_transcript) as history,
            patch.object(provider, "_resolve_native_status", return_value=None),
        ):
            assert provider.probe_stale_processing_capture(snapshot) == TerminalStatus.UNKNOWN
            assert provider.probe_stale_processing_capture(snapshot) == TerminalStatus.UNKNOWN
            assert provider.commit_stale_processing_capture(snapshot, TerminalStatus.IDLE) is False
            assert (
                provider.commit_stale_processing_capture(snapshot, TerminalStatus.COMPLETED)
                is False
            )
            history.assert_not_called()

    def test_extract_last_message_keeps_sole_reply_resembling_agent_model_bar(self):
        output = (
            "❯ who are you\n"
            "vscode · GitHub Copilot · AI assistant\n"
            "\n"
            "❯\n"
            "← open sidebar · Autopilot · / commands · tab next tab\n"
            "data_analyst · GitHub Copilot • GPT-5.6 Terra\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.extract_last_message_from_script(output) == (
            "vscode · GitHub Copilot · AI assistant"
        )

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_idle_with_model_row_lacking_agent_prefix(self, mock_tmux):
        rule = "─" * 120
        output = (
            f"{rule}\n❯\n{rule}\n"
            " ← open sidebar · Autopilot · Allow All · / commands · tab next tab"
            "            GitHub Copilot • Claude Sonnet 5\n"
        )
        mock_tmux.return_value.get_native_status.return_value = None
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_processing_when_hint_segments_wrap_in_narrow_pane(self, mock_tmux):
        """Below ~69 columns Copilot wraps each hint segment inside its own column, so
        the rows interleave. They are deliberately left unrecognized: not ready."""
        rule = "─" * 50
        output = (
            f"{rule}\n❯\n{rule}\n"
            "← open · Autopilot · Allow · / commands · tab next\n"
            "sidebar              All                  tab\n"
            "data_analyst · Claude Sonnet 5\n"
        )
        mock_tmux.return_value.get_native_status.return_value = None
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    @pytest.mark.parametrize(
        "agent_model_row",
        [
            "data_analyst · Claude Sonnet 5",
            "Claude Sonnet 5",
            "GitHub Copilot • GPT-5.6 Terra",
            "data_analyst · GitHub Copilot • AI assistant",
        ],
    )
    @pytest.mark.parametrize(
        "hint_row",
        [
            "← open sidebar · Autopilot · Allow All · / commands · tab next tab",
            "Autopilot · Allow All · / commands · tab next tab",
        ],
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_idle_with_agent_model_row_below_hint_row(
        self, mock_tmux, hint_row, agent_model_row
    ):
        """Below ~115 columns the agent/model box moves to its own last row. It may
        carry no provider label, and the hint row lacks "← open sidebar" when
        Copilot's sidebar feature is off."""
        rule = "─" * 100
        output = f"{rule}\n❯\n{rule}\n {hint_row}\n {agent_model_row}\n"
        mock_tmux.return_value.get_native_status.return_value = None
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE

    @pytest.mark.parametrize(
        "footer_rows",
        [
            pytest.param(["{row}", "{hint}", "{model}"], id="above-footer"),
            pytest.param(["{hint}", "{row}", "{model}"], id="between-hint-and-model"),
            pytest.param(["{hint}", "{model}", "{row}"], id="below-two-row-footer"),
            pytest.param(["{wide}", "{row}"], id="below-one-row-footer"),
            pytest.param(["{hint}   {row}", "{model}"], id="on-hint-row"),
        ],
    )
    @pytest.mark.parametrize(
        "row, expected",
        [
            ("∙ Thinking (Esc to cancel)", TerminalStatus.PROCESSING),
            ("⠋ Working · esc interrupt · enqueue", TerminalStatus.PROCESSING),
            ("Allow this tool to run? [y/n]", TerminalStatus.WAITING_USER_ANSWER),
        ],
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_not_ready_with_busy_or_waiting_row_near_footer(
        self, mock_tmux, footer_rows, row, expected
    ):
        hint = "← open sidebar · Autopilot · Allow All · / commands · tab next tab"
        model = "data_analyst · Claude Sonnet 5"
        rule = "─" * 120
        rows = [
            template.format(row=row, hint=hint, model=model, wide=f"{hint}            {model}")
            for template in footer_rows
        ]
        output = "❯ do the thing\n● Working on it\n" + f"{rule}\n❯\n{rule}\n" + "\n".join(rows)
        mock_tmux.return_value.get_native_status.return_value = None
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == expected

    @pytest.mark.parametrize(
        "footer_rows",
        [
            pytest.param(
                [
                    "← open sidebar · Autopilot · Allow All · / commands · tab next tab",
                    "Queued",
                    "data_analyst · GitHub Copilot • GPT-5.6 Terra",
                ],
                id="unknown-row-before-agent-model-row",
            ),
            pytest.param(
                [
                    "← open sidebar · Autopilot · Allow All · / commands · tab next tab"
                    "            data_analyst · Claude Sonnet 5",
                    "NEW ROW alpha",
                ],
                id="unknown-row-below-one-row-footer",
            ),
            pytest.param(["← Back to chat"], id="arrow-row-that-is-not-the-hint-row"),
        ],
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_processing_with_unrecognized_row_near_footer(self, mock_tmux, footer_rows):
        rule = "─" * 120
        output = (
            "❯ do the thing\n● Working on it\n" + f"{rule}\n❯\n{rule}\n" + "\n".join(footer_rows)
        )
        mock_tmux.return_value.get_native_status.return_value = None
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    @pytest.mark.parametrize("row", ["Queued", "Waiting", "Compacting context"])
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_processing_with_versionless_row_below_hint_row(self, mock_tmux, row):
        hint = "← open sidebar · Autopilot · Allow All · / commands · tab next tab"
        rule = "─" * 100
        output = f"❯ do the thing\n● Working on it\n{rule}\n❯\n{rule}\n{hint}\n{row}\n"
        mock_tmux.return_value.get_native_status.return_value = None
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_extract_last_message_without_trailing_prompt_is_not_duplicated(self):
        output = "❯ summarize\n● First point\nSecond line\n"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.extract_last_message_from_script(output) == "● First point\nSecond line"

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_idle_on_v1091_capture_pane_with_shared_footer_row(self, mock_tmux):
        """Real 1.0.91 capture-pane shape: separator rules around ❯ and, in a wide
        pane, the hint bar and the agent/model bar rendered on one row."""
        rule = "─" * 120
        output = (
            " ● Selected custom agent: data_analyst\n"
            " /tmp/repo [⎇ main*]\n"
            f"{rule}\n"
            "❯\n"
            f"{rule}\n"
            " ← open sidebar · Autopilot · Allow All · / commands · tab next tab"
            "            data_analyst · Claude Sonnet 5\n"
        )
        mock_tmux.return_value.get_native_status.return_value = None
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_send_enter_uses_tmux_client(self, mock_tmux):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        provider._send_enter()
        mock_tmux.return_value.send_special_key.assert_called_once_with(
            "test-session", "window-0", "Enter"
        )

    def test_get_idle_pattern_for_log(self):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert "type @ to mention files" in provider.get_idle_pattern_for_log().lower()

    def test_exit_cli(self):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.exit_cli() == "/exit"

    def test_build_runtime_mcp_config_includes_terminal_id(self):
        provider = CopilotCliProvider("abc12345", "test-session", "window-0")
        runtime_cfg = json.loads(provider._build_runtime_mcp_config())
        assert "cao-mcp-server" in runtime_cfg["mcpServers"]
        assert runtime_cfg["mcpServers"]["cao-mcp-server"]["env"]["CAO_TERMINAL_ID"] == "abc12345"

    def test_build_runtime_mcp_config_resolves_bundled_command(self):
        """The bundled cao-mcp-server is resolved to a PATH-independent
        invocation (wiring guard: a refactor that drops the
        resolve_cao_mcp_command call must fail this test)."""
        provider = CopilotCliProvider("abc12345", "test-session", "window-0")
        MOD = "cli_agent_orchestrator.utils.mcp_resolution"
        with (
            patch(f"{MOD}._sibling_script", return_value="/venv/bin/cao-mcp-server"),
            patch(f"{MOD}.shutil.which", return_value=None),
        ):
            runtime_cfg = json.loads(provider._build_runtime_mcp_config())
        assert runtime_cfg["mcpServers"]["cao-mcp-server"]["command"] == "/venv/bin/cao-mcp-server"

    def test_cleanup_resets_initialized_state(self):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        provider._initialized = True
        provider.cleanup()
        assert provider._initialized is False


class TestCopilotCliServerSettings:
    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_server_settings")
    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_for_shell")
    async def test_initialize_uses_provider_init_timeout(self, mock_wait_shell, mock_settings):
        """initialize() reads provider_init_timeout from server settings."""
        mock_settings.return_value = {
            "mcp_request_timeout": 120,
            "event_bus_max_queue_size": 8192,
            "provider_init_timeout": 45,
            "startup_prompt_handler_timeout": 5,
        }
        mock_wait_shell.return_value = False
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        with pytest.raises(TimeoutError):
            await provider.initialize()

        mock_wait_shell.assert_called_once_with("test1234", timeout=45)


class TestCopilotInitializationAfterTrustRedraw:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("layout", ["prompt", "box"])
    async def test_initialize_recovers_stale_waiting_after_real_trust_handler(self, layout):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        from cli_agent_orchestrator.services.status_monitor import StatusMonitor

        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        sm = StatusMonitor()
        backend = MagicMock()
        backend.supports_event_inbox.return_value = False
        backend.get_native_status.return_value = None
        trust = "Confirm folder trust\n1. Yes\n2. Yes, and remember\n"
        hint = "← open sidebar · Autopilot · Allow All · / commands · tab next tab"
        composer = "❯\n" if layout == "prompt" else "╻▄▄▄▄▄▄▄▄\n┃\n╹▀▀▀▀▀▀▀▀\n"
        pane = {"text": trust}
        clock = {"now": 1000.0}
        fake_time = SimpleNamespace(time=lambda: clock["now"], monotonic=lambda: clock["now"])

        async def advance(seconds):
            clock["now"] += seconds

        def accept_trust(session, window, key):
            assert key == "Enter"
            pane["text"] = composer + hint
            # The rendered pane is ready, but its FIFO redraw is cursor-addressed.
            sm._process_chunk(provider.terminal_id, "\x1b[2J\x1b[H❯\x1b[2;1H" + hint)

        backend.get_history.side_effect = lambda *args, **kwargs: pane["text"]
        backend.send_special_key.side_effect = accept_trust
        with (
            patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
            patch("cli_agent_orchestrator.providers.copilot_cli.get_backend", return_value=backend),
            patch(
                "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
                return_value=provider,
            ),
            patch("cli_agent_orchestrator.services.status_monitor.status_monitor", sm),
            patch(
                "cli_agent_orchestrator.services.status_monitor.get_server_settings",
                return_value={"state_buffer_max": 32768},
            ),
            patch(
                "cli_agent_orchestrator.providers.copilot_cli.wait_for_shell",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "cli_agent_orchestrator.providers.copilot_cli.get_server_settings",
                return_value={"provider_init_timeout": 30},
            ),
            patch("cli_agent_orchestrator.providers.copilot_cli.time", fake_time),
            patch("cli_agent_orchestrator.services.status_monitor.time", fake_time),
            patch(
                "cli_agent_orchestrator.providers.copilot_cli.asyncio.sleep",
                new=AsyncMock(side_effect=advance),
            ),
            patch.object(provider, "_command", return_value="copilot"),
            patch.object(sm, "_running_loop", return_value=None),
        ):
            sm._process_chunk(provider.terminal_id, trust)
            assert sm.get_status(provider.terminal_id) == TerminalStatus.WAITING_USER_ANSWER
            assert await provider.initialize() is True
            assert sm.get_status(provider.terminal_id) == TerminalStatus.IDLE
        assert provider._initialized is True
        backend.send_special_key.assert_called_once_with("test-session", "window-0", "Enter")
        captures = [
            call for call in backend.get_history.call_args_list if call.kwargs.get("visible_only")
        ]
        assert len(captures) == 2
        assert clock["now"] < 1060


class TestCopilotCompletionChromeExtraction:
    hint = "← open sidebar · Autopilot · Allow All · / commands · tab next tab"

    @classmethod
    def screen(cls, reply, *, layout="prompt", summary="", cwd="/tmp/example"):
        rule = "─" * 80
        composer = f"{rule}\n❯\n{rule}\n" if layout == "prompt" else "╻▄▄▄▄▄▄▄▄\n┃\n╹▀▀▀▀▀▀▀▀\n"
        return (
            "❯ return the readiness token\n"
            + reply
            + "\n\n"
            + summary
            + f"\n\n {cwd}            Session: 0.51 AIC used\n"
            + composer
            + cls.hint
            + "\n"
        )

    @pytest.mark.parametrize("layout", ["prompt", "box"])
    @pytest.mark.parametrize("ansi", [False, True])
    def test_trailing_task_complete_finding_is_preserved(self, layout, ansi):
        reply = (
            "● Review results:\n"
            " ● Task complete\n"
            "   └ Blocking finding: candidate accepts age 18 incorrectly."
        )
        output = self.screen(reply, layout=layout)
        if ansi:
            output = "\x1b[32m" + output + "\x1b[0m"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.extract_last_message_from_script(output) == reply

    @pytest.mark.parametrize("layout", ["prompt", "box"])
    @pytest.mark.parametrize("cwd", ["/tmp/example", "~/example", "/tmp/example with spaces"])
    @pytest.mark.parametrize("wrapped", [False, True])
    @pytest.mark.parametrize("ansi", [False, True])
    def test_completion_summary_is_preserved_and_cwd_excluded(self, layout, cwd, wrapped, ansi):
        reply = "● CAO_READY_V1095"
        summary = " ● Task complete\n   └ Returned the readiness token.\n"
        if wrapped:
            summary += "     No other work was performed.\n"
        output = self.screen(reply, layout=layout, summary=summary, cwd=cwd)
        if ansi:
            output = "\x1b[32m" + output + "\x1b[0m"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.extract_last_message_from_script(output) == reply + "\n" + summary.rstrip()
        with patch.object(provider, "_resolve_native_status", return_value=None):
            assert provider.get_status(output) == TerminalStatus.COMPLETED
            assert provider.probe_stale_processing_capture(output) == TerminalStatus.COMPLETED

    @pytest.mark.parametrize(
        "reply",
        [
            "● Task complete",
            "● Task complete\n   └ This is the requested answer.",
            "● Explain the UI:\n ● Task complete\n   └ Example summary.\nMore response content.",
            "● Example:\n /tmp/example            Session: 0.51 AIC used\nThis is response content.",
            "● /tmp/example            Session: 0.51 AIC used",
            "● Answer\n ● Task complete\n   └ A detail\nAn unindented continuation.",
        ],
    )
    def test_response_content_resembling_chrome_is_preserved(self, reply):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.extract_last_message_from_script(self.screen(reply)) == reply

    def test_cwd_row_is_excluded_without_a_summary(self):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.extract_last_message_from_script(self.screen("● Answer")) == "● Answer"

    @pytest.mark.parametrize("layout", ["prompt", "box"])
    def test_paragraph_ending_in_session_shaped_finding_is_preserved(self, layout):
        reply = "● Review results:\n\n /tmp/candidate            Session: 0.51 AIC used"
        rule = "─" * 80
        composer = (
            f"{rule}\n❯\n{rule}\n"
            if layout == "prompt"
            else "╻" + "▄" * 79 + "\n┃\n╹" + "▀" * 79 + "\n"
        )
        output = "❯ review\n" + reply + "\n" + composer + self.hint
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.extract_last_message_from_script(output).splitlines() == [
            "● Review results:",
            " /tmp/candidate            Session: 0.51 AIC used",
        ]

    def test_only_the_cwd_row_next_to_composer_is_removed(self):
        reply = "● Directory example:\n /tmp/example            Session: 0.51 AIC used"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.extract_last_message_from_script(self.screen(reply)) == reply

    @pytest.mark.parametrize("layout", ["prompt", "box"])
    def test_final_answer_row_resembling_cwd_is_preserved_without_separation(self, layout):
        reply = "● Finding: the stale cwd row was rendered as\n /tmp/example            Session: 0.51 AIC used"
        composer = "❯\n" if layout == "prompt" else "╻▄▄▄▄▄▄▄▄\n┃\n╹▀▀▀▀▀▀▀▀\n"
        output = "❯ inspect\n" + reply + "\n" + composer + self.hint
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.extract_last_message_from_script(output) == reply

    @pytest.mark.parametrize("layout", ["prompt", "box"])
    def test_leading_thought_heading_is_excluded_with_its_ui_block(self, layout):
        reply = " ⌄ Thought for 1s\n │ Internal note.\n\n ● CAO_READY_V1095"
        summary = " ● Task complete\n   └ Returned the readiness token.\n"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert (
            provider.extract_last_message_from_script(
                self.screen(reply, layout=layout, summary=summary)
            )
            == "● CAO_READY_V1095\n" + summary.rstrip()
        )

    def test_thought_heading_mentioned_in_a_response_is_preserved(self):
        reply = "● UI labels:\n ⌄ Thought for 1s\nThis label reports elapsed time."
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.extract_last_message_from_script(self.screen(reply)) == reply

    def test_unanchored_summary_is_preserved(self):
        output = "❯ explain\n● Answer\n ● Task complete\n   └ Keep this text.\n"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert (
            provider.extract_last_message_from_script(output)
            == "● Answer\n ● Task complete\n   └ Keep this text."
        )
