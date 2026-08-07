"""Exact Kimi Code 0.32.0 support as a stage-verified build (cond-0315).

The operator's binary auto-updated again, and the managed launcher refused the
installed 0.32.0 fail-closed (``kimi version drift: accepted [...], installed
'0.32.0'; resume refuses (41)`` — run cond-0303-pr74-review-k3-r5,
``failure_class: managed-preflight_blocked``, zero task bytes).  0.32.0 is
admitted as a *separate proven build*, keyed by its installed bundle digest
(``dist/main.mjs`` sha256
``b02ebfe77dda7d9f38cf61c5a923567eb7ff4f3bc914dff24b02b5fd22b4ff79``), only
after stage verification proved the version-specific behaviour the managed
launcher relies on:

* the composer keybinds, the ``expandPasteMarkers(lines.join("\\n")).trim()``
  submit computation, the 120 ms paste-burst window with content-neutral
  reset, and the ``Key.ctrl("s")`` steer chord read byte-identical to the
  0.29.x/0.30.0/0.31.0 line from the installed 0.32.0 bundle, so the existing
  normalization identifier remains truthful and no behavioral drift is
  claimed;
* the build still rewrites ``process.title`` to ``kimi-code`` after parsing
  its argv (the COND-0312 rewrite introduced in 0.31.0), so the resumed
  session id is unreadable from the kernel argv and the rendered native
  header remains the bound-session proof — live-verified on the installed
  0.32.0, which renders the strict boot header (exactly one each of
  ``Directory``, ``Session``, ``Model``, and ``Version`` label lines, framed
  by box verticals) naming the resumed session;
* the bounded ``--version`` probe answers ``0.32.0``; the exact
  ``--session <id>`` resume form (and its ``-S``/``-r`` aliases) is
  unchanged; and the zero-prompt ACP route probe selects and reads back the
  exact K3/max route.

This suite pins the 0.32.0-specific proofs:

1.  the version gates accept exact ``0.32.0`` (bare and banner) and keep
    refusing unknown/future neighbours — an exact set, never a range;
2.  the composer-newline and steer-chord tables carry separate proven
    0.32.0 entries keyed by the bundle sha256, and a multiline message on a
    0.32.0 session uses them;
3.  the rendered-header proof table carries a separate 0.32.0 entry, and a
    0.32.0-shaped header proves exactly the bound session — while a wrong
    session, a wrong version line, a missing/duplicated label, or an
    unproven neighbouring build all fail closed; and
4.  the exact resume contract still admits only ``--session <id>``,
    ``-S <id>``, and ``-r <id>`` with an exact id.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services import kimi_native_control as knc
from cli_agent_orchestrator.services import kimi_native_launch as knl
from cli_agent_orchestrator.services import provider_contracts as pc

PIN_0320 = "0.32.0"
BUNDLE_SHA256 = "b02ebfe77dda7d9f38cf61c5a923567eb7ff4f3bc914dff24b02b5fd22b4ff79"

# The header shape the installed 0.32.0 paints, framed the way
# ``capture-pane`` renders it (see the COND-0312 header rows for 0.31.0).
HEADER_0320_ROWS = [
    "│  Welcome to Kimi Code!                                                                              │",
    "│  Directory: /private/tmp/stage-probe                                                                │",
    "│  Session:   session_9f2c41ab                                                                        │",
    "│  Model:     K3                                                                                      │",
    "│  Version:   0.32.0                                                                                  │",
]


# --------------------------------------------------------------------
# The version gates accept exact 0.32.0 and refuse every neighbour
# --------------------------------------------------------------------


@pytest.mark.parametrize("banner", ["0.32.0", "kimi 0.32.0"])
def test_check_pinned_version_accepts_the_stage_verified_0320(banner):
    pc.check_pinned_version("kimi", banner)


@pytest.mark.parametrize("bad", ["kimi", ""])
def test_check_pinned_version_still_refuses_unknown_and_unparseable_builds(bad):
    # 0.32.0 remains a proven build.  In open enforcement, semver-shaped
    # neighbours are accepted at the launch boundary but do not inherit
    # feature-specific authority.  (0.33.0 and 0.34.0 are later
    # stage-verified/proven builds.)  Only unparseable banners fail closed.
    with pytest.raises(pc.ProviderVersionDrift):
        pc.check_pinned_version("kimi", bad)


@pytest.mark.parametrize("version", ["0.31.1", "0.32.1", "0.33.1", "1.0.0"])
def test_semver_neighbours_launch_but_get_no_composer_authority(version):
    # Open enforcement accepts semver-shaped neighbours at launch...
    pc.check_pinned_version("kimi", version)
    # ...but they do not inherit the proven composer/steer entries.
    plan = knc.plan_composer_keystrokes("line one\nline two", provider_version=version)
    assert plan["deliverable"] is False
    assert version in plan["undeliverable_reason"]
    assert knc.steer_chords(version) == frozenset()


def test_0320_is_a_separate_accepted_build_never_a_range_widening():
    assert PIN_0320 in pc.SUPPORTED_VERSIONS["kimi"]
    # Every earlier proven build stays admitted for already-minted sessions.
    for retained in ("0.31.0", "0.30.0", "0.29.2", "0.29.1", "0.29.0"):
        assert retained in pc.SUPPORTED_VERSIONS["kimi"]


# --------------------------------------------------------------------
# Separate proven 0.32.0 composer/steer entries, keyed by the bundle sha256
# --------------------------------------------------------------------


def test_the_composer_newline_table_has_a_separate_proven_0320_entry():
    entry = knc._PROVEN_COMPOSER_NEWLINE.get(PIN_0320)
    assert entry is not None, "0.32.0 must be a separate keyed entry, never a range"
    assert BUNDLE_SHA256 in entry["evidence"]
    assert entry["keystroke"] == "C-j"
    assert entry["burst_reset_keystroke"] == "End"
    assert entry["submit_settle_seconds"] == 0.25
    # Same proven computation as the earlier builds: the model-visible
    # normalization is still join-LF-then-trim, so the identifier stays.
    assert entry["normalization"] == knc.NORMALIZATION_JOIN_LF_THEN_TRIM


def test_0320_steer_chords_are_proven_and_unproven_builds_refuse():
    assert knc.steer_chords(PIN_0320) == frozenset({"C-s"})
    assert knc.steer_chords(f"kimi {PIN_0320}") == frozenset({"C-s"})
    # An adjacent unread build gets the honest empty answer, never a guess.
    assert knc.steer_chords("0.32.1") == frozenset()


def test_a_multiline_message_on_0320_uses_the_proven_entry():
    plan = knc.plan_composer_keystrokes("line one\nline two", provider_version=PIN_0320)
    assert plan["deliverable"] is True
    assert plan["undeliverable_reason"] is None
    assert plan["soft_newline_keystroke"] == "C-j"
    assert plan["burst_reset_keystroke"] == "End"
    assert plan["submit_settle_seconds"] == 0.25
    assert plan["provider_normalization"] == knc.NORMALIZATION_JOIN_LF_THEN_TRIM
    assert plan["encoding"] == knc.ENCODING_SOFT_NEWLINE


def test_an_unproven_neighbouring_version_is_not_silently_accepted():
    plan = knc.plan_composer_keystrokes("line one\nline two", provider_version="0.32.1")
    assert plan["deliverable"] is False
    assert "0.32.1" in plan["undeliverable_reason"]


# --------------------------------------------------------------------
# The rendered native header is the 0.32.0 bound-session proof (COND-0312)
# --------------------------------------------------------------------


def test_the_rendered_session_proof_table_has_a_separate_proven_0320_entry():
    proof = knl.rendered_session_proof_for(PIN_0320)
    assert proof is not None, "0.32.0 rewrites its title; its header proof must be keyed"
    assert proof.provider == "kimi_cli"
    assert proof.rule == knl.RULE_KIMI_NATIVE_HEADER
    assert BUNDLE_SHA256 in proof.evidence
    # The banner form names the same build.
    assert knl.rendered_session_proof_for(f"kimi {PIN_0320}") == proof


def test_the_0320_header_proves_exactly_the_bound_session():
    assert knl.renders_session_exactly(
        HEADER_0320_ROWS, "session_9f2c41ab", provider_version=PIN_0320
    )
    assert knl.renders_session_exactly(
        HEADER_0320_ROWS, "session_9f2c41ab", provider_version=f"kimi {PIN_0320}"
    )


def _mutated_rows(*, session: str = "session_9f2c41ab", version: str = PIN_0320) -> list[str]:
    return [
        row.replace("session_9f2c41ab", session).replace("0.32.0", version)
        for row in HEADER_0320_ROWS
    ]


def test_a_header_naming_another_session_proves_nothing():
    assert not knl.renders_session_exactly(
        _mutated_rows(session="session_deadbeef"), "session_9f2c41ab", provider_version=PIN_0320
    )


def test_a_header_naming_another_version_proves_nothing():
    # The header is the 0.31.0 shape: it must not prove anything for a
    # 0.32.0 binding, and a 0.32.0 header must not serve a 0.31.0 one.
    assert not knl.renders_session_exactly(
        _mutated_rows(version="0.31.0"), "session_9f2c41ab", provider_version=PIN_0320
    )
    assert not knl.renders_session_exactly(
        HEADER_0320_ROWS, "session_9f2c41ab", provider_version="0.31.0"
    )


def test_a_missing_or_duplicated_label_fails_closed():
    without_session = [row for row in HEADER_0320_ROWS if "Session:" not in row]
    assert not knl.renders_session_exactly(
        without_session, "session_9f2c41ab", provider_version=PIN_0320
    )
    duplicated = HEADER_0320_ROWS + [
        "│  Session:   session_9f2c41ab                                                                        │"
    ]
    assert not knl.renders_session_exactly(
        duplicated, "session_9f2c41ab", provider_version=PIN_0320
    )


def test_an_unproven_build_cannot_inherit_the_0320_proof():
    # A header shaped like 0.32.0's proves nothing for a build whose
    # title-rewrite and header layout were never read.
    assert not knl.renders_session_exactly(
        HEADER_0320_ROWS, "session_9f2c41ab", provider_version="0.32.1"
    )
    assert knl.rendered_session_proof_for("0.32.1") is None


# --------------------------------------------------------------------
# The exact resume contract is unchanged on 0.32.0
# --------------------------------------------------------------------


def test_exact_resume_forms_still_validate():
    assert pc.validate_resume_argv("kimi", ["--session", "session_abc"]).native_id == "session_abc"
    assert pc.validate_resume_argv("kimi", ["-S", "session_abc"]).native_id == "session_abc"
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
def test_inexact_resume_forms_still_refuse(argv):
    with pytest.raises(pc.ResumeFormRefused):
        pc.validate_resume_argv("kimi", argv)
