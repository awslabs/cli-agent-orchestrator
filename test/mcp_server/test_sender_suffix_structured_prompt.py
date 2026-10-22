"""Sender-ID suffix must not land inside a kiro-cli ``@<prompt> [args]``
invocation, and must still reach every other provider.

Issue #70 follow-up, revised after the review on PR #1028. kiro-cli treats any
input whose first character is ``@`` as an MCP prompt invocation: it strips
the ``@``, ``shlex``-splits the WHOLE remaining input (newlines are
whitespace), takes the first token as the prompt name and zips every remaining
token positionally onto the prompt's declared arguments. There is no
terminator, so a suffix appended anywhere after the invocation is parsed as
parameter values -- the maintainer measured ``thoroughness: [Assigned`` for
``@agent-sop:code-analysis query="..."``.

That is a property of the RECEIVING terminal's CLI, not of the text. Claude
Code, Codex and the other providers read a leading ``@path`` as a file mention
in prose and need the callback instruction exactly as much as any other task,
so the guard is keyed on the receiver's provider at every send site.

These tests pin the CAO-side contract: ``_assign_impl`` (local and remote) and
``_send_message_impl`` deliver an ``@``-prefixed message byte-for-byte to a
kiro_cli receiver and WITH the suffix to every other provider; the provider is
looked up only when the text leads with ``@``; and every suffix is composed
through the single shared helper with an explicit ``provider=`` so a third
call site cannot reintroduce either defect.
"""

import ast
import inspect
import os
import shlex
from unittest.mock import MagicMock, Mock, patch

import pytest
import requests

from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.utils import orchestration
from cli_agent_orchestrator.utils.orchestration import EphemeralClaim, HandoffContext

_ORCH = "cli_agent_orchestrator.utils.orchestration"

SOP_INVOCATION = '@agent-sop:code-analysis query="What is the project structure?"'
# Declared arguments of the ``code-analysis`` SOP from the issue thread.
SOP_SCHEMA = ["query", "thoroughness"]
# The reviewer's probe: an ``@path`` file mention that is ordinary prose to
# every provider except kiro-cli.
AT_PATH_TASK = "@src/app.py has a null check bug, fix it"

KIRO = ProviderType.KIRO_CLI.value
NON_KIRO = sorted(
    p.value for p in ProviderType if p.value not in orchestration.STRUCTURED_PROMPT_PROVIDERS
)


def _kiro_prompt_arguments(text: str) -> dict:
    """Model of kiro-cli's ``@`` dispatch (amazon-q-developer-cli
    ``chat/mod.rs`` + ``tool_manager.rs::process_prompt_arguments``).

    Positional: the ``name=`` prefix stays inside the first token, which is
    why the maintainer's capture read ``task: query=What is ...``. The
    defect is the SECOND key appearing at all."""
    assert text.startswith("@")
    tokens = shlex.split(text[1:])
    return dict(zip(SOP_SCHEMA, tokens[1:]))


def _row(provider=None, status_code=200):
    """A ``GET /terminals/{id}`` response carrying ``provider``."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"id": "receiver-123", "provider": provider}
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(
            f"status {status_code}", response=resp
        )
    return resp


class TestPredicate:
    """``is_structured_prompt_invocation`` needs BOTH the grammar and the provider."""

    @pytest.mark.parametrize(
        "message",
        [
            SOP_INVOCATION,
            "@codebase-summary",
            "@agent-sop:pdd task_description='ship it' mode=fsc",
            "  @agent-sop:x leading whitespace still dispatches as a prompt",
            AT_PATH_TASK,  # kiro has no file-mention grammar: this IS a prompt call
        ],
    )
    def test_kiro_receiver_with_leading_at_is_an_invocation(self, message):
        assert orchestration.is_structured_prompt_invocation(message, KIRO) is True

    @pytest.mark.parametrize(
        "message",
        [
            "Analyze the logs",
            "Review @src/main.py and report",  # an @-mention mid-prose is not an invocation
            "email bob@example.com",
            "",
        ],
    )
    def test_prose_is_never_an_invocation(self, message):
        assert orchestration.is_structured_prompt_invocation(message, KIRO) is False

    @pytest.mark.parametrize("provider", NON_KIRO)
    @pytest.mark.parametrize("message", [SOP_INVOCATION, AT_PATH_TASK])
    def test_non_kiro_receiver_reads_leading_at_as_prose(self, provider, message):
        assert orchestration.is_structured_prompt_invocation(message, provider) is False

    def test_unknown_provider_is_treated_as_prose(self):
        """Unknown receiver: keep the suffix, which is what every provider got pre-#70."""
        assert orchestration.is_structured_prompt_invocation(SOP_INVOCATION, None) is False


class TestSuffixHelper:
    """Direct contract of the shared helper."""

    def test_prose_gets_suffix_on_its_own_paragraph(self):
        out = orchestration.append_sender_suffix(
            "Analyze the logs", orchestration.ASSIGN_SUFFIX_TEMPLATE, "a1b2c3d4", provider=KIRO
        )
        assert out == (
            "Analyze the logs\n\n[Assigned by terminal a1b2c3d4. "
            "When done, send results back to terminal a1b2c3d4 using send_message]"
        )

    def test_kiro_invocation_is_returned_unchanged(self):
        out = orchestration.append_sender_suffix(
            SOP_INVOCATION, orchestration.ASSIGN_SUFFIX_TEMPLATE, "a1b2c3d4", provider=KIRO
        )
        assert out == SOP_INVOCATION

    @pytest.mark.parametrize("provider", NON_KIRO)
    def test_non_kiro_receiver_keeps_suffix_on_at_leading_message(self, provider):
        out = orchestration.append_sender_suffix(
            AT_PATH_TASK, orchestration.ASSIGN_SUFFIX_TEMPLATE, "a1b2c3d4", provider=provider
        )
        assert out == (
            AT_PATH_TASK + "\n\n[Assigned by terminal a1b2c3d4. "
            "When done, send results back to terminal a1b2c3d4 using send_message]"
        )

    def test_unknown_receiver_keeps_suffix(self):
        out = orchestration.append_sender_suffix(
            AT_PATH_TASK, orchestration.MESSAGE_SUFFIX_TEMPLATE, "deadbeef", provider=None
        )
        assert out.endswith(
            "[Message from terminal deadbeef. Use send_message MCP tool for any follow-up work.]"
        )

    def test_resolver_is_consulted_only_for_at_leading_messages(self):
        """The lookup can cost an HTTP GET; prose must never pay for it."""
        resolver = Mock(return_value=KIRO)
        orchestration.append_sender_suffix(
            "Analyze the logs", orchestration.ASSIGN_SUFFIX_TEMPLATE, "a1b2c3d4", provider=resolver
        )
        resolver.assert_not_called()

        out = orchestration.append_sender_suffix(
            SOP_INVOCATION, orchestration.ASSIGN_SUFFIX_TEMPLATE, "a1b2c3d4", provider=resolver
        )
        resolver.assert_called_once_with()
        assert out == SOP_INVOCATION

    def test_resolver_answer_decides(self):
        claude = Mock(return_value=ProviderType.CLAUDE_CODE.value)
        out = orchestration.append_sender_suffix(
            SOP_INVOCATION, orchestration.ASSIGN_SUFFIX_TEMPLATE, "a1b2c3d4", provider=claude
        )
        assert out.startswith(SOP_INVOCATION + "\n\n[Assigned by terminal a1b2c3d4")

    def test_provider_is_mandatory(self):
        """A call site that does not say who receives the message is a bug."""
        with pytest.raises(TypeError):
            orchestration.append_sender_suffix(  # type: ignore[call-arg]
                "Analyze the logs", orchestration.ASSIGN_SUFFIX_TEMPLATE, "a1b2c3d4"
            )


class TestAssignLocalWorker:
    """``_assign_impl`` without ``target_host``: the worker's provider is the
    one ``_create_terminal`` will resolve (claim, else profile with the
    supervisor's provider as fallback)."""

    _ENV = {"CAO_TERMINAL_ID": "a1b2c3d4"}

    def _ctx(self, provider):
        return HandoffContext(
            provider=provider, session_name="cao-s", caller_id="a1b2c3d4", allowed_tools=None
        )

    @patch(f"{_ORCH}._get_cleanup_nudge", return_value="")
    @patch(f"{_ORCH}.ENABLE_SENDER_ID_INJECTION", True)
    @patch(f"{_ORCH}._resolve_handoff_provider")
    @patch(f"{_ORCH}._create_terminal")
    def test_kiro_worker_gets_sop_invocation_byte_for_byte(self, mock_create, mock_ctx, _nudge):
        mock_ctx.return_value = self._ctx(KIRO)
        mock_create.return_value = ("worker-1", KIRO)

        with patch.dict(os.environ, self._ENV):
            result = orchestration._assign_impl("developer", SOP_INVOCATION)

        assert result["success"] is True
        sent = mock_create.call_args.kwargs["initial_message"]
        assert sent == SOP_INVOCATION
        # What the SOP would actually receive: the optional parameter stays unset.
        assert _kiro_prompt_arguments(sent) == {"query": "query=What is the project structure?"}

    @pytest.mark.parametrize("provider", [ProviderType.CLAUDE_CODE.value, ProviderType.CODEX.value])
    @patch(f"{_ORCH}._get_cleanup_nudge", return_value="")
    @patch(f"{_ORCH}.ENABLE_SENDER_ID_INJECTION", True)
    @patch(f"{_ORCH}._resolve_handoff_provider")
    @patch(f"{_ORCH}._create_terminal")
    def test_non_kiro_worker_keeps_callback_instruction_on_at_path_task(
        self, mock_create, mock_ctx, _nudge, provider
    ):
        """The reviewer's probe on PR #1028: a Claude Code worker given
        ``@src/app.py ...`` must still be told to send results back."""
        mock_ctx.return_value = self._ctx(provider)
        mock_create.return_value = ("worker-1", provider)

        with patch.dict(os.environ, self._ENV):
            orchestration._assign_impl("developer", AT_PATH_TASK)

        sent = mock_create.call_args.kwargs["initial_message"]
        assert sent == (
            AT_PATH_TASK + "\n\n[Assigned by terminal a1b2c3d4. "
            "When done, send results back to terminal a1b2c3d4 using send_message]"
        )
        mock_ctx.assert_called_once_with("developer")

    @pytest.mark.parametrize(
        ("provider", "expect_suffix"),
        [(KIRO, False), (ProviderType.CLAUDE_CODE.value, True)],
    )
    @patch(f"{_ORCH}._get_cleanup_nudge", return_value="")
    @patch(f"{_ORCH}.ENABLE_SENDER_ID_INJECTION", True)
    @patch(f"{_ORCH}._resolve_handoff_provider")
    @patch(f"{_ORCH}._claim_ephemeral")
    @patch(f"{_ORCH}._create_terminal")
    def test_ephemeral_claim_pins_the_provider_without_a_lookup(
        self, mock_create, mock_claim, mock_ctx, _nudge, provider, expect_suffix
    ):
        mock_claim.return_value = EphemeralClaim(provider, ["send_message"], "claim-1")
        mock_create.return_value = ("worker-1", provider)

        with patch.dict(os.environ, self._ENV):
            orchestration._assign_impl("Ramones-log_triage-3f9a", AT_PATH_TASK)

        sent = mock_create.call_args.kwargs["initial_message"]
        assert ("[Assigned by terminal a1b2c3d4" in sent) is expect_suffix
        mock_ctx.assert_not_called()

    @patch(f"{_ORCH}._get_cleanup_nudge", return_value="")
    @patch(f"{_ORCH}.ENABLE_SENDER_ID_INJECTION", True)
    @patch(f"{_ORCH}._resolve_handoff_provider")
    @patch(f"{_ORCH}._create_terminal")
    def test_prose_is_suffixed_without_a_provider_lookup(self, mock_create, mock_ctx, _nudge):
        """The guard is narrow: ordinary task prose keeps the reminder and
        does not pay for the extra GET."""
        mock_create.return_value = ("worker-1", KIRO)

        with patch.dict(os.environ, self._ENV):
            orchestration._assign_impl("developer", "Analyze the logs")

        sent = mock_create.call_args.kwargs["initial_message"]
        assert sent.startswith("Analyze the logs\n\n[Assigned by terminal a1b2c3d4")
        mock_ctx.assert_not_called()


class TestAssignRemoteWorker:
    """``_assign_impl`` with ``target_host``: the provider lives in the remote
    node's profile store (``_resolve_remote_provider``)."""

    _ENV = {
        "CAO_TERMINAL_ID": "a1b2c3d4",
        "CAO_ADVERTISED_URL": "http://cao-supervisor:9889",
    }

    def _post(self):
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"id": "beef0001", "session_name": "cao-remote"}
        return resp

    @pytest.mark.parametrize(
        ("provider", "expected"),
        [
            (KIRO, SOP_INVOCATION),
            (
                ProviderType.CLAUDE_CODE.value,
                SOP_INVOCATION + "\n\n[Assigned by terminal a1b2c3d4. "
                "When done, send results back to terminal a1b2c3d4 using send_message]",
            ),
        ],
    )
    @patch(f"{_ORCH}.ENABLE_SENDER_ID_INJECTION", True)
    @patch(f"{_ORCH}._resolve_remote_provider")
    @patch(f"{_ORCH}.requests.post")
    def test_remote_worker_provider_decides(self, mock_post, mock_provider, provider, expected):
        mock_provider.return_value = provider
        mock_post.return_value = self._post()

        with patch.dict(os.environ, self._ENV):
            result = orchestration._assign_impl(
                "developer", SOP_INVOCATION, target_host="cao-worker-0"
            )

        assert result["success"] is True
        body = mock_post.call_args.kwargs["json"]
        assert body["initial_message"] == expected
        mock_provider.assert_called_once_with("http://cao-worker-0:9889", "developer")

    @patch(f"{_ORCH}.ENABLE_SENDER_ID_INJECTION", True)
    @patch(f"{_ORCH}._resolve_remote_provider")
    @patch(f"{_ORCH}.requests.post")
    def test_remote_prose_is_suffixed_without_a_provider_lookup(self, mock_post, mock_provider):
        mock_post.return_value = self._post()

        with patch.dict(os.environ, self._ENV):
            orchestration._assign_impl("developer", "Analyze the logs", target_host="cao-worker-0")

        body = mock_post.call_args.kwargs["json"]
        assert body["initial_message"].startswith(
            "Analyze the logs\n\n[Assigned by terminal a1b2c3d4"
        )
        mock_provider.assert_not_called()

    @patch(f"{_ORCH}.ENABLE_SENDER_ID_INJECTION", False)
    @patch(f"{_ORCH}._resolve_remote_provider")
    @patch(f"{_ORCH}.requests.post")
    def test_injection_off_delivers_remote_message_as_given(self, mock_post, mock_provider):
        mock_post.return_value = self._post()

        with patch.dict(os.environ, self._ENV):
            orchestration._assign_impl("developer", "Analyze the logs", target_host="cao-worker-0")

        assert mock_post.call_args.kwargs["json"]["initial_message"] == "Analyze the logs"
        mock_provider.assert_not_called()


class TestSendMessageReceiver:
    """``_send_message_impl``: the RECEIVER's row says which CLI reads the text."""

    _ENV = {"CAO_TERMINAL_ID": "deadbeef"}
    _SUFFIX = "[Message from terminal deadbeef. Use send_message MCP tool for any follow-up work.]"

    @patch(f"{_ORCH}._target_binding_denial", return_value=None)
    @patch(f"{_ORCH}.ENABLE_SENDER_ID_INJECTION", True)
    @patch(f"{_ORCH}.requests.get")
    @patch(f"{_ORCH}._send_to_inbox", return_value={"success": True})
    def test_kiro_receiver_gets_sop_invocation_byte_for_byte(self, mock_inbox, mock_get, _unbound):
        mock_get.return_value = _row(KIRO)

        with patch.dict(os.environ, self._ENV):
            orchestration._send_message_impl("receiver-123", SOP_INVOCATION)

        sent = mock_inbox.call_args[0][1]
        assert sent == SOP_INVOCATION
        assert _kiro_prompt_arguments(sent) == {"query": "query=What is the project structure?"}
        assert mock_get.call_args[0][0].endswith("/terminals/receiver-123")

    @pytest.mark.parametrize("provider", [ProviderType.CLAUDE_CODE.value, ProviderType.CODEX.value])
    @patch(f"{_ORCH}._target_binding_denial", return_value=None)
    @patch(f"{_ORCH}.ENABLE_SENDER_ID_INJECTION", True)
    @patch(f"{_ORCH}.requests.get")
    @patch(f"{_ORCH}._send_to_inbox", return_value={"success": True})
    def test_non_kiro_receiver_keeps_sender_id_on_at_leading_message(
        self, mock_inbox, mock_get, _unbound, provider
    ):
        mock_get.return_value = _row(provider)

        with patch.dict(os.environ, self._ENV):
            orchestration._send_message_impl("receiver-123", AT_PATH_TASK)

        assert mock_inbox.call_args[0][1] == AT_PATH_TASK + "\n\n" + self._SUFFIX

    @pytest.mark.parametrize(
        "lookup",
        [
            pytest.param(_row(status_code=404), id="receiver-unknown-404"),
            pytest.param(_row(status_code=503), id="server-error"),
            pytest.param(requests.ConnectionError("down"), id="connection-error"),
        ],
    )
    @patch(f"{_ORCH}._target_binding_denial", return_value=None)
    @patch(f"{_ORCH}.ENABLE_SENDER_ID_INJECTION", True)
    @patch(f"{_ORCH}.requests.get")
    @patch(f"{_ORCH}._send_to_inbox", return_value={"success": True})
    def test_unreadable_receiver_row_keeps_suffix_and_still_delivers(
        self, mock_inbox, mock_get, _unbound, lookup
    ):
        """The lookup is advisory: an unknown receiver keeps pre-#70 behaviour
        and the inbox POST reports any real delivery error itself."""
        if isinstance(lookup, Exception):
            mock_get.side_effect = lookup
        else:
            mock_get.return_value = lookup

        with patch.dict(os.environ, self._ENV):
            result = orchestration._send_message_impl("receiver-123", AT_PATH_TASK)

        assert result == {"success": True}
        assert mock_inbox.call_args[0][1] == AT_PATH_TASK + "\n\n" + self._SUFFIX

    @patch(f"{_ORCH}._target_binding_denial", return_value=None)
    @patch(f"{_ORCH}.ENABLE_SENDER_ID_INJECTION", True)
    @patch(f"{_ORCH}.requests.get")
    @patch(f"{_ORCH}._send_to_inbox", return_value={"success": True})
    def test_prose_is_suffixed_without_a_receiver_lookup(self, mock_inbox, mock_get, _unbound):
        with patch.dict(os.environ, self._ENV):
            orchestration._send_message_impl("receiver-123", "Here are the results")

        sent = mock_inbox.call_args[0][1]
        assert sent.startswith("Here are the results\n\n[Message from terminal deadbeef")
        mock_get.assert_not_called()

    @patch(f"{_ORCH}.ENABLE_SENDER_ID_INJECTION", True)
    @patch(f"{_ORCH}.requests.get")
    @patch(f"{_ORCH}._send_to_inbox", return_value={"success": True})
    def test_cross_node_supervisor_row_is_read_from_the_callback_node(self, mock_inbox, mock_get):
        """A remote worker replying up: the supervisor's row lives on the
        callback node, exactly where ``_send_to_inbox`` posts."""
        mock_get.return_value = _row(KIRO)
        env = {
            **self._ENV,
            "CAO_CALLBACK_URL": "http://cao-supervisor:9889/",
            "CAO_CALLBACK_TERMINAL_ID": "a1b2c3d4",
        }

        with patch.dict(os.environ, env):
            orchestration._send_message_impl(None, SOP_INVOCATION)

        assert mock_get.call_args[0][0] == "http://cao-supervisor:9889/terminals/a1b2c3d4"
        assert mock_inbox.call_args[0][1] == SOP_INVOCATION


class TestSuffixHasOneOwner:
    """Structural guard for the class: every sender-ID suffix in orchestration.py
    is built by ``append_sender_suffix``; the literals live only in the two
    module-level templates; every send site calls the helper and names the
    receiving provider; and the provider set is spelled from the enum."""

    _LITERALS = ("[Assigned by terminal", "[Message from terminal")
    _TEMPLATES = {"ASSIGN_SUFFIX_TEMPLATE", "MESSAGE_SUFFIX_TEMPLATE"}
    # Every function that hands a sender suffix to a worker. A new send path
    # must be added here AND call the helper with ``provider=``.
    _SEND_SITES = ("_assign_impl", "_assign_remote", "_send_message_impl")

    @pytest.fixture(scope="class")
    def tree(self):
        return ast.parse(inspect.getsource(orchestration))

    @staticmethod
    def _helper_calls(fn):
        return [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "append_sender_suffix"
        ]

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

    @pytest.mark.parametrize("impl", _SEND_SITES)
    def test_each_send_site_names_the_receiving_provider(self, tree, impl):
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == impl)
        calls = self._helper_calls(fn)
        assert calls, f"{impl} composes its suffix inline"
        for call in calls:
            keywords = {kw.arg for kw in call.keywords}
            assert "provider" in keywords, f"{impl} appends a suffix without a provider"

    def test_no_send_site_outside_the_enumerated_set(self, tree):
        """A fourth function that appends a suffix must be added to _SEND_SITES
        (and so to the provider-aware tests above)."""
        callers = {
            fn.name
            for fn in tree.body
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and self._helper_calls(fn)
        }
        assert callers == set(self._SEND_SITES), callers

    def test_structured_prompt_providers_are_real_providers(self):
        known = {p.value for p in ProviderType}
        assert orchestration.STRUCTURED_PROMPT_PROVIDERS <= known
        assert orchestration.STRUCTURED_PROMPT_PROVIDERS, "the set must name kiro_cli"
