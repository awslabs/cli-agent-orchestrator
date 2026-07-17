#!/usr/bin/env python3
"""Mirror the canonical repo-root ``skills/`` tree into the package skills tree.

Single source of truth for CAO skills is the repo-root ``skills/<name>/``
directory (the editing surface that docs, ``cao skills add ./skills/X``, and
doc paths all point to). The package copies under
``src/cli_agent_orchestrator/skills/<name>/`` are **generated mirrors** that must
ship inside the wheel/editable install so ``seed_default_skills()`` can resolve
them via ``importlib.resources``. Because the two copies are committed, they
drift silently — this script keeps them in lockstep.

Usage::

    python scripts/sync_skills.py            # regenerate package mirrors
    python scripts/sync_skills.py --check     # CI guard: exit 1 on any drift

Only skills in the explicit ``SHIPPED_SKILLS`` allowlist are mirrored, so
repo-only development skills are never shipped by accident. Each skill folder is
mirrored recursively (including ``references/`` and any other subdirectories):
files present in the package mirror but absent from the canonical source are
removed so the mirror is byte-for-byte identical.
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path
from typing import List

# Canonical, explicit allowlist of skills that ship inside the package.
# Keep this in sync with the repo-root ``skills/`` directories intended to ship.
SHIPPED_SKILLS: List[str] = [
    "agui-author",
    "cao-mcp-apps",
    "mcp-apps-builder",
    "cao-session-management",
    "cao-supervisor-protocols",
    "cao-worker-protocols",
    "cao-memory",
    "cao-workflow",
    "cao-plugin",
    "cao-provider",
]

REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_SKILLS_DIR = REPO_ROOT / "skills"
PACKAGE_SKILLS_DIR = REPO_ROOT / "src" / "cli_agent_orchestrator" / "skills"


def _relative_files(root: Path) -> List[Path]:
    """Return sorted relative paths of every regular file under ``root``."""
    if not root.is_dir():
        return []
    return sorted(
        path.relative_to(root)
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )


def _diff_skill(name: str) -> List[str]:
    """Return human-readable drift descriptions for a single shipped skill."""
    source_dir = CANONICAL_SKILLS_DIR / name
    dest_dir = PACKAGE_SKILLS_DIR / name
    problems: List[str] = []

    if not source_dir.is_dir():
        problems.append(f"  MISSING canonical source: skills/{name}/")
        return problems

    source_files = set(_relative_files(source_dir))
    dest_files = set(_relative_files(dest_dir))

    for rel in sorted(source_files - dest_files):
        problems.append(f"  MISSING in package: src/cli_agent_orchestrator/skills/{name}/{rel}")
    for rel in sorted(dest_files - source_files):
        problems.append(f"  STALE in package:   src/cli_agent_orchestrator/skills/{name}/{rel}")
    for rel in sorted(source_files & dest_files):
        if not filecmp.cmp(source_dir / rel, dest_dir / rel, shallow=False):
            problems.append(f"  CONTENT differs:    {name}/{rel}")

    return problems


def check() -> int:
    """Report any drift between canonical sources and package mirrors."""
    all_problems: List[str] = []
    for name in SHIPPED_SKILLS:
        problems = _diff_skill(name)
        if problems:
            all_problems.append(f"[{name}]")
            all_problems.extend(problems)

    if all_problems:
        print("Skill packaging drift detected:")
        print("\n".join(all_problems))
        print("\nRun `python scripts/sync_skills.py` to regenerate the package mirrors.")
        return 1

    print(f"OK: {len(SHIPPED_SKILLS)} shipped skills are in sync.")
    return 0


def sync() -> int:
    """Regenerate the package mirror for every shipped skill."""
    missing_sources = [
        name for name in SHIPPED_SKILLS if not (CANONICAL_SKILLS_DIR / name).is_dir()
    ]
    if missing_sources:
        print(
            "ERROR: canonical sources missing for: "
            + ", ".join(f"skills/{n}/" for n in missing_sources)
        )
        return 1

    PACKAGE_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    synced = 0
    for name in SHIPPED_SKILLS:
        source_dir = CANONICAL_SKILLS_DIR / name
        dest_dir = PACKAGE_SKILLS_DIR / name

        source_files = set(_relative_files(source_dir))
        dest_files = set(_relative_files(dest_dir))

        # Remove stale files no longer present in the canonical source.
        for rel in sorted(dest_files - source_files):
            (dest_dir / rel).unlink()

        # Copy every canonical file (byte-for-byte) into the mirror.
        for rel in sorted(source_files):
            target = dest_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_dir / rel, target)

        # Prune now-empty directories left behind by stale removals.
        for path in sorted(dest_dir.rglob("*"), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()

        synced += 1

    print(f"Synced {synced} shipped skills into {PACKAGE_SKILLS_DIR}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if any package mirror has drifted (CI/pre-commit guard).",
    )
    args = parser.parse_args()
    return check() if args.check else sync()


if __name__ == "__main__":
    sys.exit(main())
