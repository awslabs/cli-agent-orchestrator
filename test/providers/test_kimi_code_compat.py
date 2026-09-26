"""Kimi Code compatibility regression suite (A2).

Companion to ``test_kimi_cli_unit.py``. That file pins the **legacy** behaviour;
this file pins the **Kimi Code** behaviour and the shared machinery the two now
depend on:

* ``A1.1`` dialect detection from capabilities (never version strings),
* ``A1.2`` working-directory contract (real cwd, no ``cd``),
* ``A1.3`` per-worker ``KIMI_CODE_HOME`` builder,
* ``A1.4`` MCP merge semantics,
* ``A1.5`` launch-scoped timeouts + auto-update suppression,
* ``A1.6`` Markdown agent contract (``${base_prompt}`` preserved),
* ``A1.7`` workspace-trust handling,
* ``A2.2``/``A2.3``/``A2.4`` transcript classification against the A0 fixtures.

The ``kimi_code_0431_*`` fixtures are scrubbed ``tmux capture-pane -p -e``
captures of Kimi Code **0.43.1** (see ``fixtures/METADATA.json`` in the A0
evidence root). They are the only end-to-end truth available here, so the
status/extraction tests drive the real ``get_status()`` /
``extract_last_message_from_script()`` against them rather than against
hand-written approximations.
"""

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers import kimi_cli as kimi_cli_module
from cli_agent_orchestrator.providers import kimi_transcript as kt
from cli_agent_orchestrator.providers.base import OutputExtractionError
from cli_agent_orchestrator.providers.kimi_cli import (
    CODE_CAPABILITY_FLAGS,
    KIMI_MCP_STARTUP_TIMEOUT_MS,
    KIMI_MCP_TOOL_TIMEOUT_MS,
    KIMI_NO_AUTO_UPDATE_ENV,
    KIMI_PROBE_END_MARKER,
    KIMI_TRUST_OPT_IN_ENV,
    LEGACY_CAPABILITY_FLAGS,
    KimiCliProvider,
    KimiDialect,
    KimiProbeResult,
    _cache_dialect,
    _cached_dialect,
    classify_kimi_capabilities,
    kimi_trust_opt_in,
    reset_dialect_cache,
)
from cli_agent_orchestrator.providers.kimi_runtime_home import (
    MAX_TRUST_ENTRIES,
    NEVER_COPY,
    PRESERVE_DIRS,
    PRESERVE_FILES,
    TRUST_DIR_NAME,
    KimiCodeRuntimeHomeBuilder,
    iter_forbidden_runtime_state,
    kimi_agent_name,
    merge_mcp_servers,
    read_user_mcp_servers,
    resolve_source_home,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8", errors="replace")


# =============================================================================
# Help-text samples
# =============================================================================

# Verbatim (trimmed) from `kimi --help` on Kimi Code 0.43.1. Every flag the
# classifier keys on is reproduced with its real surrounding punctuation, so the
# `_has_cli_flag` word-boundary behaviour is exercised for real.
KIMI_CODE_HELP = """\
Usage: kimi [options] [command]

Options:
  -V, --version                 output the version number
  -S, --session [id]            Resume a session.
  -c, --continue                Continue the previous session for the working directory.
  -y, --yolo                    Start in Ask When Needed mode: routine edits and commands run
                                automatically; risky actions, questions, and plans still ask.
  --auto                        Start in Never Ask mode: never interrupts you; everything runs and
                                is decided automatically. (default: false)
  -m, --model <model>           LLM model alias to use for this invocation.
  --output-format <format>      Output format for prompt mode. Defaults to text.
  --skills-dir <dir>            Load skills from this directory instead of auto-discovered user and
                                project directories. Can be repeated. (default: [])
  --agent <name>                Agent profile to start the new session with.
  --agent-file <path>           Load an agent definition from a Markdown file and select it for the
                                new session. (default: [])
  --add-dir <dir>               Add an additional workspace directory for this session.
  --plan                        Start in plan mode. (default: false)
  -h, --help                    Show help.
"""

# Reconstructed legacy `kimi-cli` surface: the MCP mechanism is a CLI flag and
# neither `--auto` nor `--output-format` exists. `--agent-file` is deliberately
# included because legacy CAO already passes it (a YAML agent file), which is
# exactly why the CODE signature is an AND over three flags rather than a
# single-flag test.
LEGACY_HELP = """\
Usage: kimi [options]

Options:
  -m, --model <model>           Model alias.
  --mcp-config <json>           MCP servers configuration as a JSON string.
  --mcp-config-file <path>      MCP servers configuration file.
  --agent-file <path>           Agent file (YAML).
  -y, --yolo                    Auto-approve all tool actions.
  -h, --help                    Show help.
"""


def _code_provider(terminal_id: str = "term-code", binary: str = "/usr/bin/kimi"):
    """A provider pre-armed as if the launch-shell probe returned CODE."""

    provider = KimiCliProvider(terminal_id, "session-1", "window-1")
    provider._kimi_binary = binary
    provider._dialect = KimiDialect.CODE
    return provider


# =============================================================================
# A1.1 — dialect detection
# =============================================================================


class TestKimiDialectDetection:
    """Capability-based dialect classification. No version strings anywhere."""

    def test_code_help_classifies_code(self):
        dialect, observed = classify_kimi_capabilities(KIMI_CODE_HELP)
        assert dialect is KimiDialect.CODE
        assert observed["--auto"] is True
        assert observed["--agent-file"] is True
        assert observed["--output-format"] is True
        assert observed["--mcp-config"] is False

    def test_legacy_help_classifies_legacy(self):
        dialect, observed = classify_kimi_capabilities(LEGACY_HELP)
        assert dialect is KimiDialect.LEGACY
        assert observed["--mcp-config"] is True
        assert observed["--auto"] is False

    def test_both_signatures_is_unknown(self):
        """Contradictory evidence must not be resolved by precedence."""

        dialect, _ = classify_kimi_capabilities(KIMI_CODE_HELP + LEGACY_HELP)
        assert dialect is KimiDialect.UNKNOWN

    def test_neither_signature_is_unknown(self):
        dialect, _ = classify_kimi_capabilities("Usage: kimi\n\nOptions:\n  -h, --help\n")
        assert dialect is KimiDialect.UNKNOWN

    def test_empty_help_is_unknown(self):
        dialect, _ = classify_kimi_capabilities("")
        assert dialect is KimiDialect.UNKNOWN

    def test_partial_code_signature_is_unknown(self):
        """Two of the three CODE markers is not enough — no guessing."""

        partial = KIMI_CODE_HELP.replace("  --output-format <format>", "  --no-such-flag")
        dialect, _ = classify_kimi_capabilities(partial)
        assert dialect is KimiDialect.UNKNOWN

    def test_flag_match_is_word_bounded_not_substring(self):
        """`--auto-update` must never satisfy the `--auto` marker."""

        dialect, observed = classify_kimi_capabilities(
            "  --auto-update <mode>   Update policy.\n"
            "  --agent-file <path>    Agent file.\n"
            "  --output-format <f>    Format.\n"
        )
        assert observed["--auto"] is False
        assert dialect is KimiDialect.UNKNOWN

    def test_flag_match_accepts_line_start_and_comma_context(self):
        assert classify_kimi_capabilities("--auto --agent-file <p> --output-format <f>")[0] is (
            KimiDialect.CODE
        )
        assert classify_kimi_capabilities("--mcp-config=<json>")[0] is KimiDialect.LEGACY

    def test_signature_flag_sets_are_disjoint_where_it_matters(self):
        """The CODE markers must not include the legacy MCP mechanism."""

        assert "--mcp-config" not in CODE_CAPABILITY_FLAGS
        assert "--mcp-config-file" not in CODE_CAPABILITY_FLAGS
        assert set(LEGACY_CAPABILITY_FLAGS) & {"--auto", "--output-format"} == set()

    def test_real_0431_help_is_code(self):
        """The live 0.43.1 help text captured on the A0 host must classify CODE."""

        assert classify_kimi_capabilities(KIMI_CODE_HELP)[0] is KimiDialect.CODE

    def test_successful_classification_is_cached(self, tmp_path):
        binary = tmp_path / "kimi"
        binary.write_text("#!/bin/sh\n")
        reset_dialect_cache()
        assert _cached_dialect(str(binary)) is None
        _cache_dialect(str(binary), KimiDialect.CODE, {"--auto": True})
        cached = _cached_dialect(str(binary))
        assert cached is not None
        assert cached[0] is KimiDialect.CODE

    def test_unknown_is_never_cached(self, tmp_path):
        """A transient probe failure must not become sticky for the process."""

        binary = tmp_path / "kimi"
        binary.write_text("#!/bin/sh\n")
        reset_dialect_cache()
        _cache_dialect(str(binary), KimiDialect.UNKNOWN, {})
        assert _cached_dialect(str(binary)) is None

    def test_cache_is_keyed_on_binary_identity(self, tmp_path):
        """Replacing the binary in place must invalidate the cache entry."""

        binary = tmp_path / "kimi"
        binary.write_text("#!/bin/sh\necho old\n")
        reset_dialect_cache()
        _cache_dialect(str(binary), KimiDialect.LEGACY, {})
        assert _cached_dialect(str(binary))[0] is KimiDialect.LEGACY

        binary.write_text("#!/bin/sh\necho a much longer replacement\n")
        assert _cached_dialect(str(binary)) is None

    def test_missing_binary_is_not_cached(self, tmp_path):
        reset_dialect_cache()
        _cache_dialect(str(tmp_path / "absent"), KimiDialect.CODE, {})
        assert _cached_dialect(str(tmp_path / "absent")) is None

    def test_reset_dialect_cache_clears(self, tmp_path):
        binary = tmp_path / "kimi"
        binary.write_text("x")
        _cache_dialect(str(binary), KimiDialect.CODE, {})
        assert _cached_dialect(str(binary)) is not None
        reset_dialect_cache()
        assert _cached_dialect(str(binary)) is None

    def test_probe_result_defaults(self):
        probe = KimiProbeResult(KimiDialect.CODE, "/usr/bin/kimi", Path("/home/u/.kimi-code"))
        assert probe.observed == {}
        assert probe.binary == "/usr/bin/kimi"


# =============================================================================
# A2.2 / A2.3 / A2.4 — transcript classification
# =============================================================================


class TestKimiTranscriptClassifier:
    """The single semantic classifier both dialects route through."""

    def test_bullet_glyphs_both_accepted(self):
        assert kt.BULLET_ANY_RE.match("• legacy bullet")
        assert kt.BULLET_ANY_RE.match("● kimi code bullet")

    def test_indented_bullet_accepted(self):
        assert kt.BULLET_ANY_RE.match("  ● indented")

    def test_status_bar_bullet_rejected(self):
        """A `●` inside the footer is chrome, not assistant output (A0 D2)."""

        assert not kt.BULLET_ANY_RE.match("agent (Kimi-k2.6 ●)")
        assert not kt.BULLET_ANY_RE.match("context: 4.0% (10.4k/262.1k)")

    def test_embedded_bullet_rejected(self):
        assert not kt.BULLET_ANY_RE.match("text ● not a bullet")

    def test_final_bullet_classified(self):
        raw = "\x1b[38;5;253m● \x1b[39mSTEP 1"
        assert kt.classify_line(raw) is kt.KimiLineKind.FINAL_BULLET

    def test_thinking_bullet_classified_grey_plus_italic(self):
        raw = "\x1b[38;5;244m● \x1b[3m**Generating Fixture List**\x1b[0m"
        assert kt.classify_line(raw) is kt.KimiLineKind.THINKING_BULLET

    def test_thinking_bullet_classified_grey_only(self):
        raw = "\x1b[38;5;244m•\x1b[39m reasoning text"
        assert kt.classify_line(raw) is kt.KimiLineKind.THINKING_BULLET

    def test_thinking_bullet_classified_truecolor(self):
        raw = "\x1b[38;2;128;128;128m● reasoning"
        assert kt.classify_line(raw) is kt.KimiLineKind.THINKING_BULLET

    def test_plain_bullet_without_grey_is_final(self):
        """Legacy fixtures have unstyled `•` — they must stay answers."""

        assert kt.classify_line("• Hello!") is kt.KimiLineKind.FINAL_BULLET

    def test_braille_row_is_live_spinner(self):
        raw = "\x1b[38;5;111m⠙\x1b[39m working…"
        assert kt.classify_line(raw) is kt.KimiLineKind.LIVE_SPINNER

    def test_moon_tip_row_is_idle_tip_not_spinner(self):
        """A0 D1: the idle rotating tip carries a moon phase."""

        raw = "\U0001f315\x1b[38;5;244m · Tip: ctrl-s to add guidance"
        assert kt.classify_line(raw) is kt.KimiLineKind.IDLE_TIP

    def test_bare_moon_without_tip_is_not_idle_tip(self):
        """A bare moon is ambiguous, so it is not silently reclassified."""

        assert kt.classify_line("\U0001f315") is not kt.KimiLineKind.IDLE_TIP

    def test_tool_call_row_classified(self):
        raw = "\x1b[38;5;253m● \x1b[1m\x1b[38;5;111mRunning a command\x1b[0;2m · $ uname -a"
        assert kt.classify_line(raw) is kt.KimiLineKind.TOOL_CALL

    def test_collapsed_tool_output_is_tool_chrome(self):
        assert kt.classify_line("… (3 more lines, ctrl+o to expand)") is kt.KimiLineKind.TOOL_CHROME

    def test_tool_key_hints_are_tool_chrome(self):
        assert (
            kt.classify_line("  Press Ctrl+B to run in background") is kt.KimiLineKind.TOOL_CHROME
        )
        assert kt.classify_line("Press Esc to interrupt") is kt.KimiLineKind.TOOL_CHROME

    def test_prose_beginning_with_press_is_content(self):
        """The hint pattern is exact, so a real answer is never deleted."""

        assert kt.classify_line("Press Ctrl+C to stop the server.") is kt.KimiLineKind.CONTENT

    def test_bare_box_rule_is_chrome(self):
        assert kt.classify_line("─" * 40) is kt.KimiLineKind.RULE
        assert kt.KimiLineKind.RULE in kt.CHROME_KINDS
        assert kt.KimiLineKind.RULE not in kt.ANSWER_KINDS

    def test_markdown_ascii_rule_is_content(self):
        assert kt.classify_line("---") is kt.KimiLineKind.CONTENT

    def test_trust_dialog_rows_classified(self):
        assert kt.classify_line("Trust this folder?") is kt.KimiLineKind.TRUST_DIALOG
        assert kt.classify_line("❯ Trust this folder") is kt.KimiLineKind.TRUST_DIALOG
        assert kt.classify_line("Don't trust") is kt.KimiLineKind.TRUST_DIALOG
        assert (
            kt.classify_line("↑↓ navigate · Enter select · Esc exit")
            is kt.KimiLineKind.TRUST_DIALOG
        )

    def test_approval_dialog_rows_classified(self):
        assert kt.classify_line("▶ Run this command?") is kt.KimiLineKind.APPROVAL_DIALOG
        assert (
            kt.classify_line("↑/↓ select · 1/2/3/4 choose · ↵ confirm")
            is kt.KimiLineKind.APPROVAL_DIALOG
        )
        assert kt.classify_line("1. Approve") is kt.KimiLineKind.APPROVAL_DIALOG

    def test_boot_chrome_rows_classified(self):
        assert kt.classify_line("Loading configuration…") is kt.KimiLineKind.BOOT_CHROME
        assert kt.classify_line("Restoring conversation…") is kt.KimiLineKind.BOOT_CHROME
        assert kt.classify_line("Send /help for help information") is kt.KimiLineKind.BOOT_CHROME

    def test_welcome_banner_accepts_both_variants(self):
        assert kt.classify_line("Welcome to Kimi Code CLI!") is kt.KimiLineKind.BOOT_CHROME
        assert kt.classify_line("Welcome to Kimi Code!") is kt.KimiLineKind.BOOT_CHROME

    def test_status_footer_rows_classified(self):
        assert kt.classify_line("context: 4.0% (10.4k/262.1k)") is kt.KimiLineKind.STATUS_FOOTER
        assert (
            kt.classify_line("  ctrl-o to hide or reveal tool output")
            is kt.KimiLineKind.STATUS_FOOTER
        )

    def test_composer_rows_are_ready_frame(self):
        assert kt.classify_line("╭──────────────────╮") is kt.KimiLineKind.READY_INPUT_FRAME
        assert kt.classify_line("╰──────────────────╯") is kt.KimiLineKind.READY_INPUT_FRAME
        assert kt.classify_line("│ > ") is kt.KimiLineKind.READY_INPUT_FRAME
        assert kt.classify_line("── input ──") is kt.KimiLineKind.READY_INPUT_FRAME

    def test_markdown_table_row_is_content_not_composer(self):
        """A `│`-leading table row is answer text, not ready chrome.

        Regression guard: a bare "starts with a box glyph" composer test
        classified table rows as READY_INPUT_FRAME, which deleted them from
        the extracted answer.
        """

        assert kt.classify_line("│ a │ b │") is kt.KimiLineKind.CONTENT
        assert kt.classify_line("| a | b |") is kt.KimiLineKind.CONTENT
        assert kt.is_composer_row("│ a │ b │") is False
        assert kt.is_composer_row("│ > ") is True
        assert kt.is_composer_row("│") is True
        assert kt.is_composer_row("│──────│") is True

    def test_composer_row_helper_rejects_plain_text(self):
        assert kt.is_composer_row("just prose") is False
        assert kt.is_composer_row("") is False

    def test_user_input_echo_classified(self):
        raw = "\x1b[1m\x1b[38;5;222m✨ summarize the repo\x1b[0m"
        assert kt.classify_line(raw) is kt.KimiLineKind.USER_INPUT

    def test_blank_line_classified(self):
        assert kt.classify_line("") is kt.KimiLineKind.BLANK
        assert kt.classify_line("   ") is kt.KimiLineKind.BLANK

    def test_plain_prose_is_content(self):
        assert kt.classify_line("Python is a programming language.") is kt.KimiLineKind.CONTENT

    def test_answer_kinds_exclude_execution_plumbing(self):
        assert kt.ANSWER_KINDS == frozenset({kt.KimiLineKind.FINAL_BULLET, kt.KimiLineKind.CONTENT})
        assert kt.KimiLineKind.TOOL_CALL not in kt.ANSWER_KINDS
        assert kt.KimiLineKind.TOOL_CHROME not in kt.ANSWER_KINDS

    def test_chrome_kinds_cover_both_tool_kinds(self):
        assert kt.KimiLineKind.TOOL_CALL in kt.CHROME_KINDS
        assert kt.KimiLineKind.TOOL_CHROME in kt.CHROME_KINDS

    def test_classify_lines_returns_triples_for_every_row(self):
        text = "line one\n\x1b[38;5;253m● \x1b[39manswer\n"
        triples = kt.classify_lines(text)
        assert len(triples) == 3
        for raw, clean, kind in triples:
            assert isinstance(kind, kt.KimiLineKind)
            assert "\x1b" not in clean
        assert triples[1][2] is kt.KimiLineKind.FINAL_BULLET
        assert triples[1][0].startswith("\x1b")  # raw preserved

    def test_strip_sgr_removes_only_colour(self):
        assert kt.strip_sgr("\x1b[38;5;253m● \x1b[39mhi") == "● hi"


class TestKimiTranscriptFixtures:
    """The classifier against the real 0.43.1 captures."""

    def test_fixture_01_banner_is_boot_chrome(self):
        triples = kt.classify_lines(_fixture("kimi_code_0431_01_fresh_startup_idle.txt"))
        assert any(
            k is kt.KimiLineKind.BOOT_CHROME and "Welcome to Kimi Code!" in c for _, c, k in triples
        )

    def test_fixture_01_has_no_live_spinner_and_no_bullet(self):
        triples = kt.classify_lines(_fixture("kimi_code_0431_01_fresh_startup_idle.txt"))
        kinds = {k for _, _, k in triples}
        assert kt.KimiLineKind.LIVE_SPINNER not in kinds
        assert kt.KimiLineKind.FINAL_BULLET not in kinds

    def test_fixture_01_has_composer_and_footer(self):
        triples = kt.classify_lines(_fixture("kimi_code_0431_01_fresh_startup_idle.txt"))
        kinds = [k for _, _, k in triples]
        assert kinds.count(kt.KimiLineKind.READY_INPUT_FRAME) >= 3
        assert kt.KimiLineKind.STATUS_FOOTER in kinds

    def test_fixture_02_braille_row_is_live_spinner(self):
        triples = kt.classify_lines(_fixture("kimi_code_0431_02_processing_turn.txt"))
        assert kt.KimiLineKind.LIVE_SPINNER in {k for _, _, k in triples}

    def test_fixture_02_user_echo_is_user_input(self):
        triples = kt.classify_lines(_fixture("kimi_code_0431_02_processing_turn.txt"))
        assert kt.KimiLineKind.USER_INPUT in {k for _, _, k in triples}

    def test_fixture_03_thinking_and_final_bullets_both_present(self):
        triples = kt.classify_lines(_fixture("kimi_code_0431_03_final_answer.txt"))
        kinds = {k for _, _, k in triples}
        assert kt.KimiLineKind.THINKING_BULLET in kinds
        assert kt.KimiLineKind.FINAL_BULLET in kinds

    def test_fixture_03_status_bar_row_is_not_a_bullet(self):
        triples = kt.classify_lines(_fixture("kimi_code_0431_03_final_answer.txt"))
        for _, clean, kind in triples:
            if "context:" in clean:
                assert kind is kt.KimiLineKind.STATUS_FOOTER

    def test_fixture_05_moon_tip_is_idle_not_spinner(self):
        """A0 D1 — the false PROCESSING signal."""

        triples = kt.classify_lines(_fixture("kimi_code_0431_05_mcp_startup.txt"))
        assert kt.KimiLineKind.IDLE_TIP in {k for _, _, k in triples}
        assert not any(
            k is kt.KimiLineKind.LIVE_SPINNER and kt._MOON_RE.search(c) for _, c, k in triples
        )

    def test_fixture_09_moon_tip_is_idle_not_spinner(self):
        triples = kt.classify_lines(_fixture("kimi_code_0431_09_false_moon_spinner_idle.txt"))
        assert kt.KimiLineKind.IDLE_TIP in {k for _, _, k in triples}
        assert not any(
            k is kt.KimiLineKind.LIVE_SPINNER and kt._MOON_RE.search(c) for _, c, k in triples
        )

    def test_fixture_08_tool_rows_are_not_answer_text(self):
        triples = kt.classify_lines(_fixture("kimi_code_0431_08_command_approval_dialog.txt"))
        assert any(
            k is kt.KimiLineKind.TOOL_CALL and "Running a command" in c for _, c, k in triples
        )
        assert not any(
            k in kt.ANSWER_KINDS and ("Running a command" in c or "more lines" in c)
            for _, c, k in triples
        )

    def test_fixture_08_approval_dialog_detected(self):
        triples = kt.classify_lines(_fixture("kimi_code_0431_08_command_approval_dialog.txt"))
        assert any(k is kt.KimiLineKind.APPROVAL_DIALOG for _, _, k in triples)


# =============================================================================
# A1.7 — workspace trust
# =============================================================================


class TestKimiTrustDialogDetection:
    """`detect_trust_dialog` must be positive-identification only."""

    def test_detected_in_both_fixtures(self):
        for name in (
            "kimi_code_0431_06_workspace_trust_dialog_gated_mcp.txt",
            "kimi_code_0431_07_workspace_trust_dialog_plain.txt",
        ):
            rows = _fixture(name).split("\n")
            dialog = kt.detect_trust_dialog(rows)
            assert dialog is not None, name
            assert dialog.options == [kt.TRUST_OPTION_TRUST, kt.TRUST_OPTION_REJECT], name
            assert dialog.selected_option == kt.TRUST_OPTION_TRUST, name
            assert dialog.workspace, name

    def test_not_detected_in_ordinary_pane(self):
        assert (
            kt.detect_trust_dialog(_fixture("kimi_code_0431_01_fresh_startup_idle.txt").split("\n"))
            is None
        )

    def test_not_detected_when_title_alone(self):
        assert kt.detect_trust_dialog(["Trust this folder?"]) is None

    def test_not_detected_when_hint_alone(self):
        assert kt.detect_trust_dialog(["↑↓ navigate · Enter select · Esc exit"]) is None


class TestKimiFixturePrivacyGuard:
    """Fail-closed privacy guard over the whole ``kimi_code_0431_*`` family.

    Complements the repo-wide personal-email guard
    (``test/test_fixtures_no_personal_pii.py``): these captures are live
    terminal recordings, so every file in the family — including files added
    later — must stay free of real local identity markers: real home paths,
    ``user@host`` prompts, and live Kimi session UUIDs.

    The family's scrub placeholders (``<HOME>``, ``<USER>@<HOST>``,
    ``session_<UUID>``, ``<A0DIR>``, ``<CAOTMP>``) deliberately do not match
    the shapes below, so a re-capture that forgets to scrub fails here.
    """

    # /home/<user> with a real-looking (lowercase identifier) user segment.
    # The ``<HOME>`` placeholder does not match this shape.
    _REAL_HOME_PATH_RE = re.compile(r"(?<![\w<])/home/[a-z0-9][a-z0-9_-]*")

    # user@host prompt shapes. Placeholders (``<USER>@<HOST>``) cannot match:
    # '@' is preceded by '<' or followed by '<'. Well-known non-personal hosts
    # are exempted so git-remote and example banners stay usable.
    _REAL_USER_HOST_RE = re.compile(r"(?<![<\w])[a-z_][a-z0-9_-]{0,31}@[a-z0-9][a-z0-9.-]{0,62}")
    _SAFE_HOSTS = {"github.com", "example.com", "example.org", "example.net", "localhost"}

    # A live Kimi session id (UUID-shaped). The scrub placeholder is the
    # literal ``session_<UUID>`` and does not match a hex shape.
    _LIVE_SESSION_RE = re.compile(
        r"session_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}" r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    )

    def _family_files(self) -> list[Path]:
        return sorted(FIXTURES_DIR.glob("kimi_code_0431_*.txt"))

    def _assert_clean(self, offenders: dict[str, set[str]], what: str) -> None:
        assert not offenders, (
            f"Real {what} found in Kimi Code fixtures — replace with the family's "
            "placeholder style (<HOME>, <USER>@<HOST>, session_<UUID>):\n"
            + "\n".join(f"  {f}: {', '.join(sorted(h))}" for f, h in sorted(offenders.items()))
        )

    def test_guard_covers_the_whole_family(self) -> None:
        """The guard scans every file in the family, present and future."""
        files = self._family_files()
        assert files, "kimi_code_0431_* fixture family is missing"
        assert len(files) >= 12, (
            "kimi_code_0431_* family unexpectedly small — verify no fixture was "
            "dropped from the capture set"
        )

    def test_no_real_home_path_in_fixtures(self) -> None:
        offenders: dict[str, set[str]] = {}
        for path in self._family_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            hits = set(self._REAL_HOME_PATH_RE.findall(text))
            if "/home/time" in text:  # belt-and-suspenders: today's known marker
                hits.add("/home/time")
            if hits:
                offenders[path.name] = hits
        self._assert_clean(offenders, "home paths")

    def test_no_user_at_host_prompt_in_fixtures(self) -> None:
        offenders: dict[str, set[str]] = {}
        for path in self._family_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            hits = {
                m.group(0)
                for m in self._REAL_USER_HOST_RE.finditer(text)
                if m.group(0).rsplit("@", 1)[1] not in self._SAFE_HOSTS
            }
            if "cowboy" in text:  # belt-and-suspenders: today's known hostname
                hits.add("cowboy")
            if hits:
                offenders[path.name] = hits
        self._assert_clean(offenders, "user@host identity markers")

    def test_no_live_session_uuid_in_fixtures(self) -> None:
        offenders: dict[str, set[str]] = {}
        for path in self._family_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            hits = set(self._LIVE_SESSION_RE.findall(text))
            if hits:
                offenders[path.name] = hits
        self._assert_clean(offenders, "session UUIDs")


class TestKimiTrustHandler:
    """`_handle_trust_dialog` end-to-end against the captured dialog."""

    pytestmark = pytest.mark.asyncio

    @pytest.fixture(autouse=True)
    def _opt_in(self, monkeypatch):
        """These tests exercise the *post-opt-in* path.

        A3-5 made answering the dialog conditional on an explicit operator
        opt-in; the gate itself has its own tests
        (:class:`TestKimiTrustOptInGate`). Setting it here keeps every A2
        expectation about identification/navigation intact.
        """

        monkeypatch.setenv(KIMI_TRUST_OPT_IN_ENV, "1")

    @pytest.fixture
    def provider(self):
        return KimiCliProvider("term-trust", "session-1", "window-1")

    async def test_no_dialog_sends_nothing(self, provider):
        with pytest.MonkeyPatch.context() as mp:
            backend = MagicMock()
            mp.setattr("cli_agent_orchestrator.providers.kimi_cli.get_backend", lambda: backend)
            handled = await provider._handle_trust_dialog("just a normal pane\n")
        assert handled is False
        backend.send_special_key.assert_not_called()

    async def test_default_trust_accepted_with_enter(self, provider):
        pane = _fixture("kimi_code_0431_07_workspace_trust_dialog_plain.txt")
        dialog = kt.detect_trust_dialog(pane.split("\n"))
        with pytest.MonkeyPatch.context() as mp:
            backend = MagicMock()
            backend.get_pane_working_directory.return_value = dialog.workspace
            mp.setattr("cli_agent_orchestrator.providers.kimi_cli.get_backend", lambda: backend)
            mp.setattr(
                "cli_agent_orchestrator.services.status_monitor.status_monitor.notify_input_sent",
                lambda *a, **k: None,
            )
            handled = await provider._handle_trust_dialog(pane)

        assert handled is True
        assert provider._trust_handled is True
        backend.send_special_key.assert_called_once()
        assert backend.send_special_key.call_args[0][2] == "Enter"

    async def test_mismatched_workspace_refuses(self, provider):
        pane = _fixture("kimi_code_0431_07_workspace_trust_dialog_plain.txt")
        with pytest.MonkeyPatch.context() as mp:
            backend = MagicMock()
            backend.get_pane_working_directory.return_value = "/somewhere/else"
            mp.setattr("cli_agent_orchestrator.providers.kimi_cli.get_backend", lambda: backend)
            with pytest.raises(Exception, match="other than this terminal"):
                await provider._handle_trust_dialog(pane)
        backend.send_special_key.assert_not_called()
        assert provider._trust_handled is False

    async def test_unverifiable_workspace_refuses(self, provider):
        pane = _fixture("kimi_code_0431_07_workspace_trust_dialog_plain.txt")
        with pytest.MonkeyPatch.context() as mp:
            backend = MagicMock()
            backend.get_pane_working_directory.return_value = None
            mp.setattr("cli_agent_orchestrator.providers.kimi_cli.get_backend", lambda: backend)
            with pytest.raises(Exception, match="could not be verified"):
                await provider._handle_trust_dialog(pane)
        backend.send_special_key.assert_not_called()

    async def test_second_call_is_noop_after_handling(self, provider):
        pane = _fixture("kimi_code_0431_07_workspace_trust_dialog_plain.txt")
        dialog = kt.detect_trust_dialog(pane.split("\n"))
        provider._trust_handled = True
        with pytest.MonkeyPatch.context() as mp:
            backend = MagicMock()
            backend.get_pane_working_directory.return_value = dialog.workspace
            mp.setattr("cli_agent_orchestrator.providers.kimi_cli.get_backend", lambda: backend)
            handled = await provider._handle_trust_dialog(pane)
        assert handled is False
        backend.send_special_key.assert_not_called()


class TestKimiTrustOptInKnob:
    """A3-5 — the opt-in knob's own semantics.

    Fails closed on anything that is not an unambiguous "yes": a typo must not
    silently enable project-MCP trust.
    """

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "Yes", "on", " on "])
    def test_opt_in_values_enable(self, raw):
        assert kimi_trust_opt_in({KIMI_TRUST_OPT_IN_ENV: raw}) is True

    @pytest.mark.parametrize("raw", ["", "0", "false", "no", "off", "yes please", "tru", "2"])
    def test_everything_else_denies(self, raw):
        assert kimi_trust_opt_in({KIMI_TRUST_OPT_IN_ENV: raw}) is False

    def test_unset_denies(self):
        assert kimi_trust_opt_in({}) is False

    def test_opt_in_is_not_exported_to_kimi(self, tmp_path):
        """It is CAO's policy knob; Kimi has no corresponding setting.

        ``_kimi_source_home`` is redirected so building the launch command never
        reads the real ``~/.kimi-code``.
        """

        provider = _code_provider()
        provider._kimi_source_home = tmp_path / "src"
        command = provider._build_kimi_code_command()
        assert KIMI_TRUST_OPT_IN_ENV not in command


class TestKimiTrustOptInGate:
    """A3-5 — CAO must not grant project trust on its own initiative.

    The A3-5 probe against Kimi Code 0.43.1 established that choosing
    *Don't trust* exits the process, so trust is not optional for a working
    terminal — but granting it starts the launched repository's project MCP
    servers and loads its project AGENTS.md. The decision is therefore the
    operator's, and these tests pin the default-deny behaviour.
    """

    pytestmark = pytest.mark.asyncio

    @pytest.fixture
    def provider(self):
        return KimiCliProvider("term-trust-gate", "session-1", "window-1")

    @pytest.fixture
    def dialog_pane(self):
        return _fixture("kimi_code_0431_07_workspace_trust_dialog_plain.txt")

    async def test_dialog_without_opt_in_raises_and_sends_nothing(
        self, provider, dialog_pane, monkeypatch
    ):
        monkeypatch.delenv(KIMI_TRUST_OPT_IN_ENV, raising=False)
        backend = MagicMock()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("cli_agent_orchestrator.providers.kimi_cli.get_backend", lambda: backend)
            with pytest.raises(Exception, match="CAO will not answer it"):
                await provider._handle_trust_dialog(dialog_pane)

        backend.send_special_key.assert_not_called()
        backend.send_keys.assert_not_called()
        assert provider._trust_handled is False

    @pytest.mark.parametrize("raw", ["0", "false", "off", "garbage"])
    async def test_explicit_deny_raises(self, provider, dialog_pane, monkeypatch, raw):
        monkeypatch.setenv(KIMI_TRUST_OPT_IN_ENV, raw)
        backend = MagicMock()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("cli_agent_orchestrator.providers.kimi_cli.get_backend", lambda: backend)
            with pytest.raises(Exception, match="CAO will not answer it"):
                await provider._handle_trust_dialog(dialog_pane)

        backend.send_special_key.assert_not_called()
        assert provider._trust_handled is False

    async def test_error_names_the_folder_and_the_opt_in(self, provider, dialog_pane, monkeypatch):
        """The refusal must be actionable, not just loud."""

        monkeypatch.delenv(KIMI_TRUST_OPT_IN_ENV, raising=False)
        dialog = kt.detect_trust_dialog(dialog_pane.split("\n"))
        backend = MagicMock()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("cli_agent_orchestrator.providers.kimi_cli.get_backend", lambda: backend)
            with pytest.raises(Exception) as excinfo:
                await provider._handle_trust_dialog(dialog_pane)

        message = str(excinfo.value)
        assert dialog.workspace in message
        assert KIMI_TRUST_OPT_IN_ENV in message

    async def test_error_offers_both_pre_trust_and_opt_in(self, provider, dialog_pane, monkeypatch):
        """A4.5 — the guidance must name the narrow option first.

        Before A4 the message told the operator to pre-trust the folder, which
        did not actually work: the runtime home discarded the trust store. Now
        that inheritance is real, the message must say so, and must present the
        per-workspace option as the preferred one and the server-wide override
        as the broader one.
        """

        monkeypatch.delenv(KIMI_TRUST_OPT_IN_ENV, raising=False)
        backend = MagicMock()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("cli_agent_orchestrator.providers.kimi_cli.get_backend", lambda: backend)
            with pytest.raises(Exception) as excinfo:
                await provider._handle_trust_dialog(dialog_pane)

        message = str(excinfo.value)
        lowered = message.lower()
        # Option A — the per-workspace pre-trust, and the claim that it persists.
        assert "option a" in lowered
        assert "preferred" in lowered
        assert "kimi" in lowered and "trust this folder" in lowered
        assert "inherits" in lowered or "inherit" in lowered
        # Option B — the broader server-level override.
        assert "option b" in lowered
        assert "broader" in lowered
        assert KIMI_TRUST_OPT_IN_ENV in message
        # The broad option must not be presented as the recommendation.
        assert "option a (preferred" in lowered.replace("  ", " ")

    async def test_no_dialog_without_opt_in_is_not_an_error(self, provider, monkeypatch):
        """An already-trusted folder must still work with the gate closed."""

        monkeypatch.delenv(KIMI_TRUST_OPT_IN_ENV, raising=False)
        backend = MagicMock()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("cli_agent_orchestrator.providers.kimi_cli.get_backend", lambda: backend)
            handled = await provider._handle_trust_dialog("just a normal pane\n")

        assert handled is False
        backend.send_special_key.assert_not_called()

    async def test_opt_in_still_accepts(self, provider, dialog_pane, monkeypatch):
        """The gate must not break the A2 path once opted in."""

        monkeypatch.setenv(KIMI_TRUST_OPT_IN_ENV, "1")
        dialog = kt.detect_trust_dialog(dialog_pane.split("\n"))
        backend = MagicMock()
        backend.get_pane_working_directory.return_value = dialog.workspace
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("cli_agent_orchestrator.providers.kimi_cli.get_backend", lambda: backend)
            mp.setattr(
                "cli_agent_orchestrator.services.status_monitor.status_monitor.notify_input_sent",
                lambda *a, **k: None,
            )
            handled = await provider._handle_trust_dialog(dialog_pane)

        assert handled is True
        assert provider._trust_handled is True
        assert backend.send_special_key.call_args[0][2] == "Enter"


# =============================================================================
# A1.2 / A1.5 / A1.6 — launch command
# =============================================================================


class TestKimiCodeLaunchCommand:
    """The CODE launch command contract."""

    def test_uses_auto_not_yolo(self, tmp_path):
        provider = _code_provider()
        provider._kimi_source_home = tmp_path / "src"
        command = provider._build_kimi_code_command()
        assert " --auto" in command
        assert "--yolo" not in command

    def test_never_cds(self, tmp_path):
        """A1.2 — the real cwd must be preserved."""

        provider = _code_provider()
        provider._kimi_source_home = tmp_path / "src"
        command = provider._build_kimi_code_command()
        assert "cd " not in command

    def test_exports_runtime_home(self, tmp_path):
        provider = _code_provider()
        provider._kimi_source_home = tmp_path / "src"
        command = provider._build_kimi_code_command()
        assert "KIMI_CODE_HOME=" in command
        assert str(provider._managed_runtime_home()) in command

    def test_exports_terminal_id_for_mcp_children(self, tmp_path):
        provider = _code_provider("term-abc")
        provider._kimi_source_home = tmp_path / "src"
        command = provider._build_kimi_code_command()
        assert "CAO_TERMINAL_ID=term-abc" in command

    def test_exports_term_override(self, tmp_path):
        provider = _code_provider()
        provider._kimi_source_home = tmp_path / "src"
        assert "TERM=xterm-256color" in provider._build_kimi_code_command()

    def test_timeouts_travel_as_env_not_config_mutation(self, tmp_path):
        provider = _code_provider()
        provider._kimi_source_home = tmp_path / "src"
        command = provider._build_kimi_code_command()
        assert f"KIMI_MCP_TOOL_TIMEOUT_MS={KIMI_MCP_TOOL_TIMEOUT_MS}" in command
        assert f"KIMI_MCP_STARTUP_TIMEOUT_MS={KIMI_MCP_STARTUP_TIMEOUT_MS}" in command

    def test_auto_update_suppressed(self, tmp_path):
        provider = _code_provider()
        provider._kimi_source_home = tmp_path / "src"
        command = provider._build_kimi_code_command()
        for key, value in KIMI_NO_AUTO_UPDATE_ENV.items():
            assert f"{key}={value}" in command

    def test_uses_the_probed_absolute_binary(self, tmp_path):
        provider = _code_provider(binary="/opt/kimi/bin/kimi")
        provider._kimi_source_home = tmp_path / "src"
        assert "/opt/kimi/bin/kimi" in provider._build_kimi_code_command()

    def test_requires_a_resolved_binary(self):
        provider = KimiCliProvider("term-x", "s", "w")
        provider._kimi_binary = None
        with pytest.raises(Exception, match="requires a resolved binary"):
            provider._build_kimi_code_command()

    def test_no_profile_means_no_agent_file(self, tmp_path):
        provider = _code_provider()
        provider._kimi_source_home = tmp_path / "src"
        assert "--agent-file" not in provider._build_kimi_code_command()

    def test_model_override_appended(self, tmp_path):
        provider = _code_provider()
        provider._kimi_source_home = tmp_path / "src"
        provider._model = "kimi-k2.6"
        command = provider._build_kimi_code_command()
        assert "--model" in command and "kimi-k2.6" in command

    def test_builds_runtime_home_on_disk(self, tmp_path):
        source = tmp_path / "src"
        source.mkdir()
        (source / "config.toml").write_text("model = 'x'\n")
        provider = _code_provider()
        provider._kimi_source_home = source
        provider._build_kimi_code_command()
        assert (provider._managed_runtime_home() / "config.toml").is_file()

    def test_cleanup_removes_runtime_home(self, tmp_path):
        source = tmp_path / "src"
        source.mkdir()
        provider = _code_provider()
        provider._kimi_source_home = source
        provider._build_kimi_code_command()
        home = provider._managed_runtime_home()
        assert home.is_dir()
        assert provider.cleanup() is True
        assert not home.exists()


class TestKimiCodeMarkdownAgent:
    """A1.6 — the Markdown agent contract."""

    def _provider_with_prompt(self, prompt, **kwargs):
        provider = KimiCliProvider("term-md", "s", "w", agent_profile="developer", **kwargs)
        profile = MagicMock()
        profile.system_prompt = prompt
        profile.model = None
        profile.mcpServers = None
        return provider, profile

    def test_base_prompt_interpolated_before_cao_text(self):
        provider, profile = self._provider_with_prompt("CAO INSTRUCTIONS")
        rendered = provider._render_markdown_agent(profile)
        assert "${base_prompt}" in rendered
        assert rendered.index("${base_prompt}") < rendered.index("CAO INSTRUCTIONS")

    def test_frontmatter_has_kebab_name(self):
        provider, profile = self._provider_with_prompt("hello")
        rendered = provider._render_markdown_agent(profile)
        assert rendered.startswith("---\n")
        assert f"name: {kimi_agent_name('term-md')}" in rendered
        assert "cao-kimi-term-md" in rendered

    def test_empty_prompt_returns_none(self):
        provider, profile = self._provider_with_prompt("   ")
        assert provider._render_markdown_agent(profile) is None

    def test_no_profile_returns_none(self):
        provider = KimiCliProvider("term-md", "s", "w")
        assert provider._render_markdown_agent(None) is None

    def test_skill_prompt_applied(self):
        provider, profile = self._provider_with_prompt("base")
        provider._skill_prompt = "SKILL BLOCK"
        rendered = provider._render_markdown_agent(profile)
        assert "SKILL BLOCK" in rendered

    def test_agent_name_slugifies_and_defaults(self):
        assert kimi_agent_name("Term 1/Abc") == "cao-kimi-term-1-abc"
        assert kimi_agent_name("") == "cao-kimi-terminal"
        assert len(kimi_agent_name("x" * 200)) <= len("cao-kimi-") + 48


# =============================================================================
# A1.3 / A1.4 — runtime home builder
# =============================================================================


def _make_source_home(root: Path) -> Path:
    """A source home exercising every disposition class."""

    root.mkdir(parents=True, exist_ok=True)
    for name in PRESERVE_FILES:
        (root / name).write_text(f"# {name}\n")
    for name in PRESERVE_DIRS:
        (root / name).mkdir()
        (root / name / "entry.txt").write_text("x")
    (root / "bin").mkdir()
    (root / "bin" / "kimi").write_text("#!/bin/sh\n")
    # A4: workspace-trust is a snapshot-copied security input, no longer part of
    # NEVER_COPY, so it is created explicitly here to keep the promise that this
    # home exercises every disposition class.
    (root / TRUST_DIR_NAME).mkdir()
    (root / TRUST_DIR_NAME / "wd_project_example").write_text('{"root": "/tmp/project"}')
    for name in NEVER_COPY:
        target = root / name
        if target.suffix:
            target.write_text("should not be copied")
        else:
            target.mkdir(exist_ok=True)
            (target / "x").write_text("should not be copied")
    return root


class TestKimiRuntimeHomeBuilder:
    """A1.3 — per-worker home materialisation."""

    def test_home_lives_under_provider_temp(self, tmp_path):
        source = _make_source_home(tmp_path / "src")
        builder = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp")
        result = builder.build()
        assert result.home == tmp_path / "temp" / "kimi-home"

    def test_preserve_files_copied(self, tmp_path):
        source = _make_source_home(tmp_path / "src")
        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()
        for name in PRESERVE_FILES:
            assert (result.home / name).is_file(), name
        assert set(result.copied_files) == set(PRESERVE_FILES)

    def test_preserve_dirs_copied(self, tmp_path):
        source = _make_source_home(tmp_path / "src")
        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()
        for name in PRESERVE_DIRS:
            assert (result.home / name / "entry.txt").is_file(), name
        assert set(result.copied_dirs) == set(PRESERVE_DIRS)

    def test_never_copy_entries_absent(self, tmp_path):
        source = _make_source_home(tmp_path / "src")
        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()
        for name in NEVER_COPY:
            assert not (result.home / name).exists(), name

    def test_bin_is_linked_not_copied(self, tmp_path):
        source = _make_source_home(tmp_path / "src")
        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()
        linked = result.home / "bin"
        assert linked.is_symlink()
        assert result.linked_dirs == ["bin"]

    def test_home_dir_is_private(self, tmp_path):
        source = _make_source_home(tmp_path / "src")
        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()
        assert stat.S_IMODE(os.stat(result.home).st_mode) == 0o700

    def test_secret_files_are_0600(self, tmp_path):
        source = _make_source_home(tmp_path / "src")
        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()
        for name in ("config.toml", "mcp.json"):
            assert stat.S_IMODE(os.stat(result.home / name).st_mode) == 0o600, name

    def test_source_home_is_not_mutated(self, tmp_path):
        """A1.3 — the real Kimi state must stay untouched."""

        source = _make_source_home(tmp_path / "src")
        before = {p.name: p.stat().st_mtime_ns for p in source.iterdir()}
        KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()
        after = {p.name: p.stat().st_mtime_ns for p in source.iterdir()}
        assert before == after
        assert not (source / "kimi-home").exists()

    def test_missing_source_home_still_builds(self, tmp_path):
        """A first-run install has no source home; that must not be fatal."""

        result = KimiCodeRuntimeHomeBuilder(tmp_path / "absent", tmp_path / "temp").build()
        assert result.home.is_dir()
        assert result.copied_files == []

    def test_build_is_idempotent(self, tmp_path):
        source = _make_source_home(tmp_path / "src")
        builder = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp")
        first = builder.build()
        second = builder.build()
        assert first is second
        assert builder.built is True

    def test_cleanup_removes_home(self, tmp_path):
        source = _make_source_home(tmp_path / "src")
        builder = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp")
        builder.build()
        assert builder.cleanup() is True
        assert not builder.home.exists()
        assert builder.built is False

    def test_cleanup_refuses_foreign_path(self, tmp_path):
        """The recursive delete is scoped to this builder's own temp dir."""

        builder = KimiCodeRuntimeHomeBuilder(tmp_path / "src", tmp_path / "temp")
        builder._home = tmp_path / "elsewhere" / "kimi-home"
        assert builder.cleanup() is False

    def test_two_workers_get_isolated_homes(self, tmp_path):
        """The A0.9 property: per-worker homes cannot cross-talk."""

        source = _make_source_home(tmp_path / "src")
        (source / "mcp.json").write_text(
            json.dumps({"mcpServers": {"user-only": {"command": "u"}}})
        )

        a = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp-a")
        b = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp-b")
        ra = a.build({"cao-mcp-server": {"command": "a-server"}})
        rb = b.build({"other-server": {"command": "b-server"}})

        assert ra.home != rb.home
        assert set(ra.mcp_server_names) == {"user-only", "cao-mcp-server"}
        assert set(rb.mcp_server_names) == {"user-only", "other-server"}
        assert "b-server" not in ra.mcp_path.read_text()
        assert "a-server" not in rb.mcp_path.read_text()

    def test_built_home_has_no_forbidden_state(self, tmp_path):
        """The runtime home must never carry session/log/trust state."""

        source = _make_source_home(tmp_path / "src")
        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()
        assert list(iter_forbidden_runtime_state(result.home)) == []

    def test_forbidden_state_detector_actually_detects(self, tmp_path):
        """Guard the guard: a leaked `sessions/` must be reported."""

        home = tmp_path / "leaky"
        (home / "sessions").mkdir(parents=True)
        assert list(iter_forbidden_runtime_state(home)) == ["sessions"]


class TestKimiRuntimeHomeSymlinks:
    """A3-7 — a preserved entry that is itself a symlink.

    ``Path.is_dir()`` follows symlinks, so before A3-7 a top-level link was
    traversed incidentally: whatever it pointed at got copied, with no bound.
    The intended behaviour is now explicit — materialise a legitimate link,
    refuse a pathological one — and these tests pin it.
    """

    @staticmethod
    def _builder_with_link(tmp_path, name, target, *, target_is_directory=True):
        source = tmp_path / "src"
        source.mkdir(parents=True, exist_ok=True)
        (source / name).symlink_to(target, target_is_directory=target_is_directory)
        return source, KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp")

    def test_symlinked_preserve_dir_is_materialised(self, tmp_path):
        """A link to a real directory elsewhere is copied by content."""

        external = tmp_path / "external-skills"
        external.mkdir()
        (external / "deep").mkdir()
        (external / "deep" / "skill.md").write_text("s")

        source, builder = self._builder_with_link(tmp_path, "skills", external)
        result = builder.build()

        assert (result.home / "skills" / "deep" / "skill.md").is_file()
        assert "skills" in result.copied_dirs
        assert result.symlinked_dirs == {"skills": str(external)}
        assert result.skipped_dirs == []

    def test_materialised_dir_is_a_real_directory_not_a_link(self, tmp_path):
        """The link is never reproduced, so nothing can write back through it."""

        external = tmp_path / "external-skills"
        external.mkdir()
        (external / "skill.md").write_text("s")

        source, builder = self._builder_with_link(tmp_path, "skills", external)
        result = builder.build()

        assert not (result.home / "skills").is_symlink()
        assert (result.home / "skills").is_dir()

    def test_materialising_does_not_mutate_the_link_or_its_target(self, tmp_path):
        """A3-7 — do not mutate source state."""

        external = tmp_path / "external-skills"
        external.mkdir()
        (external / "skill.md").write_text("s")
        source, builder = self._builder_with_link(tmp_path, "skills", external)

        link = source / "skills"
        assert link.is_symlink()
        before_link = os.readlink(link)
        before_target = sorted(p.name for p in external.iterdir())
        before_mtime = external.stat().st_mtime_ns

        builder.build()

        assert os.readlink(link) == before_link
        assert sorted(p.name for p in external.iterdir()) == before_target
        assert external.stat().st_mtime_ns == before_mtime

    def test_symlinked_credentials_are_materialised_and_private(self, tmp_path):
        """A credential-bearing link gets explicit treatment, not traversal."""

        external = tmp_path / "external-creds"
        external.mkdir()
        secret = external / "token.json"
        secret.write_text("{}")
        os.chmod(secret, 0o644)

        source, builder = self._builder_with_link(tmp_path, "credentials", external)
        result = builder.build()

        copied = result.home / "credentials" / "token.json"
        assert copied.is_file()
        assert not (result.home / "credentials").is_symlink()
        # Never widen: a 0644 source becomes 0600 inside the runtime home.
        assert stat.S_IMODE(os.stat(copied).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(result.home / "credentials").st_mode) == 0o700
        assert result.symlinked_dirs == {"credentials": str(external)}

    def test_symlink_to_non_directory_is_skipped(self, tmp_path):
        """A link to a file is not a directory to preserve."""

        plain = tmp_path / "not-a-dir.txt"
        plain.write_text("x")

        source, builder = self._builder_with_link(
            tmp_path, "plugins", plain, target_is_directory=False
        )
        result = builder.build()

        assert not (result.home / "plugins").exists()
        assert result.skipped_dirs == ["plugins"]
        assert "plugins" not in result.copied_dirs
        assert result.home.is_dir()  # the home still builds

    def test_dangling_symlink_is_skipped(self, tmp_path):
        source, builder = self._builder_with_link(tmp_path, "skills", tmp_path / "gone")
        result = builder.build()

        assert not (result.home / "skills").exists()
        assert result.skipped_dirs == ["skills"]

    def test_symlink_to_an_ancestor_of_the_source_home_is_skipped(self, tmp_path):
        """Copying it would pull the source home into itself."""

        source = tmp_path / "src"
        source.mkdir(parents=True)
        (source / "skills").symlink_to(tmp_path, target_is_directory=True)
        builder = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp")

        result = builder.build()

        assert not (result.home / "skills").exists()
        assert result.skipped_dirs == ["skills"]

    def test_symlink_to_the_source_home_itself_is_skipped(self, tmp_path):
        source = tmp_path / "src"
        source.mkdir(parents=True)
        (source / "skills").symlink_to(source, target_is_directory=True)
        builder = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp")

        result = builder.build()

        assert not (result.home / "skills").exists()
        assert result.skipped_dirs == ["skills"]

    def test_symlink_to_the_filesystem_root_is_skipped(self, tmp_path):
        """The pathological case: an unbounded walk of the whole filesystem."""

        source = tmp_path / "src"
        source.mkdir(parents=True)
        (source / "skills").symlink_to(Path("/"), target_is_directory=True)
        builder = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp")

        result = builder.build()

        assert not (result.home / "skills").exists()
        assert result.skipped_dirs == ["skills"]

    def test_symlink_inside_a_preserved_tree_is_copied_as_a_link(self, tmp_path):
        """Inner links keep user semantics; they are never walked through."""

        source = tmp_path / "src"
        (source / "skills").mkdir(parents=True)
        (source / "skills" / "real.md").write_text("r")
        (source / "skills" / "alias.md").symlink_to(tmp_path / "src" / "skills" / "real.md")

        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        assert (result.home / "skills" / "real.md").is_file()
        assert (result.home / "skills" / "alias.md").is_symlink()
        assert result.symlinked_dirs == {}

    def test_cleanup_never_follows_a_symlinked_home_into_the_real_home(self, tmp_path):
        """A3-7 — cleanup unlinks the link; the real home must survive."""

        real_home = tmp_path / "real-home"
        (real_home / "skills").mkdir(parents=True)
        (real_home / "skills" / "keep.md").write_text("keep")

        temp = tmp_path / "temp"
        temp.mkdir()
        home = temp / "kimi-home"
        home.symlink_to(real_home, target_is_directory=True)

        builder = KimiCodeRuntimeHomeBuilder(tmp_path / "src", temp)
        assert builder.cleanup() is True

        assert not home.exists()
        assert not home.is_symlink()
        assert (real_home / "skills" / "keep.md").read_text() == "keep"

    def test_skipped_link_does_not_stop_other_entries(self, tmp_path):
        """One pathological link degrades that entry only."""

        source = tmp_path / "src"
        source.mkdir(parents=True)
        (source / "skills").symlink_to(Path("/"), target_is_directory=True)
        (source / "plugins").mkdir()
        (source / "plugins" / "p.txt").write_text("p")

        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        assert result.skipped_dirs == ["skills"]
        assert "plugins" in result.copied_dirs
        assert (result.home / "plugins" / "p.txt").is_file()

    def test_bin_link_risk_is_recorded(self, tmp_path):
        """`bin/` stays a reference — documented risk, pinned by a test."""

        source = tmp_path / "src"
        (source / "bin").mkdir(parents=True)
        (source / "bin" / "kimi").write_text("#!/bin/sh\n")

        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        assert (result.home / "bin").is_symlink()
        assert result.linked_dirs == ["bin"]
        assert "bin" not in result.copied_dirs


class TestA4WorkspaceTrustSnapshot:
    """A4 — preserve the operator's existing workspace-trust decisions.

    Kimi raises its trust dialog for any cwd with no record in the home it runs
    against. Because every CAO worker gets a fresh, disposable
    ``KIMI_CODE_HOME``, discarding the source trust store re-asked a question the
    operator had already answered — for every terminal — which left the
    server-wide ``CAO_KIMI_CODE_TRUST_WORKSPACE`` override as the only unattended
    path. A4 snapshots the store instead.

    The security properties these tests pin are the point of the change, not an
    afterthought: the copy is a real directory of real files (never a link, so
    Kimi's writes cannot reach the source), a symlinked store is refused rather
    than followed, and an internal symlink is never retained.
    """

    @staticmethod
    def _snapshot(root: Path) -> Dict[str, Any]:
        """Content + mode fingerprint of a tree, for before/after comparison."""

        out: Dict[str, Any] = {}
        for path in sorted(root.rglob("*")):
            rel = str(path.relative_to(root))
            if path.is_symlink():
                out[rel] = ("link", os.readlink(path))
            elif path.is_dir():
                out[rel] = ("dir", stat.S_IMODE(os.stat(path).st_mode))
            else:
                out[rel] = (
                    "file",
                    stat.S_IMODE(os.stat(path).st_mode),
                    path.read_bytes(),
                )
        return out

    @staticmethod
    def _source_with_trust(root: Path, *, record: str = "wd_project_example") -> Path:
        root.mkdir(parents=True, exist_ok=True)
        (root / "config.toml").write_text('theme = "dark"\n')
        trust = root / TRUST_DIR_NAME
        trust.mkdir(mode=0o700)
        os.chmod(trust, 0o700)
        rec = trust / record
        rec.write_text('{"root": "/tmp/project", "trustedAt": 123}')
        os.chmod(rec, 0o600)
        return root

    # -- existing trust is inherited ---------------------------------------

    def test_existing_trust_is_snapshotted(self, tmp_path):
        source = self._source_with_trust(tmp_path / "src")
        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        runtime_trust = result.home / TRUST_DIR_NAME
        runtime_rec = runtime_trust / "wd_project_example"
        source_rec = source / TRUST_DIR_NAME / "wd_project_example"

        assert runtime_trust.is_dir()
        assert runtime_rec.is_file()
        assert runtime_rec.read_bytes() == source_rec.read_bytes()
        assert result.trust_source_state == "copied"
        assert result.trust_records == ["wd_project_example"]
        assert result.trust_skipped == []

    def test_snapshot_is_a_real_dir_of_real_files(self, tmp_path):
        """A link would be a write-through back into the real home."""

        source = self._source_with_trust(tmp_path / "src")
        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        assert not (result.home / TRUST_DIR_NAME).is_symlink()
        assert not (result.home / TRUST_DIR_NAME / "wd_project_example").is_symlink()

    def test_snapshot_permissions_are_tight(self, tmp_path):
        source = self._source_with_trust(tmp_path / "src")
        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        runtime_trust = result.home / TRUST_DIR_NAME
        assert stat.S_IMODE(os.stat(runtime_trust).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(runtime_trust / "wd_project_example").st_mode) == 0o600

    def test_source_trust_is_not_mutated_by_the_build(self, tmp_path):
        source = self._source_with_trust(tmp_path / "src")
        before = self._snapshot(source)
        rec = source / TRUST_DIR_NAME / "wd_project_example"
        mtime_before = rec.stat().st_mtime_ns

        KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        assert self._snapshot(source) == before
        assert rec.stat().st_mtime_ns == mtime_before

    def test_runtime_writes_do_not_flow_back_to_the_source(self, tmp_path):
        """The runtime copy is the worker's; the source stays the operator's."""

        source = self._source_with_trust(tmp_path / "src")
        before = self._snapshot(source)
        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()
        runtime_trust = result.home / TRUST_DIR_NAME

        # Kimi writes here during a real run.
        (runtime_trust / "wd_new_repo_abc123").write_text('{"root": "/tmp/new"}')
        (runtime_trust / "wd_project_example").write_text('{"root": "/tmp/tampered"}')

        assert self._snapshot(source) == before
        assert not (source / TRUST_DIR_NAME / "wd_new_repo_abc123").exists()
        assert (
            source / TRUST_DIR_NAME / "wd_project_example"
        ).read_text() == '{"root": "/tmp/project", "trustedAt": 123}'

    def test_nested_records_are_copied(self, tmp_path):
        source = self._source_with_trust(tmp_path / "src")
        nested = source / TRUST_DIR_NAME / "nested"
        nested.mkdir()
        (nested / "wd_deep").write_text('{"root": "/tmp/deep"}')

        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        assert (result.home / TRUST_DIR_NAME / "nested" / "wd_deep").is_file()
        assert sorted(result.trust_records) == ["nested/wd_deep", "wd_project_example"]

    # -- no source trust ---------------------------------------------------

    def test_absent_source_trust_builds_normally(self, tmp_path):
        source = tmp_path / "src"
        source.mkdir()
        (source / "config.toml").write_text("x")

        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        assert result.home.is_dir()
        assert not (result.home / TRUST_DIR_NAME).exists()
        assert result.trust_source_state == "absent"
        assert result.trust_records == []

    def test_absent_source_trust_is_not_synthesised(self, tmp_path):
        """No store must not become an empty store: Kimi treats them alike."""

        source = tmp_path / "src"
        source.mkdir()
        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        assert not (result.home / TRUST_DIR_NAME).exists()
        assert TRUST_DIR_NAME not in result.copied_dirs

    # -- top-level symlink: fail closed ------------------------------------

    def test_top_level_symlink_is_refused(self, tmp_path, caplog):
        source = tmp_path / "src"
        source.mkdir()
        external = tmp_path / "external-store"
        external.mkdir()
        (external / "wd_external").write_text('{"root": "/tmp/external"}')
        os.symlink(external, source / TRUST_DIR_NAME, target_is_directory=True)

        external_before = self._snapshot(external)
        source_before = self._snapshot(source)

        with caplog.at_level(logging.WARNING):
            result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        assert not (result.home / TRUST_DIR_NAME).exists()
        assert result.trust_source_state == "skipped-symlink"
        assert result.trust_records == []
        assert self._snapshot(external) == external_before
        assert self._snapshot(source) == source_before
        assert any(
            "kimi_runtime_home_trust_skip" in r.message and "top-level-symlink" in r.message
            for r in caplog.records
        )

    def test_top_level_symlink_does_not_block_the_build(self, tmp_path):
        source = tmp_path / "src"
        source.mkdir()
        (source / "config.toml").write_text("x")
        os.symlink(tmp_path / "nowhere", source / TRUST_DIR_NAME, target_is_directory=True)

        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        assert result.home.is_dir()
        assert (result.home / "config.toml").is_file()

    def test_top_level_symlink_leaves_the_worker_untrusted(self, tmp_path):
        """Refusing inheritance must fall through to the A3 policy, not grant."""

        source = tmp_path / "src"
        source.mkdir()
        external = tmp_path / "external-store"
        external.mkdir()
        (external / "wd_external").write_text('{"root": "/tmp/external"}')
        os.symlink(external, source / TRUST_DIR_NAME, target_is_directory=True)

        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        assert not (result.home / TRUST_DIR_NAME).exists()

    # -- internal symlinks: never retained ---------------------------------

    def test_internal_symlink_entries_are_skipped(self, tmp_path, caplog):
        source = self._source_with_trust(tmp_path / "src")
        external = tmp_path / "external-target"
        external.mkdir()
        (external / "wd_should_not_appear").write_text('{"root": "/tmp/x"}')

        trust = source / TRUST_DIR_NAME
        os.symlink(external / "wd_should_not_appear", trust / "wd_linked_record")
        (trust / "nested").mkdir()
        os.symlink(external, trust / "nested" / "linked_dir")

        external_before = self._snapshot(external)

        with caplog.at_level(logging.WARNING):
            result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        runtime_trust = result.home / TRUST_DIR_NAME
        assert (runtime_trust / "wd_project_example").is_file()
        assert not (runtime_trust / "wd_linked_record").exists()
        assert not (runtime_trust / "nested" / "linked_dir").exists()
        assert sorted(result.trust_skipped) == ["nested/linked_dir", "wd_linked_record"]
        assert not any(p.is_symlink() for p in runtime_trust.rglob("*"))
        assert self._snapshot(external) == external_before
        assert any("reason=symlink" in r.message for r in caplog.records)

    def test_internal_symlink_does_not_leak_external_records(self, tmp_path):
        source = self._source_with_trust(tmp_path / "src")
        external = tmp_path / "external-target"
        external.mkdir()
        (external / "wd_secret").write_text('{"root": "/tmp/secret"}')
        os.symlink(external, source / TRUST_DIR_NAME / "linked_dir")

        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        leaked = list((result.home / TRUST_DIR_NAME).rglob("wd_secret"))
        assert leaked == []

    # -- not a directory ---------------------------------------------------

    def test_trust_name_that_is_not_a_directory_is_refused(self, tmp_path, caplog):
        source = tmp_path / "src"
        source.mkdir()
        (source / TRUST_DIR_NAME).write_text("not a directory")

        with caplog.at_level(logging.WARNING):
            result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        assert not (result.home / TRUST_DIR_NAME).exists()
        assert result.trust_source_state == "skipped-not-a-directory"
        assert any("reason=not-a-directory" in r.message for r in caplog.records)

    # -- entry budget ------------------------------------------------------

    def test_entry_budget_fails_closed(self, tmp_path):
        """A pathological store must be truncated, not walked without bound."""

        source = self._source_with_trust(tmp_path / "src")
        trust = source / TRUST_DIR_NAME
        for i in range(MAX_TRUST_ENTRIES + 5):
            (trust / f"wd_bulk_{i:05d}").write_text("{}")

        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        assert len(result.trust_records) == MAX_TRUST_ENTRIES
        # A4.1: truncation is reported as a flag, not by enumerating the
        # remainder — enumerating it would itself be unbounded.
        assert result.trust_truncated is True
        assert result.trust_skipped == []

    # -- interaction with the existing exclusions --------------------------

    def test_trust_is_no_longer_forbidden_state(self):
        assert TRUST_DIR_NAME not in NEVER_COPY

    def test_remaining_runtime_state_is_still_excluded(self, tmp_path):
        source = self._source_with_trust(tmp_path / "src")
        for name in NEVER_COPY:
            target = source / name
            if target.suffix:
                target.write_text("runtime state")
            else:
                target.mkdir()
                (target / "x").write_text("runtime state")

        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        for name in NEVER_COPY:
            assert not (result.home / name).exists(), name
        assert list(iter_forbidden_runtime_state(result.home)) == []

    def test_forbidden_state_detector_ignores_trust(self, tmp_path):
        """A built home legitimately contains trust; the detector must not flag it."""

        source = self._source_with_trust(tmp_path / "src")
        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        assert (result.home / TRUST_DIR_NAME).is_dir()
        assert list(iter_forbidden_runtime_state(result.home)) == []


class TestA41TrustBound:
    """A4.1 — the snapshot bound must count traversal, and reject non-regulars.

    The A4 bound counted only the regular files it had copied, so a tree of
    empty directories was walked and materialised without limit: 4160 empty
    directories produced 4160 runtime directories with zero records and zero
    skips, and the "cannot be unbounded" claim was false. Separately, a FIFO in
    the store reached ``shutil.copyfile`` and raised ``SpecialFileError``,
    aborting the launch.

    These tests pin the corrected behaviour: every traversal entry consumes
    budget, and only ordinary regular files are ever copied.
    """

    @staticmethod
    def _source(root: Path) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        (root / "config.toml").write_text("x")
        (root / TRUST_DIR_NAME).mkdir(mode=0o700)
        return root

    @staticmethod
    def _count_dirs(path: Path) -> int:
        if not path.is_dir():
            return 0
        return sum(1 for p in path.rglob("*") if p.is_dir())

    # -- directory bypass --------------------------------------------------

    def test_many_empty_directories_cannot_bypass_the_bound(self, tmp_path, caplog):
        """The exact reproduction: MAX + N empty directories must be bounded."""

        source = self._source(tmp_path / "src")
        trust = source / TRUST_DIR_NAME
        for i in range(MAX_TRUST_ENTRIES + 64):
            (trust / f"d{i:05d}").mkdir()

        with caplog.at_level(logging.WARNING):
            result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        runtime_trust = result.home / TRUST_DIR_NAME
        assert self._count_dirs(runtime_trust) <= MAX_TRUST_ENTRIES
        assert result.trust_truncated is True
        assert result.trust_source_state == "copied"
        assert any("kimi_runtime_home_trust_truncated" in r.message for r in caplog.records)

    def test_directory_bypass_build_still_succeeds(self, tmp_path):
        """Truncation must degrade the worker, never abort the launch."""

        source = self._source(tmp_path / "src")
        trust = source / TRUST_DIR_NAME
        for i in range(MAX_TRUST_ENTRIES + 64):
            (trust / f"d{i:05d}").mkdir()

        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        assert result.home.is_dir()
        assert (result.home / "config.toml").is_file()

    def test_mixed_files_and_directories_share_one_budget(self, tmp_path):
        """Directories and files must not have separate allowances."""

        source = self._source(tmp_path / "src")
        trust = source / TRUST_DIR_NAME
        half = MAX_TRUST_ENTRIES // 2
        for i in range(half):
            (trust / f"d{i:05d}").mkdir()
        for i in range(half + 64):
            (trust / f"f{i:05d}").write_text("{}")

        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        runtime_trust = result.home / TRUST_DIR_NAME
        copied_dirs = self._count_dirs(runtime_trust)
        copied_files = len(result.trust_records)
        assert copied_dirs + copied_files <= MAX_TRUST_ENTRIES
        assert result.trust_truncated is True

    def test_budget_counts_symlink_entries(self, tmp_path):
        """Skipped entries still consume budget, so they cannot be free."""

        source = self._source(tmp_path / "src")
        trust = source / TRUST_DIR_NAME
        external = tmp_path / "external"
        external.mkdir()
        (external / "target").write_text("x")
        for i in range(MAX_TRUST_ENTRIES + 32):
            os.symlink(external / "target", trust / f"l{i:05d}")

        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        assert result.trust_records == []
        assert len(result.trust_skipped) <= MAX_TRUST_ENTRIES
        assert result.trust_truncated is True
        assert not (result.home / TRUST_DIR_NAME / "l00000").exists()

    def test_under_budget_is_not_truncated(self, tmp_path):
        """A normal store must not be reported as truncated."""

        source = self._source(tmp_path / "src")
        trust = source / TRUST_DIR_NAME
        for i in range(8):
            (trust / f"d{i:03d}").mkdir()
            (trust / f"d{i:03d}" / "wd_record").write_text("{}")

        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        assert result.trust_truncated is False
        assert len(result.trust_records) == 8
        assert result.trust_skipped == []

    def test_budget_boundary_is_exact(self, tmp_path):
        """Exactly MAX entries must be copied whole, with no truncation."""

        source = self._source(tmp_path / "src")
        trust = source / TRUST_DIR_NAME
        for i in range(MAX_TRUST_ENTRIES):
            (trust / f"f{i:05d}").write_text("{}")

        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        assert len(result.trust_records) == MAX_TRUST_ENTRIES
        assert result.trust_truncated is False

    # -- non-regular entries -----------------------------------------------

    def test_fifo_is_skipped_and_build_continues(self, tmp_path, caplog):
        """The exact reproduction: a FIFO must not abort the launch."""

        source = self._source(tmp_path / "src")
        trust = source / TRUST_DIR_NAME
        (trust / "wd_real_record").write_text('{"root": "/tmp/real"}')
        os.mkfifo(trust / "wd_fifo")

        with caplog.at_level(logging.WARNING):
            result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        assert result.home.is_dir()
        assert result.trust_source_state == "copied"

    def test_fifo_is_not_materialised_and_is_recorded(self, tmp_path, caplog):
        source = self._source(tmp_path / "src")
        trust = source / TRUST_DIR_NAME
        (trust / "wd_real_record").write_text('{"root": "/tmp/real"}')
        os.mkfifo(trust / "wd_fifo")

        with caplog.at_level(logging.WARNING):
            result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        runtime_trust = result.home / TRUST_DIR_NAME
        assert not (runtime_trust / "wd_fifo").exists()
        assert "wd_fifo" in result.trust_skipped
        assert any("reason=fifo" in r.message for r in caplog.records)

    def test_regular_record_beside_a_fifo_is_still_copied(self, tmp_path):
        source = self._source(tmp_path / "src")
        trust = source / TRUST_DIR_NAME
        (trust / "wd_real_record").write_text('{"root": "/tmp/real"}')
        os.mkfifo(trust / "wd_fifo")

        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        copied = result.home / TRUST_DIR_NAME / "wd_real_record"
        assert copied.is_file()
        assert not copied.is_symlink()
        assert copied.read_bytes() == b'{"root": "/tmp/real"}'
        assert result.trust_records == ["wd_real_record"]

    def test_socket_is_skipped(self, tmp_path, caplog):
        """A unix socket must be skipped, not copied."""

        source = self._source(tmp_path / "src")
        trust = source / TRUST_DIR_NAME
        (trust / "wd_real_record").write_text("{}")
        sock_path = trust / "wd_socket"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(sock_path))
            with caplog.at_level(logging.WARNING):
                result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()
        finally:
            sock.close()

        runtime_trust = result.home / TRUST_DIR_NAME
        assert not (runtime_trust / "wd_socket").exists()
        assert "wd_socket" in result.trust_skipped
        assert "wd_real_record" in result.trust_records

    def test_no_special_file_appears_in_the_runtime_home(self, tmp_path):
        """Nothing in the snapshot may be non-regular or a link."""

        source = self._source(tmp_path / "src")
        trust = source / TRUST_DIR_NAME
        (trust / "wd_real_record").write_text("{}")
        os.mkfifo(trust / "wd_fifo")
        external = tmp_path / "external"
        external.mkdir()
        (external / "t").write_text("x")
        os.symlink(external / "t", trust / "wd_link")
        (trust / "nested").mkdir()
        os.mkfifo(trust / "nested" / "wd_nested_fifo")

        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        runtime_trust = result.home / TRUST_DIR_NAME
        for path in runtime_trust.rglob("*"):
            assert not path.is_symlink(), path
            mode = os.lstat(path).st_mode
            assert not stat.S_ISFIFO(mode), path
            assert not stat.S_ISSOCK(mode), path
            assert not stat.S_ISBLK(mode), path
            assert not stat.S_ISCHR(mode), path

    def test_special_entries_are_skipped_not_enumerated_as_records(self, tmp_path):
        source = self._source(tmp_path / "src")
        trust = source / TRUST_DIR_NAME
        (trust / "wd_real_record").write_text("{}")
        os.mkfifo(trust / "wd_fifo")

        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        assert result.trust_records == ["wd_real_record"]
        assert result.trust_skipped == ["wd_fifo"]
        assert result.trust_truncated is False

    # -- source integrity and unchanged policy -----------------------------

    def test_source_is_untouched_by_the_bounded_walk(self, tmp_path):
        source = self._source(tmp_path / "src")
        trust = source / TRUST_DIR_NAME
        (trust / "wd_real_record").write_text('{"root": "/tmp/real"}')
        os.mkfifo(trust / "wd_fifo")
        for i in range(MAX_TRUST_ENTRIES + 16):
            (trust / f"d{i:05d}").mkdir()

        before = {
            str(p.relative_to(source)): (
                os.lstat(p).st_mode,
                p.read_bytes() if p.is_file() and not p.is_symlink() else None,
            )
            for p in source.rglob("*")
        }

        KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        after = {
            str(p.relative_to(source)): (
                os.lstat(p).st_mode,
                p.read_bytes() if p.is_file() and not p.is_symlink() else None,
            )
            for p in source.rglob("*")
        }
        assert before == after

    def test_top_level_symlink_still_refuses_inheritance(self, tmp_path):
        """A4.1 must not change the A4 symlink policy."""

        source = tmp_path / "src"
        source.mkdir()
        external = tmp_path / "external-store"
        external.mkdir()
        (external / "wd_external").write_text("{}")
        os.symlink(external, source / TRUST_DIR_NAME, target_is_directory=True)

        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        assert not (result.home / TRUST_DIR_NAME).exists()
        assert result.trust_source_state == "skipped-symlink"
        assert result.trust_truncated is False

    def test_internal_symlink_still_skipped(self, tmp_path):
        """A4.1 must not change the A4 internal-symlink policy."""

        source = self._source(tmp_path / "src")
        trust = source / TRUST_DIR_NAME
        (trust / "wd_real_record").write_text("{}")
        external = tmp_path / "external"
        external.mkdir()
        (external / "wd_secret").write_text("{}")
        os.symlink(external / "wd_secret", trust / "wd_link")

        result = KimiCodeRuntimeHomeBuilder(source, tmp_path / "temp").build()

        runtime_trust = result.home / TRUST_DIR_NAME
        assert not (runtime_trust / "wd_link").exists()
        assert "wd_link" in result.trust_skipped
        assert list(runtime_trust.rglob("wd_secret")) == []


class TestKimiMcpMerge:
    """A1.4 — merge semantics."""

    def test_read_user_mcp_servers_roundtrip(self, tmp_path):
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({"mcpServers": {"a": {"command": "x"}}}))
        assert set(read_user_mcp_servers(path)) == {"a"}

    def test_missing_user_file_is_empty(self, tmp_path):
        assert read_user_mcp_servers(tmp_path / "absent.json") == {}

    def test_unparseable_user_file_is_empty(self, tmp_path):
        path = tmp_path / "mcp.json"
        path.write_text("{ this is not json")
        assert read_user_mcp_servers(path) == {}

    def test_profile_servers_merge_with_user(self):
        merged = merge_mcp_servers({"user": {"command": "u"}}, {"profile": {"command": "p"}})
        assert set(merged) == {"user", "profile"}

    def test_profile_wins_on_name_collision(self):
        merged = merge_mcp_servers(
            {"shared": {"command": "user-cmd"}}, {"shared": {"command": "profile-cmd"}}
        )
        assert merged["shared"]["command"] == "profile-cmd"

    def test_transport_is_never_injected(self):
        merged = merge_mcp_servers({}, {"s": {"command": "npx", "args": ["-y", "x"]}})
        assert "transport" not in merged["s"]

    def test_terminal_id_is_never_injected(self):
        """CODE reaches MCP children via inherited env, not per-server env."""

        merged = merge_mcp_servers({}, {"s": {"command": "npx"}})
        assert "CAO_TERMINAL_ID" not in merged["s"].get("env", {})

    def test_explicit_env_preserved(self):
        merged = merge_mcp_servers({}, {"s": {"command": "npx", "env": {"K": "V"}}})
        assert merged["s"]["env"] == {"K": "V"}

    def test_timeout_becomes_both_mcp_timeouts(self):
        """CAO's single knob maps onto Kimi's per-server pair (A1.4 rule 5)."""

        merged = merge_mcp_servers({}, {"s": {"command": "npx", "timeout": 120}})
        assert merged["s"]["startupTimeoutMs"] == 120
        assert merged["s"]["toolTimeoutMs"] == 120
        assert "timeout" not in merged["s"]

    def test_timeout_is_clamped_into_kimi_schema_range(self):
        """Kimi rejects a timeout outside 1 … 2147483647."""

        low = merge_mcp_servers({}, {"s": {"command": "npx", "timeout": 0}})
        assert low["s"]["toolTimeoutMs"] == 1
        high = merge_mcp_servers({}, {"s": {"command": "npx", "timeout": 10**12}})
        assert high["s"]["toolTimeoutMs"] == 2147483647

    def test_url_entry_passes_through_untouched(self):
        """A non-`command` entry is left alone; Kimi infers the transport."""

        merged = merge_mcp_servers({}, {"s": {"url": "https://example.invalid/mcp"}})
        assert merged["s"] == {"url": "https://example.invalid/mcp"}
        assert "transport" not in merged["s"]

    def test_profile_none_yields_user_servers(self):
        assert set(merge_mcp_servers({"u": {"command": "x"}}, None)) == {"u"}


class TestResolveSourceHome:
    def test_absolute_captured_value_wins(self, tmp_path):
        assert resolve_source_home("/custom/home", tmp_path) == Path("/custom/home")

    def test_tilde_is_expanded(self, tmp_path):
        assert resolve_source_home("~/kh", tmp_path).is_absolute()

    def test_empty_falls_back_to_default(self, tmp_path):
        assert resolve_source_home("", tmp_path) == tmp_path / ".kimi-code"
        assert resolve_source_home(None, tmp_path) == tmp_path / ".kimi-code"

    def test_relative_value_falls_back_not_to_cwd(self, tmp_path):
        """A relative value must never resolve against the process cwd."""

        assert resolve_source_home("relative/path", tmp_path) == tmp_path / ".kimi-code"


# =============================================================================
# Status + extraction against the real captures
# =============================================================================


class TestKimiCodeStatusOnRealCaptures:
    """A2.4 — `get_status` end-to-end on the 0.43.1 captures."""

    def _status(self, name: str, terminal_id: str):
        provider = KimiCliProvider(terminal_id, "session-1", "window-1")
        return provider.get_status(_fixture(name))

    def test_fresh_startup_is_idle(self):
        assert (
            self._status("kimi_code_0431_01_fresh_startup_idle.txt", "t-01") is TerminalStatus.IDLE
        )

    def test_processing_turn_is_processing(self):
        assert (
            self._status("kimi_code_0431_02_processing_turn.txt", "t-02")
            is TerminalStatus.PROCESSING
        )

    def test_final_answer_is_completed(self):
        """A0 D2 regression: the `●` bullet must latch received-input."""

        assert (
            self._status("kimi_code_0431_03_final_answer.txt", "t-03") is TerminalStatus.COMPLETED
        )

    def test_mcp_startup_is_not_processing(self):
        """A0 D1 regression: boot chrome must not read as a live turn."""

        assert self._status("kimi_code_0431_05_mcp_startup.txt", "t-05") is TerminalStatus.IDLE

    def test_false_moon_spinner_is_not_processing(self):
        """A0 D1 regression, the exact defect fixture."""

        status = self._status("kimi_code_0431_09_false_moon_spinner_idle.txt", "t-09")
        assert status is TerminalStatus.IDLE
        assert status is not TerminalStatus.PROCESSING

    def test_empty_output_is_unknown(self):
        provider = KimiCliProvider("t-empty", "s", "w")
        assert provider.get_status("") is TerminalStatus.UNKNOWN

    def test_legacy_fixture_still_completes(self):
        """A2.1 — the legacy path must be untouched."""

        provider = KimiCliProvider("t-legacy", "s", "w")
        status = provider.get_status(_fixture("kimi_cli_completed_output.txt"))
        assert status in {TerminalStatus.COMPLETED, TerminalStatus.IDLE}


class TestKimiCodeExtractionOnRealCaptures:
    """A2.5 — extraction must never hand back reasoning."""

    def test_final_answer_extracts_prose(self):
        provider = KimiCliProvider("t-x03", "s", "w")
        result = provider.extract_last_message_from_script(
            _fixture("kimi_code_0431_03_final_answer.txt")
        )
        assert result.strip()

    def test_thinking_text_absent_from_answer(self):
        provider = KimiCliProvider("t-x03b", "s", "w")
        result = provider.extract_last_message_from_script(
            _fixture("kimi_code_0431_03_final_answer.txt")
        )
        for _, clean, kind in kt.classify_lines(_fixture("kimi_code_0431_03_final_answer.txt")):
            if kind is kt.KimiLineKind.THINKING_BULLET and clean.strip():
                body = clean.strip().lstrip("•●").strip()
                if len(body) > 12:
                    assert body not in result

    def test_answer_has_no_spinner_glyph(self):
        provider = KimiCliProvider("t-x03c", "s", "w")
        result = provider.extract_last_message_from_script(
            _fixture("kimi_code_0431_03_final_answer.txt")
        )
        assert not any(kt.has_live_spinner_glyph(line) for line in result.split("\n"))

    def test_answer_has_no_status_footer(self):
        provider = KimiCliProvider("t-x03d", "s", "w")
        result = provider.extract_last_message_from_script(
            _fixture("kimi_code_0431_03_final_answer.txt")
        )
        assert "context:" not in result

    def test_all_thinking_raises_extraction_error(self):
        provider = KimiCliProvider("t-think", "s", "w")
        output = (
            "╭──────────────────╮\n"
            "│ analyze this       │\n"
            "╰──────────────────╯\n"
            "\x1b[38;5;244m● \x1b[3mLet me analyze the code.\x1b[0m\n"
            "\x1b[38;5;244m● \x1b[3mI see several patterns.\x1b[0m\n"
            "user@my-app💫\n"
        )
        with pytest.raises(OutputExtractionError):
            provider.extract_last_message_from_script(output)

    def test_extraction_error_does_not_leak_reasoning(self):
        provider = KimiCliProvider("t-think2", "s", "w")
        output = (
            "╭──────────────────╮\n"
            "│ analyze this       │\n"
            "╰──────────────────╯\n"
            "\x1b[38;5;244m● \x1b[3mLet me analyze the code.\x1b[0m\n"
            "user@my-app💫\n"
        )
        with pytest.raises(OutputExtractionError) as excinfo:
            provider.extract_last_message_from_script(output)
        assert "analyze the code" not in str(excinfo.value)

    def test_thinking_then_answer_returns_only_answer(self):
        provider = KimiCliProvider("t-mix", "s", "w")
        output = (
            "╭──────────────────╮\n"
            "│ analyze this       │\n"
            "╰──────────────────╯\n"
            "\x1b[38;5;244m● \x1b[3mLet me analyze the code.\x1b[0m\n"
            "● The answer is 391.\n"
            "user@my-app💫\n"
        )
        result = provider.extract_last_message_from_script(output)
        assert "391" in result
        assert "analyze the code" not in result

    def test_legacy_fixture_extraction_unchanged(self):
        provider = KimiCliProvider("t-legacy-x", "s", "w")
        result = provider.extract_last_message_from_script(
            _fixture("kimi_cli_completed_output.txt")
        )
        assert result.strip()


class TestKimiCodeResponseRegion:
    """A2.4 — the region must be located by layout, not by box position.

    The Kimi Code TUI renders the transcript *above* a persistent composer.
    The historical "everything after the last input box" rule therefore anchors
    on the composer and returns the status footer as the answer. Measured on
    the 0.43.1 captures: the legacy rule yielded 173 characters of footer
    chrome for a completed turn whose answer is `STEP 1 … A0-FIXTURE-DONE.`.
    """

    def test_final_answer_region_excludes_footer(self):
        provider = KimiCliProvider("t-region", "s", "w")
        result = provider.extract_last_message_from_script(
            _fixture("kimi_code_0431_03_final_answer.txt")
        )
        assert "STEP 1" in result
        assert "A0-FIXTURE-DONE." in result
        assert "shift-tab to Plan mode" not in result
        assert "thinking" not in result
        assert "context:" not in result

    def test_final_answer_region_excludes_collapsed_tool_row(self):
        provider = KimiCliProvider("t-region2", "s", "w")
        result = provider.extract_last_message_from_script(
            _fixture("kimi_code_0431_03_final_answer.txt")
        )
        assert "more lines" not in result
        assert "ctrl+o" not in result

    def test_locator_returns_none_without_user_echo(self):
        kinds = [kt.KimiLineKind.CONTENT, kt.KimiLineKind.STATUS_FOOTER]
        assert KimiCliProvider._locate_response_region(kinds) is None

    def test_locator_ends_at_composer(self):
        kinds = [
            kt.KimiLineKind.USER_INPUT,
            kt.KimiLineKind.FINAL_BULLET,
            kt.KimiLineKind.CONTENT,
            kt.KimiLineKind.READY_INPUT_FRAME,
            kt.KimiLineKind.STATUS_FOOTER,
        ]
        assert KimiCliProvider._locate_response_region(kinds) == (1, 3)

    def test_locator_ends_at_status_footer_when_no_composer(self):
        kinds = [
            kt.KimiLineKind.USER_INPUT,
            kt.KimiLineKind.FINAL_BULLET,
            kt.KimiLineKind.STATUS_FOOTER,
        ]
        assert KimiCliProvider._locate_response_region(kinds) == (1, 2)

    def test_locator_falls_through_to_end_of_capture(self):
        kinds = [
            kt.KimiLineKind.USER_INPUT,
            kt.KimiLineKind.FINAL_BULLET,
            kt.KimiLineKind.CONTENT,
        ]
        assert KimiCliProvider._locate_response_region(kinds) == (1, 3)

    def test_locator_uses_the_last_echo(self):
        kinds = [
            kt.KimiLineKind.USER_INPUT,
            kt.KimiLineKind.FINAL_BULLET,
            kt.KimiLineKind.USER_INPUT,
            kt.KimiLineKind.FINAL_BULLET,
        ]
        assert KimiCliProvider._locate_response_region(kinds) == (3, 4)

    def test_mid_turn_pane_returns_partial_answer_without_chrome(self):
        """A processing pane legitimately yields partial output — but no chrome.

        CAO extracts mid-turn during PROCESSING, so partial text is expected.
        What must never appear is the spinner row, the status footer, or the
        thinking/tool rows that surround the partial answer.
        """

        provider = KimiCliProvider("t-mid", "s", "w")
        result = provider.extract_last_message_from_script(
            _fixture("kimi_code_0431_02_processing_turn.txt")
        )
        assert result.strip()
        assert not any(kt.has_live_spinner_glyph(line) for line in result.split("\n"))
        assert "context:" not in result
        assert "· Tip:" not in result

    def test_approval_dialog_pane_yields_no_answer(self):
        """Fixture 08 is a turn blocked on approval — there is no answer yet.

        The pane holds six `● Running a command` rows, their `Press Ctrl+B`
        hints, the collapse rows, and the dialog. Every one of those is chrome,
        so the correct outcome is an extraction failure rather than handing
        execution plumbing to the caller. (With `--auto` this dialog is not
        reached at all — fixture 08 was captured under `--yolo`.)
        """

        provider = KimiCliProvider("t-08x", "s", "w")
        with pytest.raises(OutputExtractionError) as excinfo:
            provider.extract_last_message_from_script(
                _fixture("kimi_code_0431_08_command_approval_dialog.txt")
            )
        message = str(excinfo.value)
        assert "Running a command" not in message
        assert "Press Ctrl+B" not in message

    def test_fresh_startup_pane_has_no_extractable_answer(self):
        provider = KimiCliProvider("t-fresh", "s", "w")
        with pytest.raises(OutputExtractionError):
            provider.extract_last_message_from_script(
                _fixture("kimi_code_0431_01_fresh_startup_idle.txt")
            )


class TestKimiCodePatterns:
    """The provider-level pattern constants the classifier backs."""

    def test_welcome_banner_accepts_both_variants(self):
        import re

        from cli_agent_orchestrator.providers.kimi_cli import WELCOME_BANNER_PATTERN

        assert re.search(WELCOME_BANNER_PATTERN, "Welcome to Kimi Code CLI!")
        assert re.search(WELCOME_BANNER_PATTERN, "Welcome to Kimi Code!")

    def test_response_bullet_pattern_accepts_both_glyphs(self):
        import re

        from cli_agent_orchestrator.providers.kimi_cli import RESPONSE_BULLET_PATTERN

        assert re.search(RESPONSE_BULLET_PATTERN, "● answer")
        assert re.search(RESPONSE_BULLET_PATTERN, "• answer")
        assert not re.search(RESPONSE_BULLET_PATTERN, "  • indented")

    def test_moon_is_not_a_spinner(self):
        from cli_agent_orchestrator.providers.kimi_cli import _is_live_turn_spinner_line

        assert not _is_live_turn_spinner_line(
            "\U0001f315\x1b[38;5;244m · Tip: ctrl-s to add guidance"
        )

    def test_braille_is_a_spinner(self):
        from cli_agent_orchestrator.providers.kimi_cli import _is_live_turn_spinner_line

        assert _is_live_turn_spinner_line("\x1b[38;5;111m⠙\x1b[39m working…")

    def test_boot_chrome_braille_is_not_a_spinner(self):
        from cli_agent_orchestrator.providers.kimi_cli import _is_live_turn_spinner_line

        assert not _is_live_turn_spinner_line("⠧ MCP Servers: 0/1")


# =============================================================================
# A2.1 — legacy compatibility guards
# =============================================================================


class TestKimiLegacyUnchanged:
    """A2.1 — every legacy contract the CODE path must not have disturbed."""

    def test_legacy_builder_still_yolo_and_cd(self):
        provider = KimiCliProvider("term-legacy", "s", "w")
        command = provider._build_kimi_command()
        assert "kimi --yolo" in command
        assert "cd " in command
        assert "TERM=xterm-256color" in command

    def test_legacy_builder_uses_supplied_binary(self):
        provider = KimiCliProvider("term-legacy2", "s", "w")
        command = provider._build_kimi_command("/opt/kimi")
        assert "/opt/kimi --yolo" in command

    def test_legacy_yaml_agent_still_emitted(self, tmp_path, monkeypatch):
        provider = KimiCliProvider("term-legacy3", "s", "w", agent_profile="developer")
        profile = MagicMock()
        profile.system_prompt = "You are helpful"
        profile.model = None
        profile.mcpServers = None
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.kimi_cli.load_agent_profile",
            lambda _name: profile,
        )
        command = provider._build_kimi_command()
        assert "--agent-file" in command
        agent_yaml = Path(provider._temp_dir) / "agent.yaml"
        assert agent_yaml.is_file()
        assert "extend: default" in agent_yaml.read_text()

    def test_legacy_mcp_config_flag_still_used(self, monkeypatch):
        provider = KimiCliProvider("term-legacy4", "s", "w", agent_profile="developer")
        profile = MagicMock()
        profile.system_prompt = None
        profile.model = None
        profile.mcpServers = {"cao-mcp-server": {"command": "npx", "args": ["-y", "x"]}}
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.kimi_cli.load_agent_profile",
            lambda _name: profile,
        )
        monkeypatch.setattr(KimiCliProvider, "_ensure_mcp_timeout", classmethod(lambda cls: None))
        command = provider._build_kimi_command()
        assert "--mcp-config" in command
        assert "CAO_TERMINAL_ID" in command

    def test_legacy_uses_temp_cwd_not_real_cwd(self):
        provider = KimiCliProvider("term-legacy5", "s", "w")
        command = provider._build_kimi_command()
        assert f"cd {provider._temp_dir}" in command

    def test_legacy_moon_processing_still_accepted(self):
        """The bare-moon processing signal stays on the legacy path."""

        provider = KimiCliProvider("term-legacy6", "s", "w")
        # A legacy pane: emoji prompt chrome plus a bare moon as the only
        # in-flight signal. It must not be forced to IDLE by the CODE-side
        # moon-tip change.
        output = (
            "╭──────────────────╮\n"
            "│ do the thing       │\n"
            "╰──────────────────╯\n"
            "user@my-app💫 do the thing\n"
            "\U0001f315\n"
        )
        assert provider.get_status(output) is not TerminalStatus.UNKNOWN


# =============================================================================
# A3 — defects found by review, not covered by the A2 suite
# =============================================================================

#: The Kimi Code composer, as it appears below the transcript.
_A3_COMPOSER = ("╭────────────────────╮", "│ >                  │", "╰────────────────────╯")
#: A realistic Kimi Code status footer (matches NEW_TUI_STATUS_PATTERN).
_A3_FOOTER = " Never Ask  A3 Probe thinking  …/proj  master   context: 0% (0/977k)"


def _answer(text: str) -> str:
    """A final-answer row drawn the way the renderer draws it (colour 253).

    The tool-block state machine ends a block only on *renderer* evidence, and
    an unstyled ``●`` is not evidence: it is exactly what tool payload that
    starts with a bullet looks like. Synthetic rows therefore carry the measured
    answer styling, so these tests describe what a real capture contains.
    """

    return f" \x1b[38;5;253m● \x1b[39m{text}"


def _styled_footer(text: str = "context: 4% (33.7k/977k)") -> str:
    """A status-footer row drawn in the renderer's own colour."""

    return f" \x1b[38;5;253m{text}\x1b[39m"


def _styled_composer() -> tuple:
    """The composer frame as drawn: box glyphs in colour 240."""

    return (
        " \x1b[38;5;240m╭────────────────────╮\x1b[39m",
        " \x1b[38;5;240m│\x1b[39m > \x1b[7m \x1b[0m",
        " \x1b[38;5;240m╰────────────────────╯\x1b[39m",
    )


class TestA3WrappedBulletStatus:
    """A3-1 — a wrapped status-bar fragment must not latch COMPLETED.

    PR #664's defect: on a narrow terminal the status bar wraps so a row can
    begin with a bare ``●`` followed by punctuation. Matching that as assistant
    output latched "input received" on a terminal that had never been sent
    anything, and the terminal reported COMPLETED while idle.
    """

    @pytest.mark.parametrize(
        "row,expected",
        [
            ("● answer", True),
            ("• answer", True),
            ("●)", False),
            ("•)", False),
            ("●", False),
            ("•", False),
            ("  • response", True),
            ("\t• response", True),
            ("text • more", False),
            ("●   spaced payload", True),
        ],
    )
    def test_response_marker_rows(self, row, expected):
        assert kt.is_response_marker_line(row) is expected

    def test_provider_and_classifier_share_one_bullet_rule(self):
        """A3-1 — no second, narrower bullet definition may reappear."""

        from cli_agent_orchestrator.providers import kimi_cli

        assert kimi_cli.BULLET_LINE_PATTERN is kt.BULLET_ANY_RE
        assert not hasattr(kimi_cli, "ANY_BULLET_PATTERN")

    def test_narrow_terminal_pane_is_idle_on_the_raw_path(self):
        """The exact A3-1 reproduction, raw buffer."""

        provider = _code_provider("a3-1-raw")
        pane = "\n".join(
            [
                "Welcome to Kimi Code!",
                "●)",
                "agent (kimi-k2.6 ●)",
                "context: 100%",
            ]
        )
        assert provider.get_status(pane) is TerminalStatus.IDLE

    def test_narrow_terminal_pane_is_idle_on_the_screen_path(self):
        """The same reproduction through the escape-free rendered screen."""

        provider = _code_provider("a3-1-screen")
        rows = ["Welcome to Kimi Code!", "●)", "agent (kimi-k2.6 ●)", "context: 100%"]
        assert provider.get_status_from_screen(rows) is TerminalStatus.IDLE

    def test_both_status_paths_agree_on_a_real_answer(self):
        """Raw and rendered paths must not disagree about a finished turn."""

        pane = "\n".join(["✨ what is 2+2", "● The answer is 4.", *_A3_COMPOSER, _A3_FOOTER])
        rows = [line for line in pane.split("\n") if line.strip()]

        raw = _code_provider("a3-1-agree-raw")
        screen = _code_provider("a3-1-agree-screen")
        assert raw.get_status(pane) is TerminalStatus.COMPLETED
        assert screen.get_status_from_screen(rows) is TerminalStatus.COMPLETED

    def test_wrapped_fragment_does_not_latch_received_input(self):
        provider = _code_provider("a3-1-latch")
        provider.get_status("\n".join(["●)", "context: 100%"]))
        assert provider._has_received_input is False

    def test_real_answer_does_latch_received_input(self):
        provider = _code_provider("a3-1-latch-real")
        provider.get_status("\n".join(["● a real answer", "context: 100%"]))
        assert provider._has_received_input is True


class TestA3ContentChromeCollision:
    """A3-2 — chrome classification must be structural, not substring-based.

    ``_locate_response_region`` ends the transcript at the first chrome row, so
    a substring rule turned any answer that *mentioned* boot vocabulary or the
    footer into a truncation point.
    """

    #: Assistant prose that must survive extraction verbatim.
    ADVERSARIAL_LINES = (
        "● connecting to mcp servers is not the problem",
        "● Loading configuration is the next step",
        "● Welcome to Kimi Code! is the literal banner",
        "The reported context: 50% is expected.",
        "Use ctrl-o to hide or reveal tool output if needed.",
    )

    def _pane(self, body):
        return "\n".join(["✨ summarise the findings", *body, *_A3_COMPOSER, _A3_FOOTER])

    @pytest.mark.parametrize("line", ADVERSARIAL_LINES)
    def test_adversarial_line_is_answer_content(self, line):
        kind = kt.classify_line(line, line)
        assert kind in kt.ANSWER_KINDS, f"{line!r} -> {kind}"

    @pytest.mark.parametrize("line", ADVERSARIAL_LINES)
    def test_adversarial_line_is_neither_boot_chrome_nor_footer(self, line):
        assert kt.is_boot_chrome_line(line, line) is False
        assert kt.is_status_footer_line(line) is False

    def test_reproduction_pane_is_not_truncated(self):
        """The A3-2 reproduction: only the first line used to survive."""

        pane = self._pane(
            [
                "● First line",
                "connecting to mcp servers is only a phrase",
                "Final line",
            ]
        )
        result = _code_provider("a3-2-repro").extract_last_message_from_script(pane)
        assert result == ("● First line\nconnecting to mcp servers is only a phrase\nFinal line")

    def test_all_adversarial_lines_survive_one_extraction(self):
        pane = self._pane(list(self.ADVERSARIAL_LINES))
        result = _code_provider("a3-2-all").extract_last_message_from_script(pane)
        for line in self.ADVERSARIAL_LINES:
            assert line in result, line

    def test_real_boot_chrome_is_still_detected(self):
        """Tightening must not weaken genuine chrome detection."""

        real_chrome = (
            "⠋ Loading configuration...",
            "⠏ Restoring conversation...",
            "Send /help for help information.",
            "No session yet — one will be created on your first message.",
            "Run /web to continue your session in the browser",
            "Welcome to Kimi Code!",
            # The 0.43.1 banner row as measured: leading edge, glyph, banner
            # text, space-padded to the terminal width — no closing edge.
            "│  ▐█▛█▛█▌  Welcome to Kimi Code!",
            "│  ▐█▛█▛█▌  Welcome to Kimi Code!                                                                     ",
            "⠧ MCP Servers: 0/1",
            'MCP server "cao-sentinel" connected · 0 tools (stdio)',
        )
        for row in real_chrome:
            assert kt.is_boot_chrome_line(row, row) is True, row

    def test_the_real_banner_row_is_recognised_without_ansi(self):
        """The screen path sees escape-free rows; the box rule must still fire."""

        raw_row = next(
            line
            for line in _fixture("kimi_code_0431_01_fresh_startup_idle.txt").split("\n")
            if "Welcome to Kimi Code" in line
        )
        assert kt.is_boot_chrome_line(raw_row, raw_row) is True
        assert kt.is_boot_chrome_line(kt.strip_sgr(raw_row), "") is True

    def test_nothing_in_the_fresh_startup_capture_is_answer_content(self):
        """A2 evidence, re-asserted under A3-2's structural rules.

        The startup capture contains the banner, the MCP rows and the composer.
        If chrome classification regressed anywhere, one of those rows would
        fall through to ``ANSWER_KINDS`` and a fresh terminal would look like it
        had produced an answer.

        The capture's first rows are the launch shell echo (``[user@host dir]$
        …``) — an artefact of how A0 captured the pane, not TUI content — so the
        assertion starts at the first Kimi row.
        """

        rows = kt.classify_lines(
            _fixture("kimi_code_0431_01_fresh_startup_idle.txt"), kt.SpinnerSemantics.CODE
        )
        first_tui = next(
            index for index, (_, _, kind) in enumerate(rows) if kind is kt.KimiLineKind.BOOT_CHROME
        )
        leaked = [
            clean
            for _, clean, kind in rows[first_tui:]
            if kind in kt.ANSWER_KINDS and clean.strip()
        ]
        assert leaked == []

    def test_real_footer_rows_are_still_detected(self):
        real_footers = (
            "context: 0% (0/977k)",
            "                    context: 0.0% (0/262.1k",
            "agent (kimi-k2.6 ●)",
            "  ctrl-o to hide or reveal tool output",
            " Never Ask  A3 Probe thinking  …/proj  master  shift-tab to Plan mode  context: 4.0% (10.4k/262.1k)",
            "23:14  yolo  agent (kimi-for-coding, thinking)  ctrl-x: toggle mode",
        )
        for row in real_footers:
            assert kt.is_status_footer_line(row) is True, row

    def test_boot_chrome_ends_a_region_only_when_shaped_like_chrome(self):
        """A region containing a chrome-shaped row still ends there."""

        pane = "\n".join(
            [
                "✨ go",
                "● Answer line",
                "⠙ Loading configuration...",
                "not part of the answer",
                *_A3_COMPOSER,
                _A3_FOOTER,
            ]
        )
        result = _code_provider("a3-2-end").extract_last_message_from_script(pane)
        assert result == "● Answer line"


class TestA3ToolRowLeak:
    """A3-E2E — a completed tool row must not reach the extracted answer.

    Found by the A3 live E2E, not by the A2 suite: Kimi Code 0.43.1 renders a
    finished tool call as ``● Used <Tool> (<arg>) · <N> lines``, and neither
    tool-row pattern covered the ``Used`` verb. The row shares the answer's
    colour-253 bullet, so it was classified as a final bullet and the extracted
    message began with execution plumbing.

    The A0 fixtures contain no completed tool call, which is why only a real
    turn could surface this.
    """

    TOOL_ROWS = (
        "● Used Read (ANSWER_SPEC.md) · 10 lines",
        "● Used Bash (ls -la) · 3 lines",
        "● Used Grep (pattern=foo) · 1 line",
        "● Running a command · $ uname -a",
    )

    #: Ordinary prose that merely starts with the same verb.
    #:
    #: `● Used Python` is deliberately here rather than in TOOL_ROWS. A bare
    #: `<verb> <word>` row carries no structural evidence — no `·` detail
    #: separator, no argument list — so by the same doctrine that keeps a row's
    #: *text* from making it UI state it is answer content. The measured collapse
    #: is always `● Used <Tool> (<arg>) · <N> lines`, so the evidence is there
    #: whenever the renderer drew a tool row; treating the bare shape as a tool
    #: row instead refused answers such as "Used Python".
    PROSE_ROWS = (
        "● Used widely in production.",
        "● Used Python extensively for this.",
        "● Used to be a problem, but no longer.",
        "● Used Python",
        "• Used pandas",
    )

    @pytest.mark.parametrize("row", TOOL_ROWS)
    def test_tool_rows_are_not_answer_content(self, row):
        kind = kt.classify_line(row, row)
        assert kind is kt.KimiLineKind.TOOL_CALL, f"{row!r} -> {kind}"
        assert kind not in kt.ANSWER_KINDS

    @pytest.mark.parametrize("row", PROSE_ROWS)
    def test_prose_starting_with_used_stays_content(self, row):
        """The guard against over-matching: this must not be deleted."""

        kind = kt.classify_line(row, row)
        assert kind in kt.ANSWER_KINDS, f"{row!r} -> {kind}"

    def test_styled_tool_row_is_not_answer_content(self):
        row = "\x1b[38;5;253m● \x1b[1m\x1b[38;5;111mUsed Read\x1b[0m (ANSWER_SPEC.md) · 10 lines"
        assert kt.classify_line(row, kt.strip_sgr(row)) is kt.KimiLineKind.TOOL_CALL

    def test_tool_row_absent_from_the_extracted_answer(self):
        pane = "\n".join(
            [
                "✨ do the thing",
                "● Used Read (ANSWER_SPEC.md) · 10 lines",
                _answer("The real answer."),
                *_A3_COMPOSER,
                _A3_FOOTER,
            ]
        )
        result = _code_provider("a3-tool-leak").extract_last_message_from_script(pane)
        assert result == "● The real answer."


class TestA3ScreenStatusBootGate:
    """A3-3 — the screen boot gate must use shared bullet semantics."""

    def test_prose_mention_does_not_strand_a_completed_terminal(self):
        """A `●` row quoting the boot chrome is an answer, not boot chrome."""

        rows = [
            "✨ is mcp the problem",
            "● connecting to mcp servers is not the problem",
            "context: 4.0% (10.4k/262.1k)",
        ]
        assert _code_provider("a3-3-prose").get_status_from_screen(rows) is TerminalStatus.COMPLETED

    def test_real_boot_row_still_gates(self):
        rows = [
            "Welcome to Kimi Code!",
            "⠧ MCP Servers: 0/1",
            "context: 0% (0/977k)",
        ]
        assert _code_provider("a3-3-boot").get_status_from_screen(rows) is TerminalStatus.PROCESSING

    def test_legacy_bullet_answer_is_not_gated(self):
        """The gate must not exclude only one dialect's glyph."""

        rows = [
            "user@my-app💫 what changed",
            "• connecting to mcp servers is only a phrase",
            "context: 4.0% (10.4k/262.1k)",
        ]
        assert (
            _code_provider("a3-3-legacy-glyph").get_status_from_screen(rows)
            is TerminalStatus.COMPLETED
        )


class TestA3DialectSpinner:
    """A3-4 — spinner semantics must be dialect-correct."""

    def test_code_braille_is_processing(self):
        assert kt.is_live_spinner_line("⠙ working…", "⠙ working…", kt.SpinnerSemantics.CODE) is True

    def test_code_idle_moon_tip_is_not_processing(self):
        row = "🌕 · Tip: use ctrl-o to expand tool output"
        assert kt.is_live_spinner_line(row, row, kt.SpinnerSemantics.CODE) is False

    def test_code_standalone_moon_is_not_processing(self):
        """A moon line in a Kimi Code answer is content, not work."""

        assert kt.is_live_spinner_line("🌕", "🌕", kt.SpinnerSemantics.CODE) is False

    def test_legacy_bare_moon_is_still_processing(self):
        assert kt.is_live_spinner_line("🌕", "🌕", kt.SpinnerSemantics.LEGACY) is True

    def test_legacy_is_the_default_semantics(self):
        assert kt.is_live_spinner_line("🌕") is True

    def test_provider_forwards_its_dialect_semantics(self):
        from cli_agent_orchestrator.providers.kimi_cli import _is_live_turn_spinner_line

        assert _is_live_turn_spinner_line("🌕", kt.SpinnerSemantics.LEGACY) is True
        assert _is_live_turn_spinner_line("🌕", kt.SpinnerSemantics.CODE) is False

    def test_completed_answer_with_a_standalone_moon_stays_completed(self):
        """The A3-4 regression: a moon line must not force PROCESSING."""

        pane = "\n".join(
            [
                "✨ show me the moon phases",
                "● Here are the phases:",
                "🌕",
                "● That is all.",
                *_A3_COMPOSER,
                _A3_FOOTER,
            ]
        )
        provider = _code_provider("a3-4-moon")
        assert provider.get_status(pane) is TerminalStatus.COMPLETED

        result = provider.extract_last_message_from_script(pane)
        assert "🌕" in result
        assert "That is all." in result

    def test_completed_answer_with_a_moon_stays_completed_on_screen(self):
        rows = [
            "✨ show me the moon phases",
            "● Here are the phases:",
            "🌕",
            "● That is all.",
            "context: 2% (18.5k/977k)",
        ]
        assert (
            _code_provider("a3-4-moon-screen").get_status_from_screen(rows)
            is TerminalStatus.COMPLETED
        )

    def test_legacy_moon_pane_is_still_processing(self):
        """The legacy fail-safe must survive the dialect split."""

        provider = KimiCliProvider("a3-4-legacy", "s", "w")
        pane = "\n".join(["✨ do the thing", "🌕"])
        assert provider.get_status(pane) is TerminalStatus.PROCESSING


class TestA3TruecolorThinking:
    """A3-6 — truecolor reasoning detection must be measured, not "any colour"."""

    @staticmethod
    def _bullet(r, g, b, text="line"):
        return f"\x1b[38;2;{r};{g};{b}m● {text}\x1b[0m"

    def test_truecolor_grey_bullet_is_thinking(self):
        for triple in ((128, 128, 128), (129, 130, 127), (0, 16, 8)):
            assert kt.is_thinking_styled(self._bullet(*triple)) is True, triple

    def test_truecolor_saturated_bullet_is_not_thinking(self):
        """A themed final answer must not be suppressed as reasoning."""

        for triple in ((255, 100, 50), (30, 144, 255), (0, 200, 0), (200, 0, 255)):
            assert kt.is_thinking_styled(self._bullet(*triple)) is False, triple

    def test_truecolor_bright_grey_is_still_thinking(self):
        """Documented fail-safe asymmetry, not an oversight.

        Both of Kimi's own styles are greyscale (reasoning 244, answer 253), so
        channel spread cannot separate them — the measured answer colour is what
        does. For a truecolor row CAO therefore errs toward reasoning: a misread
        *answer* bullet raises OutputExtractionError (loud), whereas a misread
        *reasoning* bullet would silently publish private reasoning as the
        agent's message.
        """

        assert kt.is_thinking_styled(self._bullet(255, 255, 255)) is True
        assert kt.is_thinking_styled(self._bullet(230, 232, 228)) is True
        # ...but the measured answer colour is decisive in the other direction.
        assert kt.is_thinking_styled("\x1b[38;5;253m● answer\x1b[0m") is False

    def test_grey_threshold_is_the_boundary(self):
        tolerance = kt.TRUECOLOR_GREY_TOLERANCE
        just_inside = (100, 100 + tolerance, 100)
        just_outside = (100, 100 + tolerance + 1, 100)
        assert kt.is_thinking_styled(self._bullet(*just_inside)) is True
        assert kt.is_thinking_styled(self._bullet(*just_outside)) is False

    def test_final_answer_colour_is_decisive(self):
        assert kt.is_thinking_styled("\x1b[38;5;253m● final answer\x1b[0m") is False
        assert kt.is_thinking_styled("\x1b[38;5;253m\x1b[3m● italic final\x1b[0m") is False

    def test_grey_244_is_still_thinking(self):
        assert kt.is_thinking_styled("\x1b[38;5;244m● reasoning\x1b[0m") is True

    def test_truecolor_grey_answer_row_is_filtered_from_extraction(self):
        pane = "\n".join(
            [
                "✨ go",
                self._bullet(128, 128, 128, "private reasoning here"),
                "● the visible answer",
                *_A3_COMPOSER,
                _A3_FOOTER,
            ]
        )
        result = _code_provider("a3-6-grey").extract_last_message_from_script(pane)
        assert "private reasoning here" not in result
        assert "the visible answer" in result

    def test_truecolor_answer_row_is_not_filtered(self):
        pane = "\n".join(
            [
                "✨ go",
                self._bullet(255, 100, 50, "a themed final answer"),
                *_A3_COMPOSER,
                _A3_FOOTER,
            ]
        )
        result = _code_provider("a3-6-sat").extract_last_message_from_script(pane)
        assert "a themed final answer" in result

    def test_all_truecolor_grey_raises(self):
        """The fail-closed property survives the tightening."""

        pane = "\n".join(
            [
                "✨ go",
                self._bullet(128, 128, 128, "first private thought"),
                self._bullet(130, 126, 130, "second private thought"),
                *_A3_COMPOSER,
                _A3_FOOTER,
            ]
        )
        with pytest.raises(OutputExtractionError):
            _code_provider("a3-6-all").extract_last_message_from_script(pane)


class TestA3McpTimeoutUnits:
    """A3-8 — the MCP timeout bindings are milliseconds, verified not assumed.

    Evidence (Kimi Code 0.43.1, bundled source):

    * ``MCP_STARTUP_TIMEOUT_ENV = "KIMI_MCP_STARTUP_TIMEOUT_MS"`` and the
      matching tool binding are read through ``parseTimeoutMsEnv``, whose body
      is ``Number(raw)`` accepted only when
      ``Number.isInteger(parsed) && parsed >= 1 && parsed <= 2147483647``;
    * the schema is ``McpTimeoutMsSchema = number().int().min(1).max(MAX_MCP_TIMEOUT_MS)``
      with ``MAX_MCP_TIMEOUT_MS = 2147483647``;
    * Kimi's own embedded MCP documentation states "Every timeout must be an
      integer from ``1`` to ``2147483647`` milliseconds";
    * the values bind to ``[mcp] startup_timeout_ms`` / ``[mcp] tool_timeout_ms``,
      and per-server ``startupTimeoutMs`` / ``toolTimeoutMs`` override them.

    So CAO's constants are already correct in unit; these tests pin that and
    stop a future "convert to seconds" change from silently shortening them.
    """

    def test_timeout_constants_are_milliseconds(self):
        assert KIMI_MCP_STARTUP_TIMEOUT_MS == 60_000
        assert KIMI_MCP_TOOL_TIMEOUT_MS == 600_000
        # 600 s: CAO's handoff budget, preserved from the legacy path.
        assert KIMI_MCP_TOOL_TIMEOUT_MS // 1000 == 600

    def test_timeout_constants_are_within_kimis_range(self):
        from cli_agent_orchestrator.providers.kimi_runtime_home import (
            MAX_MCP_TIMEOUT_MS,
            MIN_MCP_TIMEOUT_MS,
        )

        assert MAX_MCP_TIMEOUT_MS == 2_147_483_647
        assert MIN_MCP_TIMEOUT_MS == 1
        for value in (KIMI_MCP_STARTUP_TIMEOUT_MS, KIMI_MCP_TOOL_TIMEOUT_MS):
            assert MIN_MCP_TIMEOUT_MS <= value <= MAX_MCP_TIMEOUT_MS

    def test_bindings_are_exported_with_the_ms_names(self, tmp_path):
        provider = _code_provider("a3-8")
        provider._kimi_source_home = tmp_path / "src"
        command = provider._build_kimi_code_command()
        assert f"KIMI_MCP_STARTUP_TIMEOUT_MS={KIMI_MCP_STARTUP_TIMEOUT_MS}" in command
        assert f"KIMI_MCP_TOOL_TIMEOUT_MS={KIMI_MCP_TOOL_TIMEOUT_MS}" in command

    def test_profile_timeout_is_passed_through_as_ms(self, tmp_path):
        """A profile `timeout` becomes Kimi's per-server ms fields unchanged."""

        merged = merge_mcp_servers({}, {"s": {"command": "x", "timeout": 600_000}})
        assert merged["s"]["startupTimeoutMs"] == 600_000
        assert merged["s"]["toolTimeoutMs"] == 600_000
        assert "timeout" not in merged["s"]

    def test_profile_timeout_is_clamped_to_kimis_range(self):
        merged = merge_mcp_servers(
            {},
            {
                "too-big": {"command": "x", "timeout": 9_999_999_999},
                "too-small": {"command": "y", "timeout": 0},
            },
        )
        assert merged["too-big"]["startupTimeoutMs"] == 2_147_483_647
        assert merged["too-small"]["startupTimeoutMs"] == 1


# =============================================================================
# D6 — MCP tool blocks must not reach the extracted final response
# =============================================================================
#
# The D6 production smoke installed the frozen branch, launched a real
# CAO-managed Kimi Code worker in a pre-trusted repository, and asked it to call
# the CAO MCP tool `find_profiles`. `GET /terminals/{id}/output?mode=last` — the
# string CAO hands to handoff/assign callers and to the memory layer — returned:
#
#     hand off, message, or delete anything.
#     ● Used find_profiles · MCP/cao-mcp-server (kimi)
#     [{"name":"kimi-installed-deploy-smoke", …}] …
#     ● MCP-OK=1
#
# Only the last line was the answer. Three independent classification defects
# were responsible, and the deployment was rolled back:
#
#   D6-F1  the *wrapped* submitted user message was not recognised as user
#          echo (only the sparkle row was), so its continuation stayed inside
#          the response region;
#   D6-F2  the completed tool row was not recognised as a tool row, because the
#          rule required a **capitalised** tool name and CAO's own MCP tools are
#          snake_case (`find_profiles`, `send_message`, …);
#   D6-F3  the tool payload that follows a header carries no marker of its own,
#          so a row-by-row classifier cannot tell it from an indented prose
#          continuation and it reached the answer as `CONTENT`.
#
# The A3 suite could not catch any of these: it has no wrapped user message, its
# tool rows are capitalised built-ins, and it has no multi-line tool payload.

#: The submitted message as Kimi Code renders it: row 1 carries the sparkle and
#: colour 222 in the SPLIT SGR form, the wrapped continuation carries colour 222
#: in the COMBINED form and no sparkle at all.
_D6_PROMPT_FIRST = "\x1b[1m\x1b[38;5;222m✨ Use the CAO MCP tool find_profiles exactly once.\x1b[0m"
_D6_PROMPT_CONT = "    \x1b[1;38;5;222mhand off, message, or delete anything.\x1b[22m\x1b[39m"
#: The completed tool row, exactly as measured live: colour-114 bullet, plain
#: `Used `, bold colour-111 snake_case tool name, dim `· MCP/<server> (arg)`.
_D6_TOOL_ROW = (
    " \x1b[38;5;114m● \x1b[39mUsed \x1b[38;5;111m\x1b[1mfind_profiles\x1b[22m\x1b[39m"
    "\x1b[2m · MCP/cao-mcp-server\x1b[22m\x1b[2m (\x1b[22m\x1b[2mkimi\x1b[22m\x1b[2m)\x1b[22m\x1b[0m"
)
#: The payload row: dim-only and indented, no bullet.
_D6_PAYLOAD_ROW = '   \x1b[2m[{"name":"d6-probe"}]\x1b[22m\x1b[2m …\x1b[22m\x1b[0m'
_D6_FINAL_ROW = " \x1b[38;5;253m● \x1b[39mMCP-OK=1"


class TestD6UserInputContinuation:
    """D6-F1 — a wrapped submitted message is user echo, not answer content.

    Kimi Code styles submitted input in colour 222. Only the first row carries
    the sparkle, so a rule keyed on the sparkle (or on one literal SGR ordering)
    leaves every wrapped continuation row inside the response region.
    """

    def test_split_sgr_sparkle_row_is_user_input(self):
        assert kt.classify_line(_D6_PROMPT_FIRST) is kt.KimiLineKind.USER_INPUT

    def test_combined_sgr_continuation_is_user_input(self):
        """The measured defect: `ESC[1;38;5;222m`, no sparkle."""

        assert kt.classify_line(_D6_PROMPT_CONT) is kt.KimiLineKind.USER_INPUT

    def test_split_sgr_continuation_without_sparkle_is_user_input(self):
        raw = "    \x1b[1m\x1b[38;5;222mhand off, message, or delete anything.\x1b[0m"
        assert kt.classify_line(raw) is kt.KimiLineKind.USER_INPUT

    @pytest.mark.parametrize(
        "raw",
        [
            "    \x1b[38;5;222mcontinuation only\x1b[39m",
            "\x1b[1;38;5;222mcombined, no indent\x1b[0m",
        ],
    )
    def test_all_measured_colour_222_forms_are_user_input(self, raw):
        assert kt.classify_line(raw) is kt.KimiLineKind.USER_INPUT

    def test_unstyled_continuation_prose_stays_content(self):
        """The guard: arbitrary unstyled prose is NOT user input."""

        row = "hand off, message, or delete anything."
        assert kt.classify_line(row) is kt.KimiLineKind.CONTENT

    def test_unstyled_indented_prose_stays_content(self):
        row = "    just an indented continuation of the answer"
        assert kt.classify_line(row) is kt.KimiLineKind.CONTENT

    def test_answer_bullet_containing_a_sparkle_is_not_user_input(self):
        """A styled answer that quotes a sparkle must not be read as a submission."""

        row = "\x1b[38;5;253m● \x1b[39mthe sparkle ✨ is used for submissions"
        assert kt.classify_line(row) is kt.KimiLineKind.FINAL_BULLET

    def test_colour_222_bullet_is_not_user_input(self):
        """A response bullet wins even when it carries the user-input colour."""

        row = "\x1b[38;5;253m● \x1b[39mhand off, message, or delete anything."
        assert kt.classify_line(row) is kt.KimiLineKind.FINAL_BULLET
        assert kt.is_user_input_echo(row) is False

    def test_background_colour_222_is_not_user_input(self):
        """Only a *foreground* 222 counts; a background fill is not the echo."""

        row = "\x1b[48;5;222mhighlighted prose\x1b[0m"
        assert kt.is_user_input_echo(row) is False
        assert kt.classify_line(row) is kt.KimiLineKind.CONTENT

    def test_truecolor_foreground_is_not_user_input(self):
        row = "\x1b[38;2;222;222;222mgreyish prose\x1b[0m"
        assert kt.is_user_input_echo(row) is False

    def test_region_starts_after_the_wrapped_continuation(self):
        """The whole point of F1: the echo's continuation is outside the region."""

        pane = "\n".join([_D6_PROMPT_FIRST, _D6_PROMPT_CONT, "", _D6_FINAL_ROW])
        kinds = kt.classify_rows(pane.split("\n"), semantics=kt.SpinnerSemantics.CODE)
        assert kinds[:2] == [kt.KimiLineKind.USER_INPUT, kt.KimiLineKind.USER_INPUT]
        assert KimiCliProvider._locate_response_region(kinds) == (2, 4)


class TestD6McpToolRows:
    """D6-F2 — tool rows are recognised by shape, never by capitalisation.

    CAO's own MCP tools are snake_case. The pre-D6 clean-form rule required
    `Used [A-Z]...`, so every CAO MCP tool row fell through to the bullet branch
    and was published as the agent's answer.
    """

    TOOL_ROWS = (
        "● Used find_profiles · MCP/cao-mcp-server (kimi)",
        "● Using find_profiles · MCP/cao-mcp-server",
        "● Used send_message · MCP/cao-mcp-server",
        "● Used memory_recall · MCP/cao-mcp-server",
        "● Used load_skill · MCP/cao-mcp-server",
        "● Used Read (FILE) · 10 lines",
        "● Used Bash · 3 lines",
        "● Used Bash (ls -la) · 3 lines",
        "● Running a command · $ uname -a",
    )

    #: Ordinary prose that must remain answer text.
    PROSE_ROWS = (
        "● Used widely in production.",
        "● Using examples can help.",
        "● Used lower_case identifiers in this example.",
        "● Used Python extensively for this.",
        "● Used to be a problem, but no longer.",
    )

    @pytest.mark.parametrize("row", TOOL_ROWS)
    def test_tool_rows_are_tool_calls(self, row):
        kind = kt.classify_line(row, row)
        assert kind is kt.KimiLineKind.TOOL_CALL, f"{row!r} -> {kind}"
        assert kind not in kt.ANSWER_KINDS

    @pytest.mark.parametrize("row", PROSE_ROWS)
    def test_prose_stays_answer_content(self, row):
        kind = kt.classify_line(row, row)
        assert kind in kt.ANSWER_KINDS, f"{row!r} -> {kind}"

    def test_live_styled_mcp_row_is_a_tool_call(self):
        """The exact production row, styling included."""

        assert (
            kt.classify_line(_D6_TOOL_ROW, kt.strip_sgr(_D6_TOOL_ROW)) is kt.KimiLineKind.TOOL_CALL
        )

    def test_styled_mcp_row_with_reversed_sgr_order_is_a_tool_call(self):
        row = " \x1b[38;5;253m● \x1b[39mUsing \x1b[38;5;111m\x1b[1mfind_profiles\x1b[22m\x1b[39m\x1b[2m · MCP/cao-mcp-server\x1b[22m"
        assert kt.classify_line(row, kt.strip_sgr(row)) is kt.KimiLineKind.TOOL_CALL

    def test_snake_case_row_does_not_reach_the_answer(self):
        pane = "\n".join([_D6_PROMPT_FIRST, _D6_TOOL_ROW, _D6_FINAL_ROW, *_A3_COMPOSER, _A3_FOOTER])
        result = _code_provider("d6-f2").extract_last_message_from_script(pane)
        assert result == "● MCP-OK=1"


class TestD6ToolOutputBlock:
    """D6-F3 — sequence context, so a tool payload is not read as prose.

    A payload row is dim and indented; so is an indented continuation of a
    multi-line answer. The only thing that separates them is whether a
    positively identified tool header precedes them, which is why the state has
    to be sequence-level and can only start on a real `TOOL_CALL`.
    """

    HEADER = "● Used find_profiles · MCP/cao-mcp-server (kimi)"
    HEADER_2 = "● Using send_message · MCP/cao-mcp-server"
    PAYLOAD_1 = '[{"name":"a",'
    PAYLOAD_2 = ' "role":"developer"}]'
    FINAL = _answer("FINAL")

    def kinds(self, rows):
        return kt.classify_rows(list(rows), semantics=kt.SpinnerSemantics.CODE)

    def test_payload_block_is_chrome_until_the_answer(self):
        rows = [self.HEADER, self.PAYLOAD_1, self.PAYLOAD_2, self.FINAL]
        assert self.kinds(rows) == [
            kt.KimiLineKind.TOOL_CALL,
            kt.KimiLineKind.TOOL_CHROME,
            kt.KimiLineKind.TOOL_CHROME,
            kt.KimiLineKind.FINAL_BULLET,
        ]

    def test_blank_rows_do_not_end_the_block(self):
        rows = [self.HEADER, self.PAYLOAD_1, "", self.PAYLOAD_2, "", self.FINAL]
        kinds = self.kinds(rows)
        assert kinds[1] is kt.KimiLineKind.TOOL_CHROME
        assert kinds[3] is kt.KimiLineKind.TOOL_CHROME
        assert kinds[4] is kt.KimiLineKind.BLANK
        assert kinds[5] is kt.KimiLineKind.FINAL_BULLET

    def test_second_tool_call_keeps_the_block_open(self):
        rows = [self.HEADER, self.PAYLOAD_1, self.HEADER_2, self.PAYLOAD_2, self.FINAL]
        assert self.kinds(rows) == [
            kt.KimiLineKind.TOOL_CALL,
            kt.KimiLineKind.TOOL_CHROME,
            kt.KimiLineKind.TOOL_CALL,
            kt.KimiLineKind.TOOL_CHROME,
            kt.KimiLineKind.FINAL_BULLET,
        ]

    def test_collapsed_output_row_inside_a_block_is_chrome(self):
        rows = [self.HEADER, "  … (3 more lines, ctrl+o to expand)", self.FINAL]
        kinds = self.kinds(rows)
        assert kinds[1] is kt.KimiLineKind.TOOL_CHROME
        assert kinds[2] is kt.KimiLineKind.FINAL_BULLET

    def test_reasoning_inside_a_tool_block_stays_private(self):
        """An open block owns its rows: a reasoning candidate does not release it.

        Reasoning styling is a shape tool output can also carry, so a
        grey-bullet row inside an open tool block is payload, not evidence that
        the block ended. Only a positively evidenced public answer — the
        renderer's colour-253 ``●``, here ``self.FINAL`` — ends the block.
        """

        thinking = "\x1b[38;5;244m● \x1b[3mlet me think about it\x1b[0m"
        rows = [self.HEADER, self.PAYLOAD_1, thinking, self.FINAL]
        kinds = self.kinds(rows)
        assert kinds[1] is kt.KimiLineKind.TOOL_CHROME
        assert kinds[2] is kt.KimiLineKind.TOOL_CHROME
        assert kinds[2] not in kt.ANSWER_KINDS
        assert kinds[3] is kt.KimiLineKind.FINAL_BULLET

    def test_user_echo_ends_the_block(self):
        """A new *submission* ends the block: the sparkle row, not a bare
        colour-222 row (colour is continuation evidence only)."""

        rows = [self.HEADER, self.PAYLOAD_1, _D6_PROMPT_FIRST, self.FINAL]
        kinds = self.kinds(rows)
        assert kinds[1] is kt.KimiLineKind.TOOL_CHROME
        assert kinds[2] is kt.KimiLineKind.USER_INPUT

    def test_standalone_colour_222_row_does_not_start_a_submission(self):
        """The reviewed collision: colour 222 inside an answer is not an echo."""

        rows = [self.HEADER, self.PAYLOAD_1, _D6_PROMPT_CONT, self.FINAL]
        kinds = self.kinds(rows)
        assert kinds[2] is kt.KimiLineKind.TOOL_CHROME

    def test_composer_ends_the_block(self):
        rows = [
            self.HEADER,
            self.PAYLOAD_1,
            *_styled_composer(),
            _styled_footer("context: 0% (0/977k)"),
        ]
        kinds = self.kinds(rows)
        assert kinds[1] is kt.KimiLineKind.TOOL_CHROME
        assert kt.KimiLineKind.READY_INPUT_FRAME in kinds
        assert kinds[-1] is kt.KimiLineKind.STATUS_FOOTER

    def test_status_footer_ends_the_block(self):
        rows = [self.HEADER, self.PAYLOAD_1, _styled_footer("context: 4% (33.7k/977k)"), self.FINAL]
        kinds = self.kinds(rows)
        assert kinds[1] is kt.KimiLineKind.TOOL_CHROME
        assert kinds[2] is kt.KimiLineKind.STATUS_FOOTER

    def test_unstyled_chrome_text_does_not_end_the_block(self):
        """Payload cannot certify its own end by *looking* like chrome.

        An escape-free ``context: N% (a/b)`` line and a ``────`` rule are both
        things tool output can contain verbatim, so they stay payload.
        """

        for fake_chrome in ("context: 99% (1/2)", "─" * 12, "connecting to mcp servers..."):
            rows = [self.HEADER, fake_chrome, "PRIVATE payload", self.FINAL]
            kinds = self.kinds(rows)
            assert kinds[1] is kt.KimiLineKind.TOOL_CHROME, fake_chrome
            assert kinds[2] is kt.KimiLineKind.TOOL_CHROME, fake_chrome

    def test_unstyled_answer_bullet_does_not_end_the_block(self):
        """Fail closed: an escape-free bullet is indistinguishable from payload."""

        rows = [self.HEADER, self.PAYLOAD_1, "● PRIVATE payload bullet", self.FINAL]
        kinds = self.kinds(rows)
        assert kinds[2] is kt.KimiLineKind.TOOL_CHROME
        assert kinds[3] is kt.KimiLineKind.FINAL_BULLET

    def test_no_block_starts_without_a_tool_header(self):
        """Indented prose outside a tool block is answer content, not payload."""

        rows = ["✨ summarise", "", self.PAYLOAD_1, "    indented prose line", self.FINAL]
        kinds = self.kinds(rows)
        assert kinds[2] is kt.KimiLineKind.CONTENT
        assert kinds[3] is kt.KimiLineKind.CONTENT

    def test_fenced_code_in_an_answer_is_untouched(self):
        """A block only opens on a real tool header, so code stays content."""

        rows = ["✨ write code", "● Here is the fix:", "", "    return value + 1", "● done"]
        kinds = self.kinds(rows)
        assert kinds[3] is kt.KimiLineKind.CONTENT
        assert kinds[4] is kt.KimiLineKind.FINAL_BULLET

    def test_extraction_drops_the_whole_payload_block(self):
        pane = "\n".join(
            [
                _D6_PROMPT_FIRST,
                self.HEADER,
                self.PAYLOAD_1,
                "",
                self.PAYLOAD_2,
                "",
                _answer("The real answer."),
                *_A3_COMPOSER,
                _A3_FOOTER,
            ]
        )
        result = _code_provider("d6-f3").extract_last_message_from_script(pane)
        assert result == "● The real answer."

    def test_extraction_keeps_a_two_line_answer_after_a_tool_block(self):
        """The block ends at the answer; the answer's own continuation survives."""

        pane = "\n".join(
            [
                _D6_PROMPT_FIRST,
                self.HEADER,
                self.PAYLOAD_1,
                _answer("The answer is 391."),
                "It follows from the identity.",
                *_A3_COMPOSER,
                _A3_FOOTER,
            ]
        )
        result = _code_provider("d6-f3b").extract_last_message_from_script(pane)
        assert result == "● The answer is 391.\nIt follows from the identity."

    def test_pre_tool_assistant_chatter_is_not_the_final_response(self):
        """mode=LAST publishes the answer after the final tool, not pre-tool chatter."""

        pane = "\n".join(
            [
                _D6_PROMPT_FIRST,
                _answer("I'll call the tool exactly as specified."),
                self.HEADER,
                self.PAYLOAD_1,
                _answer("MCP-OK=2"),
                *_A3_COMPOSER,
                _styled_footer("context: 4% (33.4k/977k)"),
            ]
        )
        result = _code_provider("d6-final-segment").extract_last_message_from_script(pane)
        assert result == "● MCP-OK=2"

    def test_no_tool_turn_keeps_multiple_answer_bullets(self):
        """Tool segmentation must not collapse an ordinary multi-bullet answer."""

        pane = "\n".join(
            [
                _D6_PROMPT_FIRST,
                _answer("First point."),
                _answer("Second point."),
                *_A3_COMPOSER,
                _styled_footer("context: 4% (33.4k/977k)"),
            ]
        )
        result = _code_provider("d6-no-tool-multi").extract_last_message_from_script(pane)
        assert result == "● First point.\n● Second point."


class TestD6ProductionExtraction:
    """The D6 production failure, frozen as a regression.

    ``kimi_code_0431_10_mcp_tool_turn.txt`` is a scrubbed ``tmux capture-pane -p
    -e`` rendering of the live failing turn: wrapped styled user message,
    completed snake_case MCP tool row, dim indented JSON payload, the real
    answer, composer, footer.
    """

    FIXTURE = "kimi_code_0431_10_mcp_tool_turn.txt"

    def test_production_fixture_extracts_only_the_answer(self):
        result = _code_provider("d6-prod").extract_last_message_from_script(_fixture(self.FIXTURE))
        assert result == "● MCP-OK=1"

    def test_production_fixture_negative_assertions(self):
        result = _code_provider("d6-prod-neg").extract_last_message_from_script(
            _fixture(self.FIXTURE)
        )
        assert "hand off, message, or delete anything." not in result
        assert "Used find_profiles" not in result
        assert "find_profiles" not in result
        assert "kimi-installed-deploy-smoke" not in result
        assert "d6-probe" not in result
        assert "[{" not in result
        assert "context:" not in result
        assert "Never Ask" not in result
        assert "more lines" not in result

    def test_production_fixture_status_is_completed(self):
        provider = _code_provider("d6-prod-status")
        assert provider.get_status(_fixture(self.FIXTURE)) is TerminalStatus.COMPLETED

    def test_production_fixture_has_no_thinking_or_chrome_kinds_in_the_answer(self):
        provider = _code_provider("d6-prod-kinds")
        pane = _fixture(self.FIXTURE)
        raw_lines = pane.split("\n")
        clean_lines = [kt.strip_sgr(row) for row in raw_lines]
        kinds = kt.classify_rows(raw_lines, clean_lines, kt.SpinnerSemantics.CODE)
        start, end = provider._locate_response_region(kinds)
        region = [kind for kind in kinds[start:end] if kind is not kt.KimiLineKind.BLANK]
        assert region == [
            kt.KimiLineKind.TOOL_CALL,
            kt.KimiLineKind.TOOL_CHROME,
            kt.KimiLineKind.FINAL_BULLET,
        ]

    def test_independent_review_reproduction(self):
        """The minimal reproduction from the independent review, verbatim.

        Before the fix this returned all four unwanted rows.
        """

        pane = "\n".join(
            [
                "✨ Use the CAO MCP tool find_profiles exactly once. Do not create, assign,",
                "hand off, message, or delete anything.",
                "",
                "● Used find_profiles · MCP/cao-mcp-server (kimi)",
                '[{"name":"kimi-installed-deploy-smoke"}] …',
                "",
                _answer("MCP-OK=1"),
                *_A3_COMPOSER,
                _A3_FOOTER,
            ]
        )
        result = _code_provider("d6-repro").extract_last_message_from_script(pane)
        assert result == "● MCP-OK=1"

    def test_reproduction_negative_assertions(self):
        pane = "\n".join(
            [
                "✨ Use the CAO MCP tool find_profiles exactly once. Do not create, assign,",
                "hand off, message, or delete anything.",
                "",
                "● Used find_profiles · MCP/cao-mcp-server (kimi)",
                '[{"name":"kimi-installed-deploy-smoke"}] …',
                "",
                _answer("MCP-OK=1"),
                *_A3_COMPOSER,
                _A3_FOOTER,
            ]
        )
        result = _code_provider("d6-repro-neg").extract_last_message_from_script(pane)
        assert "hand off, message, or delete anything." not in result
        assert "Used find_profiles" not in result
        assert "kimi-installed-deploy-smoke" not in result

    def test_unstyled_reproduction_also_clean(self):
        """The same shape with no styling at all must also extract cleanly.

        The escape-free path is what the screen/status consumers see, and it is
        the path that has no colour to lean on.
        """

        pane = "\n".join(
            [
                "✨ Use the CAO MCP tool find_profiles exactly once. Do not create, assign,",
                "hand off, message, or delete anything.",
                "",
                "● Used find_profiles · MCP/cao-mcp-server (kimi)",
                '[{"name":"kimi-installed-deploy-smoke"}] …',
                "",
                _answer("MCP-OK=1"),
                *_A3_COMPOSER,
                _A3_FOOTER,
            ]
        )
        result = _code_provider("d6-repro-plain").extract_last_message_from_script(pane)
        assert result == "● MCP-OK=1"


class TestD6SequenceClassifierIsShared:
    """D6-F4 — one semantic source of truth for the tool-block state machine.

    The extractor must consume `classify_rows`/`classify_lines`, not re-derive
    context per row: a payload row carries no marker of its own, so a per-row
    extractor cannot recognise it.
    """

    def test_classify_lines_returns_contextual_kinds(self):
        pane = "\n".join(
            [
                "✨ go",
                "● Used find_profiles · MCP/cao-mcp-server",
                '[{"a":1}]',
                _answer("answer"),
            ]
        )
        kinds = [kind for _, _, kind in kt.classify_lines(pane, kt.SpinnerSemantics.CODE)]
        assert kinds[2] is kt.KimiLineKind.TOOL_CHROME
        assert kinds[3] is kt.KimiLineKind.FINAL_BULLET

    def test_extractor_kinds_match_the_shared_classifier(self):
        """The extractor's region kinds equal a slice of the shared result."""

        provider = _code_provider("d6-shared")
        pane = _fixture(TestD6ProductionExtraction.FIXTURE)
        raw_lines = pane.split("\n")
        clean_lines = [kt.strip_sgr(row) for row in raw_lines]
        shared = kt.classify_rows(raw_lines, clean_lines, kt.SpinnerSemantics.CODE)
        start, end = provider._locate_response_region(shared)
        _, region_kinds = provider._classify_response_region(
            raw_lines, clean_lines, start, end, shared
        )
        expected = [kind for kind in shared[start:end] if kind is not kt.KimiLineKind.BLANK]
        assert region_kinds == expected

    def test_classify_row_is_still_available_for_single_row_callers(self):
        """`classify_line` keeps its context-free contract for other consumers."""

        assert (
            kt.classify_line("● Used find_profiles · MCP/cao-mcp-server")
            is kt.KimiLineKind.TOOL_CALL
        )
        assert kt.classify_line('[{"a":1}]') is kt.KimiLineKind.CONTENT


class TestD6RealSourceSideTurn:
    """D6 — the shape a real source-side Kimi turn produced end to end.

    Captured from an isolated source-side cao-server driving real Kimi Code
    0.43.1 through the CAO MCP server (`find_profiles`). Two measured rows here
    are not in the production rollback capture and are pinned deliberately:

    * the MCP result arrives wrapped in a multi-line ``<mcp-result-extras>``
      block, so the payload spans several rows rather than one JSON line;
    * Kimi renders an intermediate reasoning bullet (*"Zero profiles returned.
      Reply one line."*) between the payload and the answer. An open tool block
      owns its rows until a positively evidenced public answer arrives, so that
      reasoning candidate is payload (``TOOL_CHROME``): it neither ends the
      block nor reaches the answer.

    The answer row carries the measured colour-253 styling (``_answer``), the
    same as the real capture, so the block ends on renderer evidence rather
    than on an unstyled bullet.
    """

    def _pane(self):
        return "\n".join(
            [
                '✨ Use the CAO MCP tool `find_profiles` exactly once with query "kimi" and limit 3.',
                "hand off, message, or delete anything.",
                "",
                "● Used find_profiles · MCP/cao-mcp-server (kimi)",
                "<mcp-result-extras>",
                '{"structuredContent":{"result":[]},"_meta":{"fastmcp":{"wrap_result":true}}}',
                "</mcp-result-extras>",
                "\x1b[38;5;244m● \x1b[3mZero profiles returned. Reply one line.\x1b[0m",
                _answer("MCP-OK=0"),
                *_A3_COMPOSER,
                _A3_FOOTER,
            ]
        )

    def test_extraction_returns_only_the_answer(self):
        result = _code_provider("d6-real").extract_last_message_from_script(self._pane())
        assert result == "● MCP-OK=0"

    def test_payload_block_and_reasoning_are_absent(self):
        result = _code_provider("d6-real-neg").extract_last_message_from_script(self._pane())
        assert "mcp-result-extras" not in result
        assert "structuredContent" not in result
        assert "Zero profiles returned" not in result
        assert "Used find_profiles" not in result
        assert "hand off, message, or delete anything." not in result

    def test_payload_and_reasoning_are_classified_as_non_answer(self):
        """The intermediate reasoning is private payload, not a reasoning kind.

        An open tool block owns its rows until a positively evidenced public
        answer arrives, so the grey-bullet reasoning row between the payload and
        the answer is ``TOOL_CHROME`` — non-answer, and never released as text.
        Only the renderer's colour-253 answer bullet ends the block.
        """

        pane = self._pane()
        raw_lines = pane.split("\n")
        clean_lines = [kt.strip_sgr(row) for row in raw_lines]
        kinds = kt.classify_rows(raw_lines, clean_lines, kt.SpinnerSemantics.CODE)
        assert kinds[4] is kt.KimiLineKind.TOOL_CHROME
        assert kinds[5] is kt.KimiLineKind.TOOL_CHROME
        assert kinds[6] is kt.KimiLineKind.TOOL_CHROME
        assert kinds[7] is kt.KimiLineKind.TOOL_CHROME
        assert kinds[7] not in kt.ANSWER_KINDS
        assert kinds[8] is kt.KimiLineKind.FINAL_BULLET
        for kind in kinds:
            assert kind not in (kt.KimiLineKind.TOOL_CALL, kt.KimiLineKind.TOOL_CHROME) or (
                kind in kt.CHROME_KINDS
            )

    def test_echo_continuation_and_tool_block_are_both_absent(self):
        """Both D6 mechanisms in one pane, asserting on the classified region.

        The region is the tool block plus the answer: the wrapped echo is
        outside it, and the intermediate reasoning row is one more payload row
        inside it.
        """

        pane = self._pane()
        raw_lines = pane.split("\n")
        clean_lines = [kt.strip_sgr(row) for row in raw_lines]
        kinds = kt.classify_rows(raw_lines, clean_lines, kt.SpinnerSemantics.CODE)
        start, end = KimiCliProvider._locate_response_region(kinds)
        region = [k for k in kinds[start:end] if k is not kt.KimiLineKind.BLANK]
        assert kt.KimiLineKind.USER_INPUT not in region
        assert kt.KimiLineKind.THINKING_BULLET not in region
        assert region == [
            kt.KimiLineKind.TOOL_CALL,
            kt.KimiLineKind.TOOL_CHROME,
            kt.KimiLineKind.TOOL_CHROME,
            kt.KimiLineKind.TOOL_CHROME,
            kt.KimiLineKind.TOOL_CHROME,
            kt.KimiLineKind.FINAL_BULLET,
        ]


class TestD6SourceSideRealCapture:
    """The real source-side capture, frozen as a fixture.

    ``kimi_code_0431_11_mcp_tool_turn_source_e2e.txt`` is a scrubbed
    ``tmux capture-pane -p -e`` dump taken from an isolated source-side
    cao-server driving real Kimi Code 0.43.1 through the CAO MCP server. It is
    the rendered history the ``mode=last`` path actually consumes — launch-shell
    echo, probe command, boot chrome, the wrapped submitted message, the MCP
    tool block, an intermediate reasoning bullet, the answer, composer, footer.
    """

    FIXTURE = "kimi_code_0431_11_mcp_tool_turn_source_e2e.txt"

    def test_extraction_yields_only_the_answer(self):
        result = _code_provider("d6-src").extract_last_message_from_script(_fixture(self.FIXTURE))
        assert result == "● MCP-OK=<N>"

    def test_no_leak_from_the_real_capture(self):
        result = _code_provider("d6-src-neg").extract_last_message_from_script(
            _fixture(self.FIXTURE)
        )
        assert "hand off, message, or delete anything." not in result
        assert "Used find_profiles" not in result
        assert "mcp-result-extras" not in result
        assert "structuredContent" not in result
        assert "Zero profiles returned" not in result
        assert "context:" not in result
        assert "Never Ask" not in result
        assert "CAO_KIMI_BIN" not in result
        assert "KIMI_CODE_HOME" not in result

    def test_launch_command_in_the_capture_carries_auto_and_a_private_home(self):
        """The capture is also evidence the frozen launch contract still holds."""

        cleaned = kt.strip_sgr(_fixture(self.FIXTURE))
        assert "--auto" in cleaned
        assert "--yolo" not in cleaned
        assert "KIMI_CODE_HOME=<CAOTMP>/kimi-home" in cleaned
        assert "CAO_TERMINAL_ID=<TID>" in cleaned

    def test_status_on_the_real_capture_is_completed(self):
        provider = _code_provider("d6-src-status")
        assert provider.get_status(_fixture(self.FIXTURE)) is TerminalStatus.COMPLETED

    def test_answer_region_contains_no_answer_kind_other_than_the_answer(self):
        """The located region holds the answer and no other answer kind.

        The capture is the real rendering, so its answer bullet already carries
        the renderer's colour-253 styling: the block ends on renderer evidence,
        not on an unstyled bullet that payload could also produce. The
        intermediate reasoning row inside the block is therefore payload
        (``TOOL_CHROME``) — private, and not an answer kind.
        """

        provider = _code_provider("d6-src-region")
        pane = _fixture(self.FIXTURE)
        assert "\x1b[38;5;253m●" in pane
        raw_lines = pane.split("\n")
        clean_lines = [kt.strip_sgr(row) for row in raw_lines]
        kinds = kt.classify_rows(raw_lines, clean_lines, kt.SpinnerSemantics.CODE)
        start, end = provider._locate_response_region(kinds)
        answers = [
            clean_lines[i].strip()
            for i in range(start, end)
            if kinds[i] in kt.ANSWER_KINDS and clean_lines[i].strip()
        ]
        assert answers == ["● MCP-OK=<N>"]
        answer_kinds = [kind for kind in kinds[start:end] if kind in kt.ANSWER_KINDS]
        assert answer_kinds == [kt.KimiLineKind.FINAL_BULLET]
        assert kt.KimiLineKind.TOOL_CHROME in kinds[start:end]
        assert kt.KimiLineKind.THINKING_BULLET not in kinds[start:end]


class TestD6ReasoningBeforeToolCall:
    """The other real ordering: reasoning first, then the tool block.

    Captured from a second isolated source-side run whose worker reasoned before
    calling the tool. The reasoning bullet must not open a tool block, must not
    end one, and must not reach the answer — and the payload that follows the
    header must still be treated as payload. The ``find_profiles`` call returned
    a non-empty profile list here, so the payload rows are real metadata rather
    than an empty result.
    """

    FIXTURE = "kimi_code_0431_12_mcp_tool_turn_reasoning_first.txt"

    def test_extraction_yields_only_the_answer(self):
        result = _code_provider("d6-reason").extract_last_message_from_script(
            _fixture(self.FIXTURE)
        )
        assert result == "● MCP-OK=<N>"

    def test_reasoning_and_payload_do_not_leak(self):
        result = _code_provider("d6-reason-neg").extract_last_message_from_script(
            _fixture(self.FIXTURE)
        )
        assert "hand off, message, or delete anything." not in result
        assert "I need to call find_profiles" not in result
        assert "Used find_profiles" not in result
        assert "d6-kimi-second" not in result
        assert "find_profiles returns a list" not in result
        assert "context:" not in result
        assert "Never Ask" not in result

    def test_kind_sequence_is_reasoning_then_block_then_answer(self):
        pane = _fixture(self.FIXTURE)
        raw_lines = pane.split("\n")
        clean_lines = [kt.strip_sgr(row) for row in raw_lines]
        kinds = kt.classify_rows(raw_lines, clean_lines, kt.SpinnerSemantics.CODE)
        start, end = KimiCliProvider._locate_response_region(kinds)
        region = [k for k in kinds[start:end] if k is not kt.KimiLineKind.BLANK]
        assert region == [
            kt.KimiLineKind.THINKING_BULLET,
            kt.KimiLineKind.TOOL_CALL,
            kt.KimiLineKind.TOOL_CHROME,
            kt.KimiLineKind.FINAL_BULLET,
        ]

    def test_a_reasoning_bullet_does_not_absorb_the_following_payload(self):
        """Reasoning must not be mistaken for a block that eats the tool rows."""

        rows = [
            "\x1b[38;5;244m● \x1b[3mI need to call find_profiles.\x1b[0m",
            "● Used find_profiles · MCP/cao-mcp-server (kimi)",
            '[{"name":"a"}]',
            _answer("MCP-OK=2"),
        ]
        assert kt.classify_rows(rows, semantics=kt.SpinnerSemantics.CODE) == [
            kt.KimiLineKind.THINKING_BULLET,
            kt.KimiLineKind.TOOL_CALL,
            kt.KimiLineKind.TOOL_CHROME,
            kt.KimiLineKind.FINAL_BULLET,
        ]


# =============================================================================
# PR #799 — upstream P2 review findings
#
# One class per reviewed finding, each carrying the regression that failed
# against the pre-fix candidate. The shared `P2Review` prefix makes the family
# selectable on its own:
#
#     pytest -q test/providers/test_kimi_code_compat.py -k P2Review
# =============================================================================


def _extract_last_message(script_output: str, terminal_id: str = "t-p2-review") -> str:
    """Drive the real extraction entry point (the one terminal_service calls)."""

    provider = KimiCliProvider(terminal_id, "session-1", "window-1")
    return provider.extract_last_message_from_script(script_output)


def _without_sgr(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


class TestP2ReviewProbeShellBoundary:
    """P2-1 / #14 — the probe must reach the pane as shell-neutral tokens.

    ``_probe_kimi_environment`` types its command straight into the pane, so it
    is parsed by whatever shell the pane runs. ``fish`` rejects
    ``${VAR:-default}`` outright ("${ is not a valid variable"), so a fish pane
    never wrote the completion marker and a working Kimi Code binary was
    classified UNKNOWN. Moving the program into ``/bin/sh`` fixed that but not
    the *transport*: POSIX quoting is not fish quoting, and a probe path
    containing a backslash-before-apostrophe made ``shlex.quote`` produce a
    string fish rejects ("Unexpected end of string", exit 127).

    The command is therefore built only from :data:`SHELL_SAFE_CHARS`, with the
    POSIX program in a CAO-owned script and the probe file passed as a positional
    parameter. There is nothing for any shell to quote, split or expand.
    """

    @pytest.fixture
    def probe_command_for(self, tmp_path, monkeypatch):
        """Drive the real probe and return the exact command typed into the pane.

        Returned as a factory so a test can choose the temp directory the
        provider is pointed at.
        """

        def _run(temp_dir: Path) -> str:
            provider = KimiCliProvider("term-probe", "session-1", "window-1")
            provider._temp_dir = str(temp_dir)
            # The provider may relocate transport artifacts to a safe directory
            # when the supplied temp dir has shell-hostile characters.
            probe_path = Path(provider._ensure_shell_safe_dir()) / "kimi-probe.txt"
            captured: Dict[str, str] = {}

            def fake_send_keys(session_name, window_name, keys):
                captured["command"] = keys
                probe_path.write_text(
                    "CAO_KIMI_BIN=/usr/bin/kimi\n"
                    "CAO_KIMI_HOME=/home/u/.kimi-code\n" + KIMI_CODE_HELP + "\n"
                    "CAO-KIMI-PROBE-END\n",
                    encoding="utf-8",
                )

            backend = MagicMock()
            backend.send_keys.side_effect = fake_send_keys
            monkeypatch.setattr(kimi_cli_module, "get_backend", lambda: backend)

            probe = asyncio.run(provider._probe_kimi_environment())
            assert probe.dialect is KimiDialect.CODE
            return captured["command"]

        return _run

    def test_posix_syntax_lives_inside_the_compatible_shell_script(
        self, probe_command_for, tmp_path
    ):
        """Every token the pane parses must be shell-safe by construction."""

        command = probe_command_for(tmp_path)
        argv = shlex.split(command)

        # One argv-level call into an explicitly chosen POSIX shell, naming a
        # script and its probe file.
        assert argv[0] == kimi_cli_module.KIMI_COMPATIBLE_SHELL
        assert len(argv) == 3
        script_path, probe_path = argv[1], argv[2]
        assert Path(script_path).name == "kimi-probe.sh"
        assert Path(probe_path) == Path(script_path).with_name("kimi-probe.txt")
        assert Path(script_path).parent != tmp_path

        # Nothing in the typed command needs quoting in *any* shell: no
        # expansion, no substitution, no quoting metacharacter at all.
        for token in argv:
            assert kimi_cli_module.is_shell_safe_token(token), token
        assert "${" not in command
        assert "$(" not in command
        assert "'" not in command
        assert "\\" not in command

        # The POSIX program lives in the script, which only /bin/sh ever reads.
        body = Path(script_path).read_text(encoding="utf-8")
        assert "${KIMI_CODE_HOME:-$HOME/.kimi-code}" in body
        assert "$(command -v kimi 2>/dev/null)" in body
        assert "kimi --help" in body
        assert KIMI_PROBE_END_MARKER in body
        assert command.count("${KIMI_CODE_HOME:-$HOME/.kimi-code}") == 0

    def test_outer_command_parses_under_a_non_posix_pane_shell(self, probe_command_for, tmp_path):
        """Live check when ``fish`` is installed (4.x rejects ``${var:-x}``)."""

        fish = shutil.which("fish")
        if fish is None:
            pytest.skip("fish is not installed")

        command = probe_command_for(tmp_path)
        result = subprocess.run(
            [fish, "--no-config", "-c", command],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        )
        assert result.returncode == 0, result.stderr

        written = Path(shlex.split(command)[2]).read_text(encoding="utf-8")
        assert written.startswith("CAO_KIMI_BIN=")
        assert "CAO_KIMI_HOME=" in written
        assert KIMI_PROBE_END_MARKER in written

    def test_inner_shell_sees_the_pane_environment(self, probe_command_for, tmp_path):
        """PATH / HOME / KIMI_CODE_HOME must be the pane's, not cao-server's."""

        fish = shutil.which("fish")
        if fish is None:
            pytest.skip("fish is not installed")

        pane_bin = tmp_path / "pane-bin"
        pane_bin.mkdir()
        pane_kimi = pane_bin / "kimi"
        pane_kimi.write_text("#!/bin/sh\necho 'Usage: kimi'\n", encoding="utf-8")
        pane_kimi.chmod(0o755)

        command = probe_command_for(tmp_path)
        result = subprocess.run(
            [fish, "--no-config", "-c", command],
            capture_output=True,
            text=True,
            env={
                "PATH": f"{pane_bin}:/usr/bin:/bin",
                "HOME": str(tmp_path),
                "KIMI_CODE_HOME": "/tmp/pane home",
            },
        )
        assert result.returncode == 0, result.stderr

        written = Path(shlex.split(command)[2]).read_text(encoding="utf-8")
        # The binary the inner shell resolves is the pane's, and the resolved
        # absolute path is what the launch reuses.
        assert f"CAO_KIMI_BIN={pane_kimi}\n" in written
        assert "CAO_KIMI_HOME=/tmp/pane home\n" in written
        assert "Usage: kimi" in written

    @pytest.mark.parametrize(
        "hostile_name",
        [
            "sp ace",
            "apo'strophe",
            "back\\slash",
            "back\\'quote",
            "multi\\\\backslash",
            "dol$lar;tick`mark",
            'dq"uote',
            "par(en)s[brack]ets",
            "uni\u00e9\u4e2d",
        ],
    )
    def test_hostile_temp_dir_never_reaches_the_pane(
        self, probe_command_for, tmp_path, hostile_name
    ):
        """A shell-hostile scratch path must not become a typed token."""

        hostile = tmp_path / hostile_name
        hostile.mkdir()

        command = probe_command_for(hostile)
        argv = shlex.split(command)

        assert str(hostile) not in command
        for token in argv:
            assert kimi_cli_module.is_shell_safe_token(token), token
        # The relocated artifacts are real and reachable.
        probe_path = Path(argv[2])
        assert probe_path.parent.parent.name.startswith("cao_kimi_")
        assert KIMI_PROBE_END_MARKER in probe_path.read_text(encoding="utf-8")

    def test_hostile_temp_dir_round_trips_under_fish(self, probe_command_for, tmp_path):
        """The relocated command must execute under a real fish."""

        fish = shutil.which("fish")
        if fish is None:
            pytest.skip("fish is not installed")

        hostile = tmp_path / "back\\'quote"
        hostile.mkdir()
        command = probe_command_for(hostile)

        result = subprocess.run(
            [fish, "--no-config", "-c", command],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        )
        assert result.returncode == 0, result.stderr
        probe_path = Path(shlex.split(command)[2])
        assert KIMI_PROBE_END_MARKER in probe_path.read_text(encoding="utf-8")


class TestP2ReviewProseIsNotAToolCall:
    """P2-2 — ordinary answer prose must not be read as a tool header.

    ``classify_rows`` opens a tool block on ``TOOL_CALL``, so a misclassified
    prose bullet did not merely lose one row: every following row was folded
    into the block as ``TOOL_CHROME`` and the answer was destroyed. The
    escape-free regex keyed on the English verb prefixes alone, so
    "Calling this function twice returns two rows." and
    "Running a command is unnecessary here." both matched.
    """

    @pytest.mark.parametrize(
        "row",
        [
            "• Calling this function twice returns two rows.",
            "• Running a command is unnecessary here.",
            "● Calling this function twice returns two rows.",
            "● Running a command is unnecessary here.",
        ],
    )
    def test_prose_bullets_remain_answer_content(self, row):
        assert kt.classify_line(row) is kt.KimiLineKind.FINAL_BULLET
        assert kt.KimiLineKind.FINAL_BULLET in kt.ANSWER_KINDS

    @pytest.mark.parametrize(
        "row",
        [
            "● Running a command · $ uname -a",
            "● Used Read (ANSWER_SPEC.md) · 10 lines",
            "● Used find_profiles · MCP/cao-mcp-server (kimi)",
            "● Using find_profiles · MCP/cao-mcp-server",
        ],
    )
    def test_measured_tool_headers_stay_tool_calls(self, row):
        assert kt.classify_line(row) is kt.KimiLineKind.TOOL_CALL
        assert kt.KimiLineKind.TOOL_CALL not in kt.ANSWER_KINDS

    def test_whole_prose_answer_survives_extraction(self):
        """The reviewed mechanism: the block swallowed the continuation rows."""

        script = "\n".join(
            [
                "💫 Explain the call syntax.",
                "• Calling this function twice returns two rows.",
                "Running a command is unnecessary here.",
            ]
        )
        assert _extract_last_message(script) == (
            "• Calling this function twice returns two rows.\n"
            "Running a command is unnecessary here."
        )

    def test_tool_plumbing_is_still_removed_by_extraction(self):
        """The D6 filter must not be weakened by the tighter prose rule."""

        script = "\n".join(
            [
                "💫 Run uname.",
                "● Running a command · $ uname -a",
                "Linux host 6.1.0 x86_64 GNU/Linux",
                _answer("Checked: uname -a output above."),
            ]
        )
        result = _extract_last_message(script)
        assert result == "● Checked: uname -a output above."
        assert "Running a command" not in result
        assert "GNU/Linux" not in result


class TestP2ReviewQuotedTrustMarker:
    """P2-3 — a quoted ``❯`` must not end the response region.

    ``classify_line`` returned ``TRUST_DIALOG`` for any row *containing* the
    marker, and ``TRUST_DIALOG`` is a response-end anchor, so an answer that
    merely printed the glyph was silently truncated at that row.
    """

    def test_quoted_marker_row_is_not_a_trust_dialog(self):
        row = "• $ printf '❯'"
        assert kt.classify_line(row) is not kt.KimiLineKind.TRUST_DIALOG
        assert kt.classify_line(row) is kt.KimiLineKind.FINAL_BULLET

    def test_extraction_is_not_truncated_by_a_quoted_marker(self):
        """The reviewed case: content after the quoted marker must survive."""

        script = "\n".join(
            [
                "💫 Show me the marker.",
                "",
                "intro",
                "$ printf '❯'",
                "Done.",
                "",
            ]
        )
        assert _extract_last_message(script) == "intro\n$ printf '❯'\nDone."

    @pytest.mark.parametrize(
        "row",
        [
            "❯ Trust this folder",
            "    ❯ Trust this folder",
            "❯ Don't trust",
            "         Don't trust",
        ],
    )
    def test_real_trust_option_rows_still_classify_as_dialog(self, row):
        assert kt.classify_line(row) is kt.KimiLineKind.TRUST_DIALOG

    def test_measured_trust_dialogs_are_still_detected(self):
        for name in (
            "kimi_code_0431_06_workspace_trust_dialog_gated_mcp.txt",
            "kimi_code_0431_07_workspace_trust_dialog_plain.txt",
        ):
            dialog = kt.detect_trust_dialog(_fixture(name).split("\n"))
            assert dialog is not None, name
            assert dialog.options == [kt.TRUST_OPTION_TRUST, kt.TRUST_OPTION_REJECT], name
            assert dialog.selected_option == kt.TRUST_OPTION_TRUST, name


class TestP2ReviewLegacyIdlePromptBoundary:
    """P2-4 — the legacy bare ``✨`` / ``💫`` idle prompt must end the answer.

    The classifier had no row kind for the bare form, so the terminal's own
    idle prompt was emitted as the last line of the extracted answer.
    """

    @pytest.mark.parametrize("marker", ["💫", "✨"])
    def test_final_bare_prompt_is_not_part_of_the_answer(self, marker):
        script = "\n".join([f"{marker} What is two plus two?", "• Four.", marker, ""])
        assert _extract_last_message(script) == "• Four."

    @pytest.mark.parametrize("marker", ["💫", "✨"])
    def test_bare_prompt_row_is_ready_chrome(self, marker):
        assert kt.classify_line(marker) is kt.KimiLineKind.READY_INPUT_FRAME
        assert kt.KimiLineKind.READY_INPUT_FRAME in kt.CHROME_KINDS
        assert kt.KimiLineKind.READY_INPUT_FRAME not in kt.ANSWER_KINDS

    @pytest.mark.parametrize("marker", ["💫", "✨"])
    def test_bare_prompt_is_scoped_to_the_legacy_dialect(self, marker):
        """Kimi Code's composer is boxed, so its rows are not swept up."""

        assert (
            kt.classify_line(marker, semantics=kt.SpinnerSemantics.CODE)
            is not kt.KimiLineKind.READY_INPUT_FRAME
        )

    def test_prose_containing_a_sparkle_is_untouched(self):
        assert (
            kt.classify_line("• Use the ✨ glyph in your answer.") is kt.KimiLineKind.FINAL_BULLET
        )
        assert kt.classify_line("The ✨ marker is the legacy prompt.") is kt.KimiLineKind.CONTENT
        assert kt.classify_line("• I used 💫 to mean the composer.") is kt.KimiLineKind.FINAL_BULLET

    def test_kimi_code_moon_semantics_are_unchanged(self):
        moon = "\U0001f315"
        assert (
            kt.classify_line(moon, semantics=kt.SpinnerSemantics.CODE)
            is not kt.KimiLineKind.LIVE_SPINNER
        )
        assert (
            kt.classify_line(moon, semantics=kt.SpinnerSemantics.CODE)
            is not kt.KimiLineKind.READY_INPUT_FRAME
        )


#: A trust dialog whose workspace path contains a space, rendered as measured:
#: the workspace row is drawn in colour 255 between the navigation hint and the
#: option list.
_TRUST_DIALOG_SPACED_WORKSPACE = [
    "\x1b[1m\x1b[38;5;111m Trust this folder?\x1b[0m",
    "\x1b[38;5;242m ↑↓ navigate · Enter select · Esc exit\x1b[39m",
    "",
    "  \x1b[38;5;255m/tmp/my project\x1b[39m",
    "",
    "  \x1b[38;5;242mProject-level MCP servers are disabled until you explicitly "
    "choose Trust.\x1b[39m",
    "",
    "  \x1b[38;5;111m  ❯ \x1b[1mTrust this folder\x1b[0m",
    "     \x1b[38;5;242mEnable project MCP servers. Remembered for this folder.\x1b[39m",
    "",
    "  \x1b[38;5;244m    \x1b[38;5;253mDon't trust\x1b[0m",
    "     \x1b[38;5;242mExit Kimi Code. Asked again next launch.\x1b[39m",
]


class TestP2ReviewTrustWorkspaceWithSpaces:
    """P2-5 — a rendered workspace path may contain spaces.

    The row was matched by a whitespace-free token heuristic plus an explicit
    ``" " not in stripped`` guard, so ``/tmp/my project`` was rejected and the
    dialog became unanswerable. The row must be identified from the dialog's
    structure instead, without weakening the exact-cwd comparison that is the
    actual security gate.
    """

    def test_spaced_workspace_is_recovered(self):
        dialog = kt.detect_trust_dialog(_TRUST_DIALOG_SPACED_WORKSPACE)
        assert dialog is not None
        assert dialog.workspace == "/tmp/my project"
        assert dialog.selected_option == kt.TRUST_OPTION_TRUST

    def test_spaced_workspace_is_recovered_without_styling(self):
        """Escape-free input (the screen path) must work too."""

        rows = [_without_sgr(row) for row in _TRUST_DIALOG_SPACED_WORKSPACE]
        dialog = kt.detect_trust_dialog(rows)
        assert dialog is not None
        assert dialog.workspace == "/tmp/my project"

    def test_measured_fixture_workspaces_are_unchanged(self):
        for name, expected in (
            ("kimi_code_0431_06_workspace_trust_dialog_gated_mcp.txt", "<A0DIR>/project"),
            ("kimi_code_0431_07_workspace_trust_dialog_plain.txt", "<A0DIR>/project"),
        ):
            dialog = kt.detect_trust_dialog(_fixture(name).split("\n"))
            assert dialog is not None, name
            assert dialog.workspace == expected, name

    def test_a_path_shaped_prose_row_is_not_trusted_as_the_workspace(self):
        """Structure, not "contains a slash", decides which row is the folder."""

        rows = [
            "\x1b[1m\x1b[38;5;111m Trust this folder?\x1b[0m",
            "\x1b[38;5;242m ↑↓ navigate · Enter select · Esc exit\x1b[39m",
            "  \x1b[38;5;255m/tmp/my project\x1b[39m",
            "  \x1b[38;5;242mMCP config lives in /etc/mcp/servers.json\x1b[39m",
            "  \x1b[38;5;111m  ❯ \x1b[1mTrust this folder\x1b[0m",
            "  \x1b[38;5;244m    \x1b[38;5;253mDon't trust\x1b[0m",
        ]
        dialog = kt.detect_trust_dialog(rows)
        assert dialog is not None
        assert dialog.workspace == "/tmp/my project"

    @pytest.mark.asyncio
    async def test_exact_cwd_match_is_still_required(self, monkeypatch):
        """A spaced workspace must not loosen the equality check."""

        monkeypatch.setenv(KIMI_TRUST_OPT_IN_ENV, "1")
        provider = KimiCliProvider("term-p2-trust", "session-1", "window-1")
        pane = "\n".join(_TRUST_DIALOG_SPACED_WORKSPACE)

        backend = MagicMock()
        backend.get_pane_working_directory.return_value = "/tmp/my project "
        monkeypatch.setattr(kimi_cli_module, "get_backend", lambda: backend)

        with pytest.raises(Exception, match="other than this terminal"):
            await provider._handle_trust_dialog(pane)
        backend.send_special_key.assert_not_called()
        assert provider._trust_handled is False

    @pytest.mark.asyncio
    async def test_matching_spaced_workspace_is_answered(self, monkeypatch):
        monkeypatch.setenv(KIMI_TRUST_OPT_IN_ENV, "1")
        provider = KimiCliProvider("term-p2-trust-ok", "session-1", "window-1")
        pane = "\n".join(_TRUST_DIALOG_SPACED_WORKSPACE)

        backend = MagicMock()
        backend.get_pane_working_directory.return_value = "/tmp/my project"
        monkeypatch.setattr(kimi_cli_module, "get_backend", lambda: backend)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.notify_input_sent",
            lambda *a, **k: None,
        )

        assert await provider._handle_trust_dialog(pane) is True
        assert backend.send_special_key.call_args[0][2] == "Enter"


class TestP2ReviewPublicOutputPath:
    """The reviewed findings surface through ``get_output(mode=LAST)``.

    These drive that public entry point — with a real ``KimiCliProvider`` handed
    back by the provider manager — rather than the extraction helper, so the
    fixes are proven at the boundary the review traced them through.
    """

    def _get_last(self, monkeypatch, pane: str) -> str:
        from cli_agent_orchestrator.services import terminal_service

        provider = KimiCliProvider("term-p2-public", "session-1", "window-1")
        backend = MagicMock()
        backend.get_history.return_value = pane
        monkeypatch.setattr(
            terminal_service,
            "get_terminal_metadata",
            lambda terminal_id: {"tmux_session": "s", "tmux_window": "w"},
        )
        monkeypatch.setattr(terminal_service.status_monitor, "get_buffer", lambda tid: pane)
        monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
        monkeypatch.setattr(terminal_service.provider_manager, "get_provider", lambda tid: provider)
        return terminal_service.get_output("term-p2-public", terminal_service.OutputMode.LAST)

    def test_ordinary_prose_answer_is_preserved(self, monkeypatch):
        pane = "\n".join(
            [
                "💫 Explain the call syntax.",
                "• Calling this function twice returns two rows.",
                "Running a command is unnecessary here.",
            ]
        )
        assert self._get_last(monkeypatch, pane) == (
            "• Calling this function twice returns two rows.\n"
            "Running a command is unnecessary here."
        )

    def test_tool_plumbing_is_still_removed(self, monkeypatch):
        pane = "\n".join(
            [
                "💫 Run uname.",
                "● Running a command · $ uname -a",
                "Linux host 6.1.0 x86_64 GNU/Linux",
                _answer("Checked: uname -a output above."),
            ]
        )
        assert self._get_last(monkeypatch, pane) == "● Checked: uname -a output above."

    def test_quoted_marker_does_not_truncate(self, monkeypatch):
        pane = "\n".join(["💫 Show me the marker.", "", "intro", "$ printf '❯'", "Done.", ""])
        assert self._get_last(monkeypatch, pane) == "intro\n$ printf '❯'\nDone."

    def test_legacy_final_idle_prompt_is_absent(self, monkeypatch):
        pane = "\n".join(["💫 What is two plus two?", "• Four.", "💫", ""])
        assert self._get_last(monkeypatch, pane) == "• Four."
