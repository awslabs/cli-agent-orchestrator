#!/usr/bin/env python3
"""Assert the full per-platform wheel set is present before publishing.

Issue #321, requirement SR-1. Runs in ``publish-to-pypi.yml`` immediately before each
upload, on the merged ``dist/`` directory.

WHY A COUNT IS NOT ENOUGH, AND WHY A GREEN MATRIX IS NOT EITHER
---------------------------------------------------------------
The wheel matrix runs with ``fail-fast: false`` so one platform's failure does not hide the
others. The trade-off is that a partial result looks almost identical to a complete one in
a run summary — a column of green with one red is easy to skim past, and the publish job
would then upload whatever survived as if that were the release.

Worse, a matrix leg can succeed while producing the WRONG artifact. The defect this unit
fixes is precisely that: a build that reported success and emitted a ``py3-none-any`` wheel
containing a ``Mach-O 64-bit arm64`` executable. Every job was green.

So this script asserts the *artifacts*, not the job outcomes:

1. **Every expected platform is present.** A platform the matrix builds but fails to deliver
   leaves its operators with no wheel — pip falls back to the sdist and builds from source,
   which needs a Rust toolchain they may not have.
2. **No wheel is tagged ``any``.** A single ``*-any.whl`` in ``dist/`` is the original defect
   reaching PyPI, where it OUTRANKS the platform wheels for every installer: pip prefers a
   more specific tag, but an ``any`` wheel is compatible everywhere, so any host whose
   platform is not in the set installs it and gets a binary it cannot execute.
3. **Exactly one sdist.** The sdist is the source fallback; two of them means a stale
   artifact was merged in.

Names are read from the wheel FILENAMES, which is what PyPI indexes and what pip matches
against — the same authority the installer uses.

Usage
-----
    python scripts/assert_wheel_matrix.py --dist dist/
    python scripts/assert_wheel_matrix.py --dist dist/ --expect macosx_11_0_arm64

Exits 0 on success; non-zero with an explanatory report on failure.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

# The platform set this repo's wheel matrix builds, as patterns matched against the wheel
# filename's platform tag.
#
# WINDOWS AND macOS x86_64 ARE DELIBERATELY ABSENT. Interview Q2 named four platforms; two of
# them cannot be delivered as a working install:
#
#   * `win_amd64` — `cli_agent_orchestrator` cannot be imported on Windows at all (four
#     modules import `fcntl` at module scope, and the backend is tmux).
#   * `macosx_*_x86_64` — this project floors at `cryptography>=50.0.0` for two HIGH CVEs, and
#     cryptography ships no Intel-macOS wheel at 49.0.0 or above, so an Intel Mac must compile
#     it from source no matter what we publish.
#
# Neither is built, and neither may be REQUIRED here. Requiring a platform the matrix does not
# build would fail every publish; accepting one that cannot run would be worse. See
# `build-wheels` in publish-to-pypi.yml for the measurements behind both.
#
# Patterns rather than literals because the tags carry version numbers that legitimately
# move with the runner image — `macosx_26_0_arm64` tracks the macOS SDK, and a Linux tag may
# be `linux_x86_64` or `manylinux_2_28_x86_64` depending on whether auditwheel retagged it.
# Matching the stable prefix/suffix means a runner-image bump does not fail this gate
# spuriously, while a genuinely missing platform still does.
#
# Keyed by ARCH, not by OS: `macosx` alone cannot distinguish the arm64 wheel from the
# x86_64 one, and noticing exactly that kind of absence is the point.
#
# Each entry is a LIST of acceptable patterns, because one platform legitimately has two
# valid tag forms. `manylinux_2_28_x86_64` is what actually ships on Linux: measured by running
# `auditwheel repair` inside `quay.io/pypa/manylinux_2_28_x86_64`, which rewrote
# `py3-none-linux_x86_64` to `py3-none-manylinux_2_28_x86_64` and exited 0. PyPI rejects the
# un-retagged `linux_x86_64` form outright, so the retagged form must be accepted — but the
# raw form is kept as an alternative so the gate still works if auditwheel is ever disabled.
#
# Alternatives rather than a looser wildcard on purpose. The obvious shortcut `*linux_x86_64`
# does NOT match `manylinux_2_28_x86_64` at all (that tag does not end in `linux_x86_64`), and
# a wildcard loose enough to match it would also match `musllinux_1_2_x86_64` — a platform
# this repo deliberately SKIPS. A pattern that accepts a skipped platform in place of a
# required one makes the check unfalsifiable. (#321)
REQUIRED_PLATFORM_PATTERNS: Dict[str, List[str]] = {
    "macOS arm64": ["macosx_*_arm64"],
    "Linux x86_64": ["manylinux_*_x86_64", "linux_x86_64"],
}


class MatrixError(Exception):
    """An assertion failed. Rendered as a report, never a traceback."""


def _platform_tag(wheel_name: str) -> str:
    """The platform component of a wheel filename.

    A wheel filename is ``{name}-{version}(-{build})?-{python}-{abi}-{platform}.whl``, so the
    platform tag is the last hyphen-separated field before the extension. Parsed positionally
    rather than with a regex over the whole name because the distribution name itself may
    contain underscores that a looser pattern would mis-split.
    """
    stem = wheel_name[: -len(".whl")] if wheel_name.endswith(".whl") else wheel_name
    parts = stem.split("-")
    if len(parts) < 5:
        raise MatrixError(
            f"{wheel_name!r} is not a well-formed wheel filename (expected at least "
            "name-version-python-abi-platform)"
        )
    return parts[-1]


def _matches(platform_tag: str, pattern: str) -> bool:
    """Match a platform tag against a pattern containing at most one ``*``.

    ``*`` stands for the version digits that move with the runner image or the glibc policy
    (``macosx_26_0_arm64``, ``manylinux_2_28_x86_64``), so it must NOT span the OS name: that
    is what keeps ``manylinux_*_x86_64`` from also accepting ``musllinux_1_2_x86_64``, a
    platform this repo deliberately skips.

    ``fnmatch`` is not used because its ``*`` is unanchored and would match across the OS
    prefix, making the distinction above impossible to express.
    """
    if "*" not in pattern:
        return platform_tag == pattern
    prefix, _, suffix = pattern.partition("*")
    if not (platform_tag.startswith(prefix) and platform_tag.endswith(suffix)):
        return False
    # The wildcard must actually cover something, and must not be empty-matched into an
    # overlap between prefix and suffix (which would let a too-short tag pass).
    return len(platform_tag) > len(prefix) + len(suffix)


def assert_matrix(dist: Path, expected: Dict[str, List[str]]) -> None:
    """Assert dist/ holds the full platform wheel set, one sdist, and no 'any' wheel."""
    if not dist.is_dir():
        raise MatrixError(f"no such directory: {dist}")

    wheels = sorted(p.name for p in dist.glob("*.whl"))
    sdists = sorted(p.name for p in dist.glob("*.tar.gz"))

    if not wheels:
        # Never pass quietly on an empty directory: a missing artifact is a failed check.
        raise MatrixError(
            f"no wheels found in {dist}. A missing artifact is a FAILED check, never a "
            "passed one — the matrix produced nothing to publish"
        )

    print(f"inspecting {len(wheels)} wheel(s) and {len(sdists)} sdist(s) in {dist}")
    tags = {name: _platform_tag(name) for name in wheels}
    for name, tag in tags.items():
        print(f"  {tag:<28} {name}")

    problems: List[str] = []

    # 1. No 'any' platform. Checked first: it is the defect this unit exists to fix, and it
    #    is the one failure that silently overrides every correct wheel at install time.
    any_wheels = [name for name, tag in tags.items() if tag == "any"]
    if any_wheels:
        problems.append(
            "these wheels are tagged platform 'any' while this project bundles a native "
            f"executable: {', '.join(any_wheels)}. An 'any' wheel is compatible with EVERY "
            "host, so pip installs it wherever no platform wheel matches and the operator "
            "gets a binary that cannot execute. This is issue #321's defect"
        )

    # 2. Every expected platform present — satisfied by ANY of its acceptable tag forms.
    missing = [
        label
        for label, patterns in expected.items()
        if not any(_matches(t, p) for t in tags.values() for p in patterns)
    ]
    if missing:
        problems.append(
            f"no wheel found for: {', '.join(missing)}. Expected patterns: "
            + "; ".join(f"{label} -> {' | '.join(expected[label])}" for label in missing)
            + ". A platform with no wheel falls back to the sdist, which requires a Rust "
            "toolchain on the operator's machine"
        )

    # 3. Exactly one sdist. Reported but not fatal when absent: `--dist` is also used on a
    #    wheels-only directory during development, and the workflow builds the sdist in a
    #    separate job whose absence would already fail the `needs:` gate.
    if len(sdists) > 1:
        problems.append(f"expected at most one sdist, found {len(sdists)}: {', '.join(sdists)}")

    if problems:
        raise MatrixError(
            "wheel matrix assertion failed:\n" + "\n".join(f"  - {p}" for p in problems)
        )

    print(f"OK: all {len(expected)} expected platforms present, none tagged 'any'")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dist",
        type=Path,
        required=True,
        metavar="DIR",
        help="directory holding the merged wheels and sdist",
    )
    parser.add_argument(
        "--expect",
        nargs="*",
        default=None,
        metavar="PATTERN",
        help="override the expected platform patterns (default: the platforms the wheel "
        "matrix builds)",
    )
    args = parser.parse_args(argv)

    expected: Dict[str, List[str]] = (
        {p: [p] for p in args.expect}
        if args.expect is not None
        else {label: list(patterns) for label, patterns in REQUIRED_PLATFORM_PATTERNS.items()}
    )

    try:
        assert_matrix(args.dist, expected)
    except MatrixError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
