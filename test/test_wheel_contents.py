"""Regression test for issue #610: the built wheel must ship the web UI.

cli-agent-orchestrator v2.4.1 published a wheel with no ``web_ui/`` directory, so
``GET /`` 404'd for every operator instead of serving the dashboard. The root cause:
``[tool.hatch.build] artifacts`` in pyproject.toml force-includes the gitignored
``src/cli_agent_orchestrator/web_ui/**`` build output, and hatchling treats a glob that
matches nothing as a silent no-op — no error, no warning — when the ``npm run build``
step that populates it was skipped or failed.

This proves the OTHER HALF of the fix: that the packaging config, when actually fed a
built web UI, really does put it in the wheel. A release-time check that fails loudly if
``npm run build`` produced no ``index.html`` is tracked separately (issues #610/#618,
not yet merged to ``main``); this test instead exercises the real ``uv build`` ->
hatchling pipeline end to end, so a regression in the packaging config itself (a typo'd
glob, a dropped ``packages`` entry) is caught here even if the web build itself
succeeded.

WHY IT ASSERTS A FILE SET AND NOT ``index.html`` (issue #705)
-------------------------------------------------------------
Asserting only ``cli_agent_orchestrator/web_ui/index.html`` cannot tell "the web UI
shipped" apart from "only its entry point shipped". Measured on this branch: narrowing
the artifacts glob from ``web_ui/**`` to ``web_ui/index.html`` produced a wheel whose
only web UI member was ``index.html``, and an index.html-only assertion passed against
it. That wheel is still broken — the built ``index.html`` hard-references its hashed
bundles::

    <script type="module" crossorigin src="/assets/index-<hash>.js"></script>
    <link rel="stylesheet" crossorigin href="/assets/index-<hash>.css">

so it serves ``GET /`` as a 200 whose body is an empty ``<div id="root">`` while the JS
and CSS 404 — issue #610's operator-visible symptom, reached with the check green. A
narrowed or typo'd glob is precisely the failure this test's docstring claims it exists
to catch, so the assertions below are:

1. Every file Vite wrote under ``src/cli_agent_orchestrator/web_ui/`` is a wheel member.
   Vite emits five (``index.html``, two favicons, one hashed CSS bundle, one hashed JS
   bundle); the set is read from the tree rather than hardcoded because the bundle names
   carry a content hash that changes on every web change.
2. Every root-relative local URL that ``index.html`` itself references (bundles AND
   favicons) resolves to a wheel member. This is the operator-visible property directly,
   and unlike (1) it also fails for a wheel faithfully packaging an incomplete *source*
   build — including one that never wrote a referenced favicon, which (1) cannot catch
   because it derives its expected set from files that are already present.

Assertion (2) would be vacuously true for an ``index.html`` that references nothing, so
the referenced-asset set is itself asserted non-empty.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_UI_DIR = REPO_ROOT / "src" / "cli_agent_orchestrator" / "web_ui"
WEB_UI_INDEX = WEB_UI_DIR / "index.html"

# The directory hatchling stores the web UI under INSIDE the wheel:
# `packages = ["src/cli_agent_orchestrator", ...]` strips the `src/` prefix, matching how
# `scripts/build_tui.py` derives the same mapping for the TUI binary.
WHEEL_WEB_UI_PREFIX = "cli_agent_orchestrator/web_ui/"
WHEEL_MEMBER = WHEEL_WEB_UI_PREFIX + "index.html"

# `src="/assets/index-<hash>.js"` / `href="/assets/index-<hash>.css"` / `href="/favicon.svg"`.
# Vite writes these as ROOT-RELATIVE URLs, which the StaticFiles mount at `/` serves out of
# the same `web_ui/` directory — so the leading slash maps to WHEEL_WEB_UI_PREFIX. Matches
# any local root-relative reference (not just `/assets/*`) so dropped favicons are caught
# too (issue #705 follow-up). The closing quote must immediately follow the path, so a
# URL with a querystring or fragment (`?`/`#`) does not match at all, rather than
# matching with the `?`/`#` suffix folded into the file name.
_ASSET_URL_RE = re.compile(r"""(?:src|href)=["'](/[^"':?#]+)["']""")

# hatchling never ships these, even when an `artifacts` glob matches them
# (hatchling/builders/constants.py: EXCLUDED_FILES, EXCLUDED_DIRECTORIES). A `.DS_Store`
# left in a committed directory (e.g. by macOS Finder) must not count as "on disk but
# missing from the wheel" — that is not a packaging defect.
_HATCHLING_EXCLUDED_FILES = frozenset({".DS_Store", ".git"})
_HATCHLING_EXCLUDED_DIRS = frozenset(
    {"__pycache__", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)


def _shippable_files(base: Path) -> list[Path]:
    """Every file under ``base`` that hatchling could actually ship.

    Excludes names/directories hatchling hard-excludes from every wheel regardless of
    what `artifacts` matches, so a stray `.DS_Store` cannot produce a false "the wheel
    is missing a file that's on disk" failure.
    """
    return [
        p
        for p in base.rglob("*")
        if p.is_file()
        and p.name not in _HATCHLING_EXCLUDED_FILES
        and not _HATCHLING_EXCLUDED_DIRS.intersection(p.relative_to(base).parts[:-1])
    ]


def test_shippable_files_excludes_hatchling_excluded_names(tmp_path):
    """A stray `.DS_Store` on disk must not be expected in the wheel.

    hatchling drops these unconditionally (EXCLUDED_FILES/EXCLUDED_DIRECTORIES in
    hatchling/builders/constants.py), even though an `artifacts` glob matches them. A
    `.DS_Store` left in a committed directory by macOS Finder would otherwise make
    `_built_web_ui_files()`/`_files_under()` expect a file the wheel can never contain,
    and blame the `artifacts` glob for a difference that isn't a packaging defect.
    """
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    (tmp_path / ".DS_Store").write_bytes(b"\x00")
    cache_dir = tmp_path / "__pycache__"
    cache_dir.mkdir()
    (cache_dir / "mod.pyc").write_bytes(b"\x00")

    found = {p.name for p in _shippable_files(tmp_path)}

    assert found == {"index.html"}, (
        f"_shippable_files() returned {found!r}; it must exclude files hatchling never "
        "ships (.DS_Store) and files under directories hatchling never ships "
        "(__pycache__), not just report everything rglob finds."
    )


def _built_web_ui_files() -> set[str]:
    """Every file Vite wrote under ``web_ui/``, as wheel-relative member names.

    ``vite.config.ts`` sets ``emptyOutDir: true``, so this directory is exactly one
    build's output and not an accumulation across builds.
    """
    return {
        WHEEL_WEB_UI_PREFIX + str(p.relative_to(WEB_UI_DIR).as_posix())
        for p in _shippable_files(WEB_UI_DIR)
    }


def _assets_referenced_by_index(index_path: Path = WEB_UI_INDEX) -> set[str]:
    """Wheel member names for every root-relative local URL ``index.html`` loads."""
    html = index_path.read_text(encoding="utf-8")
    return {WHEEL_WEB_UI_PREFIX + url.lstrip("/") for url in _ASSET_URL_RE.findall(html)}


def _copy_project_tree(dest: Path) -> Path:
    """Copy the files ``uv build`` needs for a wheel into ``dest`` and return the copy's root.

    Lets a test mutate the tree it builds from (e.g. deleting a file to simulate a broken
    build) without touching ``REPO_ROOT`` itself. This suite runs under `pytest -n auto`
    (DEVELOPMENT.md:142), so sibling tests build wheels from the real checkout concurrently;
    writing into it here would race them.
    """
    project_root = dest / "project"
    project_root.mkdir(parents=True)
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(REPO_ROOT / name, project_root / name)
    shutil.copytree(REPO_ROOT / "scripts", project_root / "scripts")
    shutil.copytree(REPO_ROOT / "src", project_root / "src")
    return project_root


def _build_wheel_members(tmp_path: Path, project_root: Path = REPO_ROOT) -> set[str]:
    """Run the real ``uv build`` -> hatchling pipeline and return the wheel's member names."""
    env = dict(os.environ)
    # This test only cares about web_ui/; disable the TUI's own build-time autobuild so
    # building one wheel here does not also trigger an unrelated `cargo build` (issue #610
    # is unrelated to the Rust binary, and a cargo compile would make this test slow and
    # dependent on a toolchain/network it does not need).
    env["CAO_TUI_AUTOBUILD"] = "0"

    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        f"uv build failed (exit {result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    wheels = sorted(tmp_path.glob("*.whl"))
    assert wheels, f"uv build reported success but produced no .whl in {tmp_path}"

    with zipfile.ZipFile(wheels[0]) as zf:
        return set(zf.namelist())


def web_ui_build_is_complete(web_ui_dir: Path = WEB_UI_DIR) -> bool:
    """Is a USABLE Vite build present in the tree — index.html plus every asset it loads?

    PUBLIC because ci.yml's `Web UI Build` job calls it as the skip-guard for this file:
    the test below SKIPS when the build output is absent, and a skip keeps that job green,
    which is the same false assurance this test exists to remove. The guard imports this
    function rather than re-implementing the paths in shell — a hardcoded copy drifts, and
    a drifted copy keeps passing while the test silently skips.

    Stricter than the ``skipif`` below on purpose. ``skipif`` keys on ``index.html`` alone,
    because a tree with index.html but no bundles is a BROKEN build that must fail the
    test, not skip it (issue #705). This function is the CI precondition, where "npm run
    build exited 0 but wrote an incomplete tree" must fail the job before pytest runs.
    """
    index_path = web_ui_dir / "index.html"
    if not index_path.is_file():
        return False
    referenced = _assets_referenced_by_index(index_path)
    if not referenced:
        return False
    return all((web_ui_dir / name[len(WHEEL_WEB_UI_PREFIX) :]).is_file() for name in referenced)


@pytest.mark.skipif(
    not WEB_UI_INDEX.is_file(),
    reason=(
        f"src/cli_agent_orchestrator/web_ui/index.html does not exist — the web UI has "
        "not been built in this environment. Run `cd web && npm ci && npm run build` "
        "first. This is exactly the precondition a broken/skipped release build violates "
        "(issue #610); skipping rather than failing here keeps this test from firing in "
        "CI's plain `test` job, which never builds the web UI (see the separate "
        "`web-build` job in ci.yml)."
    ),
)
def test_wheel_ships_the_web_ui(tmp_path):
    """Building a wheel from a tree with web_ui/ present must include ALL of it."""
    names = _build_wheel_members(tmp_path)

    web_ui_members = sorted(n for n in names if n.startswith(WHEEL_WEB_UI_PREFIX))

    # The entry point, called out separately so its absence reads as issue #610 verbatim
    # rather than as one line in a set diff.
    assert WHEEL_MEMBER in names, (
        f"the built wheel does not contain {WHEEL_MEMBER!r} — the web dashboard would "
        f"404 at GET / exactly as in issue #610. web_ui/ members found: "
        f"{web_ui_members or 'none'}"
    )

    # (1) The whole built file set, not just the entry point (issue #705). Asserted as
    # built-tree-minus-wheel rather than set equality: a member in the wheel that is not
    # in the tree is not a shipping defect, while a file in the tree that the wheel drops
    # is precisely the narrowed-glob regression.
    built = _built_web_ui_files()
    missing = sorted(built - names)
    assert not missing, (
        f"the built wheel is missing {len(missing)} of {len(built)} built web UI "
        f"files: {missing}. The `[tool.hatch.build] artifacts` glob for web_ui/ no "
        f"longer covers the whole build output — a wheel that ships index.html without "
        f"its bundles serves GET / as a blank page whose JS and CSS 404 (issues "
        f"#610, #705). web_ui/ members found: {web_ui_members}"
    )

    # (2) The operator-visible property: what index.html loads is actually in the wheel.
    referenced = _assets_referenced_by_index()
    assert referenced, (
        "index.html references no local URL, so there is nothing for this assertion to "
        "check. Either the Vite build changed how it emits bundles (update _ASSET_URL_RE) "
        "or the built index.html is not a real build product; a vacuous pass here is the "
        "false assurance issue #705 reports."
    )
    # A stripped index.html referencing only favicons would otherwise satisfy the
    # non-vacuity check above without exercising the bundle it exists to protect.
    assert any(name.endswith(".js") for name in referenced), (
        "index.html references no .js bundle; _ASSET_URL_RE may need updating, or "
        "index.html is not a real build product."
    )
    assert any(name.endswith(".css") for name in referenced), (
        "index.html references no .css bundle; _ASSET_URL_RE may need updating, or "
        "index.html is not a real build product."
    )
    unresolved = sorted(referenced - names)
    assert not unresolved, (
        f"index.html loads {len(unresolved)} asset(s) the wheel does not contain: "
        f'{unresolved}. GET / would return 200 with an empty <div id="root"> while '
        f"those requests 404 — issue #610's symptom with the packaging check green "
        f"(#705). web_ui/ members found: {web_ui_members}"
    )


@pytest.mark.skipif(
    not (WEB_UI_DIR / "favicon.svg").is_file(),
    reason="src/cli_agent_orchestrator/web_ui/favicon.svg does not exist — build the web UI first.",
)
def test_wheel_omitting_a_referenced_favicon_is_caught(tmp_path):
    """Negative control for issue #705's favicon gap.

    A source build that writes ``index.html`` with a ``<link rel="icon">`` to
    ``favicon.svg`` but never emits that file (e.g. a Vite/``public/`` regression) must
    fail assertion (2), not pass with ``favicon.svg`` silently absent from the wheel.
    Before ``_ASSET_URL_RE`` covered non-``/assets/*`` references, this case produced a
    false green.

    Builds from a COPY of the project tree (``_copy_project_tree``), not ``REPO_ROOT``
    itself: deleting the copy's favicon leaves the real checkout untouched, so this test
    cannot race sibling tests that build wheels from ``REPO_ROOT`` concurrently under
    `pytest -n auto`.
    """
    project_root = _copy_project_tree(tmp_path / "src-copy")
    web_ui_copy = project_root / "src" / "cli_agent_orchestrator" / "web_ui"
    (web_ui_copy / "favicon.svg").unlink()

    assert not web_ui_build_is_complete(web_ui_dir=web_ui_copy), (
        "web_ui_build_is_complete() returned True with favicon.svg missing from the "
        "source tree; it must fail closed on every local URL index.html references, "
        "not just /assets/* bundles (issue #705 follow-up)."
    )

    names = _build_wheel_members(tmp_path / "wheel-out", project_root=project_root)
    referenced = _assets_referenced_by_index(web_ui_copy / "index.html")
    favicon_member = WHEEL_WEB_UI_PREFIX + "favicon.svg"
    assert favicon_member in referenced, (
        "index.html no longer references favicon.svg — fixture assumption broken, "
        "update this test to match web/index.html."
    )
    assert favicon_member not in names, (
        "favicon.svg was removed from the copied source tree before packaging, so the "
        "built wheel should not contain it; found it anyway."
    )
    unresolved = sorted(referenced - names)
    assert favicon_member in unresolved, (
        "index.html references favicon.svg, which is absent from both the source "
        "tree and the wheel, but assertion (2)'s unresolved-reference check did not "
        "flag it — this is exactly the false green issue #705 reports."
    )
