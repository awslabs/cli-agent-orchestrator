"""Tests for edited_text sanitization in the approval path (P1-4).

Operator-supplied edit text is written to a real terminal via send_input, so
ANSI/VT escape sequences and control bytes (incl. NUL) must be stripped to
prevent terminal escape-sequence injection.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services.agui.handoff_approval import (
    ApprovalDecision,
    _sanitize_edited_text,
    _translate_decision,
)

_CONTROL_AND_ESC = (
    set(range(0x00, 0x09)) | {0x0B, 0x0C} | set(range(0x0E, 0x20)) | set(range(0x7F, 0xA0))
)


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


def test_sanitize_strips_c1_control_bytes() -> None:
    # C1 controls (U+0080-U+009F) must be stripped too: U+009B is the 8-bit
    # CSI, which some terminal stacks treat as ESC-[ — an escape-injection
    # vector that survives the 7-bit ANSI pass. U+0085 (NEL) is a C1 line
    # separator and must not survive either.
    out = _sanitize_edited_text("a\u009b31mb\u0085c")
    assert out == "a31mbc"
    assert _has_no_control_bytes(out)


def test_sanitize_preserves_tab_but_collapses_to_single_line() -> None:
    # Tab is benign and preserved. CR/LF are removed/truncated so an edited
    # answer is a single line that cannot auto-submit or smuggle a follow-on
    # command line into the live PTY (WS-2).
    assert _sanitize_edited_text("col1\tcol2") == "col1\tcol2"
    assert _sanitize_edited_text("line1\nline2") == "line1"
    assert _sanitize_edited_text("a\r\nb") == "a"
    assert _sanitize_edited_text("keep\rjoin") == "keepjoin"


def test_sanitize_strips_c1_control_bytes() -> None:
    # C1 controls (U+0080–U+009F), notably U+009B (8-bit CSI), must be stripped
    # too — the sanitizer claims C0/C1 coverage.
    assert _sanitize_edited_text("a\x9bBc") == "aBc"
    assert _sanitize_edited_text("x\x80\x9fy") == "xy"


def test_translate_decision_edit_is_sanitized() -> None:
    action = _translate_decision(
        "kiro_cli",
        ApprovalDecision.EDIT,
        edited_text="\x1b[2J\x1b[1;1Hmalicious\x00",
    )
    assert action == {"type": "text", "value": "malicious"}
    assert _has_no_control_bytes(action["value"])


def test_translate_decision_edit_handles_none() -> None:
    with pytest.raises(ValueError, match="empty after sanitization"):
        _translate_decision("kiro_cli", ApprovalDecision.EDIT, edited_text=None)
