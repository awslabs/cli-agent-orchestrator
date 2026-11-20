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


SESSION = f"cao-{FAKE_INSTANCE_ID}"


def herdr_status_stub(sandbox, server_status: str) -> Path:
    """A herdr stub shaped like herdr 0.9.3: `--session S status` prints a
    client and a server section and exits 0 whatever the server's state.
    The server is only "running" for the box's own session."""
    log = sandbox.root / "herdr.log"
    body = (
        f'echo "herdr $*" >> "{log}"\n'
        'state="not running"\n'
        f'if [ "$1" = "--session" ] && [ "$2" = "{SESSION}" ] && [ "$3" = "status" ]; then\n'
        f'  state="{server_status}"\n'
        "fi\n"
        "printf 'client:\\n  version: 0.9.3\\n  protocol: 22\\n'\n"
        "printf 'server:\\n  status: %s\\n  socket: /tmp/herdr.sock\\n' \"$state\"\n"
        "printf 'update:\\n  restart_needed: no\\n'\n"
        "exit 0\n"
    )
    write_stub(sandbox.stubs, "herdr", body)
    return log


def resume_stubs(sandbox, systemctl_code: int, server_status: str = "running") -> dict[str, str]:
    write_stub(sandbox.stubs, "systemctl", f"exit {systemctl_code}\n")
    write_stub(sandbox.stubs, "curl", "exit 0\n")
    herdr_status_stub(sandbox, server_status)
    box_env = sandbox.root / "box.env"
    box_env.write_text(f"CAO_BOX_SESSION={SESSION}\n", encoding="utf-8")
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


def test_resume_checks_the_box_session(sandbox) -> None:
    result = sandbox.run("bin/box-resume.sh", **resume_stubs(sandbox, 0))
    assert result.returncode == 0, result.stdout
    assert f"ok    herdr session {SESSION} is running" in result.stdout
    calls = (sandbox.root / "herdr.log").read_text(encoding="utf-8").splitlines()
    assert calls == [f"herdr --session {SESSION} status"]


def test_resume_fails_when_the_herdr_server_is_not_running(sandbox) -> None:
    # herdr exits 0 here, as the real one does: only the text says it's down.
    result = sandbox.run("bin/box-resume.sh", **resume_stubs(sandbox, 0, "not running"))
    assert result.returncode == 1, result.stdout
    assert f"FAIL  herdr session {SESSION} is running" in result.stdout
    assert "All up" not in result.stdout


def test_resume_fails_herdr_check_without_a_session(sandbox) -> None:
    env = resume_stubs(sandbox, 0)
    Path(env["BOX_ENV_FILE"]).unlink()
    result = sandbox.run("bin/box-resume.sh", **env)
    assert result.returncode == 1, result.stdout
    assert "FAIL  herdr session <unknown> is running" in result.stdout
    assert not (sandbox.root / "herdr.log").exists()


def test_resume_hint_is_cao_shutdown_not_ghost_line(sandbox) -> None:
    result = sandbox.run("bin/box-resume.sh", **resume_stubs(sandbox, 0))
    assert "cao shutdown --session <name>" in result.stdout
    assert "ghost" not in result.stdout
