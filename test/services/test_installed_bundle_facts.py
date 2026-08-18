"""Runtime bundle reads as the §3 override for missing per-build pins.

The extractors are tested against fixture bundles that model the relevant
region of each provider's real bundle — the exact byte patterns the
stage-verified pins recorded as evidence — never against the expectation
itself.  Version binding is tested end-to-end through ``shutil.which``:
a hint read from a bundle whose own version is not the build being driven
is the neighbour-inheritance the per-build tables existed to prevent.
"""

from __future__ import annotations

import json
import os

import pytest

from cli_agent_orchestrator.services import claude_native_control as claude_control
from cli_agent_orchestrator.services import installed_bundle_facts as ibf
from cli_agent_orchestrator.services import kimi_native_control as kimi_control
from cli_agent_orchestrator.services import kimi_native_launch
from cli_agent_orchestrator.services import muse_native_control as muse_control

KIMI_BUNDLE_WITH_HINTS = (
    b'"tui.input.newLine": {\n\t\t\tdefaultKeys: ["shift+enter", "ctrl+j"],\n\t\t}\n'
    b'if (matchesKey(normalized, Key.ctrl("s"))) { steer(); }\n'
    b"function main() {\n\tprocess.title = PROCESS_NAME;\n}\n"
    b'rows = [{label: "Model"}, {label: "Directory"}, {label: "Session"}, {label: "Version"}]\n'
)
KIMI_BUNDLE_WITHOUT_HINTS = b"function main() { boot(); }\n"
# The 0.36.1-shape bundle: the title rewrite is present but the header
# layout changed (no Version label), so neither the argv proof nor the
# known rendered proof applies.
KIMI_BUNDLE_REWRITTEN_UNKNOWN_LAYOUT = (
    b"function main() {\n\tprocess.title = PROCESS_NAME;\n}\n"
    b'rows = [{label: "Model"}, {label: "Directory"}, {label: "Session"}]\n'
)
CLAUDE_BUNDLE_WITH_HINT = b"footer hints: ctrl+j for newline; enter to submit"
CLAUDE_BUNDLE_WITHOUT_HINT = b"footer hints: enter to submit"
MUSE_BUNDLE_WITH_HINT = (
    b"keymap...submitSubmitenterSubmit the composer message."
    b"newlineshift+enterctrl+jctrl+mInsert a newline without submitting.queue..."
)
MUSE_BUNDLE_WITHOUT_HINT = b"keymap...submitSubmitenterSubmit the composer message."


def _kimi_tree(tmp_path, content: bytes, version: str = "9.9.9") -> str:
    package = tmp_path / "kimi-package"
    (package / "dist").mkdir(parents=True)
    bundle = package / "dist" / "main.mjs"
    bundle.write_bytes(content)
    (package / "package.json").write_text(json.dumps({"version": version}))
    link = tmp_path / "bin" / "kimi"
    link.parent.mkdir(exist_ok=True)
    link.symlink_to(bundle)
    return str(link)


def _claude_tree(tmp_path, content: bytes, version: str = "9.9.9") -> str:
    package = tmp_path / "claude-package"
    (package / "bin").mkdir(parents=True)
    bundle = package / "bin" / "claude.exe"
    bundle.write_bytes(content)
    (package / "package.json").write_text(json.dumps({"version": version}))
    link = tmp_path / "bin" / "claude"
    link.parent.mkdir(exist_ok=True)
    link.symlink_to(bundle)
    return str(link)


def _muse_tree(tmp_path, content: bytes, full_version: str = "9.9.9-R1.1") -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    launcher = bin_dir / "muse"
    launcher.write_text("#!/bin/sh\n# the update-capable launcher, never evidence\n")
    (bin_dir / f"muse-bin-{full_version}").write_bytes(content)
    return str(launcher)


def _patch_which(monkeypatch, mapping):
    monkeypatch.setattr(ibf.shutil, "which", lambda name: mapping.get(os.path.basename(name)))


@pytest.fixture
def bundle_trees(tmp_path, monkeypatch):
    """All three providers installed at the fixture build 9.9.9, with hints."""
    mapping = {
        "kimi": _kimi_tree(tmp_path, KIMI_BUNDLE_WITH_HINTS),
        "claude": _claude_tree(tmp_path, CLAUDE_BUNDLE_WITH_HINT),
        "muse": _muse_tree(tmp_path, MUSE_BUNDLE_WITH_HINT),
    }
    _patch_which(monkeypatch, mapping)
    return mapping


class TestNewlineKeystrokeHint:
    def test_the_claude_hint_reads_as_ctrl_j(self, bundle_trees):
        fact = ibf.newline_keystroke_hint("claude_code", "9.9.9 (Claude Code)")
        assert fact is not None
        assert fact.keystroke == "C-j"
        assert fact.bundle_version == "9.9.9"

    def test_the_kimi_hint_reads_as_ctrl_j(self, bundle_trees):
        fact = ibf.newline_keystroke_hint("kimi_cli", "kimi 9.9.9")
        assert fact is not None
        assert fact.keystroke == "C-j"

    def test_the_muse_hint_reads_as_ctrl_j(self, bundle_trees):
        fact = ibf.newline_keystroke_hint("muse_cli", "Muse Code 9.9.9 (9.9.9-R1.1)")
        assert fact is not None
        assert fact.keystroke == "C-j"
        assert fact.bundle_version == "9.9.9-R1.1"

    def test_a_version_mismatch_is_never_a_hint(self, tmp_path, monkeypatch):
        """The bundle's own version must equal the build being driven."""
        _patch_which(
            monkeypatch,
            {"claude": _claude_tree(tmp_path, CLAUDE_BUNDLE_WITH_HINT, version="9.9.9")},
        )
        assert ibf.newline_keystroke_hint("claude_code", "9.9.8 (Claude Code)") is None

    def test_a_bundle_without_the_hint_is_no_hint(self, tmp_path, monkeypatch):
        _patch_which(monkeypatch, {"claude": _claude_tree(tmp_path, CLAUDE_BUNDLE_WITHOUT_HINT)})
        assert ibf.newline_keystroke_hint("claude_code", "9.9.9") is None

    def test_an_unparseable_version_is_no_hint(self, bundle_trees):
        assert ibf.newline_keystroke_hint("claude_code", "garbage") is None
        assert ibf.newline_keystroke_hint("claude_code", None) is None

    def test_codex_has_no_readable_hint(self, bundle_trees):
        """The Codex binary compiles its keymap as data: no binding string
        exists to read, so there is no derivation and the refusal stands."""
        assert ibf.newline_keystroke_hint("codex", "0.148.0") is None


class TestKimiSteerChordsHint:
    def test_the_steer_dispatch_reads_as_ctrl_s(self, bundle_trees):
        assert ibf.kimi_steer_chords_hint("kimi 9.9.9") == frozenset({"C-s"})

    def test_a_bundle_without_the_dispatch_is_no_chords(self, tmp_path, monkeypatch):
        _patch_which(monkeypatch, {"kimi": _kimi_tree(tmp_path, KIMI_BUNDLE_WITHOUT_HINTS)})
        assert ibf.kimi_steer_chords_hint("9.9.9") == frozenset()

    def test_the_adapter_derives_chords_for_an_unlisted_build(self, bundle_trees):
        assert kimi_control.steer_chords("kimi 9.9.9") == frozenset({"C-s"})

    def test_the_adapter_keeps_the_table_answer_for_listed_builds(self, bundle_trees):
        assert kimi_control.steer_chords("kimi 0.29.2") == frozenset({"C-s"})


class TestKimiRenderedHeaderHint:
    def test_the_known_layout_is_derived(self, bundle_trees):
        evidence = ibf.kimi_rendered_header_hint("kimi 9.9.9")
        assert evidence is not None
        assert "9.9.9" in evidence

    def test_the_proof_records_itself_as_bundle_derived(self, bundle_trees):
        proof = kimi_native_launch.rendered_session_proof_for("kimi 9.9.9")
        assert proof is not None
        assert proof.rule == kimi_native_launch.RULE_KIMI_NATIVE_HEADER
        assert "runtime read" in proof.evidence

    def test_a_rewritten_title_with_an_unknown_layout_is_no_proof(self, tmp_path, monkeypatch):
        """The argv proof is known-erased for this build and the known
        header layout is absent: no proof applies, so the surface records
        unproven rather than guessing at a header."""
        _patch_which(
            monkeypatch,
            {"kimi": _kimi_tree(tmp_path, KIMI_BUNDLE_REWRITTEN_UNKNOWN_LAYOUT)},
        )
        assert kimi_native_launch.rendered_session_proof_for("kimi 9.9.9") is None

    def test_the_table_answer_wins_for_listed_builds(self, bundle_trees):
        proof = kimi_native_launch.rendered_session_proof_for("kimi 0.34.0")
        assert proof is not None
        assert "0.34.0" in proof.evidence


class TestComposerPlanDerivation:
    """An unlisted build gets multiline delivery with a non-null default —
    and the plan says the value is derived, not measured."""

    def test_claude_unlisted_build_delivers_multiline_from_the_bundle(self, bundle_trees):
        plan = claude_control.plan_composer_keystrokes("one\ntwo", provider_version="9.9.9")
        assert plan["deliverable"] is True
        assert plan["soft_newline_keystroke"] == "C-j"
        # The floor, not the null value — and marked as not a measurement.
        assert plan["submit_settle_seconds"] == claude_control._SUBMIT_SETTLE_FLOOR_SECONDS
        assert plan["submit_settle_seconds"] > 0
        assert plan["submit_settle_proven"] is False
        assert plan["composer_keystroke_source"] == "installed-bundle-hint"
        assert plan["composer_evidence"] is not None

    def test_kimi_unlisted_build_delivers_multiline_from_the_bundle(self, bundle_trees):
        plan = kimi_control.plan_composer_keystrokes("one\ntwo", provider_version="kimi 9.9.9")
        assert plan["deliverable"] is True
        assert plan["soft_newline_keystroke"] == "C-j"
        assert plan["submit_settle_seconds"] == kimi_control._SUBMIT_SETTLE_FLOOR_SECONDS
        assert plan["submit_settle_seconds"] > 0
        assert plan["submit_settle_proven"] is False
        assert plan["composer_keystroke_source"] == "installed-bundle-hint"

    def test_muse_unlisted_build_delivers_multiline_from_the_bundle(self, bundle_trees):
        plan = muse_control.plan_composer_keystrokes(
            "one\ntwo", provider_version="Muse Code 9.9.9 (9.9.9-R1.1)"
        )
        assert plan["deliverable"] is True
        assert plan["soft_newline_keystroke"] == "C-j"
        assert plan["composer_keystroke_source"] == "installed-bundle-hint"

    def test_kimi_derived_plan_states_no_digest_it_cannot_stand_behind(self, bundle_trees):
        """The derived build's submit normalization was never read, so a
        payload trimming would change records no model-input digest."""
        plan = kimi_control.plan_composer_keystrokes(
            "  padded\ntwo  ", provider_version="kimi 9.9.9"
        )
        assert plan["deliverable"] is True
        assert plan["model_input_sha256"] is None
        assert plan["model_input_is_composer_exact"] is None
        # A trim-invariant payload cannot be changed by trimming, so its
        # digest is stated.
        invariant = kimi_control.plan_composer_keystrokes("one\ntwo", provider_version="kimi 9.9.9")
        assert invariant["model_input_sha256"] is not None
        assert invariant["model_input_is_composer_exact"] is True

    def test_a_listed_build_still_reads_proven_build_table(self, bundle_trees):
        plan = claude_control.plan_composer_keystrokes("one\ntwo", provider_version="2.1.220")
        assert plan["composer_keystroke_source"] == "proven-build-table"
        assert plan["submit_settle_proven"] is True

    def test_an_unreadable_bundle_keeps_the_typed_refusal(self, tmp_path, monkeypatch):
        """The fallback is the refusal, never a guessed keystroke."""
        _patch_which(monkeypatch, {"claude": _claude_tree(tmp_path, CLAUDE_BUNDLE_WITHOUT_HINT)})
        plan = claude_control.plan_composer_keystrokes("one\ntwo", provider_version="9.9.9")
        assert plan["deliverable"] is False
        assert plan["soft_newline_keystroke"] is None
        assert "none could be read from the installed bundle" in plan["undeliverable_reason"]

    def test_codex_unlisted_build_keeps_the_typed_refusal(self, bundle_trees):
        from cli_agent_orchestrator.services import codex_native_control

        plan = codex_native_control.plan_composer_keystrokes(
            "one\ntwo", provider_version="codex-cli 0.148.0"
        )
        assert plan["deliverable"] is False
        assert plan["undeliverable_reason"] is not None


# The paste-burst reset dispatch, byte-identical to the statement the
# proven Kimi rows record as their ``burst_reset_keystroke`` evidence and
# present verbatim in the installed 0.36.1 dist/main.mjs.
KIMI_RESET_DISPATCH = (
    b"if (!this.disablePasteBurst && !isEnterKey && printableForBurst === void 0) "
    b"this.pasteBurst.reset();\n"
)
KIMI_BUNDLE_WITH_HINTS_AND_RESET = KIMI_BUNDLE_WITH_HINTS + KIMI_RESET_DISPATCH


class TestKimiBurstResetDerivation:
    """A bundle-derived Kimi plan must not rest the submit on the settle alone.

    The reset keystroke and the settle are independent, non-redundant
    defences (the adapter's table docstring): if the event loop stalls
    past the settle, the trailing printables and the Enter arrive in one
    batched pty read and the sleep bought nothing, while a reset key in
    that same batch still zeroes the suppression window first.  A derived
    plan therefore carries ``End`` only when this build's own bundle
    carries the reset dispatch — derived, never assumed.
    """

    def test_a_derived_plan_carries_the_bundle_read_reset_keystroke(self, tmp_path, monkeypatch):
        _patch_which(monkeypatch, {"kimi": _kimi_tree(tmp_path, KIMI_BUNDLE_WITH_HINTS_AND_RESET)})
        plan = kimi_control.plan_composer_keystrokes("one\ntwo", provider_version="kimi 9.9.9")
        assert plan["composer_keystroke_source"] == "installed-bundle-hint"
        assert plan["burst_reset_keystroke"] == "End"
        assert "paste-burst reset dispatch" in plan["composer_evidence"]

    def test_the_derived_reset_key_is_sent_before_the_enter(self, tmp_path, monkeypatch):
        """The ingredient is pinned above; this pins the wiring: the plan's
        reset key is what ``submit_composer_plan`` actually sends, before
        the settle and the Enter."""
        _patch_which(monkeypatch, {"kimi": _kimi_tree(tmp_path, KIMI_BUNDLE_WITH_HINTS_AND_RESET)})
        monkeypatch.setattr(kimi_control.time, "sleep", lambda _seconds: None)
        plan = kimi_control.plan_composer_keystrokes("one\ntwo", provider_version="kimi 9.9.9")

        sent: list[tuple[str, str]] = []

        class _Transport:
            def send_key(self, key):
                sent.append(("key", key))

            def send_literal(self, text):
                sent.append(("literal", text))

            def send_enter(self):
                sent.append(("enter", ""))

        kimi_control.submit_composer_plan(plan=plan, transport=_Transport())
        assert sent == [("key", "End"), ("enter", "")]

    def test_a_bundle_without_the_reset_dispatch_keeps_no_reset_key(self, tmp_path, monkeypatch):
        """The newline declaration reads but the reset dispatch does not:
        the settle-only boundary, never an assumed key."""
        _patch_which(monkeypatch, {"kimi": _kimi_tree(tmp_path, KIMI_BUNDLE_WITH_HINTS)})
        plan = kimi_control.plan_composer_keystrokes("one\ntwo", provider_version="kimi 9.9.9")
        assert plan["composer_keystroke_source"] == "installed-bundle-hint"
        assert plan["burst_reset_keystroke"] is None

    def test_a_listed_build_keeps_its_table_reset_key(self, bundle_trees):
        plan = kimi_control.plan_composer_keystrokes("one\ntwo", provider_version="kimi 0.29.2")
        assert plan["composer_keystroke_source"] == "proven-build-table"
        assert plan["burst_reset_keystroke"] == "End"


class TestBundleReadOSError:
    """A bundle that stats but cannot be read keeps the typed refusal.

    Permissions, a mid-read unlink, an unreadable mount: an ``OSError``
    out of the extractor must not propagate past the seam whose route
    promises a typed outcome for every terminal-level failure.  The
    existing "unreadable bundle" case models a *successful* read yielding
    no needle; this one models the read itself failing.
    """

    def test_an_oserror_reading_the_bundle_keeps_the_typed_refusal(self, tmp_path, monkeypatch):
        link = _claude_tree(tmp_path, CLAUDE_BUNDLE_WITH_HINT)
        _patch_which(monkeypatch, {"claude": link})
        bundle_path = os.path.realpath(link)
        real_open = open

        def _unreadable(path, *args, **kwargs):
            if str(path) == bundle_path:
                raise PermissionError(13, "Permission denied", str(path))
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(ibf, "open", _unreadable, raising=False)

        # The hint read degrades to None, and the plan keeps its typed
        # refusal — no OSError escapes toward the route.
        assert ibf.newline_keystroke_hint("claude_code", "9.9.9") is None
        plan = claude_control.plan_composer_keystrokes("one\ntwo", provider_version="9.9.9")
        assert plan["deliverable"] is False
        assert "none could be read from the installed bundle" in plan["undeliverable_reason"]


class TestKimiSessionProofGap:
    """The proven negative: a build whose own bundle rules out both proofs.

    A gap is established only from a version-matched bundle showing the
    argv-erasing title rewrite *and* an incomplete header label set — the
    0.36.1 shape.  Everything else answers ``None``: argv-preserving
    builds, complete headers, and builds the installed bundle says nothing
    about (unknown is answered by the launch's runtime proofs, not by a
    refusal).
    """

    def test_a_rewritten_title_with_an_unknown_layout_is_a_proven_gap(self, tmp_path, monkeypatch):
        _patch_which(
            monkeypatch, {"kimi": _kimi_tree(tmp_path, KIMI_BUNDLE_REWRITTEN_UNKNOWN_LAYOUT)}
        )
        gap = ibf.kimi_session_proof_gap("kimi 9.9.9")
        assert gap is not None
        assert "9.9.9" in gap

    def test_an_argv_preserving_build_has_no_gap(self, tmp_path, monkeypatch):
        """No title rewrite: the kernel argv still names the resumed session."""
        _patch_which(monkeypatch, {"kimi": _kimi_tree(tmp_path, KIMI_BUNDLE_WITHOUT_HINTS)})
        assert ibf.kimi_session_proof_gap("kimi 9.9.9") is None

    def test_a_complete_header_has_no_gap(self, tmp_path, monkeypatch):
        _patch_which(monkeypatch, {"kimi": _kimi_tree(tmp_path, KIMI_BUNDLE_WITH_HINTS)})
        assert ibf.kimi_session_proof_gap("kimi 9.9.9") is None

    def test_a_build_the_bundle_says_nothing_about_has_no_gap(self, bundle_trees):
        """The installed fixture bundle is 9.9.9; the driven build is not."""
        assert ibf.kimi_session_proof_gap("kimi 9.9.8") is None

    def test_the_launch_wrapper_prefers_a_table_proof(self, tmp_path, monkeypatch):
        """A table-row build keeps its proof even when the installed bundle
        of that exact build reads as the gap shape: any proof closes the
        gap question before the bundle read is consulted."""
        _patch_which(
            monkeypatch,
            {"kimi": _kimi_tree(tmp_path, KIMI_BUNDLE_REWRITTEN_UNKNOWN_LAYOUT, version="0.34.0")},
        )
        assert kimi_native_launch.session_proof_gap_for("kimi 0.34.0") is None

    def test_the_launch_wrapper_surfaces_the_gap_for_an_unlisted_build(self, tmp_path, monkeypatch):
        _patch_which(
            monkeypatch, {"kimi": _kimi_tree(tmp_path, KIMI_BUNDLE_REWRITTEN_UNKNOWN_LAYOUT)}
        )
        assert kimi_native_launch.session_proof_gap_for("kimi 9.9.9") is not None
