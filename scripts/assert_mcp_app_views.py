#!/usr/bin/env python3
"""Assert the four MCP App view bundles are inside every built artifact (#1487).

``[tool.hatch.build] artifacts`` lists ``ext_apps/apps_static/**``. Hatchling treats an
artifacts glob that matches nothing as a SILENT no-op, so a release job that never ran
``cd cao_mcp_apps && npm run build:all`` still builds and uploads a green wheel with no
``ui://cao/*`` views in it. This script reads the built wheels and sdist and fails when any
of the four bundles is missing or empty.

It also fails when any artifact is over PyPI's default per-file limit, and when the sdist
carries a ``node_modules/`` tree (the release jobs run ``npm ci`` before building, and a
node_modules tree in the sdist is what pushed it past that limit; #1509 review).

Usage:
    python scripts/assert_mcp_app_views.py --dist wheelhouse/     # every *.whl and *.tar.gz
    python scripts/assert_mcp_app_views.py --dist dist/
"""

from __future__ import annotations

import argparse
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import List

# Must equal the values of `_RESOURCE_FILES` in src/cli_agent_orchestrator/ext_apps/apps.py;
# test/scripts/test_mcp_app_views_guard.py asserts that agreement.
VIEW_FILES = ("dashboard.html", "agent.html", "event-stream.html", "graph.html")
_WHEEL_DIR = "cli_agent_orchestrator/ext_apps/apps_static/"
_SDIST_DIR = "src/cli_agent_orchestrator/ext_apps/apps_static/"
# PyPI's default per-file upload limit (https://docs.pypi.org/project-management/storage-limits/).
MAX_ARTIFACT_BYTES = 100_000_000


def _missing(sizes: dict, prefix_match) -> List[str]:
    out = []
    for name in VIEW_FILES:
        hits = [s for member, s in sizes.items() if prefix_match(member, name)]
        if not hits or max(hits) == 0:
            out.append(name)
    return out


def check_wheel(path: Path) -> List[str]:
    with zipfile.ZipFile(path) as zf:
        sizes = {i.filename: i.file_size for i in zf.infolist()}
    return _missing(sizes, lambda m, n: m == _WHEEL_DIR + n)


def _sdist_sizes(path: Path) -> dict:
    with tarfile.open(path) as tf:
        return {m.name: m.size for m in tf.getmembers() if m.isfile()}


def check_sdist(path: Path) -> List[str]:
    sizes = _sdist_sizes(path)
    # sdist members are prefixed with `<name>-<version>/`.
    return _missing(sizes, lambda m, n: m.split("/", 1)[-1] == _SDIST_DIR + n)


def sdist_node_modules(path: Path) -> int:
    """How many sdist members sit under a ``node_modules/`` directory."""
    return sum(1 for member in _sdist_sizes(path) if "/node_modules/" in f"/{member}")


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dist", required=True, type=Path)
    args = ap.parse_args(argv)

    artifacts = sorted(args.dist.glob("*.whl")) + sorted(args.dist.glob("*.tar.gz"))
    if not artifacts:
        print(f"::error::no wheel or sdist in {args.dist}: a missing artifact is a failed check")
        return 1
    failed = False
    for art in artifacts:
        is_wheel = art.suffix == ".whl"
        missing = check_wheel(art) if is_wheel else check_sdist(art)
        if missing:
            failed = True
            print(f"::error::{art.name} lacks MCP App views: {', '.join(missing)}")
            print("  build them first: cd cao_mcp_apps && npm ci && npm run build:all")
        else:
            print(f"ok {art.name}: all {len(VIEW_FILES)} MCP App views present")
        size = art.stat().st_size
        if size > MAX_ARTIFACT_BYTES:
            failed = True
            print(
                f"::error::{art.name} is {size} bytes, over PyPI's {MAX_ARTIFACT_BYTES}-byte "
                "per-file limit"
            )
        if not is_wheel:
            leaked = sdist_node_modules(art)
            if leaked:
                failed = True
                print(f"::error::{art.name} carries {leaked} node_modules/ members")
                print("  see [tool.hatch.build.targets.sdist] exclude in pyproject.toml")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
