"""PR #799 adversarial closure — the second-stage review findings.

One class per finding, driven through the **public boundary** wherever one
exists, rather than only through helpers:

* extraction findings run through ``terminal_service.get_output(mode=LAST)``,
  which is what handoff/assign callers and the memory layer consume;
* the probe and launch findings run through provider initialization;
* the runtime-home findings are asserted against the builder's filesystem
  effects;
* shell portability is proven by executing the real command under a real
  ``fish``.

Every case in this module was reproduced failing against the pre-fix head
``445c562c``; the pre-fix evidence is recorded in
``reports/kimi_code_compat/PR799-ADVERSARIAL-CLOSURE.md``.

Shared shape of the fixes under test: a row's *text* is never enough to make it
UI state. Tool headers need the renderer's tool-name style or the measured ``·``
detail separator, dialogs need the whole dialog, reasoning needs reasoning
styling, and tool payload cannot certify its own end by resembling chrome.
"""

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.providers import base as provider_base
from cli_agent_orchestrator.providers import kimi_cli as kimi_cli_module
from cli_agent_orchestrator.providers import kimi_runtime_home as krh
from cli_agent_orchestrator.providers import kimi_transcript as kt
from cli_agent_orchestrator.providers.base import OutputExtractionError
from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider
from cli_agent_orchestrator.providers.kimi_runtime_home import KimiCodeRuntimeHomeBuilder


def _rejected():
    """The non-retryable rejection type, resolved at call time.

    Resolved through the module rather than imported by name so this suite
    reports each finding's own failure against a head that predates the type,
    instead of failing to collect at all.
    """

    return provider_base.OutputExtractionRejected


FIXTURES = Path(__file__).parent / "fixtures"


#: The renderer's own answer bullet: colour 253.
def _answer(text: str) -> str:
    return f" \x1b[38;5;253m● \x1b[39m{text}"


#: Reasoning as the renderer draws it: grey 244 + italic.
def _thinking(text: str) -> str:
    return f" \x1b[38;5;244m● \x1b[3m{text}\x1b[0m"


def _reasoning_continuation(text: str) -> str:
    return f"   \x1b[38;5;244m\x1b[3m{text}\x1b[0m"


def _footer(text: str = "context: 2% (14.8k/977k)") -> str:
    """A status/footer row as the renderer draws it (foreground colour 253)."""

    return f" \x1b[38;5;253m{text}\x1b[39m"


def _user(text: str) -> str:
    """A submitted-message row as Kimi Code draws it (bold + colour 222)."""
    return "\x1b[1;38;5;222m" + text + "\x1b[0m"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


def _last(monkeypatch, pane: str, dialect=None):
    """Run the public ``get_output(mode=LAST)`` path against ``pane``."""

    from cli_agent_orchestrator.services import terminal_service

    provider = KimiCliProvider("term-adv", "session-1", "window-1")
    if dialect is not None:
        provider._dialect = dialect
    backend = MagicMock()
    backend.get_history.return_value = pane
    monkeypatch.setattr(
        terminal_service,
        "get_terminal_metadata",
        lambda tid: {"tmux_session": "s", "tmux_window": "w"},
    )
    monkeypatch.setattr(terminal_service.status_monitor, "get_buffer", lambda tid: pane)
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service.provider_manager, "get_provider", lambda tid: provider)
    return terminal_service.get_output("term-adv", terminal_service.OutputMode.LAST), backend


# =============================================================================
# P1 — reasoning-only fail-closed must survive the public LAST boundary
# =============================================================================

#: Private reasoning that must never appear in anything a caller can read.
PRIVATE_REASONING = "Let me think about this privately and never show it."


class TestPR799AdversarialReasoningRejection:
    """A deliberate content refusal must never become the raw-transcript fallback.

    The extractor already refused to publish reasoning-only turns, but
    ``OutputExtractionError`` subclasses ``ValueError``, and ``get_output`` used
    ``except ValueError`` to mean "response marker not found, escalate". The
    refusal was therefore swallowed, escalation ran to exhaustion, and the caller
    received ``[NO RESPONSE …]`` followed by the raw pane — which contains the
    reasoning that was just refused.
    """

    @pytest.fixture
    def reasoning_only_pane(self):
        return "\n".join(["💫 Do the thing", _thinking(PRIVATE_REASONING), ""])

    def test_public_last_raises_and_leaks_no_raw_transcript(self, monkeypatch, reasoning_only_pane):
        with pytest.raises(_rejected()) as excinfo:
            _last(monkeypatch, reasoning_only_pane)

        message = str(excinfo.value)
        assert PRIVATE_REASONING not in message
        assert "💫 Do the thing" not in message
        assert "[NO RESPONSE" not in message

    def test_public_last_does_not_escalate(self, monkeypatch, reasoning_only_pane):
        """A refusal is not retryable, so no wider capture is fetched."""

        from cli_agent_orchestrator.services import terminal_service

        provider = KimiCliProvider("term-adv-esc", "session-1", "window-1")
        backend = MagicMock()
        backend.get_history.return_value = reasoning_only_pane
        monkeypatch.setattr(
            terminal_service,
            "get_terminal_metadata",
            lambda tid: {"tmux_session": "s", "tmux_window": "w"},
        )
        monkeypatch.setattr(
            terminal_service.status_monitor, "get_buffer", lambda tid: reasoning_only_pane
        )
        monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
        monkeypatch.setattr(terminal_service.provider_manager, "get_provider", lambda tid: provider)

        with pytest.raises(_rejected()):
            terminal_service.get_output("term-adv-esc", terminal_service.OutputMode.LAST)
        assert backend.get_history.call_count == 1

    def test_rejection_is_distinguishable_from_a_missing_marker(self):
        assert issubclass(_rejected(), OutputExtractionError)
        assert issubclass(_rejected(), ValueError)
        assert _rejected() is not OutputExtractionError

    def test_marker_missing_is_still_retryable(self, monkeypatch):
        """The genuine "capture too shallow" case must still escalate."""

        provider = KimiCliProvider("term-adv2", "session-1", "window-1")
        with pytest.raises(OutputExtractionError) as excinfo:
            provider.extract_last_message_from_script("")
        assert not isinstance(excinfo.value, _rejected())

    def test_multiline_reasoning_only_is_also_rejected(self, monkeypatch):
        pane = "\n".join(
            [
                "💫 Do the thing",
                _thinking("Internal heading"),
                _reasoning_continuation(PRIVATE_REASONING),
                _reasoning_continuation("and another private line"),
                "",
            ]
        )
        with pytest.raises(_rejected()) as excinfo:
            _last(monkeypatch, pane)
        assert PRIVATE_REASONING not in str(excinfo.value)


# =============================================================================
# Structural turn/parser invariants — history must not define current UI
# =============================================================================


class TestPR799StructuralTurnParser:
    """Metamorphic guards for the turn-scoped structural parser.

    A terminal capture is scrollback, not one UI frame.  Historical dialogs,
    answers, reasoning and tool calls may all remain above the current turn.
    They must not promote text in the current answer into response-ending UI
    state.  Conversely, quoting UI text is still ordinary answer content unless
    the renderer's own local structure is present in the current turn.
    """

    @staticmethod
    def _current_turn(*body: str) -> str:
        return "\n".join([_user("✨ Summarize the configuration"), "", *body, ""])

    def test_old_trust_dialog_cannot_promote_current_heading(self, monkeypatch):
        """A9: a previous trust dialog must not own later matching prose."""

        turn = self._current_turn(
            _answer("Configuration summary:"),
            "Project MCP targets:",
            "- repo-tools",
            "All checks passed.",
        )
        expected, _ = _last(monkeypatch, turn)
        assert expected == (
            "● Configuration summary:\n"
            "Project MCP targets:\n"
            "- repo-tools\n"
            "All checks passed."
        )

        prefixed = _fixture("kimi_code_0431_07_workspace_trust_dialog_plain.txt") + "\n" + turn
        actual, _ = _last(monkeypatch, prefixed)
        assert actual == expected

    def test_old_approval_dialog_cannot_promote_current_quote(self, monkeypatch):
        """Historical approval chrome cannot turn a later quoted menu into UI."""

        turn = self._current_turn(
            _answer("Deployment notes:"),
            "▶ Run this command?",
            "▶ 1. Approve once",
            "Continue with the next step.",
        )
        expected, _ = _last(monkeypatch, turn)
        assert "Continue with the next step." in expected

        prefixed = _fixture("kimi_code_0431_08_command_approval_dialog.txt") + "\n" + turn
        actual, _ = _last(monkeypatch, prefixed)
        assert actual == expected

    def test_plain_full_trust_dialog_quote_is_answer_content(self, monkeypatch):
        """Even a complete *textual* quote is not a live rendered dialog."""

        turn = self._current_turn(
            _answer("Example dialog:"),
            "Trust this folder?",
            "↑↓ navigate · Enter select · Esc exit",
            "/tmp/example project",
            "❯ Trust this folder",
            "Don't trust",
            "After the example.",
        )
        result, _ = _last(monkeypatch, turn)
        assert result == (
            "● Example dialog:\n"
            "Trust this folder?\n"
            "↑↓ navigate · Enter select · Esc exit\n"
            "/tmp/example project\n"
            "❯ Trust this folder\n"
            "Don't trust\n"
            "After the example."
        )

    def test_plain_exact_footer_shape_is_answer_content(self, monkeypatch):
        """Footer text without renderer styling is ordinary model output."""

        turn = self._current_turn(
            _answer("Budget example:"),
            "context: 2% (14.8k/977k)",
            "That is only an example value.",
        )
        result, _ = _last(monkeypatch, turn)
        assert result == (
            "● Budget example:\n" "context: 2% (14.8k/977k)\n" "That is only an example value."
        )

    def test_box_drawing_corner_in_answer_is_not_a_composer_boundary(self, monkeypatch):
        """A corner glyph starts a frame only when the rest of the row is a border."""

        pane = "\n".join(
            [
                "💫 Explain this Unicode character",
                "• Here is the character:",
                "╭ U+256D BOX DRAWINGS LIGHT ARC DOWN AND RIGHT",
                "• It starts a rounded box.",
                "💫",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert result == (
            "• Here is the character:\n"
            "╭ U+256D BOX DRAWINGS LIGHT ARC DOWN AND RIGHT\n"
            "• It starts a rounded box."
        )

    def test_historical_private_content_does_not_make_current_miss_nonretryable(self):
        """Refusal evidence is scoped to the current turn, not old scrollback."""

        provider = KimiCliProvider("term-turn-scope", "session-1", "window-1")
        pane = "\n".join(
            [
                "💫 Old task",
                _thinking("old private reasoning"),
                _answer("Old public answer"),
                _user("✨ Current task"),
                "",
            ]
        )
        with pytest.raises(OutputExtractionError) as excinfo:
            provider.extract_last_message_from_script(pane)
        assert not isinstance(excinfo.value, _rejected())

    @pytest.mark.parametrize(
        "history_fixture",
        [
            "kimi_code_0431_03_final_answer.txt",
            "kimi_code_0431_07_workspace_trust_dialog_plain.txt",
            "kimi_code_0431_08_command_approval_dialog.txt",
            "kimi_code_0431_10_mcp_tool_turn.txt",
            "kimi_code_0431_12_mcp_tool_turn_reasoning_first.txt",
        ],
    )
    def test_history_prefix_does_not_change_current_answer(self, monkeypatch, history_fixture):
        turn = self._current_turn(
            _answer("Current answer"),
            "ordinary continuation",
        )
        expected, _ = _last(monkeypatch, turn)
        actual, _ = _last(monkeypatch, _fixture(history_fixture) + "\n" + turn)
        assert actual == expected

    @pytest.mark.parametrize(
        "answer_line",
        [
            "Project MCP targets:",
            "context: 2% (14.8k/977k)",
            "▶ Run this command?",
            "Loading configuration...",
            "| > | is a Markdown-table cell, not a composer prompt.",
            "Used Python · no external dependencies.",
            "╭ U+256D BOX DRAWINGS LIGHT ARC DOWN AND RIGHT",
        ],
    )
    @pytest.mark.parametrize(
        "history_fixture",
        [
            "kimi_code_0431_03_final_answer.txt",
            "kimi_code_0431_07_workspace_trust_dialog_plain.txt",
            "kimi_code_0431_08_command_approval_dialog.txt",
            "kimi_code_0431_10_mcp_tool_turn.txt",
            "kimi_code_0431_12_mcp_tool_turn_reasoning_first.txt",
        ],
    )
    def test_ui_like_answer_text_is_history_prefix_invariant(
        self, monkeypatch, history_fixture, answer_line
    ):
        """Old chrome may not promote identical current-turn prose into UI state.

        This is deliberately metamorphic rather than one regression per token:
        adding valid historical scrollback must not change extraction of the
        same current turn, even when the answer happens to contain text that is
        meaningful to the renderer elsewhere.
        """

        turn = self._current_turn(
            _answer("Quoted UI-looking text:"),
            answer_line,
            "Still part of the answer.",
        )
        expected, _ = _last(monkeypatch, turn)
        actual, _ = _last(monkeypatch, _fixture(history_fixture) + "\n" + turn)

        assert actual == expected
        assert answer_line in actual
        assert actual.endswith("Still part of the answer.")

    @pytest.mark.parametrize(
        "history_fixture",
        [
            "kimi_code_0431_03_final_answer.txt",
            "kimi_code_0431_07_workspace_trust_dialog_plain.txt",
            "kimi_code_0431_08_command_approval_dialog.txt",
            "kimi_code_0431_10_mcp_tool_turn.txt",
        ],
    )
    def test_history_prefix_never_turns_private_reasoning_publishable(
        self, monkeypatch, history_fixture
    ):
        """Historical public UI cannot exempt a private current turn."""

        current = "\n".join(
            [
                _user("✨ Think privately"),
                "",
                _thinking(PRIVATE_REASONING),
                _footer(),
                "",
            ]
        )
        pane = _fixture(history_fixture) + "\n" + current

        with pytest.raises(_rejected()) as excinfo:
            _last(monkeypatch, pane)

        assert PRIVATE_REASONING not in str(excinfo.value)


# =============================================================================
# P2 — multiline / wrapped reasoning continuation
# =============================================================================


class TestPR799AdversarialMultilineReasoning:
    """Reasoning styling must propagate to the block's continuation rows.

    The classifier recognised the grey thinking *bullet* but kept no reasoning
    block, so a wrapped reasoning line was ``CONTENT`` and reached the answer —
    and a turn with no final answer returned the private continuation itself.
    """

    def test_continuation_does_not_reach_the_answer(self, monkeypatch):
        pane = "\n".join(
            [
                "💫 Explain.",
                _thinking("Internal heading"),
                _reasoning_continuation(PRIVATE_REASONING),
                _answer("Public answer"),
                "",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert result == "● Public answer"
        assert PRIVATE_REASONING not in result

    def test_multiple_continuation_rows_are_all_excluded(self, monkeypatch):
        pane = "\n".join(
            [
                "💫 Explain.",
                _thinking("Heading"),
                _reasoning_continuation("private one"),
                _reasoning_continuation("private two"),
                _reasoning_continuation("private three"),
                _answer("Public answer"),
                "",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert result == "● Public answer"

    def test_no_final_answer_returns_no_reasoning(self, monkeypatch):
        pane = "\n".join(
            [
                "💫 Explain.",
                _thinking("Heading"),
                _reasoning_continuation(PRIVATE_REASONING),
                "",
            ]
        )
        with pytest.raises(_rejected()) as excinfo:
            _last(monkeypatch, pane)
        assert PRIVATE_REASONING not in str(excinfo.value)

    def test_tool_boundary_ends_the_reasoning_block(self):
        rows = [
            _thinking("Heading"),
            _reasoning_continuation("private"),
            "● Used find_profiles · MCP/cao-mcp-server (kimi)",
            "● Public answer",
        ]
        kinds = kt.classify_rows(rows, semantics=kt.SpinnerSemantics.CODE)
        assert kinds[1] is kt.KimiLineKind.THINKING_BULLET
        assert kinds[2] is kt.KimiLineKind.TOOL_CALL

    def test_unstyled_prose_after_a_thinking_bullet_is_not_absorbed(self):
        """The guard: no blind suppression of arbitrary prose."""

        rows = [_thinking("Heading"), "Ordinary prose that follows."]
        kinds = kt.classify_rows(rows, semantics=kt.SpinnerSemantics.CODE)
        assert kinds[1] is kt.KimiLineKind.CONTENT


# =============================================================================
# Maintainer P2-A / Codex #8 / #11 — tool header vs prose
# =============================================================================


class TestPR799AdversarialToolRowCollision:
    """A tool header needs renderer structure, not a verb plus a parenthesis.

    ``_TOOL_ROW_SUFFIX`` accepted an opening parenthesis as proof, so ordinary
    function-call prose opened a tool block, suppressed the continuation rows and
    degraded the public path to the raw-transcript fallback.
    """

    @pytest.mark.parametrize(
        "row",
        [
            "• Calling retry() twice is safe.",
            "• Calling connect (with TLS) encrypts the connection.",
            "• Calling this function twice returns two rows.",
            "• Running a command is unnecessary here.",
            "● Using Python (3.12) is recommended.",
            "● Used widely in production.",
        ],
    )
    def test_prose_is_answer_content(self, row):
        assert kt.classify_line(row) is kt.KimiLineKind.FINAL_BULLET

    @pytest.mark.parametrize(
        "row",
        [
            "● Running a command · $ uname -a",
            "● Used Read (ANSWER_SPEC.md) · 10 lines",
            "● Used find_profiles · MCP/cao-mcp-server (kimi)",
            "● Using find_profiles · MCP/cao-mcp-server",
            "● Used search-docs · MCP/cao-mcp-server",
            "● Used docs.search · MCP/docs",
            "● Used snake_case · MCP/x",
            " \x1b[38;5;114m● \x1b[39mUsed \x1b[1m\x1b[38;5;111mfind_profiles\x1b[0;2m"
            " · MCP/cao-mcp-server (kimi)\x1b[0m",
        ],
    )
    def test_measured_tool_headers_stay_tool_calls(self, row):
        assert kt.classify_line(row) is kt.KimiLineKind.TOOL_CALL

    @pytest.mark.parametrize(
        "prose",
        [
            "• Calling retry() twice is safe.",
            "• Calling connect (with TLS) encrypts the connection.",
            "● Using Python (3.12) is recommended.",
        ],
    )
    def test_tool_like_prose_survives_the_public_path(self, monkeypatch, prose):
        pane = "\n".join(["💫 Explain.", prose, "Install it first.", ""])
        result, _ = _last(monkeypatch, pane)
        assert prose in result
        assert "Install it first." in result

    def test_hyphenated_and_dotted_tool_payload_stays_excluded(self, monkeypatch):
        for name in ("search-docs", "docs.search"):
            pane = "\n".join(
                [
                    "💫 Search.",
                    f"● Used {name} · MCP/cao-mcp-server",
                    '[{"hit":"private"}]\x1b[2m …\x1b[22m',
                    _answer("Found 1 hit."),
                    "",
                ]
            )
            result, _ = _last(monkeypatch, pane)
            assert result == "● Found 1 hit.", name
            assert "private" not in result, name


# =============================================================================
# Codex #9 — payload cannot certify its own end
# =============================================================================

#: Payload rows that previously terminated the exclusion block by resembling
#: TUI chrome, letting private payload into the answer.
_ADVERSARIAL_PAYLOADS = {
    "rule": ["───────"],
    "bullet": ["● PRIVATE payload bullet"],
    "context": ["context: 99% (1/2)"],
    "footer-ish": ["agent (PRIVATE-k2.6 ●)"],
    "dialog-ish": ["Trust this folder?", "❯ Trust this folder"],
    "composer-ish": ["╭────────────╮", "│ >          │", "╰────────────╯"],
    "boot-ish": ["connecting to mcp servers..."],
    "collapse-ish": ["… (3 more lines, ctrl+o to expand)"],
}


class TestPR799AdversarialToolPayloadBoundaries:
    """Tool output is arbitrary content; only renderer evidence ends the block."""

    @pytest.mark.parametrize("label", sorted(_ADVERSARIAL_PAYLOADS))
    def test_payload_never_reaches_the_answer(self, monkeypatch, label):
        payload = _ADVERSARIAL_PAYLOADS[label]
        pane = "\n".join(
            [
                "💫 Read the report.",
                "● Used Read (report.txt) · 3 lines",
                *payload,
                _answer("The report is clean."),
                "",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert result == "● The report is clean.", label
        for row in payload:
            assert row not in result, (label, row)

    def test_blank_line_separated_payload_is_excluded(self, monkeypatch):
        pane = "\n".join(
            [
                "💫 Read the report.",
                "● Used Read (report.txt) · 3 lines",
                "",
                "PRIVATE payload after a blank line",
                "",
                _answer("The report is clean."),
                "",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert result == "● The report is clean."
        assert "PRIVATE payload" not in result

    def test_escape_free_payload_fails_closed(self, monkeypatch):
        """With no styling anywhere, the block stays open — payload is never
        published, even though that means refusing the turn."""

        pane = "\n".join(
            [
                "💫 Read the report.",
                "● Used Read (report.txt) · 3 lines",
                "───────",
                "PRIVATE tool payload",
                "● Public answer",
                "",
            ]
        )
        try:
            result, _ = _last(monkeypatch, pane)
        except _rejected():
            return
        assert "PRIVATE tool payload" not in result

    def test_real_capture_still_extracts_exactly_the_answer(self, monkeypatch):
        pane = _fixture("kimi_code_0431_11_mcp_tool_turn_source_e2e.txt")
        result, _ = _last(monkeypatch, pane)
        assert result.startswith("● MCP-OK=")
        assert "find_profiles" not in result
        assert "structuredContent" not in result
        assert "Zero profiles returned" not in result


# =============================================================================
# Maintainer P2-B / Codex #10 — context-free UI collisions
# =============================================================================


class TestPR799AdversarialProseCollisions:
    """A single natural-language row must not become response-ending UI state."""

    def test_numbered_procedure_is_not_an_approval_dialog(self, monkeypatch):
        pane = "\n".join(
            [
                "💫 Give me the steps.",
                "● Steps:",
                "1. Approve the plan.",
                "2. Run the deployment.",
                "Done.",
                "",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert "1. Approve the plan." in result
        assert "2. Run the deployment." in result
        assert "Done." in result

    def test_context_metric_prose_is_not_a_footer(self, monkeypatch):
        pane = "\n".join(
            [
                "💫 Explain the metric.",
                "● context: 50% means half the budget is used.",
                "Nothing else follows.",
                "",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert "context: 50% means half the budget is used." in result
        assert "Nothing else follows." in result

    def test_quoted_trust_menu_does_not_truncate(self, monkeypatch):
        pane = "\n".join(
            [
                "💫 Show me the dialog.",
                "● Menu example:",
                "❯ Trust this folder",
                "Continue with the next step.",
                "",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert "❯ Trust this folder" in result
        assert "Continue with the next step." in result

    def test_fenced_quoted_reject_option_does_not_truncate(self, monkeypatch):
        pane = "\n".join(
            [
                "💫 Show me the dialog.",
                "● Example:",
                "```",
                "❯ Don't trust",
                "```",
                "After the fence.",
                "",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert "❯ Don't trust" in result
        assert "After the fence." in result

    def test_project_mcp_targets_prose_does_not_truncate(self, monkeypatch):
        pane = "\n".join(
            [
                "💫 Explain.",
                "Project MCP targets: are documented here.",
                "More explanation.",
                "",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert "Project MCP targets: are documented here." in result
        assert "More explanation." in result

    def test_real_trust_dialog_is_still_detected_and_ends_the_region(self):
        pane = _fixture("kimi_code_0431_07_workspace_trust_dialog_plain.txt")
        dialog = kt.detect_trust_dialog(pane.split("\n"))
        assert dialog is not None
        assert dialog.selected_option == kt.TRUST_OPTION_TRUST
        kinds = kt.classify_rows(pane.split("\n"))
        assert kt.KimiLineKind.TRUST_DIALOG in kinds

    def test_real_footer_is_still_chrome(self):
        rows = [" \x1b[38;5;253mcontext: 4% (32.1k/977k)\x1b[39m"]
        assert kt.classify_line(rows[0]) is kt.KimiLineKind.STATUS_FOOTER


# =============================================================================
# Codex #12 — user-echo inference
# =============================================================================


class TestPR799AdversarialUserEcho:
    """Submission start and continuation need different evidence."""

    def test_legacy_table_answer_is_preserved(self, monkeypatch):
        pane = "\n".join(["💫 Return a table", "Name | Value", "A | 1", "💫", ""])
        result, _ = _last(monkeypatch, pane)
        assert result == "Name | Value\nA | 1"

    def test_wrapped_kimi_code_input_is_still_excluded(self, monkeypatch):
        pane = "\n".join(
            [
                "\x1b[1m\x1b[38;5;222m✨ Do not create, assign,\x1b[0m",
                "    \x1b[1;38;5;222mhand off, message, or delete anything.\x1b[22m\x1b[39m",
                "",
                _answer("Understood."),
                "",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert result == "● Understood."

    def test_colour_222_row_inside_an_answer_does_not_move_the_start(self, monkeypatch):
        """A colour-222 row is continuation evidence only, never a new start."""

        pane = "\n".join(
            [
                "💫 Write code.",
                _answer("Here is the snippet:"),
                "    \x1b[38;5;222mcolour-222 code line\x1b[39m",
                "trailing prose",
                "",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert "Here is the snippet:" in result
        assert "colour-222 code line" in result
        assert "trailing prose" in result

    def test_multiline_submission_starting_with_a_sparkle(self, monkeypatch):
        pane = "\n".join(
            [
                "\x1b[1m\x1b[38;5;222m✨ line one of the request\x1b[0m",
                "    \x1b[1;38;5;222mline two of the request\x1b[0m",
                "    \x1b[1;38;5;222mline three of the request\x1b[0m",
                "",
                _answer("Done."),
                "",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert result == "● Done."

    def test_answer_immediately_after_a_submission(self, monkeypatch):
        pane = "\n".join(["✨ go", _answer("Immediate answer."), ""])
        result, _ = _last(monkeypatch, pane)
        assert result == "● Immediate answer."


# =============================================================================
# Codex #13 — Braille membership is not spinner evidence
# =============================================================================


class TestPR799AdversarialBrailleCollision:
    """The indicator is a braille glyph in the spinner slot, not anywhere."""

    @pytest.mark.parametrize(
        "row",
        [
            "● The Braille letter A is ⠁.",
            "● The spinner glyph is ⠋ in the TUI.",
            "    return '⠙'  # braille in code",
        ],
    )
    def test_braille_in_prose_is_not_a_spinner(self, row):
        assert kt.classify_line(row) is not kt.KimiLineKind.LIVE_SPINNER

    def test_braille_prose_stays_in_the_answer(self, monkeypatch):
        pane = "\n".join(["💫 What is this glyph?", "● The Braille letter A is ⠁.", ""])
        result, _ = _last(monkeypatch, pane)
        assert "⠁" in result

    def test_real_spinner_rows_are_still_detected(self):
        assert (
            kt.classify_line("\x1b[38;5;111m⠙\x1b[39m working…", semantics=kt.SpinnerSemantics.CODE)
            is kt.KimiLineKind.LIVE_SPINNER
        )
        assert (
            kimi_cli_module._is_live_turn_spinner_line("\x1b[38;5;111m⠙\x1b[39m working…") is True
        )

    def test_boot_chrome_braille_is_not_a_live_turn(self):
        assert kt.classify_line("⠧ MCP Servers: 0/1 connected") is kt.KimiLineKind.BOOT_CHROME


# =============================================================================
# Codex #14 — shell-neutral transport for every typed token
# =============================================================================


class TestPR799AdversarialShellTransport:
    """POSIX quoting is not fish quoting, so dynamic values are not quoted.

    ``shlex.quote`` emits ``'\\''`` for an embedded apostrophe, which fish ends
    early when a backslash precedes it, and ``\\\\`` means one backslash in fish
    but two in POSIX sh. Every token typed at the pane is therefore drawn from
    :data:`SHELL_SAFE_CHARS`, with POSIX text in a CAO-owned script.
    """

    HOSTILE_NAMES = [
        "sp ace",
        "apo'strophe",
        "back\\slash",
        "back\\'quote",
        "multi\\\\backslash",
        "dol$lar",
        "semi;colon",
        "tick`mark",
        'dq"uote',
        "par(en)s",
        "bra[ck]ets",
        "uni\u00e9\u4e2d",
    ]

    @pytest.mark.parametrize("name", HOSTILE_NAMES)
    def test_probe_tokens_are_all_shell_safe(self, name, tmp_path):
        provider = KimiCliProvider("t-shell", "s", "w")
        hostile = tmp_path / name
        hostile.mkdir()
        provider._temp_dir = str(hostile)

        directory = provider._ensure_shell_safe_dir()
        script = provider._write_private_script(
            directory, "kimi-probe.sh", kimi_cli_module.KIMI_PROBE_PROGRAM
        )
        command = kimi_cli_module.build_kimi_probe_command(
            script, os.path.join(directory, "kimi-probe.txt")
        )

        for token in shlex.split(command):
            assert kimi_cli_module.is_shell_safe_token(token), (name, token)
        assert str(hostile) not in command
        assert "${" not in command
        assert "'" not in command
        assert "\\" not in command

    def test_launch_tokens_are_all_shell_safe_with_a_hostile_model_name(self, tmp_path):
        """``--model`` is operator-supplied, so the launch line must not be typed."""

        provider = KimiCliProvider("t-shell2", "s", "w")
        provider._kimi_binary = "/usr/bin/kimi"
        provider._dialect = kimi_cli_module.KimiDialect.CODE
        provider._kimi_source_home = tmp_path / "src"
        provider._model = "weird\\'model; rm -rf /"

        launch_line = provider._build_kimi_code_command()
        assert "'" in launch_line or "\\" in launch_line  # POSIX quoting is present

        pane_command = provider._materialize_launch_command(launch_line)
        for token in shlex.split(pane_command):
            assert kimi_cli_module.is_shell_safe_token(token), token
        assert "model" not in pane_command
        assert "\\" not in pane_command

    @pytest.mark.parametrize("name", ["back\\'quote", "multi\\\\backslash", "sp ace"])
    def test_both_commands_execute_under_fish(self, name, tmp_path):
        fish = shutil.which("fish")
        if fish is None:
            pytest.skip("fish is not installed")

        provider = KimiCliProvider("t-shell3", "s", "w")
        hostile = tmp_path / name
        hostile.mkdir()
        provider._temp_dir = str(hostile)

        directory = provider._ensure_shell_safe_dir()
        probe_path = os.path.join(directory, "kimi-probe.txt")
        script = provider._write_private_script(
            directory, "kimi-probe.sh", kimi_cli_module.KIMI_PROBE_PROGRAM
        )
        probe_command = kimi_cli_module.build_kimi_probe_command(script, probe_path)

        result = subprocess.run(
            [fish, "--no-config", "-c", probe_command],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        )
        assert result.returncode == 0, result.stderr
        assert kimi_cli_module.KIMI_PROBE_END_MARKER in Path(probe_path).read_text(encoding="utf-8")

        # The same transport for the launch line. The line is POSIX, so it is
        # quoted the way the provider quotes it.
        launch_line = "env KIMI_CODE_HOME=" + shlex.quote(str(hostile)) + " /bin/echo launched"
        pane_command = provider._materialize_launch_command(launch_line)
        result = subprocess.run(
            [fish, "--no-config", "-c", pane_command],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        )
        assert result.returncode == 0, result.stderr
        assert "launched" in result.stdout

    def test_posix_control_still_works(self, tmp_path):
        provider = KimiCliProvider("t-shell4", "s", "w")
        provider._temp_dir = str(tmp_path)
        directory = provider._ensure_shell_safe_dir()
        probe_path = os.path.join(directory, "kimi-probe.txt")
        script = provider._write_private_script(
            directory, "kimi-probe.sh", kimi_cli_module.KIMI_PROBE_PROGRAM
        )
        command = kimi_cli_module.build_kimi_probe_command(script, probe_path)
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        )
        assert result.returncode == 0, result.stderr
        assert kimi_cli_module.KIMI_PROBE_END_MARKER in Path(probe_path).read_text(encoding="utf-8")


# =============================================================================
# Codex #15 — credential symlinks must not survive into the runtime home
# =============================================================================


class TestPR799AdversarialCredentialIsolation:
    """Secret state must not keep a writable path back into shared state."""

    def _source_home(self, root: Path) -> Path:
        source = root / "src"
        source.mkdir()
        (source / "config.toml").write_text("x = 1\n", encoding="utf-8")
        creds = source / "credentials"
        creds.mkdir()
        (creds / "plain.json").write_text('{"token":"PLAIN"}\n', encoding="utf-8")
        return source

    @pytest.mark.parametrize("link_kind", ["relative", "absolute"])
    def test_writing_the_runtime_copy_cannot_mutate_the_target(self, tmp_path, link_kind):
        source = self._source_home(tmp_path)
        shared = tmp_path / "shared-token.json"
        shared.write_text('{"token":"ORIGINAL"}\n', encoding="utf-8")
        link = source / "credentials" / "token.json"
        link.symlink_to(shared if link_kind == "absolute" else Path("../shared-token.json"))

        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "runtime").build(None)
        runtime_token = result.home / "credentials" / "token.json"

        assert not runtime_token.is_symlink()
        if runtime_token.exists():
            runtime_token.write_text('{"token":"MUTATED"}\n', encoding="utf-8")
        assert shared.read_text(encoding="utf-8") == '{"token":"ORIGINAL"}\n'

    def test_no_symlink_survives_anywhere_under_credentials(self, tmp_path):
        source = self._source_home(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "target.json").write_text("{}", encoding="utf-8")
        (source / "credentials" / "abs.json").symlink_to(outside / "target.json")
        (source / "credentials" / "dangling.json").symlink_to(tmp_path / "nope")
        (source / "credentials" / "linkdir").symlink_to(outside, target_is_directory=True)

        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "runtime").build(None)
        creds = result.home / "credentials"
        for root, dirnames, filenames in os.walk(creds):
            assert not Path(root).is_symlink(), root
            for entry in list(dirnames) + list(filenames):
                assert not (Path(root) / entry).is_symlink(), (root, entry)

    def test_ordinary_credential_file_is_still_copied(self, tmp_path):
        source = self._source_home(tmp_path)
        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "runtime").build(None)
        copied = result.home / "credentials" / "plain.json"
        assert copied.is_file()
        assert copied.read_text(encoding="utf-8") == '{"token":"PLAIN"}\n'
        assert (copied.stat().st_mode & 0o777) == 0o600


# =============================================================================
# Codex P3 — trust traversal budget must bound enumeration
# =============================================================================


class TestPR799AdversarialTrustTraversalBound:
    """The budget must bound the scandir iterator, not just the copy loop."""

    def _counting_scandir(self, watched: Path, counter: dict):
        real = os.scandir

        class _Counting:
            def __init__(self, cm):
                self._cm = cm

            def __enter__(self):
                iterator = self._cm.__enter__()

                class _It:
                    def __iter__(self_inner):
                        return self_inner

                    def __next__(self_inner):
                        value = next(iterator)
                        counter["n"] += 1
                        return value

                return _It()

            def __exit__(self, *exc):
                return self._cm.__exit__(*exc)

        def _scandir(path, *args, **kwargs):
            if str(path) == str(watched):
                return _Counting(real(path, *args, **kwargs))
            return real(path, *args, **kwargs)

        return _scandir

    def test_enumeration_is_bounded_by_the_budget(self, tmp_path, monkeypatch):
        source = tmp_path / "src"
        source.mkdir()
        (source / "config.toml").write_text("x = 1\n", encoding="utf-8")
        trust = source / "workspace-trust"
        trust.mkdir()
        total = krh.MAX_TRUST_ENTRIES * 3 + 7
        for index in range(total):
            (trust / f"rec{index:06d}").write_text("x", encoding="utf-8")

        counter = {"n": 0}
        monkeypatch.setattr(os, "scandir", self._counting_scandir(trust, counter))

        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "runtime").build(None)

        assert len(result.trust_records) <= krh.MAX_TRUST_ENTRIES
        assert counter["n"] <= krh.MAX_TRUST_ENTRIES + 1, counter["n"]
        assert counter["n"] < total

    def test_truncation_is_still_reported(self, tmp_path, monkeypatch):
        source = tmp_path / "src2"
        source.mkdir()
        (source / "config.toml").write_text("x = 1\n", encoding="utf-8")
        trust = source / "workspace-trust"
        trust.mkdir()
        for index in range(krh.MAX_TRUST_ENTRIES + 5):
            (trust / f"rec{index:06d}").write_text("x", encoding="utf-8")

        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "runtime2").build(None)
        assert result.trust_truncated is True
        assert len(result.trust_records) == krh.MAX_TRUST_ENTRIES


# =============================================================================
# Fresh independent review, round 1 — four regressions in this closure
# =============================================================================


class TestPR799AdversarialReviewRound1:
    """Findings raised by a second, independent adversarial review.

    All four were reproduced against the closure commit before being fixed, and
    three of them are regressions the closure itself introduced: a row's *text*
    was still enough to make it UI state in the places the first pass missed.
    """

    def test_italic_final_answer_after_reasoning_is_preserved(self, monkeypatch):
        """The answer colour is decisive; italic emphasis is not reasoning.

        Kimi italicises emphasis *within* an answer, so an emphasised answer
        bullet immediately after reasoning also carries italic. The reasoning
        block absorbed it and the turn was refused as reasoning-only.
        """

        pane = "\n".join(
            [
                "💫 Task",
                "\x1b[38;5;244m• \x1b[3mPRIVATE REASONING\x1b[0m",
                "\x1b[38;5;253m• \x1b[39m\x1b[3mFINAL ANSWER\x1b[0m",
                "💫",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert "FINAL ANSWER" in result
        assert "PRIVATE REASONING" not in result

    def test_reasoning_continuation_is_still_absorbed(self):
        """The guard for the fix above: real reasoning styling still absorbs."""

        assert kt.is_reasoning_continuation("\x1b[38;5;244m\x1b[3mprivate\x1b[0m") is True
        assert kt.is_reasoning_continuation("\x1b[38;5;253m• \x1b[39m\x1b[3manswer\x1b[0m") is False

    def test_quoted_approval_prompt_does_not_truncate(self, monkeypatch):
        """A sentence quoting the prompt must not confirm itself as the dialog."""

        pane = "\n".join(
            [
                "💫 Task",
                "• First answer.",
                "The dialog says ▶ Run this command? before execution.",
                "Critical remaining answer.",
                "💫",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert "▶ Run this command?" in result
        assert "Critical remaining answer." in result

    def test_prose_mentioning_two_footer_tips_does_not_truncate(self, monkeypatch):
        pane = "\n".join(
            [
                "💫 Task",
                "• First answer.",
                "Use ctrl-o to hide or reveal tool output and shift-tab to Plan mode.",
                "Critical remaining answer.",
                "💫",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert "ctrl-o to hide or reveal tool output" in result
        assert "Critical remaining answer." in result

    def test_real_approval_dialog_is_still_confirmed(self):
        pane = _fixture("kimi_code_0431_08_command_approval_dialog.txt")
        kinds = kt.classify_rows(pane.split("\n"))
        assert kt.KimiLineKind.APPROVAL_DIALOG in kinds

    def test_real_footer_tip_row_is_still_chrome(self):
        # A bare tip row is the whole-row shape.
        assert (
            kt.classify_line("  ctrl-o to hide or reveal tool output")
            is kt.KimiLineKind.STATUS_FOOTER
        )
        # And the measured status rows, which carry a field plus a tip, still
        # classify — the tip is corroborating, not decorative.
        for name, index in (
            ("kimi_code_0431_03_final_answer.txt", 41),
            ("kimi_code_0431_01_fresh_startup_idle.txt", 26),
            ("kimi_code_0431_04_post_answer_idle.txt", 41),
            ("kimi_code_0431_05_mcp_startup.txt", 31),
        ):
            row = _fixture(name).split("\n")[index - 1]
            assert kt.classify_line(row) is kt.KimiLineKind.STATUS_FOOTER, name

    def test_tip_description_prose_is_not_a_footer(self):
        """A sentence naming a tip is prose; it has no measured status field."""

        assert (
            kt.classify_line(
                "shift-tab to Plan mode to review the approach before Kimi edits files."
            )
            is kt.KimiLineKind.CONTENT
        )

    def test_real_status_row_with_a_tip_is_still_a_footer(self):
        row = (
            " Never Ask  A3 Probe thinking  …/proj  master  "
            "shift-tab to Plan mode  context: 4.0% (10.4k/262.1k)"
        )
        assert kt.classify_line(row) is kt.KimiLineKind.STATUS_FOOTER

    def test_unreadable_trust_record_does_not_abort_the_launch(self, tmp_path):
        """One unreadable record degrades to "not inherited", not "no launch"."""

        source = tmp_path / "src"
        source.mkdir()
        (source / "config.toml").write_text("x = 1\n", encoding="utf-8")
        trust = source / "workspace-trust"
        trust.mkdir()
        (trust / "wd_good").write_text("record\n", encoding="utf-8")
        unreadable = trust / "wd_unreadable"
        unreadable.write_text("record\n", encoding="utf-8")
        os.chmod(unreadable, 0o000)
        try:
            result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "runtime").build(None)
        finally:
            os.chmod(unreadable, 0o600)

        assert "wd_good" in result.trust_records
        assert "wd_unreadable" not in result.trust_records
        assert "wd_unreadable" in result.trust_skipped

    def test_record_vanishing_between_enumeration_and_copy_is_skipped(self, tmp_path, monkeypatch):
        source = tmp_path / "src2"
        source.mkdir()
        (source / "config.toml").write_text("x = 1\n", encoding="utf-8")
        trust = source / "workspace-trust"
        trust.mkdir()
        (trust / "wd_a").write_text("record\n", encoding="utf-8")

        real_copyfile = shutil.copyfile
        monkeypatch.setattr(
            krh.shutil,
            "copyfile",
            MagicMock(
                side_effect=lambda src, dst, **kw: (
                    (_ for _ in ()).throw(FileNotFoundError("removed mid-scan"))
                    if "workspace-trust" in str(src)
                    else real_copyfile(src, dst, **kw)
                )
            ),
        )
        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "runtime2").build(None)
        assert result.trust_records == []
        assert "wd_a" in result.trust_skipped


# =============================================================================
# Latest-main integration — Agent Plugins MCP delivery on both dialects
# =============================================================================


class TestPR799AdversarialPluginMcpDelivery:
    """Both Kimi dialects must consume the plugin-augmented profile.

    ``with_plugin_mcp`` is the launch-time seam every provider that re-reads its
    profile at launch passes through — the upstream Agent Plugins work exists
    precisely because providers re-read and would otherwise discard the
    install-time merge. Upstream applied it to the two legacy Kimi sites; the
    Kimi Code builder loads the profile at its own site, so without it the merged
    ``mcp.json`` is built from the profile alone and every installed plugin's MCP
    servers are silently missing from this dialect.
    """

    @staticmethod
    def _code_provider(tmp_path, monkeypatch, profile):
        monkeypatch.setattr(kimi_cli_module, "load_agent_profile", lambda name: profile)
        provider = KimiCliProvider("term-plugin", "s", "w", agent_profile="dev")
        provider._kimi_binary = "/usr/bin/kimi"
        provider._dialect = kimi_cli_module.KimiDialect.CODE
        provider._temp_dir = str(tmp_path)
        provider._kimi_source_home = tmp_path / "src-home"
        (tmp_path / "src-home").mkdir(exist_ok=True)
        return provider

    @staticmethod
    def _profile(servers):
        profile = MagicMock()
        profile.model = None
        profile.system_prompt = None
        profile.name = "dev"
        profile.mcpServers = dict(servers)
        return profile

    def test_kimi_code_merges_plugin_servers_into_the_runtime_mcp_json(self, tmp_path, monkeypatch):
        profile = self._profile({"profile-server": {"command": "srv"}})
        seen = {}

        def fake_with_plugin_mcp(loaded, provider=None):
            seen["provider"] = provider
            merged = dict(loaded.mcpServers or {})
            merged["plugin-server"] = {"command": "plugin-srv", "args": []}
            loaded.mcpServers = merged
            return loaded

        monkeypatch.setattr(kimi_cli_module, "_with_plugin_mcp", fake_with_plugin_mcp)
        provider = self._code_provider(tmp_path, monkeypatch, profile)

        provider._build_kimi_code_command()

        assert seen.get("provider") == "kimi_cli"
        mcp_doc = json.loads(
            (provider._managed_runtime_home() / "mcp.json").read_text(encoding="utf-8")
        )
        servers = mcp_doc["mcpServers"]
        assert "plugin-server" in servers, sorted(servers)
        assert "profile-server" in servers, sorted(servers)

    def test_legacy_dialect_still_merges_plugin_servers(self, tmp_path, monkeypatch):
        """The upstream legacy behaviour is preserved, not replaced."""

        profile = self._profile({})
        seen = {}

        def fake_with_plugin_mcp(loaded, provider=None):
            seen["provider"] = provider
            loaded.mcpServers = {"plugin-server": {"command": "plugin-srv"}}
            return loaded

        monkeypatch.setattr(kimi_cli_module, "_with_plugin_mcp", fake_with_plugin_mcp)
        provider = KimiCliProvider("term-plugin-legacy", "s", "w", agent_profile="dev")
        provider._temp_dir = str(tmp_path / "legacy")
        Path(provider._temp_dir).mkdir()
        monkeypatch.setattr(kimi_cli_module, "load_agent_profile", lambda name: profile)

        command = provider._build_kimi_command("/usr/local/bin/kimi")

        assert seen.get("provider") == "kimi_cli"
        assert "plugin-server" in command

    def test_profile_loader_is_called_once_per_build(self, tmp_path, monkeypatch):
        """No double-merge: each build re-reads and wraps exactly once."""

        calls = {"load": 0, "wrap": 0}
        profile = self._profile({})

        def counting_load(name):
            calls["load"] += 1
            return profile

        def counting_wrap(loaded, provider=None):
            calls["wrap"] += 1
            return loaded

        provider = self._code_provider(tmp_path, monkeypatch, profile)
        # Applied after the fixture's own loader patch, which would otherwise win.
        monkeypatch.setattr(kimi_cli_module, "load_agent_profile", counting_load)
        monkeypatch.setattr(kimi_cli_module, "_with_plugin_mcp", counting_wrap)

        provider._build_kimi_code_command()

        assert calls == {"load": 1, "wrap": 1}, calls


# =============================================================================
# Second-review residuals — reasoning/chrome, blank-separated blocks,
# composer shape, and the retryable / non-retryable split
# =============================================================================


class TestPR799AdversarialRound2Residuals:
    """The five findings of the second fresh independent review.

    Four share one root: a block's lifetime was decided by layout (a blank row,
    or a single row's shape) instead of by positive renderer evidence, and the
    refusal/retry decision was made by which raise site ran rather than by what
    the region positively contained.
    """

    # --- R2-1: reasoning plus chrome must still refuse --------------------

    @pytest.mark.parametrize(
        "chrome",
        [
            [_footer()],
            ["╭────────────╮", "│ >          │", "╰────────────╯"],
            [""],
            ["", _footer()],
        ],
    )
    def test_reasoning_beside_chrome_is_refused_without_raw_fallback(self, monkeypatch, chrome):
        pane = "\n".join([_thinking(PRIVATE_REASONING), *chrome])
        with pytest.raises(_rejected()) as excinfo:
            _last(monkeypatch, pane)
        message = str(excinfo.value)
        assert PRIVATE_REASONING not in message
        assert "[NO RESPONSE" not in message

    def test_reasoning_beside_chrome_does_not_escalate(self, monkeypatch):
        from cli_agent_orchestrator.services import terminal_service

        pane = "\n".join([_thinking(PRIVATE_REASONING), _footer()])
        provider = KimiCliProvider("term-r21", "s", "w")
        backend = MagicMock()
        backend.get_history.return_value = pane
        monkeypatch.setattr(
            terminal_service,
            "get_terminal_metadata",
            lambda tid: {"tmux_session": "s", "tmux_window": "w"},
        )
        monkeypatch.setattr(terminal_service.status_monitor, "get_buffer", lambda tid: pane)
        monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
        monkeypatch.setattr(terminal_service.provider_manager, "get_provider", lambda tid: provider)

        with pytest.raises(_rejected()):
            terminal_service.get_output("term-r21", terminal_service.OutputMode.LAST)
        assert backend.get_history.call_count == 1

    def test_reasoning_before_a_real_answer_is_published(self, monkeypatch):
        """The guard: a valid turn is not refused because it reasoned first."""

        pane = "\n".join(
            [
                "💫 Task",
                _thinking(PRIVATE_REASONING),
                "",
                _answer("Public answer"),
                "",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert result == "● Public answer"
        assert PRIVATE_REASONING not in result

    # --- R2-2: a blank paragraph is not the end of reasoning --------------

    def test_blank_separated_reasoning_paragraph_is_excluded(self, monkeypatch):
        pane = "\n".join(
            [
                "💫 Task",
                _thinking("Private heading"),
                "",
                _reasoning_continuation(PRIVATE_REASONING),
                _answer("Public answer"),
                "",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert result == "● Public answer"
        assert PRIVATE_REASONING not in result

    def test_many_blank_separated_reasoning_paragraphs_are_excluded(self, monkeypatch):
        pane = "\n".join(
            [
                "💫 Task",
                _thinking("Private heading"),
                "",
                _reasoning_continuation("private one"),
                "",
                _reasoning_continuation("private two"),
                "",
                _answer("Public answer"),
                "",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert result == "● Public answer"

    def test_reasoning_blank_chrome_is_refused(self, monkeypatch):
        pane = "\n".join(
            [
                _thinking("Private heading"),
                "",
                _reasoning_continuation(PRIVATE_REASONING),
                "",
                _footer(),
            ]
        )
        with pytest.raises(_rejected()) as excinfo:
            _last(monkeypatch, pane)
        assert PRIVATE_REASONING not in str(excinfo.value)

    # --- R2-3: multiline submissions --------------------------------------

    def test_blank_separated_user_paragraph_is_excluded(self, monkeypatch):
        pane = "\n".join(
            [
                _user("✨ Summarize the report"),
                "",
                _user("PRIVATE USER PARAGRAPH"),
                _answer("Public answer"),
                "",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert result == "● Public answer"
        assert "PRIVATE USER PARAGRAPH" not in result

    def test_user_bullet_continuation_is_excluded(self, monkeypatch):
        """A pasted list in a submission is still the submission."""

        pane = "\n".join(
            [
                _user("✨ Summarize these items"),
                _user("● PRIVATE USER ITEM"),
                _answer("Public answer"),
                "",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert result == "● Public answer"
        assert "PRIVATE USER ITEM" not in result

    def test_colour_222_row_inside_an_answer_is_not_a_submission(self, monkeypatch):
        """The control: colour 222 elsewhere in assistant output is content."""

        pane = "\n".join(
            [
                "💫 Write code.",
                _answer("Here is the snippet:"),
                "    \x1b[38;5;222mcolour-222 code line\x1b[39m",
                "trailing prose",
                "",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert "Here is the snippet:" in result
        assert "colour-222 code line" in result
        assert "trailing prose" in result

    def test_legacy_answer_prose_after_a_submission_is_not_absorbed(self, monkeypatch):
        """A blank cannot pull ordinary legacy answer prose into the submission."""

        rows = ["✨ summarise", "", '{"name":"a",', "    indented prose line", _answer("FINAL")]
        kinds = kt.classify_rows(rows, semantics=kt.SpinnerSemantics.CODE)
        assert kinds[2] is kt.KimiLineKind.CONTENT
        assert kinds[3] is kt.KimiLineKind.CONTENT

    # --- R2-4: composer needs frame context -------------------------------

    @pytest.mark.parametrize("operator", [">", "<", ">>", ">=", "| > |"])
    def test_markdown_table_row_is_not_a_composer(self, monkeypatch, operator):
        pane = "\n".join(
            [
                "💫 Explain shell operators",
                "• Operators:",
                "| Operator | Meaning |",
                "| --- | --- |",
                f"| {operator} | Redirect stdout |",
                "Use these carefully.",
                "💫",
                "",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert "Use these carefully." in result
        assert "| --- | --- |" in result
        assert f"| {operator} | Redirect stdout |" in result

    def test_a_real_composer_still_ends_the_region(self):
        """The control: a framed prompt row is chrome and stays an anchor."""

        rows = [
            *["x"] * 5,
            _answer("The answer"),
            " ╭────────────────────╮",
            " │ >                  │",
            " ╰────────────────────╯",
        ]
        kinds = kt.classify_rows(rows, semantics=kt.SpinnerSemantics.CODE)
        assert kinds[7] is kt.KimiLineKind.READY_INPUT_FRAME

    def test_unframed_prompt_shaped_row_is_content(self):
        rows = [_answer("The answer"), "| > | Redirect stdout |"]
        kinds = kt.classify_rows(rows, semantics=kt.SpinnerSemantics.CODE)
        assert kinds[1] is kt.KimiLineKind.CONTENT

    # --- R2-5: a missing anchor must stay retryable -----------------------

    def test_a_wider_capture_recovers_the_answer(self, monkeypatch):
        """A small capture that lacks the echo must not be terminal."""

        rows = _fixture("kimi_code_0431_03_final_answer.txt").split("\n")
        kinds = kt.classify_rows(rows, semantics=kt.SpinnerSemantics.CODE)
        index = kinds.index(kt.KimiLineKind.FINAL_BULLET)
        rows[index + 1 : index + 1] = ["Continuation of the public answer."] * 220
        pane = "\n".join(rows)

        from cli_agent_orchestrator.services import terminal_service

        provider = KimiCliProvider("term-r25", "s", "w")
        provider._dialect = kimi_cli_module.KimiDialect.CODE
        backend = MagicMock()
        backend.get_history.side_effect = lambda *a, **kw: (
            "\n".join(rows[-kw["tail_lines"] :]) if "tail_lines" in kw else pane
        )
        monkeypatch.setattr(
            terminal_service,
            "get_terminal_metadata",
            lambda tid: {"tmux_session": "s", "tmux_window": "w"},
        )
        monkeypatch.setattr(terminal_service.status_monitor, "get_buffer", lambda tid: pane)
        monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
        monkeypatch.setattr(terminal_service.provider_manager, "get_provider", lambda tid: provider)

        result = terminal_service.get_output("term-r25", terminal_service.OutputMode.LAST)

        assert backend.get_history.call_count > 1
        assert "● STEP 1" in result
        assert "Continuation of the public answer." in result

    def test_chrome_only_region_is_retryable(self):
        """Pure chrome is a missed anchor, not a refusal.

        Asserted at the extractor, because the public path's response to a
        retryable failure is to escalate and then return a labelled fallback —
        which is exactly the behaviour the wider-capture case above relies on.
        """

        provider = KimiCliProvider("term-r25b", "s", "w")
        with pytest.raises(OutputExtractionError) as excinfo:
            provider.extract_last_message_from_script(_footer())
        assert not isinstance(excinfo.value, _rejected())

    def test_tool_payload_only_region_is_not_republished(self, monkeypatch):
        """The fail-closed half is preserved: payload is never the answer."""

        pane = "\n".join(
            [
                "💫 Read the report.",
                "● Used Read (report.txt) · 3 lines",
                "───────",
                "PRIVATE tool payload",
                "● Public answer",
                "",
            ]
        )
        with pytest.raises(_rejected()) as excinfo:
            _last(monkeypatch, pane)
        assert "PRIVATE tool payload" not in str(excinfo.value)


# =============================================================================
# Renderer palette — the measured colours depend on the launch environment
# =============================================================================


class TestPR799AdversarialRendererPalette:
    """Both dialects must launch the TUI on the palette the extractor measured.

    Found by the live E2E against real Kimi Code 2.0.2 on a host advertising
    ``COLORTERM=truecolor``: the inherited capability switched the renderer to
    24-bit colour, the final-answer bullet rendered ``38;2;224;224;224`` — a
    near-grey that the fail-closed reasoning rule reads as reasoning — the answer
    colour 253 never appeared, and ``mode=last`` degraded to the raw-transcript
    fallback. The user echo moved to ``38;2;255;203;107`` in the same switch, so
    submission continuation styling was lost as well. ``TERM`` was already
    pinned; the 24-bit capability is removed the same way.
    """

    def test_kimi_code_launch_removes_the_ambient_capability(self, tmp_path, monkeypatch):
        profile = MagicMock()
        profile.model = None
        profile.system_prompt = None
        profile.name = "dev"
        profile.mcpServers = {}
        monkeypatch.setattr(kimi_cli_module, "load_agent_profile", lambda name: profile)
        provider = KimiCliProvider("term-palette", "s", "w", agent_profile="dev")
        provider._kimi_binary = "/usr/bin/kimi"
        provider._dialect = kimi_cli_module.KimiDialect.CODE
        provider._temp_dir = str(tmp_path)
        provider._kimi_source_home = tmp_path / "src-home"
        (tmp_path / "src-home").mkdir(exist_ok=True)

        command = provider._build_kimi_code_command()

        assert "-u COLORTERM" in command, command

    def test_legacy_launch_removes_the_ambient_capability(self, tmp_path, monkeypatch):
        provider = KimiCliProvider("term-palette-legacy", "s", "w")
        provider._temp_dir = str(tmp_path / "legacy")
        Path(provider._temp_dir).mkdir()

        command = provider._build_kimi_command("/usr/local/bin/kimi")

        assert "-u COLORTERM" in command, command

    def test_truecolor_answer_bullet_is_not_publishable(self):
        """Why the capability must go: the near-grey answer reads as reasoning.

        This is the measured 2.0.2 rendering, not a synthetic shape — the fix is
        to prevent the palette switch, and this case documents that the extractor
        alone cannot recover it (the fail-closed direction is deliberate).
        """

        pane = "\n".join(
            [
                "\x1b[1m\x1b[38;2;255;203;107m✨ Task\x1b[0m",
                " \x1b[38;2;224;224;224m● \x1b[39mMCP-OK=2",
                "",
            ]
        )
        kinds = kt.classify_rows(pane.split("\n"), semantics=kt.SpinnerSemantics.CODE)
        assert kt.KimiLineKind.THINKING_BULLET in kinds
        assert kt.KimiLineKind.FINAL_BULLET not in kinds


class TestPR799AdversarialThirdRound:
    """The three findings of the third fresh independent review.

    All three were reproduced before the fix, against the commit that closed the
    second round.
    """

    # --- F1 (P1): an answer-shaped chrome row must not defeat the refusal ---

    def test_reasoning_with_chrome_elsewhere_still_refuses(self, monkeypatch):
        """The shell preamble and boot banner classify as CONTENT.

        The refusal used to return early whenever *any* row of the capture was
        answer-shaped, and answer-shaped is not answer. A turn showing reasoning
        and no answer therefore raised the retryable error instead, exhausted the
        escalation and republished the reasoning inside the raw pane.
        """

        rows = _fixture("kimi_code_0431_12_mcp_tool_turn_reasoning_first.txt").split("\n")
        pane = "\n".join(row for row in rows if "\x1b[38;5;253m●" not in row)

        with pytest.raises(_rejected()) as excinfo:
            _last(monkeypatch, pane)

        message = str(excinfo.value)
        assert "I need to call find_profiles once" not in message
        assert "[NO RESPONSE" not in message

    def test_reasoning_beside_a_chrome_row_outside_the_region_still_refuses(self, monkeypatch):
        """The footer's own wrapped row classifies as CONTENT, and sits outside
        the located region — it must not exempt the capture from the refusal."""

        pane = "\n".join(
            [
                _user("✨ Summarize the report"),
                _thinking(PRIVATE_REASONING),
                " ╭────────────────────╮",
                " │ >                  │",
                " ╰────────────────────╯",
                "Never Ask  Some Model thinking: high  /tmp/project  master",
                "",
            ]
        )
        with pytest.raises(_rejected()) as excinfo:
            _last(monkeypatch, pane)
        assert PRIVATE_REASONING not in str(excinfo.value)

    def test_an_answer_in_the_region_is_still_published(self, monkeypatch):
        """The guard: the refusal must not swallow a region that has an answer."""

        pane = "\n".join(
            [
                "💫 Task",
                _thinking(PRIVATE_REASONING),
                _answer("Public answer"),
                "",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert result == "● Public answer"

    # --- F2 (P2): tool-ish prose must stay an answer -----------------------

    @pytest.mark.parametrize("verb", ["Used", "Using", "Calling"])
    def test_short_prose_after_a_tool_verb_is_an_answer(self, verb):
        """`• Used pandas` is prose; `• Used Read` is a collapsed tool row."""

        assert kt.is_tool_call_row(f"• {verb} pandas") is False
        assert kt.is_tool_call_row(f"● {verb} pandas") is False

    def test_the_measured_tool_shapes_are_still_tool_rows(self):
        """The positive controls the escape-free rule exists for."""

        for row in [
            "● Used Read (ANSWER_SPEC.md) · 10 lines",
            "● Used find_profiles · MCP/cao-mcp-server (kimi)",
            "● Used Read (report.txt) · 3 lines",
            "● Used memory.recall (query=x) · 2 lines",
            "● Running a command · $ uname -a",
        ]:
            assert kt.is_tool_call_row(row) is True, row

    def test_a_bare_verb_and_word_is_prose(self):
        """No `·` detail separator, so it is answer content, not a tool row."""

        for row in [
            "● Used Read",
            "● Used Python",
            "• Used pandas",
            "● Using Docker",
            "• Using Python (3.12)",
        ]:
            assert kt.is_tool_call_row(row) is False, row

    def test_a_prose_answer_after_a_tool_verb_is_extracted(self, monkeypatch):
        pane = "\n".join(
            ["💫 Which parser did you use? Reply with two words.", "• Used pandas", "💫"]
        )
        result, _ = _last(monkeypatch, pane)
        assert result == "• Used pandas"

    # --- F3 (P2): italic alone is not reasoning ---------------------------

    def test_an_italic_legacy_answer_is_not_reasoning(self):
        assert kt.is_thinking_styled("• \x1b[3mHello there!\x1b[0m") is False

    def test_grey_backed_italics_are_still_reasoning(self):
        """The measured shapes keep working, colour on bullet or on text."""

        assert kt.is_thinking_styled("\x1b[38;5;244m• \x1b[39m\x1b[3m\x1b[38;5;244mthinking\x1b[0m")
        assert kt.is_thinking_styled("• \x1b[3m\x1b[38;5;244mthinking\x1b[0m") is True

    def test_an_italic_answer_is_extracted(self, monkeypatch):
        pane = "\n".join(["💫 Say hello in italics", "• \x1b[3mHello there!\x1b[0m", "💫"])
        result, _ = _last(monkeypatch, pane)
        assert result == "• Hello there!"

    def test_a_bare_verb_and_word_answer_is_extracted(self, monkeypatch):
        """A2-F1: a bare `<verb> <word>` row carries no structural evidence."""

        pane = "\n".join(["💫 Which language did you use?", "• Used Python", "💫"])
        result, _ = _last(monkeypatch, pane)
        assert result == "• Used Python"

    def test_an_italic_answer_after_reasoning_is_extracted(self, monkeypatch):
        """A2-F2: an answer bullet ends the reasoning block however it is styled."""

        pane = "\n".join(
            [
                "💫 Say hello in italics",
                "\x1b[38;5;244m• Choosing a greeting.\x1b[0m",
                "• \x1b[3mHello!\x1b[0m",
                "💫",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert result == "• Hello!"

    def test_prose_with_a_parenthesised_argument_is_not_a_tool_row(self, monkeypatch):
        """A3-F1: `• Using Python (3.12)` is prose, not execution plumbing."""

        pane = "\n".join(
            [
                "💫 Which runtime does this example use?",
                "• Using Python (3.12)",
                "Run python main.py to start.",
                "💫",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert result == "• Using Python (3.12)\nRun python main.py to start."

    def test_a_code_line_shaped_like_a_boot_message_survives(self, monkeypatch):
        """A3-F2: a heredoc body reading as a boot message is not chrome."""

        pane = "\n".join(
            [
                "💫 Write a shell script that prints a startup message",
                "• Run this script:",
                "cat <<'EOF'",
                "Loading configuration...",
                "EOF",
                "exec myapp",
                "💫",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert "Loading configuration..." in result
        assert "exec myapp" in result

    def test_real_boot_chrome_still_ends_the_region(self, monkeypatch):
        """The guard: a boot row the renderer drew still ends the region."""

        pane = "\n".join(
            [
                "✨ go",
                "● Answer line",
                "⠙ Loading configuration...",
                "not part of the answer",
                "╭──────╮",
                "│ >    │",
                "╰──────╯",
                "",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert result == "● Answer line"

    def test_a_table_row_is_not_framed_by_a_legacy_input_box(self):
        """A4-F1: only an actual box makes a prompt-shaped row composer chrome."""

        pane = "\n".join(
            [
                "╭────────────────────────────╮",
                "│ Explain shell redirection. │",
                "╰────────────────────────────╯",
                "• Operators:",
                "| Operator | Meaning |",
                "|----------|---------|",
                "| > | Redirect stdout |",
                "| >> | Append stdout |",
                "💫",
            ]
        )
        result = KimiCliProvider("t", "s", "w").extract_last_message_from_script(pane)
        assert "| > | Redirect stdout |" in result
        assert "| >> | Append stdout |" in result

    def test_prose_with_a_detail_separator_is_not_a_tool_row(self, monkeypatch):
        """A4-F2: the separator alone is not the measured detail grammar."""

        pane = "\n".join(
            [
                "💫 Summarize the implementation.",
                "• Used Python · no external dependencies.",
                "Run python3 app.py to start it.",
                "💫",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert (
            result == "• Used Python · no external dependencies.\nRun python3 app.py to start it."
        )

    def test_plugin_sse_server_is_translated_for_the_runtime_home(self):
        """A5-F1: the Kimi Code mcp.json must select the declared protocol.

        `type` is a portable Agent Plugins field that Kimi ignores; without the
        `transport` translation an SSE server published at `/events` starts as
        Streamable HTTP.
        """

        from cli_agent_orchestrator.providers.kimi_runtime_home import merge_mcp_servers

        merged = merge_mcp_servers({}, {"events": {"type": "sse", "url": "https://x/events"}})
        assert merged["events"] == {"transport": "sse", "url": "https://x/events"}

        http = merge_mcp_servers({}, {"api": {"type": "streamable-http", "url": "https://x/api"}})
        assert http["api"]["transport"] == "http"
        assert "type" not in http["api"]

    def test_a_typed_entry_is_left_alone(self):
        """An unrecognised/absent portable `type` invents nothing."""

        from cli_agent_orchestrator.providers.kimi_runtime_home import merge_mcp_servers

        merged = merge_mcp_servers({}, {"hand": {"url": "https://x/y"}})
        assert "transport" not in merged["hand"]

    def test_reasoning_in_a_partial_capture_stays_retryable(self):
        """A5-F2: a capture that merely missed the answer must not refuse.

        The A5-F2 intent is unchanged: a private-content row in a capture that
        did not reach the answer must never become a terminal refusal, because
        a wider capture may still hold the answer. The outcome is now stronger
        than "retryable". Under the CODE dialect the legacy ``╰─`` box-end
        anchor no longer establishes a submitted-turn boundary, so the 220
        leading reasoning rows do not push the answer past the anchor: the
        same widened capture returns it directly. Widening recovers the answer
        instead of merely being permitted to.
        """

        rows = _fixture("kimi_code_0431_03_final_answer.txt").split("\n")
        index = next(i for i, row in enumerate(rows) if "\x1b[38;5;253m●" in row)
        rows[index:index] = [" \x1b[38;5;244m● \x1b[3mAnother private reasoning step.\x1b[0m"] * 220

        provider = KimiCliProvider("term-a5f2", "s", "w")
        provider._dialect = kimi_cli_module.KimiDialect.CODE
        try:
            result = provider.extract_last_message_from_script("\n".join(rows[-200:]))
        except OutputExtractionError as excinfo:
            # Never a refusal, and no longer even a retry: the public answer is
            # reachable, so failing to return it is the wrong outcome.
            assert not isinstance(excinfo, _rejected())
            pytest.fail(f"the widened capture must return the public answer: {excinfo}")
        assert result == "● STEP 1\nSTEP 2\nSTEP 3\nSTEP 4\nSTEP 5\nA0-FIXTURE-DONE."
        assert "Another private reasoning step." not in result

    def test_a_quoted_approval_menu_is_answer_content(self, monkeypatch):
        """A5-F3: hint + options without the title is prose, not a live dialog."""

        pane = "\n".join(
            [
                "💫 Explain the approval menu",
                "• Available choices:",
                "    1. Approve once",
                "    2. Reject",
                "    ↑/↓ select · 1/2/3/4 choose",
                "• Choose 2 to reject the command.",
                "💫",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert "Choose 2 to reject the command." in result

    def test_a_styled_code_line_shaped_like_a_boot_message_survives(self, monkeypatch):
        """A6-F1: syntax highlighting is not the renderer's boot indicator."""

        pane = "\n".join(
            [
                "💫 Write a shell script",
                "• Run:",
                "```sh",
                "cat <<EOF",
                "\x1b[32mLoading configuration...\x1b[0m",
                "EOF",
                "echo done",
                "```",
                "💫",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert "Loading configuration..." in result
        assert "echo done" in result

    def test_a_quoted_full_approval_dialog_survives(self, monkeypatch):
        """A6-F2: a static menu has no selection cursor, so it is not live."""

        pane = "\n".join(
            [
                "💫 Explain the approval menu",
                "• The menu is:",
                "```text",
                "▶ Run this command?",
                "1. Approve once",
                "2. Reject",
                "↑/↓ select · 1/2/3/4 choose",
                "```",
                "Choose Reject to cancel.",
                "💫",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert "Choose Reject to cancel." in result

    def test_a_live_approval_dialog_still_ends_the_region(self, monkeypatch):
        """The guard: the cursor-bearing dialog the renderer draws is live."""

        pane = "\n".join(
            [
                "✨ run it",
                "● The command is ready.",
                "   \x1b[1m\x1b[38;5;215m▶\x1b[0m " "\x1b[1m\x1b[38;5;215mRun this command?\x1b[0m",
                "",
                "   \x1b[1m\x1b[38;5;116m▶\x1b[0m " "\x1b[1m\x1b[38;5;116m1. Approve once\x1b[0m",
                "   \x1b[38;5;255m  2. Reject\x1b[39m",
                "",
                "   \x1b[38;5;242m↑/↓ select · 1/2/3/4 choose · ↵ confirm\x1b[39m",
                " ────────────────────────────────────────",
                " Never Ask  Model thinking  /tmp/proj  master",
                "",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert result == "● The command is ready."

    def test_a_previous_turns_answer_does_not_exempt_this_turn(self, monkeypatch):
        """A7-F1 (P1): an old answer bullet must not excuse a reasoning-only turn.

        The exemption that lets a partial capture escalate is scoped to the
        current turn. Applying it capture-wide let a previous turn's answer
        exempt a reasoning-only turn now, which escalated into the raw pane and
        disclosed the previous turn's private reasoning.
        """

        pane = "\n".join(
            [
                "💫 Previous task",
                "\x1b[38;5;244m• PRIVATE_PREVIOUS_TURN\x1b[0m",
                "• Previous public answer",
                "💫 New task",
                "\x1b[38;5;244m• Working through the new task\x1b[0m",
                "💫",
            ]
        )
        with pytest.raises(_rejected()) as excinfo:
            _last(monkeypatch, pane)
        assert "PRIVATE_PREVIOUS_TURN" not in str(excinfo.value)

    def test_a_moon_inside_a_line_is_content(self, monkeypatch):
        """A7-F2: the indicator slot is the start of the row, not anywhere."""

        assert kt.is_live_spinner_line("🌕", "🌕", kt.SpinnerSemantics.LEGACY) is True
        assert (
            kt.is_live_spinner_line(
                '0 0 * * * echo "🌕 Backup starting"',
                '0 0 * * * echo "🌕 Backup starting"',
                kt.SpinnerSemantics.LEGACY,
            )
            is False
        )

        pane = "\n".join(
            [
                "💫 Return the cron entries",
                "• Cron configuration:",
                '0 0 * * * echo "🌕 Backup starting"',
                "0 1 * * * /usr/bin/backup",
                "💫",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert 'echo "🌕 Backup starting"' in result
        assert "/usr/bin/backup" in result

    def test_dimmed_payload_cannot_close_its_own_tool_block(self, monkeypatch):
        """A8-F1 (P1): payload is dimmed, so "any SGR" is not spinner evidence."""

        from cli_agent_orchestrator.services import terminal_service

        rows = _fixture("kimi_code_0431_10_mcp_tool_turn.txt").split("\n")
        index = next(i for i, row in enumerate(rows) if '[{"name"' in row)
        rows[index : index + 1] = [
            "   \x1b[2m⠙ working…\x1b[22m",
            "   \x1b[2mPRIVATE_TOOL_PAYLOAD\x1b[22m",
        ]
        pane = "\n".join(rows)

        provider = KimiCliProvider("term-a8f1", "s", "w")
        provider._dialect = kimi_cli_module.KimiDialect.CODE
        backend = MagicMock()
        backend.get_history.return_value = pane
        monkeypatch.setattr(
            terminal_service,
            "get_terminal_metadata",
            lambda tid: {"tmux_session": "s", "tmux_window": "w"},
        )
        monkeypatch.setattr(terminal_service.status_monitor, "get_buffer", lambda tid: pane)
        monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
        monkeypatch.setattr(terminal_service.provider_manager, "get_provider", lambda tid: provider)

        result = terminal_service.get_output("term-a8f1", terminal_service.OutputMode.LAST)
        assert "PRIVATE_TOOL_PAYLOAD" not in result

    def test_a_moon_prefixed_answer_line_survives_in_legacy(self, monkeypatch):
        """A8-F2: the legacy indicator is a *bare* moon, not a moon-prefixed line."""

        pane = "\n".join(
            [
                "💫 Give me an observing guide",
                "• Observing guide:",
                "🌕 Full moon: 2026-09-26",
                "• End of guide.",
                "💫",
            ]
        )
        result, _ = _last(monkeypatch, pane)
        assert "🌕 Full moon: 2026-09-26" in result


# =============================================================================
# Structural channel-ownership review — renderer evidence must own boundaries
# =============================================================================


class TestPR799StructuralChannelOwnership:
    """Fresh-review regressions for private-channel ownership and UI evidence.

    These are intentionally public-path tests where practical. The common
    contract is stronger than any one collision:

    * an established private block owns its rows until positive renderer
      evidence changes channel;
    * a shallow/unanchored capture is never sufficient evidence to publish an
      orphan continuation;
    * exhausted Kimi extraction never substitutes the raw pane;
    * destructive UI kinds require their own renderer evidence rather than
      borrowing styling from unrelated rows later in the capture.
    """

    @staticmethod
    def _composer():
        return [
            " \x1b[38;5;240m╭────────╮\x1b[39m",
            " \x1b[38;5;240m│ >      │\x1b[39m",
            " \x1b[38;5;240m╰────────╯\x1b[39m",
        ]

    @classmethod
    def _pane(cls, *body):
        return "\n".join([_user("✨ Current task"), "", *body, *cls._composer(), _footer()])

    @staticmethod
    def _get_last_code(monkeypatch, captures):
        """Drive public LAST with a real CODE provider and controlled captures."""

        from cli_agent_orchestrator.services import terminal_service

        provider = KimiCliProvider("term-structural-review", "s", "w")
        provider._dialect = kimi_cli_module.KimiDialect.CODE
        backend = MagicMock()
        if isinstance(captures, (list, tuple)):
            values = list(captures)
            backend.get_history.side_effect = values
            buffer_value = values[0]
        else:
            backend.get_history.return_value = captures
            buffer_value = captures

        monkeypatch.setattr(
            terminal_service,
            "get_terminal_metadata",
            lambda tid: {"tmux_session": "s", "tmux_window": "w"},
        )
        monkeypatch.setattr(terminal_service.status_monitor, "get_buffer", lambda tid: buffer_value)
        monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
        monkeypatch.setattr(terminal_service.provider_manager, "get_provider", lambda tid: provider)
        return (
            terminal_service.get_output("term-structural-review", terminal_service.OutputMode.LAST),
            backend,
        )

    def test_tool_payload_cannot_fake_a_user_submission_boundary(self, monkeypatch):
        pane = self._pane(
            "● Used Read (report.txt) · 3 lines",
            "\x1b[2m✨ quoted prompt\x1b[0m",
            "\x1b[2m● PRIVATE_TOOL_PAYLOAD\x1b[0m",
            _answer("SAFE_FINAL"),
        )

        result, _ = self._get_last_code(monkeypatch, pane)
        assert result == "● SAFE_FINAL"

    # --- renderer evidence required before a channel transition -----------

    def test_shallow_capture_cannot_publish_a_dimmed_payload_bullet(self, monkeypatch):
        """A dimmed `●` row is payload, not renderer-evidenced answer text.

        The unanchored capture rule originally accepted any ``FINAL_BULLET`` kind
        as the answer marker. Tool payload is drawn dim and routinely starts with
        the answer's bullet, so the marker must carry the renderer's own colour.
        """

        pane = "\n".join(
            [
                "\x1b[2m● PRIVATE_TOOL_PAYLOAD\x1b[0m",
                _answer("SAFE_FINAL"),
                "── input ──",
                _footer(),
            ]
        )

        result, _ = self._get_last_code(monkeypatch, pane)
        assert result == "● SAFE_FINAL"
        assert "PRIVATE_TOOL_PAYLOAD" not in result

    def test_dimmed_sparkle_is_not_a_legacy_text_anchor(self, monkeypatch):
        """The legacy `✨` anchor is text; under CODE it needs the submission colour.

        An ANSI-stripped dimmed sparkle matches the legacy prompt pattern exactly,
        which anchored the response region *after* it and published the payload
        that followed — bypassing the CODE ownership rule entirely.
        """

        shallow = "\n".join(
            [
                "\x1b[2m✨ quoted prompt\x1b[0m",
                "\x1b[2mPRIVATE_TOOL_PAYLOAD\x1b[0m",
                _answer("SAFE_FINAL"),
                "── input ──",
                _footer(),
            ]
        )
        full = self._pane(_answer("SAFE_FINAL"))

        result, backend = self._get_last_code(monkeypatch, [shallow, full])
        assert result == "● SAFE_FINAL"
        assert backend.get_history.call_count == 2
        assert "PRIVATE_TOOL_PAYLOAD" not in result

    def test_plain_sparkle_inside_an_established_tool_block_stays_payload(self, monkeypatch):
        """An open private block owns its rows over a candidate transition.

        The escape-free fallback lets a plain sparkle *start* a submission where
        no private block is open. Inside an established tool block that same row
        must not take the channel: it reset the tool state and published the
        dimmed payload after it.
        """

        pane = "\n".join(
            [
                _user("✨ Current task"),
                "",
                "● Used Read (report.txt) · 3 lines",
                "✨ quoted prompt",
                "\x1b[2m● PRIVATE_TOOL_PAYLOAD\x1b[0m",
                _answer("SAFE_FINAL"),
                *self._composer(),
                _footer(),
            ]
        )

        result, _ = self._get_last_code(monkeypatch, pane)
        assert result == "● SAFE_FINAL"
        assert "PRIVATE_TOOL_PAYLOAD" not in result

    def test_row_drawn_in_the_answer_colour_is_not_a_spinner(self, monkeypatch):
        """Braille in an answer line is answer text, not the working indicator.

        The measured indicator is drawn in the spinner colour; answer text is
        drawn in 253. Reading a 253 row as work dropped it from the answer *and*
        pinned a settled terminal at PROCESSING.
        """

        from cli_agent_orchestrator.models.terminal import TerminalStatus

        pane = self._pane(
            _answer("Braille alphabet:"),
            "   \x1b[38;5;253m⠋ is F\x1b[39m",
            "TAIL",
        )
        result, _ = self._get_last_code(monkeypatch, pane)
        assert "⠋ is F" in result
        assert "TAIL" in result

        # The predicate the status path delegates to must agree.
        drawn = "   \x1b[38;5;253m⠋ is F\x1b[39m"
        assert kt.is_live_spinner_line(kt.strip_sgr(drawn), drawn) is False

        provider = KimiCliProvider("term-structural-frame", "s", "w")
        provider._dialect = kimi_cli_module.KimiDialect.CODE
        rows = [
            kt.strip_sgr(_user("✨ Current task")),
            kt.strip_sgr(_answer("Braille alphabet:")),
            drawn,
            "TAIL",
            *[kt.strip_sgr(row) for row in self._composer()],
            kt.strip_sgr(_footer()),
        ]
        assert provider.get_status_from_screen(rows) is not TerminalStatus.PROCESSING

    def test_row_drawn_in_the_answer_colour_is_not_an_idle_tip(self, monkeypatch):
        """A moon-tip line inside an answer is answer text."""

        pane = self._pane(
            _answer("Example:"),
            "   \x1b[38;5;253m🌕 · Tip: use /help\x1b[39m",
            "TAIL",
        )
        result, _ = self._get_last_code(monkeypatch, pane)
        assert "🌕 · Tip: use /help" in result
        assert "TAIL" in result

        drawn = "   \x1b[38;5;253m🌕 · Tip: use /help\x1b[39m"
        assert kt.is_idle_tip_line(kt.strip_sgr(drawn), drawn) is False

    def test_measured_indicator_colours_still_classify(self):
        """The controls: the renderer's own indicator rows still classify."""

        spinner = "\x1b[38;5;111m⠙\x1b[39m working…"
        case = "\x1b[38;5;111m⠙\x1b[39m Using handoff({...})"
        tip = "🌕\x1b[38;5;244m · Tip: ctrl-s to add guidance"
        for row in (spinner, case):
            assert kt.is_live_spinner_line(kt.strip_sgr(row), row) is True
            assert kt.classify_line(row, kt.strip_sgr(row)) is kt.KimiLineKind.LIVE_SPINNER
        assert kt.is_idle_tip_line(kt.strip_sgr(tip), tip) is True
        assert kt.classify_line(tip, kt.strip_sgr(tip)) is kt.KimiLineKind.IDLE_TIP

    def test_wrapped_submission_is_still_absorbed_at_the_boundary(self, monkeypatch):
        """The control: the plain-sparkle guard does not weaken the echo rule."""

        pane = "\n".join(
            [
                "\x1b[1;38;5;222m✨ current task\x1b[0m",
                "    \x1b[1;38;5;222mline two of the submission\x1b[0m",
                "",
                _answer("SAFE_FINAL"),
                _footer(),
            ]
        )

        result, _ = self._get_last_code(monkeypatch, pane)
        assert result == "● SAFE_FINAL"
        assert "PRIVATE_TOOL_PAYLOAD" not in result

    @pytest.mark.parametrize(
        "orphan",
        [
            "\x1b[38;5;244m\x1b[3mPRIVATE_REASONING\x1b[0m",
            "\x1b[38;5;222mPRIVATE_USER_CONTINUATION\x1b[0m",
            "\x1b[2mPRIVATE_TOOL_CONTINUATION\x1b[0m",
        ],
    )
    def test_unanchored_private_continuation_retries_until_owner_is_visible(
        self, monkeypatch, orphan
    ):
        # The compact named input rule is the exact shallow-capture shape from
        # the fresh review. With no user/response header in view, the leading
        # styled continuation is ambiguous and must trigger widening rather than
        # being published as CONTENT.
        shallow = "\n".join([orphan, "── input ──", _footer()])
        full = self._pane(_answer("SAFE_FINAL"))

        result, backend = self._get_last_code(monkeypatch, [shallow, full])
        assert result == "● SAFE_FINAL"
        assert backend.get_history.call_count == 2
        assert "PRIVATE_" not in result

    def test_exhausted_kimi_last_never_returns_raw_submitted_input(self, monkeypatch):
        from cli_agent_orchestrator.providers.base import OutputExtractionError

        pane = "\n".join([_user("✨ PRIVATE USER TEXT"), *self._composer(), _footer()])

        with pytest.raises(OutputExtractionError) as excinfo:
            self._get_last_code(monkeypatch, pane)

        message = str(excinfo.value)
        assert "PRIVATE USER TEXT" not in message
        assert "[NO RESPONSE" not in message
        assert "[PARTIAL RESPONSE" not in message

    @pytest.mark.parametrize(
        "quoted",
        [
            [
                "~~~text",
                "▶ Run this command?",
                "↑/↓ select · 1/2/3/4 choose",
                "▶ 1. Approve once",
                "2. Reject",
                "~~~",
            ],
            [
                "~~~text",
                "Trust this folder?",
                "↑↓ navigate · Enter select · Esc exit",
                "/tmp/example",
                "❯ Trust this folder",
                "Don't trust",
                "~~~",
            ],
        ],
    )
    def test_plain_quoted_dialog_cannot_borrow_styling_from_real_composer(
        self, monkeypatch, quoted
    ):
        pane = self._pane(_answer("Example:"), *quoted, "TAIL")
        result, _ = self._get_last_code(monkeypatch, pane)
        assert "TAIL" in result
        assert quoted[1] in result

    def test_answer_bullet_colour_cannot_certify_footer_prose(self, monkeypatch):
        pane = self._pane(
            # Keep colour 253 active across the whole answer row. This is a
            # valid renderer shape and is the exact collision the reviewer
            # reproduced: the row contains two footer-looking fields but is
            # positively an answer bullet.
            " \x1b[38;5;253m● The fields are context: 2% and " "agent (Kimi-k2.6 ●).\x1b[39m",
            "TAIL",
        )
        result, _ = self._get_last_code(monkeypatch, pane)
        # Exact equality, not membership: at the pre-fix head this pane could
        # only be served by the raw-pane fallback, which contains the answer text
        # too and would satisfy a substring assertion for the wrong reason.
        assert result == "● The fields are context: 2% and agent (Kimi-k2.6 ●).\nTAIL"

    def test_answer_moon_tip_phrase_is_not_idle_tip(self, monkeypatch):
        pane = self._pane(
            _answer("The UI displays 🌕 · Tip: use /help."),
            "TAIL",
        )
        result, _ = self._get_last_code(monkeypatch, pane)
        assert "The UI displays 🌕 · Tip: use /help." in result
        assert "TAIL" in result

    def test_leading_braille_prose_is_not_a_spinner(self, monkeypatch):
        pane = self._pane(
            _answer("Braille alphabet:"),
            "⠁ is A",
            "⠃ is B",
            "TAIL",
        )
        result, _ = self._get_last_code(monkeypatch, pane)
        assert "⠁ is A" in result
        assert "⠃ is B" in result
        assert "TAIL" in result

    def test_braille_prose_over_a_settled_composer_is_not_processing(self):
        """The status side of the same collision: braille text is not work.

        The extraction path is only half the defect — reading any braille
        codepoint as the working indicator also pins a settled terminal at
        PROCESSING, which is what the inbox keys delivery off.
        """

        from cli_agent_orchestrator.models.terminal import TerminalStatus

        provider = KimiCliProvider("term-structural-braille", "s", "w")
        provider._dialect = kimi_cli_module.KimiDialect.CODE
        rows = [
            kt.strip_sgr(_user("✨ Current task")),
            "",
            kt.strip_sgr(_answer("Braille alphabet:")),
            "⠁ is A",
            "⠃ is B",
            "TAIL",
            *[kt.strip_sgr(row) for row in self._composer()],
            kt.strip_sgr(_footer()),
        ]
        assert provider.get_status("\n".join(rows)) is TerminalStatus.COMPLETED

    def test_measured_spinner_frame_is_still_processing(self):
        """The control: the frames the renderer actually animates stay work."""

        from cli_agent_orchestrator.models.terminal import TerminalStatus

        provider = KimiCliProvider("term-structural-spinner", "s", "w")
        provider._dialect = kimi_cli_module.KimiDialect.CODE
        rows = [
            kt.strip_sgr(_user("✨ Current task")),
            kt.strip_sgr(_answer("Working through it.")),
            "\x1b[38;5;111m⠹\x1b[39m Using handoff({...})",
            *[kt.strip_sgr(row) for row in self._composer()],
            kt.strip_sgr(_footer()),
        ]
        assert provider.get_status("\n".join(rows)) is TerminalStatus.PROCESSING

    def test_plain_quoted_box_art_is_not_a_composer(self, monkeypatch):
        pane = self._pane(
            _answer("Draw this:"),
            "~~~text",
            "╭──╮",
            "│ > │",
            "╰──╯",
            "~~~",
            "TAIL",
        )
        result, _ = self._get_last_code(monkeypatch, pane)
        assert "╭──╮" in result
        assert "│ > │" in result
        assert "TAIL" in result

    # --- answer text that merely *mentions* chrome vocabulary -------------

    def test_answer_mentioning_collapsed_output_text_survives(self, monkeypatch):
        """Quoting the collapse phrase is prose, not execution plumbing.

        The measured collapse row is the row's own leading content
        (``   … (3 more lines, ctrl+o to expand)``). An unanchored search made
        the phrase destructive anywhere on a row, so an answer that explained it
        was dropped — even with a colour-253 answer bullet.
        """

        body = "The UI shows … (3 more lines, ctrl+o to expand) when output is collapsed."
        pane = self._pane(_answer(body), "TAIL")

        result, _ = self._get_last_code(monkeypatch, pane)
        assert result == f"● {body}\nTAIL"

    def test_legacy_answer_mentioning_collapsed_output_text_survives(self, monkeypatch):
        """The same collision on the legacy path, which has no answer colour."""

        body = "The UI shows … (3 more lines, ctrl+o to expand) when output is collapsed."
        pane = "\n".join(["💫 Explain the output", "", f"• {body}", "", "💫"])

        result, _ = _last(monkeypatch, pane)
        assert result == f"• {body}"

    def test_answer_with_inline_colour_222_span_is_not_an_echo(self, monkeypatch):
        """An inline submission-coloured span is the answer's own emphasis.

        The renderer draws a submitted row in 222 from its leading graphic
        position; colour applied to a fragment mid-sentence is not a wrapped
        submission. Reading it as one absorbed the answer into the user echo.
        """

        pane = "\n".join(
            [
                "💫 Explain the output",
                "",
                "• Use \x1b[38;5;222mVALUE\x1b[39m here.",
                "",
                "💫",
            ]
        )

        result, _ = _last(monkeypatch, pane)
        assert result == "• Use VALUE here."

    def test_measured_collapsed_output_row_stays_tool_chrome(self):
        """The control: the renderer's own collapse row is still plumbing."""

        assert (
            kt.classify_line("   \x1b[2m… (3 more lines, ctrl+o to expand)\x1b[0m")
            is kt.KimiLineKind.TOOL_CHROME
        )
        assert kt.classify_line("… (3 more lines, ctrl+o to expand)") is kt.KimiLineKind.TOOL_CHROME

    def test_wrapped_submission_colour_still_continues_the_echo(self):
        """The control: a row *drawn* in 222 is still a submission continuation.

        A submitted message may contain a pasted list, so a row whose own leading
        content (bullet included) is drawn in the submission colour continues the
        block rather than becoming a fresh answer bullet.
        """

        assert kt.is_user_input_continuation("    \x1b[1;38;5;222mline two\x1b[22m\x1b[39m") is True
        assert kt.is_user_input_continuation("• \x1b[38;5;222m- pasted item") is True
        assert kt.is_user_input_continuation("• Use \x1b[38;5;222mVALUE\x1b[39m here.") is False
        assert (
            kt.is_user_input_continuation(" \x1b[38;5;253m● \x1b[39massertion ran\x1b[38;5;222m")
            is False
        )

    def test_wrapped_submission_is_still_absorbed_through_the_public_path(self, monkeypatch):
        """The control at the public boundary: the echo is not published."""

        pane = "\n".join(
            [
                "\x1b[1;38;5;222m✨ current task\x1b[0m",
                "    \x1b[1;38;5;222mline two of the submission\x1b[0m",
                "",
                _answer("SAFE_FINAL"),
                _footer(),
            ]
        )

        result, _ = self._get_last_code(monkeypatch, pane)
        assert result == "● SAFE_FINAL"


# =============================================================================
# Final ownership closure — B1: legacy anchor authority, B3: block ownership
# =============================================================================


class TestPR799FinalOwnershipClosure:
    """The last two private-disclosure seams, at the ownership/dialect level.

    **B1.** The legacy pre-v1.20 ``╰─`` box-end anchor is a *text* shape, and Kimi
    Code draws the same glyph in private payload. Under CODE it must not by itself
    establish a submitted-turn boundary: the dialect has its own current-turn
    machinery, its own submission evidence and its own retryable unanchored
    fallback. The defect is the anchor's *authority*, not one glyph spelling.

    **B3.** An open tool block owns its rows until positive public evidence
    displaces it. A reasoning-shaped row is a candidate channel transition, not
    evidence that the block ended — reproduced: a grey bullet inside a tool block
    released the block and the dim payload after it was published as the answer.
    """

    #: The renderer's answer bullet, and rows that are private whichever channel
    #: they belong to (dim payload, a legacy box border drawn dim, a grey bullet).
    _PUBLIC = "\x1b[38;5;253m● \x1b[39mPUBLIC"
    _PAYLOAD = "\x1b[2mPRIVATE_PAYLOAD\x1b[0m"
    _BOX_DIM = "\x1b[2m╰──╯\x1b[0m"
    _BOX_PLAIN = "╰──────────────────────────╯"
    _BOX_WIDE = "╰──────────────────────────────────────────────────────────╯"
    _GREY_BULLET = "\x1b[38;5;244m● quote\x1b[0m"

    #: Reuse the sibling class's public-boundary driver rather than duplicating it.
    _composer = staticmethod(TestPR799StructuralChannelOwnership._composer)
    _get_last_code = staticmethod(TestPR799StructuralChannelOwnership._get_last_code)

    # --- B1: the legacy box anchor has no authority under CODE --------------

    @pytest.mark.parametrize("box", [_BOX_DIM, _BOX_PLAIN, _BOX_WIDE])
    def test_legacy_box_end_cannot_anchor_a_code_turn(self, monkeypatch, box):
        """Every spelling of the legacy box end, not just the dimmed one."""

        pane = "\n".join(
            [self._PAYLOAD, box, self._PAYLOAD, self._PUBLIC, "── input ──", _footer()]
        )

        result, _ = self._get_last_code(monkeypatch, pane)
        assert result == "● PUBLIC"
        assert "PRIVATE_PAYLOAD" not in result

    def test_legacy_box_end_without_an_answer_stays_retryable(self, monkeypatch):
        """No public answer and no anchor: the CODE contract is widening, not bytes."""

        pane = "\n".join([self._PAYLOAD, self._BOX_DIM, self._PAYLOAD, "── input ──", _footer()])

        from cli_agent_orchestrator.providers.base import OutputExtractionError

        with pytest.raises(OutputExtractionError):
            self._get_last_code(monkeypatch, pane)

    def test_legacy_box_end_still_anchors_the_legacy_dialect(self, monkeypatch):
        """The legacy path keeps its own anchor: this is a dialect rule, not a ban."""

        pane = "\n".join([self._PAYLOAD, self._BOX_PLAIN, self._PUBLIC, "✨"])

        result, _ = _last(monkeypatch, pane)
        assert "PUBLIC" in result

    def test_legacy_box_anchor_is_not_replaced_by_another_text_heuristic(self, monkeypatch):
        """The CODE pane must not fall back to publishing leading content either way.

        Whichever anchor the extractor chooses, the private row above it is not
        answer text: with a public answer present the answer is returned, and the
        payload before it is not.
        """

        pane = "\n".join(
            [
                "── input ──",
                self._PAYLOAD,
                self._BOX_WIDE,
                self._PAYLOAD,
                self._PUBLIC,
                "── input ──",
                _footer(),
            ]
        )

        result, _ = self._get_last_code(monkeypatch, pane)
        assert "PRIVATE_PAYLOAD" not in result
        assert result == "● PUBLIC"

    # --- B3: an open tool block owns its rows ------------------------------

    @pytest.mark.parametrize(
        "intruder",
        [
            _GREY_BULLET,
            "\x1b[38;5;244m\x1b[3mquote continuation\x1b[0m",
            "● plain bullet payload",
            "… (3 more lines, ctrl+o to expand)",
            "\x1b[38;5;244m● first\x1b[0m\n\x1b[38;5;244m● second\x1b[0m",
            "\n\x1b[38;5;244m● after a blank\x1b[0m",
            "ordinary prose that is really payload",
        ],
    )
    def test_tool_block_owns_its_rows_until_public_evidence(self, monkeypatch, intruder):
        """A reasoning-shaped (or chrome-shaped, or prose) row cannot release a block."""

        pane = "\n".join(
            [
                _user("✨ task"),
                "",
                "● Used Read (report.txt) · 3 lines",
                *intruder.split("\n"),
                self._PAYLOAD,
                self._PUBLIC,
                *self._composer(),
                _footer(),
            ]
        )

        result, _ = self._get_last_code(monkeypatch, pane)
        assert result == "● PUBLIC"
        assert "PRIVATE_PAYLOAD" not in result

    def test_a_real_tool_then_reasoning_then_answer_sequence_stays_private(self, monkeypatch):
        """§12: the intermediate reasoning never needs to be published."""

        pane = "\n".join(
            [
                _user("✨ task"),
                "",
                "● Used Read (report.txt) · 3 lines",
                '\x1b[2m[{"a":1}]\x1b[0m',
                self._GREY_BULLET,
                "● still tool owned",
                self._PUBLIC,
                *self._composer(),
                _footer(),
            ]
        )

        result, _ = self._get_last_code(monkeypatch, pane)
        assert result == "● PUBLIC"
        for leaked in ("PRIVATE", "quote", "still tool owned", "Used Read", '[{"a"'):
            assert leaked not in result


class TestPR799CurrentMaintainerReview:
    """Current upstream review, reproduced on 10ddb550 before source edits."""

    @pytest.mark.parametrize(
        "dialect", [kimi_cli_module.KimiDialect.LEGACY, kimi_cli_module.KimiDialect.CODE]
    )
    @pytest.mark.parametrize("elapsed", [0, 9])
    def test_dropped_initial_paste_is_not_started(self, monkeypatch, dialect, elapsed):
        import time

        from cli_agent_orchestrator.services import terminal_service as ts

        pane = _fixture(
            "kimi_code_0431_01_fresh_startup_idle.txt"
            if dialect is kimi_cli_module.KimiDialect.CODE
            else "kimi_cli_idle_output.txt"
        )
        provider = KimiCliProvider("review-drop", "s", "w")
        provider._dialect = dialect
        provider.mark_input_received()
        provider._last_dispatch_time = time.time() - elapsed
        backend = MagicMock()
        backend.get_history.return_value = pane
        monkeypatch.setattr(ts.status_monitor, "get_buffer", lambda _: pane)
        monkeypatch.setattr(ts, "get_backend", lambda: backend)
        monkeypatch.setattr(kimi_cli_module, "get_backend", lambda: backend)
        monkeypatch.setattr(
            ts, "get_terminal_metadata", lambda _: {"tmux_session": "s", "tmux_window": "w"}
        )
        assert ts._worker_is_started_direct("review-drop", provider) is False
        send = MagicMock()
        monkeypatch.setattr(ts, "send_input", send)
        monkeypatch.setattr(ts, "_message_visible_in_box", lambda *_: False)
        assert ts.redeliver_dropped_message("review-drop", "do the task", 1, provider) is False
        send.assert_called_once()

    @pytest.mark.parametrize(
        "dialect", [kimi_cli_module.KimiDialect.LEGACY, kimi_cli_module.KimiDialect.CODE]
    )
    @pytest.mark.parametrize("example", [False, True])
    def test_public_last_preserves_answer_owned_tool_examples(self, monkeypatch, dialect, example):
        from cli_agent_orchestrator.services import terminal_service as ts

        provider = KimiCliProvider("review-prose", "s", "w")
        provider._dialect = dialect
        body = (
            [
                _answer("Example:"),
                "```text",
                "● Used search_docs · MCP/helpdesk",
                "```",
                "The API is sufficient.",
            ]
            if example
            else [
                _answer("Running a command · optional for this read-only check."),
                "The API is sufficient.",
            ]
        )
        pane = "\n".join([_user("✨ Explain the check"), "", *body, ""])
        backend = MagicMock()
        backend.get_history.return_value = pane
        monkeypatch.setattr(ts, "get_backend", lambda: backend)
        monkeypatch.setattr(
            ts, "get_terminal_metadata", lambda _: {"tmux_session": "s", "tmux_window": "w"}
        )
        monkeypatch.setattr(ts.status_monitor, "get_buffer", lambda _: pane)
        monkeypatch.setattr(ts.provider_manager, "get_provider", lambda _: provider)
        expected = "\n".join(kt.strip_sgr(row).strip() for row in body)
        assert ts.get_output("review-prose", ts.OutputMode.LAST) == expected

    @pytest.mark.parametrize(
        "dialect", [kimi_cli_module.KimiDialect.LEGACY, kimi_cli_module.KimiDialect.CODE]
    )
    @pytest.mark.parametrize("fence", ["```", "~~~~"])
    def test_public_last_preserves_fence_on_first_answer_bullet(self, monkeypatch, dialect, fence):
        """Reviewer round 4: the renderer prefix must not hide a leading fence."""

        from cli_agent_orchestrator.services import terminal_service as ts

        provider = KimiCliProvider("review-leading-fence", "s", "w")
        provider._dialect = dialect
        opener = (
            _answer(fence + "text")
            if dialect is kimi_cli_module.KimiDialect.CODE
            else f"• {fence}text"
        )
        submission = (
            _user("✨ Explain the log format")
            if dialect is kimi_cli_module.KimiDialect.CODE
            else "💫 Explain the log format"
        )
        body = [
            opener,
            "● Used docs.search · MCP/manuals",
            fence,
            "This is a quoted example, not an executed tool.",
        ]
        pane = "\n".join([submission, "", *body, ""])
        backend = MagicMock()
        backend.get_history.return_value = pane
        monkeypatch.setattr(ts, "get_backend", lambda: backend)
        monkeypatch.setattr(
            ts, "get_terminal_metadata", lambda _: {"tmux_session": "s", "tmux_window": "w"}
        )
        monkeypatch.setattr(ts.status_monitor, "get_buffer", lambda _: pane)
        monkeypatch.setattr(ts.provider_manager, "get_provider", lambda _: provider)
        expected = "\n".join(kt.strip_sgr(row).strip() for row in body)
        assert ts.get_output("review-leading-fence", ts.OutputMode.LAST) == expected

    def test_restart_removes_legacy_launch_script(self, tmp_path, monkeypatch):
        monkeypatch.setattr(kimi_cli_module, "CAO_HOME_DIR", tmp_path / "cao")
        monkeypatch.setattr(kimi_cli_module, "shell_safe_temp_root", lambda: str(tmp_path))
        from cli_agent_orchestrator.providers import manager as manager_module
        from cli_agent_orchestrator.providers.manager import ProviderManager

        profile = MagicMock(model=None, system_prompt=None)
        profile.mcpServers = {
            "example": {"command": "example-mcp", "env": {"TOKEN": "synthetic-test-secret"}}
        }
        monkeypatch.setattr(kimi_cli_module, "load_agent_profile", lambda _: profile)
        monkeypatch.setattr(kimi_cli_module, "_with_plugin_mcp", lambda profile, _: profile)
        monkeypatch.setattr(KimiCliProvider, "_ensure_mcp_timeout", lambda _: None)
        provider = KimiCliProvider("review-restart", "s", "w", agent_profile="review")
        provider._dialect = kimi_cli_module.KimiDialect.LEGACY
        command = provider._build_kimi_command("/usr/bin/kimi")
        assert f"cd {provider._temp_dir}" in command
        assert "--yolo" in command
        provider._materialize_launch_command(command)
        script = Path(provider._shell_safe_dir) / "kimi-launch.sh"
        assert "synthetic-test-secret" in script.read_text()
        assert script.stat().st_mode & 0o777 == 0o700
        restarted = KimiCliProvider("review-restart", "s", "w")
        restarted._dialect = kimi_cli_module.KimiDialect.LEGACY
        assert restarted._temp_dir is None
        monkeypatch.setattr(
            manager_module,
            "get_terminal_metadata",
            lambda _: {
                "provider": "kimi_cli",
                "tmux_session": "s",
                "tmux_window": "w",
                "agent_profile": "review",
                "provider_variant": "legacy",
            },
        )
        assert ProviderManager().cleanup_provider("review-restart") is True
        assert not script.exists()

    @pytest.mark.parametrize(
        "fixture,dialect",
        [
            ("kimi_code_0431_02_processing_turn.txt", kimi_cli_module.KimiDialect.CODE),
            ("kimi_code_0431_04_post_answer_idle.txt", kimi_cli_module.KimiDialect.CODE),
            ("kimi_cli_processing_output.txt", kimi_cli_module.KimiDialect.LEGACY),
            ("kimi_cli_completed_output.txt", kimi_cli_module.KimiDialect.LEGACY),
        ],
    )
    def test_genuine_execution_prevents_resend(self, monkeypatch, fixture, dialect):
        from cli_agent_orchestrator.services import status_monitor as sm
        from cli_agent_orchestrator.services import terminal_service as ts

        provider = KimiCliProvider("review-real", "s", "w")
        provider._dialect = dialect
        monitor = sm.StatusMonitor()
        monkeypatch.setattr(sm.provider_manager, "get_provider", lambda _: provider)
        monkeypatch.setattr(monitor, "_schedule_raw_detection", lambda *_: None)
        monkeypatch.setattr(monitor, "_schedule_screen_detection", lambda *_: None)
        monkeypatch.setattr(ts, "status_monitor", monitor)
        monitor.clear_rolling_buffer(provider.terminal_id, provider)
        provider.mark_input_received()
        backend = MagicMock()
        backend.get_history.return_value = _fixture("kimi_code_0431_01_fresh_startup_idle.txt")
        # Settled text alone cannot prove acceptance. These captures follow a
        # transient processing frame in the same post-clear raw byte burst.
        activity = (
            "⠙ Thinking… 1s · 4 tokens" if dialect is kimi_cli_module.KimiDialect.CODE else "🌑"
        )
        current = activity + "\n" + _fixture(fixture)
        monitor._process_chunk(provider.terminal_id, current)
        monkeypatch.setattr(ts, "get_backend", lambda: backend)
        monkeypatch.setattr(
            ts, "get_terminal_metadata", lambda _: {"tmux_session": "s", "tmux_window": "w"}
        )
        send, key = MagicMock(), MagicMock()
        monkeypatch.setattr(ts, "send_input", send)
        monkeypatch.setattr(ts, "send_special_key", key)
        assert ts.redeliver_dropped_message("review-real", "do the task", 1, provider) is True
        backend.get_history.assert_not_called()
        send.assert_not_called()
        key.assert_not_called()

    def test_observed_execution_survives_scrolling_but_resets_for_next_dispatch(self):
        provider = KimiCliProvider("review-latch", "s", "w")
        provider._dialect = kimi_cli_module.KimiDialect.CODE
        provider.mark_input_received()
        current = _fixture("kimi_code_0431_02_processing_turn.txt")
        assert provider.has_execution_evidence(current) is True
        assert provider.has_execution_evidence("") is True
        provider.mark_input_received()
        assert provider.has_execution_evidence("") is False

    @pytest.mark.parametrize(
        "pane",
        [
            _fixture("kimi_code_0431_05_mcp_startup.txt"),
            _fixture("kimi_code_0431_09_false_moon_spinner_idle.txt"),
            _user("✨ task pasted but no execution yet"),
            "── input ──\n> task pasted but no execution yet\ncontext: 2% (1/2)",
        ],
    )
    def test_boot_composer_and_submission_are_not_execution(self, pane):
        provider = KimiCliProvider("review-not-started", "s", "w")
        provider._dialect = kimi_cli_module.KimiDialect.CODE
        provider.mark_input_received()
        assert provider.has_execution_evidence(pane) is False

    @pytest.mark.parametrize(
        "text", ["Used search_docs · MCP/helpdesk", "Running a command · $ example"]
    )
    def test_answer_styling_wins_over_tool_text(self, monkeypatch, text):
        pane = "\n".join(["💫 Explain", _answer(text), "The API is sufficient.", ""])
        actual, _ = _last(monkeypatch, pane)
        assert actual == "● " + text + "\nThe API is sufficient."

    @pytest.mark.parametrize(
        "dialect", [kimi_cli_module.KimiDialect.LEGACY, kimi_cli_module.KimiDialect.CODE]
    )
    @pytest.mark.parametrize("fence", ["```", "~~~~"])
    def test_fence_inside_private_tool_output_cannot_release_payload(
        self, monkeypatch, fence, dialect
    ):
        pane = "\n".join(
            [
                _user("✨ Check"),
                "",
                "● Used search_docs · MCP/helpdesk",
                fence + "text",
                "● Used quoted_tool · MCP/helpdesk",
                "PRIVATE-TOOL-PAYLOAD",
                fence,
                "PRIVATE-AFTER-FENCE",
                _answer("The API is sufficient."),
                "",
            ]
        )
        actual, _ = _last(monkeypatch, pane, dialect)
        assert actual == "● The API is sufficient."

    @pytest.mark.parametrize(
        "dialect", [kimi_cli_module.KimiDialect.LEGACY, kimi_cli_module.KimiDialect.CODE]
    )
    @pytest.mark.parametrize("styled", [False, True])
    def test_real_tool_headers_and_payload_stay_private(self, monkeypatch, styled, dialect):
        tool = "● Running a command · $ uname -a"
        if styled:
            tool = "\x1b[38;5;253m● \x1b[1m\x1b[38;5;111mRunning a command\x1b[0;2m · $ uname -a"
        pane = "\n".join(["💫 Check", tool, "PRIVATE-COMMAND-OUTPUT", _answer("Done."), ""])
        actual, _ = _last(monkeypatch, pane, dialect)
        assert actual == "● Done."

    @pytest.mark.parametrize("replacement", ["root-link", "leaf-link", "internal-link"])
    def test_scratch_cleanup_never_follows_symlinks(self, tmp_path, monkeypatch, replacement):
        monkeypatch.setattr(
            KimiCliProvider,
            "_managed_scratch_root",
            staticmethod(lambda: tmp_path / f"cao_kimi_{os.getuid()}"),
        )
        provider = KimiCliProvider("review-safety", "s", "w")
        directory = Path(provider._ensure_temp_dir())
        outside = tmp_path / "outside"
        outside.mkdir()
        sentinel = outside / "keep"
        sentinel.write_text("untouched")
        if replacement == "root-link":
            directory.rmdir()
            directory.parent.rmdir()
            directory.parent.symlink_to(outside, target_is_directory=True)
        elif replacement == "leaf-link":
            directory.rmdir()
            directory.symlink_to(outside, target_is_directory=True)
        else:
            (directory / "link").symlink_to(outside, target_is_directory=True)
        assert provider.cleanup() is (replacement != "root-link")
        assert sentinel.read_text() == "untouched"
        if replacement != "root-link":
            assert not os.path.lexists(directory)

    def test_scratch_cleanup_rejects_arbitrary_paths_and_preserves_neighbor(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            KimiCliProvider,
            "_managed_scratch_root",
            staticmethod(lambda: tmp_path / f"cao_kimi_{os.getuid()}"),
        )
        first = KimiCliProvider("review-one", "s", "w")
        second = KimiCliProvider("review-two", "s", "w")
        owned = Path(first._ensure_temp_dir())
        neighbor = Path(second._ensure_temp_dir())
        arbitrary = tmp_path / "do-not-delete"
        arbitrary.mkdir()
        first._temp_dir = str(arbitrary)
        assert not first._is_managed_scratch_dir(neighbor)
        assert not first._is_managed_scratch_dir(owned.parent)
        assert not first._is_managed_scratch_dir(owned / ".." / neighbor.name)
        assert first.cleanup() is True
        assert not owned.exists()
        assert arbitrary.exists() and neighbor.exists()

    @pytest.mark.parametrize(
        "failure", [PermissionError("busy"), FileNotFoundError("child vanished")]
    )
    def test_scratch_failure_is_retryable_after_restart(self, tmp_path, monkeypatch, failure):
        from unittest.mock import patch

        monkeypatch.setattr(
            KimiCliProvider,
            "_managed_scratch_root",
            staticmethod(lambda: tmp_path / f"cao_kimi_{os.getuid()}"),
        )
        provider = KimiCliProvider("review-retry", "s", "w")
        directory = Path(provider._ensure_temp_dir())
        (directory / "kimi-launch.sh").write_text("synthetic-secret")
        restarted = KimiCliProvider("review-retry", "s", "w")
        with patch.object(kimi_cli_module.shutil, "rmtree", side_effect=failure):
            assert restarted.cleanup() is False
        assert directory.exists()
        assert restarted.cleanup() is True
        assert not directory.exists()

    def test_scratch_launch_refuses_symlinked_script(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            KimiCliProvider,
            "_managed_scratch_root",
            staticmethod(lambda: tmp_path / f"cao_kimi_{os.getuid()}"),
        )
        provider = KimiCliProvider("review-script-link", "s", "w")
        directory = Path(provider._ensure_shell_safe_dir())
        outside = tmp_path / "untouched"
        outside.write_text("keep")
        (directory / "kimi-launch.sh").symlink_to(outside)
        with pytest.raises(OSError):
            provider._materialize_launch_command("synthetic-secret")
        assert outside.read_text() == "keep"

    @pytest.mark.parametrize("current", ["", "idle"])
    @pytest.mark.parametrize("status_probe", [None, "buffer", "screen"])
    def test_hostile_stale_history_cannot_confirm_new_dispatch(
        self, monkeypatch, current, status_probe
    ):
        from cli_agent_orchestrator.services import terminal_service as ts

        idle = _fixture("kimi_code_0431_01_fresh_startup_idle.txt")
        history = _fixture("kimi_code_0431_04_post_answer_idle.txt") + "\n" + idle
        provider = KimiCliProvider("review-stale-history", "s", "w")
        provider._dialect = kimi_cli_module.KimiDialect.CODE
        provider.mark_input_received()
        backend = MagicMock()
        backend.get_history.return_value = history
        monkeypatch.setattr(ts, "get_backend", lambda: backend)
        monkeypatch.setattr(kimi_cli_module, "get_backend", lambda: backend)
        monkeypatch.setattr(
            ts, "get_terminal_metadata", lambda _: {"tmux_session": "s", "tmux_window": "w"}
        )
        monkeypatch.setattr(ts.status_monitor, "get_buffer", lambda _: idle if current else "")
        if status_probe == "buffer":
            provider.get_status(history)
        elif status_probe == "screen":
            provider.get_status_from_screen(kt.strip_sgr(history).splitlines())
        assert ts._worker_is_started_direct(provider.terminal_id, provider) is False
        assert provider._execution_observed is False
        send = MagicMock()
        monkeypatch.setattr(ts, "send_input", send)
        monkeypatch.setattr(ts, "_message_visible_in_box", lambda *_: False)
        assert ts.redeliver_dropped_message(provider.terminal_id, "new task", 1, provider) is False
        send.assert_called_once()

    def test_hostile_temp_root_drift_cleans_original_script(self, tmp_path, monkeypatch):
        root_a, root_b = tmp_path / "rootA", tmp_path / "rootB"
        root_a.mkdir()
        root_b.mkdir()
        monkeypatch.setenv("TMPDIR", str(root_a))
        monkeypatch.setattr(kimi_cli_module, "shell_safe_temp_root", lambda: str(root_a))
        provider = KimiCliProvider("review-root-drift", "s", "w")
        provider._dialect = kimi_cli_module.KimiDialect.LEGACY
        provider._materialize_launch_command("kimi --mcp-config synthetic-test-secret")
        script = Path(provider._shell_safe_dir) / "kimi-launch.sh"
        assert "synthetic-test-secret" in script.read_text()
        monkeypatch.setenv("TMPDIR", str(root_b))
        monkeypatch.setattr(kimi_cli_module, "shell_safe_temp_root", lambda: str(root_b))
        restarted = KimiCliProvider("review-root-drift", "s", "w")
        restarted._dialect = kimi_cli_module.KimiDialect.LEGACY
        assert restarted._temp_dir is None
        assert restarted.cleanup() is True
        assert not script.exists()

    def test_fixed_scratch_root_preserves_install_and_terminal_namespaces(
        self, tmp_path, monkeypatch
    ):
        first_home, second_home = tmp_path / "install-a", tmp_path / "install-b"
        monkeypatch.setattr(kimi_cli_module, "CAO_HOME_DIR", first_home)
        first = KimiCliProvider("same-terminal", "s", "w")
        neighbor = KimiCliProvider("other-terminal", "s", "w")
        first_path = Path(first._ensure_temp_dir())
        neighbor_path = Path(neighbor._ensure_temp_dir())
        monkeypatch.setattr(kimi_cli_module, "CAO_HOME_DIR", second_home)
        other_install = KimiCliProvider("same-terminal", "s", "w")
        other_path = Path(other_install._ensure_temp_dir())
        assert first_path.parent == neighbor_path.parent == other_path.parent
        assert first_path.parent == Path("/tmp").resolve() / f"cao_kimi_{os.getuid()}"
        assert len({first_path, neighbor_path, other_path}) == 3
        assert other_install.cleanup() is True
        assert first_path.exists() and neighbor_path.exists()
        monkeypatch.setattr(kimi_cli_module, "CAO_HOME_DIR", first_home)
        assert first.cleanup() is True
        assert neighbor_path.exists()
        assert neighbor.cleanup() is True

    @pytest.mark.parametrize(
        "dialect", [kimi_cli_module.KimiDialect.LEGACY, kimi_cli_module.KimiDialect.CODE]
    )
    @pytest.mark.parametrize("new_activity", [False, True])
    def test_post_clear_settled_redraw_requires_new_activity(
        self, monkeypatch, dialect, new_activity
    ):
        from cli_agent_orchestrator.services import terminal_service as ts
        from cli_agent_orchestrator.services.status_monitor import StatusMonitor

        provider = KimiCliProvider("review-redraw", "s", "w")
        provider._dialect = dialect
        monitor = StatusMonitor()
        old = "\n".join(
            [
                (
                    _user("✨ Previous task")
                    if dialect is kimi_cli_module.KimiDialect.CODE
                    else "💫 Previous task"
                ),
                "",
                _thinking("Previous reasoning"),
                "● Used search_docs · MCP/helpdesk",
                "private old payload",
                _answer("Previous final answer."),
                _footer() if dialect is kimi_cli_module.KimiDialect.CODE else "💫",
            ]
        )
        activity = (
            "\x1b[38;5;111m⠙ Thinking… 1s · 4 tokens\x1b[0m"
            if dialect is kimi_cli_module.KimiDialect.CODE
            else "🌑"
        )
        provider.mark_input_received()
        assert provider.has_execution_evidence(activity + "\n") is True
        monitor._buffers[provider.terminal_id] = old
        backend = MagicMock()
        backend.get_history.return_value = old

        def paste_or_drop(*args, **kwargs):
            assert monitor.get_buffer(provider.terminal_id) == ""
            assert provider._execution_observed is False
            # The previous screen is repainted AFTER clear/mark. A successful
            # fast turn additionally emits live activity followed by completion.
            monitor._buffers[provider.terminal_id] = old + (
                "\n" + activity + "\n" + _answer("New final answer.") if new_activity else ""
            )

        backend.send_keys.side_effect = paste_or_drop
        monkeypatch.setattr(ts, "status_monitor", monitor)
        monkeypatch.setattr(ts, "get_backend", lambda: backend)
        monkeypatch.setattr(
            ts, "get_terminal_metadata", lambda _: {"tmux_session": "s", "tmux_window": "w"}
        )
        monkeypatch.setattr(ts.provider_manager, "get_provider", lambda _: provider)
        monkeypatch.setattr(ts, "inject_memory_context", lambda message, *_: message)
        monkeypatch.setattr(ts, "update_last_active", lambda _: None)
        assert ts.send_input(provider.terminal_id, "New task") is True
        assert ts._worker_is_started_direct(provider.terminal_id, provider) is new_activity
        assert provider._execution_observed is new_activity
        resend = MagicMock()
        monkeypatch.setattr(ts, "send_input", resend)
        monkeypatch.setattr(ts, "_message_visible_in_box", lambda *_: False)
        assert (
            ts.redeliver_dropped_message(provider.terminal_id, "New task", 1, provider)
            is new_activity
        )
        assert resend.call_count == (0 if new_activity else 1)
        backend.get_history.assert_not_called()

    @pytest.mark.parametrize(
        "dialect", [kimi_cli_module.KimiDialect.LEGACY, kimi_cli_module.KimiDialect.CODE]
    )
    @pytest.mark.parametrize(
        "old",
        [
            _answer("Old final answer"),
            "● Used search_docs · MCP/helpdesk\nold payload",
            _thinking("Old reasoning"),
            _user("✨ Old submitted task"),
        ],
    )
    def test_redrawn_transcript_channels_are_not_activity(self, dialect, old):
        provider = KimiCliProvider("review-old-channels", "s", "w")
        provider._dialect = dialect
        provider.mark_input_received()
        assert provider.has_execution_evidence(old) is False
        assert provider.has_execution_evidence(old + "\n") is False
        assert provider._execution_observed is False

    @pytest.mark.parametrize(
        "dialect", [kimi_cli_module.KimiDialect.LEGACY, kimi_cli_module.KimiDialect.CODE]
    )
    @pytest.mark.parametrize("boundary", ["mark", "epoch"])
    def test_activity_is_scoped_to_execution_generation(self, dialect, boundary):
        provider = KimiCliProvider("review-generations", "s", "w")
        provider._dialect = dialect
        provider.notify_status_buffer_reset(1)
        provider.mark_input_received()
        activity = (
            "⠙ Thinking… 1s · 4 tokens" if dialect is kimi_cli_module.KimiDialect.CODE else "🌑"
        )
        settled = _answer("Identical answer in consecutive turns.")
        assert provider._awaiting_turn is True
        assert provider._turn_activity_seen is False
        assert provider.has_execution_evidence(settled) is False
        assert provider.has_execution_evidence(activity + "\n") is True
        assert provider._turn_activity_seen is True
        assert provider.has_execution_evidence(settled) is True
        provider.notify_status_buffer_reset(1)  # An unchanged epoch is not a reset.
        assert provider.has_execution_evidence("") is True
        if boundary == "mark":
            provider.mark_input_received()
        else:
            provider.notify_status_buffer_reset(2)
        assert provider._awaiting_turn is True
        assert provider._turn_activity_seen is False
        assert provider.has_execution_evidence(settled) is False
        assert provider.has_execution_evidence(activity + "\n" + settled) is True

    @pytest.mark.parametrize(
        "dialect", [kimi_cli_module.KimiDialect.LEGACY, kimi_cli_module.KimiDialect.CODE]
    )
    def test_fast_turn_raw_cursor_frames_preserve_activity(self, dialect):
        provider = KimiCliProvider("review-raw-fast", "s", "w")
        provider._dialect = dialect
        provider.mark_input_received()
        label = (
            "Thinking… 1s · 4 tokens"
            if dialect is kimi_cli_module.KimiDialect.CODE
            else "Using Shell (pwd)"
        )
        raw = "\x1b[1G\x1b[2K\x1b[38;5;111m⠙ " + label + "\x1b[0m"
        raw += "\r\x1b[2K" + _answer("Completed before the first probe.")
        assert provider.has_execution_evidence(raw) is True

    @pytest.mark.parametrize(
        "dialect", [kimi_cli_module.KimiDialect.LEGACY, kimi_cli_module.KimiDialect.CODE]
    )
    @pytest.mark.parametrize(
        "old",
        [
            _answer("⠙ Thinking… is an example, not activity"),
            _answer("Example:") + "\n```text\n⠙ Thinking… 1s · 4 tokens\n```",
            "● Used search_docs · MCP/helpdesk\n   ⠙ Thinking… from a captured log",
            _thinking("Example:")
            + "\n"
            + _reasoning_continuation("⠙ Thinking… from a captured log"),
        ],
    )
    def test_quoted_processing_in_settled_content_is_not_activity(self, dialect, old):
        provider = KimiCliProvider("review-quoted-activity", "s", "w")
        provider._dialect = dialect
        provider.mark_input_received()
        assert provider.has_execution_evidence(old) is False
        assert provider.has_execution_evidence(old + "\n") is False

    @pytest.mark.parametrize("screen", [False, True])
    def test_generic_status_cannot_import_old_processing_into_generation(self, screen):
        provider = KimiCliProvider("review-old-spinner", "s", "w")
        provider._dialect = kimi_cli_module.KimiDialect.CODE
        provider.mark_input_received()
        old = "⠙ Thinking… 1s · 4 tokens\n" + _answer("Old final answer.")
        if screen:
            provider.get_status_from_screen(kt.strip_sgr(old).splitlines())
        else:
            provider.get_status(old)
        assert provider._turn_activity_seen is False
        assert provider.has_execution_evidence("") is False

    @pytest.mark.parametrize(
        "dialect", [kimi_cli_module.KimiDialect.LEGACY, kimi_cli_module.KimiDialect.CODE]
    )
    def test_review2_new_submission_displaces_stale_answer_fence(self, monkeypatch, dialect):
        pane = "\n".join(
            [
                _answer("Old example:"),
                "```text",
                _user("✨ NEW PRIVATE USER PROMPT"),
                "● Used search_docs · MCP/helpdesk",
                "\x1b[2mPRIVATE-TOOL-PAYLOAD\x1b[0m",
                "\x1b[2m```\x1b[0m",
                _answer("Done."),
            ]
        )
        assert _last(monkeypatch, pane, dialect)[0] == "● Done."

    def test_review2_partial_quoted_spinner_cannot_latch(self, monkeypatch):
        from cli_agent_orchestrator.services import terminal_service as ts

        provider = KimiCliProvider("review-partial-fence", "s", "w")
        provider._dialect = kimi_cli_module.KimiDialect.CODE
        provider.mark_input_received()
        monkeypatch.setattr(
            ts, "get_terminal_metadata", lambda _: {"tmux_session": "s", "tmux_window": "w"}
        )
        partial = _answer("Old example:") + "\n```text\n⠙ Thinking… 1s · 4 tokens\n"
        monkeypatch.setattr(ts.status_monitor, "get_buffer", lambda _: partial)
        before_close = ts._worker_is_started_direct(provider.terminal_id, provider)
        partial += "```\n"
        after_close = ts._worker_is_started_direct(provider.terminal_id, provider)
        assert (before_close, after_close, provider._execution_observed) == (False, False, False)

    def test_review2_cursor_boundary_preserves_answer_style(self):
        provider = KimiCliProvider("review-cursor-style", "s", "w")
        provider._dialect = kimi_cli_module.KimiDialect.CODE
        provider.mark_input_received()
        raw = "\x1b[38;5;253m\x1b[1G⠙ Thinking… quoted in an old answer"
        assert provider.has_execution_evidence(raw) is False
        assert provider.has_execution_evidence(raw + "\n") is False

    @pytest.mark.parametrize(
        "dialect", [kimi_cli_module.KimiDialect.LEGACY, kimi_cli_module.KimiDialect.CODE]
    )
    @pytest.mark.parametrize("delivery", ["large", "large-burst", "fast", "dropped"])
    def test_review2_real_monitor_observes_activity_before_eviction(
        self, monkeypatch, dialect, delivery
    ):
        from cli_agent_orchestrator.services import status_monitor as sm
        from cli_agent_orchestrator.services import terminal_service as ts

        provider = KimiCliProvider("review-monitor-eviction", "s", "w")
        provider._dialect = dialect
        monitor = sm.StatusMonitor()
        assert sm.get_server_settings()["state_buffer_max"] == 32768
        activity = (
            "\x1b[38;5;111m⠙ Thinking… 1s · 4 tokens\x1b[0m\n"
            if dialect is kimi_cli_module.KimiDialect.CODE
            else "🌑\n"
        )
        answer = _answer("Done. " + ("x" * 40000 if delivery.startswith("large") else ""))
        monitor._buffers[provider.terminal_id] = _answer("Old response")
        backend = MagicMock()

        def paste_or_drop(*args, **kwargs):
            assert monitor.get_buffer(provider.terminal_id) == ""
            assert provider._execution_observed is False
            if delivery == "large-burst":
                monitor._process_chunk(provider.terminal_id, activity + answer)
            else:
                if delivery != "dropped":
                    monitor._process_chunk(provider.terminal_id, activity)
                monitor._process_chunk(provider.terminal_id, answer)

        backend.send_keys.side_effect = paste_or_drop
        monkeypatch.setattr(ts, "status_monitor", monitor)
        monkeypatch.setattr(ts, "get_backend", lambda: backend)
        monkeypatch.setattr(
            ts, "get_terminal_metadata", lambda _: {"tmux_session": "s", "tmux_window": "w"}
        )
        monkeypatch.setattr(ts.provider_manager, "get_provider", lambda _: provider)
        monkeypatch.setattr(sm.provider_manager, "get_provider", lambda _: provider)
        # Disable only generic status scheduling: it is deliberately not acceptance evidence.
        monkeypatch.setattr(monitor, "_schedule_raw_detection", lambda *_: None)
        monkeypatch.setattr(monitor, "_schedule_screen_detection", lambda *_: None)
        monkeypatch.setattr(ts, "inject_memory_context", lambda message, *_: message)
        monkeypatch.setattr(ts, "update_last_active", lambda _: None)
        assert ts.send_input(provider.terminal_id, "New task") is True
        if delivery.startswith("large"):
            assert len(monitor.get_buffer(provider.terminal_id)) == 32768
            assert activity.strip() not in monitor.get_buffer(provider.terminal_id)
        accepted = ts._worker_is_started_direct(provider.terminal_id, provider)
        resend = MagicMock()
        monkeypatch.setattr(ts, "send_input", resend)
        monkeypatch.setattr(ts, "_message_visible_in_box", lambda *_: False)
        result = ts.redeliver_dropped_message(provider.terminal_id, "New task", 1, provider)
        expected = delivery != "dropped"
        assert (accepted, result, resend.call_count) == (expected, expected, 0 if expected else 1)

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("\x1b[38;5;111m\x1b[1G⠙ Thinking… live", True),
            ("\x1b[38;5;253mold\r⠙ Thinking… quoted", False),
            ("\x1b[38;5;253mold\r\x1b[38;5;111m⠙ Thinking… live", True),
            ("\x1b[38;5;253mold\r\x1b[0m⠙ Thinking… live", True),
            ("\x1b[38;5;253mold\r\x1b[39m⠙ Thinking… live", True),
            ("\x1b[38;5;244m● \x1b[3mold\r⠙ Thinking… quoted", False),
        ],
    )
    def test_review2_cursor_style_inheritance_and_reset(self, raw, expected):
        provider = KimiCliProvider("review-style-controls", "s", "w")
        provider._dialect = kimi_cli_module.KimiDialect.CODE
        provider.mark_input_received()
        # Include a row boundary so both acceptance and style rejection are
        # tested after the streaming row is eligible for classification.
        assert provider.has_execution_evidence(raw + "\n") is expected

    @pytest.mark.parametrize("fence", ["```text", "~~~~text"])
    def test_review2_unclosed_fence_ends_at_positive_submission(self, fence):
        provider = KimiCliProvider("review-fence-generation", "s", "w")
        provider._dialect = kimi_cli_module.KimiDialect.CODE
        provider.mark_input_received()
        old = _answer("Old example:") + "\n" + fence + "\n⠙ Thinking… quoted\n"
        assert provider.has_execution_evidence(old) is False
        current = old + _user("✨ New task") + "\n⠙ Thinking… 1s · 4 tokens\n"
        assert provider.has_execution_evidence(current) is True

    @pytest.mark.parametrize("quoted", [False, True])
    def test_review2_real_monitor_chunk_boundaries_do_not_change_ownership(
        self, monkeypatch, quoted
    ):
        from cli_agent_orchestrator.services import status_monitor as sm

        provider = KimiCliProvider("review-chunk-boundaries", "s", "w")
        provider._dialect = kimi_cli_module.KimiDialect.CODE
        monitor = sm.StatusMonitor()
        monkeypatch.setattr(sm.provider_manager, "get_provider", lambda _: provider)
        monkeypatch.setattr(monitor, "_schedule_raw_detection", lambda *_: None)
        monkeypatch.setattr(monitor, "_schedule_screen_detection", lambda *_: None)
        monitor.clear_rolling_buffer(provider.terminal_id, provider)
        provider.mark_input_received()
        raw = (
            _answer("Old example:") + "\n```text\n⠙ Thinking… 1s · 4 tokens\n"
            if quoted
            else "\x1b[38;5;253m\x1b[1G⠙ Thinking… quoted in an old answer\n"
        )
        for character in raw:
            monitor._process_chunk(provider.terminal_id, character)
            assert provider._execution_observed is False
        if quoted:
            monitor._process_chunk(provider.terminal_id, "```\n")
            assert provider._execution_observed is False
        monitor._process_chunk(provider.terminal_id, "\x1b[0m⠙ Thinking… 2s · 8 tokens\n")
        assert provider._execution_observed is True

    @pytest.mark.parametrize("private", ["fence", "tool"])
    def test_review2_evicted_ownership_is_ambiguous_and_cannot_authorize_resend(
        self, monkeypatch, private
    ):
        from cli_agent_orchestrator.services import status_monitor as sm
        from cli_agent_orchestrator.services import terminal_service as ts

        provider = KimiCliProvider("review-evicted-ownership", "s", "w")
        provider._dialect = kimi_cli_module.KimiDialect.CODE
        monitor = sm.StatusMonitor()
        monkeypatch.setattr(sm.provider_manager, "get_provider", lambda _: provider)
        monkeypatch.setattr(monitor, "_schedule_raw_detection", lambda *_: None)
        monkeypatch.setattr(monitor, "_schedule_screen_detection", lambda *_: None)
        monkeypatch.setattr(ts, "status_monitor", monitor)
        monkeypatch.setattr(
            ts, "get_terminal_metadata", lambda _: {"tmux_session": "s", "tmux_window": "w"}
        )
        monkeypatch.setattr(ts, "_message_visible_in_box", lambda *_: False)
        resend = MagicMock()
        monkeypatch.setattr(ts, "send_input", resend)
        monitor.clear_rolling_buffer(provider.terminal_id, provider)
        provider.mark_input_received()
        prefix = (
            _answer("Old example:") + "\n```text\n"
            if private == "fence"
            else "● Used search_docs · MCP/helpdesk\n"
        )
        monitor._process_chunk(provider.terminal_id, prefix + "payload\n" * 5000)
        monitor._process_chunk(provider.terminal_id, "\n⠙ Thinking… quoted\n")
        assert ts._worker_is_started_direct(provider.terminal_id, provider) is False
        assert provider.execution_evidence_ambiguous is True
        assert ts.redeliver_dropped_message(provider.terminal_id, "New task", 1, provider) is False
        resend.assert_not_called()
        monitor.clear_rolling_buffer(provider.terminal_id, provider)
        provider.mark_input_received()
        assert provider.execution_evidence_ambiguous is False
        assert ts.redeliver_dropped_message(provider.terminal_id, "New task", 1, provider) is False
        resend.assert_called_once()

    @pytest.mark.parametrize(
        "dialect", [kimi_cli_module.KimiDialect.LEGACY, kimi_cli_module.KimiDialect.CODE]
    )
    @pytest.mark.parametrize(
        "row",
        [
            "⠧ MCP Servers: 0/1 connected",
            "⠋ Loading configuration...",
            "⠏ Restoring conversation...",
            "⠦ custom-service (connecting)",
        ],
    )
    @pytest.mark.parametrize("delivery", ["split-slot", "characters"])
    def test_review3_real_monitor_split_boot_cannot_certify_execution(
        self, monkeypatch, dialect, row, delivery
    ):
        from cli_agent_orchestrator.services import status_monitor as sm
        from cli_agent_orchestrator.services import terminal_service as ts

        provider = KimiCliProvider("review-split-boot", "s", "w")
        provider._dialect = dialect
        monitor = sm.StatusMonitor()
        monkeypatch.setattr(sm.provider_manager, "get_provider", lambda _: provider)
        monkeypatch.setattr(monitor, "_schedule_raw_detection", lambda *_: None)
        monkeypatch.setattr(monitor, "_schedule_screen_detection", lambda *_: None)
        monkeypatch.setattr(ts, "status_monitor", monitor)
        monkeypatch.setattr(
            ts, "get_terminal_metadata", lambda _: {"tmux_session": "s", "tmux_window": "w"}
        )
        monitor.clear_rolling_buffer(provider.terminal_id, provider)
        provider.mark_input_received()
        chunks = [row[:2], row[2:], "\n"] if delivery == "split-slot" else list(row + "\n")
        observed = []
        for chunk in chunks:
            monitor._process_chunk(provider.terminal_id, chunk)
            observed.append(
                (
                    ts._worker_is_started_direct(provider.terminal_id, provider),
                    provider._turn_activity_seen,
                    provider._execution_observed,
                )
            )
        assert kt.classify_line(row) is kt.KimiLineKind.BOOT_CHROME
        # Record the entire sequence: later boot classification cannot undo an
        # earlier irreversible acceptance, including through the direct probe.
        assert observed == [(False, False, False)] * len(chunks)
        assert provider.execution_evidence_ambiguous is False
        resend = MagicMock()
        monkeypatch.setattr(ts, "send_input", resend)
        monkeypatch.setattr(ts, "_message_visible_in_box", lambda *_: False)
        assert ts.redeliver_dropped_message(provider.terminal_id, "New task", 1, provider) is False
        resend.assert_called_once()

    @pytest.mark.parametrize(
        "dialect, row",
        [
            (kimi_cli_module.KimiDialect.CODE, "⠙ Thinking… 1s · 4 tokens"),
            (kimi_cli_module.KimiDialect.LEGACY, "⠼ Using Shell (pwd)"),
            (kimi_cli_module.KimiDialect.LEGACY, "🌑"),
        ],
    )
    @pytest.mark.parametrize("boundary", ["\n", "\r", "\x1b[1G"])
    def test_review3_real_monitor_split_processing_waits_for_row_boundary(
        self, monkeypatch, dialect, row, boundary
    ):
        from cli_agent_orchestrator.services import status_monitor as sm
        from cli_agent_orchestrator.services import terminal_service as ts

        provider = KimiCliProvider("review-split-processing", "s", "w")
        provider._dialect = dialect
        monitor = sm.StatusMonitor()
        monkeypatch.setattr(sm.provider_manager, "get_provider", lambda _: provider)
        monkeypatch.setattr(monitor, "_schedule_raw_detection", lambda *_: None)
        monkeypatch.setattr(monitor, "_schedule_screen_detection", lambda *_: None)
        monkeypatch.setattr(ts, "status_monitor", monitor)
        monkeypatch.setattr(
            ts, "get_terminal_metadata", lambda _: {"tmux_session": "s", "tmux_window": "w"}
        )
        monitor.clear_rolling_buffer(provider.terminal_id, provider)
        provider.mark_input_received()
        raw = "\x1b[38;5;111m" + row + "\x1b[0m" + boundary
        for character in raw[:-1]:
            monitor._process_chunk(provider.terminal_id, character)
            assert ts._worker_is_started_direct(provider.terminal_id, provider) is False
            assert provider._turn_activity_seen is False
            assert provider._execution_observed is False
        monitor._process_chunk(provider.terminal_id, raw[-1])
        assert ts._worker_is_started_direct(provider.terminal_id, provider) is True
        assert provider._turn_activity_seen is True
        assert provider._execution_observed is True
        monitor._process_chunk(provider.terminal_id, _answer("Done. " + "x" * 40000))
        assert ts._worker_is_started_direct(provider.terminal_id, provider) is True
        resend = MagicMock()
        monkeypatch.setattr(ts, "send_input", resend)
        monkeypatch.setattr(ts, "_message_visible_in_box", lambda *_: False)
        assert ts.redeliver_dropped_message(provider.terminal_id, "New task", 1, provider) is True
        resend.assert_not_called()
        monitor.clear_rolling_buffer(provider.terminal_id, provider)
        provider.mark_input_received()
        assert ts._worker_is_started_direct(provider.terminal_id, provider) is False
