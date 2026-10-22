"""A note saved mid-read is re-read a bounded number of times before it is skipped.

Issue #644 AC11 residual. ADR-006 specifies: stat, read, stat again; if the
identity changed across the read, retry up to three times with a short backoff,
then mark ``unstable_skipped`` (warn) and leave the prior row untouched. The
first release skipped on the first unstable read, so a reconcile concurrent
with an Obsidian autosave lost that note for a whole cycle.
"""

from __future__ import annotations

import os
from test.services.vault.test_scan_exclusions import _vault
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.services.vault import scan
from cli_agent_orchestrator.services.vault.findings import FindingCode
from cli_agent_orchestrator.services.vault.scan import (
    MAX_UNSTABLE_REREADS,
    UNSTABLE_REREAD_DELAY_SECONDS,
    scan_vault,
)


def _mapped_note(tmp_path, text: str = "stable body"):
    root = tmp_path / "vault"
    (root / "CAO").mkdir(parents=True)
    mapped = root / "Mapped"
    mapped.mkdir()
    target = mapped / "Changing.md"
    target.write_text(text, encoding="utf-8")
    return root, target


def _stat_like(base: os.stat_result, **overrides) -> SimpleNamespace:
    values = {
        "st_dev": base.st_dev,
        "st_ino": base.st_ino,
        "st_size": base.st_size,
        "st_mtime_ns": base.st_mtime_ns,
        "st_ctime_ns": base.st_ctime_ns,
        "st_mode": base.st_mode,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _ScriptedReader:
    """Stand-in for ``_read_stable_utf8`` that replays a scripted outcome per call."""

    def __init__(self, outcomes, stat: os.stat_result):
        self._outcomes = list(outcomes)
        self._stat = stat
        self.calls = 0

    def __call__(self, candidate, root, max_note_bytes, *, max_read_bytes=None):
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if outcome == "unstable":
            return None, self._stat, FindingCode.UNSTABLE_SKIPPED
        return outcome.encode("utf-8"), self._stat, None


@pytest.fixture
def no_sleep(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(scan.time, "sleep", slept.append)
    return slept


def test_reread_bound_matches_adr_006():
    assert MAX_UNSTABLE_REREADS == 3
    assert 0 < UNSTABLE_REREAD_DELAY_SECONDS <= 0.1


def test_stable_first_read_is_read_once_without_sleeping(tmp_path, monkeypatch, no_sleep):
    root, target = _mapped_note(tmp_path)
    reader = _ScriptedReader(["first"], os.stat(target))
    monkeypatch.setattr(scan, "_read_stable_utf8", reader)

    report = scan_vault(_vault(root))

    note = report.notes[0]
    assert reader.calls == 1
    assert no_sleep == []
    assert note.status == "indexed"
    assert note.text == "first"
    assert note.findings == ()


def test_unstable_then_stable_uses_the_second_read(tmp_path, monkeypatch, no_sleep):
    root, target = _mapped_note(tmp_path)
    reader = _ScriptedReader(["unstable", "second"], os.stat(target))
    monkeypatch.setattr(scan, "_read_stable_utf8", reader)

    report = scan_vault(_vault(root))

    note = report.notes[0]
    assert reader.calls == 2
    assert no_sleep == [UNSTABLE_REREAD_DELAY_SECONDS]
    assert note.status == "indexed"
    assert note.text == "second"
    assert not any(finding.code == FindingCode.UNSTABLE_SKIPPED for finding in note.findings)
    assert report.total_bytes_scanned == len(b"second")


def test_stable_on_the_last_permitted_reread_is_still_indexed(tmp_path, monkeypatch, no_sleep):
    root, target = _mapped_note(tmp_path)
    reader = _ScriptedReader(["unstable"] * MAX_UNSTABLE_REREADS + ["final"], os.stat(target))
    monkeypatch.setattr(scan, "_read_stable_utf8", reader)

    report = scan_vault(_vault(root))

    assert reader.calls == MAX_UNSTABLE_REREADS + 1
    assert len(no_sleep) == MAX_UNSTABLE_REREADS
    assert report.notes[0].status == "indexed"
    assert report.notes[0].text == "final"


def test_never_stable_within_the_bound_is_skipped_with_the_attempt_count(
    tmp_path, monkeypatch, no_sleep
):
    root, target = _mapped_note(tmp_path)
    reader = _ScriptedReader(["unstable"] * (MAX_UNSTABLE_REREADS + 5), os.stat(target))
    monkeypatch.setattr(scan, "_read_stable_utf8", reader)

    report = scan_vault(_vault(root))

    note = report.notes[0]
    assert reader.calls == MAX_UNSTABLE_REREADS + 1, "the bound must cap the number of reads"
    assert no_sleep == [
        UNSTABLE_REREAD_DELAY_SECONDS * attempt for attempt in range(1, MAX_UNSTABLE_REREADS + 1)
    ]
    assert note.status == "skipped"
    assert note.text is None
    assert note.findings[0].code == FindingCode.UNSTABLE_SKIPPED
    assert note.findings[0].severity == "warn"
    assert note.findings[0].detail == (
        f"file changed during read; still changing after {MAX_UNSTABLE_REREADS + 1} reads"
    )
    assert report.total_bytes_scanned == 0


def test_other_refusals_are_not_retried(tmp_path, monkeypatch, no_sleep):
    root, target = _mapped_note(tmp_path)
    calls = 0

    def refuse(candidate, root, max_note_bytes, *, max_read_bytes=None):
        nonlocal calls
        calls += 1
        return None, os.stat(target), FindingCode.NOTE_TOO_LARGE

    monkeypatch.setattr(scan, "_read_stable_utf8", refuse)

    report = scan_vault(_vault(root))

    assert calls == 1
    assert no_sleep == []
    assert report.notes[0].findings[0].code == FindingCode.NOTE_TOO_LARGE


def test_real_reader_recovers_from_a_single_mid_read_save(tmp_path, monkeypatch, no_sleep):
    """End to end through the real reader: the first read window is torn, the second is clean."""
    root, target = _mapped_note(tmp_path, "before")
    original_fstat = scan.os.fstat
    fstat_calls = 0

    def fstat_with_one_torn_window(fd: int):
        nonlocal fstat_calls
        fstat_calls += 1
        result = original_fstat(fd)
        if fstat_calls == 2:
            # ``after`` of the first read: a same-size in-place save bumped ctime.
            return _stat_like(result, st_ctime_ns=result.st_ctime_ns + 1)
        return result

    monkeypatch.setattr(scan.os, "fstat", fstat_with_one_torn_window)

    report = scan_vault(_vault(root))

    assert fstat_calls == 4
    assert report.notes[0].status == "indexed"
    assert report.notes[0].text == "before"


def test_real_reader_gives_up_when_every_window_is_torn(tmp_path, monkeypatch, no_sleep):
    root, target = _mapped_note(tmp_path, "before")
    original_fstat = scan.os.fstat
    tick = 0

    def always_changing(fd: int):
        nonlocal tick
        tick += 1
        return _stat_like(original_fstat(fd), st_ctime_ns=tick)

    monkeypatch.setattr(scan.os, "fstat", always_changing)

    report = scan_vault(_vault(root))

    assert tick == 2 * (MAX_UNSTABLE_REREADS + 1)
    assert report.notes[0].status == "skipped"
    assert report.notes[0].findings[0].code == FindingCode.UNSTABLE_SKIPPED
