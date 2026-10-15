#!/usr/bin/env python3
"""Refuse to build a release whose tag names a version the tree does not carry.

Runs first in ``publish-to-pypi.yml`` on every ``release`` event, before any wheel is
compiled.

WHY THIS EXISTS: THREE OF THE LAST FOUR RELEASES FAILED THE SAME WAY
--------------------------------------------------------------------
``release.yml`` (bump, commit, tag, release) has not been able to push to ``main`` since
the branch rulesets landed, so releases are cut by hand: a ``chore: release vX.Y.Z`` PR
that bumps ``pyproject.toml``, ``uv.lock`` and the devcontainer feature, then a GitHub
Release whose tag points at that merge commit. When the second step happens without the
first, the tag says one thing and the tree says another:

    tag       pyproject.toml   result
    v2.5.1    2.5.0            TestPyPI: 400 File already exists  (2026-09-11)
    v2.5.2    2.5.0            TestPyPI: 400 File already exists  (2026-10-06)
    v2.5.3    2.5.3            published                          (2026-10-08, PR #899)
    v2.5.4    2.5.3            TestPyPI: 400 File already exists  (2026-10-15)

Each failure cost ~80 minutes of wheel builds on two platforms before twine reported a
filename collision, and the error named the wrong thing: the artifact, not the tag. This
script fails in seconds and names the fix.

WHAT IS CHECKED
---------------
1. The tag is ``vMAJOR.MINOR.PATCH``. Anything else is refused: ``GITHUB_REF_NAME`` is the
   only source of the version on the release path, and a ref that is not a release tag
   must not be allowed to publish by accident.
2. ``pyproject.toml`` ``[project] version`` equals the tag.
3. ``uv.lock``'s entry for the project itself equals the tag. The workflow runs every uv
   command under ``UV_LOCKED=1``, so a lock that lags the bump fails LATER and LESS
   clearly; catching it here keeps one failure mode.
4. ``.devcontainer/features/cao/devcontainer-feature.json`` equals the tag, because
   ``bump_version.py`` and ``release.yml`` both treat it as part of the bump.
5. Optionally (``--refuse-published``), the version is not already on PyPI. A re-tag of a
   published version would otherwise fail at upload time with the same unhelpful 400.
   A network error here is a WARNING, not a failure: PyPI being unreachable must not
   block a release, it only means this one check could not be made.

Exits 0 on success; 1 with a report naming every disagreement and the fix.

Usage
-----
    python scripts/assert_release_version.py --tag v2.5.4
    python scripts/assert_release_version.py --tag "$GITHUB_REF_NAME" --refuse-published
    python scripts/assert_release_version.py --refuse-published   # tree-only, no tag
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 — tomli is a declared dependency there
    import tomli as tomllib  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
UV_LOCK = REPO_ROOT / "uv.lock"
DEVCONTAINER_FEATURE = (
    REPO_ROOT / ".devcontainer" / "features" / "cao" / "devcontainer-feature.json"
)

TAG_PATTERN = re.compile(r"^v(\d+\.\d+\.\d+)$")

FIX = (
    "Fix: merge a `chore: release vX.Y.Z` PR that bumps pyproject.toml, uv.lock and "
    ".devcontainer/features/cao/devcontainer-feature.json together (the shape of PR #899), "
    "then delete this tag and its GitHub Release and re-create both on that merge commit. "
    "Re-running this workflow on the same tag will fail the same way."
)


def version_from_tag(tag: str) -> str:
    """``v2.5.4`` -> ``2.5.4``; anything not shaped like a release tag is refused."""
    match = TAG_PATTERN.match(tag.strip())
    if not match:
        raise ValueError(
            f"{tag!r} is not a release tag; expected vMAJOR.MINOR.PATCH (for example v2.5.4)"
        )
    return match.group(1)


def pyproject_version(path: Path = PYPROJECT) -> str:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def pyproject_name(path: Path = PYPROJECT) -> str:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return str(data["project"]["name"])


def lock_version(name: str, path: Path = UV_LOCK) -> Optional[str]:
    """The version ``uv.lock`` records for the project itself, or None if absent.

    The project's own entry is the one whose ``source`` is the editable root (``.``).
    Matching on name alone would be wrong the day a dependency shares the name.
    """
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    for package in data.get("package", []):
        if package.get("name") != name:
            continue
        source = package.get("source", {})
        if source.get("editable") == "." or source.get("virtual") == ".":
            return str(package.get("version"))
    return None


def devcontainer_version(path: Path = DEVCONTAINER_FEATURE) -> Optional[str]:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    version = data.get("version")
    return None if version is None else str(version)


def pypi_has_version(
    name: str,
    version: str,
    fetch_status: Optional[Callable[[str], int]] = None,
) -> Optional[bool]:
    """True if PyPI serves ``name==version``, False if not, None if PyPI could not be asked.

    ``fetch_status`` is injectable so the tests never touch the network.
    """
    url = f"https://pypi.org/pypi/{name}/{version}/json"

    def _default_fetch(target: str) -> int:
        request = urllib.request.Request(
            target, headers={"User-Agent": "cli-agent-orchestrator release guard"}
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)

    fetch = fetch_status or _default_fetch
    try:
        status = fetch(url)
    except Exception as exc:  # network down, DNS, TLS: all "could not ask"
        print(f"WARNING: could not ask PyPI about {name}=={version}: {exc}", file=sys.stderr)
        return None
    if status == 200:
        return True
    if status == 404:
        return False
    print(
        f"WARNING: PyPI answered HTTP {status} for {name}=={version}; treating as unknown",
        file=sys.stderr,
    )
    return None


def check(
    tag: Optional[str],
    *,
    pyproject: Path = PYPROJECT,
    lock: Path = UV_LOCK,
    devcontainer: Path = DEVCONTAINER_FEATURE,
    refuse_published: bool = False,
    fetch_status: Optional[Callable[[str], int]] = None,
) -> list[str]:
    """Return the list of problems; empty means the release may proceed."""
    problems: list[str] = []

    name = pyproject_name(pyproject)
    tree_version = pyproject_version(pyproject)
    expected = tree_version
    if tag is not None:
        try:
            expected = version_from_tag(tag)
        except ValueError as exc:
            return [str(exc)]
        if tree_version != expected:
            problems.append(
                f"tag {tag} names {expected} but pyproject.toml says {tree_version}; the "
                f"build would produce {name}-{tree_version} artifacts"
            )

    locked = lock_version(name, lock)
    if locked is None:
        problems.append(f"uv.lock has no entry for the project {name!r} itself")
    elif locked != expected:
        problems.append(f"uv.lock records {name} {locked}, expected {expected}")

    dev = devcontainer_version(devcontainer)
    if dev is None:
        problems.append(f"{devcontainer} is missing or has no version field")
    elif dev != expected:
        problems.append(f"{devcontainer.name} says {dev}, expected {expected}")

    if refuse_published and not problems:
        published = pypi_has_version(name, expected, fetch_status)
        if published is True:
            problems.append(
                f"{name} {expected} is already on PyPI; a filename can never be re-uploaded"
            )

    return problems


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--tag",
        default=None,
        help="the release tag (GITHUB_REF_NAME on a release event); omit for a tree-only check",
    )
    parser.add_argument(
        "--refuse-published",
        action="store_true",
        help="also fail if the version is already on PyPI (network errors only warn)",
    )
    parser.add_argument("--pyproject", type=Path, default=PYPROJECT, help=argparse.SUPPRESS)
    parser.add_argument("--lock", type=Path, default=UV_LOCK, help=argparse.SUPPRESS)
    parser.add_argument(
        "--devcontainer", type=Path, default=DEVCONTAINER_FEATURE, help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)

    problems = check(
        args.tag,
        pyproject=args.pyproject,
        lock=args.lock,
        devcontainer=args.devcontainer,
        refuse_published=args.refuse_published,
    )
    if problems:
        print("RELEASE VERSION CHECK FAILED", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(f"  {FIX}", file=sys.stderr)
        return 1

    tree_version = pyproject_version(args.pyproject)
    what = f"tag {args.tag} matches" if args.tag else "tree agrees on"
    print(f"{what} version {tree_version} in pyproject.toml, uv.lock and the devcontainer feature")
    return 0


if __name__ == "__main__":
    sys.exit(main())
