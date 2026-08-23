import os
import tempfile
import unicodedata
from pathlib import Path
from test.fixtures.vault_factory import build_vault_fixture

import pytest

from cli_agent_orchestrator.services.vault.config import FolderMapping, VaultSpec
from cli_agent_orchestrator.services.vault.findings import FindingCode
from cli_agent_orchestrator.services.vault.scan import (
    MAX_TOTAL_SCAN_BYTES,
    SCAN_BYTE_BUDGET_EXCEEDED,
    SCAN_NOTE_LIMIT_EXCEEDED,
    scan_vault,
)


def test_factory_refuses_nonempty_root_and_creates_realistic_names(tmp_path):
    fixture = build_vault_fixture(tmp_path)

    assert (fixture.root / "Projects/CAO Design/Don't Panic.md").exists()
    assert (fixture.root / "Projects/CAO Design/Notes, drafts (v2).md").exists()
    assert (fixture.root / "Projects/CAO Design/Références.md").exists()
    try:
        build_vault_fixture(tmp_path)
    except ValueError as exc:
        assert str(exc) == "fixture root must be empty"
    else:
        raise AssertionError("factory accepted a non-empty root")


def test_factory_refuses_absolute_roots_and_symlinked_tmp_paths(tmp_path):
    with pytest.raises(ValueError, match="directly under tmp_path"):
        build_vault_fixture(tmp_path, root_name=str(Path(tempfile.gettempdir()) / "vault"))

    linked_tmp = tmp_path / "linked-tmp"
    target = tmp_path / "target"
    target.mkdir()
    try:
        linked_tmp.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this filesystem: {exc}")
    with pytest.raises(ValueError, match="must not be a symlink"):
        build_vault_fixture(linked_tmp)


def test_exclusions_are_applied_before_open_and_always_exclusions_are_unconditional(
    tmp_path, monkeypatch
):
    fixture = build_vault_fixture(tmp_path)
    opened: list[str] = []
    from cli_agent_orchestrator.services.vault import scan

    original_open = scan.os.open

    def tracked_open(path, *args, **kwargs):
        opened.append(str(path))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(scan.os, "open", tracked_open)
    report = scan_vault(fixture.vault)

    assert all("Private/Secret.md" not in path for path in opened)
    assert all(".obsidian" not in path for path in opened)
    assert all(".trash" not in path for path in opened)
    assert all(".git" not in path for path in opened)
    assert all("_cao-private" not in path for path in opened)
    assert "Private/Secret.md" not in {note.vault_relpath for note in report.notes}
    assert "Projects/CAO Design/.obsidian/app.json" not in {
        note.vault_relpath for note in report.notes
    }
    assert "Projects/CAO Design/.trash/Deleted.md" not in {
        note.vault_relpath for note in report.notes
    }
    assert "Projects/CAO Design/.git/config" not in {note.vault_relpath for note in report.notes}
    assert "Projects/CAO Design/_cao-private.md" not in {
        note.vault_relpath for note in report.notes
    }


def test_exclusion_globs_match_root_case_insensitively_and_always_exclusions_anywhere(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "CAO").mkdir()
    mapped = root / "Mapped"
    mapped.mkdir()
    (mapped / "Drawing.excalidraw.md").write_text("drawing", encoding="utf-8")
    (mapped / "PRIVATE" / "Visible.md").parent.mkdir()
    (mapped / "PRIVATE" / "Visible.md").write_text("private", encoding="utf-8")
    (mapped / ".OBSIDIAN" / "State.md").parent.mkdir()
    (mapped / ".OBSIDIAN" / "State.md").write_text("state", encoding="utf-8")
    (mapped / "_CAO-note.md").write_text("private", encoding="utf-8")
    vault = _vault(root)
    vault.exclude = ["**/*.excalidraw.md", "mapped/private/**"]

    report = scan_vault(vault)

    assert report.notes == ()


def test_fixture_parser_and_sync_refusals_are_reported_with_their_specific_codes(tmp_path):
    fixture = build_vault_fixture(tmp_path)

    report = scan_vault(fixture.vault)
    by_path = {note.vault_relpath: note for note in report.notes}

    assert by_path["Projects/CAO Design/Malformed.md"].findings[0].code == (
        FindingCode.FRONTMATTER_MALFORMED
    )
    assert by_path["Projects/CAO Design/Torn.sync-conflict-1.md"].findings[0].code == (
        FindingCode.SYNC_ARTIFACT_SKIPPED
    )


def test_real_sync_conflict_filename_patterns_are_skipped(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "CAO").mkdir()
    mapped = root / "Mapped"
    mapped.mkdir()
    (mapped / "A (conflicted copy 2024).md").write_text("conflict", encoding="utf-8")
    (mapped / ".~LOCK.Note.md").write_text("lock", encoding="utf-8")

    report = scan_vault(_vault(root))

    assert all(note.findings[0].code == FindingCode.SYNC_ARTIFACT_SKIPPED for note in report.notes)


def test_per_note_and_total_byte_caps_refuse_before_open(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "CAO").mkdir()
    mapped = root / "Mapped"
    mapped.mkdir()
    (mapped / "Big.md").write_bytes(b"x" * 17)
    (mapped / "First.md").write_bytes(b"first")
    (mapped / "Second.md").write_bytes(b"second")
    vault = _vault(root, max_note_bytes=16)

    report = scan_vault(vault, max_total_bytes=6)
    by_path = {note.vault_relpath: note for note in report.notes}

    assert by_path["Mapped/Big.md"].findings[0].code == FindingCode.NOTE_TOO_LARGE
    assert by_path["Mapped/Second.md"].findings[0].code == SCAN_BYTE_BUDGET_EXCEEDED
    assert by_path["Mapped/Second.md"].findings[0].code == FindingCode.BYTE_BUDGET_EXCEEDED
    assert by_path["Mapped/Second.md"].findings[0].detail.endswith("1 candidates skipped")
    assert report.total_bytes_scanned <= 6 < MAX_TOTAL_SCAN_BYTES


def test_note_count_cap_stops_before_opening_another_candidate(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "CAO").mkdir()
    mapped = root / "Mapped"
    mapped.mkdir()
    (mapped / "First.md").write_text("first", encoding="utf-8")
    (mapped / "Second.md").write_text("second", encoding="utf-8")
    vault = _vault(root)
    vault.max_notes = 1

    report = scan_vault(vault)

    assert report.notes[-1].findings[0].code == SCAN_NOTE_LIMIT_EXCEEDED
    assert report.notes[-1].findings[0].detail.endswith("1 candidates skipped")


def test_total_byte_budget_cannot_be_disabled(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "CAO").mkdir()
    (root / "Mapped").mkdir()

    with pytest.raises(ValueError, match="max_total_bytes must be between"):
        scan_vault(_vault(root), max_total_bytes=MAX_TOTAL_SCAN_BYTES + 1)


def test_missing_and_unreadable_mapping_folders_are_reported(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "CAO").mkdir()
    mapped = root / "Mapped"
    mapped.mkdir()
    (mapped / "Visible.md").write_text("visible", encoding="utf-8")
    vault = _vault(root)
    vault.mappings.insert(0, FolderMapping(folder="Missing", scope="agent", scope_id="missing"))
    from cli_agent_orchestrator.services.vault import scan

    original_walk = scan.os.walk

    def unreadable_walk(path, *args, **kwargs):
        if path == str(mapped):
            kwargs["onerror"](OSError(13, "permission denied", str(mapped)))
            return iter(())
        return original_walk(path, *args, **kwargs)

    monkeypatch.setattr(scan.os, "walk", unreadable_walk)
    report = scan_vault(vault)
    codes = {note.findings[0].code for note in report.notes}

    assert FindingCode.MAPPING_FOLDER_MISSING in codes
    assert FindingCode.MAPPING_FOLDER_UNREADABLE in codes


def test_bom_is_removed_before_text_and_both_hashes(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "CAO").mkdir()
    mapped = root / "Mapped"
    mapped.mkdir()
    content = "---\ncao:\n  key: bom\n---\nBody\n"
    (mapped / "BOM.md").write_text("\ufeff" + content, encoding="utf-8")
    (mapped / "Plain.md").write_text(content, encoding="utf-8")

    by_path = {note.vault_relpath: note for note in scan_vault(_vault(root)).notes}

    assert by_path["Mapped/BOM.md"].text == content
    assert by_path["Mapped/BOM.md"].content_sha256 == by_path["Mapped/Plain.md"].content_sha256
    assert (
        by_path["Mapped/BOM.md"].frontmatter_sha256 == by_path["Mapped/Plain.md"].frontmatter_sha256
    )


def test_nul_bytes_are_refused(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "CAO").mkdir()
    mapped = root / "Mapped"
    mapped.mkdir()
    (mapped / "Nul.md").write_bytes(b"before\x00after")

    report = scan_vault(_vault(root))

    assert report.notes[0].findings[0].code == FindingCode.NOTE_CONTAINS_NUL


def test_nfc_and_nfd_filesystems_produce_identical_report_ordering(tmp_path):
    reports = []
    for index, name in enumerate(
        (
            unicodedata.normalize("NFC", "Références"),
            unicodedata.normalize("NFD", "Références"),
        )
    ):
        root = tmp_path / f"vault-{index}"
        root.mkdir()
        (root / "CAO").mkdir()
        mapped = root / "Mapped"
        mapped.mkdir()
        for filename in ("Rat.md", "Rz.md", f"{name}.md"):
            (mapped / filename).write_text(filename, encoding="utf-8")
        reports.append(scan_vault(_vault(root)))

    assert [note.vault_relpath for note in reports[0].notes] == [
        note.vault_relpath for note in reports[1].notes
    ]


def _vault(root: Path, *, max_note_bytes: int = 4096) -> VaultSpec:
    return VaultSpec(
        id="scan-test",
        root=str(root),
        managed_folder="CAO",
        max_note_bytes=max_note_bytes,
        max_notes=100,
        max_frontmatter_bytes=1024,
        mappings=[
            FolderMapping(
                folder="Mapped",
                scope="project",
                scope_id="scan-project",
                writable=False,
            ),
            FolderMapping(folder="CAO", scope="global", writable=True),
        ],
    )
