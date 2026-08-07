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


def test_extract_composer_text_returns_none_when_region_unreadable():
    rows = ["no composer here"]
    assert extract_composer_text(rows, _codex_pin()) is None


def test_composer_observation_pin_is_build_exact():
    assert native_pane_input.composer_observation_pin_for("codex", "0.146.0") is not None
    assert native_pane_input.composer_observation_pin_for("codex", "0.145.0") is None
    assert native_pane_input.composer_observation_pin_for("kimi_cli", "0.29.2") is not None
    assert native_pane_input.composer_observation_pin_for("kimi_cli", "0.29.1") is None
    assert native_pane_input.composer_observation_pin_for("claude_code", "2.1.220") is None
