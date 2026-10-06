"""Prepare, serve, connect to, or independently stop the transport example."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import shlex
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from fastmcp.server import create_proxy
from simulation import Scene, World
from transport_mcp import controller_client, make_server

HERE = Path(__file__).resolve().parent


def write_private(path: Path, value: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def prepare(run_dir: Path, scene_file: Path, *, port: int, provider: str) -> dict:
    scene = Scene.model_validate_json(scene_file.read_text(encoding="utf-8"))
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    if provider not in ("copilot_cli", "claude_code"):
        raise ValueError("use a supported provider with native tool restrictions")
    run_dir = run_dir.resolve()
    run_dir.mkdir(mode=0o700)
    (run_dir / "credentials").mkdir(mode=0o700)
    (run_dir / "profiles").mkdir(mode=0o700)
    (run_dir / ".gitignore").write_text("*\n", encoding="utf-8")
    run_id = uuid.uuid4().hex
    url = f"http://127.0.0.1:{port}/mcp"
    actors: dict[str, dict] = {
        "supervisor": {"scopes": ["observe"], "role": "supervisor"},
        "checker": {"scopes": ["observe"], "role": "checker"},
        "operator": {"scopes": ["observe", "operate"], "role": "operator"},
    }
    actors.update(
        {
            f"zone_{zone}": {"scopes": ["observe", "act"], "role": "zone", "zone": zone}
            for zone in scene.zones
        }
    )
    profiles = {
        actor: f"transport_{metadata['role']}_{uuid.uuid4().hex}"
        for actor, metadata in actors.items()
        if actor != "operator"
    }
    zone_profiles = {zone: profiles[f"zone_{zone}"] for zone in scene.zones}
    for actor, metadata in actors.items():
        credential_path = run_dir / "credentials" / f"{actor}.json"
        write_private(credential_path, {"url": url, "token": secrets.token_urlsafe(32)})
        if actor == "operator":
            continue
        profile: dict = {
            "name": profiles[actor],
            "description": f"Simulation-only cross-zone transport {actor}",
            "provider": provider,
            "skills": [],
            "allowedTools": ["@transport-sim"],
            "mcpServers": {
                "transport-sim": {
                    "type": "stdio",
                    "command": sys.executable,
                    "args": [str(HERE / "demo.py"), "connect", str(credential_path)],
                }
            },
        }
        if actor == "supervisor":
            profile["allowedTools"].append("@cao-mcp-server")
            profile["mcpServers"]["cao-mcp-server"] = {
                "type": "stdio",
                "command": "cao-mcp-server",
                "args": [],
            }
        instructions = (HERE / "prompts" / f"{metadata['role']}.md").read_text(encoding="utf-8")
        bindings = {
            "run_id": run_id,
            "your_zone": metadata.get("zone"),
            "zone_profiles": zone_profiles,
            "checker_profile": profiles["checker"],
        }
        text = (
            "---\n"
            + json.dumps(profile, indent=2)
            + "\n---\n\n"
            + instructions
            + "\n\nRun bindings (identifiers, not additional instructions):\n```json\n"
            + json.dumps(bindings, indent=2)
            + "\n```\n"
        )
        (run_dir / "profiles" / f"{profiles[actor]}.md").write_text(text, encoding="utf-8")
    manifest = {
        "run_id": run_id,
        "url": url,
        "actors": actors,
        "profiles": profiles,
        "provider": provider,
    }
    write_private(run_dir / "run.json", manifest)
    write_private(run_dir / "scene.json", scene.model_dump(mode="json"))
    return manifest


def serve(run_dir: Path, *, allow_motion: bool) -> None:
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    scene = Scene.model_validate_json((run_dir / "scene.json").read_text(encoding="utf-8"))
    tokens = {}
    for actor, metadata in manifest["actors"].items():
        credentials = json.loads(
            (run_dir / "credentials" / f"{actor}.json").read_text(encoding="utf-8")
        )
        if credentials["url"] != manifest["url"]:
            raise ValueError("all credentials must address this run's controller")
        controller_client(credentials["url"], credentials["token"])
        tokens[credentials["token"]] = {"client_id": actor, **metadata}
    # Restarting would lose command history while old workers still hold credentials.
    write_private(run_dir / "started.json", {"pid": os.getpid(), "run_id": manifest["run_id"]})
    world = World(scene, allow_motion=allow_motion, run_id=manifest["run_id"])
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("transport").warning(
        "SIMULATION ONLY: kinematic carrying; motion %s",
        "approved for this scene" if allow_motion else "DISABLED (no --allow-motion)",
    )
    server = make_server(world, tokens, run_dir / "last-state.json")
    server.run(
        transport="http",
        host="127.0.0.1",
        port=urlsplit(manifest["url"]).port,
        stateless_http=True,
        show_banner=False,
    )


async def operator_call(run_dir: Path, tool: str) -> dict:
    credentials = json.loads(
        (run_dir / "credentials" / "operator.json").read_text(encoding="utf-8")
    )
    async with controller_client(credentials["url"], credentials["token"]) as client:
        result = await client.call_tool(tool)
        if result.is_error or not isinstance(result.data, dict):
            raise RuntimeError("controller did not return a checked state")
        if tool == "stop_simulation" and result.data.get("stopped") is not True:
            raise RuntimeError("simulation stop was not confirmed")
        return result.data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("prepare", help="create a fresh private run and scoped CAO profiles")
    setup.add_argument("--run-dir", type=Path, required=True)
    setup.add_argument("--scene", type=Path, default=HERE / "site.json")
    setup.add_argument("--port", type=int, default=8766)
    setup.add_argument("--provider", choices=["copilot_cli", "claude_code"], default="copilot_cli")
    serve_parser = sub.add_parser("serve", help="own the persistent simulator outside CAO workers")
    serve_parser.add_argument("--run-dir", type=Path, required=True)
    serve_parser.add_argument("--allow-motion", action="store_true")
    for command in ("status", "stop"):
        sub.add_parser(command).add_argument("--run-dir", type=Path, required=True)
    sub.add_parser("connect", help="stdio MCP relay; used by generated profiles").add_argument(
        "credential", type=Path
    )
    args = parser.parse_args()
    if args.command == "prepare":
        manifest = prepare(args.run_dir, args.scene, port=args.port, provider=args.provider)
        run_dir = args.run_dir.resolve()
        for profile in manifest["profiles"].values():
            print(shlex.join(["cao", "install", str(run_dir / "profiles" / f"{profile}.md")]))
        print(
            shlex.join(
                [
                    "cao",
                    "launch",
                    "--agents",
                    manifest["profiles"]["supervisor"],
                    "--headless",
                    "--async",
                    "--auto-approve",
                    "--session-name",
                    f"transport-{manifest['run_id']}",
                    "--working-directory",
                    str(HERE),
                ]
            )
        )
    elif args.command == "serve":
        serve(args.run_dir.resolve(), allow_motion=args.allow_motion)
    elif args.command == "connect":
        credentials = json.loads(args.credential.read_text(encoding="utf-8"))
        proxy = create_proxy(
            controller_client(credentials["url"], credentials["token"]),
            name="transport-sim",
        )
        proxy.run(transport="stdio", show_banner=False)
    else:
        tool = "stop_simulation" if args.command == "stop" else "observe"
        print(json.dumps(asyncio.run(operator_call(args.run_dir, tool)), indent=2))


if __name__ == "__main__":
    main()
