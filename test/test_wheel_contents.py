"""Regression test for issue #610: the built wheel must ship the web UI.

cli-agent-orchestrator v2.4.1 published a wheel with no ``web_ui/`` directory, so
``GET /`` 404'd for every operator instead of serving the dashboard. The root cause:
``[tool.hatch.build] artifacts`` in pyproject.toml force-includes the gitignored
``src/cli_agent_orchestrator/web_ui/**`` build output, and hatchling treats a glob that
matches nothing as a silent no-op — no error, no warning — when the ``npm run build``
step that populates it was skipped or failed.

This proves the OTHER HALF of the fix: that the packaging config, when actually fed a
built web UI, really does put it in the wheel. The release workflow fails loudly if
``npm run build`` produced no ``index.html`` (the release-time half of this fix); this
test instead exercises the real ``uv build`` -> hatchling pipeline end to end, so a
regression in the packaging config itself (a typo'd glob, a dropped ``packages`` entry)
is caught here even if the web build itself succeeded.

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
2. Every ``/assets/...`` URL that ``index.html`` itself references resolves to a wheel
   member. This is the operator-visible property directly, and unlike (1) it also fails
   for a wheel faithfully packaging an incomplete *source* build.

Assertion (2) would be vacuously true for an ``index.html`` that references nothing, so
the referenced-asset set is itself asserted non-empty.
"""

from __future__ import annotations

import os
import re
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

# `src="/assets/index-<hash>.js"` / `href="/assets/index-<hash>.css"`. Vite writes these
# as ROOT-RELATIVE URLs, which the StaticFiles mount at `/` serves out of the same
# `web_ui/` directory — so the leading slash maps to WHEEL_WEB_UI_PREFIX.
_ASSET_URL_RE = re.compile(r"""(?:src|href)=["'](/assets/[^"']+)["']""")


def _built_web_ui_files() -> set[str]:
    """Every file Vite wrote under ``web_ui/``, as wheel-relative member names.

    ``vite.config.ts`` sets ``emptyOutDir: true``, so this directory is exactly one
    build's output and not an accumulation across builds.
    """
    return {
        WHEEL_WEB_UI_PREFIX + str(p.relative_to(WEB_UI_DIR).as_posix())
        for p in WEB_UI_DIR.rglob("*")
        if p.is_file()
    }


def _assets_referenced_by_index() -> set[str]:
    """Wheel member names for the ``/assets/...`` URLs ``index.html`` loads."""
    html = WEB_UI_INDEX.read_text(encoding="utf-8")
    return {WHEEL_WEB_UI_PREFIX + url.lstrip("/") for url in _ASSET_URL_RE.findall(html)}


def web_ui_build_is_complete() -> bool:
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
    if not WEB_UI_INDEX.is_file():
        return False
    referenced = _assets_referenced_by_index()
    if not referenced:
        return False
    return all((WEB_UI_DIR / name[len(WHEEL_WEB_UI_PREFIX) :]).is_file() for name in referenced)


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
    env = dict(os.environ)
    # This test only cares about web_ui/; disable the TUI's own build-time autobuild so
    # building one wheel here does not also trigger an unrelated `cargo build` (issue #610
    # is unrelated to the Rust binary, and a cargo compile would make this test slow and
    # dependent on a toolchain/network it does not need).
    env["CAO_TUI_AUTOBUILD"] = "0"

    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=REPO_ROOT,
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
        names = set(zf.namelist())

    web_ui_members = sorted(n for n in names if n.startswith(WHEEL_WEB_UI_PREFIX))

    # The entry point, called out separately so its absence reads as issue #610 verbatim
    # rather than as one line in a set diff.
    assert WHEEL_MEMBER in names, (
        f"{wheels[0].name} does not contain {WHEEL_MEMBER!r} — the web dashboard would "
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
        f"{wheels[0].name} is missing {len(missing)} of {len(built)} built web UI "
        f"files: {missing}. The `[tool.hatch.build] artifacts` glob for web_ui/ no "
        f"longer covers the whole build output — a wheel that ships index.html without "
        f"its bundles serves GET / as a blank page whose JS and CSS 404 (issues "
        f"#610, #705). web_ui/ members found: {web_ui_members}"
    )

    # (2) The operator-visible property: what index.html loads is actually in the wheel.
    referenced = _assets_referenced_by_index()
    assert referenced, (
        "index.html references no /assets/* URL, so there is nothing for this assertion "
        "to check. Either the Vite build changed how it emits bundles (update "
        "_ASSET_URL_RE) or the built index.html is not a real build product; a vacuous "
        "pass here is the false assurance issue #705 reports."
    )
    unresolved = sorted(referenced - names)
    assert not unresolved, (
        f"index.html loads {len(unresolved)} asset(s) the wheel does not contain: "
        f'{unresolved}. GET / would return 200 with an empty <div id="root"> while '
        f"those requests 404 — issue #610's symptom with the packaging check green "
        f"(#705). web_ui/ members found: {web_ui_members}"
    )
