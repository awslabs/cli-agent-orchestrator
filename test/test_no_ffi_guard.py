"""Regression tests for the no-FFI dependency-graph gate (``scripts/assert_no_ffi.py``).

Issue #321, requirement FR-5.2 / SR-4, hard constraint T-10.

WHY THESE TESTS EXIST
---------------------
The gate was proven able to reject a real planted ``pyo3`` dependency by hand: the crate was
added to ``tui/Cargo.toml``, the graph re-resolved, the check run (it failed, naming both
``pyo3`` and the transitively-pulled ``pyo3-ffi``), and the manifests restored byte-identically.

That proof is not repeatable in CI — it mutates ``Cargo.lock``. So these tests assert the same
properties against synthetic ``cargo metadata`` graphs, which is what keeps the guard from
silently decaying into a check that cannot fail. This project has repeatedly hit exactly that
failure mode, so the detection path is tested here directly, not just the happy path.

The tests deliberately call the REAL predicate (``assert_no_ffi.main``) rather than
re-expressing its logic locally — a test that reimplements the comparison cannot fail when
production's comparison is wrong.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "assert_no_ffi.py"


def _load_module():
    """Import the script by path — ``scripts/`` is not an importable package."""
    spec = importlib.util.spec_from_file_location("assert_no_ffi", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


assert_no_ffi = _load_module()


def _fake_graph(extra_packages: list[dict] | None = None) -> dict:
    """A minimal but plausible ``cargo metadata`` graph containing the root crate.

    Large enough to clear the anti-vacuity floor, so a test that expects a *verdict* gets one
    rather than the "implausibly small graph" refusal.
    """
    packages = [
        {"name": "cao-tui", "version": "0.1.0", "dependencies": []},
        {"name": "thiserror", "version": "2.0.19", "dependencies": []},
        {"name": "serde", "version": "1.0.229", "dependencies": []},
    ]
    packages.extend(extra_packages or [])
    return {"packages": packages}


def _run_main(monkeypatch, graph: dict, tmp_path: Path) -> int:
    """Run the real ``main()`` against a synthetic graph, stubbing only the cargo call."""
    manifest = tmp_path / "Cargo.toml"
    manifest.write_text('[package]\nname = "cao-tui"\n')
    monkeypatch.setattr(assert_no_ffi, "load_graph", lambda _path: graph)
    return assert_no_ffi.main(["--manifest-path", str(manifest)])


# --------------------------------------------------------------------------------------
# The happy path: a clean graph passes.
# --------------------------------------------------------------------------------------


def test_clean_graph_passes(monkeypatch, tmp_path):
    assert _run_main(monkeypatch, _fake_graph(), tmp_path) == 0


# --------------------------------------------------------------------------------------
# THE DETECTION PATH — the half that makes this a gate rather than a decoration.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("crate", ["pyo3", "pyo3-ffi", "cpython", "python3-sys"])
def test_each_banned_crate_is_rejected(monkeypatch, tmp_path, crate):
    """Every member of the banned set must be caught — not just the one that was probed.

    Parametrised because a gate that catches ``pyo3`` but silently permits ``python3-sys``
    would pass a hand-check on the obvious crate while leaving the others open.
    """
    graph = _fake_graph([{"name": crate, "version": "0.1.0", "dependencies": []}])
    assert _run_main(monkeypatch, graph, tmp_path) == 1


def test_underscore_spelling_is_rejected(monkeypatch, tmp_path):
    """``pyo3_ffi`` must be caught as well as ``pyo3-ffi``.

    An exact string compare against the hyphenated form would miss this.
    """
    graph = _fake_graph([{"name": "pyo3_ffi", "version": "0.22.6", "dependencies": []}])
    assert _run_main(monkeypatch, graph, tmp_path) == 1


def test_transitively_pulled_ffi_crate_is_rejected(monkeypatch, tmp_path):
    """An FFI crate reached only through another dependency must still fail.

    This is the realistic arrival route and the reason the rule requires a graph check rather
    than code review: nobody adds ``pyo3`` to ``[dependencies]`` on purpose.
    """
    graph = _fake_graph(
        [
            {
                "name": "some-innocent-crate",
                "version": "1.0.0",
                "dependencies": [{"name": "pyo3", "kind": None}],
            },
            {"name": "pyo3", "version": "0.22.6", "dependencies": []},
        ]
    )
    assert _run_main(monkeypatch, graph, tmp_path) == 1


def test_failure_report_names_the_crate_and_its_dependent(monkeypatch, tmp_path, capsys):
    """The failure must be actionable: which crate, and what pulled it in."""
    graph = _fake_graph(
        [
            {
                "name": "some-innocent-crate",
                "version": "1.0.0",
                "dependencies": [{"name": "pyo3", "kind": "dev"}],
            },
            {"name": "pyo3", "version": "0.22.6", "dependencies": []},
        ]
    )
    assert _run_main(monkeypatch, graph, tmp_path) == 1
    err = capsys.readouterr().err
    assert "pyo3 v0.22.6" in err
    assert "some-innocent-crate" in err


# --------------------------------------------------------------------------------------
# FAILING CLOSED — a broken data source must not read as a clean graph.
# --------------------------------------------------------------------------------------


def test_empty_graph_is_refused_not_passed(monkeypatch, tmp_path):
    """An empty ``packages`` array must NOT satisfy 'no FFI crate found'.

    This is the vacuity case: the search finds nothing because it looked at nothing.
    """
    assert _run_main(monkeypatch, {"packages": []}, tmp_path) == 2


def test_implausibly_small_graph_is_refused_even_with_the_root_crate(monkeypatch, tmp_path):
    """A graph holding ONLY the root crate must be refused by the size floor.

    Separate from the empty-graph case on purpose. An empty graph is caught twice over — by
    the floor AND by the missing-root check — so it cannot tell the two guards apart: setting
    ``MIN_PLAUSIBLE_PACKAGES = 0`` leaves the empty-graph test green, which was measured.
    This graph contains ``cao-tui``, so the root check is satisfied and only the floor can
    reject it. That makes the floor independently load-bearing rather than incidentally
    shadowed. (#321)
    """
    graph = {"packages": [{"name": "cao-tui", "version": "0.1.0", "dependencies": []}]}
    assert _run_main(monkeypatch, graph, tmp_path) == 2


def test_graph_without_the_root_crate_is_refused(monkeypatch, tmp_path):
    """A graph that does not contain ``cao-tui`` is the wrong graph — refuse to judge it."""
    graph = {
        "packages": [
            {"name": "some-other-project", "version": "1.0.0", "dependencies": []},
            {"name": "serde", "version": "1.0.229", "dependencies": []},
            {"name": "thiserror", "version": "2.0.19", "dependencies": []},
        ]
    }
    assert _run_main(monkeypatch, graph, tmp_path) == 2


def test_cargo_metadata_failure_is_a_failed_check(monkeypatch, tmp_path):
    """If ``cargo metadata`` errors, the check must fail — never green on missing data."""
    manifest = tmp_path / "Cargo.toml"
    manifest.write_text('[package]\nname = "cao-tui"\n')

    def _boom(_path):
        raise RuntimeError("cargo metadata exited 101")

    monkeypatch.setattr(assert_no_ffi, "load_graph", _boom)
    assert assert_no_ffi.main(["--manifest-path", str(manifest)]) == 2


def test_missing_manifest_is_a_failed_check(tmp_path):
    assert assert_no_ffi.main(["--manifest-path", str(tmp_path / "absent.toml")]) == 2


def test_unparseable_metadata_json_fails(monkeypatch, tmp_path):
    """Unparseable JSON from cargo must fail closed, exercising the real ``load_graph``."""
    manifest = tmp_path / "Cargo.toml"
    manifest.write_text('[package]\nname = "cao-tui"\n')

    class _Proc:
        returncode = 0
        stdout = "not json at all"
        stderr = ""

    monkeypatch.setattr(assert_no_ffi.shutil, "which", lambda _n: "/usr/bin/cargo")
    monkeypatch.setattr(assert_no_ffi.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(RuntimeError, match="unparseable JSON"):
        assert_no_ffi.load_graph(manifest)


def test_nonzero_cargo_exit_raises(monkeypatch, tmp_path):
    """A non-zero ``cargo metadata`` exit must raise, not return a partial graph."""
    manifest = tmp_path / "Cargo.toml"
    manifest.write_text('[package]\nname = "cao-tui"\n')

    class _Proc:
        returncode = 101
        stdout = ""
        stderr = "error: failed to select a version"

    monkeypatch.setattr(assert_no_ffi.shutil, "which", lambda _n: "/usr/bin/cargo")
    monkeypatch.setattr(assert_no_ffi.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(RuntimeError, match="exited 101"):
        assert_no_ffi.load_graph(manifest)


def test_load_graph_passes_locked(monkeypatch, tmp_path):
    """``--locked`` must be passed: the gate has to inspect the COMMITTED lockfile.

    Without it cargo may re-resolve, and the graph checked would not be the graph built.
    """
    manifest = tmp_path / "Cargo.toml"
    manifest.write_text('[package]\nname = "cao-tui"\n')
    seen: dict = {}

    class _Proc:
        returncode = 0
        stdout = json.dumps({"packages": []})
        stderr = ""

    def _capture(argv, **kwargs):
        seen["argv"] = argv
        return _Proc()

    monkeypatch.setattr(assert_no_ffi.shutil, "which", lambda _n: "/usr/bin/cargo")
    monkeypatch.setattr(assert_no_ffi.subprocess, "run", _capture)
    assert_no_ffi.load_graph(manifest)

    assert "--locked" in seen["argv"]
    # An argv vector, never a shell string (project.md Forbidden).
    assert isinstance(seen["argv"], list)


def test_no_cargo_on_path_is_a_failed_check(monkeypatch, tmp_path):
    manifest = tmp_path / "Cargo.toml"
    manifest.write_text('[package]\nname = "cao-tui"\n')
    monkeypatch.setattr(assert_no_ffi.shutil, "which", lambda _n: None)
    with pytest.raises(RuntimeError, match="cargo not found"):
        assert_no_ffi.load_graph(manifest)


# --------------------------------------------------------------------------------------
# The banned set must stay aligned with the affirmed rule.
# --------------------------------------------------------------------------------------


def test_banned_set_is_exactly_the_four_named_crates():
    """FR-5.2 / SR-4 name four crates. Hard-coded here rather than derived from the module,
    so a silent narrowing of the production set turns this test red."""
    assert assert_no_ffi.FFI_CRATES == frozenset({"pyo3", "pyo3-ffi", "cpython", "python3-sys"})


# --------------------------------------------------------------------------------------
# End-to-end against the REAL crate graph.
# --------------------------------------------------------------------------------------


# `shutil.which`, not `subprocess.run(["which", ...])`. A skipif condition is evaluated at
# COLLECTION time, so an absent `which` binary raises FileNotFoundError before the skip can ever
# apply — the decorator meant to make this test optional would instead break collection for the
# whole module. `which` is not a Windows builtin, which is precisely where this test is most
# likely to be skipped. `shutil.which` is the stdlib equivalent, needs no subprocess, and honours
# PATHEXT on Windows. (#321; reported by review on PR #547)
@pytest.mark.skipif(
    shutil.which("cargo") is None,
    reason="cargo not installed; the Rust CI job covers this path",
)
def test_real_graph_is_clean():
    """The committed graph must actually be clean — the assertion CI relies on."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--manifest-path", str(REPO_ROOT / "tui" / "Cargo.toml")],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "no Python-FFI crate" in proc.stdout
