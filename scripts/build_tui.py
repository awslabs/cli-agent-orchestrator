#!/usr/bin/env python3
"""Build the Rust TUI binary into the wheel package dir, and prove it landed.

Issue #321. Two jobs, deliberately in one script so the assertion can never be
skipped independently of the build:

1. ``build``       — ``cargo build --release --locked`` in ``tui/``, then copy the
                     emitted binary to ``src/cli_agent_orchestrator/``, which is
                     where the fourth ``[tool.hatch.build] artifacts`` glob looks.
2. ``check``       — assert that glob resolves NON-EMPTY, either in the source tree
                     (pre-build gate) or inside a built ``.whl`` (``--wheel``, the
                     pre-publish gate).

WHY THE ASSERTION EXISTS
------------------------
hatchling treats an ``artifacts`` glob that matches nothing as a silent no-op — the
behaviour this repo already exhibits as defect D1: ``ext_apps/apps_static/`` is empty,
its glob is declared, and the published wheel ships it empty with every job green.

Applied to a compiled binary that is the same failure with a worse blast radius: an
operator installs the wheel, runs ``cao tui``, and there is no binary — while the
build, the publish, and the smoke test all reported success. So the glob is asserted
non-empty rather than assumed to match. A glob that "should" match is exactly what D1
was. (#321)

This mirrors the established precedent for prebuilt assets in this repo: ``web_ui/`` is
built by an explicit step (``npm run build`` in ``web/``) BEFORE ``uv build`` runs, not
by a hatchling build hook. This script is the Rust equivalent of that step.

Usage
-----
    python scripts/build_tui.py build              # cargo build + copy + source + size check
    python scripts/build_tui.py check              # source-tree glob check only
    python scripts/build_tui.py check --wheel dist # assert the binary is inside the wheel
    python scripts/build_tui.py check --require-all  # enforce all 4 globs (see D1 note)
    python scripts/build_tui.py check --wheel dist --max-binary-bytes 10485760
                                                   # + assert NFR-2's 10 MB ceiling

Exits 0 on success, non-zero with a single explanatory line on failure.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10 — tomli is a declared dependency there
    import tomli as tomllib  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parents[1]
CRATE_DIR = REPO_ROOT / "tui"
PACKAGE_DIR = REPO_ROOT / "src" / "cli_agent_orchestrator"

# Must match `[[bin]] name` in tui/Cargo.toml. Asserted, not assumed — see
# _binary_name_from_cargo_toml.
BINARY_STEM = "cao-tui"

# The single glob this unit owns and therefore enforces by default. The other three
# declared globs are reported but not enforced unless --require-all is passed: two of
# them are empty in a fresh checkout (that is pre-existing defect D1, out of scope for
# this intent), and failing on them here would make this gate un-runnable and get it
# disabled — which would defeat the whole point. --require-all is the switch for
# whoever fixes D1. (#321)
ENFORCED_GLOB = "src/cli_agent_orchestrator/cao-tui*"


class BuildError(Exception):
    """A build or assertion step failed. Rendered as one line, never a traceback."""


# NFR-2's ceiling, pinned at nfr-requirements 3.2 Q5. Read strictly as binary megabytes
# (10 * 1024 * 1024), the stricter of the two readings of "10 MB". Asserted per platform
# by `check --max-binary-bytes`; the measured macOS arm64 release binary is 408,528 bytes,
# 3.9% of this. The ceiling exists to catch a STEP CHANGE — debug symbols retained, LTO
# disabled — not gradual creep. (#321)
DEFAULT_MAX_BINARY_BYTES = 10 * 1024 * 1024

# macOS cross-compilation: cibuildwheel drives the macOS x86_64 build from an arm64 runner
# by setting ARCHFLAGS (see [tool.cibuildwheel.macos] in pyproject.toml), which is what
# hatchling's tag inference reads too. cargo does not read ARCHFLAGS, so it must be told
# the matching --target explicitly or it would emit an arm64 binary inside a wheel tagged
# x86_64 — a wheel that installs on Intel Macs and then fails to exec. That mismatch is
# invisible without this mapping, which is why it is derived from the same variable the
# tag comes from rather than configured separately. (#321)
_ARCHFLAGS_TO_RUST_TARGET = {
    "arm64": "aarch64-apple-darwin",
    "x86_64": "x86_64-apple-darwin",
}


def _binary_filename() -> str:
    """Platform-correct filename cargo emits for the TUI binary."""
    return f"{BINARY_STEM}.exe" if os.name == "nt" else BINARY_STEM


def _requested_macos_arch() -> str | None:
    """The single macOS arch requested via ARCHFLAGS, or None.

    Returns None when ARCHFLAGS is unset/empty (a native build), and also when it names
    MORE than one arch: a universal2 build needs a `lipo` of two cargo targets, which this
    script does not implement. Returning None there would silently produce a single-arch
    binary in a universal2-tagged wheel, so the caller raises instead — the configuration
    skips universal2 for exactly this reason.
    """
    archflags = os.environ.get("ARCHFLAGS", "")
    if not archflags.strip():
        return None
    archs = re.findall(r"-arch\s+(\S+)", archflags)
    unique = sorted(set(archs))
    if len(unique) != 1:
        raise BuildError(
            f"ARCHFLAGS requests {len(unique)} architectures ({', '.join(unique) or 'none'}); "
            "this script builds ONE cargo target and cannot produce a universal2 binary "
            "(that needs `lipo` of two targets). The cibuildwheel config restricts macOS to "
            "single-arch builds for this reason"
        )
    return unique[0]


def _load_pyproject() -> dict:
    path = REPO_ROOT / "pyproject.toml"
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BuildError(f"cannot read {path}: {exc}") from exc
    except ValueError as exc:  # tomllib.TOMLDecodeError subclasses ValueError
        raise BuildError(f"{path} is not valid TOML: {exc}") from exc


def _declared_artifact_globs(pyproject: dict) -> List[str]:
    """Every glob under ``[tool.hatch.build] artifacts``.

    Read from pyproject rather than hard-coded so this check cannot silently drift
    away from the config it is supposed to be checking.
    """
    artifacts = pyproject.get("tool", {}).get("hatch", {}).get("build", {}).get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise BuildError(
            "[tool.hatch.build] artifacts is missing or empty in pyproject.toml; "
            "the TUI binary would be excluded from the wheel"
        )
    return [g for g in artifacts if isinstance(g, str)]


def _binary_name_from_cargo_toml() -> str:
    """The ``[[bin]] name`` declared in tui/Cargo.toml.

    Cross-checked against BINARY_STEM so renaming the crate's binary without updating
    this script (and the artifacts glob) fails loudly here instead of shipping a wheel
    with nothing at the globbed path.
    """
    manifest = CRATE_DIR / "Cargo.toml"
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BuildError(f"cannot read {manifest}: {exc}") from exc
    except ValueError as exc:
        raise BuildError(f"{manifest} is not valid TOML: {exc}") from exc
    bins = data.get("bin")
    if not isinstance(bins, list) or not bins or not isinstance(bins[0], dict):
        raise BuildError(f"{manifest} declares no [[bin]] target")
    name = bins[0].get("name")
    if not isinstance(name, str) or not name:
        raise BuildError(f"{manifest} [[bin]] has no name")
    return name


def _resolve_source_glob(glob: str) -> List[Path]:
    """Files in the source tree matching a declared hatch ``artifacts`` glob.

    Only the two glob shapes this repo actually uses are handled:

    * ``some/dir/**``  — every file under a directory, recursively
    * ``some/dir/name*`` — filename wildcard within one directory

    An unrecognised shape raises rather than returning an empty list. Returning empty
    would make this check report "glob matches nothing" for a glob it simply failed to
    parse — a false failure is recoverable, but the mirror-image bug (a parser that
    silently returns "fine") is the vacuous guard this whole script exists to avoid.
    """
    if glob.endswith("/**"):
        root = REPO_ROOT / glob[: -len("/**")]
        if not root.is_dir():
            return []
        return [p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    parent, _, leaf = glob.rpartition("/")
    if "*" in parent or "**" in leaf:
        raise BuildError(
            f"unsupported artifacts glob shape {glob!r}; this checker understands "
            "'dir/**' and 'dir/name*' only — extend it rather than trusting it"
        )
    root = REPO_ROOT / parent
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob(leaf) if p.is_file())


def _wheel_member_glob(glob: str, pyproject: dict) -> str:
    """Translate a source-tree glob to the path it takes INSIDE the wheel.

    hatchling strips each configured package's parent prefix (``src/``), so
    ``src/cli_agent_orchestrator/cao-tui*`` is stored as ``cli_agent_orchestrator/cao-tui*``.
    The prefix is derived from ``[tool.hatch.build.targets.wheel] packages`` rather than
    hard-coded, so a layout change surfaces here instead of quietly mismatching.
    """
    packages = (
        pyproject.get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("wheel", {})
        .get("packages", [])
    )
    for pkg in packages:
        if not isinstance(pkg, str):
            continue
        prefix = pkg.rpartition("/")[0]
        if prefix and glob.startswith(prefix + "/"):
            return glob[len(prefix) + 1 :]
    return glob


def _find_wheel(target: Path) -> Path:
    """Resolve a wheel path from a file or a directory, failing loudly if absent.

    A missing wheel must fail, never pass quietly. This is the same defect class as a
    coverage ratchet that silently passes when its report file is missing.
    """
    if target.is_file():
        if target.suffix != ".whl":
            raise BuildError(f"{target} is not a .whl file")
        return target
    if not target.is_dir():
        raise BuildError(f"no such wheel or directory: {target}")
    wheels = sorted(target.glob("*.whl"))
    if not wheels:
        raise BuildError(
            f"no .whl found in {target} — run `uv build` first. A missing wheel is a "
            "failed check, not a passed one"
        )
    if len(wheels) > 1:
        # Ambiguity is a real risk here: dist/ accumulates wheels across builds and
        # checking a stale one would attest the wrong artifact.
        names = ", ".join(w.name for w in wheels)
        raise BuildError(
            f"{len(wheels)} wheels in {target} ({names}); pass the exact wheel to check "
            "so a stale build cannot be attested by mistake"
        )
    return wheels[0]


def _wheel_matches(wheel: Path, member_glob: str) -> List[str]:
    """Wheel members matching ``member_glob`` (directory entries excluded)."""
    try:
        with zipfile.ZipFile(wheel) as zf:
            names = zf.namelist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise BuildError(f"cannot read wheel {wheel}: {exc}") from exc
    pattern = member_glob.replace("/**", "/**/*") if member_glob.endswith("/**") else member_glob
    matched = []
    for name in names:
        if name.endswith("/"):
            continue
        if fnmatch.fnmatch(name, pattern) or (
            pattern.endswith("/**/*") and name.startswith(pattern[: -len("**/*")])
        ):
            matched.append(name)
    return sorted(matched)


def _assert_binary_size(wheel: Optional[Path], max_bytes: int) -> None:
    """Assert every TUI binary is under NFR-2's ceiling, and PRINT what it measured.

    Measures the binary INSIDE the wheel when one is given, and the staged source-tree
    binary otherwise. Checking the wheel is the point at publish time: it attests the
    artifact that ships rather than the tree it was built from.

    Prints the measured size on success as well as failure, so a CI log records the number
    and the headroom is a fact rather than an assumption. A check that only speaks up when
    it fails leaves nobody able to see the trend before the step change arrives. (#321)
    """
    measured: List[Tuple[str, int]] = []
    if wheel is not None:
        try:
            with zipfile.ZipFile(wheel) as zf:
                measured = [
                    (info.filename, info.file_size)
                    for info in zf.infolist()
                    if not info.is_dir() and Path(info.filename).name.startswith(BINARY_STEM)
                ]
        except (OSError, zipfile.BadZipFile) as exc:
            raise BuildError(f"cannot read wheel {wheel}: {exc}") from exc
    else:
        measured = [
            (str(p.relative_to(REPO_ROOT)), p.stat().st_size)
            for p in sorted(PACKAGE_DIR.glob(f"{BINARY_STEM}*"))
            if p.is_file()
        ]

    if not measured:
        # Never pass quietly on "nothing to measure": that is the same vacuous-guard defect
        # as a ratchet that silently passes when its report file is absent.
        where = f"wheel {wheel.name}" if wheel is not None else "the source tree"
        raise BuildError(
            f"no {BINARY_STEM}* binary found in {where}, so the NFR-2 size ceiling could not "
            "be checked. A missing binary is a FAILED check, not a passed one"
        )

    for name, size in measured:
        pct = 100.0 * size / max_bytes
        if size > max_bytes:
            raise BuildError(
                f"{name} is {size} bytes, over NFR-2's ceiling of {max_bytes} bytes "
                f"({pct:.1f}%). A jump this large normally means debug symbols were retained "
                "or LTO was disabled — check the release profile rather than raising the ceiling"
            )
        print(f"  [OK] {size:>9} bytes  {pct:5.1f}% of the {max_bytes}-byte NFR-2 ceiling  {name}")


def do_build() -> None:
    """cargo build --release --locked, then stage the binary for the wheel."""
    if not CRATE_DIR.is_dir():
        raise BuildError(f"no Rust crate at {CRATE_DIR}")

    declared = _binary_name_from_cargo_toml()
    if declared != BINARY_STEM:
        raise BuildError(
            f"tui/Cargo.toml declares [[bin]] name = {declared!r} but this script and the "
            f"pyproject artifacts glob expect {BINARY_STEM!r}; update all three together"
        )

    if shutil.which("cargo") is None:
        raise BuildError(
            "cargo is not on PATH — install the Rust toolchain "
            "(https://rustup.rs) before building the TUI binary"
        )

    # --locked so the build fails if Cargo.lock would change: the bundled binary must be
    # built from the pinned dependency set, not from whatever resolves today. (#321)
    command: List[str] = ["cargo", "build", "--release", "--locked"]

    # Honour a cibuildwheel-driven macOS cross-build. When ARCHFLAGS names an arch other
    # than this machine's, cargo must be given the matching --target or it silently builds
    # for the host: an arm64 binary inside an x86_64-tagged wheel installs on Intel Macs
    # and then fails to exec. cargo also nests cross-target output one directory deeper,
    # so the staging path below follows the same branch. (#321)
    target_triple: Optional[str] = None
    if sys.platform == "darwin":
        requested_arch = _requested_macos_arch()
        if requested_arch is not None:
            target_triple = _ARCHFLAGS_TO_RUST_TARGET.get(requested_arch)
            if target_triple is None:
                raise BuildError(
                    f"ARCHFLAGS requests macOS arch {requested_arch!r}, which has no known Rust "
                    f"target here (known: {', '.join(sorted(_ARCHFLAGS_TO_RUST_TARGET))}). "
                    "Refusing to build for the host arch under a wheel tagged for another"
                )
            command += ["--target", target_triple]
            print(f"cross-build requested via ARCHFLAGS: {requested_arch} -> {target_triple}")

    print(f"$ (cd tui && {' '.join(command)})", flush=True)
    try:
        result = subprocess.run(command, cwd=CRATE_DIR)
    except OSError as exc:
        raise BuildError(f"failed to run cargo: {exc}") from exc
    if result.returncode != 0:
        raise BuildError(f"cargo build failed with exit code {result.returncode}")

    # cargo writes to target/release/ for a host build and target/<triple>/release/ for a
    # cross build. Asserted below rather than assumed, so a wrong guess fails loudly here
    # instead of staging a stale binary from a previous host build — which would be an
    # arch-mismatched wheel that passes every build gate.
    out_dir = CRATE_DIR / "target" / (target_triple or "") / "release"
    built = out_dir / _binary_filename()
    if not built.is_file():
        raise BuildError(
            f"cargo reported success but {built} does not exist; refusing to stage a "
            "binary that was not produced by this build"
        )

    PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    staged = PACKAGE_DIR / _binary_filename()
    # UNLINK BEFORE COPYING, so the copy lands on a NEW inode.
    #
    # `shutil.copy2` onto an existing path writes THROUGH it, keeping the inode. On macOS the
    # kernel caches code-signature pages per inode, so a Mach-O overwritten at a path it has
    # already been executed from is SIGKILLed on the next exec — exit 137, zero output, no
    # diagnostic. Measured: the overwritten file and its source were byte-identical, both
    # `codesign --verify` valid, and only the overwritten one died. `rm` + `cp` fixed it.
    #
    # An operator hit this as a silent `cao tui` failure after a rebuild, which looks exactly
    # like the TUI hanging on cao-server. `missing_ok=True` keeps a first-ever build working.
    # (#321)
    staged.unlink(missing_ok=True)
    shutil.copy2(built, staged)
    # copy2 preserves mode, but assert it rather than trusting it: a wheel carrying a
    # non-executable binary fails at `cao tui` time on the operator's machine.
    staged.chmod(staged.stat().st_mode | 0o111)
    print(f"staged {staged.relative_to(REPO_ROOT)} ({staged.stat().st_size} bytes)")


def do_check(
    wheel_target: Path | None,
    require_all: bool,
    max_binary_bytes: Optional[int] = None,
) -> None:
    """Assert declared artifacts globs resolve non-empty (source tree or wheel).

    When ``max_binary_bytes`` is given, also assert NFR-2's size ceiling on the TUI binary.
    """
    pyproject = _load_pyproject()
    globs = _declared_artifact_globs(pyproject)

    if ENFORCED_GLOB not in globs:
        raise BuildError(
            f"pyproject.toml [tool.hatch.build] artifacts does not declare {ENFORCED_GLOB!r}; "
            "without it hatchling excludes the TUI binary and the wheel ships without it"
        )

    wheel = _find_wheel(wheel_target) if wheel_target is not None else None
    if wheel is not None:
        print(f"checking wheel {wheel.name}")

    results: Dict[str, Tuple[int, str]] = {}
    for glob in globs:
        if wheel is not None:
            member_glob = _wheel_member_glob(glob, pyproject)
            matches = _wheel_matches(wheel, member_glob)
            results[glob] = (len(matches), member_glob)
        else:
            results[glob] = (len(_resolve_source_glob(glob)), glob)

    failures = []
    for glob, (count, checked) in results.items():
        enforced = require_all or glob == ENFORCED_GLOB
        status = "OK " if count else ("FAIL" if enforced else "warn")
        print(f"  [{status}] {count:>4} file(s)  {checked}")
        if count == 0 and enforced:
            failures.append(glob)

    if failures:
        where = f"wheel {wheel.name}" if wheel is not None else "the source tree"
        detail = "; ".join(failures)
        remedy = "run `python scripts/build_tui.py build` (and `uv build` again) before publishing"
        raise BuildError(
            f"artifacts glob(s) resolve EMPTY in {where}: {detail}. hatchling treats an "
            f"empty glob as a no-op, so publishing now ships a wheel with no TUI binary "
            f"while every job reports success (defect D1's failure mode). {remedy}"
        )

    if not require_all:
        empty_unenforced = [g for g, (c, _) in results.items() if c == 0]
        if empty_unenforced:
            # Reported, not failed: these are pre-existing D1 territory, out of scope for
            # issue #321. Named explicitly so the warning is not mistaken for noise.
            print(
                "note: "
                + ", ".join(empty_unenforced)
                + " resolve empty (pre-existing defect D1 — not enforced here; "
                "use --require-all once D1 is fixed)"
            )

    # NFR-2 runs after the glob check on purpose: an absent binary must be reported as the
    # missing-artifact failure (which names the remedy) rather than as a size failure.
    if max_binary_bytes is not None:
        print(f"checking NFR-2 binary size ceiling ({max_binary_bytes} bytes)")
        _assert_binary_size(wheel, max_binary_bytes)

    print("artifacts check passed")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("build", help="cargo build --release --locked, stage into the package dir")

    check = sub.add_parser("check", help="assert artifacts globs resolve non-empty")
    check.add_argument(
        "--wheel",
        type=Path,
        default=None,
        metavar="PATH",
        help="a .whl file or a directory containing exactly one; checks INSIDE the wheel "
        "rather than the source tree (the pre-publish gate)",
    )
    check.add_argument(
        "--require-all",
        action="store_true",
        help="fail on ANY empty declared glob, not just the TUI binary's",
    )
    check.add_argument(
        "--max-binary-bytes",
        type=int,
        default=None,
        metavar="N",
        help="also assert the TUI binary is at most N bytes (NFR-2's ceiling is "
        f"{DEFAULT_MAX_BINARY_BYTES}); measured inside the wheel when --wheel is given",
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            do_build()
            # The build is not done until the glob it feeds is proven non-empty AND the
            # binary is within NFR-2's ceiling. Keeping all three together means neither
            # assertion can be skipped by calling only `build`. The ceiling is applied by
            # default here (not opt-in) so a local build cannot produce an oversized binary
            # that only CI would catch. (#321)
            do_check(None, require_all=False, max_binary_bytes=DEFAULT_MAX_BINARY_BYTES)
        else:
            do_check(
                args.wheel,
                require_all=args.require_all,
                max_binary_bytes=args.max_binary_bytes,
            )
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
