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


@pytest.mark.parametrize(
    "transport_request",
    [
        "Move parcel from stock to etch and have an independent checker verify delivery.",
        "Return sample-tray from rack to inspection without changing its custody early.",
        '--inspect "operator\'s parcel"; preserve $STATUS\nwithout moving it.',
    ],
)
def test_prepare_prints_a_nonblocking_launch_with_the_request(
    tmp_path, monkeypatch, capsys, transport_request
):
    run_dir = tmp_path / "run"
    monkeypatch.setattr(
        sys,
        "argv",
        ["demo.py", "prepare", "--run-dir", str(run_dir), f"--request={transport_request}"],
    )
    demo.main()
    commands = shlex.split(capsys.readouterr().out)
    launch = commands[commands.index("launch") - 1 :]
    assert launch[:2] == ["cao", "launch"]
    assert {"--headless", "--async", "--auto-approve"} <= set(launch)
    assert "--yolo" not in launch
    run_id = json.loads((run_dir / "run.json").read_text())["run_id"]
    assert launch[launch.index("--session-name") + 1] == f"cao-transport-{run_id}"
    assert launch[-2:] == ["--", transport_request]


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


@pytest.mark.parametrize("scene_file", ["site.json", "return-site.json"])
@pytest.mark.parametrize("provider", sorted(demo.SUPPORTED_PROVIDERS))
def test_profiles_bind_only_their_own_credential_and_no_builtin_tools(
    tmp_path, scene_file, provider
):
    run_dir = tmp_path / "run"
    manifest = demo.prepare(run_dir, EXAMPLE / scene_file, port=8766, provider=provider)
    all_tokens = [
        json.loads(path.read_text())["token"] for path in (run_dir / "credentials").glob("*.json")
    ]
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    assert len(set(all_tokens)) == len(all_tokens)
    assert (run_dir / ".gitignore").read_text() == "*\n"
    assert "operator" not in manifest["profiles"]
    for actor, name in manifest["profiles"].items():
        text = (run_dir / "profiles" / f"{name}.md").read_text()
        profile = yaml.safe_load(text.split("---", 2)[1])
        schema = EXAMPLE.parents[2] / "src/cli_agent_orchestrator/schemas/agent_profile.schema.json"
        validate(profile, json.loads(schema.read_text()))
        assert profile["provider"] == provider
        assert profile["skills"] == []
        assert set(profile["allowedTools"]) == (
            {"@transport-sim", "@cao-mcp-server"} if actor == "supervisor" else {"@transport-sim"}
        )
        connection = profile["mcpServers"]["transport-sim"]
        assert connection["type"] == "stdio"
        assert connection["args"][-1] == str(run_dir / "credentials" / f"{actor}.json")
        assert all(token not in text for token in all_tokens)
        assert "operator.json" not in text
        assert stat.S_IMODE((run_dir / "credentials" / f"{actor}.json").stat().st_mode) == 0o600
        assert manifest["run_id"] in text


def test_prepare_does_not_overwrite_an_existing_run(tmp_path):
    run_dir = tmp_path / "run"
    demo.prepare(run_dir, EXAMPLE / "site.json", port=8766, provider="copilot_cli")
    old = (run_dir / "run.json").read_bytes()
    with pytest.raises(FileExistsError):
        demo.prepare(run_dir, EXAMPLE / "site.json", port=8766, provider="copilot_cli")
    assert (run_dir / "run.json").read_bytes() == old


def test_server_cannot_restart_with_lost_command_history_and_old_credentials(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    demo.prepare(run_dir, EXAMPLE / "site.json", port=8766, provider="copilot_cli")
    calls = []

    class Server:
        def run(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(demo, "make_server", lambda *args: Server())
    demo.serve(run_dir, allow_motion=False)
    assert calls[0]["host"] == "127.0.0.1"
    with pytest.raises(FileExistsError):
        demo.serve(run_dir, allow_motion=True)
    assert len(calls) == 1


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.skipif(sys.platform == "win32", reason="Ctrl+C is SIGINT on POSIX")
def test_ctrl_c_stops_the_controller_cleanly(tmp_path):
    # The README stops the controller with Ctrl+C: no traceback, exit 0, final state.
    run_dir = tmp_path / "run"
    port = _free_port()
    demo.prepare(run_dir, EXAMPLE / "site.json", port=port, provider="copilot_cli")
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
        process.send_signal(signal.SIGINT)
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
    demo.prepare(run_dir, EXAMPLE / "site.json", port=8766, provider="copilot_cli")

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
    with caplog.at_level("INFO", logger="transport"):
        demo.serve(run_dir, allow_motion=False, record=tmp_path / "frames")
    assert "Recorded no frames" in caplog.text
    assert "index.html" not in caplog.text


def test_additional_zones_get_the_same_shared_operator_prompt(tmp_path):
    data = json.loads((EXAMPLE / "site.json").read_text())
    data["zones"]["north"] = {"bounds": [0, 1, 3, 3]}
    data["locations"]["north-dock"] = {"xy": [2, 1], "zones": ["east", "north"], "handoff": True}
    data["locations"]["warehouse"] = {"xy": [2, 2], "zones": ["north"]}
    data["robots"]["cart-east"]["locations"].append("north-dock")
    data["robots"]["cart-north"] = {
        "zone": "north",
        "at": "north-dock",
        "locations": ["north-dock", "warehouse"],
        "payload_kg": 4,
        "fixtures": ["parcel_clamp"],
        "speed_m_s": 1,
    }
    scene_file = tmp_path / "three-zones.json"
    scene_file.write_text(json.dumps(data))
    run_dir = tmp_path / "run"
    manifest = demo.prepare(run_dir, scene_file, port=8766, provider="copilot_cli")
    assert len(manifest["profiles"]) == len(data["zones"]) + 2
    _, shared = demo.read_profile(EXAMPLE / "transport_zone_worker.md")
    for zone in data["zones"]:
        name = manifest["profiles"][f"zone_{zone}"]
        assert shared in (run_dir / "profiles" / f"{name}.md").read_text()


@pytest.mark.parametrize("scene_name", ["site.json", "return-site.json"])
def test_run_env_names_the_profile_of_each_agent(tmp_path, scene_name):
    # The README sources run.env and installs each agent with its own command.
    run_dir = tmp_path / "run"
    manifest = demo.prepare(run_dir, EXAMPLE / scene_name, port=8766, provider="copilot_cli")
    assert stat.S_IMODE((run_dir / "run.env").stat().st_mode) == 0o600
    names = ["RUN_ID", "SESSION", "SUPERVISOR", "CHECKER"] + [
        f"ZONE_{zone.upper()}" for zone in json.loads((EXAMPLE / scene_name).read_text())["zones"]
    ]
    script = f'set -eu; . "{run_dir}/run.env"; ' + "; ".join(
        f'echo "{name}=${name}"' for name in names
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    profiles = manifest["profiles"]
    assert values["RUN_ID"] == manifest["run_id"]
    assert values["SESSION"] == f"cao-transport-{manifest['run_id']}"
    assert values["SUPERVISOR"] == profiles["supervisor"]
    assert values["CHECKER"] == profiles["checker"]
    zones = json.loads((EXAMPLE / scene_name).read_text())["zones"]
    for zone in zones:
        assert values[f"ZONE_{zone.upper()}"] == profiles[f"zone_{zone}"]
        assert (run_dir / "profiles" / f"{values[f'ZONE_{zone.upper()}']}.md").is_file()


def test_long_zone_identifiers_do_not_overflow_cao_profile_names(tmp_path):
    data = json.loads((EXAMPLE / "site.json").read_text())
    zone_name = "z" * 64
    data["zones"][zone_name] = data["zones"].pop("west")
    for location in data["locations"].values():
        location["zones"] = [zone_name if zone == "west" else zone for zone in location["zones"]]
    data["robots"]["cart-west"]["zone"] = zone_name
    data["payloads"]["parcel"]["owner"] = zone_name
    scene = tmp_path / "long-zone.json"
    scene.write_text(json.dumps(data))
    run_dir = tmp_path / "run"
    manifest = demo.prepare(run_dir, scene, port=8766, provider="copilot_cli")
    schema = json.loads(
        (
            EXAMPLE.parents[2] / "src/cli_agent_orchestrator/schemas/agent_profile.schema.json"
        ).read_text()
    )
    for name in manifest["profiles"].values():
        text = (run_dir / "profiles" / f"{name}.md").read_text()
        validate(yaml.safe_load(text.split("---", 2)[1]), schema)


SCHEMA = EXAMPLE.parents[2] / "src/cli_agent_orchestrator/schemas/agent_profile.schema.json"


@pytest.mark.parametrize("role", sorted(demo.PROFILE_SOURCES))
def test_committed_profiles_are_valid_cao_profiles(role):
    source = demo.PROFILE_SOURCES[role]
    profile, instructions = demo.read_profile(source)
    validate(profile, json.loads(SCHEMA.read_text()))
    assert source.parent == EXAMPLE
    assert profile["name"] == source.stem
    assert "provider" not in profile
    assert instructions
    assert "transport-sim" in profile["mcpServers"]
    assert ("@cao-mcp-server" in profile["allowedTools"]) == (role == "supervisor")
    assert ("cao-mcp-server" in profile["mcpServers"]) == (role == "supervisor")


@pytest.mark.parametrize("provider", sorted(demo.SUPPORTED_PROVIDERS))
def test_run_copies_are_the_committed_profiles_with_run_values(tmp_path, provider):
    run_dir = tmp_path / "run"
    manifest = demo.prepare(run_dir, EXAMPLE / "site.json", port=8766, provider=provider)
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
            if server != "transport-sim":
                assert run_copy["mcpServers"][server] == entry
        assert run_copy["mcpServers"]["transport-sim"]["command"] == sys.executable
        assert body.startswith(instructions)
        assert "<run-dir>" not in path.read_text()


def test_read_profile_refuses_a_file_without_frontmatter(tmp_path):
    path = tmp_path / "broken.md"
    path.write_text("No frontmatter here.\n")
    with pytest.raises(ValueError, match="frontmatter"):
        demo.read_profile(path)


CAO_SOURCE = EXAMPLE.parents[2] / "src/cli_agent_orchestrator"


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
        demo.prepare(run_dir, EXAMPLE / "site.json", port=8766, provider="not_a_provider")
    assert not run_dir.exists()


@pytest.mark.parametrize("provider", ["cursor_cli", "hermes", "opencode_cli"])
def test_a_provider_without_per_agent_mcp_servers_is_refused(
    tmp_path, monkeypatch, capsys, provider
):
    if provider == "cursor_cli":
        # The Cursor provider leaves the profile body out of a direct launch.
        source = (CAO_SOURCE / "providers/cursor_cli.py").read_text()
        assert "System prompt injection is intentionally omitted" in source
    elif provider == "opencode_cli":
        # cao install writes the servers of every OpenCode profile into one shared
        # configuration keyed by server name; the last install replaces the others.
        source = (CAO_SOURCE / "utils/opencode_config.py").read_text()
        assert 'data.setdefault("mcp", {})[name] = config' in source
    else:
        # The Hermes provider never reads the mcpServers of the CAO profile.
        assert "mcpServers" not in (CAO_SOURCE / "providers/hermes.py").read_text()
    assert demo.SUPPORTED_PROVIDERS == demo.CAO_PROVIDERS - {"cursor_cli", "hermes", "opencode_cli"}
    run_dir = tmp_path / "run"
    with pytest.raises(ValueError, match=f"{provider} is not supported"):
        demo.prepare(run_dir, EXAMPLE / "site.json", port=8766, provider=provider)
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
