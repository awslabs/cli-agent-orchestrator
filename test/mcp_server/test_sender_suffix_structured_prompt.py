"""Sender-ID suffix must not land inside an ``@<prompt> [args]`` invocation.

Issue #70 follow-up. kiro-cli treats any input whose first character is ``@``
as an MCP prompt invocation: it strips the ``@``, ``shlex``-splits the WHOLE
remaining input (newlines are whitespace), takes the first token as the prompt
name and zips every remaining token positionally onto the prompt's declared
arguments. There is no terminator, so a suffix appended anywhere after the
invocation is parsed as parameter values -- the maintainer measured
``thoroughness: [Assigned`` for ``@agent-sop:code-analysis query="..."``.

These tests pin the CAO-side contract: ``_assign_impl`` and
``_send_message_impl`` deliver an ``@``-prefixed message byte-for-byte, and
both compose their suffix through the single shared helper so a third call
site cannot reintroduce the defect.
"""

import ast
import inspect
import os
import shlex
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.utils import orchestration

SOP_INVOCATION = '@agent-sop:code-analysis query="What is the project structure?"'
# Declared arguments of the ``code-analysis`` SOP from the issue thread.
SOP_SCHEMA = ["query", "thoroughness"]


def _kiro_prompt_arguments(text: str) -> dict:
    """Model of kiro-cli's ``@`` dispatch (amazon-q-developer-cli
    ``chat/mod.rs`` + ``tool_manager.rs::process_prompt_arguments``).

    Positional: the ``name=`` prefix stays inside the first token, which is
    why the maintainer's capture read ``task: query=What is ...``. The
    defect is the SECOND key appearing at all."""
    assert text.startswith("@")
    tokens = shlex.split(text[1:])
    return dict(zip(SOP_SCHEMA, tokens[1:]))


class TestSuffixHelper:
    """Direct contract of the shared helper."""

    @pytest.mark.parametrize(
        "message",
        [
            SOP_INVOCATION,
            "@codebase-summary",
            "@agent-sop:pdd task_description='ship it' mode=fsc",
            "  @agent-sop:x leading whitespace still dispatches as a prompt",
        ],
    )
    def test_structured_prompt_invocation_detected(self, message):
        assert orchestration.is_structured_prompt_invocation(message) is True

    @pytest.mark.parametrize(
        "message",
        [
            "Analyze the logs",
            "Review @src/main.py and report",  # an @-mention mid-prose is not an invocation
            "email bob@example.com",
            "",
        ],
    )
    def test_prose_is_not_structured_prompt_invocation(self, message):
        assert orchestration.is_structured_prompt_invocation(message) is False

    def test_prose_gets_suffix_on_its_own_paragraph(self):
        out = orchestration.append_sender_suffix(
            "Analyze the logs", orchestration.ASSIGN_SUFFIX_TEMPLATE, "a1b2c3d4"
        )
        assert out == (
            "Analyze the logs\n\n[Assigned by terminal a1b2c3d4. "
            "When done, send results back to terminal a1b2c3d4 using send_message]"
        )

    def test_structured_invocation_is_returned_unchanged(self):
        out = orchestration.append_sender_suffix(
            SOP_INVOCATION, orchestration.ASSIGN_SUFFIX_TEMPLATE, "a1b2c3d4"
        )
        assert out == SOP_INVOCATION


class TestAssignLeavesStructuredPromptIntact:
    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration.ENABLE_SENDER_ID_INJECTION", True)
    @patch("cli_agent_orchestrator.utils.orchestration._create_terminal")
    def test_assign_delivers_sop_invocation_byte_for_byte(self, mock_create, _nudge):
        mock_create.return_value = ("worker-1", "kiro_cli")

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            result = orchestration._assign_impl("developer", SOP_INVOCATION)

        assert result["success"] is True
        sent = mock_create.call_args.kwargs["initial_message"]
        assert sent == SOP_INVOCATION
        # What the SOP would actually receive: the optional parameter stays unset.
        assert _kiro_prompt_arguments(sent) == {"query": "query=What is the project structure?"}

    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration.ENABLE_SENDER_ID_INJECTION", True)
    @patch("cli_agent_orchestrator.utils.orchestration._create_terminal")
    def test_assign_still_suffixes_prose(self, mock_create, _nudge):
        """The guard is narrow: ordinary task prose keeps the reminder."""
        mock_create.return_value = ("worker-1", "kiro_cli")

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            orchestration._assign_impl("developer", "Analyze the logs")

        sent = mock_create.call_args.kwargs["initial_message"]
        assert sent.startswith("Analyze the logs\n\n[Assigned by terminal a1b2c3d4")


class TestSendMessageLeavesStructuredPromptIntact:
    @patch("cli_agent_orchestrator.utils.orchestration._target_binding_denial", return_value=None)
    @patch("cli_agent_orchestrator.utils.orchestration.ENABLE_SENDER_ID_INJECTION", True)
    @patch("cli_agent_orchestrator.utils.orchestration._send_to_inbox")
    def test_send_message_delivers_sop_invocation_byte_for_byte(self, mock_inbox, _unbound):
        mock_inbox.return_value = {"success": True}

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "deadbeef"}):
            orchestration._send_message_impl("receiver-123", SOP_INVOCATION)

        sent = mock_inbox.call_args[0][1]
        assert sent == SOP_INVOCATION
        assert _kiro_prompt_arguments(sent) == {"query": "query=What is the project structure?"}

    @patch("cli_agent_orchestrator.utils.orchestration._target_binding_denial", return_value=None)
    @patch("cli_agent_orchestrator.utils.orchestration.ENABLE_SENDER_ID_INJECTION", True)
    @patch("cli_agent_orchestrator.utils.orchestration._send_to_inbox")
    def test_send_message_still_suffixes_prose(self, mock_inbox, _unbound):
        mock_inbox.return_value = {"success": True}

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "deadbeef"}):
            orchestration._send_message_impl("receiver-123", "Here are the results")

        sent = mock_inbox.call_args[0][1]
        assert sent.startswith("Here are the results\n\n[Message from terminal deadbeef")


class TestSuffixHasOneOwner:
    """Structural guard for the class: every sender-ID suffix in orchestration.py
    is built by ``append_sender_suffix``; the literals live only in the two
    module-level templates; and both tool implementations call the helper."""

    _LITERALS = ("[Assigned by terminal", "[Message from terminal")
    _TEMPLATES = {"ASSIGN_SUFFIX_TEMPLATE", "MESSAGE_SUFFIX_TEMPLATE"}

    @pytest.fixture(scope="class")
    def tree(self):
        return ast.parse(inspect.getsource(orchestration))

    def test_suffix_literals_live_only_in_templates(self, tree):
        owners = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                for literal in self._LITERALS:
                    if literal in value.value:
                        for target in node.targets:
                            owners[literal] = getattr(target, "id", None)
        assert set(owners.values()) == self._TEMPLATES, owners

        # And no other string constant anywhere in the module carries them.
        strays = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and any(lit in node.value for lit in self._LITERALS)
        ]
        assert len(strays) == len(self._LITERALS), strays

    @pytest.mark.parametrize("impl", ["_assign_impl", "_send_message_impl"])
    def test_each_call_site_goes_through_helper(self, tree, impl):
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == impl)
        calls = {
            n.func.id
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "append_sender_suffix" in calls, f"{impl} composes its suffix inline"
