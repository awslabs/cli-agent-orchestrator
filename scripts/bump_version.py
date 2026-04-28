#!/usr/bin/env python3
"""Version bump script for cli-agent-orchestrator."""

import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
PYPROJECT = ROOT / "pyproject.toml"


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
    content = re.sub(r'version = "[^"]+"', f'version = "{new_version}"', content)
    PYPROJECT.write_text(content)


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

    update_pyproject(new)
    generate_changelog(new)

    print(f"Bumped {old} -> {new}")
    print(f"\nNext steps:")
    print(f"  1. git add pyproject.toml CHANGELOG.md")
    print(f"  2. git commit -m 'chore: release v{new}'")
    print(f"  3. git tag v{new}")
    print(f"  4. git push && git push --tags")


if __name__ == "__main__":
    main()
