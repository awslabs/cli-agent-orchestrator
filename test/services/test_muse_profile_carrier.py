"""Runtime self-proof authority for Muse's internal profile carrier."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services import muse_native_launch as muse
from cli_agent_orchestrator.services.managed_launch import ManagedLaunchConflict

BANNER = "Muse Code 0.2.1 (0.2.1-R1215.1)"
REVISION = "0.2.1-R1215.1"


def _make_probe_stub(
    tmp_path,
    *,
    behavior: str = "normal",
    revision: str = REVISION,
):
    """Create a wrapper and inner binary stub that models real Muse behavior."""
    wrapper = tmp_path / "muse"
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    wrapper.chmod(0o755)
    (tmp_path / ".muse-version").write_text(revision, encoding="utf-8")
    inner = tmp_path / f"muse-bin-{revision}"

    if behavior == "normal":
        script = f"""#!{sys.executable}
import os, sys
from pathlib import Path
env_path = os.environ.get("{muse.PROFILE_SYSTEM_PROMPT_ENV}")
if env_path is not None:
    p = Path(env_path)
    if not p.exists():
        sys.stderr.write(f"failed to read {muse.PROFILE_SYSTEM_PROMPT_ENV} path `{{env_path}}`: No such file or directory (os error 2)\\n")
        sys.exit(1)
    if p.stat().st_size > 0:
        sys.stderr.write("{muse.CARRIER_PROBE_REFUSAL}\\n")
        sys.exit(1)
    sys.stderr.write("muse: workspace root: /tmp\\n")
    sys.stdout.write("echo: ping\\n")
    sys.exit(0)
sys.stderr.write("muse: workspace root: /tmp\\n")
sys.stdout.write("echo: ping\\n")
sys.exit(0)
"""
    elif behavior == "disproved":
        script = f"""#!{sys.executable}
import sys
sys.stderr.write("muse: workspace root: /tmp\\n")
sys.stdout.write("echo: ping\\n")
sys.exit(0)
"""
    elif behavior == "usage_dump":
        script = f"""#!{sys.executable}
import sys
sys.stderr.write("unknown preset <name>; expected native-basic|miniswe\\n")
sys.exit(2)
"""
    elif behavior == "missing_file_error":
        script = f"""#!{sys.executable}
import sys
sys.stderr.write("failed to read TBH_EVAL_APPEND_SYSTEM_PROMPT_FILE path `/tmp/fake`: No such file or directory (os error 2)\\n")
sys.exit(1)
"""
    elif behavior == "unrecognized_error":
        script = f"""#!{sys.executable}
import sys
sys.stderr.write("fatal: unexpected internal error\\n")
sys.exit(1)
"""
    elif behavior == "control_fails":
        script = f"""#!{sys.executable}
import os, sys
from pathlib import Path
env_path = os.environ.get("{muse.PROFILE_SYSTEM_PROMPT_ENV}")
if env_path is not None and Path(env_path).exists() and Path(env_path).stat().st_size > 0:
    sys.stderr.write("{muse.CARRIER_PROBE_REFUSAL}\\n")
    sys.exit(1)
sys.stderr.write("control leg crash\\n")
sys.exit(1)
"""
    else:
        raise ValueError(f"Unknown behavior: {behavior}")

    inner.write_text(script, encoding="utf-8")
    inner.chmod(0o755)
    return wrapper, inner


def test_profile_carrier_digest_reads_a_large_inner_file_correctly(tmp_path):
    inner = tmp_path / "muse-bin-fixture"
    payload = (b"muse carrier digest\n" * 196_608) + b"tail"
    inner.write_bytes(payload)

    assert muse._sha256_file(str(inner)) == hashlib.sha256(payload).hexdigest()


def test_probe_truth_table_probed_row(tmp_path):
    wrapper, inner = _make_probe_stub(tmp_path, behavior="normal")
    proof, detail = muse.probe_profile_carrier(str(inner))

    assert proof == muse.PROOF_PROBED
    assert detail == ""

    capability = muse.profile_carrier_capability(
        wrapper_executable=str(wrapper), full_banner=BANNER
    )
    assert capability.supported is True
    assert capability.proof == muse.PROOF_PROBED
    assert capability.reason == ""
    assert capability.cell is None
    assert capability.inner_executable == str(inner)
    assert capability.inner_executable_sha256 == muse._sha256_file(str(inner))


def test_probe_truth_table_disproved_row(tmp_path):
    wrapper, inner = _make_probe_stub(tmp_path, behavior="disproved")
    proof, detail = muse.probe_profile_carrier(str(inner))

    assert proof == muse.PROOF_DISPROVED

    capability = muse.profile_carrier_capability(
        wrapper_executable=str(wrapper), full_banner=BANNER
    )
    assert capability.supported is False
    assert capability.proof == muse.PROOF_DISPROVED
    assert (
        capability.reason
        == "the installed build ran a clean muse exec --provider echo turn with non-empty base instructions present"
    )


def test_probe_truth_table_unproven_usage_dump_row(tmp_path):
    wrapper, inner = _make_probe_stub(tmp_path, behavior="usage_dump")
    proof, detail = muse.probe_profile_carrier(str(inner))

    assert proof == muse.PROOF_UNPROVEN
    assert "unknown preset" in detail

    capability = muse.profile_carrier_capability(
        wrapper_executable=str(wrapper), full_banner=BANNER
    )
    assert capability.supported is True
    assert capability.proof == muse.PROOF_UNPROVEN
    assert "unknown preset" in capability.reason


def test_probe_truth_table_unproven_missing_file_error_row(tmp_path):
    wrapper, inner = _make_probe_stub(tmp_path, behavior="missing_file_error")
    proof, detail = muse.probe_profile_carrier(str(inner))

    assert proof == muse.PROOF_UNPROVEN
    assert "failed to read" in detail

    capability = muse.profile_carrier_capability(
        wrapper_executable=str(wrapper), full_banner=BANNER
    )
    assert capability.supported is True
    assert capability.proof == muse.PROOF_UNPROVEN
    assert "failed to read" in capability.reason


def test_probe_truth_table_unproven_unrecognized_error_row(tmp_path):
    wrapper, inner = _make_probe_stub(tmp_path, behavior="unrecognized_error")
    proof, detail = muse.probe_profile_carrier(str(inner))

    assert proof == muse.PROOF_UNPROVEN
    assert "fatal: unexpected internal error" in detail

    capability = muse.profile_carrier_capability(
        wrapper_executable=str(wrapper), full_banner=BANNER
    )
    assert capability.supported is True
    assert capability.proof == muse.PROOF_UNPROVEN
    assert "fatal: unexpected internal error" in capability.reason


def test_probe_truth_table_unproven_control_fails_row(tmp_path):
    wrapper, inner = _make_probe_stub(tmp_path, behavior="control_fails")
    proof, detail = muse.probe_profile_carrier(str(inner))

    assert proof == muse.PROOF_UNPROVEN
    assert "control leg crash" in detail

    capability = muse.profile_carrier_capability(
        wrapper_executable=str(wrapper), full_banner=BANNER
    )
    assert capability.supported is True
    assert capability.proof == muse.PROOF_UNPROVEN
    assert "control leg crash" in capability.reason


def test_probe_truth_table_unproven_timeout_row(tmp_path, monkeypatch):
    wrapper, inner = _make_probe_stub(tmp_path, behavior="normal")

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="fake", timeout=0.1)

    monkeypatch.setattr(subprocess, "run", _timeout)
    proof, detail = muse.probe_profile_carrier(str(inner))

    assert proof == muse.PROOF_UNPROVEN


def test_probe_truth_table_unproven_spawn_error_row(tmp_path):
    nonexistent = tmp_path / "nonexistent-binary"
    proof, detail = muse.probe_profile_carrier(str(nonexistent))

    assert proof == muse.PROOF_UNPROVEN


def test_operator_override_clears_disproved_build(tmp_path, monkeypatch):
    wrapper, inner = _make_probe_stub(tmp_path, behavior="disproved")
    inner_digest = muse._sha256_file(str(inner))

    # Without override, disproved build is unsupported
    cap_before = muse.profile_carrier_capability(
        wrapper_executable=str(wrapper), full_banner=BANNER
    )
    assert cap_before.supported is False
    assert cap_before.proof == muse.PROOF_DISPROVED

    # With non-matching override, still unsupported
    monkeypatch.setenv(muse.CAO_MUSE_PROFILE_CARRIER_PROVEN_ENV, "0" * 64)
    cap_mismatch = muse.profile_carrier_capability(
        wrapper_executable=str(wrapper), full_banner=BANNER
    )
    assert cap_mismatch.supported is False
    assert cap_mismatch.proof == muse.PROOF_DISPROVED

    # With matching override, becomes probed_by_operator and supported
    monkeypatch.setenv(muse.CAO_MUSE_PROFILE_CARRIER_PROVEN_ENV, inner_digest)
    cap_cleared = muse.profile_carrier_capability(
        wrapper_executable=str(wrapper), full_banner=BANNER
    )
    assert cap_cleared.supported is True
    assert cap_cleared.proof == muse.PROOF_PROBED_BY_OPERATOR
    assert cap_cleared.reason == ""
    assert cap_cleared.inner_executable_sha256 == inner_digest


def test_inner_executable_resolution_failures(tmp_path):
    # Wrapper missing
    cap_no_wrapper = muse.profile_carrier_capability(
        wrapper_executable=str(tmp_path / "missing-muse"), full_banner=BANNER
    )
    assert cap_no_wrapper.supported is False
    assert cap_no_wrapper.inner_executable is None
    assert "profile_carrier_unverified" in cap_no_wrapper.reason

    # .muse-version missing
    wrapper = tmp_path / "muse-only"
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    wrapper.chmod(0o755)
    cap_no_version = muse.profile_carrier_capability(
        wrapper_executable=str(wrapper), full_banner=BANNER
    )
    assert cap_no_version.supported is False
    assert "profile_carrier_unverified" in cap_no_version.reason

    # Revision mismatch
    (tmp_path / ".muse-version").write_text("0.1.0-R999.1", encoding="utf-8")
    cap_diff_version = muse.profile_carrier_capability(
        wrapper_executable=str(wrapper), full_banner=BANNER
    )
    assert cap_diff_version.supported is False
    assert "profile_carrier_unverified" in cap_diff_version.reason

    # Inner executable missing
    (tmp_path / ".muse-version").write_text(REVISION, encoding="utf-8")
    cap_no_inner = muse.profile_carrier_capability(
        wrapper_executable=str(wrapper), full_banner=BANNER
    )
    assert cap_no_inner.supported is False
    assert "profile_carrier_unverified" in cap_no_inner.reason


def test_probe_memoization(tmp_path, monkeypatch):
    _wrapper, inner = _make_probe_stub(tmp_path, behavior="normal")
    calls = []
    real_run = subprocess.run

    def _spy_run(*args, **kwargs):
        calls.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _spy_run)

    proof1, _ = muse.probe_profile_carrier(str(inner))
    call_count_1 = len(calls)
    assert call_count_1 > 0
    assert proof1 == muse.PROOF_PROBED

    proof2, _ = muse.probe_profile_carrier(str(inner))
    assert proof2 == muse.PROOF_PROBED
    assert len(calls) == call_count_1


def test_capability_advertisement_names_the_probed_facts(monkeypatch):
    accepted = muse.MuseProfileCarrierCapability(
        supported=True,
        reason="",
        proof=muse.PROOF_PROBED,
        full_banner=BANNER,
        inner_executable="/stable/muse-bin-0.2.1-R1215.1",
        inner_executable_sha256="b67f181fb7a519007146104c56fad372f47428da9608ade59835899160f2d6e9",
    )
    monkeypatch.setattr(muse, "installed_profile_carrier_capability", lambda: accepted)

    advertised = v2.native_tui_capabilities()["providers"]["muse_cli"]

    assert advertised["supported"] is True
    assert advertised["profile_carrier_proof"] == muse.PROOF_PROBED
    assert (
        advertised["profile_carrier_inner_sha256"]
        == "b67f181fb7a519007146104c56fad372f47428da9608ade59835899160f2d6e9"
    )
    assert advertised["profile_carrier_reason"] == ""


def test_disproved_carrier_refuses_before_profile_file_or_pane_effect(monkeypatch, tmp_path):
    wrapper, _inner = _make_probe_stub(tmp_path, behavior="disproved")
    wrote_profile = False

    def _write_profile(**_kwargs):
        nonlocal wrote_profile
        wrote_profile = True
        raise AssertionError("the carrier gate must run before this")

    monkeypatch.setattr(v2, "_write_native_profile_file", _write_profile)
    with pytest.raises(ManagedLaunchConflict, match="Muse profile carrier is unavailable"):
        v2._prepare_muse_fresh_launch(
            record={"terminal_id": "t", "generation": "g", "working_directory": str(tmp_path)},
            request={"expected_model": "muse-spark-1.2-contributor", "expected_effort": "high"},
            executable=str(wrapper),
            version_output=BANNER,
            digest=hashlib.sha256(wrapper.read_bytes()).hexdigest(),
            profile_material={"system_prompt": "private", "profile_sha256": "a" * 64},
        )
    assert wrote_profile is False


def test_an_unproven_probe_proceeds_but_claims_nothing(tmp_path):
    """An inconclusive probe must not block work, and must not overclaim.

    Refusing here would block every managed Muse launch for ordinary machine
    load — a slow spawn or a busy temp dir lands in this branch — while
    protecting against a barely reachable sequence: a build that ignores the
    base-instructions file exits zero and is already ``disproved``. What the
    capability must not do is assert a verification nobody performed, so the
    verdict and its reason travel to the reader instead.
    """
    wrapper, inner = _make_probe_stub(tmp_path, behavior="unrecognized_error")

    cap = muse.profile_carrier_capability(wrapper_executable=str(wrapper), full_banner=BANNER)

    assert cap.proof == muse.PROOF_UNPROVEN
    assert cap.supported is True
    # unproven and disproved stay distinguishable: they are different
    # observations and call for different operator responses.
    assert "profile_carrier_unproven" in cap.reason
    assert muse.PROOF_DISPROVED not in cap.reason


def test_the_operator_override_also_clears_an_unproven_verdict(tmp_path, monkeypatch):
    """The refusal ships a verb that clears it in every state that fires it."""
    wrapper, inner = _make_probe_stub(tmp_path, behavior="unrecognized_error")
    inner_digest = muse._sha256_file(str(inner))

    monkeypatch.setenv(muse.CAO_MUSE_PROFILE_CARRIER_PROVEN_ENV, inner_digest)
    cap = muse.profile_carrier_capability(wrapper_executable=str(wrapper), full_banner=BANNER)

    assert cap.supported is True
    assert cap.proof == muse.PROOF_PROBED_BY_OPERATOR
