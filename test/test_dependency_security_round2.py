"""#1400 review round: behaviour tests for brief items A-J (tests only).

Each rule has a negative case and a control, so no test can pass vacuously. Nothing here
runs real Trivy, a build, ``uv``, ``cargo`` or the network. ``trivy`` is faked at
``subprocess.run``, the same seam ``test/test_dependency_security.py`` uses, and shipped
sets are injected with ``--shipped-set``. Workflow tests read the YAML.

Names follow the "#1400 review round" section of the module docstring.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "dependency_security_round2", ROOT / "scripts" / "dependency_security.py"
)
assert spec is not None and spec.loader is not None
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

TODAY = "2026-11-17"
PICOMATCH_CVE = "CVE-2026-112366"
TRACKED = {
    "uv.lock": "uv",
    "web/package-lock.json": "npm",
    "docusaurus/package-lock.json": "npm",
    "examples/demo/package-lock.json": "npm",
}
WORKFLOWS = ROOT / ".github" / "workflows"
ACTION = ROOT / ".github" / "actions" / "dependency-security" / "action.yml"


# ── fixtures (same shapes as test_dependency_security_shipped.py) ────────────


def pkg(name, version, depends_on=()):
    return {
        "ID": f"{name}@{version}",
        "Name": name,
        "Version": version,
        "DependsOn": [f"{d}@{v}" for d, v in depends_on],
    }


def vuln(name, version, severity="HIGH", advisory="CVE-2099-20001", fixed=""):
    return {
        "VulnerabilityID": advisory,
        "PkgName": name,
        "InstalledVersion": version,
        "Severity": severity,
        "FixedVersion": fixed,
    }


def baseline(target):
    """Each lockfile's inventory, including the C sentinels (react in web, fastapi in uv)."""
    if target == "uv.lock":
        return [pkg("fastapi", "0.115.0"), pkg("click", "8.1.7"), pkg("pytest", "8.3.0")]
    return [pkg("react", "18.3.1"), pkg("vite", "7.0.0")]


def trivy_report(**extra):
    results = []
    for target, kind in TRACKED.items():
        packages, vulns = extra.get(target, (baseline(target), ()))
        results.append(
            {"Target": target, "Type": kind, "Packages": packages, "Vulnerabilities": list(vulns)}
        )
    return {"SchemaVersion": 2, "Results": results}


def shipped_set(overrides=None):
    sets = {
        "uv.lock": {
            "producer": "uv export",
            "packages": [
                {"name": "fastapi", "version": "0.115.0"},
                {"name": "click", "version": "8.1.7"},
            ],
        },
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
        "paths": ["web/package-lock.json"],
    }
    entry.update(fields)
    lines = ["vulnerabilities:"]
    first = True
    for key, value in entry.items():
        if value is None:
            continue
        lines.append(("  - " if first else "    ") + f"{key}: {json.dumps(value)}")
        first = False
    return "\n".join(lines) + "\n"


@pytest.fixture
def repository(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    for target in TRACKED:
        path = root / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "version = 1\n" if target.endswith(".lock") else '{"lockfileVersion":3,"packages":{}}'
        )
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    _commit(root, "base", "2026-11-01T12:00:00+00:00")
    monkeypatch.setattr(audit, "__file__", str(root / "scripts" / "dependency_security.py"))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("CAO_DEPSEC_BASE_REV", raising=False)
    return root


def _commit(root: Path, message: str, when: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_DATE": when,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", message],
        cwd=root,
        check=True,
        env=env,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def run_gate(
    tmp_path,
    monkeypatch,
    report,
    shipped=None,
    ignore="vulnerabilities: []\n",
    extra=(),
    ignore_path=None,
):
    real = subprocess.run

    def run(command, **kwargs):
        if command[0] != "trivy":
            return real(command, **kwargs)
        Path(command[command.index("--output") + 1]).write_text(json.dumps(report))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(audit.subprocess, "run", run)
    out = tmp_path / "out"
    shipped_path = tmp_path / "shipped-set.json"
    shipped_path.write_text(json.dumps(shipped or shipped_set()))
    if ignore_path is None:
        ignore_path = tmp_path / "ignore.yaml"
        ignore_path.write_text(ignore)
    code = audit.main(
        [
            "--output-dir",
            str(out),
            "--today",
            TODAY,
            "--shipped-set",
            str(shipped_path),
            "--ignore-file",
            str(ignore_path),
            *extra,
        ]
    )
    return code, json.loads((out / "report.json").read_text()), out


# ── A. shipped membership by canonical NAME only ─────────────────────────────

PICOMATCH_WEB = baseline("web/package-lock.json") + [
    pkg("picomatch", "4.0.7"),
    pkg("picomatch", "4.0.4"),
    pkg("picomatch", "2.3.2"),
]


def three_picomatch():
    vulns = [vuln("picomatch", v, advisory=PICOMATCH_CVE) for v in ("4.0.7", "4.0.4", "2.3.2")]
    return trivy_report(**{"web/package-lock.json": (PICOMATCH_WEB, vulns)})


def test_a_one_bundled_version_makes_every_version_of_that_name_shipped(
    repository, tmp_path, monkeypatch
):
    """The brief's rule: name only, over-blocking is the safe direction."""
    code, report, _ = run_gate(
        tmp_path, monkeypatch, three_picomatch(), web_shipped(("picomatch", "2.3.2"))
    )
    assert code == 1
    found = [f for f in report["findings"] if f["advisory"] == PICOMATCH_CVE]
    assert {f["version"] for f in found if f["classification"] == "shipped"} == {
        "4.0.7",
        "4.0.4",
        "2.3.2",
    }
    assert report["blocking"] == 3


def test_a_control_no_version_bundled_means_all_toolchain(repository, tmp_path, monkeypatch):
    code, report, _ = run_gate(tmp_path, monkeypatch, three_picomatch(), web_shipped())
    assert code == 0
    assert {f["classification"] for f in report["findings"]} == {"toolchain"}


def test_a_name_match_does_not_cross_lockfiles(repository, tmp_path, monkeypatch):
    """Bundled in web/ says nothing about docusaurus/'s copy of the same name."""
    docs = baseline("docusaurus/package-lock.json") + [pkg("picomatch", "2.3.2")]
    report_in = trivy_report(
        **{
            "docusaurus/package-lock.json": (
                docs,
                [vuln("picomatch", "2.3.2", advisory=PICOMATCH_CVE)],
            )
        }
    )
    code, report, _ = run_gate(
        tmp_path, monkeypatch, report_in, web_shipped(("picomatch", "1.0.0"))
    )
    assert code == 0
    assert report["findings"][0]["classification"] == "toolchain"


@pytest.mark.parametrize(
    "lock, shipped_name, scanned_name, expected",
    [
        ("uv.lock", "Foo_Bar", "foo-bar", "shipped"),
        ("web/package-lock.json", "Lodash", "lodash", "toolchain"),
    ],
    ids=["uv-case-and-underscore", "npm-exact"],
)
def test_a_name_rules_unchanged(
    repository, tmp_path, monkeypatch, lock, shipped_name, scanned_name, expected
):
    base = baseline(lock)
    report_in = trivy_report(
        **{lock: (base + [pkg(scanned_name, "9.9.9")], [vuln(scanned_name, "9.9.9")])}
    )
    current = shipped_set()["sets"][lock]["packages"]
    shipped = shipped_set({lock: current + [{"name": shipped_name, "version": "1.0.0"}]})
    code, report, _ = run_gate(tmp_path, monkeypatch, report_in, shipped)
    [finding] = report["findings"]
    assert finding["classification"] == expected, "version differs on purpose: name-only"
    assert code == int(expected == "shipped")


# ── B. completeness of the expected build outputs (policy.json) ──────────────

MCP_VIEWS = ["agent", "dashboard", "event-stream", "graph"]


def build_policy():
    outputs = [
        {
            "project": "web",
            "config": "web",
            "lockfile": "web/package-lock.json",
            "output_dir": "src/cli_agent_orchestrator/web_ui",
        }
    ]
    outputs += [
        {
            "project": "cao_mcp_apps",
            "config": view,
            "lockfile": "cao_mcp_apps/package-lock.json",
            "output_dir": "src/cli_agent_orchestrator/mcp_apps_dist",
        }
        for view in MCP_VIEWS
    ]
    outputs += [
        {
            "project": "docusaurus",
            "config": c,
            "lockfile": "docusaurus/package-lock.json",
            "output_dir": None,
        }
        for c in ("client", "server")
    ]
    return {
        "schema": 1,
        "expected_outputs": outputs,
        "sentinels": {"web/package-lock.json": ["react"], "uv.lock": ["fastapi"]},
        "html_writers": {"docusaurus/package-lock.json": ["rehype-*", "remark-*", "@mdx-js/*"]},
    }


def _record(
    root: Path,
    project: str,
    config: str,
    *,
    packages=(("react", "18.3.1"),),
    output_dir=None,
    outputs=(),
):
    shipped = root / project / ".shipped"
    shipped.mkdir(parents=True, exist_ok=True)
    body = {
        "schema": 1,
        "project": project,
        "config": config,
        "packages": [{"name": n, "version": v} for n, v in packages],
        "output_dir": output_dir,
        "outputs": list(outputs),
    }
    (shipped / f"bundled-packages.{config}.json").write_text(json.dumps(body))
    if output_dir:
        for name in outputs:
            target = root / output_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("//\n")


def complete_tree(root: Path, *, skip=None, empty=None):
    for entry in build_policy()["expected_outputs"]:
        key = (entry["project"], entry["config"])
        if key == skip:
            continue
        outputs = [f"{entry['config']}.js", f"{entry['config']}.css"] if entry["output_dir"] else []
        _record(
            root,
            entry["project"],
            entry["config"],
            packages=() if key == empty else (("react", "18.3.1"),),
            output_dir=entry["output_dir"],
            outputs=outputs,
        )


def test_b_control_complete_tree_passes(tmp_path):
    complete_tree(tmp_path)
    audit.verify_build_outputs(tmp_path, build_policy())


@pytest.mark.parametrize(
    "skip",
    [
        ("web", "web"),
        ("cao_mcp_apps", "event-stream"),
        ("docusaurus", "server"),
        ("docusaurus", "client"),
    ],
    ids=["web", "mcp-view", "docusaurus-server", "docusaurus-client"],
)
def test_b_missing_expected_output_fails(tmp_path, skip):
    complete_tree(tmp_path, skip=skip)
    with pytest.raises(audit.ShippedSetError):
        audit.verify_build_outputs(tmp_path, build_policy())


def test_b_empty_expected_output_fails(tmp_path):
    complete_tree(tmp_path, empty=("cao_mcp_apps", "graph"))
    with pytest.raises(audit.ShippedSetError):
        audit.verify_build_outputs(tmp_path, build_policy())


@pytest.mark.parametrize("name", ["assets/stray.js", "x.mjs", "x.css", "index.html"])
def test_b_unmapped_file_in_an_output_dir_fails(tmp_path, name):
    complete_tree(tmp_path)
    stray = tmp_path / "src/cli_agent_orchestrator/web_ui" / name
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("//\n")
    with pytest.raises(audit.ShippedSetError):
        audit.verify_build_outputs(tmp_path, build_policy())


def test_b_control_non_code_file_in_an_output_dir_is_ignored(tmp_path):
    """Only .js/.mjs/.css/.html must be mapped; a favicon is not a chunk."""
    complete_tree(tmp_path)
    (tmp_path / "src/cli_agent_orchestrator/web_ui/favicon.png").write_bytes(b"\x89PNG")
    audit.verify_build_outputs(tmp_path, build_policy())


def test_b_old_record_without_outputs_still_loads(tmp_path):
    shipped = tmp_path / "web" / ".shipped"
    shipped.mkdir(parents=True)
    (shipped / "bundled-packages.web.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "project": "web",
                "config": "web",
                "packages": [{"name": "react", "version": "18.3.1"}],
            }
        )
    )
    assert audit.load_bundled_packages(tmp_path / "web") == {("react", "18.3.1")}


def test_b_explicit_missing_policy_fails_closed(repository, tmp_path, monkeypatch):
    code, report, _ = run_gate(
        tmp_path, monkeypatch, trivy_report(), extra=["--policy", str(tmp_path / "absent.json")]
    )
    assert code == 2 and report["status"] == "error"


def test_b_control_default_missing_policy_warns(repository, tmp_path, monkeypatch):
    code, report, _ = run_gate(tmp_path, monkeypatch, trivy_report())
    assert code == 0
    assert any("policy" in w.lower() for w in report["warnings"]), report["warnings"]


def test_b_malformed_policy_fails_closed(repository, tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    policy.write_text('{"schema": 2}')
    code, _, _ = run_gate(tmp_path, monkeypatch, trivy_report(), extra=["--policy", str(policy)])
    assert code == 2


def test_b_mcp_view_count_matches_the_repository():
    """The policy fixture uses 4 MCP views; the repo has exactly 4 view configs."""
    views = sorted(p.name for p in (ROOT / "cao_mcp_apps").glob("vite.config.*.ts"))
    assert len(views) == len(MCP_VIEWS) == 4, views


def test_b_checked_in_policy_lists_every_expected_output():
    policy = audit.load_policy(ROOT / ".github" / "dependency-security" / "policy.json")
    keys = {(e["project"], e["config"]) for e in policy["expected_outputs"]}
    assert ("docusaurus", "client") in keys and ("docusaurus", "server") in keys
    assert any(p == "web" for p, _ in keys)
    assert len([k for k in keys if k[0] == "cao_mcp_apps"]) == 4
    assert policy["sentinels"]["web/package-lock.json"] and policy["sentinels"]["uv.lock"]


# ── C. Trivy parse cross-check ───────────────────────────────────────────────


def _inventory(path, names):
    return [
        {
            "path": path,
            "ecosystem": "npm",
            "packages": len(names),
            "dependencies": [{"name": n, "version": "1.0.0"} for n in names],
        }
    ]


def test_c_missing_sentinel_raises():
    inventory = _inventory("web/package-lock.json", ["vite"]) + [
        {
            "path": "uv.lock",
            "ecosystem": "uv",
            "packages": 1,
            "dependencies": [{"name": "fastapi", "version": "0.115.0"}],
        }
    ]
    with pytest.raises(ValueError):
        audit.verify_sentinels(inventory, build_policy())


def test_c_missing_uv_sentinel_raises():
    inventory = _inventory("web/package-lock.json", ["react"]) + [
        {
            "path": "uv.lock",
            "ecosystem": "uv",
            "packages": 1,
            "dependencies": [{"name": "click", "version": "8.1.7"}],
        }
    ]
    with pytest.raises(ValueError):
        audit.verify_sentinels(inventory, build_policy())


def test_c_control_sentinels_present():
    inventory = _inventory("web/package-lock.json", ["react", "vite"]) + [
        {
            "path": "uv.lock",
            "ecosystem": "uv",
            "packages": 1,
            "dependencies": [{"name": "fastapi", "version": "0.115.0"}],
        }
    ]
    audit.verify_sentinels(inventory, build_policy())


def test_c_empty_package_list_fails_closed(repository, tmp_path, monkeypatch):
    code, _, _ = run_gate(
        tmp_path, monkeypatch, trivy_report(**{"docusaurus/package-lock.json": ([], ())})
    )
    assert code == 2


# ── D. docusaurus server/SSR bundle and HTML-writing packages ────────────────


def test_d_server_record_packages_are_shipped(tmp_path):
    _record(tmp_path, "docusaurus", "client")
    _record(tmp_path, "docusaurus", "server", packages=(("prism-react-renderer", "2.4.0"),))
    assert ("prism-react-renderer", "2.4.0") in audit.shipped_docusaurus_set(tmp_path)


def _policy_file(tmp_path, policy):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    return path


def _docs_finding(name):
    docs = baseline("docusaurus/package-lock.json") + [pkg(name, "7.0.0")]
    return trivy_report(**{"docusaurus/package-lock.json": (docs, [vuln(name, "7.0.0")])})


# Assumption to confirm: with --shipped-set, the html_writers and sentinel checks still
# apply and verify_build_outputs does not run (there is no build in the fixture repository).
def test_d_html_writer_package_counts_as_shipped(repository, tmp_path, monkeypatch):
    policy = _policy_file(tmp_path, build_policy())
    code, report, _ = run_gate(
        tmp_path, monkeypatch, _docs_finding("rehype-raw"), extra=["--policy", str(policy)]
    )
    assert code == 1
    assert report["findings"][0]["classification"] == "shipped"


def test_d_control_non_matching_package_stays_toolchain(repository, tmp_path, monkeypatch):
    policy = _policy_file(tmp_path, build_policy())
    code, report, _ = run_gate(
        tmp_path, monkeypatch, _docs_finding("chokidar"), extra=["--policy", str(policy)]
    )
    assert code == 0
    assert report["findings"][0]["classification"] == "toolchain"


# ── F. tiers ─────────────────────────────────────────────────────────────────


def _malware_report(**fields):
    base = baseline("web/package-lock.json") + [pkg("evil-dev-tool", "1.0.0")]
    overrides = {"severity": "LOW", **fields.pop("v", {})}
    record = vuln("evil-dev-tool", "1.0.0", **overrides)
    record.update(fields)
    return trivy_report(**{"web/package-lock.json": (base, [record])})


@pytest.mark.parametrize(
    "fields",
    [
        {"v": {"advisory": "MAL-2026-0001"}},
        {"v": {"advisory": "GHSA-aaaa-bbbb-cccc"}, "Title": "Malware in evil-dev-tool"},
        {"v": {"advisory": "GHSA-aaaa-bbbb-dddd"}, "Title": "Malicious code in evil-dev-tool"},
    ],
    ids=["osv-mal", "ghsa-malware-in", "ghsa-malicious-code-in"],
)
def test_f_malware_blocks_in_any_scope(repository, tmp_path, monkeypatch, fields):
    code, report, _ = run_gate(tmp_path, monkeypatch, _malware_report(**fields))
    assert code == 1
    [finding] = report["findings"]
    assert finding["malware"] is True and finding["tier"] == "malware"


def test_f_control_normal_advisory_with_a_normal_title_passes(repository, tmp_path, monkeypatch):
    code, report, _ = run_gate(
        tmp_path, monkeypatch, _malware_report(Title="Prototype pollution in evil-dev-tool")
    )
    assert code == 0
    assert report["findings"][0]["malware"] is False


def test_f_malware_cannot_be_ignored(repository, tmp_path, monkeypatch):
    report_in = _malware_report(v={"advisory": "MAL-2026-0001", "severity": "HIGH"})
    code, _, _ = run_gate(
        tmp_path,
        monkeypatch,
        report_in,
        web_shipped(("evil-dev-tool", "1.0.0")),
        ignore_yaml(id="MAL-2026-0001"),
    )
    assert code in (1, 2), "an ignore entry never turns malware into a pass"


def test_f_unfixed_toolchain_stays_report_only(repository, tmp_path, monkeypatch):
    """picomatch has no fix released: it must keep passing under the tiers."""
    code, report, _ = run_gate(tmp_path, monkeypatch, three_picomatch(), web_shipped())
    assert code == 0
    assert report["blocking"] == 0


def _toolchain(fixed, version="2.0.0"):
    finding = {
        "path": "web/package-lock.json",
        "advisory": "CVE-2099-30001",
        "severity": "HIGH",
        "package": "build-tool",
        "version": version,
        "fixed_version": fixed,
        "malware": False,
    }
    inventory = [{"path": "web/package-lock.json", "ecosystem": "npm", "_graph": []}]
    shipped = shipped_set()["sets"]
    return finding, inventory, shipped


@pytest.mark.parametrize(
    "fixed, base, tier",
    [
        ("2.0.1", {("build-tool", "1.0.0")}, "toolchain-fixable-new"),  # PR changed the version
        ("2.0.1", set(), "toolchain-fixable-new"),  # PR added the package
        ("2.0.1", {("build-tool", "2.0.0")}, "toolchain"),  # unchanged since base
        ("", set(), "toolchain"),  # no fix released
    ],
    ids=["changed", "added", "unchanged", "unfixed"],
)
def test_f_toolchain_fixable_new_tier(fixed, base, tier):
    finding, inventory, shipped = _toolchain(fixed)
    [out] = audit.classify_findings(
        [finding], inventory, shipped, [], base_packages={"web/package-lock.json": base}
    )
    assert out["tier"] == tier
    assert audit.is_blocking(out) is (tier == "toolchain-fixable-new")


def test_f_control_no_base_disables_the_new_with_fix_tier():
    finding, inventory, shipped = _toolchain("2.0.1")
    [out] = audit.classify_findings([finding], inventory, shipped, [], base_packages=None)
    assert out["tier"] == "toolchain" and audit.is_blocking(out) is False


def _npm_lock(packages):
    body = {"lockfileVersion": 3, "packages": {"": {"name": "x"}}}
    for name, version in packages:
        body["packages"][f"node_modules/{name}"] = {"version": version}
    return json.dumps(body)


def test_f_base_lockfile_packages_reads_the_base_commit(repository):
    lock = repository / "web" / "package-lock.json"
    lock.write_text(_npm_lock([("build-tool", "1.0.0")]))
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    base = _commit(repository, "base lock", "2026-11-02T12:00:00+00:00")
    lock.write_text(_npm_lock([("build-tool", "2.0.0")]))
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    _commit(repository, "head lock", "2026-11-03T12:00:00+00:00")
    result = audit.base_lockfile_packages(repository, base, [Path("web/package-lock.json")])
    assert ("build-tool", "1.0.0") in result["web/package-lock.json"]
    assert ("build-tool", "2.0.0") not in result["web/package-lock.json"]


def test_f_lockfile_absent_at_base_is_an_empty_set(repository):
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()
    new = repository / "newapp" / "package-lock.json"
    new.parent.mkdir()
    new.write_text(_npm_lock([("a", "1.0.0")]))
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    _commit(repository, "add newapp", "2026-11-03T12:00:00+00:00")
    result = audit.base_lockfile_packages(repository, base, [Path("newapp/package-lock.json")])
    assert result["newapp/package-lock.json"] == set()


def test_f_unknown_base_rev_raises(repository):
    with pytest.raises(audit.BaseUnavailableError):
        audit.base_lockfile_packages(repository, "0" * 40, [Path("web/package-lock.json")])


def test_f_pull_request_without_a_base_fails_closed(repository, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    code, report, _ = run_gate(tmp_path, monkeypatch, trivy_report())
    assert code == 2 and report["status"] == "error"


def test_f_control_push_without_a_base_passes(repository, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    code, _, _ = run_gate(tmp_path, monkeypatch, trivy_report())
    assert code == 0


def test_f_unknown_base_rev_on_the_cli_fails_closed(repository, tmp_path, monkeypatch):
    code, _, _ = run_gate(tmp_path, monkeypatch, trivy_report(), extra=["--base-rev", "0" * 40])
    assert code == 2


# ── G. ignore file ───────────────────────────────────────────────────────────


def _ignore(tmp_path, text):
    path = tmp_path / "ignore.yaml"
    path.write_text(text)
    return path


@pytest.mark.parametrize(
    "fields", [{"paths": None}, {"paths": []}], ids=["no-scope", "empty-paths"]
)
def test_g_unscoped_entry_is_rejected(tmp_path, fields):
    with pytest.raises(audit.IgnoreFileError):
        audit.load_ignore_file(_ignore(tmp_path, ignore_yaml(**fields)), date(2026, 11, 17))


@pytest.mark.parametrize(
    "fields",
    [{}, {"paths": None, "purl": "pkg:npm/picomatch@4.0.7"}],
    ids=["paths", "purl"],
)
def test_g_control_scoped_entry_is_accepted(tmp_path, fields):
    [entry] = audit.load_ignore_file(_ignore(tmp_path, ignore_yaml(**fields)), date(2026, 11, 17))
    assert entry["state"] == "valid"


WEB_HIGH = {
    "web/package-lock.json": (
        baseline("web/package-lock.json") + [pkg("example-shipped", "1.0.0")],
        [vuln("example-shipped", "1.0.0")],
    )
}


@pytest.mark.parametrize(
    "ignore, bundled",
    [
        (ignore_yaml(id="CVE-2099-99999"), True),
        (ignore_yaml(), False),
        (ignore_yaml(paths=["docusaurus/package-lock.json"]), True),
        (ignore_yaml(paths=None, purl="pkg:npm/other-package@1.0.0"), True),
    ],
    ids=["other-advisory", "finding-is-toolchain", "other-lockfile", "other-purl"],
)
def test_g_entry_matching_no_shipped_finding_is_rejected(
    repository, tmp_path, monkeypatch, ignore, bundled
):
    shipped = web_shipped(("example-shipped", "1.0.0")) if bundled else web_shipped()
    code, report, _ = run_gate(tmp_path, monkeypatch, trivy_report(**WEB_HIGH), shipped, ignore)
    assert code == 2
    assert report["status"] == "error"


@pytest.mark.parametrize(
    "ignore",
    [ignore_yaml(), ignore_yaml(paths=None, purl="pkg:npm/example-shipped@1.0.0")],
    ids=["paths", "purl"],
)
def test_g_control_entry_covering_a_shipped_finding_passes(
    repository, tmp_path, monkeypatch, ignore
):
    code, report, _ = run_gate(
        tmp_path,
        monkeypatch,
        trivy_report(**WEB_HIGH),
        web_shipped(("example-shipped", "1.0.0")),
        ignore,
    )
    assert code == 0
    assert report["findings"][0]["ignore"]["state"] == "valid"


def test_g_stale_ignore_entries_pure(tmp_path):
    text = ignore_yaml() + ignore_yaml(id="CVE-2099-99999").replace("vulnerabilities:\n", "")
    covering, stale = audit.load_ignore_file(_ignore(tmp_path, text), date(2026, 11, 17))
    findings = [
        {
            "path": "web/package-lock.json",
            "advisory": "CVE-2099-20001",
            "severity": "HIGH",
            "package": "example-shipped",
            "version": "1.0.0",
            "fixed_version": "",
            "classification": "shipped",
            "malware": False,
        }
    ]
    assert audit.stale_ignore_entries([covering, stale], findings) == [stale]


IGNORE_REL = ".github/dependency-security/trivyignore.yaml"


def _commit_ignore(root: Path, text: str, when: str) -> str:
    path = root / IGNORE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    return _commit(root, "ignore", when)


def test_g_added_comes_from_the_introducing_commit(repository):
    _commit_ignore(repository, ignore_yaml(), "2026-10-01T12:00:00+00:00")
    head = _commit(repository, "later", "2026-11-15T12:00:00+00:00")
    dates = audit.entry_added_dates(repository, repository / IGNORE_REL, head, ["CVE-2099-20001"])
    assert dates == {"CVE-2099-20001": date(2026, 10, 1)}


def test_g_control_entry_not_in_the_base_is_added_today(repository):
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()
    dates = audit.entry_added_dates(repository, repository / IGNORE_REL, base, ["CVE-2099-20001"])
    # needs interface: entry_added_dates takes no clock, so "today" is the real date here.
    assert dates["CVE-2099-20001"] in {date.today(), datetime.now(timezone.utc).date()}


def test_g_git_date_older_than_14_days_fails_even_if_the_yaml_is_fresh(
    repository, tmp_path, monkeypatch
):
    """YAML says added 2026-11-10; git says the entry arrived 2026-10-01: exit 2."""
    base = _commit_ignore(repository, ignore_yaml(), "2026-10-01T12:00:00+00:00")
    code, report, _ = run_gate(
        tmp_path,
        monkeypatch,
        trivy_report(**WEB_HIGH),
        web_shipped(("example-shipped", "1.0.0")),
        ignore_path=repository / IGNORE_REL,
        extra=["--base-rev", base],
    )
    assert code == 2 and report["status"] == "error"


def test_g_control_git_date_inside_the_window_passes(repository, tmp_path, monkeypatch):
    base = _commit_ignore(repository, ignore_yaml(), "2026-11-12T12:00:00+00:00")
    code, report, _ = run_gate(
        tmp_path,
        monkeypatch,
        trivy_report(**WEB_HIGH),
        web_shipped(("example-shipped", "1.0.0")),
        ignore_path=repository / IGNORE_REL,
        extra=["--base-rev", base],
    )
    assert code == 0, report.get("error")


def test_g_shallow_clone_with_a_found_entry_fails_closed(repository, tmp_path):
    _commit_ignore(repository, ignore_yaml(), "2026-10-01T12:00:00+00:00")
    _commit(repository, "later", "2026-11-15T12:00:00+00:00")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--quiet", "--depth", "1", f"file://{repository}", str(shallow)],
        check=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=shallow, check=True, capture_output=True, text=True
    ).stdout.strip()
    with pytest.raises(audit.IgnoreFileError):
        audit.entry_added_dates(shallow, shallow / IGNORE_REL, head, ["CVE-2099-20001"])


# ── H. packages that left the shipped set ────────────────────────────────────


BASELINE_REL = ".github/dependency-security/shipped-baseline.json"


def _left_names(report):
    # The relay said "list of names"; the head emits {"lockfile", "name"} records. Accept both.
    return {e["name"] if isinstance(e, dict) else e for e in report["left_shipped"]}


def _with_zustand(shipped_names):
    web = baseline("web/package-lock.json") + [pkg("zustand", "5.0.0")]
    report_in = trivy_report(**{"web/package-lock.json": (web, ())})
    shipped = web_shipped(*[(n, v) for n, v in shipped_names])
    return report_in, shipped


def test_h_summary_lists_a_package_that_left_the_shipped_set(repository, tmp_path, monkeypatch):
    baseline_file = repository / BASELINE_REL
    report_in, shipped = _with_zustand([("zustand", "5.0.0")])
    code, _, _ = run_gate(
        tmp_path,
        monkeypatch,
        report_in,
        shipped,
        extra=["--baseline", str(baseline_file), "--write-baseline"],
    )
    assert code == 0 and baseline_file.is_file()
    report_in, shipped = _with_zustand([])
    code, report, out = run_gate(
        tmp_path, monkeypatch, report_in, shipped, extra=["--baseline", str(baseline_file)]
    )
    assert "zustand" in _left_names(report)
    summary = (out / "summary.md").read_text()
    assert "Left the shipped set" in summary and "zustand" in summary


def test_h_control_unchanged_shipped_set_lists_nothing(repository, tmp_path, monkeypatch):
    baseline_file = repository / BASELINE_REL
    report_in, shipped = _with_zustand([("zustand", "5.0.0")])
    run_gate(
        tmp_path,
        monkeypatch,
        report_in,
        shipped,
        extra=["--baseline", str(baseline_file), "--write-baseline"],
    )
    _, report, _ = run_gate(
        tmp_path, monkeypatch, report_in, shipped, extra=["--baseline", str(baseline_file)]
    )
    assert report["left_shipped"] == []


def test_h_a_package_no_longer_locked_is_not_listed(repository, tmp_path, monkeypatch):
    """Removed from the lockfile is a lockfile change, not a silent drop from the shipped set."""
    baseline_file = repository / BASELINE_REL
    report_in, shipped = _with_zustand([("zustand", "5.0.0")])
    run_gate(
        tmp_path,
        monkeypatch,
        report_in,
        shipped,
        extra=["--baseline", str(baseline_file), "--write-baseline"],
    )
    _, report, _ = run_gate(
        tmp_path,
        monkeypatch,
        trivy_report(),
        web_shipped(),
        extra=["--baseline", str(baseline_file)],
    )
    assert "zustand" not in _left_names(report)


# ── I. workflow shape ────────────────────────────────────────────────────────


def _load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _events(doc):
    return doc.get("on", doc.get(True)) or {}


def test_i_ci_runs_on_pull_request_never_pull_request_target():
    ci = _load(WORKFLOWS / "ci.yml")
    events = _events(ci)
    assert "pull_request" in events
    assert "pull_request_target" not in events
    for path in WORKFLOWS.glob("*.y*ml"):
        assert "pull_request_target" not in _events(_load(path)), path.name


def test_i_gate_reads_no_secrets():
    texts = [ACTION.read_text(), (WORKFLOWS / "dependency-security.yml").read_text()]
    ci = _load(WORKFLOWS / "ci.yml")
    texts.append(yaml.safe_dump(ci["jobs"]["dependency-security"]))
    for text in texts:
        assert "secrets." not in text


def test_i_gate_job_permissions_are_minimal():
    ci = _load(WORKFLOWS / "ci.yml")
    scheduled = _load(WORKFLOWS / "dependency-security.yml")
    allowed = {"contents": "read", "security-events": "write"}
    for job in (ci["jobs"]["dependency-security"], scheduled["jobs"]["dependency-security"]):
        assert job["name"] == "Dependency Security"
        perms = job.get("permissions")
        assert isinstance(perms, dict) and perms.get("contents") == "read"
        assert set(perms.items()) <= set(allowed.items()), perms


def test_i_issues_write_only_on_the_schedule_tracking_job():
    ci = _load(WORKFLOWS / "ci.yml")
    assert "issues" not in yaml.safe_dump(ci.get("permissions") or {})
    for name, job in ci["jobs"].items():
        assert (job.get("permissions") or {}).get("issues") is None, name
    scheduled = _load(WORKFLOWS / "dependency-security.yml")
    holders = [
        name
        for name, job in scheduled["jobs"].items()
        if (job.get("permissions") or {}).get("issues") == "write"
    ]
    assert holders == ["toolchain-tracking"]
    job = scheduled["jobs"]["toolchain-tracking"]
    assert "schedule" in str(job.get("if", ""))
    script = "\n".join(str(step.get("run", "")) for step in job["steps"])
    assert "npm " not in script and "uv " not in script, "the issue writer runs no build tool"


GATE_FILES = [ACTION, WORKFLOWS / "ci.yml", WORKFLOWS / "dependency-security.yml"]


def _gate_steps():
    """(file, job, step) for the gate action and every Dependency Security job."""
    action = _load(ACTION)
    yield ACTION.name, "(action)", None, action["runs"]["steps"]
    for path in GATE_FILES[1:]:
        for name, job in (_load(path).get("jobs") or {}).items():
            if name.startswith("dependency-security") or "Dependency Security" in str(
                job.get("name", "")
            ):
                yield path.name, name, job, job.get("steps") or []


def test_i_toolchain_sarif_is_never_uploaded_to_code_scanning():
    """SARIF stays artifact-only: no gate job uploads it, and none holds security-events."""
    seen = 0
    for file, job_name, job, steps in _gate_steps():
        seen += 1
        for step in steps:
            assert "upload-sarif" not in str(step.get("uses", "")), f"{file}:{job_name}"
        if job is not None:
            assert "security-events" not in (job.get("permissions") or {}), f"{file}:{job_name}"
    assert seen >= 3, "the action, the CI job and the scheduled job were all inspected"
    for path in GATE_FILES:
        for step in _all_steps(path):
            target = json.dumps(step.get("with") or {})
            if "upload-sarif" in str(step.get("uses", "")):
                assert (
                    "toolchain" not in target and "dependency-security" not in target
                ), f"{path.name}: an upload-sarif step uploads the gate's SARIF"


def _all_steps(path):
    doc = _load(path)
    if "runs" in doc:
        return list(doc["runs"].get("steps") or [])
    return [s for job in (doc.get("jobs") or {}).values() for s in (job.get("steps") or [])]


def test_i_toolchain_sarif_leaves_only_as_an_artifact():
    """The report directory, which holds toolchain.sarif, is published by upload-artifact."""
    steps = _load(ACTION)["runs"]["steps"]
    gate = next(i for i, s in enumerate(steps) if "dependency_security.py" in str(s.get("run", "")))
    run = steps[gate]["run"]
    assert "--output-dir" in run
    output_dir = run.split("--output-dir", 1)[1].split()[0].strip("\"'")
    uploads = [s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
    assert uploads, "the report directory must be published"
    assert any(
        output_dir.replace("$RUNNER_TEMP", "${{ runner.temp }}").rstrip("/")
        == str(s["with"]["path"]).rstrip("/")
        for s in uploads
    ), (output_dir, [s["with"]["path"] for s in uploads])
    assert all(s.get("if") == "${{ !cancelled() }}" for s in uploads)


# ── J. dependency-review scopes ──────────────────────────────────────────────


def test_j_dependency_review_keeps_its_base_scopes():
    """#1400 round 2 ruling: no fail-on-scopes override. The new-with-a-fix tier already gates
    a fixable toolchain HIGH/CRITICAL that a PR adds or changes, without re-blocking an
    unfixed one (picomatch) that a lockfile refresh merely moves."""
    ci = _load(WORKFLOWS / "ci.yml")
    steps = ci["jobs"]["dependency-review"]["steps"]
    [review] = [s for s in steps if "dependency-review-action" in str(s.get("uses", ""))]
    assert "fail-on-scopes" not in review["with"]
    assert review["with"]["fail-on-severity"] == "high", "the existing setting stays"
