import os
from test.services.vault.test_scan_exclusions import _vault

import pytest

from cli_agent_orchestrator.services.vault.config import FolderMapping
from cli_agent_orchestrator.services.vault.findings import FindingCode
from cli_agent_orchestrator.services.vault.scan import scan_vault


def test_symlinked_file_and_directory_are_refused(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "CAO").mkdir()
    mapped = root / "Mapped"
    mapped.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Secret.md").write_text("outside", encoding="utf-8")
    try:
        (mapped / "Symlinked.md").symlink_to(outside / "Secret.md")
        (mapped / "Escape").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this filesystem: {exc}")

    report = scan_vault(_vault(root))
    findings = {note.vault_relpath: note.findings[0].code for note in report.notes}

    assert findings["Mapped/Symlinked.md"] == FindingCode.SYMLINK_REFUSED
    assert findings["Mapped/Escape"] == FindingCode.SYMLINK_REFUSED


def test_hardlinks_are_refused_unless_mapping_allows_them(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "CAO").mkdir()
    mapped = root / "Mapped"
    mapped.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    try:
        os.link(outside, mapped / "Hardlinked.md")
    except OSError as exc:
        pytest.skip(f"hardlinks unavailable on this filesystem: {exc}")

    refused = scan_vault(_vault(root))
    assert refused.notes[0].findings[0].code == FindingCode.HARDLINK_REFUSED

    allowed_vault = _vault(root)
    allowed_vault.mappings[0].allow_hardlinks = True
    allowed = scan_vault(allowed_vault)
    assert allowed.notes[0].status == "indexed"
