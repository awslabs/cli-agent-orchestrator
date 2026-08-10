"""Closed runtime authority for Muse's internal profile carrier."""

from __future__ import annotations

import hashlib

import pytest

from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services import muse_native_launch as muse
from cli_agent_orchestrator.services.managed_launch import ManagedLaunchConflict

BANNER = "Muse Code 0.1.0 (0.1.0-R708.1)"
KNOWN_DIGEST = "4290bfafa5bbb81a6fd493aaea12f848c789b1d22edfa0c4b849151deba3e70c"


def _launcher(tmp_path, *, revision="0.1.0-R708.1"):
    wrapper = tmp_path / "muse"
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    wrapper.chmod(0o755)
    (tmp_path / ".muse-version").write_text(revision, encoding="utf-8")
    inner = tmp_path / f"muse-bin-{revision}"
    inner.write_bytes(b"the real inner executable fixture")
    inner.chmod(0o755)
    return wrapper, inner


def test_profile_carrier_digest_reads_a_large_inner_file_correctly(tmp_path):
    inner = tmp_path / "muse-bin-fixture"
    payload = (b"muse carrier digest\n" * 196_608) + b"tail"
    inner.write_bytes(payload)

    assert muse._sha256_file(str(inner)) == hashlib.sha256(payload).hexdigest()


def test_exact_banner_and_inner_digest_select_the_closed_carrier_cell(tmp_path, monkeypatch):
    wrapper, inner = _launcher(tmp_path)
    seen = []

    def _known(path):
        seen.append(path)
        assert path == str(inner)
        return KNOWN_DIGEST

    monkeypatch.setattr(muse, "_sha256_file", _known)
    capability = muse.profile_carrier_capability(
        wrapper_executable=str(wrapper), full_banner=BANNER
    )

    assert capability.supported is True
    assert capability.cell == muse.PROFILE_CARRIER_CAPABILITY_CELL
    assert capability.inner_executable == str(inner)
    assert capability.inner_executable_sha256 == KNOWN_DIGEST
    assert seen == [str(inner)]


def test_same_semver_different_r_revision_is_refused(tmp_path, monkeypatch):
    wrapper, _inner = _launcher(tmp_path, revision="0.1.0-R709.1")
    banner = "Muse Code 0.1.0 (0.1.0-R709.1)"
    monkeypatch.setattr(muse, "_sha256_file", lambda _path: KNOWN_DIGEST)

    capability = muse.profile_carrier_capability(
        wrapper_executable=str(wrapper), full_banner=banner
    )

    assert capability.supported is False
    assert capability.reason == "profile_carrier_unverified"


def test_exact_banner_with_changed_inner_digest_is_refused(tmp_path, monkeypatch):
    wrapper, _inner = _launcher(tmp_path)
    monkeypatch.setattr(muse, "_sha256_file", lambda _path: "0" * 64)

    capability = muse.profile_carrier_capability(
        wrapper_executable=str(wrapper), full_banner=BANNER
    )

    assert capability.supported is False
    assert capability.reason == "profile_carrier_unverified"


def test_wrapper_digest_cannot_substitute_for_the_inner_binary(tmp_path, monkeypatch):
    wrapper, inner = _launcher(tmp_path)
    wrapper_digest = hashlib.sha256(wrapper.read_bytes()).hexdigest()
    assert wrapper_digest != KNOWN_DIGEST
    monkeypatch.setattr(
        muse,
        "_sha256_file",
        lambda path: KNOWN_DIGEST if path == str(inner) else wrapper_digest,
    )

    capability = muse.profile_carrier_capability(
        wrapper_executable=str(wrapper), full_banner=BANNER
    )

    assert capability.supported is True
    assert capability.inner_executable_sha256 == KNOWN_DIGEST


def test_capability_advertisement_and_launch_share_the_closed_reason(monkeypatch):
    unsupported = muse.MuseProfileCarrierCapability(False, "profile_carrier_unverified")
    monkeypatch.setattr(muse, "installed_profile_carrier_capability", lambda: unsupported)

    advertised = v2.native_tui_capabilities()["providers"]["muse_cli"]

    assert advertised["supported"] is False
    assert advertised["reason"] == "profile_carrier_unverified"


def test_capability_advertisement_names_the_accepted_cell(monkeypatch):
    accepted = muse.MuseProfileCarrierCapability(
        True,
        "",
        cell=muse.PROFILE_CARRIER_CAPABILITY_CELL,
        full_banner=BANNER,
        inner_executable="/stable/muse-bin-0.1.0-R708.1",
        inner_executable_sha256=KNOWN_DIGEST,
    )
    monkeypatch.setattr(muse, "installed_profile_carrier_capability", lambda: accepted)

    advertised = v2.native_tui_capabilities()["providers"]["muse_cli"]

    assert advertised["supported"] is True
    assert advertised["profile_carrier_capability"] == muse.PROFILE_CARRIER_CAPABILITY_CELL
    assert advertised["profile_carrier_inner_sha256"] == KNOWN_DIGEST


def test_unverified_carrier_refuses_before_profile_file_or_pane_effect(monkeypatch, tmp_path):
    wrapper, _inner = _launcher(tmp_path)
    wrote_profile = False

    def _write_profile(**_kwargs):
        nonlocal wrote_profile
        wrote_profile = True
        raise AssertionError("the carrier gate must run before this")

    monkeypatch.setattr(v2, "_write_native_profile_file", _write_profile)
    with pytest.raises(ManagedLaunchConflict, match="profile carrier.*profile_carrier_unverified"):
        v2._prepare_muse_fresh_launch(
            record={"terminal_id": "t", "generation": "g", "working_directory": str(tmp_path)},
            request={"expected_model": "muse-spark-1.2-contributor", "expected_effort": "high"},
            executable=str(wrapper),
            version_output=BANNER,
            digest=hashlib.sha256(wrapper.read_bytes()).hexdigest(),
            profile_material={"system_prompt": "private", "profile_sha256": "a" * 64},
        )
    assert wrote_profile is False
