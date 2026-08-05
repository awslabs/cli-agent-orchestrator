"""The §4.1 composer-emptiness determination, per provider+build pin.

The guard's three answers are load-bearing: a false "empty" is the r5
concatenation defect, and a false "non-empty" strands a legitimate
command behind a refusal.  These tests pin both directions against
synthetic captures in the exact shapes the pinned builds render, so a
misread region or styling rule fails loudly.  Live verification per
build is §10.3; this is the unit-tier half.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services import native_pane_input as npi

ESC = "\x1b"
RESET = f"{ESC}[0m"
DIM = f"{ESC}[2m"
INVERSE = f"{ESC}[7m"
GRAY = f"{ESC}[38;2;136;136;136m"


def _kimi_rows(content_rows):
    """A Kimi Code 0.29.2 screen: conversation, the rounded composer box
    (live-verified form: untitled '╭─╮' … '╰─╯', '│ > ' prompt rows), and
    the status bar."""
    return [
        " ✨ What is 17*23?",
        "   Interrupted by user",
        "",
        " ╭────────────────────────────────────────────────────────────────────────────────────────────────╮",
        *content_rows,
        " ╰────────────────────────────────────────────────────────────────────────────────────────────────╯",
        " K2.7 Coding Highspeed thinking  /tmp/work  master",
        "                                                                               context: 0% (0/256k)",
    ]


def _claude_rows(prompt_row):
    """A Claude Code screen: transcript, prompt box (two rules + prompt)."""
    return [
        "⏺ Here is the response",
        f"{GRAY}────────────────────────{RESET}",
        "❯ second task",
        f"{GRAY}────────────────────────{RESET}",
        prompt_row,
        f"{GRAY}────────────────────────{RESET}",
    ]


def _codex_rows(prompt_row, *wrapped_rows, suggestion=None):
    """A styled Codex 0.146.0 screen ending in its live composer/footer."""
    rows = [
        f"{ESC}[1m›{RESET} {DIM}earlier transcript prompt{RESET}",
        "",
        "• Earlier response",
        "",
        prompt_row,
        *wrapped_rows,
        "",
    ]
    if suggestion is not None:
        rows.append(suggestion)
    rows.extend(
        [
            "",
            f"  {DIM}gpt-5.6-terra xhigh · ~/project · branch{RESET}",
        ]
    )
    return rows


class TestEmptinessPins:
    def test_the_pinned_builds_are_exactly_the_live_verified_ones(self):
        pin = npi.composer_emptiness_pin_for("kimi_cli", "0.29.2")
        assert pin is not None and pin.rule == "kimi-composer-box" and not pin.styled
        pin = npi.composer_emptiness_pin_for("claude_code", "2.1.220")
        assert pin is not None and pin.rule == "claude-prompt-box" and pin.styled
        pin = npi.composer_emptiness_pin_for("codex", "0.146.0")
        assert pin is not None and pin.rule == "codex-prompt-footer" and pin.styled

    def test_non_live_verified_kimi_builds_are_honestly_unpinned(self):
        """Only 0.29.2's composer is live-verified (§10.3): every other
        accepted build — including the text-proven 0.30.0 and 0.31.0 (cond-0310)
        and 0.32.0/0.33.0 (cond-0315) — refuses provider-unsupported rather than
        borrow a region determination nobody has read on them. Adding a build
        to the version set never grants it a composer-emptiness pin."""
        for build in ("0.29.0", "0.29.1", "0.30.0", "0.31.0", "0.32.0", "0.33.0"):
            assert npi.composer_emptiness_pin_for("kimi_cli", build) is None, build

    def test_version_banners_normalize_like_the_adapter_pins(self):
        assert npi.composer_emptiness_pin_for("kimi_cli", "kimi 0.29.2") is not None
        assert npi.composer_emptiness_pin_for("claude_code", "2.1.220 (Claude Code)")

    def test_an_unpinned_build_or_provider_has_no_pin(self):
        assert npi.composer_emptiness_pin_for("kimi_cli", "0.28.0") is None
        assert npi.composer_emptiness_pin_for("kimi_cli", None) is None
        assert npi.composer_emptiness_pin_for("codex", "0.145.0") is None
        assert npi.composer_emptiness_pin_for(None, "0.29.2") is None

    def test_every_pin_carries_its_evidence(self):
        for provider, version in (
            ("kimi_cli", "0.29.2"),
            ("claude_code", "2.1.220"),
            ("codex", "0.146.0"),
        ):
            pin = npi.composer_emptiness_pin_for(provider, version)
            assert pin.evidence and "§10.3" in pin.evidence


class TestKimiComposerBox:
    """The live-verified 0.29.2 form: an untitled rounded box with a '> '
    prompt and no placeholder (§10.3 evidence lane-a-10.3 cases 07/08)."""

    def test_an_empty_box_is_proven_empty(self):
        assert (
            npi._kimi_composer_empty(
                _kimi_rows(
                    [
                        " │ >                                                                                              │"
                    ]
                )
            )
            is True
        )

    def test_any_content_after_the_prompt_is_prefill(self):
        assert (
            npi._kimi_composer_empty(
                _kimi_rows(
                    [
                        " │ > queued draft                                                                                 │"
                    ]
                )
            )
            is False
        )
        # Whitespace-only content is not prefill.
        assert (
            npi._kimi_composer_empty(
                _kimi_rows(
                    [
                        " │ > \t                                                                                │"
                    ]
                )
            )
            is True
        )

    def test_a_wrapped_prefill_row_is_content(self):
        assert (
            npi._kimi_composer_empty(
                _kimi_rows(
                    [
                        " │ > a long draft that wrapped                                                            │",
                        " │   onto a second row                                                                    │",
                    ]
                )
            )
            is False
        )

    def test_a_box_without_the_prompt_glyph_is_unproven(self):
        """A rounded box that is not the composer (a menu, a dialog) must
        never read as an empty composer."""
        assert (
            npi._kimi_composer_empty(
                _kimi_rows(
                    [
                        " │ Select a model                                                                       │"
                    ]
                )
            )
            is None
        )

    def test_a_missing_box_is_unproven_never_empty(self):
        assert npi._kimi_composer_empty(["some", "random", "rows"]) is None
        # A top rule without its bottom rule is not a box.
        assert npi._kimi_composer_empty([" ╭───╮", " │ > ", "status"]) is None

    def test_the_status_bar_below_the_box_is_not_content(self):
        assert (
            npi._kimi_composer_empty(
                _kimi_rows(
                    [
                        " │ >                                                                                              │"
                    ]
                )
            )
            is True
        )

    def test_the_last_rounded_box_wins(self):
        rows = [
            " ╭──────────────────────────────╮",
            " │  Welcome to Kimi Code CLI!   │",
            " ╰──────────────────────────────╯",
        ] + _kimi_rows(
            [
                " │ >                                                                                              │"
            ]
        )
        assert npi._kimi_composer_empty(rows) is True


class TestClaudePromptBox:
    def test_a_dim_placeholder_is_an_empty_composer(self):
        # The 2.1.220 form: the cursor cell inverse, the suggestion dim.
        placeholder = f'{INVERSE}T{ESC}[0;2mry{RESET} {DIM}"hello"{RESET}'
        assert npi._claude_composer_empty(_claude_rows(f"❯ {placeholder}")) is True

    def test_a_bare_prompt_is_empty(self):
        assert npi._claude_composer_empty(_claude_rows("❯ ")) is True

    def test_normally_styled_content_is_prefill(self):
        """The r5 case: queued text renders in normal video and must read
        as content — a dim-reading here would be the concatenation defect."""
        assert npi._claude_composer_empty(_claude_rows("❯ queued draft")) is False

    def test_prefill_on_a_wrapped_second_row_is_content(self):
        rows = _claude_rows("❯ ")
        rows.insert(-1, "more content")
        assert npi._claude_composer_empty(rows) is False

    def test_a_missing_box_or_prompt_is_unproven(self):
        assert npi._claude_composer_empty(["no rules here"]) is None
        # One rule is not a box.
        assert npi._claude_composer_empty(["─" * 24, "❯ hi"]) is None
        # Rules framing no prompt row are not a prompt box.
        assert npi._claude_composer_empty(["─" * 24, "no prompt", "─" * 24]) is None

    def test_an_unparseable_styling_state_is_unproven(self):
        """A guessed styling state could read prefill as placeholder; the
        proof must fail closed instead."""
        rows = _claude_rows("❯ ")
        rows[-2] = "❯ " + ESC + "[38;2;1"  # truncated escape: unknowable
        assert npi._claude_composer_empty(rows) is None

    def test_osc_sequences_do_not_break_the_parse(self):
        rows = _claude_rows(f"❯ {ESC}]8;;https://example.com{ESC}\\{DIM}hint{RESET}")
        assert npi._claude_composer_empty(rows) is True


class TestCodexPromptFooter:
    def test_a_dim_placeholder_is_an_empty_composer(self):
        placeholder = f"{ESC}[1m›{RESET} {DIM}Explain this codebase{RESET}"
        assert npi._codex_composer_empty(_codex_rows(placeholder)) is True

    def test_a_bare_prompt_is_empty(self):
        assert npi._codex_composer_empty(_codex_rows(f"{ESC}[1m›{RESET} ")) is True

    def test_normally_styled_prefill_is_content(self):
        prompt = f"{ESC}[1m›{RESET} /compact"
        assert npi._codex_composer_empty(_codex_rows(prompt)) is False

    def test_wrapped_normally_styled_prefill_is_content(self):
        prompt = f"{ESC}[1m›{RESET} "
        assert npi._codex_composer_empty(_codex_rows(prompt, "wrapped draft")) is False

    def test_footer_and_slash_suggestion_are_not_composer_content(self):
        prompt = f"{ESC}[1m›{RESET} {DIM}Summarize recent commits{RESET}"
        suggestion = (
            f"  {ESC}[1m{ESC}[38;5;6m/compact  summarize conversation to prevent "
            f"hitting the context limit{RESET}"
        )
        assert npi._codex_composer_empty(_codex_rows(prompt, suggestion=suggestion)) is True

    def test_last_prompt_wins_and_missing_separator_is_unproven(self):
        prompt = f"{ESC}[1m›{RESET} {DIM}Explain this codebase{RESET}"
        assert npi._codex_composer_empty(_codex_rows(prompt)) is True
        assert npi._codex_composer_empty([prompt, "footer without separator"]) is None

    def test_missing_prompt_or_unparseable_style_is_unproven(self):
        assert npi._codex_composer_empty(["no prompt", "", "footer"]) is None
        assert npi._codex_composer_empty([f"› {ESC}[38;2;1", ""]) is None


class TestObserveComposerEmpty:
    def test_the_plain_capture_serves_the_kimi_rule(self):
        pin = npi.composer_emptiness_pin_for("kimi_cli", "0.29.2")
        seen = {}

        def screen():
            seen["called"] = True
            return _kimi_rows(
                [
                    " │ >                                                                                              │"
                ]
            )

        assert npi.observe_composer_empty("%1", pin, screen=screen) is True
        assert seen == {"called": True}

    def test_the_styled_capture_serves_the_claude_rule(self):
        pin = npi.composer_emptiness_pin_for("claude_code", "2.1.220")
        assert (
            npi.observe_composer_empty("%1", pin, screen=lambda: _claude_rows("❯ prefill")) is False
        )

    def test_the_styled_capture_serves_the_codex_rule(self):
        pin = npi.composer_emptiness_pin_for("codex", "codex-cli 0.146.0")
        prompt = f"{ESC}[1m›{RESET} /compact"
        assert npi.observe_composer_empty("%1", pin, screen=lambda: _codex_rows(prompt)) is False

    def test_an_unknown_rule_proves_nothing(self):
        pin = npi.ComposerEmptinessPin(
            provider="future", rule="some-future-rule", styled=False, evidence=""
        )
        assert npi.observe_composer_empty("%1", pin, screen=lambda: ["x"]) is None
