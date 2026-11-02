#!/usr/bin/env python3
"""Commit a release bump, tag it, and push the commit and the tag in ONE atomic push.

Runs in ``release.yml`` after ``bump_version.py`` and ``assert_release_version.py``.

WHY ATOMIC (review on PR #652, N2)
----------------------------------
The workflow used to run ``git push && git push --tags``. If the branch push landed and the
tag push then failed on a network blip, ``main`` carried an untagged bump commit. The next
scheduled run still saw the OLD tag as the previous release, derived a bump from the same
commits again, and released the version after the one that never got its tag (3.0.0 ->
4.0.0, skipping 3.0.0). ``git push --atomic`` asks the server to update both refs or
neither.

WHAT IS REFUSED BEFORE ANYTHING IS COMMITTED
--------------------------------------------
* A version that is not ``MAJOR.MINOR.PATCH``.
* Any changed tracked file outside the release files. This commit is pushed to ``main``
  with the release deploy key, and both rulesets on ``main`` (``protect-main`` and ``branch
  protection``) list deploy keys as an ``always`` bypass actor; the key is write-enabled
  (read 2026-11-02). So it lands with no PR and no review, and may carry the bump and
  nothing else.
* A run with nothing staged: a bump that changed nothing is a bug upstream, not a release.

The push names ``HEAD:refs/heads/main`` explicitly and never forces, so if ``main`` moved
since the release SHA was checked out the push is rejected as a non-fast-forward, and
because it is atomic the tag is rejected with it.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

RELEASE_FILES = (
    "pyproject.toml",
    "uv.lock",
    "CHANGELOG.md",
    ".devcontainer/features/cao/devcontainer-feature.json",
)
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
BRANCH_REF = "refs/heads/main"
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "github-actions[bot]@users.noreply.github.com"


class Refused(RuntimeError):
    pass


def _git(root: Path, *args: str, env: Optional[Dict[str, str]] = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False, env=env
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, ["git", *args], result.stdout, result.stderr
        )
    return result.stdout


def push_command(remote: str, version: str) -> List[str]:
    """The one push: the release commit to main and its tag, both or neither."""
    tag_ref = f"refs/tags/v{version}"
    return ["push", "--atomic", remote, f"HEAD:{BRANCH_REF}", f"{tag_ref}:{tag_ref}"]


def commit_tag_and_push(root: Path, version: str, remote: str = "origin") -> str:
    if not VERSION.match(version):
        raise Refused(f"{version!r} is not a MAJOR.MINOR.PATCH version")
    changed = {
        line[3:].strip()
        for line in _git(root, "status", "--porcelain", "--untracked-files=no").splitlines()
        if line.strip()
    }
    stray = sorted(changed - set(RELEASE_FILES))
    if stray:
        raise Refused(
            "the release commit may only carry the version bump; also changed: " + ", ".join(stray)
        )
    _git(root, "add", "--", *RELEASE_FILES)
    if not _git(root, "diff", "--cached", "--name-only").strip():
        raise Refused("nothing to commit: the bump changed no release file")

    # Through the environment, which outranks any git config, so the release commit is the
    # bot's whatever the runner's config says.
    bot = {
        **os.environ,
        "GIT_AUTHOR_NAME": BOT_NAME,
        "GIT_AUTHOR_EMAIL": BOT_EMAIL,
        "GIT_COMMITTER_NAME": BOT_NAME,
        "GIT_COMMITTER_EMAIL": BOT_EMAIL,
    }
    _git(root, "commit", "-q", "-m", f"chore: release v{version}", env=bot)
    _git(root, "tag", f"v{version}")
    _git(root, *push_command(remote, version))
    return _git(root, "rev-parse", "HEAD").strip()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Commit, tag and atomically push a release.")
    parser.add_argument("--version", required=True, help="the bumped version, e.g. 2.5.5")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        sha = commit_tag_and_push(args.repo_root, args.version, args.remote)
    except Refused as exc:
        print(f"::error::refusing to release: {exc}")
        return 1
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        print(f"::error::{' '.join(exc.cmd)} failed: {detail}")
        return 1
    print(f"pushed v{args.version} and {BRANCH_REF} together at {sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
