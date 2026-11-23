"""Print the digests of every tree a release shipped under a renamed skill's old name.

The output is what ``SKILL_RENAME_SHIPPED_DIGESTS`` in
``src/cli_agent_orchestrator/cli/commands/init.py`` must hold. For each old name
in ``SKILL_RENAMES``, every ``v*`` tag that packages
``src/cli_agent_orchestrator/skills/<old name>/`` is extracted to a temporary
folder and digested with ``_skill_tree_digest``, the function ``cao init`` uses
at run time. Tags that shipped the same tree share one digest.

Run from a checkout that has the release tags (``git fetch --tags``)::

    python scripts/skill_rename_digests.py
    python scripts/skill_rename_digests.py --check   # exit 1 if init.py differs

Reads git only; writes nothing outside a temporary folder.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from cli_agent_orchestrator.cli.commands import init as init_module  # noqa: E402

PACKAGED = "src/cli_agent_orchestrator/skills"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, check=True, capture_output=True, text=True
    ).stdout


def _release_tags() -> List[str]:
    return [t for t in _git("tag", "-l", "v*", "--sort=v:refname").split() if t]


def shipped_digests(old_name: str) -> Dict[str, List[str]]:
    """Map digest -> the tags that shipped that tree, for one old skill name."""
    result: Dict[str, List[str]] = {}
    prefix = f"{PACKAGED}/{old_name}"
    for tag in _release_tags():
        listing = _git("ls-tree", "-r", "--name-only", tag, prefix).split("\n")
        files = [f for f in listing if f]
        if not files:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / old_name
            for name in files:
                target = folder / name[len(prefix) + 1 :]
                target.parent.mkdir(parents=True, exist_ok=True)
                blob = subprocess.run(
                    ["git", "show", f"{tag}:{name}"],
                    cwd=REPO_ROOT,
                    check=True,
                    capture_output=True,
                ).stdout
                target.write_bytes(blob)
            digest = init_module._skill_tree_digest(folder)
        assert digest is not None
        result.setdefault(digest, []).append(tag)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare with init.py")
    args = parser.parse_args()
    status = 0
    for old_name in init_module.SKILL_RENAMES:
        found = shipped_digests(old_name)
        print(f"{old_name}:")
        for digest, tags in found.items():
            print(f"  {digest}  {', '.join(tags)}")
        if not found:
            print("  (no release tag packages this skill)")
        if args.check:
            recorded = set(init_module.SKILL_RENAME_SHIPPED_DIGESTS.get(old_name, frozenset()))
            if recorded != set(found):
                print(f"  MISMATCH: init.py records {sorted(recorded)}", file=sys.stderr)
                status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main())
