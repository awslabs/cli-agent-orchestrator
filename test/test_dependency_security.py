"""Full-graph vulnerability policy, coverage and error-boundary regressions."""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "dependency_security", ROOT / "scripts" / "dependency_security.py"
)
assert spec is not None and spec.loader is not None
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def result(path="package-lock.json", ecosystem="npm", severity=None, fixed=""):
    record = {
        "Target": path,
        "Type": ecosystem,
        "Packages": [{"Name": "example-library", "Version": "1.0.0"}],
    }
    if severity:
        record["Vulnerabilities"] = [
            {
                "Severity": severity,
                "VulnerabilityID": "CVE-2099-10001",
                "PkgName": "example-library",
                "InstalledVersion": "1.0.0",
                "FixedVersion": fixed,
                "Description": "Unneeded scanner text must not be published",
            }
        ]
    return record


@pytest.fixture
def repository(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    (root / "package.json").write_text('{"name":"test-project"}')
    (root / "package-lock.json").write_text('{"lockfileVersion":3,"packages":{}}')
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    monkeypatch.setattr(audit, "__file__", str(root / "scripts" / "dependency_security.py"))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return root


def scanner(monkeypatch, report=None, error=None):
    run = subprocess.run
    calls = []

    def fake_run(command, **kwargs):
        if command[0] != "trivy":
            return run(command, **kwargs)
        calls.append(command)
        assert kwargs["check"] is True
        assert kwargs["timeout"] == 330
        assert command[command.index("--scanners") + 1] == "vuln"
        assert command[command.index("--ignorefile") + 1] == os.devnull
        assert "--include-dev-deps" in command
        assert "--ignore-unfixed=false" in command
        assert "--list-all-pkgs" in command
        assert "--skip-db-update=false" in command
        assert set(command[command.index("--severity") + 1].split(",")) == audit.SEVERITIES
        assert command[command.index("--ignore-status") + 1] == ""
        assert Path(command[command.index("--config") + 1]).read_text() == "{}\n"
        assert not any(
            key.startswith("TRIVY_") and key != "TRIVY_CACHE_DIR" for key in kwargs["env"]
        )
        if error:
            raise error
        output = Path(command[command.index("--output") + 1])
        output.write_text(report if isinstance(report, str) else json.dumps(report))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(audit.subprocess, "run", fake_run)
    return calls


@pytest.mark.parametrize("severity", [None, "UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"])
@pytest.mark.parametrize("fixed", ["", "1.1.0"])
def test_all_high_and_critical_findings_block_even_without_a_fix(
    repository, tmp_path, monkeypatch, severity, fixed
):
    scanner(monkeypatch, {"SchemaVersion": 2, "Results": [result(severity=severity, fixed=fixed)]})
    output = tmp_path / "reports"
    summary = tmp_path / "step-summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    code = audit.main(["--output-dir", str(output)])
    report = json.loads((output / "report.json").read_text())
    blocked = severity in {"HIGH", "CRITICAL"}
    assert code == int(blocked)
    assert report["status"] == ("blocked" if blocked else "passed")
    assert report["blocking"] == int(blocked)
    assert "Unneeded scanner text" not in (output / "report.json").read_text()
    assert summary.read_text() == (output / "summary.md").read_text()


def test_every_tracked_graph_is_scanned_without_a_diff_or_path_filter(
    repository, tmp_path, monkeypatch
):
    extra = repository / "another-project" / "uv.lock"
    extra.parent.mkdir()
    extra.write_text("version = 1\n")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    ignored = repository / "node_modules" / "untracked" / "package-lock.json"
    ignored.parent.mkdir(parents=True)
    ignored.write_text("{}")
    calls = scanner(
        monkeypatch,
        {
            "SchemaVersion": 2,
            "Results": [result(), result("another-project/uv.lock", "uv", "CRITICAL")],
        },
    )
    report = audit.scan(repository)
    assert report["blocking"] == 1
    assert set(report["input_sha256"]) == {
        "package-lock.json",
        "another-project/uv.lock",
    }
    assert not Path(calls[0][-1]).exists()
    assert extra.read_text() == "version = 1\n"


@pytest.mark.parametrize(
    "report",
    [
        None,
        {},
        {"SchemaVersion": 1, "Results": []},
        {"SchemaVersion": 2},
        {"SchemaVersion": 2, "Results": None},
        {"SchemaVersion": 2, "Results": []},
        {"SchemaVersion": 2, "Results": [None]},
        {"SchemaVersion": 2, "Results": [result("wrong/package-lock.json")]},
        {"SchemaVersion": 2, "Results": [result(ecosystem="uv")]},
        {"SchemaVersion": 2, "Results": [result(), result()]},
    ],
)
def test_missing_incomplete_or_wrong_graph_is_not_a_clean_scan(report):
    with pytest.raises(ValueError):
        audit.normalize_report(report, {"package-lock.json": "npm"})


@pytest.mark.parametrize(
    "field,value",
    [
        ("Packages", []),
        ("Packages", None),
        ("Packages", [{"Name": "missing-version"}]),
        ("Vulnerabilities", {}),
        ("Vulnerabilities", [None]),
        ("Vulnerabilities", [{"Severity": "HIGH"}]),
    ],
)
def test_malformed_inventory_and_findings_fail_closed(field, value):
    record = result()
    record[field] = value
    with pytest.raises(ValueError):
        audit.normalize_report(
            {"SchemaVersion": 2, "Results": [record]}, {"package-lock.json": "npm"}
        )


@pytest.mark.parametrize(
    "field,value", [("Severity", "invalid"), ("FixedVersion", []), ("PkgName", "")]
)
def test_invalid_advisory_fields_are_errors(field, value):
    record = result(severity="HIGH")
    record["Vulnerabilities"][0][field] = value
    with pytest.raises(ValueError):
        audit.normalize_report(
            {"SchemaVersion": 2, "Results": [record]}, {"package-lock.json": "npm"}
        )


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("trivy is missing"),
        subprocess.CalledProcessError(2, ["trivy", "fs"]),
        subprocess.TimeoutExpired(["trivy", "fs"], 330),
    ],
)
def test_scanner_errors_replace_old_success_and_clean_up(repository, tmp_path, monkeypatch, error):
    calls = scanner(monkeypatch, error=error)
    output = tmp_path / "reports"
    output.mkdir()
    (output / "report.json").write_text('{"status":"passed"}')
    assert audit.main(["--output-dir", str(output)]) == 2
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "error"
    assert report["input_sha256"] == {
        path.as_posix(): hashlib.sha256((repository / path).read_bytes()).hexdigest()
        for path in audit.tracked_inputs(repository)
    }
    assert report["inventory"] is None
    assert report["findings"] is None
    assert report["blocking"] is None
    assert "No clean security verdict" in (output / "summary.md").read_text()
    assert "unavailable" in (output / "summary.md").read_text()
    assert not Path(calls[0][-1]).exists()


@pytest.mark.parametrize("data", ["not JSON", {"SchemaVersion": 2, "Results": []}])
def test_invalid_scanner_reports_preserve_provenance(repository, tmp_path, monkeypatch, data):
    scanner(monkeypatch, report=data)
    output = tmp_path / "reports"
    assert audit.main(["--output-dir", str(output)]) == 2
    report = json.loads((output / "report.json").read_text())
    assert report["input_sha256"] == {
        path.as_posix(): hashlib.sha256((repository / path).read_bytes()).hexdigest()
        for path in audit.tracked_inputs(repository)
    }
    assert report["inventory"] is None
    assert report["findings"] is None
    assert report["blocking"] is None


def test_input_copy_failure_retains_only_copied_hashes(repository, tmp_path, monkeypatch):
    extra = repository / "z-project" / "uv.lock"
    extra.parent.mkdir()
    extra.write_text("version = 1\n")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    copy = audit.shutil.copyfile

    def fail_lock_copy(source, destination):
        if source.name == "uv.lock":
            raise OSError("Cannot copy lockfile")
        return copy(source, destination)

    monkeypatch.setattr(audit.shutil, "copyfile", fail_lock_copy)
    calls = scanner(monkeypatch)
    output = tmp_path / "reports"
    assert audit.main(["--output-dir", str(output)]) == 2
    report = json.loads((output / "report.json").read_text())
    assert report["input_sha256"] == {
        "package-lock.json": hashlib.sha256(
            (repository / "package-lock.json").read_bytes()
        ).hexdigest()
    }
    assert report["inventory"] is None
    assert report["findings"] is None
    assert report["blocking"] is None
    assert not calls


def test_input_discovery_failure_does_not_invent_inventory(repository, tmp_path, monkeypatch):
    subprocess.run(["git", "rm", "--cached", "package-lock.json"], cwd=repository, check=True)
    calls = scanner(monkeypatch)
    output = tmp_path / "reports"
    assert audit.main(["--output-dir", str(output)]) == 2
    report = json.loads((output / "report.json").read_text())
    assert report["input_sha256"] == {}
    assert report["inventory"] is None
    assert report["findings"] is None
    assert report["blocking"] is None
    assert not calls


def test_inherited_scanner_filters_cannot_hide_findings(repository, tmp_path, monkeypatch):
    monkeypatch.setenv("TRIVY_SEVERITY", "LOW")
    monkeypatch.setenv("TRIVY_IGNORE_UNFIXED", "true")
    monkeypatch.setenv("TRIVY_SKIP_FILES", "**/package-lock.json")
    monkeypatch.setenv("TRIVY_SKIP_DB_UPDATE", "true")
    monkeypatch.setenv("TRIVY_CACHE_DIR", str(tmp_path / "cache"))
    scanner(monkeypatch, {"SchemaVersion": 2, "Results": [result(severity="HIGH")]})
    assert audit.main(["--output-dir", str(tmp_path / "reports")]) == 1


def test_repository_without_lockfiles_is_not_a_clean_scan(repository):
    subprocess.run(["git", "rm", "--cached", "package-lock.json"], cwd=repository, check=True)
    with pytest.raises(ValueError, match="No tracked"):
        audit.tracked_inputs(repository)


def test_cargo_manifest_does_not_make_trivy_drop_development_dependencies(repository):
    cargo = repository / "another-project"
    cargo.mkdir()
    (cargo / "Cargo.lock").write_text("version = 4\n")
    (cargo / "Cargo.toml").write_text('[package]\nname = "example"\nversion = "1.0.0"\n')
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    inputs = audit.tracked_inputs(repository)
    assert Path("another-project/Cargo.lock") in inputs
    assert Path("another-project/Cargo.toml") not in inputs


@pytest.mark.skipif(shutil.which("trivy") is None, reason="Requires the CI-pinned Trivy binary")
def test_real_trivy_keeps_dev_only_packages_in_published_inventory(repository, tmp_path):
    manifest = {
        "name": "inventory-node",
        "version": "1.0.0",
        "dependencies": {"is-number": "7.0.0"},
        "devDependencies": {"wrappy": "1.0.2"},
    }
    (repository / "package.json").write_text(json.dumps(manifest))
    (repository / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "inventory-node",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "packages": {
                    "": manifest,
                    "node_modules/is-number": {
                        "version": "7.0.0",
                        "resolved": "https://registry.npmjs.org/is-number/-/is-number-7.0.0.tgz",
                    },
                    "node_modules/wrappy": {
                        "version": "1.0.2",
                        "resolved": "https://registry.npmjs.org/wrappy/-/wrappy-1.0.2.tgz",
                        "dev": True,
                    },
                },
            }
        )
    )
    python = repository / "python"
    python.mkdir()
    (python / "pyproject.toml").write_text(
        '[project]\nname = "inventory-python"\nversion = "1.0.0"\n'
        'requires-python = ">=3.10"\ndependencies = ["idna==3.10"]\n'
        '[dependency-groups]\ndev = ["packaging==24.2"]\n'
    )
    (python / "uv.lock").write_text(
        'version = 1\nrevision = 3\nrequires-python = ">=3.10"\n'
        '[[package]]\nname = "inventory-python"\nversion = "1.0.0"\n'
        'source = { editable = "." }\ndependencies = [{ name = "idna" }]\n'
        '[package.dev-dependencies]\ndev = [{ name = "packaging" }]\n'
        '[[package]]\nname = "idna"\nversion = "3.10"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        '[[package]]\nname = "packaging"\nversion = "24.2"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    output = tmp_path / "reports"
    code = audit.main(["--output-dir", str(output)])
    report = json.loads((output / "report.json").read_text())
    assert code in {0, 1}, report
    assert set(report["input_sha256"]) == {"package-lock.json", "python/uv.lock"}
    inventories = {
        graph["path"]: {(item["name"], item["version"]) for item in graph["dependencies"]}
        for graph in report["inventory"]
    }
    assert {("is-number", "7.0.0"), ("wrappy", "1.0.2")} <= inventories["package-lock.json"]
    assert {("idna", "3.10"), ("packaging", "24.2")} <= inventories["python/uv.lock"]


def test_real_inventory_regression_has_a_required_scanner_in_ci():
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    job = ci["jobs"]["test"]
    assert "3.12" in job["strategy"]["matrix"]["python-version"]
    setup = next(
        step
        for step in job["steps"]
        if step.get("uses", "").startswith("aquasecurity/setup-trivy@")
    )
    action = yaml.safe_load(
        (ROOT / ".github" / "actions" / "dependency-security" / "action.yml").read_text()
    )
    assert setup["if"] == "matrix.python-version == '3.12'"
    assert setup["uses"] == action["runs"]["steps"][0]["uses"]
    assert setup["with"] == action["runs"]["steps"][0]["with"]
    tests = next(step for step in job["steps"] if "uv run pytest" in step.get("run", ""))
    assert 'if [ "${{ matrix.python-version }}" = "3.12" ]; then' in tests["run"]
    assert "trivy --version" in tests["run"]


def test_dependency_input_cannot_be_a_symlink(repository, tmp_path):
    target = tmp_path / "outside.json"
    target.write_text("{}")
    lock = repository / "package-lock.json"
    lock.unlink()
    lock.symlink_to(target)
    with pytest.raises(ValueError, match="repository file"):
        audit.tracked_inputs(repository)


def test_summary_escapes_untrusted_scanner_text():
    report = {"status": "blocked", "blocking": 1, "inventory": [], "findings": []}
    finding = {
        "severity": "HIGH",
        "advisory": "<script>[misleading](https://example.invalid)",
        "package": "name|extra\n::error::text",
        "version": "1",
        "fixed_version": "",
        "path": "package-lock.json",
    }
    report["findings"].append(finding)
    rendered = audit.render_summary(report)
    assert "<script>" not in rendered
    assert "[misleading]" not in rendered
    assert "name&#124;extra ::error::text" in rendered
    assert "\n::error::" not in rendered


def test_ci_and_scheduled_dependency_gates_are_identical_and_unconditional():
    workflows = ROOT / ".github" / "workflows"
    ci = yaml.safe_load((workflows / "ci.yml").read_text())
    scheduled = yaml.safe_load((workflows / "dependency-security.yml").read_text())
    job = ci["jobs"]["dependency-security"]
    assert job == scheduled["jobs"]["dependency-security"]
    assert job["name"] == "Dependency Security"
    assert job["permissions"] == {"contents": "read"}
    assert not {"if", "needs", "continue-on-error"} & job.keys()
    assert job["steps"][0]["with"] == {"persist-credentials": False}
    assert job["steps"][1]["uses"] == "./.github/actions/dependency-security"
    events = scheduled.get("on", scheduled.get(True))
    assert events == {"schedule": [{"cron": "0 8 * * 1"}], "workflow_dispatch": None}
    assert "pull_request_target" not in ci.get("on", ci.get(True))


def test_audit_reports_and_mitigations_are_not_skipped_after_scanner_setup_failure():
    action = yaml.safe_load(
        (ROOT / ".github" / "actions" / "dependency-security" / "action.yml").read_text()
    )
    steps = action["runs"]["steps"]
    scan_index = next(
        i for i, step in enumerate(steps) if "dependency_security.py" in step.get("run", "")
    )
    assert all("continue-on-error" not in step for step in steps)
    assert all(step.get("if") == "${{ !cancelled() }}" for step in steps[scan_index:])
    upload = next(
        step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["name"] == "dependency-security-${{ github.run_attempt }}"
    assert "secrets." not in yaml.safe_dump(action)


@pytest.mark.parametrize("project", ["docusaurus", "web", "cao_mcp_apps"])
def test_all_affected_projects_install_the_shared_patch_and_regressions(project):
    manifest = json.loads((ROOT / project / "package.json").read_text())
    assert manifest["dependencies"]["braces"] == "file:braces-compat"
    assert manifest["dependencies"]["@dieub/braces-depth-guard"] == "3.0.3-pn.3"
    assert manifest["overrides"]["braces"] == "$braces"
    assert manifest["dependencies"]["patch-package"] == "8.0.1"
    assert (
        manifest["scripts"]["postinstall"] == "patch-package --patch-dir ../patches --error-on-fail"
    )
    wrapper = (
        "test/dependency-security.test.cjs"
        if project == "docusaurus"
        else "scripts/dependency-security.cjs"
    )
    assert manifest["scripts"]["test:dependencies"] == f"node --test {wrapper}"
    assert "../../scripts/test-braces-security.cjs" in (ROOT / project / wrapper).read_text()
    lock = json.loads((ROOT / project / "package-lock.json").read_text())
    assert lock["lockfileVersion"] == 3
    assert "install-links=false" in (ROOT / project / ".npmrc").read_text().splitlines()
    assert lock["packages"]["node_modules/braces"] == {
        "resolved": "braces-compat",
        "link": True,
    }
    adapter = json.loads((ROOT / project / "braces-compat" / "package.json").read_text())
    assert adapter["private"] is True
    assert adapter["name"] == "cao-braces-compat"
    assert adapter["peerDependencies"] == {"@dieub/braces-depth-guard": "3.0.3-pn.3"}
    entry = lock["packages"]["node_modules/@dieub/braces-depth-guard"]
    assert entry["version"] == "3.0.3-pn.3"
    assert entry["resolved"] == (
        "https://registry.npmjs.org/@dieub/braces-depth-guard/-/"
        "braces-depth-guard-3.0.3-pn.3.tgz"
    )


@pytest.mark.parametrize(
    "failure",
    [None]
    + [
        (project, stage)
        for project in ("docusaurus", "web", "cao_mcp_apps")
        for stage in ("ci", "run")
    ],
)
def test_mitigation_step_checks_all_projects_and_preserves_failures(tmp_path, failure):
    action = yaml.safe_load(
        (ROOT / ".github" / "actions" / "dependency-security" / "action.yml").read_text()
    )
    step = next(step for step in action["runs"]["steps"] if "npm ci" in step.get("run", ""))
    npm = tmp_path / "npm"
    npm.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$*" >> "$NPM_CALLS"\n'
        'if [[ "$*" == "$FAIL_COMMAND" ]]; then exit 1; fi\nexit 0\n'
    )
    npm.chmod(0o755)
    commands = {
        (project, "ci"): f"ci --prefix {project} --no-audit --no-fund"
        for project in ("docusaurus", "web", "cao_mcp_apps")
    } | {
        (project, "run"): f"run test:dependencies --prefix {project}"
        for project in ("docusaurus", "web", "cao_mcp_apps")
    }
    calls = tmp_path / "calls"
    summary = tmp_path / "summary"
    run = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "NPM_CALLS": str(calls),
            "FAIL_COMMAND": commands[failure] if failure else "",
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert run.returncode == int(failure is not None), run.stderr
    expected = []
    for project in ("docusaurus", "web", "cao_mcp_apps"):
        expected.append(commands[project, "ci"])
        if failure != (project, "ci"):
            expected.append(commands[project, "run"])
        status = (
            "installation or mitigation checks FAILED."
            if failure and failure[0] == project
            else "mitigation checks passed; advisory status is unchanged."
        )
        assert f"- `{project}`: {status}" in summary.read_text()
    assert calls.read_text().splitlines() == expected


def test_shared_patch_and_tests_trigger_the_docs_build():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "gh-pages.yml").read_text())
    events = workflow.get("on", workflow.get(True))
    for event in ("push", "pull_request"):
        assert {"patches/**", "scripts/test-braces-security.cjs"} <= set(events[event]["paths"])
