"""The rig's safety rules (lib.sh): it refuses unsafe scratch roots and ports, and it kills only what it recorded."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Iterator

import pytest

RIG = Path(__file__).resolve().parent
HOME = Path(os.path.expanduser("~"))


@pytest.fixture()
def scratch() -> Iterator[Path]:
    # never /tmp: the rig refuses it, and it is wiped on restart
    root = Path(tempfile.mkdtemp(prefix="kc-shelf-rig-selftest-", dir=HOME))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def bash(script: str, scratch_root: str | Path, **extra: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "SHELF_SCRATCH": str(scratch_root), **extra}
    return subprocess.run(
        ["bash", "-c", f'source "{RIG}/lib.sh"; {script}'],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return (
        Path(f"/proc/{pid}/stat").exists()
        and "Z" not in Path(f"/proc/{pid}/stat").read_text().split(") ")[-1][:2]
    )


@pytest.mark.parametrize(
    "bad",
    [
        "/",
        "/tmp/shelf",
        "/var/tmp/x",
        str(HOME),
        str(HOME / ".kiro" / "scratch"),
        str(HOME / ".aws" / "s"),
        "relative/dir",
    ],
)
def test_unsafe_scratch_roots_are_refused(bad: str) -> None:
    res = bash("rig_check_scratch", bad)
    assert res.returncode != 0, res.stdout
    assert "rig:" in res.stderr


def test_empty_scratch_is_refused() -> None:
    res = bash("SHELF_SCRATCH=''; rig_check_scratch", "")
    assert res.returncode != 0


@pytest.mark.parametrize("name,port", [("SHELF_KC_PORT", "5476"), ("SHELF_CAO_PORT", "9889")])
def test_the_owners_ports_are_refused(scratch: Path, name: str, port: str) -> None:
    res = bash("rig_check_scratch; rig_check_ports", scratch, **{name: port})
    assert res.returncode != 0
    assert "belongs to the owner" in res.stderr


def test_spawn_records_and_stop_kills_only_the_recorded_process_and_its_children(
    scratch: Path,
) -> None:
    ident = f"HOME={scratch}/home"
    script = f"""
        rig_check_scratch
        pid="$(rig_spawn demo demo '{ident}' "$LOG_DIR/demo.log" -- env -i HOME='{scratch}/home' PATH=/usr/bin:/bin sh -c 'sleep 300 & sleep 300')"
        echo "$pid"
        rec_alive demo && echo ALIVE
        child="$(descendants "$pid" | head -n 1)"
        echo "child=$child"
    """
    res = bash(script, scratch)
    assert res.returncode == 0, res.stderr
    lines = res.stdout.split()
    pid = int(lines[0])
    assert "ALIVE" in lines
    bystander = subprocess.Popen(["sleep", "300"])  # a process the rig did not start
    try:
        stop = bash("rig_stop demo", scratch)
        assert stop.returncode == 0, stop.stderr
        time.sleep(0.5)
        assert not alive(pid)
        assert alive(bystander.pid), "the rig must not touch a process it did not record"
        assert not (scratch / "work-c" / "run" / "demo.pid").exists()
    finally:
        bystander.kill()
        bystander.wait()


def test_stop_refuses_a_record_that_does_not_match_the_live_process(scratch: Path) -> None:
    victim = subprocess.Popen(["sleep", "300"])
    try:
        run_dir = scratch / "work-c" / "run"
        run_dir.mkdir(parents=True)
        # a record that names an unrelated live pid: wrong start time and no identity in its environment
        (run_dir / "gateway.pid").write_text(
            f"pid={victim.pid}\nstart=1\nkind=x\nidentity=HOME={scratch}/home\n", encoding="utf-8"
        )
        res = bash("rig_check_scratch; rig_stop gateway", scratch)
        assert res.returncode != 0
        assert "NOT killing" in res.stderr
        assert alive(victim.pid)
    finally:
        victim.kill()
        victim.wait()


def test_stop_with_no_record_does_nothing(scratch: Path) -> None:
    res = bash("rig_check_scratch; rig_stop gateway", scratch)
    assert res.returncode == 0
    assert "no pid record" in res.stdout


def test_kc_wrapper_refuses_the_commands_that_act_on_the_real_service(scratch: Path) -> None:
    for sub in ("stop", "restart", "service", "doctor", "gateway"):
        res = subprocess.run(
            [str(RIG / "kc.sh"), "kirocrew", sub],
            env={**os.environ, "SHELF_SCRATCH": str(scratch)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.returncode != 0 and "refused" in res.stderr, sub


@pytest.mark.parametrize("port", ["5476", "9889", "5499", "9990"])
def test_the_model_port_cannot_be_an_owner_or_rig_port(scratch: Path, port: str) -> None:
    res = bash("rig_check_scratch; rig_check_ports", scratch, SHELF_MODEL_PORT=port)
    assert res.returncode != 0
    assert "refusing model port" in res.stderr


def test_the_adapter_folder_must_live_in_the_scratch_folder(scratch: Path) -> None:
    res = bash("rig_check_scratch", scratch, SHELF_ACP_DIR=str(HOME / "elsewhere-acp"))
    assert res.returncode != 0
    assert "must live inside SHELF_SCRATCH" in res.stderr


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def kiro_crew(scratch: Path, *args: str, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(RIG / "kiro_crew.sh"), *args],
        env={**os.environ, "SHELF_SCRATCH": str(scratch), **extra},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_mock_model_starts_on_loopback_with_a_record_and_stops_by_that_record(scratch: Path) -> None:
    port = str(free_port())
    bystander = subprocess.Popen(["sleep", "300"])
    try:
        up = kiro_crew(scratch, "model-start", SHELF_MODEL_PORT=port)
        assert up.returncode == 0, up.stderr + up.stdout
        rec = (scratch / "work-c" / "run" / "mock-model.pid").read_text()
        assert f"identity=SHELF_MOCK_MODEL={scratch}/work-c" in rec
        pid = int(rec.split("pid=")[1].split()[0])
        listen = subprocess.run(["ss", "-ltnH", f"sport = :{port}"], capture_output=True, text=True)
        local_addrs = [line.split()[3] for line in listen.stdout.splitlines() if line.strip()]
        assert local_addrs == [f"127.0.0.1:{port}"]
        again = kiro_crew(scratch, "model-start", SHELF_MODEL_PORT=port)
        assert again.returncode == 0 and "already running" in again.stdout
        down = kiro_crew(scratch, "model-stop", SHELF_MODEL_PORT=port)
        assert down.returncode == 0, down.stderr
        time.sleep(0.5)
        assert not alive(pid)
        assert alive(bystander.pid)
        assert not (scratch / "work-c" / "run" / "mock-model.pid").exists()
    finally:
        bystander.kill()
        bystander.wait()
        kiro_crew(scratch, "model-stop", SHELF_MODEL_PORT=port)


def test_claude_settings_go_into_the_scratch_home_only_and_keep_existing_keys(scratch: Path) -> None:
    settings = scratch / "home" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"permissions": {"allow": ["Read"]}, "model": "x"}', encoding="utf-8")
    real = HOME / ".claude" / "settings.json"
    before = real.stat().st_mtime_ns if real.exists() else None
    res = kiro_crew(scratch, "claude-settings")
    assert res.returncode == 0, res.stderr
    data = __import__("json").loads(settings.read_text(encoding="utf-8"))
    ops = [
        f"mcp__cao-shelf_ops__{t}"
        for t in ("send_session_message", "get_terminal_status", "read_session_output", "get_session_info")
    ]
    assert data["permissions"]["allow"] == ["Read", *ops, "mcp__kirocrew-core__wait"]
    assert data["env"]["ENABLE_TOOL_SEARCH"] == "false" and data["model"] == "x"
    assert (real.stat().st_mtime_ns if real.exists() else None) == before
