"""Focused regression for the live-evidence sanitizer (r1, cond steers
109/112): the machine temp root is redacted in BOTH its raw
(``/var/folders/...``) and resolved (``/private/var/folders/...``) forms —
macOS symlinks ``/var`` and provider transcripts render the resolved one —
and account display names are harvested for redaction, so a rerun of the
live drills can never reintroduce raw tmp paths or the banner name.

Runs in the ordinary unit suite (deliberately not e2e-marked): the
sanitizer is a pure function of the redaction table, no live provider
needed.
"""

from __future__ import annotations

import os
import tempfile
from test.e2e.test_native_tui_provider_acceptance import Evidence
from test.e2e.test_operator_message_live import _harvest_account_display_names


def _sanitize_with_machine_redactions(text: str, tmp_path) -> str:
    evidence = Evidence(tmp_path)
    evidence.redact(tempfile.gettempdir(), "<HOST_TMP>")
    evidence.redact(os.path.realpath(tempfile.gettempdir()), "<HOST_TMP>")
    return evidence.sanitize(text)


def test_raw_tempdir_form_is_redacted(tmp_path):
    raw = tempfile.gettempdir()
    text = f"Directory: {raw}/pytest-of-op/pytest-576/cao_state_l"
    assert (
        _sanitize_with_machine_redactions(text, tmp_path)
        == "Directory: <HOST_TMP>/pytest-of-op/pytest-576/cao_state_l"
    )


def test_resolved_private_tempdir_form_is_redacted(tmp_path):
    resolved = os.path.realpath(tempfile.gettempdir())
    text = f"Read the image file at {resolved}/pytest-of-op/p"
    assert (
        _sanitize_with_machine_redactions(text, tmp_path)
        == "Read the image file at <HOST_TMP>/pytest-of-op/p"
    )


def test_truncated_fragment_form_is_redacted(tmp_path):
    """The box-truncated renderings that defeated exact-path redaction."""
    resolved = os.path.realpath(tempfile.gettempdir())
    text = f"Directory: {resolved}/pytest-of-op/pytest-57…"
    assert (
        _sanitize_with_machine_redactions(text, tmp_path)
        == "Directory: <HOST_TMP>/pytest-of-op/pytest-57…"
    )


def test_display_name_harvest_covers_full_and_first_token(tmp_path):
    (tmp_path / ".claude.json").write_text(
        '{"oauthAccount": {"displayName": "Amber Example", '
        '"organizationName": "Example Org", '
        '"emailAddress": "amber@example.com"}}',
        encoding="utf-8",
    )
    names = _harvest_account_display_names(tmp_path)
    assert "Amber Example" in names
    assert "Amber" in names
    assert "Example Org" in names
    # Email addresses are the email harvest's domain, not this one's.
    assert "amber@example.com" not in names


def test_display_name_harvest_tolerates_a_missing_or_broken_file(tmp_path):
    assert _harvest_account_display_names(tmp_path) == []
    (tmp_path / ".claude.json").write_text("{not json", encoding="utf-8")
    assert _harvest_account_display_names(tmp_path) == []
