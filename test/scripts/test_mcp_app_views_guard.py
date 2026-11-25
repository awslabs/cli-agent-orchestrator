"""Release-workflow shape and guard tests for the MCP App views (#1487).

The wheels shipped without the ``ui://cao/*`` views because ``publish-to-pypi.yml`` built the
Web UI but never ran ``npm run build:all`` in ``cao_mcp_apps``, and hatchling silently drops an
``artifacts`` glob that matches nothing.
"""

from __future__ import annotations

import importlib.util
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish-to-pypi.yml"
SCRIPT = REPO_ROOT / "scripts" / "assert_mcp_app_views.py"


def _guard():
    spec = importlib.util.spec_from_file_location("assert_mcp_app_views", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _jobs():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _index(steps, pred):
    for i, s in enumerate(steps):
        if pred(s):
            return i
    return None


def _builds_views(step):
    return (
        "build:all" in (step.get("run") or "") and step.get("working-directory") == "cao_mcp_apps"
    )


@pytest.mark.parametrize(
    "job,builder", [("build-wheels", "pypa/cibuildwheel"), ("build-sdist", None)]
)
def test_views_are_built_before_the_artifact_and_guarded_after(job, builder):
    steps = _jobs()[job]["steps"]
    built = _index(steps, _builds_views)
    assert built is not None, f"{job} never runs `npm run build:all` in cao_mcp_apps"
    if builder:
        artifact = _index(steps, lambda s: (s.get("uses") or "").startswith(builder))
    else:
        artifact = _index(steps, lambda s: "uv build" in (s.get("run") or ""))
    guard = _index(steps, lambda s: "assert_mcp_app_views.py" in (s.get("run") or ""))
    upload = _index(steps, lambda s: (s.get("uses") or "").startswith("actions/upload-artifact"))
    assert built < artifact < guard < upload


def test_smoke_test_checks_the_installed_views():
    steps = _jobs()["smoke-test"]["steps"]
    hits = [s for s in steps if "apps_static" in (s.get("run") or "")]
    assert hits, "smoke-test does not check the installed MCP App views"
    for name in _guard().VIEW_FILES:
        assert name in hits[0]["run"]


def test_guard_names_match_the_server_resource_map():
    from cli_agent_orchestrator.ext_apps.apps import _RESOURCE_FILES

    assert set(_guard().VIEW_FILES) == set(_RESOURCE_FILES.values())


def _wheel(path, names):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("cli_agent_orchestrator/__init__.py", "")
        for n in names:
            zf.writestr(f"cli_agent_orchestrator/ext_apps/apps_static/{n}", "<html></html>")


def test_guard_passes_with_all_views(tmp_path):
    g = _guard()
    _wheel(tmp_path / "a-1-py3-none-any.whl", g.VIEW_FILES)
    assert g.main(["--dist", str(tmp_path)]) == 0


def test_guard_fails_when_a_view_is_missing(tmp_path, capsys):
    g = _guard()
    _wheel(tmp_path / "a-1-py3-none-any.whl", g.VIEW_FILES[:-1])
    assert g.main(["--dist", str(tmp_path)]) == 1
    assert "graph.html" in capsys.readouterr().out


def test_guard_fails_on_empty_dir_and_empty_view(tmp_path):
    g = _guard()
    assert g.main(["--dist", str(tmp_path)]) == 1
    with zipfile.ZipFile(tmp_path / "a-1-py3-none-any.whl", "w") as zf:
        for n in g.VIEW_FILES:
            zf.writestr(f"cli_agent_orchestrator/ext_apps/apps_static/{n}", "")
    assert g.main(["--dist", str(tmp_path)]) == 1


def test_guard_reads_an_sdist(tmp_path):
    g = _guard()
    src = tmp_path / "src"
    for n in g.VIEW_FILES:
        f = src / "pkg-1" / "src" / "cli_agent_orchestrator" / "ext_apps" / "apps_static" / n
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("<html></html>")
    out = tmp_path / "dist"
    out.mkdir()
    with tarfile.open(out / "pkg-1.tar.gz", "w:gz") as tf:
        tf.add(src / "pkg-1", arcname="pkg-1")
    assert g.main(["--dist", str(out)]) == 0


# --- sdist size and node_modules (#1509 review, P1-1) -------------------------------
# `build-sdist` runs `npm ci` in web/ and cao_mcp_apps/, and the published 2.5.5 sdist
# already carried web/node_modules (74 MB). With both trees the sdist passed PyPI's
# 100 MB per-file limit, so the TestPyPI upload would fail.


def _sdist(tmp_path, extra=()):
    g = _guard()
    src = tmp_path / "src"
    root = src / "pkg-1"
    for n in g.VIEW_FILES:
        f = root / "src" / "cli_agent_orchestrator" / "ext_apps" / "apps_static" / n
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("<html></html>")
    for rel in extra:
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
    out = tmp_path / "dist"
    out.mkdir()
    with tarfile.open(out / "pkg-1.tar.gz", "w:gz") as tf:
        tf.add(root, arcname="pkg-1")
    return g, out


def test_guard_fails_when_the_sdist_carries_node_modules(tmp_path, capsys):
    g, out = _sdist(tmp_path, extra=["cao_mcp_apps/node_modules/left-pad/index.js"])
    assert g.main(["--dist", str(out)]) == 1
    assert "node_modules" in capsys.readouterr().out


def test_guard_fails_when_an_artifact_is_over_the_pypi_limit(tmp_path, capsys, monkeypatch):
    g, out = _sdist(tmp_path)
    monkeypatch.setattr(g, "MAX_ARTIFACT_BYTES", 10)
    assert g.main(["--dist", str(out)]) == 1
    assert "over" in capsys.readouterr().out


def test_guard_limit_is_pypis_default():
    assert _guard().MAX_ARTIFACT_BYTES == 100_000_000


def test_sdist_target_excludes_node_modules():
    import tomllib

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    sdist = pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert "node_modules/" in sdist["exclude"]
