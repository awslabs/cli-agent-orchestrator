"""Structural guard for the CLASS behind issue #705.

THE CLASS: hatchling treats a `[tool.hatch.build] artifacts` glob that matches nothing as a
SILENT no-op — no error, no warning (pyproject.toml:371 says so, and #321 and #610 are both
instances). Every entry in that list therefore has the same failure mode: narrow it, typo it
or delete it and the wheel ships a partial artifact while every build and publish job reports
success. #705's instance was the `web_ui/**` entry verified by one hardcoded member
(`web_ui/index.html`), which a glob narrowed to that very path still satisfies.

`test/test_wheel_contents.py` fixes that one entry. This file guards the list. There are four
entries (pyproject.toml:349-373) and before this file NO test read that list at all, so:

  * `ext_apps/apps_static/**` and `ext_apps/static/**` had no wheel-contents assertion
    anywhere (`grep -rn apps_static test/ .github/` was empty);
  * `cao-tui*` is asserted only at RELEASE time (`publish-to-pypi.yml:272`), not in CI;
  * a fifth entry added later would inherit zero coverage.

THREE DIRECTIONS, so the class is closed rather than one more instance:

  1. `test_force_include_roots_are_globbed_whole` — every root below must be force-included
     with a recursive `<root>/**` pattern. Pure config, no build, NEVER skips, so it runs in
     all three `Unit Tests` legs. This is the direction that catches #705's defect reintroduced
     as a narrowed glob, and it catches it without the web UI being built.
  2. `test_artifacts_entries_are_all_guarded` — every entry in `artifacts` is either one of
     those roots' `/**` pattern or in `OTHERWISE_GUARDED`. A new force-include glob added
     without a guard fails here with instructions, instead of silently joining the unguarded
     set.
  3. `test_force_include_roots_ship_whole_into_the_wheel` — build a real wheel and assert
     every file present under each root is a member. Per-root skip when the root is absent
     (unbuilt output in a plain checkout), with a non-vacuity assertion so an all-absent tree
     cannot pass. `ext_apps/static/` is committed, so this direction always has at least one
     root to check, including in the `Unit Tests` legs where test_wheel_contents.py can only
     skip — but for that one root it can only catch a `packages`/exclude regression, not an
     `artifacts`-glob regression: the directory is tracked (not gitignored), so it ships via
     normal packaging regardless of what its `artifacts` entry says. Only a gitignored root
     (like `web_ui/`, present only after the `web-build` job's `npm run build`) actually
     exercises the `artifacts`-glob failure mode this direction exists to catch.
"""

from __future__ import annotations

from pathlib import Path

# Reuses test_wheel_contents.py's `uv build` invocation so there is a single definition of
# "how this repo builds a wheel for a packaging assertion" (CAO_TUI_AUTOBUILD=0 so no cargo
# compile, 300s timeout, exit-code and .whl-produced checks).
from test.test_wheel_contents import _build_wheel_members, _shippable_files

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"

# `packages = ["src/cli_agent_orchestrator", ...]` (pyproject.toml:155) strips the `src/`
# prefix inside the wheel.
_SRC_PREFIX = "src/"

# Force-include roots whose ENTIRE contents must reach the wheel, mapped to what produces
# them. Keyed by repo-relative source path. Adding a `[tool.hatch.build] artifacts` entry
# without adding its root here fails test (2).
FORCE_INCLUDE_ROOTS = {
    "src/cli_agent_orchestrator/web_ui": "web dashboard; `cd web && npm run build` (#610, #705)",
    "src/cli_agent_orchestrator/ext_apps/apps_static": (
        "MCP App single-file bundles; `cd cao_mcp_apps && npm run build:all`"
    ),
    "src/cli_agent_orchestrator/ext_apps/static": "topology widget; committed, no build step",
}

# Entries deliberately NOT root-globbed, with the guard that covers them instead. These are
# file patterns, not directories, so "every file under the root" is the wrong shape for them.
OTHERWISE_GUARDED = {
    # Asserted by `scripts/build_tui.py check --wheel` in publish-to-pypi.yml. Release-time
    # only, which is a known gap tracked with #321 — not this file's class to close.
    "src/cli_agent_orchestrator/cao-tui*": "scripts/build_tui.py check --wheel (#321)",
}


def _artifacts() -> list[str]:
    """`[tool.hatch.build] artifacts`, read from pyproject.toml rather than hardcoded."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10; `tomli` is a dev dependency for it.
        import tomli as tomllib  # type: ignore[no-redef]

    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    entries = data["tool"]["hatch"]["build"]["artifacts"]
    assert entries, "[tool.hatch.build] artifacts is empty — every force-include is now a no-op"
    return [str(e) for e in entries]


def _files_under(root: str) -> set[str]:
    """Every file on disk under ``root``, as wheel-relative member names."""
    base = REPO_ROOT / root
    if not base.is_dir():
        return set()
    wheel_root = root[len(_SRC_PREFIX) :] if root.startswith(_SRC_PREFIX) else root
    return {f"{wheel_root}/{p.relative_to(base).as_posix()}" for p in _shippable_files(base)}


def test_force_include_roots_are_globbed_whole() -> None:
    """Each root must be force-included by a RECURSIVE glob, not by a narrower pattern.

    This is #705's defect stated as config: `web_ui/index.html` in place of `web_ui/**` is a
    glob that still matches, still builds, still passes an index.html-only member assertion —
    and ships a wheel whose JS and CSS 404. Nothing but the pattern itself distinguishes the
    two, so the pattern is what gets asserted.
    """
    entries = set(_artifacts())
    for root, produced_by in sorted(FORCE_INCLUDE_ROOTS.items()):
        expected = f"{root}/**"
        if expected in entries:
            continue
        narrower = sorted(e for e in entries if e.startswith(f"{root}/"))
        assert False, (
            f"[tool.hatch.build] artifacts does not force-include {root!r} whole: "
            f"expected the entry {expected!r}, found {narrower or 'no entry for it at all'}. "
            f"That directory holds {produced_by}. A glob narrower than `/**` ships SOME of it "
            f"and hatchling reports nothing — a wheel with an entry point and no bundles "
            f"serves GET / as a blank page whose assets 404 (issues #610, #705). If this "
            f"directory is genuinely no longer shipped, drop it from FORCE_INCLUDE_ROOTS in "
            f"this file in the same commit."
        )


def test_artifacts_entries_are_all_guarded() -> None:
    """No `artifacts` entry may exist without a guard — new globs cannot join silently."""
    known = {f"{root}/**" for root in FORCE_INCLUDE_ROOTS} | set(OTHERWISE_GUARDED)
    unguarded = sorted(set(_artifacts()) - known)
    assert not unguarded, (
        f"{len(unguarded)} [tool.hatch.build] artifacts entries have no wheel-contents "
        f"guard: {unguarded}. Each one force-includes gitignored or generated output whose "
        f"non-match hatchling ignores silently, so an unguarded entry is a wheel that can "
        f"ship incomplete with CI green (issues #321, #610, #705). Add the directory to "
        f"FORCE_INCLUDE_ROOTS in this file, or — if something else already asserts it — to "
        f"OTHERWISE_GUARDED naming that assertion."
    )


def test_force_include_roots_ship_whole_into_the_wheel(tmp_path: Path) -> None:
    """Every file present under each root must be a member of a really-built wheel.

    Skips per-root when the root is absent, so a plain checkout (no `npm run build`, no
    `cargo build`) still passes — but asserts at least one root WAS checked, because
    "everything was absent" and "everything shipped" must not look alike. `ext_apps/static/`
    is committed, so at least that root is always checked.
    """
    expected: dict[str, set[str]] = {
        root: files for root in FORCE_INCLUDE_ROOTS if (files := _files_under(root))
    }
    assert expected, (
        "no force-include root has any file on disk, so this test would pass vacuously: "
        "'everything was absent' must not look like 'everything shipped'. At least "
        "src/cli_agent_orchestrator/ext_apps/static/ is committed and should be present — "
        "has it moved or been deleted?"
    )

    names = _build_wheel_members(tmp_path)

    missing = {root: sorted(files - names) for root, files in expected.items() if files - names}
    assert not missing, (
        "the built wheel drops force-included files that are present on disk: "
        + "; ".join(
            f"{root} missing {len(paths)} of {len(expected[root])}: {paths}"
            for root, paths in sorted(missing.items())
        )
        + ". The `[tool.hatch.build] artifacts` glob for that directory no longer covers its "
        "whole contents (issues #610, #705)."
    )


def test_cao_tui_entry_still_matches_its_documented_shape() -> None:
    """`OTHERWISE_GUARDED` must stay honest: the entry it excuses has to still be there.

    Without this, renaming the TUI glob would silently move it into the unguarded set that
    test (2) checks... and out of this file's attention entirely, since test (2) only sees
    what IS in `artifacts`.
    """
    entries = _artifacts()
    for pattern, guard in sorted(OTHERWISE_GUARDED.items()):
        assert pattern in entries, (
            f"OTHERWISE_GUARDED excuses {pattern!r} from the whole-directory rule because "
            f"{guard} covers it, but that entry is no longer in [tool.hatch.build] artifacts. "
            f"Either the artifact is no longer shipped (drop it here too) or it was renamed "
            f"(update both this file and {guard})."
        )


def test_force_include_roots_are_under_a_packaged_tree() -> None:
    """A root outside `packages` cannot land in the wheel at the member path we assert.

    `_files_under()` derives wheel member names by stripping `src/`, which is only correct
    because `packages` (pyproject.toml:155) is `src/`-rooted. If a root moves out from under
    `src/`, the whole-directory assertion above would compare against the wrong member
    names and FAIL on every file under that root — not a silent pass, but a confusing
    failure that looks like a packaging regression instead of a stale path in this file.
    """
    for root in sorted(FORCE_INCLUDE_ROOTS):
        assert root.startswith(_SRC_PREFIX), (
            f"{root!r} is not under src/, so stripping that prefix to derive its wheel member "
            "names is wrong — update _files_under() alongside pyproject's `packages`."
        )
        assert (REPO_ROOT / root).parent.is_dir(), (
            f"the parent of force-include root {root!r} does not exist in the checkout, so "
            "the path in FORCE_INCLUDE_ROOTS is stale and every assertion keyed on it is "
            "silently checking an empty set."
        )
