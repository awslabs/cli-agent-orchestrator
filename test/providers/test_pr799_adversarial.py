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


def _user(text: str) -> str:
    """A submitted-message row as Kimi Code draws it (bold + colour 222)."""
    return "\x1b[1;38;5;222m" + text + "\x1b[0m"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


def _last(monkeypatch, pane: str):
    """Run the public ``get_output(mode=LAST)`` path against ``pane``."""

    from cli_agent_orchestrator.services import terminal_service

    provider = KimiCliProvider("term-adv", "session-1", "window-1")
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
        mcp_doc = json.loads((tmp_path / "kimi-home" / "mcp.json").read_text(encoding="utf-8"))
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
            ["context: 2% (14.8k/977k)"],
            ["╭────────────╮", "│ >          │", "╰────────────╯"],
            [""],
            ["", "context: 2% (14.8k/977k)"],
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

        pane = "\n".join([_thinking(PRIVATE_REASONING), "context: 2% (14.8k/977k)"])
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
                "context: 2% (14.8k/977k)",
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
            provider.extract_last_message_from_script("context: 2% (14.8k/977k)")
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
