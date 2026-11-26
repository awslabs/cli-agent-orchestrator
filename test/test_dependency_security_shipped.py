"""#1398: Dependency Security blocks only on what CAO ships; toolchain findings are reported.

Written against the interface in ``scripts/dependency_security.py`` (module docstring).
Every rule has a control case, so a test cannot pass by doing nothing:

* a HIGH finding blocks when its (package, version) is in the shipped set, and the SAME
  report passes when it is not;
* an ignore entry suppresses a shipped finding only when it is valid, unexpired, for the
  same advisory and (if ``paths`` is given) the same lockfile;
* every producer fails closed on failure, empty or malformed output.

No test runs real Trivy, a real build, ``uv``, ``cargo`` or the network: ``trivy``,
``uv`` and ``cargo`` are faked at ``subprocess.run`` (the same seam
``test/test_dependency_security.py`` uses), and the shipped set is injected with
``--shipped-set`` or by monkeypatching the producers.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "dependency_security_1398", ROOT / "scripts" / "dependency_security.py"
)
assert spec is not None and spec.loader is not None
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


@pytest.fixture(autouse=True)
def _no_ci_event(monkeypatch):
    """CI sets GITHUB_EVENT_NAME=pull_request, which makes the gate require a base revision.

    These tests choose their mode explicitly, so the runner's own event must not leak in.
    """
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("CAO_DEPSEC_BASE_REV", raising=False)


TODAY = "2026-11-17"
PICOMATCH_CVE = "CVE-2026-112366"

# Lockfiles tracked in the fixture repository, and the Trivy type for each.
TRACKED = {
    "uv.lock": "uv",
    "web/package-lock.json": "npm",
    "docusaurus/package-lock.json": "npm",
    "examples/demo/package-lock.json": "npm",
}


# ── fixture builders ─────────────────────────────────────────────────────────


def pkg(name, version, depends_on=()):
    return {
        "ID": f"{name}@{version}",
        "Name": name,
        "Version": version,
        "DependsOn": [f"{dep}@{ver}" for dep, ver in depends_on],
    }


def vuln(name, version, severity="HIGH", advisory="CVE-2099-20001", fixed=""):
    return {
        "VulnerabilityID": advisory,
        "PkgName": name,
        "InstalledVersion": version,
        "Severity": severity,
        "FixedVersion": fixed,
    }


def result(target, packages, vulnerabilities=()):
    return {
        "Target": target,
        "Type": TRACKED[target],
        "Packages": packages,
        "Vulnerabilities": list(vulnerabilities),
    }


def baseline_packages(target):
    """A non-empty inventory per lockfile, so the #884 coverage checks pass."""
    if target == "uv.lock":
        return [pkg("click", "8.1.7"), pkg("pytest", "8.3.0")]
    return [pkg("react", "18.3.1"), pkg("vite", "7.0.0")]


def trivy_report(**extra):
    """``extra`` maps a target to ``(packages, vulnerabilities)`` that replace its baseline."""
    results = []
    for target in TRACKED:
        packages, vulns = extra.get(target, (baseline_packages(target), ()))
        results.append(result(target, packages, vulns))
    return {"SchemaVersion": 2, "Results": results}


def shipped_set(overrides=None):
    """A valid shipped-set.json; examples/** is absent on purpose.

    ``overrides`` maps a lockfile path to its new ``packages`` list, or to ``None`` to
    drop that lockfile's entry.
    """
    sets = {
        "uv.lock": {"producer": "uv export", "packages": [{"name": "click", "version": "8.1.7"}]},
        "web/package-lock.json": {
            "producer": "bundled-packages.json",
            "packages": [{"name": "react", "version": "18.3.1"}],
        },
        "docusaurus/package-lock.json": {
            "producer": "bundled-packages.json",
            "packages": [{"name": "react", "version": "18.3.1"}],
        },
    }
    for target, packages in (overrides or {}).items():
        if packages is None:
            sets.pop(target)
        else:
            sets[target]["packages"] = packages
    return {"schema": 1, "sets": sets}


def web_shipped(*extra):
    return shipped_set(
        {
            "web/package-lock.json": [{"name": "react", "version": "18.3.1"}]
            + [{"name": n, "version": v} for n, v in extra]
        }
    )


def ignore_yaml(**fields):
    entry = {
        "id": "CVE-2099-20001",
        "url": "https://github.com/advisories/GHSA-xxxx-xxxx-xxxx",
        "statement": "Not reachable: only called from a build script.",
        "added": "2026-11-10",
        "expired_at": "2026-11-24",
        # #1400 G: an entry must be scoped (paths or purl); a path-less one is rejected.
        "paths": ["web/package-lock.json"],
    }
    entry.update(fields)
    lines = ["vulnerabilities:"]
    first = True
    for key, value in entry.items():
        if value is None:
            continue
        prefix = "  - " if first else "    "
        first = False
        if isinstance(value, list):
            rendered = json.dumps(value)
        elif key == "statement" and value != '""':
            rendered = json.dumps(value)  # quoted: a reachability statement often has a colon
        else:
            rendered = str(value)
        lines.append(f"{prefix}{key}: {rendered}")
    return "\n".join(lines) + "\n"


# ── harness ──────────────────────────────────────────────────────────────────


@pytest.fixture
def repository(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    for target in TRACKED:
        path = root / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("version = 1\n" if target.endswith(".lock") else "{}")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    monkeypatch.setattr(audit, "__file__", str(root / "scripts" / "dependency_security.py"))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return root


def fake_trivy(monkeypatch, report):
    real = subprocess.run

    def run(command, **kwargs):
        if command[0] != "trivy":
            return real(command, **kwargs)
        output = Path(command[command.index("--output") + 1])
        output.write_text(json.dumps(report))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(audit.subprocess, "run", run)


def run_gate(tmp_path, monkeypatch, report, shipped=None, ignore=None, today=TODAY):
    """Run ``main`` with an injected Trivy report, shipped set and ignore file."""
    fake_trivy(monkeypatch, report)
    out = tmp_path / "out"
    argv = ["--output-dir", str(out), "--today", today]
    shipped_path = tmp_path / "shipped-set.json"
    shipped_path.write_text(
        shipped if isinstance(shipped, str) else json.dumps(shipped or shipped_set())
    )
    argv += ["--shipped-set", str(shipped_path)]
    ignore_path = tmp_path / "ignore.yaml"
    if ignore is not None:
        ignore_path.write_text(ignore)
    argv += ["--ignore-file", str(ignore_path)]
    code = audit.main(argv)
    report_json = json.loads((out / "report.json").read_text())
    return code, report_json, out


def findings_for(report, advisory):
    return [f for f in report["findings"] if f["advisory"] == advisory]


# ── #1398 box 2 / box 3: shipped blocks, devDependency-only passes with a warning ──


WEB_HIGH = {
    "web/package-lock.json": (
        [pkg("react", "18.3.1"), pkg("example-shipped", "1.0.0")],
        [vuln("example-shipped", "1.0.0")],
    )
}


def test_high_finding_bundled_into_web_blocks(repository, tmp_path, monkeypatch):
    code, report, _ = run_gate(
        tmp_path, monkeypatch, trivy_report(**WEB_HIGH), web_shipped(("example-shipped", "1.0.0"))
    )
    assert code == audit.EXIT_BLOCKED == 1
    assert report["status"] == "blocked"
    assert report["blocking"] == 1
    [finding] = findings_for(report, "CVE-2099-20001")
    assert finding["classification"] == "shipped"


def test_dev_only_high_passes_with_a_warning(repository, tmp_path, monkeypatch):
    """The control for the test above: the same report, the package not bundled."""
    code, report, out = run_gate(tmp_path, monkeypatch, trivy_report(**WEB_HIGH), web_shipped())
    assert code == audit.EXIT_PASSED == 0
    assert report["status"] == "passed"
    assert report["blocking"] == 0
    [finding] = findings_for(report, "CVE-2099-20001")
    assert finding["classification"] == "toolchain", "listed, not dropped"
    assert report["toolchain"] >= 1
    assert any("CVE-2099-20001" in w for w in report["warnings"]), report["warnings"]
    assert "CVE-2099-20001" in (out / "summary.md").read_text()


@pytest.mark.parametrize("severity", ["UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"])
@pytest.mark.parametrize("bundled", [True, False], ids=["shipped", "toolchain"])
def test_only_shipped_high_or_critical_blocks(repository, tmp_path, monkeypatch, severity, bundled):
    report_in = trivy_report(
        **{
            "web/package-lock.json": (
                [pkg("react", "18.3.1"), pkg("example-shipped", "1.0.0")],
                [vuln("example-shipped", "1.0.0", severity=severity)],
            )
        }
    )
    extra = [("example-shipped", "1.0.0")] if bundled else []
    code, report, _ = run_gate(tmp_path, monkeypatch, report_in, web_shipped(*extra))
    blocks = bundled and severity in {"HIGH", "CRITICAL"}
    assert code == int(blocks)
    assert report["blocking"] == int(blocks)
    [finding] = report["findings"]
    assert finding["classification"] == ("shipped" if bundled else "toolchain")


def test_shipped_match_is_by_name_at_any_version(repository, tmp_path, monkeypatch):
    """#1400 A: bundled 2.0.0 makes the vulnerable 1.0.0 copy shipped too (over-block)."""
    code, report, _ = run_gate(
        tmp_path, monkeypatch, trivy_report(**WEB_HIGH), web_shipped(("example-shipped", "2.0.0"))
    )
    assert code == 1
    assert report["findings"][0]["classification"] == "shipped"


def test_shipped_match_is_per_lockfile(repository, tmp_path, monkeypatch):
    """Bundled into web/ does not make the docusaurus/ copy of the same package shipped."""
    report_in = trivy_report(
        **{
            "web/package-lock.json": WEB_HIGH["web/package-lock.json"],
            "docusaurus/package-lock.json": (
                [pkg("react", "18.3.1"), pkg("example-shipped", "1.0.0")],
                [vuln("example-shipped", "1.0.0")],
            ),
        }
    )
    code, report, _ = run_gate(
        tmp_path, monkeypatch, report_in, web_shipped(("example-shipped", "1.0.0"))
    )
    assert code == 1
    assert report["blocking"] == 1, "the package in both sets blocks once, from web/"
    by_path = {f["path"]: f["classification"] for f in report["findings"]}
    assert by_path == {
        "web/package-lock.json": "shipped",
        "docusaurus/package-lock.json": "toolchain",
    }


def test_examples_ship_nothing(repository, tmp_path, monkeypatch):
    report_in = trivy_report(
        **{
            "examples/demo/package-lock.json": (
                [pkg("react", "18.3.1")],
                [vuln("react", "18.3.1", severity="CRITICAL")],
            )
        }
    )
    code, report, _ = run_gate(tmp_path, monkeypatch, report_in)
    assert code == 0
    [finding] = report["findings"]
    assert finding["classification"] == "toolchain"
    assert "ships nothing" in finding["reason"]


@pytest.mark.parametrize(
    "lock, shipped_name, scanned_name, expected",
    [
        ("uv.lock", "Foo_Bar", "foo-bar", "shipped"),
        ("web/package-lock.json", "Example-Lib", "example-lib", "toolchain"),
    ],
    ids=["uv-case-and-underscore", "npm-exact"],
)
def test_name_matching_rules(
    repository, tmp_path, monkeypatch, lock, shipped_name, scanned_name, expected
):
    base = baseline_packages(lock)
    report_in = trivy_report(
        **{lock: (base + [pkg(scanned_name, "1.0.0")], [vuln(scanned_name, "1.0.0")])}
    )
    first = base[0]
    shipped = shipped_set(
        {
            lock: [
                {"name": first["Name"], "version": first["Version"]},
                {"name": shipped_name, "version": "1.0.0"},
            ]
        }
    )
    code, report, _ = run_gate(tmp_path, monkeypatch, report_in, shipped)
    [finding] = report["findings"]
    assert finding["classification"] == expected
    assert code == int(expected == "shipped")


# ── #1398 box 1: picomatch CVE-2026-112366 is toolchain, with its paths ──────


PICOMATCH_WEB = [
    pkg("cao-web", "0.0.0", [("vite", "7.0.0"), ("vitest", "4.0.0"), ("patch-package", "8.0.1")]),
    pkg("react", "18.3.1"),
    pkg("vite", "7.0.0", [("picomatch", "4.0.7"), ("tinyglobby", "0.2.15")]),
    pkg("vitest", "4.0.0", [("picomatch", "4.0.7")]),
    pkg("tinyglobby", "0.2.15", [("picomatch", "4.0.4")]),
    pkg("patch-package", "8.0.1", [("micromatch", "4.0.8")]),
    pkg("micromatch", "4.0.8", [("picomatch", "2.3.2")]),
    pkg("picomatch", "4.0.7"),
    pkg("picomatch", "4.0.4"),
    pkg("picomatch", "2.3.2"),
]
PICOMATCH_DOCS = [
    pkg("cao-docs", "0.0.0", [("chokidar", "3.6.0")]),
    pkg("react", "18.3.1"),
    pkg("chokidar", "3.6.0", [("picomatch", "2.3.2")]),
    pkg("picomatch", "2.3.2"),
]


def picomatch_report():
    def vulns(versions):
        return [vuln("picomatch", v, advisory=PICOMATCH_CVE) for v in versions]

    return trivy_report(
        **{
            "web/package-lock.json": (PICOMATCH_WEB, vulns(["4.0.7", "4.0.4", "2.3.2"])),
            "docusaurus/package-lock.json": (PICOMATCH_DOCS, vulns(["2.3.2"])),
        }
    )


def test_picomatch_cve_is_toolchain_with_every_path(repository, tmp_path, monkeypatch):
    code, report, out = run_gate(tmp_path, monkeypatch, picomatch_report())
    assert code == 0
    assert report["status"] == "passed"
    found = findings_for(report, PICOMATCH_CVE)
    assert {(f["path"], f["version"]) for f in found} == {
        ("web/package-lock.json", "4.0.7"),
        ("web/package-lock.json", "4.0.4"),
        ("web/package-lock.json", "2.3.2"),
        ("docusaurus/package-lock.json", "2.3.2"),
    }
    for finding in found:
        assert finding["classification"] == "toolchain"
        assert finding["dependency_paths"], finding
        assert all(chain[-1] == "picomatch" for chain in finding["dependency_paths"])
    summary = (out / "summary.md").read_text()
    assert PICOMATCH_CVE in summary
    assert "web/package-lock.json" in summary and "docusaurus/package-lock.json" in summary


def test_picomatch_blocks_once_a_copy_is_bundled(repository, tmp_path, monkeypatch):
    """The control: one bundled version makes every picomatch in that lockfile shipped (#1400 A).

    The docusaurus copy stays toolchain: membership is per lockfile.
    """
    code, report, _ = run_gate(
        tmp_path, monkeypatch, picomatch_report(), web_shipped(("picomatch", "2.3.2"))
    )
    assert code == 1
    assert report["blocking"] == 3
    shipped = sorted(
        (f["path"], f["version"]) for f in report["findings"] if f["classification"] == "shipped"
    )
    assert shipped == [
        ("web/package-lock.json", "2.3.2"),
        ("web/package-lock.json", "4.0.4"),
        ("web/package-lock.json", "4.0.7"),
    ]
    docs = [f for f in report["findings"] if f["path"] == "docusaurus/package-lock.json"]
    assert docs and {f["classification"] for f in docs} == {"toolchain"}


def test_dependency_paths_follow_depends_on():
    chains = audit.dependency_paths(PICOMATCH_WEB, "picomatch", "4.0.4")
    assert chains
    assert any(chain[-2:] == ["tinyglobby", "picomatch"] for chain in chains), chains
    assert not any("micromatch" in chain for chain in chains), "4.0.4 is not under micromatch"


def test_dependency_paths_respects_the_limit():
    many = [pkg("root", "0", [(f"p{i}", "1") for i in range(6)])]
    many += [pkg(f"p{i}", "1", [("leaf", "1")]) for i in range(6)] + [pkg("leaf", "1")]
    assert len(audit.dependency_paths(many, "leaf", "1", limit=3)) == 3


# ── #1398 box 4: fail closed ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "shipped",
    [
        "{not json",
        json.dumps({"schema": 2, "sets": shipped_set()["sets"]}),
        json.dumps(shipped_set({"web/package-lock.json": []})),
        json.dumps(shipped_set({"uv.lock": None})),
        json.dumps({"schema": 1, "sets": {}}),
        json.dumps(shipped_set({"uv.lock": [{"name": "click"}]})),
    ],
    ids=["malformed", "wrong-schema", "empty-set", "missing-lockfile", "no-sets", "no-version"],
)
def test_invalid_shipped_set_fails_closed(repository, tmp_path, monkeypatch, shipped):
    code, report, _ = run_gate(tmp_path, monkeypatch, trivy_report(), shipped)
    assert code == audit.EXIT_ERROR == 2
    assert report["status"] == "error"


def test_valid_shipped_set_is_the_control(repository, tmp_path, monkeypatch):
    """Without this, every fail-closed case above could pass on a gate that always errors."""
    code, report, out = run_gate(tmp_path, monkeypatch, trivy_report())
    assert code == 0 and report["status"] == "passed"
    for name in ("report.json", "summary.md", "shipped-set.json", "toolchain.sarif"):
        assert (out / name).is_file(), name


def test_producer_failure_fails_closed_end_to_end(repository, tmp_path, monkeypatch):
    """No ``--shipped-set``: the real producer path, with one producer failing."""
    fake_trivy(monkeypatch, trivy_report())

    def broken(root, inputs):
        raise audit.ShippedSetError("uv export returned nothing")

    monkeypatch.setattr(audit, "collect_shipped_sets", broken)
    out = tmp_path / "out"
    ignore = tmp_path / "ignore.yaml"
    ignore.write_text("vulnerabilities: []\n")
    code = audit.main(["--output-dir", str(out), "--today", TODAY, "--ignore-file", str(ignore)])
    assert code == 2
    assert json.loads((out / "report.json").read_text())["status"] == "error"


def _write_bundled(project: Path, config: str, packages, schema=1):
    shipped = project / ".shipped"
    shipped.mkdir(parents=True, exist_ok=True)
    body = {"schema": schema, "project": project.name, "config": config, "packages": packages}
    (shipped / f"bundled-packages.{config}.json").write_text(json.dumps(body))


def test_bundled_packages_union_every_config(tmp_path):
    project = tmp_path / "cao_mcp_apps"
    _write_bundled(project, "vite.a", [{"name": "react", "version": "18.3.1"}])
    _write_bundled(project, "vite.b", [{"name": "zustand", "version": "5.0.0"}])
    assert audit.load_bundled_packages(project) == {("react", "18.3.1"), ("zustand", "5.0.0")}


@pytest.mark.parametrize("case", ["none", "empty", "malformed", "wrong-schema", "no-version"])
def test_bundled_packages_fail_closed(tmp_path, case):
    project = tmp_path / "web"
    project.mkdir()
    if case == "empty":
        _write_bundled(project, "vite", [])
    elif case == "malformed":
        (project / ".shipped").mkdir()
        (project / ".shipped" / "bundled-packages.vite.json").write_text("{not json")
    elif case == "wrong-schema":
        _write_bundled(project, "vite", [{"name": "react", "version": "18.3.1"}], schema=2)
    elif case == "no-version":
        _write_bundled(project, "vite", [{"name": "react"}])
    with pytest.raises(audit.ShippedSetError):
        audit.load_bundled_packages(project)


def test_docusaurus_set_is_its_bundled_packages(tmp_path):
    """Same format as Vite: the webpack plugin writes bundled-packages.client.json."""
    docs = tmp_path / "docusaurus"
    _write_bundled(
        docs,
        "client",
        [{"name": "react", "version": "18.3.1"}, {"name": "clsx", "version": "2.1.1"}],
    )
    assert audit.shipped_docusaurus_set(tmp_path) == {("react", "18.3.1"), ("clsx", "2.1.1")}
    assert audit.shipped_docusaurus_set(tmp_path) == audit.load_bundled_packages(docs)


@pytest.mark.parametrize("case", ["missing", "empty", "malformed"])
def test_docusaurus_set_fails_closed(tmp_path, case):
    docs = tmp_path / "docusaurus"
    docs.mkdir()
    if case == "empty":
        _write_bundled(docs, "client", [])
    elif case == "malformed":
        (docs / ".shipped").mkdir()
        (docs / ".shipped" / "bundled-packages.client.json").write_text("{not json")
    with pytest.raises(audit.ShippedSetError):
        audit.shipped_docusaurus_set(tmp_path)


def _fake_tool(monkeypatch, tool, *, stdout="", error=None, calls=None):
    real = subprocess.run

    def run(command, **kwargs):
        if command[0] != tool:
            return real(command, **kwargs)
        if calls is not None:
            calls.append((command, kwargs))
        if error is not None:
            raise error
        text = kwargs.get("text") or kwargs.get("encoding") or kwargs.get("universal_newlines")
        return subprocess.CompletedProcess(command, 0, stdout if text else stdout.encode(), "")

    monkeypatch.setattr(audit.subprocess, "run", run)


def test_python_set_parses_uv_export_without_dev(tmp_path, monkeypatch):
    calls = []
    _fake_tool(monkeypatch, "uv", stdout="click==8.1.7\nidna==3.10\n", calls=calls)
    assert audit.shipped_python_set(tmp_path) == {("click", "8.1.7"), ("idna", "3.10")}
    command = calls[0][0]
    assert command[:2] == ["uv", "export"] and "--no-dev" in command
    assert "--locked" in command and "--frozen" not in command, "--frozen installs a stale lock"


def test_cargo_set_uses_normal_edges_only(tmp_path, monkeypatch):
    (tmp_path / "tui").mkdir()
    calls = []
    _fake_tool(monkeypatch, "cargo", stdout="ratatui v0.29.0\ncrossterm v0.28.1\n", calls=calls)
    assert {("ratatui", "0.29.0"), ("crossterm", "0.28.1")} <= audit.shipped_cargo_set(tmp_path)
    command = calls[0][0]
    assert command[:2] == ["cargo", "tree"]
    assert command[command.index("-e") + 1] == "normal"


@pytest.mark.parametrize(
    "tool, producer", [("uv", "shipped_python_set"), ("cargo", "shipped_cargo_set")]
)
@pytest.mark.parametrize(
    "mode", ["exit-nonzero", "timeout", "empty", "unparseable"], ids=lambda m: m
)
def test_command_producers_fail_closed(tmp_path, monkeypatch, tool, producer, mode):
    (tmp_path / "tui").mkdir()
    if mode == "exit-nonzero":
        _fake_tool(monkeypatch, tool, error=subprocess.CalledProcessError(1, [tool]))
    elif mode == "timeout":
        _fake_tool(monkeypatch, tool, error=subprocess.TimeoutExpired([tool], 1))
    elif mode == "empty":
        _fake_tool(monkeypatch, tool, stdout="")
    else:
        _fake_tool(monkeypatch, tool, stdout="this is not a package line\n")
    with pytest.raises(audit.ShippedSetError):
        getattr(audit, producer)(tmp_path)


def _all_producers(monkeypatch, **override):
    good = {
        "shipped_python_set": lambda root: {("click", "8.1.7")},
        "shipped_cargo_set": lambda root: {("ratatui", "0.29.0")},
        "shipped_docusaurus_set": lambda root: {("react", "18.3.1")},
        "load_bundled_packages": lambda project: {("react", "18.3.1")},
    }
    good.update(override)
    for name, fn in good.items():
        monkeypatch.setattr(audit, name, fn)


INPUTS = [Path(p) for p in TRACKED] + [
    Path("tui/Cargo.lock"),
    Path("cao_mcp_apps/package-lock.json"),
]


def test_collect_covers_every_shipping_lockfile_and_skips_examples(tmp_path, monkeypatch):
    _all_producers(monkeypatch)
    sets = audit.collect_shipped_sets(tmp_path, INPUTS)
    assert set(sets) == {p.as_posix() for p in INPUTS} - {"examples/demo/package-lock.json"}
    assert all(entry["packages"] for entry in sets.values())


@pytest.mark.parametrize(
    "producer",
    ["shipped_python_set", "shipped_cargo_set", "shipped_docusaurus_set", "load_bundled_packages"],
)
@pytest.mark.parametrize("mode", ["raises", "empty"])
def test_collect_fails_closed_per_producer(tmp_path, monkeypatch, producer, mode):
    def bad(_arg):
        if mode == "raises":
            raise audit.ShippedSetError(f"{producer} failed")
        return set()

    _all_producers(monkeypatch, **{producer: bad})
    with pytest.raises(audit.ShippedSetError):
        audit.collect_shipped_sets(tmp_path, INPUTS)


def test_tracked_lockfile_without_a_producer_fails_closed(tmp_path, monkeypatch):
    _all_producers(monkeypatch)
    with pytest.raises(audit.ShippedSetError):
        audit.collect_shipped_sets(tmp_path, INPUTS + [Path("newproject/package-lock.json")])


def test_the_pr_review_tool_lockfile_ships_nothing(tmp_path, monkeypatch):
    # The pr-review workflow installs Copilot CLI from it on a runner; CAO does not ship it.
    _all_producers(monkeypatch)
    lockfile = Path(".github/pr-review/copilot/package-lock.json")
    sets = audit.collect_shipped_sets(tmp_path, INPUTS + [lockfile])
    assert lockfile.as_posix() not in sets


# ── #1398 box 5: ignore-file validation ──────────────────────────────────────


def _ignore_file(tmp_path, text):
    path = tmp_path / "ignore.yaml"
    path.write_text(text)
    return path


def test_missing_ignore_file_has_no_entries(tmp_path):
    assert audit.load_ignore_file(tmp_path / "absent.yaml", date(2026, 11, 17)) == []


def test_valid_entry_is_valid(tmp_path):
    [entry] = audit.load_ignore_file(_ignore_file(tmp_path, ignore_yaml()), date(2026, 11, 17))
    assert entry["id"] == "CVE-2099-20001"
    assert entry["state"] == "valid"


def test_exactly_fourteen_days_is_allowed(tmp_path):
    text = ignore_yaml(added="2026-11-10", expired_at="2026-11-24")
    [entry] = audit.load_ignore_file(_ignore_file(tmp_path, text), date(2026, 11, 17))
    assert entry["state"] == "valid"


@pytest.mark.parametrize(
    "fields",
    [
        {"expired_at": "2026-11-25"},  # 15 days after added
        {"statement": None},
        {"statement": '""'},
        {"id": None},
        {"url": None},
        {"url": "http://example.invalid/advisory"},
        {"added": None},
        {"expired_at": None},
        {"expired_at": "2026-11-10"},  # not after added
        {"added": "not-a-date"},
    ],
    ids=[
        "over-14-days",
        "missing-statement",
        "empty-statement",
        "missing-id",
        "missing-url",
        "http-url",
        "missing-added",
        "missing-expired_at",
        "expiry-not-after-added",
        "bad-date",
    ],
)
def test_invalid_entry_rejects_the_file(tmp_path, fields):
    with pytest.raises(audit.IgnoreFileError):
        audit.load_ignore_file(_ignore_file(tmp_path, ignore_yaml(**fields)), date(2026, 11, 17))


@pytest.mark.parametrize("today", [date(2026, 11, 24), date(2026, 12, 1)], ids=["on-day", "after"])
def test_entry_on_or_after_expiry_is_expired(tmp_path, today):
    [entry] = audit.load_ignore_file(_ignore_file(tmp_path, ignore_yaml()), today)
    assert entry["state"] == "expired"


def _shipped_high():
    return trivy_report(**WEB_HIGH), web_shipped(("example-shipped", "1.0.0"))


def test_valid_ignore_covers_a_shipped_finding(repository, tmp_path, monkeypatch):
    report_in, shipped = _shipped_high()
    code, report, _ = run_gate(tmp_path, monkeypatch, report_in, shipped, ignore_yaml())
    assert code == 0
    [finding] = report["findings"]
    assert finding["classification"] == "shipped"
    assert finding["ignore"]["state"] == "valid"
    assert report["blocking"] == 0


@pytest.mark.parametrize(
    "ignore, today, expected",
    [
        (ignore_yaml(), "2026-11-24", 1),
        # #1400 G: an entry that matches no current shipped finding is itself an error.
        (ignore_yaml(id="CVE-2099-99999"), TODAY, 2),
        (ignore_yaml(paths=["docusaurus/package-lock.json"]), TODAY, 2),
    ],
    ids=["expired-blocks-again", "other-advisory", "other-lockfile"],
)
def test_ignore_that_does_not_cover_still_blocks(
    repository, tmp_path, monkeypatch, ignore, today, expected
):
    report_in, shipped = _shipped_high()
    code, report, _ = run_gate(tmp_path, monkeypatch, report_in, shipped, ignore, today=today)
    assert code == expected
    if expected == 1:
        assert report["blocking"] == 1
    else:
        assert "match no current shipped finding" in report["error"]


def test_unquoted_colon_in_a_statement_is_invalid(repository, tmp_path, monkeypatch):
    """Why the fixture builder quotes ``statement``: YAML rejects ``a: b: c``, and so must the gate."""
    report_in, shipped = _shipped_high()
    unquoted = ignore_yaml().replace(
        '"Not reachable: only called from a build script."',
        "Not reachable: only called from a build script.",
    )
    assert "statement: Not reachable:" in unquoted, "the fixture really is unquoted"
    code, report, _ = run_gate(tmp_path, monkeypatch, report_in, shipped, unquoted)
    assert code == 2
    assert report["status"] == "error"


def test_invalid_ignore_file_fails_closed(repository, tmp_path, monkeypatch):
    report_in, shipped = _shipped_high()
    code, report, _ = run_gate(
        tmp_path, monkeypatch, report_in, shipped, ignore_yaml(expired_at="2026-12-31")
    )
    assert code == 2
    assert report["status"] == "error"


# ── SARIF ────────────────────────────────────────────────────────────────────


def _finding(advisory, classification, severity="HIGH"):
    return {
        "path": "web/package-lock.json",
        "advisory": advisory,
        "severity": severity,
        "package": "example",
        "version": "1.0.0",
        "fixed_version": "",
        "classification": classification,
        "reason": "test",
        "dependency_paths": [["cao-web", "example"]],
        "ignore": None,
    }


def test_sarif_is_a_2_1_0_log_with_one_result_per_toolchain_finding():
    findings = [
        _finding("CVE-2099-00001", "toolchain"),
        _finding("CVE-2099-00003", "toolchain", severity="LOW"),
        _finding("CVE-2099-00002", "shipped"),
    ]
    log = audit.render_sarif(findings)
    assert log["version"] == "2.1.0"
    assert "sarif" in log.get("$schema", "").lower()
    [run] = log["runs"]
    assert run["tool"]["driver"]["name"]
    results = run["results"]
    assert len(results) == 2, "one result per toolchain finding"
    text = json.dumps(results)
    assert "CVE-2099-00001" in text and "CVE-2099-00003" in text
    assert "CVE-2099-00002" not in json.dumps(log), "a shipped finding never appears in it"
    for item in results:
        assert item["ruleId"] and item["message"]["text"]
        assert item["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]


def test_sarif_without_toolchain_findings_has_no_results():
    log = audit.render_sarif([_finding("CVE-2099-00002", "shipped")])
    assert log["version"] == "2.1.0"
    assert all(not run.get("results") for run in log["runs"])
    assert "CVE-2099-00002" not in json.dumps(log)


def test_sarif_file_matches_the_report(repository, tmp_path, monkeypatch):
    """toolchain.sarif written by main lists exactly the report's toolchain findings."""
    code, report, out = run_gate(tmp_path, monkeypatch, picomatch_report())
    assert code == 0
    sarif = json.loads((out / "toolchain.sarif").read_text())
    toolchain = [f for f in report["findings"] if f["classification"] == "toolchain"]
    assert len(sarif["runs"][0]["results"]) == len(toolchain) == 4


# ── #1400 C: sentinels in the landed policy.json ─────────────────────────────

POLICY = ROOT / ".github" / "dependency-security" / "policy.json"
EXPECTED_SENTINELS = {
    "web/package-lock.json": {"react"},
    "cao_mcp_apps/package-lock.json": {"react", "sigma"},
    "docusaurus/package-lock.json": {"react", "@docusaurus/core"},
    "uv.lock": {"fastapi"},
    "tui/Cargo.lock": {"serde", "ratatui"},
}
ECOSYSTEM = {"uv.lock": "uv", "tui/Cargo.lock": "cargo"}


def _sentinel_inventory(drop=None):
    inventory = []
    for lockfile, names in EXPECTED_SENTINELS.items():
        present = sorted(names - {drop[1]} if drop and drop[0] == lockfile else names)
        dependencies = [{"name": n, "version": "1.0.0"} for n in present + ["filler"]]
        inventory.append(
            {
                "path": lockfile,
                "ecosystem": ECOSYSTEM.get(lockfile, "npm"),
                "packages": len(dependencies),
                "dependencies": dependencies,
            }
        )
    return inventory


def test_landed_policy_names_every_sentinel():
    policy = audit.load_policy(POLICY)
    assert {k: set(v) for k, v in policy["sentinels"].items()} == EXPECTED_SENTINELS


def test_sentinels_control_all_present_passes():
    audit.verify_sentinels(_sentinel_inventory(), audit.load_policy(POLICY))


@pytest.mark.parametrize(
    "drop",
    [(lock, name) for lock, names in EXPECTED_SENTINELS.items() for name in sorted(names)],
    ids=lambda d: f"{d[0]}-{d[1]}",
)
def test_missing_sentinel_raises(drop):
    with pytest.raises(ValueError, match=drop[1]):
        audit.verify_sentinels(_sentinel_inventory(drop), audit.load_policy(POLICY))


def _cli_policy(tmp_path, sentinels):
    """The landed policy with sentinels limited to the fixture repository's lockfiles."""
    policy = json.loads(POLICY.read_text())
    policy["sentinels"] = sentinels
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    return path


@pytest.mark.parametrize("has_react, expected", [(True, 0), (False, 2)], ids=["control", "missing"])
def test_missing_sentinel_fails_closed_end_to_end(
    repository, tmp_path, monkeypatch, has_react, expected
):
    web = [pkg("react", "18.3.1"), pkg("vite", "7.0.0")] if has_react else [pkg("vite", "7.0.0")]
    report_in = trivy_report(**{"web/package-lock.json": (web, ())})
    policy = _cli_policy(tmp_path, {"web/package-lock.json": ["react"]})
    fake_trivy(monkeypatch, report_in)
    out = tmp_path / "out"
    shipped_path = tmp_path / "shipped-set.json"
    shipped_path.write_text(json.dumps(shipped_set()))
    ignore = tmp_path / "ignore.yaml"
    ignore.write_text("vulnerabilities: []\n")
    code = audit.main(
        [
            "--output-dir",
            str(out),
            "--today",
            TODAY,
            "--shipped-set",
            str(shipped_path),
            "--ignore-file",
            str(ignore),
            "--policy",
            str(policy),
        ]
    )
    report = json.loads((out / "report.json").read_text())
    assert code == expected, report.get("error")
    if expected == 2:
        assert "react" in report["error"]
