"""The operator dashboard job gets tmux without apt on a warm cache (#1370)."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_tmux.sh"
CI = ROOT / ".github" / "workflows" / "ci.yml"
PACKAGE_MANAGER = re.compile(r"\bapt(-get)?\b|\bdpkg\b")


def _job():
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["ag-ui-operator-dashboard"]


def _steps():
    return _job()["steps"]


def _step(prefix):
    return next(s for s in _steps() if s.get("name", "").startswith(prefix))


def _code_lines():
    return [
        line
        for line in SCRIPT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_build_script_pins_a_sha256_and_verifies_it():
    lines = _code_lines()
    assert re.search(r'^TMUX_SHA256="[0-9a-f]{64}"$', "\n".join(lines), re.M)
    checks = [i for i, line in enumerate(lines) if "sha256sum --check --strict" in line]
    assert len(checks) == 1, "expected exactly one executable checksum check"
    assert "${TMUX_SHA256}" in lines[checks[0]] or "$TMUX_SHA256" in lines[checks[0]]
    extract = [i for i, line in enumerate(lines) if re.search(r"\btar -x", line)]
    assert extract and checks[0] < min(extract), "verify the tarball before extracting it"


def test_apt_runs_only_in_the_cold_cache_build_step():
    steps = _steps()
    offenders = [
        s.get("name")
        for s in steps
        if PACKAGE_MANAGER.search(s.get("run") or "")
        and not s.get("name", "").startswith("Build tmux")
    ]
    assert offenders == [], f"apt/dpkg outside the cold-cache step: {offenders}"

    build = _step("Build tmux")
    assert build["if"] == "steps.tmux-cache.outputs.cache-hit != 'true'"
    assert "scripts/build_tmux.sh" in build["run"]
    assert PACKAGE_MANAGER.search(SCRIPT.read_text(encoding="utf-8"))

    install = _step("Install tmux")
    assert not PACKAGE_MANAGER.search(install["run"])

    cache_dir = _step("Restore cached tmux")["with"]["path"]
    assert build["run"].split()[-1] == cache_dir, "build output dir must be the cached path"
    assert f"{cache_dir}/tmux" in install["run"], "install must read from the cached path"


def test_cache_key_covers_arch_image_and_build_script():
    key = _step("Restore cached tmux")["with"]["key"]
    assert "${{ runner.arch }}" in key
    # `job.container.image` is not an available expression, so the digest is spelled
    # out in the key and must follow the job's container.
    digest = _job()["container"].split("@sha256:")[1]
    assert f"sha256-{digest}" in key
    assert "hashFiles('scripts/build_tmux.sh')" in key
