"""Guard test: MCP Apps references must cite the canonical sources of truth.

The MCP Apps surface is documented across docs/, skills/, examples/, the root
and subproject READMEs, and inline in the ``ext_apps`` / plugin code. Those
references migrated from the pre-GA SEP page and the ``draft`` spec to the
stable, canonical sources:

* https://modelcontextprotocol.io/extensions/apps/overview (+ /build)
* https://modelcontextprotocol.io/extensions/overview#negotiation
* https://modelcontextprotocol.io/extensions/client-matrix
* https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx
* https://www.npmjs.com/package/@modelcontextprotocol/ext-apps (v1.7.4)

This test scans the documented surface and fails if any **stale** reference URL
reappears, so a future edit can't silently regress to an outdated source. The
SEP-1865 *PR* link (the provenance/discussion reference) is intentionally kept
and is not flagged.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

import cli_agent_orchestrator

_PACKAGE_ROOT = Path(cli_agent_orchestrator.__file__).parent
_REPO_ROOT = _PACKAGE_ROOT.parents[1]

# Substrings that must no longer appear (stale sources of truth).
FORBIDDEN_SUBSTRINGS: Tuple[str, ...] = (
    # The pre-GA SEP page (superseded by /extensions/apps/overview + the stable spec).
    "seps/1865-mcp-apps",
    # The SEP-2133 page (superseded by /extensions/overview#negotiation).
    "seps/2133",
    # The moving "draft" spec path (pinned to the stable 2026-01-26 spec instead).
    "specification/draft/apps.mdx",
)

# Files/dirs that document the MCP Apps surface and must stay on canonical refs.
_SCAN_TARGETS: Tuple[Path, ...] = (
    _REPO_ROOT / "docs" / "mcp-apps.md",
    _REPO_ROOT / "README.md",
    _REPO_ROOT / "cao_mcp_apps" / "README.md",
    _REPO_ROOT / "skills" / "cao-mcp-apps",
    _REPO_ROOT / "examples" / "mcp-apps",
    _PACKAGE_ROOT / "ext_apps",
    _PACKAGE_ROOT / "plugins" / "builtin" / "mcp_apps.py",
)

_TEXT_SUFFIXES = {".md", ".py", ".ts", ".tsx", ".txt"}


def _iter_files() -> List[Path]:
    files: List[Path] = []
    for target in _SCAN_TARGETS:
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            files.extend(p for p in target.rglob("*") if p.is_file() and p.suffix in _TEXT_SUFFIXES)
    return sorted(set(files))


def test_no_stale_mcp_apps_references() -> None:
    """No documented MCP Apps surface file may cite a stale source-of-truth URL."""

    offenders: List[str] = []
    for path in _iter_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for needle in FORBIDDEN_SUBSTRINGS:
            if needle in text:
                rel = path.relative_to(_REPO_ROOT)
                offenders.append(f"{rel}: contains stale reference '{needle}'")

    assert not offenders, "Stale MCP Apps references found:\n" + "\n".join(offenders)


def test_scan_actually_covers_files() -> None:
    """Sanity: the scan set is non-empty (paths didn't silently move)."""

    files = _iter_files()
    assert files, "canonical-link scan matched no files; check _SCAN_TARGETS paths"
    # The core docs must be present in the scan.
    names = {p.name for p in files}
    assert "mcp-apps.md" in names
