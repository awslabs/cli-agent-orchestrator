#!/usr/bin/env python3
"""Assert no Python-FFI crate appears in the ``cao-tui`` dependency graph.

Issue #321, requirement FR-5.2 / SR-4, hard constraint T-10. Runs in ``ci.yml``'s ``rust``
job on both ``macos-latest`` and ``ubuntu-latest``.

WHAT THIS ENFORCES
------------------
T-10 forbids linking the Rust TUI against the Python runtime: no PyO3, no embedded
interpreter, no shared in-process state with ``cli_agent_orchestrator``. The boundary is
**subprocess for CLI invocation and HTTP for server reads**, and nothing else.

The affirmed rule is explicit that this must be a *deterministic check on the dependency
graph*, not a code-review convention. The reason is that code review cannot see it: an FFI
crate does not arrive as a line someone adds to ``Cargo.toml``. It arrives four levels down a
transitive chain, in a dependency of a dependency, in a diff that shows only ``Cargo.lock``
churn. A reviewer scanning ``[dependencies]`` would pass it.

WHY ``cargo metadata`` AND NOT ``cargo tree``
---------------------------------------------
``cargo tree -i <crate>`` looks like the obvious tool and is the wrong one — its exit status
is inverted for this purpose. Measured on this crate:

    cargo tree -i pyo3   ->  error: package ID specification `pyo3` did not match any
                             packages          (i.e. NON-ZERO when the crate is ABSENT)
    cargo tree -i nix    ->  prints the tree   (i.e. ZERO when the crate is PRESENT)

So a naive ``cargo tree -i pyo3`` gate fails on a clean graph and passes on a poisoned one.
``cargo metadata`` instead emits the whole resolved graph as JSON, once, and lets this script
decide — and its resolution includes **dev-dependencies and other platforms' crates**
(measured: 39 packages, covering ``portable-pty``/``nix`` from ``[dev-dependencies]`` and the
Windows-only ``winapi``/``windows-sys``). Both matter here: a Python binding pulled in as
test-only tooling, or under ``[target.'cfg(windows)'.dependencies]``, is exactly the arrival
route a host-only check would miss.

FAILING CLOSED
--------------
Two properties keep this from becoming a check that cannot fail:

1. **A ``cargo metadata`` failure is a FAILED check, not a passed one.** If the subprocess
   errors, exits non-zero, or emits unparseable JSON, this script exits non-zero. A gate that
   greens when its own data source breaks attests nothing.
2. **The graph is sanity-checked before the verdict.** The root crate must be present and the
   package count must be plausible. Without this, an empty or truncated ``packages`` array
   would satisfy "no FFI crate found" trivially — the failure mode where the gate reports
   success precisely because it saw nothing at all.

Usage
-----
    python scripts/assert_no_ffi.py --manifest-path tui/Cargo.toml
    python scripts/assert_no_ffi.py --manifest-path tui/Cargo.toml --list

Exits 0 when the graph is clean; non-zero with the offending crate and its inclusion path
otherwise.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

# The banned set, verbatim from FR-5.2 / SR-4 and the affirmed rule in project.md.
#
# `pyo3` is the modern binding and `cpython` its predecessor; `pyo3-ffi` is the raw
# CPython-ABI layer that `pyo3` builds on and can be depended upon directly; `python3-sys` is
# the equivalent under the `cpython` crate family. All four mean the same thing for T-10: the
# process would be linked against libpython. (#321)
FFI_CRATES = frozenset({"pyo3", "pyo3-ffi", "cpython", "python3-sys"})

# The graph must contain at least the root crate plus its direct dependencies. Deliberately a
# floor rather than an exact figure: the real count is 39 today, and pinning that would turn
# every legitimate dependency change into a failure of THIS check, which is not what it is
# for. The floor only has to be high enough that an empty or truncated graph cannot pass. (#321)
MIN_PLAUSIBLE_PACKAGES = 3

ROOT_CRATE = "cao-tui"


def _normalise(name: str) -> str:
    """Fold a crate name to its comparison form.

    Cargo treats ``-`` and ``_`` as distinct in crate names but interchangeable in many
    contexts (the lib target of ``pyo3-ffi`` is ``pyo3_ffi``), and registry names are
    lowercase. Normalising both prevents a trivially-evaded match — an exact string compare
    against ``"pyo3-ffi"`` would miss a ``pyo3_ffi`` entry. (#321)
    """
    return name.strip().lower().replace("_", "-")


NORMALISED_FFI_CRATES = {_normalise(c) for c in FFI_CRATES}


def load_graph(manifest_path: Path) -> dict:
    """Return the resolved cargo metadata graph, or raise with a diagnosable message.

    ``--locked`` is passed for the same reason CI passes it to ``cargo test`` and
    ``cargo build`` (TS-2, interview Q7): the check must run against the COMMITTED
    ``Cargo.lock``. Without it, cargo is free to re-resolve, and this gate would then be
    inspecting a graph that differs from the one the build uses — including, in principle, one
    where the FFI crate has been resolved away.
    """
    if shutil.which("cargo") is None:
        raise RuntimeError(
            "cargo not found on PATH — cannot inspect the dependency graph. "
            "This is a FAILED check: the graph was never examined."
        )

    # An argv vector, never an interpolated shell string (project.md Forbidden). No value here
    # is attacker-controlled, but the rule is deny-by-default for a reason and the cost is nil.
    argv = [
        "cargo",
        "metadata",
        "--locked",
        "--format-version",
        "1",
        "--manifest-path",
        str(manifest_path),
    ]

    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            check=False,
            # A language-level bound, deliberately NOT the coreutils `timeout(1)`: that binary
            # is absent on some developer machines (hit three times while building this unit)
            # and cannot be assumed present on a runner either. The job also carries
            # `timeout-minutes` as the outer bound. (#321)
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"`{' '.join(argv)}` timed out after 300s") from exc
    except OSError as exc:
        raise RuntimeError(f"could not execute `{' '.join(argv)}`: {exc}") from exc

    if proc.returncode != 0:
        raise RuntimeError(
            f"`cargo metadata` exited {proc.returncode}; the graph was never inspected.\n"
            f"--- stderr ---\n{proc.stderr.strip()}"
        )

    try:
        graph = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"`cargo metadata` emitted unparseable JSON: {exc}") from exc

    if not isinstance(graph, dict) or "packages" not in graph:
        raise RuntimeError("`cargo metadata` JSON has no 'packages' key — unexpected schema")

    return graph


def sanity_check(packages: list[dict]) -> None:
    """Refuse to render a verdict on a graph too small to be real.

    This is the anti-vacuity guard. "No FFI crate found" is only meaningful if the search
    actually looked at the dependency graph; an empty ``packages`` array would otherwise
    produce a confident pass.
    """
    if len(packages) < MIN_PLAUSIBLE_PACKAGES:
        raise RuntimeError(
            f"only {len(packages)} package(s) in the graph — implausibly small, so a "
            f"'no FFI crate' verdict would be vacuous. Expected at least "
            f"{MIN_PLAUSIBLE_PACKAGES}."
        )

    names = {_normalise(str(p.get("name", ""))) for p in packages}
    if _normalise(ROOT_CRATE) not in names:
        raise RuntimeError(
            f"root crate '{ROOT_CRATE}' absent from the graph — this is not the crate "
            f"we meant to check. Found: {sorted(names)[:10]}..."
        )


def find_dependents(packages: list[dict], target: str) -> list[str]:
    """Name every package that declares a dependency on ``target``.

    Reported so the failure is actionable: a transitive FFI crate is useless to know about
    without knowing which dependency dragged it in.
    """
    dependents = []
    for pkg in packages:
        for dep in pkg.get("dependencies") or []:
            if _normalise(str(dep.get("name", ""))) == _normalise(target):
                kind = dep.get("kind") or "normal"
                dependents.append(f"{pkg.get('name')} v{pkg.get('version')} ({kind})")
                break
    return sorted(dependents)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--manifest-path",
        type=Path,
        default=Path("tui/Cargo.toml"),
        help="path to the crate manifest to inspect (default: tui/Cargo.toml)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print every crate in the graph (diagnostic; the verdict is unchanged)",
    )
    args = parser.parse_args(argv)

    if not args.manifest_path.is_file():
        print(
            f"FAIL: manifest not found at {args.manifest_path} — nothing was inspected.",
            file=sys.stderr,
        )
        return 2

    try:
        graph = load_graph(args.manifest_path)
        packages = graph.get("packages") or []
        sanity_check(packages)
    except RuntimeError as exc:
        # Fail closed, loudly. A broken data source is not a clean graph.
        print(f"FAIL (no-FFI check could not be evaluated): {exc}", file=sys.stderr)
        return 2

    present = {}
    for pkg in packages:
        norm = _normalise(str(pkg.get("name", "")))
        if norm in NORMALISED_FFI_CRATES:
            present[f"{pkg.get('name')} v{pkg.get('version')}"] = find_dependents(
                packages, str(pkg.get("name", ""))
            )

    if args.list:
        print(f"--- {len(packages)} crates in the graph of {args.manifest_path} ---")
        for name in sorted(f"{p.get('name')} v{p.get('version')}" for p in packages):
            print(f"  {name}")
        print()

    if present:
        print(
            "FAIL: Python-FFI crate(s) present in the cao-tui dependency graph.\n"
            "\n"
            "T-10 (issue #321) forbids linking the Rust TUI against the Python runtime.\n"
            "The boundary is subprocess for CLI invocation and HTTP for server reads —\n"
            "no PyO3, no embedded interpreter, no shared in-process state.\n",
            file=sys.stderr,
        )
        for crate, dependents in sorted(present.items()):
            print(f"  BANNED: {crate}", file=sys.stderr)
            if dependents:
                for dep in dependents:
                    print(f"      required by: {dep}", file=sys.stderr)
            else:
                print("      required by: (a graph root / direct manifest entry)", file=sys.stderr)
        print(
            f"\nBanned set (FR-5.2 / SR-4): {', '.join(sorted(FFI_CRATES))}",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK: no Python-FFI crate in the dependency graph of {args.manifest_path} "
        f"({len(packages)} crates inspected; "
        f"banned set: {', '.join(sorted(FFI_CRATES))})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
