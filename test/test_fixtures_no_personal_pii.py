# ABOUTME: Guard test that fails if any personal-email PII leaks into test fixtures.
# ABOUTME: Prevents captured-live-TUI recordings from shipping a real account address.
"""Fail-closed guard against personal PII in test fixtures.

Provider status-detection fixtures are captured from *live* CLI TUIs, whose
login banners often print the authenticated account's email. That email is
personal data and must never be committed. This regression happened once
(PR #436 shipped an antigravity login banner with a maintainer's Gmail address,
and an older claude_code capture carried another contributor's address); this
test makes a recurrence a hard, pre-merge CI failure rather than something a
human has to catch by eye in a diff.

Scope: any file under a ``fixtures/`` directory within ``test/``. We flag
addresses at well-known *personal* mail providers (gmail, yahoo, icloud, …).
Non-personal, obviously-synthetic addresses (``example.com``, ``noreply``,
``git@github.com``) are intentionally NOT flagged so legitimate sample data and
git-remote banners stay usable. When capturing a new fixture, scrub the login
banner (replace the account line with ``user@example.com``) before committing.
"""

import re
from pathlib import Path

# Personal mail providers whose presence in a fixture is almost certainly a real
# person's address captured from a live login banner. Deliberately narrow to
# avoid flagging synthetic sample data (``@example.com``) or tooling banners
# (``git@github.com``), which are legitimate in fixtures.
_PERSONAL_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@(?:gmail|googlemail|yahoo|ymail|hotmail|outlook|live|msn|"
    r"icloud|me|mac|aol|proton|protonmail|pm|gmx|zoho|yandex|mail)\.[A-Za-z]{2,}",
    re.IGNORECASE,
)

_TEST_ROOT = Path(__file__).resolve().parent


def _fixture_files() -> list[Path]:
    """Every file living under a ``fixtures/`` directory anywhere in ``test/``."""

    return [p for p in _TEST_ROOT.rglob("fixtures/**/*") if p.is_file()]


# A token can hide inside an escape sequence, where a rendered view and a
# visible-text grep both miss it. Claude Code prints its session URL as an OSC 8
# hyperlink (``ESC ] 8 ; id=... ; https://claude.ai/code/session_01... BEL``), and
# a live capture once carried a real one. These checks read the raw bytes, so the
# escape sequences are included.
#
# ``session_`` followed by 16+ letters/digits is the session-id shape; snake_case
# identifiers such as ``session_name`` stop at the next ``_`` and do not match.
# The only allowed values are the placeholders ``session_01XXXX...`` and
# ``session_REDACTED``.
#
# Scope: only the ``session_<token>`` and ``claude.ai/code/session_`` shapes.
# UUID-style or hyphenated session ids used by other providers are not covered.
_SESSION_ID_RE = re.compile(rb"session_(?!01X+(?![A-Za-z0-9])|REDACTED)[A-Za-z0-9]{16,}")
_SESSION_URL_RE = re.compile(
    rb"claude\.ai/code/session_(?!01X+(?![A-Za-z0-9])|REDACTED(?![A-Za-z0-9]))"
)

# OSC payloads (``ESC ] ... BEL`` or ``ESC ] ... ESC \\``), and CSI sequences,
# which can split visible text such as ``user\x1b[1m@gmail.com``.
_OSC_RE = re.compile(rb"\x1b\](.*?)(?:\x07|\x1b\\)", re.DOTALL)
_CSI_RE = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")


def _escape_aware_findings(raw: bytes) -> set[str]:
    """Session ids and URLs in the raw bytes, plus personal emails in the text
    with escape sequences stripped and in each decoded OSC payload."""

    found = {m.group(0).decode("utf-8", "replace") for m in _SESSION_ID_RE.finditer(raw)}
    found |= {m.group(0).decode("utf-8", "replace") for m in _SESSION_URL_RE.finditer(raw)}
    texts = [_OSC_RE.sub(b"", _CSI_RE.sub(b"", raw))]
    texts += [m.group(1) for m in _OSC_RE.finditer(raw)]
    for chunk in texts:
        text = chunk.decode("utf-8", errors="replace")
        found |= {m.group(0) for m in _PERSONAL_EMAIL_RE.finditer(text)}
    return found


def _finding_type(finding: str) -> str:
    if finding.startswith("claude.ai/code/session_"):
        return "session URL"
    if finding.startswith("session_"):
        return "session id"
    return "personal email"


def test_escape_aware_scan_covers_the_raw_claude_capture() -> None:
    """The scan must reach the raw capture that once carried a real session URL."""

    names = {p.name for p in _fixture_files()}
    assert "claude_code_2332_write_permission_prompt_raw.txt" in names


def test_escape_aware_scan_catches_tokens_inside_escape_sequences() -> None:
    """Positive and negative controls for the raw-byte scan."""

    real_url = b"\x1b]8;id=1;https://claude.ai/code/session_01AbCdEfGhIjKlMnOpQrStUv?from=cli\x07"
    assert _escape_aware_findings(real_url) == {
        "session_01AbCdEfGhIjKlMnOpQrStUv",
        "claude.ai/code/session_",
    }
    assert _escape_aware_findings(b"\x1b]0;me@gmail.com\x1b\\") == {"me@gmail.com"}
    assert _escape_aware_findings(b"me\x1b[1m@gmail.com") == {"me@gmail.com"}
    placeholders = (
        b"\x1b]8;id=1;https://claude.ai/code/session_01XXXXXXXXXXXXXXXXXXXXXX?from=cli\x07"
        b" https://claude.ai/code/session_REDACTED session_name session_rsa_keys"
    )
    assert _escape_aware_findings(placeholders) == set()


def test_no_session_tokens_or_personal_emails_inside_escape_sequences() -> None:
    """No fixture may hide a session id or URL, or a personal email, in its raw
    bytes, inside an OSC payload, or split by an escape sequence.

    Covers only the ``session_<token>`` and ``claude.ai/code/session_`` shapes,
    not UUID-style or hyphenated session ids of other providers."""

    offenders: dict[str, set[str]] = {}
    for path in _fixture_files():
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        found = _escape_aware_findings(raw)
        if found:
            # Finding types only, so the failure output does not republish a token.
            offenders[str(path.relative_to(_TEST_ROOT))] = {_finding_type(f) for f in found}

    assert not offenders, (
        "Session ids/URLs or personal emails found in raw fixture bytes (check "
        "OSC 8 hyperlinks and window titles). Replace a session id with a "
        "same-length `session_01XXXX...` placeholder:\n"
        + "\n".join(f"  {f}: {', '.join(sorted(v))}" for f, v in sorted(offenders.items()))
    )


def test_no_personal_email_addresses_in_fixtures() -> None:
    """No test fixture may contain a personal-provider email address.

    Reads each fixture leniently (fixtures are raw terminal captures with ANSI
    escapes, not necessarily valid UTF-8) and asserts no personal email matches.
    On failure the message names the file(s) and the offending address(es) so the
    fix is obvious: delete the orphaned fixture or scrub the login banner.
    """

    offenders: dict[str, set[str]] = {}
    for path in _fixture_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        matches = {m.group(0) for m in _PERSONAL_EMAIL_RE.finditer(text)}
        if matches:
            offenders[str(path.relative_to(_TEST_ROOT))] = matches

    assert not offenders, (
        "Personal email addresses (PII) found in test fixtures — scrub the login "
        "banner to `user@example.com` or delete the orphaned fixture:\n"
        + "\n".join(f"  {f}: {', '.join(sorted(addrs))}" for f, addrs in sorted(offenders.items()))
    )
