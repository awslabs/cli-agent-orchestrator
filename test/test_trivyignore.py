"""Structural guard for the scoped, expiring Security Scan exemption file.

``.trivyignore.yaml`` (see its header comment and SECURITY.md) suppresses CVE-2026-118764 for Security
Scan only, at two exact lockfile paths and purls, until it expires. This test
pins its shape — every entry stays scoped and attributable — and pins that
both trivy-action steps in ci.yml see the same exemption file. It
deliberately does NOT assert on ``expired_at``'s value: that would make every
PR red the moment the date approaches, which is Trivy's job, not this test's.
"""

import importlib.util
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TRIVYIGNORE = ROOT / ".trivyignore.yaml"

_spec = importlib.util.spec_from_file_location(
    "dependency_security", ROOT / "scripts" / "dependency_security.py"
)
assert _spec is not None and _spec.loader is not None
_audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_audit)


def _entries():
    data = yaml.safe_load(TRIVYIGNORE.read_text())
    # Any other top-level section (secrets, misconfigurations, licenses) is an
    # unscoped exemption this guard does not check, so it must fail here.
    assert set(data) == {"vulnerabilities"}, sorted(data)
    return data["vulnerabilities"]


def test_every_entry_is_fully_scoped_and_attributable():
    entries = _entries()
    assert entries
    tracked = set(
        subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
        )
        .stdout.decode()
        .split("\0")
    )
    for entry in entries:
        assert entry["id"], entry
        assert entry["paths"], entry
        for path in entry["paths"]:
            assert path in tracked, f"{path} is not a tracked repository file"
            assert Path(path).name in _audit.LOCKFILES, f"{path} is not a recognised lockfile"
        assert entry["purls"], entry
        assert entry["statement"].strip(), entry
        assert entry["expired_at"], entry


def test_both_trivy_steps_share_the_same_ignorefile():
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    steps = ci["jobs"]["security"]["steps"]
    trivy_steps = [
        step
        for step in steps
        if step.get("uses", "").startswith("aquasecurity/trivy-action@")
    ]
    assert len(trivy_steps) == 2
    ignorefiles = {step["with"].get("trivyignores") for step in trivy_steps}
    assert ignorefiles == {".trivyignore.yaml"}
