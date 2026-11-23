#!/usr/bin/env python3
"""Version bump script for cli-agent-orchestrator.

Bumps every place that carries the version, together: ``pyproject.toml``, ``uv.lock`` (the
project's own entry) and the devcontainer feature, then promotes the hand-written
``CHANGELOG.md`` Unreleased section and collects ``changelog.d`` fragments.
``publish-to-pypi.yml``'s ``verify-release-version`` job refuses a tag unless all three
agree (``scripts/assert_release_version.py``). ``release.yml`` used to skip ``uv.lock``,
which is why 2.5.3 and 2.5.4 had to be bumped by hand in a PR (#899, #946).
"""

import difflib
import os
import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).parent.parent
PYPROJECT = ROOT / "pyproject.toml"
UV_LOCK = ROOT / "uv.lock"
DEVCONTAINER_FEATURE = ROOT / ".devcontainer" / "features" / "cao" / "devcontainer-feature.json"
CHANGELOG = ROOT / "CHANGELOG.md"
PACKAGE = "cli-agent-orchestrator"

REVISION_LINE = re.compile(r"(?m)^revision = \d+$")
UNRELEASED_HEADING = re.compile(rb"(?m)^## \[Unreleased\](?P<newline>\r?\n|$)")
NEXT_HEADING = re.compile(rb"(?m)^## \[")
FRAGMENT_NAME = re.compile(r"([1-9][0-9]*)\.(Added|Changed|Deprecated|Removed|Fixed|Security)\.md")
FRAGMENT_TYPES = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class LockDrift(RuntimeError):
    """``uv lock`` changed more than the project's own version."""


class ChangelogError(RuntimeError):
    """The hand-written changelog cannot be promoted safely."""


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

    The release commit is pushed to ``main`` with the release deploy key, and both rulesets
    on ``main`` (``protect-main`` and ``branch protection``) list deploy keys as an
    ``always`` bypass actor; the key is write-enabled (read 2026-11-02). So the commit lands
    with no PR and no review, and it must be the mechanical bump and nothing else: a
    re-resolution riding along would ship dependency changes nobody reviewed. A uv older or
    newer than the one that wrote the lock can also rewrite markers and wheel lists: uv 0.9.4
    on this repo's revision-5 lock removed 9 lines and added 13, including ``revision = 3``.
    The ``revision =`` line is kept as it was; any other difference restores the old lock and
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


def _release_date() -> str:
    return date.today().isoformat()


def read_fragments() -> dict[Path, bytes]:
    """Validate all fragments before changing any version carrier."""
    directory = ROOT / "changelog.d"
    fragments: dict[Path, bytes] = {}
    if not directory.exists():
        return fragments
    for path in directory.iterdir():
        if path.name == "README.md":
            continue
        match = FRAGMENT_NAME.fullmatch(path.name)
        if match is None or not path.is_file():
            raise ChangelogError(
                f"Invalid changelog fragment {path.name!r}; expected <PR>.<type>.md "
                f"with type in {', '.join(FRAGMENT_TYPES)}"
            )
        content = path.read_bytes()
        try:
            lines = content.decode("utf-8").strip().splitlines()
        except UnicodeDecodeError as exc:
            raise ChangelogError(
                f"Invalid changelog fragment {path.name!r}: expected UTF-8"
            ) from exc
        if len(lines) != 1 or not re.fullmatch(r"- \S.*", lines[0]):
            raise ChangelogError(
                f"Invalid changelog fragment {path.name!r}: expected one '- ' entry"
            )
        fragments[path] = content
    return fragments


def promoted_changelog(new_version: str, fragments: Optional[dict[Path, bytes]] = None) -> bytes:
    """Promote Unreleased and append fragments in type and PR order."""
    if fragments is None:
        fragments = read_fragments()
    before = CHANGELOG.read_bytes()
    matches = list(UNRELEASED_HEADING.finditer(before))
    if len(matches) != 1:
        raise ChangelogError(
            f"CHANGELOG.md must contain exactly one '## [Unreleased]' heading; found {len(matches)}"
        )

    match = matches[0]
    newline = match.group("newline") or b"\n"
    next_heading = NEXT_HEADING.search(before, match.end())
    section_end = next_heading.start() if next_heading else len(before)
    section = before[match.end() : section_end]
    if fragments:
        entries: dict[str, list[tuple[int, bytes]]] = {kind: [] for kind in FRAGMENT_TYPES}
        for path, content in fragments.items():
            fragment_match = FRAGMENT_NAME.fullmatch(path.name)
            assert fragment_match is not None
            number, kind = fragment_match.groups()
            entries[kind].append((int(number), content.decode("utf-8").strip().encode("utf-8")))
        additions = b"".join(
            b"### "
            + kind.encode("ascii")
            + newline
            + newline.join(entry for _, entry in sorted(entries[kind]))
            + newline
            + newline
            for kind in FRAGMENT_TYPES
            if entries[kind]
        )
        section = section.rstrip(b"\r\n") + newline + newline + additions
    released = f"## [{new_version}] - {_release_date()}".encode("ascii")
    replacement = b"## [Unreleased]" + newline + newline + released + match.group("newline")
    return before[: match.start()] + replacement + section + before[section_end:]


def _write_changelog(content: bytes) -> None:
    """Replace the changelog only after a complete temporary write succeeds."""
    descriptor, name = tempfile.mkstemp(dir=CHANGELOG.parent, prefix=".CHANGELOG-")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        os.chmod(temporary, CHANGELOG.stat().st_mode)
        os.replace(temporary, CHANGELOG)
    finally:
        temporary.unlink(missing_ok=True)


def generate_changelog(new_version: str) -> None:
    """Promote the single Unreleased section (the release entry point is ``main``)."""
    _write_changelog(promoted_changelog(new_version))


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("major", "minor", "patch"):
        print(f"Usage: {sys.argv[0]} <major|minor|patch>")
        print(f"Current version: {get_version()}")
        sys.exit(1)

    old = get_version()
    new = bump(sys.argv[1], old)
    fragments = read_fragments()
    new_changelog = promoted_changelog(new, fragments)
    old_changelog = CHANGELOG.read_bytes()

    originals = {path: path.read_bytes() for path in (PYPROJECT, DEVCONTAINER_FEATURE, UV_LOCK)}
    try:
        update_pyproject(new)
        update_devcontainer_feature(new)
        update_uv_lock(old, new)
        _write_changelog(new_changelog)
        for path in fragments:
            path.unlink()
    except BaseException as exc:
        for path, content in originals.items():
            if path.read_bytes() != content:
                path.write_bytes(content)
        if CHANGELOG.read_bytes() != old_changelog:
            CHANGELOG.write_bytes(old_changelog)
        for path, content in fragments.items():
            if not path.exists():
                path.write_bytes(content)
        if isinstance(exc, LockDrift):
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        raise

    print(f"Bumped {old} -> {new}")
    print("\nNext steps:")
    print(
        f"  1. git add pyproject.toml uv.lock CHANGELOG.md changelog.d {DEVCONTAINER_FEATURE.relative_to(ROOT)}"
    )
    print(f"  2. git commit -m 'chore: release v{new}'")
    print(f"  3. git tag v{new}")
    print(f"  4. git push --atomic origin HEAD:main v{new}  (both refs or neither)")


if __name__ == "__main__":
    main()
