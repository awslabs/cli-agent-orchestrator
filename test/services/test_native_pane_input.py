"""Focused tests for the composer-observation primitives in native_pane_input.

These prove the provider-pinned text extraction directly, independent of the
HTTP route, so a bug in extraction shows up as a service-level failure rather
than being hidden inside a route test.
"""

from __future__ import annotations

import hashlib

import pytest

from cli_agent_orchestrator.services import native_pane_input
from cli_agent_orchestrator.services.native_pane_input import (
    ComposerObservationPin,
    extract_composer_text,
)

TEXT = "/compact"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _codex_pin() -> ComposerObservationPin:
    return ComposerObservationPin(
        provider="codex",
        rule=native_pane_input._RULE_CODEX_PROMPT_FOOTER,
        composer_tail_rows=4,
        evidence="test pin",
    )


def _kimi_pin() -> ComposerObservationPin:
    return ComposerObservationPin(
        provider="kimi_cli",
        rule=native_pane_input._RULE_KIMI_COMPOSER_BOX,
        composer_tail_rows=5,
        evidence="test pin",
    )


def test_extract_composer_text_for_codex():
    rows = [
        "transcript row",
        f"› {TEXT}",
        "",
        "  footer/status",
    ]
    extracted = extract_composer_text(rows, _codex_pin())
    assert extracted == TEXT
    assert _sha256(extracted) == _sha256(TEXT)


def test_extract_composer_text_preserves_internal_whitespace():
    text = "tell worker: use  two spaces"
    rows = ["transcript row", f"› {text}", "", "  footer/status"]
    assert extract_composer_text(rows, _codex_pin()) == text


def test_extract_composer_text_for_kimi():
    rows = [
        "transcript row",
        "╭──────────────────────────────────────────────────────╮",
        f"│ > {TEXT} │",
        "╰──────────────────────────────────────────────────────╯",
        "  footer/status",
    ]
    extracted = extract_composer_text(rows, _kimi_pin())
    assert extracted == TEXT


def test_extract_composer_text_for_kimi_0330():
    # The 0.33.0 pin reuses the same rounded-box rule; extraction must still
    # return the exact payload from a one-row composer box.
    rows = [
        "transcript row",
        "╭──────────────────────────────────────────────────────╮",
        f"│ > {TEXT} │",
        "╰──────────────────────────────────────────────────────╯",
        "  footer/status",
    ]
    pin = native_pane_input.composer_observation_pin_for("kimi_cli", "0.33.0")
    assert pin is not None
    extracted = extract_composer_text(rows, pin)
    assert extracted == TEXT


def test_extract_composer_text_for_kimi_0330_uses_expected_bytes_to_remove_box_padding():
    text = (
        "[conduct] Continue the retained round: re-read the durable task at "
        "/Users/colin/.local/state/cao-conductor/p1-closure/runs/"
        "cond-0225-0230-native-attestation-spec-review-k3-r12/task-round-5.md "
        "and proceed."
    )
    rows = [
        "transcript row",
        " ╭────────────────────────────────────────────────────────────────╮",
        f" │ > {text}{' ' * 68}│",
        " ╰────────────────────────────────────────────────────────────────╯",
        " footer/status",
    ]
    pin = native_pane_input.composer_observation_pin_for("kimi_cli", "0.33.0")
    assert pin is not None
    assert (
        extract_composer_text(
            rows,
            pin,
            expected_text_bytes=len(text.encode("utf-8")),
        )
        == text
    )


def test_extract_composer_text_for_kimi_refuses_trailing_space_ambiguity():
    # A composer holding "text" paints the same prefix as an expected
    # "text  " followed by frame padding.  Refuse instead of treating box
    # padding as user-supplied trailing whitespace.
    rows = [
        "╭────────────────────────╮",
        f"│ > text{' ' * 16}│",
        "╰────────────────────────╯",
    ]
    assert extract_composer_text(rows, _kimi_pin(), expected_text_bytes=len(b"text  ")) is None


def test_extract_composer_text_for_kimi_refuses_non_padding_suffix():
    rows = [
        "╭────────────────────────╮",
        "│ > expected-extra      │",
        "╰────────────────────────╯",
    ]
    assert extract_composer_text(rows, _kimi_pin(), expected_text_bytes=len(b"expected")) is None


def test_extract_composer_text_for_kimi_refuses_partial_utf8_character():
    rows = [
        "╭────────────────────────╮",
        "│ > café                 │",
        "╰────────────────────────╯",
    ]
    assert extract_composer_text(rows, _kimi_pin(), expected_text_bytes=4) is None


def test_extract_composer_text_returns_none_when_region_unreadable():
    rows = ["no composer here"]
    assert extract_composer_text(rows, _codex_pin()) is None


def test_extract_composer_text_refuses_wrapped_or_ambiguous_rows():
    wrapped = ["transcript row", "› first", "second", "", "footer/status"]
    trailing = ["transcript row", "› text ", "", "footer/status"]
    assert extract_composer_text(wrapped, _codex_pin()) is None
    assert extract_composer_text(trailing, _codex_pin()) is None


def test_composer_observation_pin_is_build_exact():
    assert native_pane_input.composer_observation_pin_for("codex", "0.146.0") is not None
    assert native_pane_input.composer_observation_pin_for("codex", "0.145.0") is None
    assert native_pane_input.composer_observation_pin_for("kimi_cli", "0.29.2") is not None
    assert native_pane_input.composer_observation_pin_for("kimi_cli", "0.29.1") is None
    # 0.33.0 is a separate pinned build; adjacent/unverified builds refuse.
    assert native_pane_input.composer_observation_pin_for("kimi_cli", "0.33.0") is not None
    assert native_pane_input.composer_observation_pin_for("kimi_cli", "0.32.0") is None
    assert native_pane_input.composer_observation_pin_for("kimi_cli", "0.33.1") is None
    assert native_pane_input.composer_observation_pin_for("kimi_cli", "0.34.0") is None
    assert native_pane_input.composer_observation_pin_for("claude_code", "2.1.220") is None
