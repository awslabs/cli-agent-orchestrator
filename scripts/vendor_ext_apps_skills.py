#!/usr/bin/env python3
"""Vendor the upstream ``modelcontextprotocol/ext-apps`` MCP Apps builder skills.

This is the **offline / air-gapped** counterpart to the online ``mcp-apps-builder``
bridge skill (which fetches the skills on demand via ``npx skills add`` /
the plugin marketplace). It clones the upstream repo at a **pinned tag**, copies
the four builder skills into ``skills/vendor/ext-apps/<skill>/``, and writes an
Apache-2.0 ``NOTICE`` recording the exact source commit.

Design notes
------------
* **Pinned, not floating.** ``PINNED_REF`` / ``PINNED_SHA`` are committed
  constants. The script clones ``PINNED_REF`` and then asserts the resolved
  ``HEAD`` equals ``PINNED_SHA`` so a moved/retagged upstream ref can never
  silently change the vendored bytes. Refresh the pin via
  ``make refresh-ext-apps-skills`` (see ``skills/vendor/ext-apps/README.md``).
* **Idempotent.** Re-running with the same pin reproduces byte-for-byte
  identical output (each destination skill dir is replaced wholesale and the
  NOTICE is regenerated deterministically).
* **Out of the default seed.** The vendored skills land under the **repo-root**
  ``skills/`` tree, *not* inside the package ``src/cli_agent_orchestrator/skills/``
  that ``seed_default_skills()`` reads from — so ``cao init`` is unaffected. They
  are opt-in via ``cao skills add skills/vendor/ext-apps/<skill>``.
* ``--check`` re-fetches the pin and verifies the on-disk vendored content
  matches; it exits non-zero on drift and degrades gracefully when the network
  (or ``git``) is unavailable.

Usage
-----
    python scripts/vendor_ext_apps_skills.py            # vendor / refresh
    python scripts/vendor_ext_apps_skills.py --check     # verify against the pin
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

# --- Pin -------------------------------------------------------------------
# Upstream source and the exact ref/commit this vendored copy is taken from.
# Bump both together when refreshing (they must agree or the script aborts).
REPO_URL = "https://github.com/modelcontextprotocol/ext-apps.git"
PINNED_REF = "v1.7.4"
PINNED_SHA = "ca1d29894fabbd1558885a9ec8620dcb01d7457e"

# Path inside the upstream repo that holds the builder skills.
UPSTREAM_SKILLS_SUBPATH = "plugins/mcp-apps/skills"

# The four MCP Apps builder skills we vendor.
SKILLS: List[str] = [
    "create-mcp-app",
    "add-app-to-server",
    "migrate-oai-app",
    "convert-web-app",
]

# --- Local layout ----------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = ROOT / "skills" / "vendor" / "ext-apps"
NOTICE_PATH = VENDOR_DIR / "NOTICE"


class VendorError(RuntimeError):
    """Raised for any non-recoverable vendoring failure."""


def _run_git(args: List[str], cwd: Optional[Path] = None) -> str:
    """Run a git command, returning stdout; raise VendorError on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:  # git not installed
        raise VendorError("git executable not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise VendorError(
            f"git {' '.join(args)} failed (exit {exc.returncode}): " f"{exc.stderr.strip()}"
        ) from exc
    return result.stdout.strip()


def _clone_pinned(dest: Path) -> str:
    """Shallow + sparse clone of the pinned ref into ``dest``.

    Returns the resolved commit SHA and verifies it matches ``PINNED_SHA`` so a
    retagged upstream ref cannot silently alter the vendored bytes.
    """
    _run_git(
        [
            "clone",
            "--depth",
            "1",
            "--branch",
            PINNED_REF,
            "--filter=blob:none",
            "--sparse",
            REPO_URL,
            str(dest),
        ]
    )
    _run_git(["sparse-checkout", "set", UPSTREAM_SKILLS_SUBPATH], cwd=dest)

    resolved = _run_git(["rev-parse", "HEAD"], cwd=dest)
    if resolved != PINNED_SHA:
        raise VendorError(
            f"Pin mismatch: ref {PINNED_REF!r} resolved to {resolved}, "
            f"expected {PINNED_SHA}. Refresh the pin deliberately "
            f"(update PINNED_REF/PINNED_SHA) if this is intentional."
        )
    return resolved


def _upstream_skill_dir(repo: Path, skill: str) -> Path:
    """Return (and validate) the upstream source directory for a skill."""
    src = repo / UPSTREAM_SKILLS_SUBPATH / skill
    if not (src / "SKILL.md").is_file():
        raise VendorError(
            f"Upstream skill {skill!r} missing SKILL.md at {src} "
            f"(pin {PINNED_REF}). Upstream layout may have changed."
        )
    return src


def _render_notice(sha: str) -> str:
    """Render the NOTICE attribution text for the given source commit."""
    skill_lines = "\n".join(f"  - {skill}" for skill in SKILLS)
    return f"""\
ext-apps vendored MCP Apps builder skills
=========================================

The skills under this directory are vendored, unmodified, from the upstream
Model Context Protocol "ext-apps" repository. They are reproduced here to
support offline / air-gapped installation (see README.md for the online vs
offline paths).

Source project : Model Context Protocol — ext-apps
Source URL     : {REPO_URL}
Source ref     : {PINNED_REF}
Source commit  : {sha}
Upstream path  : {UPSTREAM_SKILLS_SUBPATH}
License        : Apache License 2.0

Vendored skills:
{skill_lines}

These files are licensed under the Apache License, Version 2.0. You may obtain
a copy of the License at:

    http://www.apache.org/licenses/LICENSE-2.0

The full upstream license text accompanies the source repository at the pinned
commit above (see its top-level LICENSE file). This vendored copy is provided
"AS IS", WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
implied. To refresh this copy to a newer upstream release, update PINNED_REF
and PINNED_SHA in scripts/vendor_ext_apps_skills.py and re-run
`make refresh-ext-apps-skills`.
"""


def _vendor(repo: Path, sha: str) -> None:
    """Copy each pinned skill into the vendor dir and (re)write the NOTICE."""
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    for skill in SKILLS:
        src = _upstream_skill_dir(repo, skill)
        dest = VENDOR_DIR / skill
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
    NOTICE_PATH.write_text(_render_notice(sha))


def _diff_trees(left: Path, right: Path) -> List[str]:
    """Return a list of human-readable differences between two trees."""
    cmp = filecmp.dircmp(left, right)
    diffs: List[str] = []

    def _walk(node: filecmp.dircmp, rel: str) -> None:
        for name in node.left_only:
            diffs.append(f"missing on disk: {rel}{name}")
        for name in node.right_only:
            diffs.append(f"unexpected on disk: {rel}{name}")
        for name in node.diff_files:
            diffs.append(f"content differs: {rel}{name}")
        for name in node.funny_files:
            diffs.append(f"uncomparable: {rel}{name}")
        for sub_name, sub in node.subdirs.items():
            _walk(sub, f"{rel}{sub_name}/")

    _walk(cmp, "")
    return diffs


def _check(repo: Path, sha: str) -> int:
    """Verify on-disk vendored content matches the freshly fetched pin."""
    problems: List[str] = []

    for skill in SKILLS:
        src = _upstream_skill_dir(repo, skill)
        dest = VENDOR_DIR / skill
        if not dest.exists():
            problems.append(f"vendored skill missing: {dest.relative_to(ROOT)}")
            continue
        problems.extend(f"{skill}/{d}" for d in _diff_trees(src, dest))

    expected_notice = _render_notice(sha)
    if not NOTICE_PATH.exists():
        problems.append("NOTICE missing")
    elif NOTICE_PATH.read_text() != expected_notice:
        problems.append("NOTICE does not match the pin")

    if problems:
        print("Vendored ext-apps skills are OUT OF SYNC with the pin:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\nRun: python scripts/vendor_ext_apps_skills.py  (then commit the result)",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK: vendored ext-apps skills match pin {PINNED_REF} ({sha[:12]}) "
        f"[{len(SKILLS)} skills]"
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify vendored content matches the pin instead of writing.",
    )
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="ext-apps-vendor-") as tmp:
        repo = Path(tmp) / "ext-apps"
        try:
            sha = _clone_pinned(repo)
        except VendorError as exc:
            # Network/git unavailable: degrade gracefully so callers (and CI in
            # air-gapped environments) get a clear, actionable message.
            print(f"ext-apps vendoring could not reach the pin: {exc}", file=sys.stderr)
            if args.check:
                print(
                    "Skipping --check (network-gated). Vendored content cannot be "
                    "verified against the pin in this environment.",
                    file=sys.stderr,
                )
                # Distinct exit code so CI can treat 'unverifiable' differently
                # from 'verified mismatch' (which is exit 1).
                return 2
            print(
                "Vendoring requires network access to "
                f"{REPO_URL} at {PINNED_REF}. Run this where the network is "
                "available, then commit skills/vendor/ext-apps/.",
                file=sys.stderr,
            )
            return 2

        if args.check:
            return _check(repo, sha)

        _vendor(repo, sha)

    print(
        f"Vendored {len(SKILLS)} ext-apps skills from {PINNED_REF} ({sha[:12]}) "
        f"into {VENDOR_DIR.relative_to(ROOT)}/"
    )
    print("These skills are opt-in. Add one with:")
    for skill in SKILLS:
        print(f"  cao skills add skills/vendor/ext-apps/{skill}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
