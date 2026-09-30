"""Programmatic, tmp-path-confined fixture vault for vault feature tests."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cli_agent_orchestrator.services.vault.config import FolderMapping, VaultSpec


@dataclass(frozen=True)
class VaultFixture:
    root: Path
    vault: VaultSpec


def build_vault_fixture(
    tmp_path: Path,
    *,
    root_name: str = "vault",
    creation_order: Literal["forward", "reverse"] = "forward",
    fixed_mtimes: bool = False,
) -> VaultFixture:
    """Create a representative vault only beneath an empty pytest tmp directory."""
    if tmp_path.is_symlink():
        raise ValueError("fixture tmp_path must not be a symlink")
    tmp_root = tmp_path.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if not tmp_root.is_relative_to(temp_root):
        raise ValueError("fixture tmp_path must be beneath the system temp directory")
    root = (tmp_root / root_name).resolve()
    if root.parent != tmp_root:
        raise ValueError("fixture root must be directly under tmp_path")
    if root.exists() and any(root.iterdir()):
        raise ValueError("fixture root must be empty")
    root.mkdir(parents=True, exist_ok=True)

    def write(relpath: str, content: bytes) -> None:
        path = (root / relpath).resolve()
        if not path.is_relative_to(root):
            raise ValueError("fixture path escapes tmp_path vault root")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    entries = (
        ("CAO/.keep", b""),
        ("Projects/CAO Design/.obsidian/app.json", b"{}"),
        ("Projects/CAO Design/.trash/Deleted.md", b"deleted"),
        ("Projects/CAO Design/.git/config", b"[core]"),
        ("Projects/CAO Design/_cao-private.md", b"private"),
        ("Projects/CAO Design/Design.md", b"---\ncao:\n  key: design\n---\nDesign"),
        ("Projects/CAO Design/Don't Panic.md", b"Don't panic."),
        ("Projects/CAO Design/Notes, drafts (v2).md", b"Draft"),
        ("Projects/CAO Design/R\u00e9f\u00e9rences.md", b"References"),
        ("Projects/CAO Design/Malformed.md", b"---\ncao: [\n---\n"),
        ("Projects/CAO Design/Dangling.md", b"[[No Such Note]]"),
        ("Projects/CAO Design/Credential.md", b"AKIA1234567890ABCDEF"),
        ("Projects/CAO Design/NotUtf8.md", b"\xff"),
        ("Projects/CAO Design/Torn.sync-conflict-1.md", b"conflict"),
        ("Private/Secret.md", b"secret"),
        ("Reference/Glossary.md", b"Glossary"),
        ("Injectable/Team Handbook.md", b"Handbook"),
    )
    if creation_order == "reverse":
        entries = tuple(reversed(entries))
    for relpath, content in entries:
        write(relpath, content)
    if fixed_mtimes:
        for offset, (relpath, _content) in enumerate(sorted(entries), start=1):
            timestamp_ns = 1_700_000_000_000_000_000 + offset
            os.utime(root / relpath, ns=(timestamp_ns, timestamp_ns))

    vault = VaultSpec(
        id="fixture",
        root=str(root),
        managed_folder="CAO",
        exclude=["Private/**"],
        max_note_bytes=4096,
        max_notes=100,
        max_frontmatter_bytes=1024,
        mappings=[
            FolderMapping(
                folder="Projects/CAO Design",
                scope="project",
                scope_id="fixture-project",
            ),
            FolderMapping(folder="Reference", scope="agent", scope_id="fixture-reference"),
            FolderMapping(
                folder="Injectable",
                scope="agent",
                scope_id="fixture-agent",
                inject=True,
            ),
            FolderMapping(folder="Private", scope="agent", scope_id="private"),
            FolderMapping(folder="CAO", scope="global", writable=True),
        ],
    )
    return VaultFixture(root=root, vault=vault)
