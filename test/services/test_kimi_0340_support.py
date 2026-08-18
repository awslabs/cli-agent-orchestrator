"""Kimi Code 0.34.0 support under the open provider-version policy (cond-0331).

The installed Kimi Code binary is 0.34.0.  A bounded compatibility check
verified its provider bindings and rendered a native header after an ACP
``session/new`` mint.  The source and live observations are recorded in the
build-specific evidence strings; no future version inherits this proof.

Rather than adding 0.34.0 as another hard-only pin that would block the next
normal update, this change introduces a generic provider-version policy:

* Kimi is ``open``: any non-empty semver-shaped observed version is accepted
  at the launch identity boundary, so routine updates do not trip stale route
  breakers before task bytes.
* The exact ``SUPPORTED_VERSIONS`` tuple remains the authority for
  feature-specific capabilities (native control, rendered-session proof,
  steer/composer, image, resume, route authority).  0.34.0 is in that tuple
  because it passed the compatibility check; a future semver like 0.35.0 can
  launch but inherits none of those advanced capabilities.
* Unknown/unparseable versions still fail closed at the launch identity
  boundary.
* A per-provider environment variable
  ``CAO_PROVIDER_VERSION_ENFORCEMENT_KIMI=strict`` reverts Kimi to exact
  pins without a code change, to be used after a reproducible regression.

This suite proves the 0.34.0 policy shape: launch acceptance, proven-build
feature authority, the rendered-header contract, and the strict override.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services import kimi_native_control as knc
from cli_agent_orchestrator.services import kimi_native_launch as knl
from cli_agent_orchestrator.services import provider_contracts as pc

PIN_0340 = "0.34.0"

# The header shape the installed 0.34.0 paints on a real private-tmux capture: every row
# carries the one-cell GutterContainer left pad, and an MCP row follows the
# four proof labels when servers connect.  The geometry is byte-identical to
# the 0.33.0 line; only the Version value changes.
HEADER_0340_ROWS = [
    " ╭────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╮",
    " │  ▐█▛█▛█▌  Welcome to Kimi Code!                                                                                                  │",
    " │  ▐█████▌  Send /help for help information.                                                                                       │",
    " │                                                                                                                                  │",
    " │  Directory: /private/tmp/stage34-probe                                                                                           │",
    " │  Session:   session_9f2c41ab                                                                                                     │",
    " │  Model:     K3                                                                                                                   │",
    " │  Version:   0.34.0                                                                                                               │",
    " │  MCP:       5 connected                                                                                                          │",
    " │                                                                                                                                  │",
    " ╰────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯",
]


# --------------------------------------------------------------------
# The version gates accept 0.34.0 and any semver, but malformed fails closed
# --------------------------------------------------------------------


@pytest.mark.parametrize("banner", ["0.34.0", "kimi 0.34.0"])
def test_check_pinned_version_accepts_the_stage_verified_0340(banner):
    pc.check_pinned_version("kimi", banner)


@pytest.mark.parametrize("banner", ["0.35.0", "kimi 0.35.0", "9.9.9"])
def test_check_pinned_version_accepts_a_future_semver_at_launch_boundary(banner):
    # Open enforcement: normal future updates must not be refused before
    # task bytes just because the pin file has not been updated.
    pc.check_pinned_version("kimi", banner)


@pytest.mark.parametrize("bad", ["kimi", ""])
def test_check_pinned_version_still_refuses_unparseable_builds(bad):
    # Unparseable banners fail closed even in open mode.
    with pytest.raises(pc.ProviderVersionDrift):
        pc.check_pinned_version("kimi", bad)


def test_0340_is_the_current_pin_and_open_enforcement_allows_future_semver():
    assert pc.PINNED_VERSIONS["kimi"] == PIN_0340
    assert pc.SUPPORTED_VERSIONS["kimi"] == (
        PIN_0340,
        "0.33.0",
        "0.32.0",
        "0.31.0",
        "0.30.0",
        "0.29.2",
        "0.29.1",
        "0.29.0",
    )
    assert pc.version_enforcement_mode("kimi") == pc.VERSION_ENFORCEMENT_OPEN


def test_open_admission_keeps_quarantine_membership_exact():
    """Listing is the strict-mode quarantine set, not a capability gate."""
    assert pc.is_listed_version("kimi", PIN_0340)
    assert not pc.is_listed_version("kimi", "0.35.0")
    assert not pc.is_listed_version("unknown", PIN_0340)


# --------------------------------------------------------------------
# Strict override reverts to exact pins after a reproducible regression
# --------------------------------------------------------------------


def test_strict_override_refuses_unproven_semver(monkeypatch):
    monkeypatch.setenv("CAO_PROVIDER_VERSION_ENFORCEMENT_KIMI", "strict")
    assert pc.version_enforcement_mode("kimi") == pc.VERSION_ENFORCEMENT_STRICT
    pc.check_pinned_version("kimi", PIN_0340)
    with pytest.raises(pc.ProviderVersionDrift):
        pc.check_pinned_version("kimi", "0.35.0")


# --------------------------------------------------------------------
# Proven 0.34.0 composer/steer entries, keyed by the compatibility check
# --------------------------------------------------------------------


def test_the_composer_newline_table_has_a_separate_proven_0340_entry():
    entry = knc._PROVEN_COMPOSER_NEWLINE.get(PIN_0340)
    assert entry is not None, "0.34.0 must be a separate keyed entry, never a range"
    assert "d3e781774e7a95f71e9d813e2cda95486d15db73712b3e821dd4a357b0511d8c" in entry["evidence"]
    assert entry["keystroke"] == "C-j"
    assert entry["burst_reset_keystroke"] == "End"
    assert entry["submit_settle_seconds"] == 0.25
    assert entry["normalization"] == knc.NORMALIZATION_JOIN_LF_THEN_TRIM


def test_0340_steer_chords_are_proven_and_unproven_builds_refuse():
    assert knc.steer_chords(PIN_0340) == frozenset({"C-s"})
    assert knc.steer_chords(f"kimi {PIN_0340}") == frozenset({"C-s"})
    # A future semver accepted at launch has no proven steer chord.
    assert knc.steer_chords("0.35.0") == frozenset()


def test_a_multiline_message_on_0340_uses_the_proven_entry():
    plan = knc.plan_composer_keystrokes("line one\nline two", provider_version=PIN_0340)
    assert plan["deliverable"] is True
    assert plan["undeliverable_reason"] is None
    assert plan["soft_newline_keystroke"] == "C-j"
    assert plan["burst_reset_keystroke"] == "End"
    assert plan["submit_settle_seconds"] == 0.25
    assert plan["provider_normalization"] == knc.NORMALIZATION_JOIN_LF_THEN_TRIM
    assert plan["encoding"] == knc.ENCODING_SOFT_NEWLINE


def test_a_future_semver_is_not_silently_accepted_for_composer_control():
    plan = knc.plan_composer_keystrokes("line one\nline two", provider_version="0.35.0")
    assert plan["deliverable"] is False
    assert "0.35.0" in plan["undeliverable_reason"]


# --------------------------------------------------------------------
# The rendered native header is the 0.34.0 bound-session proof
# --------------------------------------------------------------------


def test_the_rendered_session_proof_table_has_a_separate_proven_0340_entry():
    proof = knl.rendered_session_proof_for(PIN_0340)
    assert proof is not None, "0.34.0 rewrites its title; its header proof must be keyed"
    assert proof.provider == "kimi_cli"
    assert proof.rule == knl.RULE_KIMI_NATIVE_HEADER
    assert "d3e781774e7a95f71e9d813e2cda95486d15db73712b3e821dd4a357b0511d8c" in proof.evidence
    assert knl.rendered_session_proof_for(f"kimi {PIN_0340}") == proof


def test_the_0340_header_proves_exactly_the_bound_session_on_the_real_geometry():
    assert knl.renders_session_exactly(
        HEADER_0340_ROWS, "session_9f2c41ab", provider_version=PIN_0340
    )
    assert knl.renders_session_exactly(
        HEADER_0340_ROWS, "session_9f2c41ab", provider_version=f"kimi {PIN_0340}"
    )


def _mutated_rows(*, session: str = "session_9f2c41ab", version: str = PIN_0340) -> list[str]:
    return [
        row.replace("session_9f2c41ab", session).replace("0.34.0", version)
        for row in HEADER_0340_ROWS
    ]


def test_a_header_naming_another_session_proves_nothing():
    assert not knl.renders_session_exactly(
        _mutated_rows(session="session_deadbeef"), "session_9f2c41ab", provider_version=PIN_0340
    )


def test_a_header_naming_another_version_proves_nothing():
    assert not knl.renders_session_exactly(
        _mutated_rows(version="0.33.0"), "session_9f2c41ab", provider_version=PIN_0340
    )
    assert not knl.renders_session_exactly(
        HEADER_0340_ROWS, "session_9f2c41ab", provider_version="0.33.0"
    )


def test_a_missing_or_duplicated_label_fails_closed():
    without_session = [row for row in HEADER_0340_ROWS if "Session:" not in row]
    assert not knl.renders_session_exactly(
        without_session, "session_9f2c41ab", provider_version=PIN_0340
    )
    duplicated = HEADER_0340_ROWS + [
        " │  Session:   session_9f2c41ab                                                                                                     │"
    ]
    assert not knl.renders_session_exactly(
        duplicated, "session_9f2c41ab", provider_version=PIN_0340
    )


def test_a_future_semver_cannot_inherit_the_0340_proof():
    assert not knl.renders_session_exactly(
        HEADER_0340_ROWS, "session_9f2c41ab", provider_version="0.35.0"
    )
    assert knl.rendered_session_proof_for("0.35.0") is None


# --------------------------------------------------------------------
# The exact resume contract is unchanged on 0.34.0
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
