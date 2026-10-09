from __future__ import annotations

import json
import shlex
import stat
import sys
from pathlib import Path

import demo
import pytest
import yaml
from jsonschema import validate

EXAMPLE = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "transport_request",
    [
        "Move tote from stock to etch and have an independent checker verify delivery.",
        "Return sample-tray from rack to inspection without changing its custody early.",
        '--inspect "operator\'s tote"; preserve $STATUS\nwithout moving it.',
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
@pytest.mark.parametrize("provider", ["copilot_cli", "claude_code"])
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
        "fixtures": ["tote_clamp"],
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


def test_long_zone_identifiers_do_not_overflow_cao_profile_names(tmp_path):
    data = json.loads((EXAMPLE / "site.json").read_text())
    zone_name = "z" * 64
    data["zones"][zone_name] = data["zones"].pop("west")
    for location in data["locations"].values():
        location["zones"] = [zone_name if zone == "west" else zone for zone in location["zones"]]
    data["robots"]["cart-west"]["zone"] = zone_name
    data["payloads"]["tote"]["owner"] = zone_name
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


@pytest.mark.parametrize("provider", ["copilot_cli", "claude_code"])
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
