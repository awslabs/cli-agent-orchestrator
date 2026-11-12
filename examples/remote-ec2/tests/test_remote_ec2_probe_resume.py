"""bin/resource-probe-linux.sh with fake /proc files; bin/box-resume.sh with stubs."""

from __future__ import annotations

from pathlib import Path

import pytest
from remote_ec2_support import FAKE_INSTANCE_ID, write_stub


def fake_proc(base: Path, total_kb: int, avail_kb: int, avg10: str | None) -> Path:
    proc = base / "proc"
    proc.mkdir()
    (proc / "meminfo").write_text(
        f"MemTotal:       {total_kb} kB\nMemFree:          1000 kB\nMemAvailable:   {avail_kb} kB\n",
        encoding="utf-8",
    )
    if avg10 is not None:
        (proc / "pressure").mkdir()
        (proc / "pressure" / "memory").write_text(
            f"some avg10={avg10} avg60=0.00 avg300=0.00 total=1\n"
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n",
            encoding="utf-8",
        )
    return proc


@pytest.mark.parametrize(
    ("avail", "avg10", "code", "word"),
    [
        (32_000_000, "0.50", 0, "ok"),
        (32_000_000, "42.10", 1, "throttle"),
        (2_000_000, "0.00", 1, "throttle"),
        (32_000_000, None, 0, "ok"),
    ],
)
def test_probe(sandbox, avail, avg10, code, word) -> None:
    proc = fake_proc(sandbox.root, 64_000_000, avail, avg10)
    result = sandbox.run("bin/resource-probe-linux.sh", PROC_ROOT=str(proc))
    assert result.returncode == code, result.stdout + result.stderr
    assert result.stdout.startswith(word)
    if avg10 is None:
        assert "psi=unavailable" in result.stdout


def test_probe_unknown_without_meminfo(sandbox) -> None:
    result = sandbox.run("bin/resource-probe-linux.sh", PROC_ROOT=str(sandbox.root / "missing"))
    assert result.returncode == 2
    assert result.stdout.startswith("unknown")


def resume_stubs(sandbox, systemctl_code: int) -> dict[str, str]:
    write_stub(sandbox.stubs, "systemctl", f"exit {systemctl_code}\n")
    write_stub(sandbox.stubs, "curl", "exit 0\n")
    write_stub(sandbox.stubs, "herdr", "exit 0\n")
    box_env = sandbox.root / "box.env"
    box_env.write_text(f"CAO_BOX_SESSION=cao-{FAKE_INSTANCE_ID}\n", encoding="utf-8")
    return {"BOX_ENV_FILE": str(box_env)}


def test_resume_all_ok(sandbox) -> None:
    result = sandbox.run("bin/box-resume.sh", **resume_stubs(sandbox, 0))
    assert result.returncode == 0, result.stdout
    assert f"session: cao-{FAKE_INSTANCE_ID}" in result.stdout
    assert "FAIL" not in result.stdout
    assert "All up" in result.stdout


def test_resume_reports_a_down_unit(sandbox) -> None:
    result = sandbox.run("bin/box-resume.sh", **resume_stubs(sandbox, 3))
    assert result.returncode == 1
    assert "FAIL  herdr-cao.service is active" in result.stdout
    assert "ok    cao-server /health answers" in result.stdout
