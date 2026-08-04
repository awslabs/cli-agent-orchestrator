"""Hatchling build hook: tag the wheel for the platform its Rust binary targets.

Issue #321. This is the fix for the defect that `wheel-matrix` owns.

THE DEFECT
----------
The wheel built from this branch was tagged ``py3-none-any`` while carrying a
``Mach-O 64-bit arm64`` executable (both facts read from a real built artifact, not
inferred). ``py3-none-any`` asserts "pure Python, runs anywhere", so ``pip`` happily
installs that wheel on Linux and Windows — where the bundled ``cao-tui`` cannot execute.
The operator then gets an exec failure at ``cao tui`` time, long after every build,
publish, and smoke-test job reported success.

A wheel carrying a native binary must declare the platform that binary targets, so pip
REFUSES it on an incompatible host instead of installing something broken. That is what
this hook does.

WHY A BUILD HOOK, AND NOT CONFIGURATION
---------------------------------------
There is no pyproject key for this. Verified by reading hatchling 1.31's source: the wheel
filename comes from ``build_data["tag"]`` and ``Root-Is-Purelib`` from
``build_data["pure_python"]``::

    wheel.py:517   if "tag" not in build_data:
    wheel.py:519       if build_data["infer_tag"]:
    wheel.py:520           build_data["tag"] = self.get_best_matching_tag()
    wheel.py:522       else: build_data["tag"] = self.get_default_tag()
    wheel.py:769   Root-Is-Purelib: {"true" if build_data["pure_python"] else "false"}
    wheel.py:875   "infer_tag": False,
    wheel.py:876   "pure_python": True,

``infer_tag`` and ``pure_python`` appear NOWHERE else in hatchling — there is no
``[tool.hatch.build.targets.wheel] infer-tag`` option to set. ``build_data`` is reachable
only from a build hook's ``initialize()``, which hatchling calls immediately before the
artifact is built (``builders/plugin/interface.py:147-148``, then ``:155-156``). So a hook
is the only supported mechanism, not a preference.

THE PLATFORM COMPONENT COMES FROM HATCHLING; THE INTERPRETER COMPONENT DOES NOT
------------------------------------------------------------------------------
This hook takes the *platform* part of hatchling's own ``get_best_matching_tag()`` and
pairs it with ``py3-none``. It deliberately does not construct a platform tag itself:
hand-rolling one would mean re-implementing ``packaging.tags`` plus the macOS
deployment-target and ARCHFLAGS normalisation in ``hatchling/builders/macos.py``, and a tag
that is merely *plausible* rather than correct is worse than ``py3-none-any`` because it is
wrong in a way nobody reads. Verified that hatchling's function already honours the
cross-compilation mechanism cibuildwheel uses (it sets ARCHFLAGS and _PYTHON_HOST_PLATFORM
per build identifier)::

    ARCHFLAGS='-arch x86_64'  -> cp3XX-cp3XX-macosx_..._x86_64
    ARCHFLAGS='-arch arm64'   -> cp3XX-cp3XX-macosx_..._arm64

But the ``cp3XX-cp3XX`` half is taken from the *running* interpreter and must NOT be
shipped: this wheel contains no CPython extension module, and pinning it to one ABI would
make it uninstallable on every other Python. See the long comment at the assignment in
``initialize()`` — that trap is the reason this hook sets ``tag`` explicitly instead of
just flipping ``infer_tag``.

WHY THE OPT-IN IS AN ENVIRONMENT VARIABLE
-----------------------------------------
The tag must be platform-specific ONLY when the binary is actually in the tree. A plain
``uv build`` by a contributor with no Rust toolchain must still produce an installable
pure-Python wheel — otherwise every existing source workflow breaks, and `cao tui` already
degrades with a clear operator message when the binary is absent.

So the hook is inert unless it can see the staged binary. It also refuses to guess: if the
binary is present but the environment asked for a pure wheel (or vice versa), that
disagreement is a hard error rather than a silent choice, because "silently produced the
wrong tag" is the defect class this whole file exists to close. (#321)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

# Must match `[[bin]] name` in tui/Cargo.toml, the artifacts glob in pyproject.toml,
# scripts/build_tui.py's BINARY_STEM, and cli/commands/tui.py's _BINARY_STEM.
# scripts/build_tui.py cross-checks the Cargo manifest against its own copy and fails
# the build if they diverge; test/scripts/test_wheel_matrix.py asserts all five agree.
BINARY_STEM = "cao-tui"

# Relative to the project root — the directory the wheel's fourth artifacts glob points
# at, and where scripts/build_tui.py stages the compiled binary.
PACKAGE_RELATIVE_DIR = "src/cli_agent_orchestrator"

# Set by cibuildwheel for every build it drives (cibuildwheel/__main__.py:370
# `os.environ["CIBUILDWHEEL"] = "1"`). Used only as a signal that a platform wheel is
# EXPECTED, so a missing binary under cibuildwheel fails loudly instead of silently
# emitting a pure wheel that cibuildwheel would then reject with a less specific error.
CIBUILDWHEEL_ENV = "CIBUILDWHEEL"

# Explicit local override, for building a platform wheel outside cibuildwheel (which is
# how the macOS path was proven locally). "1"/"true"/"yes"/"on" enable it.
FORCE_ENV = "CAO_TUI_PLATFORM_WHEEL"

_TRUTHY = {"1", "true", "yes", "on"}


def _env_flag(name: str) -> bool | None:
    """Tri-state read of a boolean env var: True, False, or None when unset/blank.

    None (unset) and False (explicitly "0") are genuinely different here: unset means
    "decide from the tree", while an explicit falsey value means "the caller asked for a
    pure wheel" and must be honoured — and contradicted loudly if the binary is present.
    Collapsing the two would turn an explicit request into a guess.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() in _TRUTHY


def staged_binaries(root: str) -> List[Path]:
    """Every staged TUI binary in the package dir (``cao-tui`` and/or ``cao-tui.exe``).

    Globbed rather than checked by exact name so a Windows build (``cao-tui.exe``) is
    detected by the same code path as a POSIX one, matching the trailing ``*`` in the
    pyproject artifacts glob this mirrors.
    """
    package_dir = Path(root) / PACKAGE_RELATIVE_DIR
    if not package_dir.is_dir():
        return []
    return sorted(p for p in package_dir.glob(f"{BINARY_STEM}*") if p.is_file())


def resolve_build_data(
    root: str,
    target_name: str,
    best_matching_tag: str,
    build_data: Dict[str, Any],
) -> None:
    """Decide this wheel's tag and purity, mutating ``build_data`` in place.

    Deliberately a MODULE-LEVEL function taking ``best_matching_tag`` as a plain string
    rather than a method reaching through ``self.build_config.builder``. That keeps the
    actual fix — the tag decision, which is the entire point of this unit — testable in an
    environment that has no ``hatchling`` installed. ``hatchling`` is a *build* dependency,
    so it is absent from the dev/test environment, and a decision buried in a method would
    have meant the fix's own tests were skipped in CI's test job. A guard that does not run
    is not a guard. (#321)

    Raises ``ValueError`` on any contradiction rather than picking a side: "silently emitted
    the wrong tag" is the defect class this file exists to close.
    """
    # Only the wheel target is touched. An sdist carries source, not a compiled binary, so
    # tagging it would be meaningless.
    if target_name != "wheel":
        return

    binaries = staged_binaries(root)
    requested = _env_flag(FORCE_ENV)
    if requested is None and os.environ.get(CIBUILDWHEEL_ENV):
        # Under cibuildwheel a platform wheel is the entire point of the run.
        requested = True

    # A caller that explicitly asked for a pure wheel while a native binary is staged would
    # get a py3-none-any wheel CONTAINING that binary — precisely the defect. Refuse rather
    # than pick one, and name both remedies.
    if requested is False and binaries:
        names = ", ".join(p.name for p in binaries)
        msg = (
            f"{FORCE_ENV} is set falsey but a native TUI binary is staged in "
            f"{PACKAGE_RELATIVE_DIR} ({names}). Building a py3-none-any wheel around a "
            "native executable is exactly issue #321's defect: pip would install it on "
            "an incompatible platform and `cao tui` would fail at exec time. Either "
            f"remove the staged binary to build a genuinely pure wheel, or unset {FORCE_ENV}."
        )
        raise ValueError(msg)

    # cibuildwheel exists to produce platform wheels; a missing binary there means the
    # before-build step did not run or did not stage. Failing here names the real cause,
    # whereas cibuildwheel's own NonPlatformWheelError ("a pure Python wheel was generated")
    # sends the reader looking in the wrong place.
    if requested and not binaries:
        msg = (
            "a platform-tagged wheel was requested "
            f"({FORCE_ENV}/{CIBUILDWHEEL_ENV}) but no {BINARY_STEM}* binary is staged in "
            f"{PACKAGE_RELATIVE_DIR}. Run `python scripts/build_tui.py build` first "
            "(cibuildwheel does this in `before-build`). Refusing to emit a platform "
            "wheel with no binary in it."
        )
        raise ValueError(msg)

    if not binaries:
        # No binary and nothing requested: a pure-Python wheel is correct here. This is the
        # contributor-without-a-Rust-toolchain path, which must keep working.
        return

    # THE FIX. Two parts, and the second is easy to get wrong.
    #
    # `pure_python = False` writes `Root-Is-Purelib: false`, so the metadata agrees with the
    # filename that this wheel is platform-specific.
    build_data["pure_python"] = False

    # The tag is set EXPLICITLY rather than via `infer_tag`. `infer_tag = True` makes
    # hatchling call get_best_matching_tag(), which returns the running interpreter's FULL
    # tag — measured here as `cp312-cp312-macosx_26_0_arm64`. That is wrong for this wheel in
    # a way that is worse than the bug being fixed:
    #
    #   * `cp312-cp312` pins the wheel to CPython 3.12's ABI, but this project declares
    #     `requires-python = ">=3.10"` and contains NO CPython extension module — the bundled
    #     `cao-tui` is a standalone executable, not a linked `.so`. Verified: the built wheel
    #     contains zero `.so`/`.pyd` files.
    #   * Under cibuildwheel the build runs on cp310, so the tag would be
    #     `cp310-cp310-<plat>` — which pip REFUSES to install on 3.11, 3.12, 3.13 and 3.14.
    #     Measured: `cp310-cp310-macosx_26_0_arm64` is not in this 3.12's supported tag set,
    #     while `py3-none-macosx_26_0_arm64` is. Shipping that would break every operator not
    #     on 3.10 — a wider outage than the defect it replaced.
    #   * `py3-none-<plat>` is also what makes cibuildwheel reuse ONE wheel per platform
    #     across interpreters: find_compatible_wheel() only matches previously built wheels
    #     whose abi is `none` and whose interpreter starts with `py3`
    #     (util/packaging.py:163-166).
    #
    # So: keep hatchling's PLATFORM component — it already handles macOS deployment-target
    # normalisation and ARCHFLAGS cross-compilation, which is how the macOS x86_64 build gets
    # an x86_64 tag from an arm64 runner — and replace only the interpreter/ABI components
    # with the ABI-independent `py3-none`. (#321)
    platform_tag = best_matching_tag.rsplit("-", 1)[-1]
    if not platform_tag or platform_tag == "any":
        # Never silently fall through to an `any` platform: that is the exact defect.
        msg = (
            f"hatchling resolved the platform tag to {platform_tag!r} from "
            f"{best_matching_tag!r}, which would produce a platform-independent wheel around "
            "a native binary — issue #321's defect. Refusing to build rather than emitting "
            "an 'any' wheel."
        )
        raise ValueError(msg)

    build_data["tag"] = f"py3-none-{platform_tag}"


# Imported AFTER the pure logic above so that logic stays importable without hatchling
# (see resolve_build_data's docstring). Kept at module scope, not inside a function, because
# hatchling's loader needs the subclass to exist at import time.
from hatchling.builders.hooks.plugin.interface import BuildHookInterface  # noqa: E402


class TuiPlatformTagBuildHook(BuildHookInterface):
    """Thin hatchling adapter: read the platform tag, delegate the decision."""

    PLUGIN_NAME = "cao-tui-platform-tag"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        """Set ``tag``/``pure_python`` before hatchling computes the wheel filename."""
        best: Optional[str] = None
        if self.target_name == "wheel":
            # Reached only for the wheel target: BuilderConfig for other targets has no
            # get_best_matching_tag(), so asking unconditionally would raise on an sdist.
            best = self.build_config.builder.get_best_matching_tag()
        resolve_build_data(self.root, self.target_name, best or "", build_data)


def get_build_hook() -> type[TuiPlatformTagBuildHook]:
    """Hatchling's documented selection hook for a ``custom`` build-hook script.

    ``hatchling/plugin/utils.py:20-23`` looks for a function named ``get_build_hook``
    FIRST and returns its result; only if that name is absent does it fall back to
    scanning the module for subclasses of ``BuildHookInterface``.

    Declared explicitly because the obvious alternative is a trap: writing
    ``build_hook = TuiPlatformTagBuildHook`` puts TWO names for the same class in
    ``dir(module)``, and the fallback scan collects both and dies with "Multiple
    subclasses of `BuildHookInterface` found ... select one by defining a function named
    `get_build_hook`" (``utils.py:41-45``). Verified empirically against hatchling 1.31 —
    the alias form failed to load, this form loads. (#321)
    """
    return TuiPlatformTagBuildHook
