"""Full-graph vulnerability policy, coverage and error-boundary regressions."""

import importlib.util
import json
import os
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
        "package.json",
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
    assert "No clean security verdict" in (output / "summary.md").read_text()
    assert not Path(calls[0][-1]).exists()


def test_invalid_scanner_json_fails(repository, tmp_path, monkeypatch):
    scanner(monkeypatch, report="not JSON")
    assert audit.main(["--output-dir", str(tmp_path / "reports")]) == 2


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


def test_reports_and_mitigation_checks_are_not_skipped_when_the_audit_fails():
    action = yaml.safe_load(
        (ROOT / ".github" / "actions" / "dependency-security" / "action.yml").read_text()
    )
    steps = action["runs"]["steps"]
    scan_index = next(
        i for i, step in enumerate(steps) if "dependency_security.py" in step.get("run", "")
    )
    assert all("continue-on-error" not in step for step in steps)
    assert all(step.get("if") == "${{ !cancelled() }}" for step in steps[scan_index + 1 :])
    upload = next(
        step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["name"] == "dependency-security-${{ github.run_attempt }}"
    assert "secrets." not in yaml.safe_dump(action)


@pytest.mark.parametrize("project", ["docusaurus", "web", "cao_mcp_apps"])
def test_all_affected_projects_install_the_shared_patch_and_regressions(project):
    manifest = json.loads((ROOT / project / "package.json").read_text())
    assert manifest["overrides"]["braces"] == "3.0.3"
    assert manifest["dependencies"]["patch-package"] == "8.0.1"
    assert (
        manifest["scripts"]["postinstall"] == "patch-package --patch-dir ../patches --error-on-fail"
    )
    assert "test:dependencies" in manifest["scripts"]


def test_shared_patch_and_tests_trigger_the_docs_build():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "gh-pages.yml").read_text())
    events = workflow.get("on", workflow.get(True))
    for event in ("push", "pull_request"):
        assert {"patches/**", "scripts/test-braces-security.cjs"} <= set(events[event]["paths"])
