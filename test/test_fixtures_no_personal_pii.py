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
