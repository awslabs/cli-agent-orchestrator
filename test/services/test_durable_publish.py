"""Tests for P-IMM and P-MUT durable publication (T-RP-9 / T-RP-9b)."""

from __future__ import annotations

import hashlib
import json
import os
import stat

import pytest

from cli_agent_orchestrator.services import durable_publish
from cli_agent_orchestrator.services.durable_publish import (
    ABSENT,
    PublicationConflict,
    PublicationError,
    PublicationRefused,
    publish_immutable,
    publish_mutable,
)


@pytest.fixture(autouse=True)
def _no_crash_hook():
    durable_publish.crash_hook = None
    yield
    durable_publish.crash_hook = None


# ---------------------------------------------------------------- P-IMM


def test_pimm_publishes_content_addressed_0400(tmp_path):
    data = b'{"receipt":true}\n'
    final = publish_immutable(tmp_path, lambda d: f"receipt.{d[:16]}.json", data)
    assert final.read_bytes() == data
    assert stat.S_IMODE(final.stat().st_mode) == 0o400
    assert final.name == f"receipt.{hashlib.sha256(data).hexdigest()[:16]}.json"


def test_pimm_equal_bytes_reuse_is_idempotent(tmp_path):
    data = b"same-bytes"
    first = publish_immutable(tmp_path, lambda d: f"a.{d[:16]}", data)
    second = publish_immutable(tmp_path, lambda d: f"a.{d[:16]}", data)
    assert first == second
    assert len(list(tmp_path.iterdir())) == 1


def test_pimm_different_bytes_same_address_refused(tmp_path):
    data = b"original"
    final = publish_immutable(tmp_path, lambda d: "fixed-name", data)
    with pytest.raises(PublicationConflict):
        publish_immutable(tmp_path, lambda d: "fixed-name", data + b"-mutated")
    assert final.read_bytes() == data  # zero mutation


def test_pimm_symlink_substitution_refused(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_bytes(b"secret")
    digest = hashlib.sha256(b"payload").hexdigest()
    link = tmp_path / f"receipt.{digest[:16]}.json"
    link.symlink_to(target)
    with pytest.raises(PublicationRefused):
        publish_immutable(tmp_path, lambda d: f"receipt.{d[:16]}.json", b"payload")
    assert target.read_bytes() == b"secret"


def test_pimm_empty_refused(tmp_path):
    with pytest.raises(PublicationError):
        publish_immutable(tmp_path, lambda d: f"x.{d[:16]}", b"")


@pytest.mark.parametrize(
    "kill_at",
    [
        "pimm.begin",
        "pimm.temp-opened",
        "pimm.written",
        "pimm.chmod",
        "pimm.fsync",
        "pimm.linked",
    ],
)
def test_pimm_kill_at_every_boundary_converges(tmp_path, kill_at):
    data = b'{"kill":"window"}\n'

    def hook(step: str) -> None:
        if step == kill_at:
            raise RuntimeError("simulated kill")

    durable_publish.crash_hook = hook
    with pytest.raises(RuntimeError):
        publish_immutable(tmp_path, lambda d: f"k.{d[:16]}.json", data)
    durable_publish.crash_hook = None
    # Recovery: re-publication succeeds, converges to exactly one complete
    # artifact, and stray temporaries are swept.
    final = publish_immutable(tmp_path, lambda d: f"k.{d[:16]}.json", data)
    assert final.read_bytes() == data
    assert not list(tmp_path.glob(".pimm-*.part"))


# ---------------------------------------------------------------- P-MUT


def _seq_record(seq: int, marker: str) -> bytes:
    return (json.dumps({"updated_seq": seq, "marker": marker}, sort_keys=True) + "\n").encode()


def test_pmut_first_write_requires_absent(tmp_path):
    path = tmp_path / "heartbeat.json"
    publish_mutable(path, _seq_record(1, "first"), expected_old_sha256=ABSENT)
    assert json.loads(path.read_bytes())["marker"] == "first"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_pmut_second_and_nth_update_succeeds(tmp_path):
    # The EEXIST defect fixture: a fixed-path mutable record must accept its
    # second and Nth update (a no-replace-only primitive would fail here).
    path = tmp_path / "heartbeat.json"
    publish_mutable(path, _seq_record(1, "one"), expected_old_sha256=ABSENT)
    for seq in (2, 3, 4):
        old = hashlib.sha256(path.read_bytes()).hexdigest()
        publish_mutable(path, _seq_record(seq, f"seq-{seq}"), expected_old_sha256=old)
    assert json.loads(path.read_bytes())["updated_seq"] == 4


def test_pmut_wrong_old_digest_refused_zero_mutation(tmp_path):
    path = tmp_path / "state.json"
    publish_mutable(path, _seq_record(1, "one"), expected_old_sha256=ABSENT)
    before = path.read_bytes()
    with pytest.raises(PublicationConflict):
        publish_mutable(path, _seq_record(2, "two"), expected_old_sha256="0" * 64)
    assert path.read_bytes() == before


def test_pmut_absent_expectation_against_existing_bytes_refused(tmp_path):
    path = tmp_path / "state.json"
    publish_mutable(path, _seq_record(1, "one"), expected_old_sha256=ABSENT)
    with pytest.raises(PublicationConflict):
        publish_mutable(path, _seq_record(2, "two"), expected_old_sha256=ABSENT)


def test_pmut_stale_fence_writer_refused(tmp_path):
    path = tmp_path / "heartbeat.json"
    publish_mutable(path, _seq_record(7, "new-token"), expected_old_sha256=ABSENT)
    before = path.read_bytes()

    def stale_fence(current):
        if current and current.get("token") != "current-token":
            raise PublicationConflict("writer fencing token is superseded")

    with pytest.raises(PublicationConflict):
        publish_mutable(
            path,
            _seq_record(8, "stale-writer"),
            expected_old_sha256=hashlib.sha256(before).hexdigest(),
            fence_check=stale_fence,
        )
    assert path.read_bytes() == before


def test_pmut_seq_regression_refused(tmp_path):
    path = tmp_path / "state.json"
    publish_mutable(path, _seq_record(5, "five"), expected_old_sha256=ABSENT)
    old = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(PublicationConflict):
        publish_mutable(path, _seq_record(5, "replay"), expected_old_sha256=old)
    with pytest.raises(PublicationConflict):
        publish_mutable(path, _seq_record(4, "older"), expected_old_sha256=old)


def test_pmut_symlink_substitution_refused(tmp_path):
    target = tmp_path / "victim"
    target.write_bytes(b"intact")
    link = tmp_path / "heartbeat.json"
    link.symlink_to(target)
    with pytest.raises(PublicationRefused):
        publish_mutable(link, _seq_record(1, "x"), expected_old_sha256=ABSENT)
    assert target.read_bytes() == b"intact"


@pytest.mark.parametrize(
    "kill_at",
    [
        "pmut.begin",
        "pmut.verified-old",
        "pmut.fence-ok",
        "pmut.temp-opened",
        "pmut.written",
        "pmut.chmod",
        "pmut.fsync",
        "pmut.renamed",
    ],
)
def test_pmut_kill_at_every_step_recovers_old_or_new_never_torn(tmp_path, kill_at):
    path = tmp_path / "heartbeat.json"
    publish_mutable(path, _seq_record(1, "old"), expected_old_sha256=ABSENT)
    old_bytes = path.read_bytes()
    new_bytes = _seq_record(2, "new")

    def hook(step: str) -> None:
        if step == kill_at:
            raise RuntimeError("simulated kill")

    durable_publish.crash_hook = hook
    with pytest.raises(RuntimeError):
        publish_mutable(path, new_bytes, expected_old_sha256=hashlib.sha256(old_bytes).hexdigest())
    durable_publish.crash_hook = None
    # The visible path holds either the complete old bytes or the complete
    # new bytes — never a torn record.
    after = path.read_bytes()
    assert after in (old_bytes, new_bytes)
    # A retry converges to the new bytes regardless of which side the kill
    # landed on.
    current = hashlib.sha256(after).hexdigest()
    if after != new_bytes:
        publish_mutable(path, new_bytes, expected_old_sha256=current)
    assert path.read_bytes() == new_bytes
    assert not list(tmp_path.glob(".pmut-*.part"))


def test_pmut_concurrent_winner_loses_on_digest(tmp_path):
    # Two writers holding per-process locks race; the loser observes a
    # changed old digest and refuses rather than clobbering.
    path = tmp_path / "state.json"
    publish_mutable(path, _seq_record(1, "one"), expected_old_sha256=ABSENT)
    winner_old = hashlib.sha256(path.read_bytes()).hexdigest()
    loser_stale_expectation = winner_old
    publish_mutable(path, _seq_record(2, "winner"), expected_old_sha256=winner_old)
    with pytest.raises(PublicationConflict):
        publish_mutable(path, _seq_record(2, "loser"), expected_old_sha256=loser_stale_expectation)
    assert json.loads(path.read_bytes())["marker"] == "winner"


def test_pmut_corrupt_current_record_refused(tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes(b"not-json{{{")
    with pytest.raises(PublicationConflict):
        publish_mutable(
            path,
            _seq_record(1, "x"),
            expected_old_sha256=hashlib.sha256(b"not-json{{{").hexdigest(),
        )


def test_pimm_dir_fsync_boundary(tmp_path, monkeypatch):
    # Kill after the dir fsync boundary still yields a complete artifact.
    data = b"dirfsync"
    calls = []

    def hook(step: str) -> None:
        calls.append(step)
        if step == "pimm.dir-fsynced":
            raise RuntimeError("simulated kill")

    durable_publish.crash_hook = hook
    with pytest.raises(RuntimeError):
        publish_immutable(tmp_path, lambda d: f"d.{d[:16]}", data)
    durable_publish.crash_hook = None
    assert "pimm.linked" in calls
    final = publish_immutable(tmp_path, lambda d: f"d.{d[:16]}", data)
    assert final.read_bytes() == data


def test_temp_files_are_not_left_world_readable(tmp_path):
    publish_mutable(tmp_path / "m.json", _seq_record(1, "x"), expected_old_sha256=ABSENT)
    for entry in tmp_path.iterdir():
        assert stat.S_IMODE(entry.stat().st_mode) & 0o077 == 0 or entry.suffix == ".json"
        if entry.suffix == ".json":
            assert stat.S_IMODE(entry.stat().st_mode) == 0o600
