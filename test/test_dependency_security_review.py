"""Review follow-ups for #1398: extras in the shipped set, ignore-file date rules, path-less
entries, and a fail-closed top level. Reuses the fixtures of test_dependency_security_shipped.py."""

from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path
from test.test_dependency_security_shipped import (  # noqa: F401  (fixture re-export)
    TODAY,
    _no_ci_event,
    audit,
    ignore_yaml,
    pkg,
    repository,
    run_gate,
    trivy_report,
    vuln,
    web_shipped,
)

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB_HIGH = {
    "web/package-lock.json": (
        [pkg("react", "18.3.1"), pkg("example-shipped", "1.0.0")],
        [vuln("example-shipped", "1.0.0")],
    )
}


# ── MUST-FIX 1: optional extras are shipped ──────────────────────────────────


def test_uv_export_includes_every_optional_extra(monkeypatch):
    seen = {}

    def fake(command, cwd):
        seen["command"] = command
        return "click==8.1.7\nopentelemetry-sdk==1.27.0\n"

    monkeypatch.setattr(audit, "_run_producer", fake)
    audit.shipped_python_set(ROOT)
    assert "--all-extras" in seen["command"]
    assert "--locked" in seen["command"] and "--frozen" not in seen["command"]
    assert "--no-dev" in seen["command"]


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_real_uv_export_contains_the_otel_and_agui_extras():
    names = {name.lower().replace("_", "-") for name, _ in audit.shipped_python_set(ROOT)}
    assert {"opentelemetry-sdk", "ag-ui-protocol", "grpcio", "protobuf"} <= names
    assert "pytest" not in names  # dev-only stays out


# ── SHOULD-FIX 2: the 14-day cap cannot be dodged with a future `added` ───────


def _load(tmp_path, **fields):
    fields.setdefault("paths", ["web/package-lock.json"])
    path = tmp_path / "ignore.yaml"
    path.write_text(ignore_yaml(**fields))
    return audit.load_ignore_file(path, date.fromisoformat(TODAY))


def test_added_in_the_future_is_rejected(tmp_path):
    with pytest.raises(audit.IgnoreFileError, match="future"):
        _load(tmp_path, added="2026-11-30", expired_at="2026-12-07")


def test_added_today_with_a_full_14_days_is_accepted(tmp_path):
    entries = _load(tmp_path, added=TODAY, expired_at="2026-12-01")
    assert entries[0]["state"] == "valid"


def test_expiry_more_than_14_days_from_today_is_rejected(tmp_path):
    with pytest.raises(audit.IgnoreFileError, match="14"):
        _load(tmp_path, added=TODAY, expired_at="2026-12-02")


def test_future_added_through_the_gate_is_an_error(repository, tmp_path, monkeypatch):
    code, report, _ = run_gate(
        tmp_path,
        monkeypatch,
        trivy_report(**WEB_HIGH),
        web_shipped(("example-shipped", "1.0.0")),
        ignore=ignore_yaml(id="CVE-2099-20001", added="2026-12-20", expired_at="2026-12-27"),
    )
    assert code == audit.EXIT_ERROR == 2
    assert report["status"] == "error"


# ── #1400 G: an entry scoped to nothing is rejected (was a warning in round 1) ──


def test_pathless_entry_is_rejected_and_pathful_covers(repository, tmp_path, monkeypatch):
    code, report, _ = run_gate(
        tmp_path,
        monkeypatch,
        trivy_report(**WEB_HIGH),
        web_shipped(("example-shipped", "1.0.0")),
        ignore=ignore_yaml(paths=None),  # None drops the field from the builder's output
    )
    assert code == audit.EXIT_ERROR == 2
    assert "paths" in report["error"] and "purl" in report["error"]

    pathful = tmp_path / "second"
    pathful.mkdir()
    code, report, _ = run_gate(
        pathful,
        monkeypatch,
        trivy_report(**WEB_HIGH),
        web_shipped(("example-shipped", "1.0.0")),
        ignore=ignore_yaml(paths=["web/package-lock.json"]),
    )
    assert code == 0
    assert report["findings"][0]["ignore"]["state"] == "valid"


# ── NOTE 7: an unexpected exception fails closed, never "blocked" ─────────────


def test_unexpected_exception_exits_2_with_a_report(repository, tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(audit, "dependency_paths", boom)
    code, report, out = run_gate(
        tmp_path,
        monkeypatch,
        trivy_report(**WEB_HIGH),
        web_shipped(("example-shipped", "1.0.0")),
    )
    assert code == audit.EXIT_ERROR == 2
    assert report["status"] == "error"
    assert "RuntimeError" in report["error"]
    assert report["findings"] is None and report["blocking"] is None
    assert (out / "summary.md").is_file()
    assert "No clean security verdict" in (out / "summary.md").read_text()


# ── #1400 round 2, item 1: the pull-request mode is explicit, and fails closed ──


def test_pull_request_mode_without_a_base_exits_2(repository, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    code, report, _ = run_gate(tmp_path, monkeypatch, trivy_report())
    assert code == audit.EXIT_ERROR == 2
    assert "BaseUnavailableError" in report["error"]


def test_push_mode_without_a_base_runs_the_gate(repository, tmp_path, monkeypatch):
    """The control: the same inputs outside a pull request reach a verdict."""
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    code, report, _ = run_gate(tmp_path, monkeypatch, trivy_report())
    assert code == 0 and report["status"] == "passed"


# ── #1400 round 2, P1: npm aliases ────────────────────────────────────────────

DOCS_ALIAS = {
    "docusaurus/package-lock.json": (
        [pkg("react", "18.3.1"), pkg("@slorber/react-helmet-async", "1.3.0")],
        [vuln("@slorber/react-helmet-async", "1.3.0", severity="CRITICAL")],
    )
}


def _docs_shipped(*names):
    from test.test_dependency_security_shipped import shipped_set

    return shipped_set(
        {
            "docusaurus/package-lock.json": [{"name": "react", "version": "18.3.1"}]
            + [{"name": n, "version": "1.3.0"} for n in names]
        }
    )


def test_aliased_bundled_package_blocks(repository, tmp_path, monkeypatch):
    """The recorder writes both names for an alias; Trivy's real name must then match."""
    code, report, _ = run_gate(
        tmp_path,
        monkeypatch,
        trivy_report(**DOCS_ALIAS),
        _docs_shipped("react-helmet-async", "@slorber/react-helmet-async"),
    )
    assert code == audit.EXIT_BLOCKED == 1
    assert report["findings"][0]["classification"] == "shipped"


def test_alias_folder_name_alone_does_not_match(repository, tmp_path, monkeypatch):
    """The control, and why the recorder must write the real name: folder name only misses."""
    code, report, _ = run_gate(
        tmp_path, monkeypatch, trivy_report(**DOCS_ALIAS), _docs_shipped("react-helmet-async")
    )
    assert code == 0
    assert report["findings"][0]["classification"] == "toolchain"


def test_checked_in_html_writer_patterns_match_aliased_real_names():
    import fnmatch
    import json as _json

    policy = _json.loads((ROOT / ".github" / "dependency-security" / "policy.json").read_text())
    patterns = policy["html_writers"]["docusaurus/package-lock.json"]
    for real in ("@slorber/react-helmet-async", "@docusaurus/react-loadable"):
        assert any(fnmatch.fnmatchcase(real, p) for p in patterns), real


# ── #1400 round 2, P2: a failed weekly scan must not clear the tracking issue ──


def _tracking_step():
    import yaml

    doc = yaml.safe_load((ROOT / ".github" / "workflows" / "dependency-security.yml").read_text())
    steps = doc["jobs"]["toolchain-tracking"]["steps"]
    return next(s for s in steps if s.get("name") == "Open or update the tracking issue")


def _run_tracking(tmp_path, status, with_issue_body=False):
    """Run the step's own `run:` block with a fake `gh` and an open tracking issue 42."""
    import os
    import subprocess as sp

    report = tmp_path / "report"
    report.mkdir()
    (report / "report.json").write_text(
        json.dumps({"status": status, "error": "TimeoutExpired: x"})
    )
    if with_issue_body:
        (report / "toolchain-issue.md").write_text("findings\n")
    fake = tmp_path / "bin"
    fake.mkdir()
    calls = tmp_path / "calls"
    (fake / "gh").write_text(
        '#!/bin/bash\nprintf "%s\\n" "$*" >> "$GH_CALLS"\n'
        'if [ "$1 $2" = "issue list" ]; then echo 42; fi\nexit 0\n'
    )
    (fake / "gh").chmod(0o755)
    result = sp.run(
        ["bash", "-e", "-o", "pipefail", "-c", _tracking_step()["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}",
            "GH_CALLS": str(calls),
            "REVISION": "abc123",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result, calls.read_text() if calls.exists() else ""


def test_tracking_issue_is_not_cleared_after_a_gate_error(tmp_path):
    result, calls = _run_tracking(tmp_path, "error")
    assert result.returncode == 0, result.stderr
    assert "none.md" not in calls, "an error run must not mark the issue clear"
    assert "issue close" not in calls
    assert "issue comment 42" in calls
    assert "error" in (tmp_path / "report" / "error.md").read_text()


def test_tracking_issue_is_cleared_after_a_clean_run(tmp_path):
    """The control: a passed run with no toolchain findings does mark it clear."""
    result, calls = _run_tracking(tmp_path, "passed")
    assert result.returncode == 0, result.stderr
    assert "issue edit 42 --body-file report/none.md" in calls


def test_tracking_issue_is_updated_when_findings_remain(tmp_path):
    result, calls = _run_tracking(tmp_path, "passed", with_issue_body=True)
    assert "issue edit 42 --body-file report/toolchain-issue.md" in calls


# ── #1400 round 2, P3.1: the template documents what the loader enforces ─────


def _template_example():
    """The commented example in the checked-in ignore file, uncommented."""
    text = (ROOT / ".github" / "dependency-security" / "trivyignore.yaml").read_text()
    lines = text.splitlines()
    start = lines.index("#   vulnerabilities:")
    body = []
    for line in lines[start:]:
        if not line.startswith("#   "):
            break
        body.append(line[4:].split("  # ")[0].rstrip())
    return "\n".join(body) + "\n"


def test_template_example_is_accepted_and_shows_both_scopes(tmp_path):
    example = _template_example()
    path = tmp_path / "ignore.yaml"
    path.write_text(example)
    entries = audit.load_ignore_file(path, date(2026, 11, 18))
    assert len(entries) == 2
    assert any(e["paths"] for e in entries) and any(e["purl"] for e in entries)
    text = (ROOT / ".github" / "dependency-security" / "trivyignore.yaml").read_text()
    assert "omitted means every lockfile" not in text


def test_template_example_without_a_scope_is_rejected(tmp_path):
    """The control: drop both scopes and the loader refuses the same example."""
    example = "\n".join(
        line
        for line in _template_example().splitlines()
        if "paths:" not in line and "purl:" not in line
    )
    path = tmp_path / "ignore.yaml"
    path.write_text(example + "\n")
    with pytest.raises(audit.IgnoreFileError, match="paths' or 'purl'"):
        audit.load_ignore_file(path, date(2026, 11, 18))
