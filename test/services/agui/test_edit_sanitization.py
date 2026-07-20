"""Tests for edited_text sanitization in the approval path (P1-4).

Operator-supplied edit text is written to a real terminal via send_input, so
ANSI/VT escape sequences and control bytes (incl. NUL) must be stripped to
prevent terminal escape-sequence injection.
"""

from __future__ import annotations

from cli_agent_orchestrator.services.agui.handoff_approval import (
    ApprovalDecision,
    _sanitize_edited_text,
    _translate_decision,
)

_CONTROL_AND_ESC = set(range(0x00, 0x09)) | {0x0B, 0x0C} | set(range(0x0E, 0x20)) | {0x7F}


def _has_no_control_bytes(s: str) -> bool:
    return all(ord(c) not in _CONTROL_AND_ESC for c in s)


def test_sanitize_strips_ansi_csi_and_control_bytes() -> None:
    crafted = "\x1b[31mrm -rf /\x1b[0m\x00\x07plain"
    out = _sanitize_edited_text(crafted)
    assert out == "rm -rf /plain"
    assert _has_no_control_bytes(out)


def test_sanitize_strips_osc_sequence() -> None:
    crafted = "before\x1b]0;pwned-title\x07after"
    out = _sanitize_edited_text(crafted)
    assert out == "beforeafter"
    assert "\x1b" not in out


def test_sanitize_strips_lone_escape() -> None:
    out = _sanitize_edited_text("a\x1bZb")
    assert "\x1b" not in out
    assert _has_no_control_bytes(out)


def test_sanitize_preserves_benign_whitespace() -> None:
    text = "line1\nline2\ttabbed\r\ncarriage"
    assert _sanitize_edited_text(text) == text


def test_translate_decision_edit_is_sanitized() -> None:
    action = _translate_decision(
        "kiro_cli",
        ApprovalDecision.EDIT,
        edited_text="\x1b[2J\x1b[1;1Hmalicious\x00",
    )
    assert action == {"type": "text", "value": "malicious"}
    assert _has_no_control_bytes(action["value"])


def test_translate_decision_edit_handles_none() -> None:
    action = _translate_decision("kiro_cli", ApprovalDecision.EDIT, edited_text=None)
    assert action == {"type": "text", "value": ""}
