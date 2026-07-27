"""Exact Kimi Code 0.29.2 support as the current stage-verified build.

0.29.2 is admitted as a *separate proven build*, keyed by its installed
bundle digest (``dist/main.mjs`` sha256
``2ee6e2f15d68bffdce532d1c8e50f8d40e0230090b3a0dd1dbcdb7c29bf532db``,
matching the npm-published digest).  A three-way 0.29.0/0.29.1/0.29.2
bundle comparison proved the composer keybinds, the
``expandPasteMarkers(lines.join("\\n")).trim()`` submit computation, the
120 ms paste-burst window with content-neutral reset, and the
``Key.ctrl("s")`` steer chord byte-identical across all three builds, so
the existing normalization identifier remains truthful and no behavioral
drift is claimed.  This suite pins the 0.29.2-specific proofs:

1.  the composer-newline and steer-chord tables carry separate proven
    0.29.2 entries keyed by the bundle sha256;
2.  multiline and Ctrl-S plans on a 0.29.2 session use those entries,
    while an adjacent unlisted build is refused before any I/O; and
3.  the exact resume contract admits ``--session <id>`` (golden),
    ``-S <id>`` (documented short form), and ``-r <id>`` (bundle-verified
    hidden compatibility alias, registered with ``hideHelp()``) — and
    keeps refusing newest-session shortcuts.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services import kimi_native_control as knc
from cli_agent_orchestrator.services import provider_contracts as pc

BUNDLE_SHA256 = "2ee6e2f15d68bffdce532d1c8e50f8d40e0230090b3a0dd1dbcdb7c29bf532db"


# --------------------------------------------------------------------
# Separate proven 0.29.2 entries, keyed by the bundle sha256
# --------------------------------------------------------------------


def test_the_composer_newline_table_has_a_separate_proven_0292_entry():
    entry = knc._PROVEN_COMPOSER_NEWLINE.get("0.29.2")
    assert entry is not None, "0.29.2 must be a separate keyed entry, never a range"
    assert BUNDLE_SHA256 in entry["evidence"]
    assert entry["keystroke"] == "C-j"
    assert entry["burst_reset_keystroke"] == "End"
    assert entry["submit_settle_seconds"] == 0.25
    # Same proven computation as the earlier builds: the model-visible
    # normalization is still join-LF-then-trim, so the identifier stays.
    assert entry["normalization"] == knc.NORMALIZATION_JOIN_LF_THEN_TRIM


def test_older_evidence_entries_name_the_paste_marker_expansion():
    # The corrected evidence names the full submit computation
    # ``expandPasteMarkers(lines.join("\n")).trim()`` in every build's
    # entry (comment accuracy; the computation itself is unchanged).
    for version in ("0.29.0", "0.29.1", "0.29.2"):
        assert "expandPasteMarkers" in knc._PROVEN_COMPOSER_NEWLINE[version]["evidence"]


def test_0292_steer_chords_are_proven_and_unproven_builds_refuse():
    assert knc.steer_chords("0.29.2") == frozenset({"C-s"})
    assert knc.steer_chords("kimi 0.29.2") == frozenset({"C-s"})
    # An adjacent unread build gets the honest empty answer, never a guess.
    assert knc.steer_chords("0.29.3") == frozenset()
    assert "C-s" in knc.advertised_steer_chords()["kimi_cli"]


# --------------------------------------------------------------------
# 0.29.2 multiline and Ctrl-S plans use the proven entries
# --------------------------------------------------------------------


def test_a_multiline_message_on_0292_uses_the_proven_entry():
    plan = knc.plan_composer_keystrokes("line one\nline two", provider_version="0.29.2")
    assert plan["deliverable"] is True
    assert plan["undeliverable_reason"] is None
    assert plan["soft_newline_keystroke"] == "C-j"
    assert plan["burst_reset_keystroke"] == "End"
    assert plan["submit_settle_seconds"] == 0.25
    assert plan["provider_normalization"] == knc.NORMALIZATION_JOIN_LF_THEN_TRIM
    assert plan["encoding"] == knc.ENCODING_SOFT_NEWLINE


# --------------------------------------------------------------------
# The exact resume contract: --session golden, -S documented, -r hidden
# --------------------------------------------------------------------


def test_golden_session_form_remains_the_launch_option():
    from cli_agent_orchestrator.services import kimi_native_launch as knl

    assert knl.RESUME_OPTION == "--session"


def test_documented_short_form_s_validates_with_an_exact_id():
    form = pc.validate_resume_argv("kimi", ["-S", "session_abc123"])
    assert form.native_id == "session_abc123"
    assert form.argv == ("-S", "session_abc123")


def test_golden_and_hidden_alias_forms_still_validate():
    assert pc.validate_resume_argv("kimi", ["--session", "session_abc"]).native_id == "session_abc"
    assert pc.validate_resume_argv("kimi", ["-r", "session_abc"]).native_id == "session_abc"


@pytest.mark.parametrize(
    "argv",
    [
        ["--session"],  # argument-less: opens the interactive picker
        ["-S"],  # same hazard on the short form
        ["-r"],  # and on the hidden alias
        ["--continue"],  # newest-session shortcut: forbidden
        ["-c"],  # newest-session shortcut: forbidden
        ["--session=session_abc"],  # only the exact two-token form
    ],
)
def test_newest_session_shortcuts_and_inexact_forms_refuse(argv):
    with pytest.raises(pc.ResumeFormRefused):
        pc.validate_resume_argv("kimi", argv)
