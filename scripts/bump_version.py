#!/usr/bin/env python3
"""Version bump script for cli-agent-orchestrator.

Bumps every place that carries the version, together: ``pyproject.toml``, ``uv.lock`` (the
project's own entry) and the devcontainer feature, then regenerates ``CHANGELOG.md``.
``publish-to-pypi.yml``'s ``verify-release-version`` job refuses a tag unless all three
agree (``scripts/assert_release_version.py``). ``release.yml`` used to skip ``uv.lock``,
which is why 2.5.3 and 2.5.4 had to be bumped by hand in a PR (#899, #946).
"""

import difflib
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).parent.parent
PYPROJECT = ROOT / "pyproject.toml"
UV_LOCK = ROOT / "uv.lock"
DEVCONTAINER_FEATURE = ROOT / ".devcontainer" / "features" / "cao" / "devcontainer-feature.json"
PACKAGE = "cli-agent-orchestrator"

REVISION_LINE = re.compile(r"(?m)^revision = \d+$")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class LockDrift(RuntimeError):
    """``uv lock`` changed more than the project's own version."""


def get_version() -> str:
    content = PYPROJECT.read_text()
    match = re.search(r'version = "([^"]+)"', content)
    return match.group(1) if match else "0.0.0"


def bump(part: str, version: str) -> str:
    major, minor, patch = map(int, version.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    elif part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def update_pyproject(new_version: str) -> None:
    content = PYPROJECT.read_text()
    # Anchor to the start of a line so only the [project] ``version`` key is
    # rewritten. An unanchored ``version = "..."`` also matches the tail of
    # ``python_version = "..."`` under [tool.mypy], which previously clobbered
    # the mypy target with the package version on every release.
    content = re.sub(r'(?m)^version = "[^"]+"', f'version = "{new_version}"', content, count=1)
    PYPROJECT.write_text(content)


def update_devcontainer_feature(new_version: str) -> None:
    content = DEVCONTAINER_FEATURE.read_text()
    content = re.sub(r'"version": "[^"]+"', f'"version": "{new_version}"', content, count=1)
    DEVCONTAINER_FEATURE.write_text(content)


def _root_entry(version: str) -> str:
    return (
        f'[[package]]\nname = "{PACKAGE}"\nversion = "{version}"\nsource = {{ editable = "." }}\n'
    )


def expected_lock(before: str, old_version: str, new_version: str) -> str:
    """``before`` with only the project's own version moved from ``old`` to ``new``."""
    old_entry = _root_entry(old_version)
    if before.count(old_entry) != 1:
        raise LockDrift(
            f"uv.lock has no single editable-root entry for {PACKAGE} {old_version}; it lags "
            "pyproject.toml already. Relock in a reviewed PR before releasing."
        )
    return before.replace(old_entry, _root_entry(new_version))


def _without_lock_flags() -> dict[str, str]:
    # The workflows set UV_LOCKED=1 so that nothing silently rewrites uv.lock. This call is
    # the one place a release is MEANT to change it, so the flag is lifted here only; the
    # result is checked line for line below and then re-checked with `uv lock --locked`.
    return {k: v for k, v in os.environ.items() if k not in ("UV_LOCKED", "UV_FROZEN")}


def update_uv_lock(old_version: str, new_version: str, run: Optional[Runner] = None) -> None:
    """Re-lock with ``uv lock`` and refuse anything but the project's own version changing.

    The release commit reaches ``main`` through the deploy key's ruleset bypass, with no PR
    review, so it must be the mechanical bump and nothing else: a re-resolution riding along
    would ship dependency changes nobody reviewed. A uv older or newer than the one that
    wrote the lock can also rewrite markers and wheel lists: uv 0.9.4 on this repo's
    revision-5 lock removed 9 lines and added 13, including ``revision = 3``. The
    ``revision =`` line is kept as it was; any other difference restores the old lock and
    fails.
    """
    runner = run or subprocess.run
    before = UV_LOCK.read_text(encoding="utf-8")
    want = expected_lock(before, old_version, new_version)
    try:
        runner(["uv", "lock"], cwd=ROOT, check=True, env=_without_lock_flags())
        after = UV_LOCK.read_text(encoding="utf-8")
        kept = REVISION_LINE.search(before)
        if kept is not None:
            after = REVISION_LINE.sub(kept.group(0), after, count=1)
        if after != want:
            changed = sum(
                1
                for line in difflib.unified_diff(
                    want.splitlines(), after.splitlines(), lineterm="", n=0
                )
                if line[:1] in "+-" and line[:3] not in ("+++", "---")
            )
            raise LockDrift(
                f"`uv lock` changed {changed} line(s) of uv.lock beyond the {PACKAGE} version. "
                "The release commit must be the bump alone: relock main in a reviewed PR with "
                "the uv that CI uses, then release again."
            )
        UV_LOCK.write_text(after, encoding="utf-8")
        runner(["uv", "lock", "--locked"], cwd=ROOT, check=True)
    except BaseException:
        UV_LOCK.write_text(before, encoding="utf-8")
        raise


def generate_changelog(new_version: str) -> None:
    # git-cliff calls the GitHub API when [remote.github] is configured in
    # cliff.toml. Transient 502s / rate-limit bumps bubble up as non-zero
    # exit. Retry 3x with 10s backoff to ride through transient hiccups;
    # authenticated requests (via GITHUB_TOKEN env in CI) make this rare.
    last_err: subprocess.CalledProcessError | None = None
    for attempt in range(3):
        try:
            subprocess.run(
                ["git-cliff", "--tag", f"v{new_version}", "-o", "CHANGELOG.md"],
                cwd=ROOT,
                check=True,
            )
            return
        except subprocess.CalledProcessError as e:
            last_err = e
            if attempt < 2:
                print(
                    f"git-cliff attempt {attempt + 1} failed; retrying in 10s...",
                    file=sys.stderr,
                )
                time.sleep(10)
    assert last_err is not None
    raise last_err


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("major", "minor", "patch"):
        print(f"Usage: {sys.argv[0]} <major|minor|patch>")
        print(f"Current version: {get_version()}")
        sys.exit(1)

    old = get_version()
    new = bump(sys.argv[1], old)

    originals = {path: path.read_text() for path in (PYPROJECT, DEVCONTAINER_FEATURE)}
    update_pyproject(new)
    update_devcontainer_feature(new)
    try:
        update_uv_lock(old, new)
    except LockDrift as exc:
        for path, text in originals.items():
            path.write_text(text)
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    generate_changelog(new)

    print(f"Bumped {old} -> {new}")
    print("\nNext steps:")
    print(
        f"  1. git add pyproject.toml uv.lock CHANGELOG.md "
        f"{DEVCONTAINER_FEATURE.relative_to(ROOT)}"
    )
    print(f"  2. git commit -m 'chore: release v{new}'")
    print(f"  3. git tag v{new}")
    print("  4. git push && git push --tags")


if __name__ == "__main__":
    main()
