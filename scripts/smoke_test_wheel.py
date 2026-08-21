#!/usr/bin/env python3
"""Per-platform wheel smoke test: prove the shipped wheel actually works here.

Issue #321, requirement SR-1. Run by cibuildwheel's ``test-command`` on every platform,
inside a venv where the just-built wheel is already installed.

WHY THIS EXISTS AS AN EXECUTION AND NOT AN IMPORT CHECK
-------------------------------------------------------
The existing TestPyPI smoke test in ``publish-to-pypi.yml`` runs ``cao --help`` and imports
``cao_workflow``. Both pass with **no TUI binary in the wheel at all** — hatchling silently
drops an ``artifacts`` glob that matched nothing (defect D1's failure mode), so the wheel
installs cleanly and only fails when the operator runs ``cao tui``. An import check would
also pass for a wheel whose binary was built for the *wrong architecture*, because nothing
ever execs it.

So this script **runs the binary**. Three assertions, each targeting a distinct real
failure this unit exists to prevent:

1. **The wheel's tag is platform-specific.** A ``py3-none-any`` wheel carrying a native
   executable is issue #321's defect verbatim: pip installs it on a platform where the
   binary cannot run. Measured on a real artifact from this branch before the fix —
   ``Tag: py3-none-any`` with a ``Mach-O 64-bit arm64`` executable inside.
2. **The bundled binary is under NFR-2's 10 MB ceiling.** Asserted against the binary
   *inside the wheel*, not the source tree, so it attests the shipped artifact. The ceiling
   catches a step change — debug symbols retained, LTO disabled — rather than creep.
3. **``cao tui`` execs and exits 0.** This is the only assertion that can fail for an
   architecture mismatch, a non-executable mode bit, or a missing dynamic loader.

Each assertion prints what it measured, so a CI log records the numbers rather than just
"passed".

Usage
-----
    python scripts/smoke_test_wheel.py --wheel <path.whl> [--max-binary-bytes N]

Exits 0 on success; non-zero with a single explanatory line on failure.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import List, Optional, Sequence

# Must match `[[bin]] name` in tui/Cargo.toml, the pyproject artifacts glob,
# scripts/build_tui.py's BINARY_STEM, and cli/commands/tui.py's _BINARY_STEM.
BINARY_STEM = "cao-tui"

# NFR-2, pinned at nfr-requirements 3.2 Q5. Binary MEBIbytes: the ceiling is stated as
# "10 MB" and read strictly here as 10 * 1024 * 1024, the stricter of the two readings.
DEFAULT_MAX_BINARY_BYTES = 10 * 1024 * 1024


class SmokeTestError(Exception):
    """An assertion failed. Rendered as one line, never a traceback."""


def _wheel_tag(wheel: Path) -> str:
    """The ``Tag:`` line(s) from the wheel's own ``WHEEL`` metadata.

    Read from the metadata rather than parsed out of the filename: the filename and the
    metadata can disagree (a retagging tool that rewrote one and not the other), and the
    metadata is what installers treat as authoritative for ``Root-Is-Purelib``.
    """
    try:
        with zipfile.ZipFile(wheel) as zf:
            wheel_members = [n for n in zf.namelist() if n.endswith(".dist-info/WHEEL")]
            if not wheel_members:
                raise SmokeTestError(f"{wheel.name} contains no .dist-info/WHEEL metadata")
            text = zf.read(wheel_members[0]).decode("utf-8")
    except (OSError, zipfile.BadZipFile) as exc:
        raise SmokeTestError(f"cannot read wheel {wheel}: {exc}") from exc

    tags = [line.partition(":")[2].strip() for line in text.splitlines() if line.startswith("Tag:")]
    if not tags:
        raise SmokeTestError(f"{wheel.name}'s WHEEL metadata declares no Tag:")
    return ", ".join(tags)


def _binary_members(wheel: Path) -> List[zipfile.ZipInfo]:
    """Every bundled TUI binary entry inside the wheel (``cao-tui`` / ``cao-tui.exe``)."""
    try:
        with zipfile.ZipFile(wheel) as zf:
            return [
                info
                for info in zf.infolist()
                if not info.is_dir() and Path(info.filename).name.startswith(BINARY_STEM)
            ]
    except (OSError, zipfile.BadZipFile) as exc:
        raise SmokeTestError(f"cannot read wheel {wheel}: {exc}") from exc


def assert_platform_tag(wheel: Path) -> None:
    """Assert the wheel is NOT tagged ``*-none-any`` — issue #321's defect.

    Checked on the ``any`` PLATFORM component rather than by string-matching the exact
    literal ``py3-none-any``: the interpreter part legitimately varies (``py3``, ``cp310``),
    but a platform of ``any`` on a wheel carrying a native executable is always wrong.
    """
    tag = _wheel_tag(wheel)
    platforms = {part.rsplit("-", 1)[-1] for part in tag.replace(", ", " ").split()}
    if "any" in platforms:
        raise SmokeTestError(
            f"{wheel.name} is tagged '{tag}' — platform 'any' — while bundling a native "
            f"{BINARY_STEM} executable. That is issue #321's defect: pip installs an 'any' "
            "wheel on every platform, including ones where this binary cannot execute. The "
            "wheel must carry a platform-specific tag so pip REFUSES it on an incompatible "
            "host. Check that scripts/hatch_build_tui_tag.py ran and that the binary was "
            "staged before the build"
        )
    print(f"  [OK] wheel tag is platform-specific: {tag}")


def assert_binary_size(wheel: Path, max_bytes: int) -> None:
    """Assert every bundled binary inside the wheel is under NFR-2's ceiling."""
    members = _binary_members(wheel)
    if not members:
        raise SmokeTestError(
            f"{wheel.name} contains no {BINARY_STEM}* binary. hatchling treats an artifacts "
            "glob matching nothing as a silent no-op, so this wheel would install cleanly and "
            "fail only when an operator runs `cao tui`"
        )
    for info in members:
        pct = 100.0 * info.file_size / max_bytes
        if info.file_size > max_bytes:
            raise SmokeTestError(
                f"{info.filename} is {info.file_size} bytes, over NFR-2's ceiling of "
                f"{max_bytes} bytes ({pct:.1f}%). A jump of this size usually means debug "
                "symbols were retained or LTO was disabled — check the release profile "
                "rather than raising the ceiling"
            )
        print(
            f"  [OK] {info.filename}: {info.file_size} bytes "
            f"({pct:.1f}% of the {max_bytes}-byte NFR-2 ceiling)"
        )


def assert_cao_tui_runs() -> None:
    """Assert ``cao tui`` execs the bundled binary and exits 0.

    Invoked as an argv VECTOR with ``shell=False`` — the same rule the production
    ``cli/commands/tui.py`` follows, so this test cannot pass via a shell path that
    production never takes.
    """
    command: Sequence[str] = ["cao", "tui"]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            # A hung TUI must fail this job fast rather than burn the CI budget — the same
            # per-read-deadline reasoning the pty harness uses.
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise SmokeTestError(
            f"`cao` is not on PATH in the test environment: {exc}. cibuildwheel runs "
            "test-command inside a venv with the wheel installed; if `cao` is missing, the "
            "wheel's entry points did not install"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SmokeTestError(
            f"`cao tui` did not exit within {exc.timeout}s. The skeleton binary prints one "
            "line and exits, so a hang means it is blocking on something — fail fast rather "
            "than let CI time out at the job level"
        ) from exc

    if result.returncode != 0:
        raise SmokeTestError(
            f"`cao tui` exited {result.returncode} — the bundled binary did not run on this "
            f"platform. This is the failure a per-platform wheel exists to prevent.\n"
            f"  stdout: {result.stdout.strip()!r}\n"
            f"  stderr: {result.stderr.strip()!r}"
        )
    print(f"  [OK] `cao tui` ran and exited 0; stdout: {result.stdout.strip()!r}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--wheel",
        type=Path,
        required=True,
        metavar="PATH",
        help="the .whl just built for this platform (cibuildwheel substitutes {wheel})",
    )
    parser.add_argument(
        "--max-binary-bytes",
        type=int,
        default=DEFAULT_MAX_BINARY_BYTES,
        metavar="N",
        help=f"NFR-2 ceiling for the bundled binary (default: {DEFAULT_MAX_BINARY_BYTES})",
    )
    args = parser.parse_args(argv)

    try:
        if not args.wheel.is_file():
            raise SmokeTestError(
                f"no such wheel: {args.wheel}. A missing wheel is a FAILED check, never a "
                "passed one"
            )
        print(f"smoke-testing {args.wheel.name}")
        assert_platform_tag(args.wheel)
        assert_binary_size(args.wheel, args.max_binary_bytes)
        assert_cao_tui_runs()
    except SmokeTestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("wheel smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
