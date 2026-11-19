"""app.json, the root package.json and the icon are consistent with the folder (workstream C3)."""

from __future__ import annotations

import json
import re
import struct
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((ROOT / "app.json").read_text(encoding="utf-8"))


def walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from walk_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk_strings(v)


def test_required_fields_and_semver(manifest: dict) -> None:
    for key in ("name", "version", "displayName", "description"):
        assert manifest.get(key), key
    assert manifest["name"] == "cao-shelf"
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", manifest["name"])
    assert SEMVER.match(manifest["version"])
    assert SEMVER.match(manifest["minKiroCrewVersion"])
    assert manifest["license"] == "Apache-2.0"


def test_no_parent_paths_anywhere(manifest: dict) -> None:
    for text in walk_strings(manifest):
        assert ".." not in text, text
        assert not text.startswith("/") or text.startswith(
            ("/apps/cao-shelf", "/health", "/api/chat")
        ), text


def test_permissions_are_the_shelf_api_the_chat_api_and_no_network(manifest: dict) -> None:
    perms = manifest["permissions"]
    assert perms["network"] is False
    assert perms["api"] == [
        "/apps/cao-shelf/api",
        "/apps/cao-shelf/api/*",
        "/api/chat",
        "/api/chat/*",
    ]
    # Kiro Crew refuses an App's POST /api/chat/mode (each relay chat's trust mode) without it; the
    # App approves no tool request with it
    assert perms["sessionApproval"] is True
    # a non-empty mcpTools makes Kiro Crew write every other host-managed tool into the relay agent's
    # managedToolPolicy.exclude, which the managed servers enforce per session
    # (apps/bridges_agents.py, _enforce_declared_mcp_tools): of kirocrew-core only `wait` is reachable
    assert perms["mcpTools"] == ["wait"]
    assert set(perms) == {"api", "network", "sessionApproval", "mcpTools"}


def test_version_is_0_2_0_everywhere(manifest: dict) -> None:
    assert manifest["version"] == "0.2.0"
    init = (ROOT / "backend" / "cao_shelf" / "__init__.py").read_text(encoding="utf-8")
    assert '__version__ = "0.2.0"' in init


def test_the_agent_is_listed_and_names_only_the_four_ops_tools_and_the_wait_tool(
    manifest: dict,
) -> None:
    assert manifest["agents"] == ["agents/cao-remote-control.json"]
    agent = json.loads((ROOT / manifest["agents"][0]).read_text(encoding="utf-8"))
    assert agent["name"] == "cao-remote-control"
    four = [
        f"@cao-shelf:ops/{t}"
        for t in (
            "send_session_message",
            "get_terminal_status",
            "read_session_output",
            "get_session_info",
        )
    ]
    wanted = four + ["@kirocrew-core/wait"]
    assert agent["tools"] == wanted and agent["allowedTools"] == wanted
    assert agent["includeMcpJson"] is False
    for entry in agent["tools"] + agent["allowedTools"]:
        assert entry.startswith("@cao-shelf:ops/") or entry == "@kirocrew-core/wait", entry
    granted = json.dumps([agent["tools"], agent["allowedTools"], agent.get("prompt", "")])
    for forbidden in ("launch_session", "shutdown_session", "install_profile"):
        assert forbidden not in granted, forbidden


# cao-ops-mcp as shipped (CAO main, 2026-11): every tool besides the four the relay uses
OTHER_OPS_TOOLS = sorted(
    [
        "decisions_list",
        "decisions_off",
        "decisions_purge",
        "decisions_set_exclusions",
        "decisions_set_point",
        "decisions_set_tier",
        "decisions_status",
        "get_profile_details",
        "get_terminal_output",
        "install_profile",
        "launch_session",
        "list_profiles",
        "list_sessions",
        "shutdown_session",
    ]
)
FOUR_OPS_TOOLS = ["send_session_message", "get_terminal_status", "read_session_output", "get_session_info"]


def test_the_agent_turns_every_other_ops_tool_off(manifest: dict) -> None:
    """On Kiro Crew 0.12.2's Claude backend an `@cao-shelf:ops/<tool>` grant mounts the whole server
    (the e2e rig saw all 18 tools offered); the agent's own `disabledTools` for that server becomes
    Claude `permissions.deny` and narrows it to the four. The entry is the only place the server is
    declared, so it is a complete spec (kiro-cli's strict parser rejects an entry without a command)."""
    agent = json.loads((ROOT / manifest["agents"][0]).read_text(encoding="utf-8"))
    assert list(agent["mcpServers"]) == ["cao-shelf:ops"]
    entry = agent["mcpServers"]["cao-shelf:ops"]
    assert {k: v for k, v in entry.items() if k != "disabledTools"} == {
        "command": "cao-ops-mcp-server",
        "args": [],
        "env": {"CAO_API_HOST": "127.0.0.1", "CAO_API_PORT": "9889"},
    }
    assert entry["disabledTools"] == OTHER_OPS_TOOLS
    assert not set(entry["disabledTools"]) & set(FOUR_OPS_TOOLS)


def test_the_disabled_list_matches_the_cao_ops_mcp_in_this_checkout() -> None:
    server = pytest.importorskip("cli_agent_orchestrator.ops_mcp_server.server")
    import asyncio

    names = sorted(t.name for t in asyncio.run(server.mcp.list_tools()))
    assert names == sorted(OTHER_OPS_TOOLS + FOUR_OPS_TOOLS)


def test_the_manifest_declares_no_mcp_server(manifest: dict) -> None:
    """Kiro Crew 0.12.2 writes every manifest `mcpServers` entry into the host's default agent config
    (`apps/bridges_mcp_config.py`, `kirocrew.json`), which would put the whole cao-ops-mcp, launch and
    shutdown included, into the owner's ordinary chats. The relay agent's own entry is enough: Kiro
    Crew keeps an App agent's own `mcpServers` (`apps/bridges_agents.py`)."""
    assert "mcpServers" not in manifest
    for forbidden in ("launch_session", "shutdown_session", "install_profile"):
        assert forbidden not in json.dumps(manifest), forbidden


def test_icon_exists_512_opaque_png(manifest: dict) -> None:
    icon = ROOT / manifest["iconPath"]
    data = icon.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height, depth, color = struct.unpack(">IIBB", data[16:26])
    assert (width, height) == (512, 512)
    assert color in (0, 2), "colour type must have no alpha channel (opaque)"
    assert b"tRNS" not in data
    # the pixel data decodes (CRC and zlib stream are intact)
    pos, idat = 8, b""
    while pos < len(data):
        length, tag = struct.unpack(">I4s", data[pos : pos + 8])
        body = data[pos + 8 : pos + 8 + length]
        crc = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])[0]
        assert zlib.crc32(tag + body) & 0xFFFFFFFF == crc
        if tag == b"IDAT":
            idat += body
        pos += 12 + length
    assert len(zlib.decompress(idat)) == 512 * (1 + 512 * 3)


def test_root_package_json_matches_and_builds_the_ui(manifest: dict) -> None:
    pkg = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert pkg["version"] == manifest["version"]
    assert pkg["license"] == "Apache-2.0"
    assert "ui" in pkg["scripts"]["build"] and "build" in pkg["scripts"]["build"]
    assert pkg.get("private") is True


def test_backend_entry_point_exists(manifest: dict) -> None:
    """Needs workstream A: backend/server.py."""
    entry = ROOT / manifest["backend"]["entryPoint"]
    assert entry.is_file(), f"missing {entry.relative_to(ROOT)} (workstream A)"
    assert manifest["backend"]["healthCheck"] == "/health"


def test_ui_entry_exists_under_ui_dir(manifest: dict) -> None:
    """Needs workstream B: ui/bundle/index.mjs (built bundle, committed)."""
    entry = ROOT / "ui" / manifest["ui"]["entry"]
    assert entry.is_file(), f"missing {entry.relative_to(ROOT)} (workstream B)"
    assert manifest["ui"]["pages"][0]["route"] == "/apps/cao-shelf"
