from __future__ import annotations

import json
import re
import shlex
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import demo
import pytest
import yaml
from jsonschema import validate

EXAMPLE = Path(__file__).resolve().parents[1]
SCENE = EXAMPLE / "factory.json"
SCHEMA = EXAMPLE.parents[2] / "src/cli_agent_orchestrator/schemas/agent_profile.schema.json"
CAO_SOURCE = EXAMPLE.parents[2] / "src/cli_agent_orchestrator"
MANAGER_ROLES = ("serial", "parallel", "formation")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.parametrize(
    "factory_request",
    [
        "Make, assemble, and ship 8 units. Start Team A at 2 s and Team B at 8 s.",
        "Ship 4 units in batches of 4 with the routine turn, run, spin.",
        '--inspect "operator\'s order"; preserve $STATUS\nwithout moving it.',
    ],
)
def test_prepare_prints_a_nonblocking_launch_with_the_request(
    tmp_path, monkeypatch, capsys, factory_request
):
    run_dir = tmp_path / "run"
    monkeypatch.setattr(
        sys,
        "argv",
        ["demo.py", "prepare", "--run-dir", str(run_dir), f"--request={factory_request}"],
    )
    demo.main()
    commands = shlex.split(capsys.readouterr().out)
    launch = commands[commands.index("launch") - 1 :]
    assert launch[:2] == ["cao", "launch"]
    assert {"--headless", "--async", "--auto-approve"} <= set(launch)
    assert "--yolo" not in launch
    run_id = json.loads((run_dir / "run.json").read_text())["run_id"]
    assert launch[launch.index("--session-name") + 1] == f"cao-factory-{run_id}"
    assert launch[-2:] == ["--", factory_request]
    # One install command for each agent: the supervisor and three managers.
    assert commands.count("install") == 4


@pytest.mark.parametrize("request_args", [[], ["--request="], ["--request= \t\n"]])
def test_prepare_requires_a_nonblank_request_before_creating_run(
    tmp_path, monkeypatch, capsys, request_args
):
    run_dir = tmp_path / "run"
    monkeypatch.setattr(
        sys, "argv", ["demo.py", "prepare", "--run-dir", str(run_dir), *request_args]
    )
    with pytest.raises(SystemExit) as error:
        demo.main()
    assert error.value.code == 2
    assert "--request" in capsys.readouterr().err
    assert not run_dir.exists()


@pytest.mark.parametrize("provider", sorted(demo.SUPPORTED_PROVIDERS))
def test_profiles_bind_only_their_own_credential_and_no_builtin_tools(tmp_path, provider):
    run_dir = tmp_path / "run"
    manifest = demo.prepare(run_dir, SCENE, port=8767, provider=provider)
    all_tokens = [
        json.loads(path.read_text())["token"] for path in (run_dir / "credentials").glob("*.json")
    ]
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    assert len(set(all_tokens)) == len(all_tokens) == 5
    assert (run_dir / ".gitignore").read_text() == "*\n"
    assert "operator" not in manifest["profiles"]
    for actor, name in manifest["profiles"].items():
        text = (run_dir / "profiles" / f"{name}.md").read_text()
        profile = yaml.safe_load(text.split("---", 2)[1])
        validate(profile, json.loads(SCHEMA.read_text()))
        assert profile["provider"] == provider
        assert profile["skills"] == []
        cao = "@cao-mcp-server" if actor == "supervisor" else "@cao-mcp-server/send_message"
        assert set(profile["allowedTools"]) == {"@factory-sim", cao}
        assert not any(tool in profile["allowedTools"] for tool in ("*", "@builtin", "fs_*"))
        connection = profile["mcpServers"]["factory-sim"]
        assert connection["type"] == "stdio"
        assert connection["args"][-1] == str(run_dir / "credentials" / f"{actor}.json")
        assert all(token not in text for token in all_tokens)
        assert "operator.json" not in text
        assert stat.S_IMODE((run_dir / "credentials" / f"{actor}.json").stat().st_mode) == 0o600
        assert manifest["run_id"] in text


def test_each_manager_gets_the_profile_of_its_team_kind_and_its_own_scope(tmp_path):
    run_dir = tmp_path / "run"
    manifest = demo.prepare(run_dir, SCENE, port=8767, provider="claude_code")
    scene = json.loads(SCENE.read_text())
    actors = manifest["actors"]
    assert actors["supervisor"]["scopes"] == ["observe"]
    assert actors["operator"]["scopes"] == ["observe", "operate"]
    for zone, team in scene["teams"].items():
        actor = actors[f"manager_{zone}"]
        assert actor == {
            "scopes": ["observe", "act", "ship" if team["kind"] == "formation" else "line"],
            "role": team["kind"],
            "zone": zone,
            "team": team["label"],
        }
        source, instructions = demo.read_profile(demo.PROFILE_SOURCES[team["kind"]])
        name = manifest["profiles"][f"manager_{zone}"]
        assert name.startswith(source["name"] + "_")
        text = (run_dir / "profiles" / f"{name}.md").read_text()
        assert instructions in text
        bindings = json.loads(text.rsplit("```json\n", 1)[1].split("\n```", 1)[0])
        assert bindings["your_zone"] == zone
        assert bindings["your_team"] == team["label"]
        # A manager's bindings name only its own team, not another manager.
        assert set(bindings["teams"]) == {zone}
        assert bindings["teams"][zone]["manager_profile"] == name
    supervisor = (run_dir / "profiles" / f"{manifest['profiles']['supervisor']}.md").read_text()
    bindings = json.loads(supervisor.rsplit("```json\n", 1)[1].split("\n```", 1)[0])
    assert bindings["your_zone"] is None
    assert {zone: entry["team"] for zone, entry in bindings["teams"].items()} == {
        "west": "Team A",
        "middle": "Team B",
        "east": "Team C",
    }


def test_prepare_does_not_overwrite_an_existing_run(tmp_path):
    run_dir = tmp_path / "run"
    demo.prepare(run_dir, SCENE, port=8767, provider="copilot_cli")
    old = (run_dir / "run.json").read_bytes()
    with pytest.raises(FileExistsError):
        demo.prepare(run_dir, SCENE, port=8767, provider="copilot_cli")
    assert (run_dir / "run.json").read_bytes() == old


def test_server_cannot_restart_with_lost_command_history_and_old_credentials(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    demo.prepare(run_dir, SCENE, port=_free_port(), provider="copilot_cli")
    calls = []

    class Server:
        def run(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(demo, "make_server", lambda *args: Server())
    demo.serve(run_dir, allow_motion=False)
    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["host_origin_protection"] is True
    with pytest.raises(FileExistsError):
        demo.serve(run_dir, allow_motion=True)
    assert len(calls) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="Ctrl+C is SIGINT on POSIX")
@pytest.mark.parametrize("stop", ["SIGINT", "SIGTERM", "SIGHUP"])
def test_ctrl_c_stops_the_controller_cleanly(tmp_path, stop):
    # The README stops the controller with Ctrl+C. kill and a closed terminal window
    # (SIGTERM, SIGHUP) must stop it the same way: no traceback, exit 0, final state.
    run_dir = tmp_path / "run"
    port = _free_port()
    demo.prepare(run_dir, SCENE, port=port, provider="copilot_cli")
    process = subprocess.Popen(
        [sys.executable, str(EXAMPLE / "demo.py"), "serve", "--run-dir", str(run_dir)],
        cwd=EXAMPLE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        while True:
            assert process.poll() is None, process.communicate()[1]
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            assert time.monotonic() < deadline, "the controller did not start"
            time.sleep(0.2)
        process.send_signal(getattr(signal, stop))
        _, stderr = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert process.returncode == 0, stderr
    assert "Traceback" not in stderr
    assert json.loads((run_dir / "last-state.json").read_text())["run_id"]


def test_serve_without_frames_does_not_point_to_a_viewer(tmp_path, monkeypatch, caplog):
    run_dir = tmp_path / "run"
    demo.prepare(run_dir, SCENE, port=_free_port(), provider="copilot_cli")

    class Server:
        def run(self, **kwargs):
            raise KeyboardInterrupt

    class Recorder:
        def __init__(self, world, directory):
            pass

        def start(self):
            pass

        def stop(self):
            return []

    monkeypatch.setattr(demo, "make_server", lambda *args: Server())
    monkeypatch.setattr(demo, "Recorder", Recorder)
    with caplog.at_level("INFO", logger="factory"):
        demo.serve(run_dir, allow_motion=False, record=tmp_path / "frames")
    assert "Recorded no frames" in caplog.text
    assert "index.html" not in caplog.text
    port = json.loads((run_dir / "run.json").read_text())["url"].split(":")[2].split("/")[0]
    assert f"LIVE VIEW: open http://127.0.0.1:{port}/ in a browser" in caplog.text


def test_serve_refuses_a_busy_port_without_consuming_the_run(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    port = _free_port()
    demo.prepare(run_dir, SCENE, port=port, provider="copilot_cli")
    monkeypatch.setattr(demo, "make_server", lambda *args: pytest.fail("must not start"))
    with socket.socket() as other_controller:
        other_controller.bind(("127.0.0.1", port))
        other_controller.listen()
        with pytest.raises(OSError, match=f"port {port} is in use"):
            demo.serve(run_dir, allow_motion=True)
    assert not (run_dir / "started.json").exists()


def test_serve_refuses_an_unusable_frames_directory_without_consuming_the_run(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "run"
    demo.prepare(run_dir, SCENE, port=_free_port(), provider="copilot_cli")
    (tmp_path / "a-file").write_text("not a directory")
    monkeypatch.setattr(demo, "make_server", lambda *args: pytest.fail("must not start"))
    with pytest.raises(OSError):
        demo.serve(run_dir, allow_motion=True, record=tmp_path / "a-file" / "frames")
    assert not (run_dir / "started.json").exists()


def test_serve_refuses_a_scene_whose_routine_limit_exceeds_the_schema(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    demo.prepare(run_dir, SCENE, port=_free_port(), provider="copilot_cli")
    data = json.loads((run_dir / "scene.json").read_text())
    data["teams"]["east"]["max_routine_moves"] += 1
    (run_dir / "scene.json").write_text(json.dumps(data))
    monkeypatch.setattr(demo, "make_server", lambda *args: pytest.fail("must not start"))
    with pytest.raises(ValueError, match="max_routine_moves"):
        demo.serve(run_dir, allow_motion=True)
    assert not (run_dir / "started.json").exists()

    # A corrected scene can then run: the run directory was never consumed.
    data["teams"]["east"]["max_routine_moves"] -= 1
    (run_dir / "scene.json").write_text(json.dumps(data))

    class Server:
        def run(self, **kwargs):
            raise KeyboardInterrupt

    monkeypatch.setattr(demo, "make_server", lambda *args: Server())
    demo.serve(run_dir, allow_motion=True)
    assert (run_dir / "started.json").exists()


def test_glfw_on_macos_turns_recording_off_instead_of_crashing(tmp_path, monkeypatch, caplog):
    run_dir = tmp_path / "run"
    demo.prepare(run_dir, SCENE, port=_free_port(), provider="copilot_cli")

    class Server:
        def run(self, **kwargs):
            raise KeyboardInterrupt

    monkeypatch.setattr(demo.sys, "platform", "darwin")
    monkeypatch.setenv("MUJOCO_GL", "glfw")
    monkeypatch.setattr(demo, "make_server", lambda *args: Server())
    monkeypatch.setattr(demo, "Recorder", lambda *args: pytest.fail("must not render"))
    with caplog.at_level("INFO", logger="factory"):
        demo.serve(run_dir, allow_motion=False, record=tmp_path / "frames")
    assert "Recording is off: MUJOCO_GL=glfw" in caplog.text
    assert not (tmp_path / "frames").exists()


def test_prepare_refuses_a_scene_whose_routine_limit_exceeds_the_schema(tmp_path):
    data = json.loads(SCENE.read_text())
    data["teams"]["east"]["max_routine_moves"] += 1
    scene = tmp_path / "oversized.json"
    scene.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="max_routine_moves"):
        demo.prepare(tmp_path / "run", scene, port=8767, provider="copilot_cli")
    assert not (tmp_path / "run").exists()


def test_a_refused_scene_or_profile_leaves_no_run_directory(tmp_path, monkeypatch):
    data = json.loads(SCENE.read_text())
    data["teams"]["a-b"] = data["teams"].pop("west")
    data["teams"]["a_b"] = data["teams"].pop("middle")
    scene = tmp_path / "clash.json"
    scene.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        demo.prepare(tmp_path / "clash", scene, port=8767, provider="copilot_cli")
    assert not (tmp_path / "clash").exists()

    broken = tmp_path / "broken.md"
    broken.write_text("no frontmatter")
    monkeypatch.setitem(demo.PROFILE_SOURCES, "formation", broken)
    with pytest.raises(ValueError, match="frontmatter"):
        demo.prepare(tmp_path / "broken", SCENE, port=8767, provider="copilot_cli")
    assert not (tmp_path / "broken").exists()


def test_manager_variables_must_not_clash():
    scene = demo.Scene.model_validate_json(SCENE.read_text())
    assert demo.zone_variables(scene) == {
        "MANAGER_WEST": "west",
        "MANAGER_MIDDLE": "middle",
        "MANAGER_EAST": "east",
    }
    data = json.loads(SCENE.read_text())
    for old, new in (("west", "a-b"), ("middle", "a_b")):
        data["zones"][new] = data["zones"].pop(old)
        data["teams"][new] = data["teams"].pop(old)
        for robot in data["robots"].values():
            robot["zone"] = new if robot["zone"] == old else robot["zone"]
        for place in data["places"].values():
            place["zones"] = [new if zone == old else zone for zone in place["zones"]]
    clash = demo.Scene.model_validate(data)
    with pytest.raises(ValueError, match="both map to MANAGER_A_B"):
        demo.zone_variables(clash)


def test_run_env_names_the_profile_of_each_agent(tmp_path):
    # The README sources run.env and installs each agent with its own command.
    run_dir = tmp_path / "run"
    manifest = demo.prepare(run_dir, SCENE, port=8767, provider="copilot_cli")
    assert stat.S_IMODE((run_dir / "run.env").stat().st_mode) == 0o600
    zones = json.loads(SCENE.read_text())["teams"]
    names = ["RUN_ID", "SESSION", "SUPERVISOR"] + [f"MANAGER_{zone.upper()}" for zone in zones]
    script = f'set -eu; . "{run_dir}/run.env"; ' + "; ".join(
        f'echo "{name}=${name}"' for name in names
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    profiles = manifest["profiles"]
    assert values["RUN_ID"] == manifest["run_id"]
    assert values["SESSION"] == f"cao-factory-{manifest['run_id']}"
    assert values["SUPERVISOR"] == profiles["supervisor"]
    for zone in zones:
        assert values[f"MANAGER_{zone.upper()}"] == profiles[f"manager_{zone}"]
        assert (run_dir / "profiles" / f"{values[f'MANAGER_{zone.upper()}']}.md").is_file()


@pytest.mark.parametrize("role", sorted(demo.PROFILE_SOURCES))
def test_committed_profiles_are_valid_cao_profiles(role):
    source = demo.PROFILE_SOURCES[role]
    profile, instructions = demo.read_profile(source)
    validate(profile, json.loads(SCHEMA.read_text()))
    assert source.parent == EXAMPLE
    assert profile["name"] == source.stem
    assert "provider" not in profile
    assert instructions
    assert set(profile["mcpServers"]) == {"factory-sim", "cao-mcp-server"}
    # Only the supervisor holds the whole CAO server. A manager holds only
    # send_message, so CAO refuses it assign and handoff (review of #1108, P2-1).
    cao = "@cao-mcp-server" if role == "supervisor" else "@cao-mcp-server/send_message"
    assert profile["allowedTools"] == ["@factory-sim", cao]
    # Only the supervisor delegates; a manager uses CAO only to report back.
    delegates = "**assign**(" in instructions
    assert delegates == (role == "supervisor")
    if role != "supervisor":
        assert "CAO refuses\n`assign`, `handoff`" in instructions


@pytest.mark.parametrize("provider", sorted(demo.SUPPORTED_PROVIDERS))
def test_run_copies_are_the_committed_profiles_with_run_values(tmp_path, provider):
    run_dir = tmp_path / "run"
    manifest = demo.prepare(run_dir, SCENE, port=8767, provider=provider)
    for actor, name in manifest["profiles"].items():
        role = manifest["actors"][actor]["role"]
        source, instructions = demo.read_profile(demo.PROFILE_SOURCES[role])
        path = run_dir / "profiles" / f"{name}.md"
        run_copy, body = demo.read_profile(path)
        assert name.startswith(source["name"] + "_")
        assert run_copy["description"].startswith(source["description"])
        assert run_copy["provider"] == provider
        for key in ("skills", "allowedTools"):
            assert run_copy[key] == source[key]
        assert set(run_copy["mcpServers"]) == set(source["mcpServers"])
        for server, entry in source["mcpServers"].items():
            if server != "factory-sim":
                assert run_copy["mcpServers"][server] == entry
        assert run_copy["mcpServers"]["factory-sim"]["command"] == sys.executable
        assert body.startswith(instructions)
        assert "<run-dir>" not in path.read_text()


def test_read_profile_refuses_a_file_without_frontmatter(tmp_path):
    path = tmp_path / "broken.md"
    path.write_text("No frontmatter here.\n")
    with pytest.raises(ValueError, match="frontmatter"):
        demo.read_profile(path)


def test_provider_lists_match_cao():
    enum = (CAO_SOURCE / "models/provider.py").read_text()
    providers = set(re.findall(r'^\s+[A-Z_]+ = "([a-z_]+)"$', enum, flags=re.M)) - {"mock_cli"}
    assert demo.CAO_PROVIDERS == providers
    levels = (CAO_SOURCE / "utils/enforcement.py").read_text()
    assert demo.NATIVE_ENFORCEMENT == set(
        re.findall(r'^\s+"([a-z_]+)": NATIVE', levels, flags=re.M)
    )


def test_prepare_refuses_an_unknown_provider_before_creating_a_run(tmp_path):
    run_dir = tmp_path / "run"
    with pytest.raises(ValueError, match="unknown CAO provider"):
        demo.prepare(run_dir, SCENE, port=8767, provider="not_a_provider")
    assert not run_dir.exists()


@pytest.mark.parametrize(
    "provider", ["antigravity_cli", "cursor_cli", "grok_cli", "hermes", "opencode_cli"]
)
def test_a_provider_without_per_agent_mcp_servers_is_refused(
    tmp_path, monkeypatch, capsys, provider
):
    assert demo.SUPPORTED_PROVIDERS == demo.CAO_PROVIDERS - {
        "antigravity_cli",
        "cursor_cli",
        "grok_cli",
        "hermes",
        "opencode_cli",
    }
    run_dir = tmp_path / "run"
    with pytest.raises(ValueError, match=f"{provider} is not supported"):
        demo.prepare(run_dir, SCENE, port=8767, provider=provider)
    assert not run_dir.exists()
    monkeypatch.setattr(
        sys,
        "argv",
        ["demo.py", "prepare", "--run-dir", str(run_dir), "--request=x", f"--provider={provider}"],
    )
    with pytest.raises(SystemExit) as exit_info:
        demo.main()
    assert exit_info.value.code == 2
    assert f"--provider {provider} is not supported" in capsys.readouterr().err
    assert not run_dir.exists()


@pytest.mark.parametrize("provider", sorted(demo.SUPPORTED_PROVIDERS))
def test_prepare_warns_only_when_the_provider_does_not_enforce_the_allowlist(
    tmp_path, monkeypatch, capsys, provider
):
    run_dir = tmp_path / "run"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "demo.py",
            "prepare",
            "--run-dir",
            str(run_dir),
            "--request=check",
            "--provider",
            provider,
        ],
    )
    demo.main()
    captured = capsys.readouterr()
    warned = "does not enforce the tool allowlist" in captured.err
    assert warned == (provider not in demo.NATIVE_ENFORCEMENT)
    assert "warning" not in captured.out
