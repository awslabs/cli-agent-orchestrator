import os
from test.services.vault.test_scan_exclusions import _vault
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.services.vault.findings import FindingCode
from cli_agent_orchestrator.services.vault.scan import (
    _Candidate,
    _read_stable_utf8,
    _stat_identity,
    scan_vault,
)


def test_same_size_mutation_between_read_stats_is_skipped(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "CAO").mkdir()
    mapped = root / "Mapped"
    mapped.mkdir()
    target = mapped / "Changing.md"
    target.write_text("before", encoding="utf-8")

    from cli_agent_orchestrator.services.vault import scan

    original_fstat = scan.os.fstat
    calls = 0
    before_stat = None

    def mutate(fd: int):
        nonlocal before_stat, calls
        calls += 1
        result = original_fstat(fd)
        if calls == 1:
            before_stat = result
            original_mtime = target.stat().st_mtime_ns
            target.write_text("after!", encoding="utf-8")
            os.utime(target, ns=(original_mtime, original_mtime))
            return result
        assert before_stat is not None
        return SimpleNamespace(
            st_dev=before_stat.st_dev,
            st_ino=before_stat.st_ino,
            st_size=before_stat.st_size,
            st_mtime_ns=before_stat.st_mtime_ns,
            st_ctime_ns=before_stat.st_ctime_ns + 1,
        )

    monkeypatch.setattr(scan.os, "fstat", mutate)
    report = scan_vault(_vault(root))

    assert report.notes[0].findings[0].code == FindingCode.UNSTABLE_SKIPPED


def test_discovery_inode_mismatch_is_refused_on_the_open_descriptor(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "CAO").mkdir()
    mapped = root / "Mapped"
    mapped.mkdir()
    target = mapped / "Changing.md"
    target.write_text("before", encoding="utf-8")
    vault = _vault(root)
    candidate = _Candidate(str(target), "Mapped/Changing.md", vault.mappings[0], os.lstat(target))
    replacement = mapped / "Replacement.md"
    replacement.write_text("after!", encoding="utf-8")
    os.replace(replacement, target)

    _content, _after, code = _read_stable_utf8(
        candidate, os.path.realpath(str(root)), vault.max_note_bytes
    )

    assert code == FindingCode.PATH_ESCAPES_ROOT


def test_nonblocking_scan_open_refuses_fifo_swapped_after_discovery(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "CAO").mkdir()
    mapped = root / "Mapped"
    mapped.mkdir()
    target = mapped / "Changing.md"
    target.write_text("before", encoding="utf-8")
    from cli_agent_orchestrator.services.vault import scan

    original_open = scan.os.open
    swapped = False

    def swap_before_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if os.fspath(path) == str(target) and not swapped:
            assert flags & os.O_NONBLOCK
            target.unlink()
            os.mkfifo(target)
            swapped = True
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(scan.os, "open", swap_before_open)
    report = scan_vault(_vault(root))

    assert swapped is True
    assert report.notes[0].findings[0].code == FindingCode.NON_REGULAR_FILE_REFUSED


def test_realpath_outside_root_is_refused_before_open(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "CAO").mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    vault = _vault(root)
    candidate = _Candidate(str(outside), "Mapped/Outside.md", vault.mappings[0], os.lstat(outside))
    from cli_agent_orchestrator.services.vault import scan

    monkeypatch.setattr(scan.os, "open", lambda *_args: pytest.fail("opened outside root"))

    _content, _after, code = _read_stable_utf8(
        candidate, os.path.realpath(str(root)), vault.max_note_bytes
    )

    assert code == FindingCode.PATH_ESCAPES_ROOT


def test_stat_identity_individually_includes_inode_and_ctime():
    class Stat:
        st_dev = 1
        st_ino = 2
        st_size = 3
        st_mtime_ns = 4
        st_ctime_ns = 5

    inode_changed = Stat()
    inode_changed.st_ino = 6
    ctime_changed = Stat()
    ctime_changed.st_ctime_ns = 7

    assert _stat_identity(Stat()) != _stat_identity(inode_changed)
    assert _stat_identity(Stat()) != _stat_identity(ctime_changed)


def test_utf8_normalization_parser_and_secret_gate_are_scanned(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "CAO").mkdir()
    mapped = root / "Mapped"
    mapped.mkdir()
    (mapped / "Windows.md").write_bytes(b"---\r\ncao:\r\n  key: windows\r\n---\r\nline\r\n")
    (mapped / "NotUtf8.md").write_bytes(b"\xff")
    (mapped / "Credential.md").write_text("password: hunter2", encoding="utf-8")

    report = scan_vault(_vault(root))
    by_path = {note.vault_relpath: note for note in report.notes}

    assert by_path["Mapped/Windows.md"].text == "---\ncao:\n  key: windows\n---\nline\n"
    assert by_path["Mapped/NotUtf8.md"].findings[0].code == FindingCode.NOTE_NOT_UTF8
    assert by_path["Mapped/Credential.md"].status == "quarantined"
    assert by_path["Mapped/Credential.md"].findings[0].code == FindingCode.SECRET_DETECTED


def test_secret_gate_leaves_user_frontmatter_outside_body_scan(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "CAO").mkdir()
    mapped = root / "Mapped"
    mapped.mkdir()
    (mapped / "FrontmatterCredential.md").write_text(
        "---\npassword: hunter2sixteen\n---\nbody", encoding="utf-8"
    )

    report = scan_vault(_vault(root))

    assert report.notes[0].status == "indexed"
    assert not any(
        finding.code == FindingCode.SECRET_DETECTED for finding in report.notes[0].findings
    )


def test_case_collisions_are_refused_before_projection(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "CAO").mkdir()
    mapped = root / "Mapped"
    mapped.mkdir()
    (mapped / "Case.md").write_text("one", encoding="utf-8")
    (mapped / "case.md").write_text("two", encoding="utf-8")
    if len(list(mapped.glob("[Cc]ase.md"))) != 2:
        pytest.skip("case-only names collapse on this filesystem")

    report = scan_vault(_vault(root))
    by_path = {note.vault_relpath: note for note in report.notes}

    assert by_path["Mapped/Case.md"].findings[0].code == FindingCode.PATH_CASE_COLLISION
    assert by_path["Mapped/case.md"].findings[0].code == FindingCode.PATH_CASE_COLLISION


def test_excalidraw_is_refused_before_projection(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "CAO").mkdir()
    mapped = root / "Mapped"
    mapped.mkdir()
    (mapped / "Plugin.excalidraw.md").write_text("plugin", encoding="utf-8")

    report = scan_vault(_vault(root))

    assert report.notes[0].findings[0].code == FindingCode.PLUGIN_FORMAT_EXCLUDED
    assert report.notes[0].status == "unsupported"
