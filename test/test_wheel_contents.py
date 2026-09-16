"""Regression test for issue #610: the built wheel must ship the web UI.

cli-agent-orchestrator v2.4.1 published a wheel with no ``web_ui/`` directory, so
``GET /`` 404'd for every operator instead of serving the dashboard. The root cause:
``[tool.hatch.build] artifacts`` in pyproject.toml force-includes the gitignored
``src/cli_agent_orchestrator/web_ui/**`` build output, and hatchling treats a glob that
matches nothing as a silent no-op — no error, no warning — when the ``npm run build``
step that populates it was skipped or failed.

This proves the OTHER HALF of the fix: that the packaging config, when actually fed a
built web UI, really does put it in the wheel. ``.github/workflows/publish-to-pypi.yml``
now fails loudly if ``npm run build`` produced no ``index.html`` (the release-time half
of this fix); this test instead exercises the real ``uv build`` -> hatchling pipeline
end to end, so a regression in the packaging config itself (a typo'd glob, a dropped
`packages` entry) is caught here even if the web build itself succeeded.
"""

from __future__ import annotations

import os
import subprocess
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_UI_INDEX = REPO_ROOT / "src" / "cli_agent_orchestrator" / "web_ui" / "index.html"

# The path hatchling stores the file at INSIDE the wheel: `packages = ["src/cli_agent_orchestrator", ...]`
# strips the `src/` prefix, matching how `scripts/build_tui.py` derives the same mapping for the TUI binary.
WHEEL_MEMBER = "cli_agent_orchestrator/web_ui/index.html"


@pytest.mark.skipif(
    not WEB_UI_INDEX.is_file(),
    reason=(
        f"{WEB_UI_INDEX.relative_to(REPO_ROOT)} does not exist — the web UI has not been "
        "built in this environment. Run `cd web && npm ci && npm run build` first. This is "
        "exactly the precondition a broken/skipped release build violates (issue #610); "
        "skipping rather than failing here keeps this test from firing in CI's plain `test` "
        "job, which never builds the web UI (see the separate `web-build` job in ci.yml)."
    ),
)
def test_wheel_ships_the_web_ui(tmp_path):
    """Building a wheel from a tree with web_ui/ present must include it in the archive."""
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
        names = zf.namelist()

    web_ui_members = [n for n in names if n.startswith("cli_agent_orchestrator/web_ui/")]
    assert WHEEL_MEMBER in names, (
        f"{wheels[0].name} does not contain {WHEEL_MEMBER!r} — the web dashboard would "
        f"404 at GET / exactly as in issue #610. web_ui/ members found: {web_ui_members or 'none'}"
    )
