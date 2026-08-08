"""Exact Kimi Code 0.33.0 support as a retained stage-verified build (cond-0315).

The operator's binary self-updated a second time — 0.32.0 to 0.33.0 — during
the 0.32.0 stage verification itself: the provider's supported background
updater installed 0.33.0 at 2026-08-05T09:54:49Z, ~2 s after an interactive
0.32.0 stage pane made the device eligible (``~/.kimi-code/updates/``
``rollout.log`` ``startup-cache reason:"eligible"`` and
``install.json.lastSuccess``).  The managed launcher then refused 0.33.0
fail-closed at every gate, exactly as designed.  0.33.0 is admitted as a
*separate proven build*, keyed by its installed bundle digest
(``dist/main.mjs`` sha256
``0e77b9c64e67a4eecb96aae011750668aab11bd781564fe3e4855513812247b2``,
matching the npm-published digest), only after stage verification proved
the version-specific behaviour the managed launcher relies on:

* the composer keybinds, the ``expandPasteMarkers(lines.join("\\n")).trim()``
  submit computation, the 120 ms paste-burst window with content-neutral
  reset, the ``Key.ctrl("s")`` steer chord, the ``process.title`` rewrite,
  the ``-S, --session [id]``/``-r, --resume [id]`` forms, and the boot
  header ``infoLines`` all read byte-identical to the attested 0.32.0
  bundle (npm tarballs compared);
* the ACP surface — reimplemented natively in 0.33.0
  (``registerNativeAcpCommand``/``KIMI_CODE_LEGACY_FLAG`` exist only in its
  bundle, so 0.32.0's ACP evidence does not transfer by bytes) — was proven
  live and zero-prompt: ``agentInfo.version`` ``0.33.0`` agrees with the
  executable, ``session/new`` minted a session, the exact ``kimi-code/k3``
  + ``max`` route read back, and the durable ACP
  session/new → kill → session/load proof
  (``kimi_acp_proof.run_identity_proof``) passes on the installed binary;
* a private-tmux stage probe (disposable socket/worktree, zero task bytes,
  ``KIMI_CODE_NO_AUTO_UPDATE=1``) resumed the ACP-minted session with no
  picker, showed the kernel argv rewritten to ``['kimi-code','','','']``,
  held a stable pane/pid/start-marker, and rendered the strict boot header
  — ``Directory``/``Session``/``Model``/``Version`` each exactly once, plus
  an ignorable ``MCP:`` row — inside the one-cell ``GutterContainer`` the
  parser now tolerates;
* the bounded ``--version`` probe answers ``0.33.0`` in 0.38–0.47 s warm.

Because this is the fourth self-update freeze, every CAO-managed Kimi
child process now runs with the provider's own deterministic kill-switch
in its environment: ``KIMI_CODE_NO_AUTO_UPDATE=1`` is the first check of
the provider's update preflight (bundle-read), skipping the update check,
the background install, and the pre-boot update prompt; it was
live-verified to write zero updater-state across ``--version``, ACP, and
an interactive TUI boot.  It is a per-process atomicity fence scoped to
CAO-managed child environments — it keeps the one process a launch
selected self-identical until that process exits, and it neither freezes
nor manages the operator's PATH installation, which stays free to update
on its own schedule (see
``provider_contracts.kimi_update_suppression_env``).

This suite pins the 0.33.0-specific proofs (same shape as the 0.32.0
suite): exact-set version admission, digest-keyed composer/steer entries,
the rendered-header proof on the real painted geometry (gutter + optional
``MCP:`` row), and the unchanged exact resume contract.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services import control_input_service as cis
from cli_agent_orchestrator.services import kimi_native_control as knc
from cli_agent_orchestrator.services import kimi_native_launch as knl
from cli_agent_orchestrator.services import native_pane_input as npi
from cli_agent_orchestrator.services import provider_contracts as pc

PIN_0330 = "0.33.0"
BUNDLE_SHA256 = "0e77b9c64e67a4eecb96aae011750668aab11bd781564fe3e4855513812247b2"

# The header shape the installed 0.33.0 paints on a real capture: every row
# carries the one-cell GutterContainer left pad, and an MCP row follows the
# four proof labels when servers connect.
HEADER_0330_ROWS = [
    " ╭────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╮",
    " │  ▐█▛█▛█▌  Welcome to Kimi Code!                                                                                                  │",
    " │  ▐█████▌  Send /help for help information.                                                                                       │",
    " │                                                                                                                                  │",
    " │  Directory: /private/tmp/stage33-probe                                                                                           │",
    " │  Session:   session_9f2c41ab                                                                                                     │",
    " │  Model:     K3                                                                                                                   │",
    " │  Version:   0.33.0                                                                                                               │",
    " │  MCP:       5 connected                                                                                                          │",
    " │                                                                                                                                  │",
    " ╰────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯",
]


# --------------------------------------------------------------------
# The version gates accept exact 0.33.0 and refuse every neighbour
# --------------------------------------------------------------------


@pytest.mark.parametrize("banner", ["0.33.0", "kimi 0.33.0"])
def test_check_pinned_version_accepts_the_stage_verified_0330(banner):
    pc.check_pinned_version("kimi", banner)


@pytest.mark.parametrize("bad", ["kimi", ""])
def test_check_pinned_version_still_refuses_unknown_and_unparseable_builds(bad):
    # In open enforcement, unparseable banners fail closed.  Semver-shaped
    # versions outside the proven set are accepted at the launch boundary
    # but do not inherit feature-specific authority.
    with pytest.raises(pc.ProviderVersionDrift):
        pc.check_pinned_version("kimi", bad)


@pytest.mark.parametrize("version", ["0.32.1", "0.33.1", "1.0.0"])
def test_semver_neighbours_launch_but_get_no_composer_authority(version):
    pc.check_pinned_version("kimi", version)
    plan = knc.plan_composer_keystrokes("line one\nline two", provider_version=version)
    assert plan["deliverable"] is False
    assert version in plan["undeliverable_reason"]
    assert knc.steer_chords(version) == frozenset()


def test_0330_is_a_retained_proven_build_and_every_attested_build_is_kept():
    # 0.34.0 is the current pin under the cond-0331 open policy; 0.33.0
    # remains a proven build and keeps every capability it was verified for.
    assert pc.PINNED_VERSIONS["kimi"] == "0.34.0"
    assert PIN_0330 in pc.SUPPORTED_VERSIONS["kimi"]
    assert pc.SUPPORTED_VERSIONS["kimi"] == (
        "0.34.0",
        PIN_0330,
        "0.32.0",
        "0.31.0",
        "0.30.0",
        "0.29.2",
        "0.29.1",
        "0.29.0",
    )


# --------------------------------------------------------------------
# The updater kill-switch is a managed-launch environment invariant
# --------------------------------------------------------------------


def test_the_update_suppression_environment_is_the_provider_kill_switch():
    env = pc.kimi_update_suppression_env()
    assert env == {"KIMI_CODE_NO_AUTO_UPDATE": "1"}
    # A fresh mapping per call: no caller can mutate the invariant for another.
    assert pc.kimi_update_suppression_env() is not env


# --------------------------------------------------------------------
# Separate proven 0.33.0 composer/steer entries, keyed by the bundle sha256
# --------------------------------------------------------------------


def test_the_composer_newline_table_has_a_separate_proven_0330_entry():
    entry = knc._PROVEN_COMPOSER_NEWLINE.get(PIN_0330)
    assert entry is not None, "0.33.0 must be a separate keyed entry, never a range"
    assert BUNDLE_SHA256 in entry["evidence"]
    assert entry["keystroke"] == "C-j"
    assert entry["burst_reset_keystroke"] == "End"
    assert entry["submit_settle_seconds"] == 0.25
    assert entry["normalization"] == knc.NORMALIZATION_JOIN_LF_THEN_TRIM


def test_0330_steer_chords_are_proven_and_unproven_builds_refuse():
    assert knc.steer_chords(PIN_0330) == frozenset({"C-s"})
    assert knc.steer_chords(f"kimi {PIN_0330}") == frozenset({"C-s"})
    assert knc.steer_chords("0.33.1") == frozenset()


def test_a_multiline_message_on_0330_uses_the_proven_entry():
    plan = knc.plan_composer_keystrokes("line one\nline two", provider_version=PIN_0330)
    assert plan["deliverable"] is True
    assert plan["undeliverable_reason"] is None
    assert plan["soft_newline_keystroke"] == "C-j"
    assert plan["burst_reset_keystroke"] == "End"
    assert plan["submit_settle_seconds"] == 0.25
    assert plan["provider_normalization"] == knc.NORMALIZATION_JOIN_LF_THEN_TRIM
    assert plan["encoding"] == knc.ENCODING_SOFT_NEWLINE


def test_an_unproven_neighbouring_version_is_not_silently_accepted():
    plan = knc.plan_composer_keystrokes("line one\nline two", provider_version="0.33.1")
    assert plan["deliverable"] is False
    assert "0.33.1" in plan["undeliverable_reason"]


# --------------------------------------------------------------------
# The rendered native header is the 0.33.0 bound-session proof (COND-0312)
# --------------------------------------------------------------------


def test_the_rendered_session_proof_table_has_a_separate_proven_0330_entry():
    proof = knl.rendered_session_proof_for(PIN_0330)
    assert proof is not None, "0.33.0 rewrites its title; its header proof must be keyed"
    assert proof.provider == "kimi_cli"
    assert proof.rule == knl.RULE_KIMI_NATIVE_HEADER
    assert BUNDLE_SHA256 in proof.evidence
    assert knl.rendered_session_proof_for(f"kimi {PIN_0330}") == proof


def test_the_0330_header_proves_exactly_the_bound_session_on_the_real_geometry():
    assert knl.renders_session_exactly(
        HEADER_0330_ROWS, "session_9f2c41ab", provider_version=PIN_0330
    )
    assert knl.renders_session_exactly(
        HEADER_0330_ROWS, "session_9f2c41ab", provider_version=f"kimi {PIN_0330}"
    )


def _mutated_rows(*, session: str = "session_9f2c41ab", version: str = PIN_0330) -> list[str]:
    return [
        row.replace("session_9f2c41ab", session).replace("0.33.0", version)
        for row in HEADER_0330_ROWS
    ]


def test_a_header_naming_another_session_proves_nothing():
    assert not knl.renders_session_exactly(
        _mutated_rows(session="session_deadbeef"), "session_9f2c41ab", provider_version=PIN_0330
    )


def test_a_header_naming_another_version_proves_nothing():
    assert not knl.renders_session_exactly(
        _mutated_rows(version="0.32.0"), "session_9f2c41ab", provider_version=PIN_0330
    )
    assert not knl.renders_session_exactly(
        HEADER_0330_ROWS, "session_9f2c41ab", provider_version="0.32.0"
    )


def test_a_missing_or_duplicated_label_fails_closed():
    without_session = [row for row in HEADER_0330_ROWS if "Session:" not in row]
    assert not knl.renders_session_exactly(
        without_session, "session_9f2c41ab", provider_version=PIN_0330
    )
    duplicated = HEADER_0330_ROWS + [
        " │  Session:   session_9f2c41ab                                                                                                     │"
    ]
    assert not knl.renders_session_exactly(
        duplicated, "session_9f2c41ab", provider_version=PIN_0330
    )


def test_an_unproven_build_cannot_inherit_the_0330_proof():
    assert not knl.renders_session_exactly(
        HEADER_0330_ROWS, "session_9f2c41ab", provider_version="0.33.1"
    )
    assert knl.rendered_session_proof_for("0.33.1") is None


# --------------------------------------------------------------------
# The exact resume contract is unchanged on 0.33.0
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


# --------------------------------------------------------------------
# The 0.33.0 composer-observation pin is build-exact (cond-0332)
# --------------------------------------------------------------------


def test_the_composer_observation_table_has_a_separate_proven_0330_entry():
    pin = npi.composer_observation_pin_for("kimi_cli", PIN_0330)
    assert pin is not None, "0.33.0 must be a separate keyed observation pin, never a range"
    assert pin.provider == "kimi_cli"
    assert pin.rule == npi._RULE_KIMI_COMPOSER_BOX
    assert pin.composer_tail_rows == 5
    assert BUNDLE_SHA256 in pin.evidence


def test_0330_composer_observation_is_build_exact():
    # Adjacent and otherwise-unverified builds must not inherit the 0.33.0 pin.
    for version in ("0.32.0", "0.33.1", "0.34.0"):
        assert npi.composer_observation_pin_for("kimi_cli", version) is None


def test_0330_advertises_composer_observation_and_unpinned_neighbours_do_not():
    resolved = cis.ResolvedControlIdentity(
        terminal_id="1ca9d289",
        terminal_incarnation=None,
        terminal_generation="gen-0332",
        provider="kimi_cli",
        native_session_id="session_0a1c081e-e252-4e96-9932-18137717c3b9",
        execution_mode=cis.EXECUTION_MODE_NATIVE_TUI,
        session_name="cao",
        provider_version=PIN_0330,
        managed_reservation_id="681ece98-c52c-4f17-b5e3-7148df41676e",
        pane_id="%72",
        window_id="@1",
        pane_pid=67059,
        managed=True,
    )
    assert cis._composer_observation_supported(resolved) is None

    for version in ("0.32.0", "0.33.1", "0.34.0"):
        unsupported = cis.ResolvedControlIdentity(
            **{**resolved.__dict__, "provider_version": version}
        )
        reason = cis._composer_observation_supported(unsupported)
        assert reason is not None
        assert "has no pinned composer observation layout" in reason
