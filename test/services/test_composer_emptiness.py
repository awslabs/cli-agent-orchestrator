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
    """A Kimi Code TUI screen: conversation, input box, status bar."""
    return [
        "╭──────────────────────────────╮",
        "│  Welcome to Kimi Code CLI!   │",
        "╰──────────────────────────────╯",
        "✨ What is 17*23?",
        "",
        "• 391.",
        "",
        "── input ─────────────────────────────────",
        *content_rows,
        "──────────────────────────────────────────",
        "yolo  agent (Kimi-k2.6 ●)  /tmp/x  ctrl-o: editor",
        "context: 4.0% (10.4k/262.1k)",
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


class TestEmptinessPins:
    def test_the_pinned_builds_are_exactly_the_supported_ones(self):
        for version in ("0.29.0", "0.29.1", "0.29.2"):
            pin = npi.composer_emptiness_pin_for("kimi_cli", version)
            assert pin is not None and pin.rule == "kimi-input-box" and not pin.styled
        pin = npi.composer_emptiness_pin_for("claude_code", "2.1.220")
        assert pin is not None and pin.rule == "claude-prompt-box" and pin.styled

    def test_version_banners_normalize_like_the_adapter_pins(self):
        assert npi.composer_emptiness_pin_for("kimi_cli", "kimi 0.29.1") is not None
        assert npi.composer_emptiness_pin_for("claude_code", "2.1.220 (Claude Code)")

    def test_an_unpinned_build_or_provider_has_no_pin(self):
        assert npi.composer_emptiness_pin_for("kimi_cli", "0.28.0") is None
        assert npi.composer_emptiness_pin_for("kimi_cli", None) is None
        assert npi.composer_emptiness_pin_for("codex", "0.145.0") is None
        assert npi.composer_emptiness_pin_for(None, "0.29.2") is None

    def test_every_pin_carries_its_evidence(self):
        for provider, version in (("kimi_cli", "0.29.2"), ("claude_code", "2.1.220")):
            pin = npi.composer_emptiness_pin_for(provider, version)
            assert pin.evidence and "§10.3" in pin.evidence


class TestKimiInputBox:
    def test_an_empty_box_is_proven_empty(self):
        assert npi._kimi_composer_empty(_kimi_rows(["", ""])) is True

    def test_any_content_row_is_prefill(self):
        assert npi._kimi_composer_empty(_kimi_rows(["queued draft", ""])) is False
        # Whitespace-only rows are not content.
        assert npi._kimi_composer_empty(_kimi_rows(["   ", "\t"])) is True

    def test_a_missing_box_is_unproven_never_empty(self):
        assert npi._kimi_composer_empty(["some", "random", "rows"]) is None
        # The input rule without its closing rule is not a box.
        assert npi._kimi_composer_empty(["── input ────────", "", "status"]) is None

    def test_the_status_bar_below_the_box_is_not_content(self):
        # The status bar carries text that must never read as prefill.
        assert npi._kimi_composer_empty(_kimi_rows([""])) is True

    def test_the_last_input_box_wins_over_stale_chrome(self):
        rows = [
            "── input ─────────────────────────────────",
            "stale transcript rule, not a live composer",
            "──────────────────────────────────────────",
        ] + _kimi_rows([""])
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


class TestObserveComposerEmpty:
    def test_the_plain_capture_serves_the_kimi_rule(self):
        pin = npi.composer_emptiness_pin_for("kimi_cli", "0.29.2")
        seen = {}

        def screen():
            seen["called"] = True
            return _kimi_rows([""])

        assert npi.observe_composer_empty("%1", pin, screen=screen) is True
        assert seen == {"called": True}

    def test_the_styled_capture_serves_the_claude_rule(self):
        pin = npi.composer_emptiness_pin_for("claude_code", "2.1.220")
        assert (
            npi.observe_composer_empty("%1", pin, screen=lambda: _claude_rows("❯ prefill")) is False
        )

    def test_an_unknown_rule_proves_nothing(self):
        pin = npi.ComposerEmptinessPin(
            provider="future", rule="some-future-rule", styled=False, evidence=""
        )
        assert npi.observe_composer_empty("%1", pin, screen=lambda: ["x"]) is None
