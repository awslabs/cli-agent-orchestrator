"""Prepare, serve, connect to, rehearse, or stop the policy-driven cell example."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import os
import secrets
import shlex
import signal
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from cell import Cell, FakeBackend, Refused, Scene
from cell_mcp import allowed_hosts, controller_client, make_server
from fastmcp.server import create_proxy

HERE = Path(__file__).resolve().parent
PROFILE_SOURCES = {
    "supervisor": HERE / "cell_supervisor.md",
    "humanoid": HERE / "humanoid_operator.md",
    "arm": HERE / "arm_operator.md",
    "checker": HERE / "cell_checker.md",
}
# The same provider rules as examples/robotics/cross-zone-transport: each agent
# needs its own MCP server with its own credential, which these providers cannot
# give it.
CAO_PROVIDERS = frozenset(
    {
        "antigravity_cli",
        "claude_code",
        "codex",
        "copilot_cli",
        "cursor_cli",
        "grok_cli",
        "hermes",
        "kimi_cli",
        "kiro_cli",
        "mcode",
        "omp",
        "opencode_cli",
    }
)
NATIVE_ENFORCEMENT = frozenset(
    {"claude_code", "copilot_cli", "grok_cli", "kiro_cli", "opencode_cli"}
)
UNSUPPORTED_PROVIDERS = {
    "antigravity_cli": "Antigravity CLI reads the MCP servers of all CAO terminals from one shared file",
    "cursor_cli": "CAO launches Cursor CLI without the instructions of the agent profile",
    "hermes": "Hermes reads MCP servers only from its own Hermes profile",
    "opencode_cli": "OpenCode keeps the MCP servers of all agents in one shared configuration",
}


def read_profile(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{path.name} must start with YAML frontmatter")
    front, separator, instructions = text[len("---\n") :].partition("\n---\n")
    profile = yaml.safe_load(front) if separator else None
    if not isinstance(profile, dict):
        raise ValueError(f"{path.name} must have a YAML mapping between '---' lines")
    return profile, instructions.strip()


def write_private(path: Path, value: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def actors_for(scene: Scene) -> dict[str, dict]:
    actors: dict[str, dict] = {
        "supervisor": {"scopes": ["observe"], "role": "supervisor"},
        "checker": {"scopes": ["observe"], "role": "checker"},
        "operator": {"scopes": ["observe", "operate"], "role": "operator"},
    }
    for robot in scene.humanoids:
        actors[f"humanoid_{robot}"] = {
            "scopes": ["observe", "walk"],
            "role": "humanoid",
            "robot": robot,
        }
    for robot in scene.arms:
        actors[f"arm_{robot}"] = {"scopes": ["observe", "pick"], "role": "arm", "robot": robot}
    return actors


def prepare(run_dir: Path, scene_file: Path, *, port: int, provider: str) -> dict:
    scene = Scene.model_validate_json(scene_file.read_text(encoding="utf-8"))
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    if provider not in CAO_PROVIDERS:
        raise ValueError(
            f"unknown CAO provider {provider!r}; use one of: {', '.join(sorted(CAO_PROVIDERS))}"
        )
    if provider in UNSUPPORTED_PROVIDERS:
        raise ValueError(f"{provider} is not supported: {UNSUPPORTED_PROVIDERS[provider]}")
    sources = {role: read_profile(path) for role, path in PROFILE_SOURCES.items()}
    run_dir = run_dir.resolve()
    run_dir.mkdir(mode=0o700)
    (run_dir / "credentials").mkdir(mode=0o700)
    (run_dir / "profiles").mkdir(mode=0o700)
    (run_dir / ".gitignore").write_text("*\n", encoding="utf-8")
    run_id = uuid.uuid4().hex
    url = f"http://127.0.0.1:{port}/mcp"
    actors = actors_for(scene)
    # A random suffix per run, so a later run never replaces a running run's profiles.
    profiles = {
        actor: f"{sources[meta['role']][0]['name']}_{uuid.uuid4().hex[:12]}"
        for actor, meta in actors.items()
        if actor != "operator"
    }
    operator_profiles = {
        meta["robot"]: profiles[actor] for actor, meta in actors.items() if "robot" in meta
    }
    for actor, meta in actors.items():
        credential_path = run_dir / "credentials" / f"{actor}.json"
        write_private(credential_path, {"url": url, "token": secrets.token_urlsafe(32)})
        if actor == "operator":
            continue
        source, instructions = sources[meta["role"]]
        description = source["description"] + (
            f" (robot {meta['robot']})" if "robot" in meta else ""
        )
        profile: dict = {
            "name": profiles[actor],
            "description": description,
            "provider": provider,
            **{
                k: copy.deepcopy(v)
                for k, v in source.items()
                if k not in ("name", "description", "provider")
            },
        }
        profile["mcpServers"]["cell-sim"] = {
            "type": "stdio",
            "command": sys.executable,
            "args": [str(HERE / "demo.py"), "connect", str(credential_path)],
        }
        bindings = {
            "run_id": run_id,
            "your_robot": meta.get("robot"),
            "operator_profiles": operator_profiles,
            "checker_profile": profiles["checker"],
        }
        text = (
            "---\n"
            + yaml.safe_dump(profile, sort_keys=False, allow_unicode=True, width=4096)
            + "---\n\n"
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


def ensure_port_free(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as error:
            raise OSError(
                f"port {port} is in use, for example by another run's controller: {error}"
            ) from error


def _interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def make_backend(name: str):
    if name == "fake":
        return FakeBackend()
    from strands_backend import StrandsBackend

    return StrandsBackend()


def serve(run_dir: Path, *, allow_motion: bool, backend: str) -> None:
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    scene = Scene.model_validate_json((run_dir / "scene.json").read_text(encoding="utf-8"))
    tokens = {}
    for actor, meta in manifest["actors"].items():
        credentials = json.loads(
            (run_dir / "credentials" / f"{actor}.json").read_text(encoding="utf-8")
        )
        if credentials["url"] != manifest["url"]:
            raise ValueError("all credentials must address this run's controller")
        tokens[credentials["token"]] = {"client_id": actor, **meta}
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    port = urlsplit(manifest["url"]).port
    ensure_port_free(port)
    write_private(run_dir / "started.json", {"pid": os.getpid(), "run_id": manifest["run_id"]})
    cell = Cell(scene, make_backend(backend), run_id=manifest["run_id"], allow_motion=allow_motion)
    logging.getLogger("cell").warning(
        "SIMULATION ONLY (%s backend); motion %s",
        backend,
        "approved for this run" if allow_motion else "DISABLED (no --allow-motion)",
    )
    server = make_server(cell, tokens, run_dir / "last-state.json")
    logging.getLogger("cell").warning(
        "LIVE VIEW: open http://127.0.0.1:%d/ in a browser to watch the run", port
    )
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _interrupt)
    try:
        server.run(
            transport="http",
            host="127.0.0.1",
            port=port,
            stateless_http=True,
            show_banner=False,
            # Check Host and Origin on every request, the MCP endpoint and the live
            # view alike, so that DNS rebinding cannot reach the controller.
            host_origin_protection=True,
            allowed_hosts=allowed_hosts(),
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


async def operator_call(run_dir: Path, tool: str) -> dict:
    credentials = json.loads(
        (run_dir / "credentials" / "operator.json").read_text(encoding="utf-8")
    )
    async with controller_client(credentials["url"], credentials["token"]) as client:
        result = await client.call_tool(tool)
        if result.is_error or not isinstance(result.data, dict):
            raise RuntimeError("controller did not return a checked state")
        return result.data


def rehearse(scene_file: Path, *, backend: str) -> int:
    """Run the default job with no LLM: the same calls the agents make, in order."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    scene = Scene.model_validate_json(scene_file.read_text(encoding="utf-8"))
    cell = Cell(scene, make_backend(backend), run_id="rehearsal", allow_motion=True)
    cell.start()
    humanoid, arm = next(iter(scene.humanoids)), next(iter(scene.arms))
    parts, bins = list(scene.parts), list(scene.bins)

    def wait(view: dict) -> dict:
        while True:
            record = next(
                c for c in cell.observe()["commands"] if c["command_id"] == view["command_id"]
            )
            if record["status"] in ("succeeded", "failed"):
                print(f"{record['command_id']}: {record['status']} - {record['exit_reason']}")
                return record
            time.sleep(0.2)

    failed = 0
    try:
        try:
            cell.pick_and_place("rehearsal", "too_early", arm, parts[0], bins[0])
            print("too_early: accepted - the interlock did NOT refuse")
            failed += 1
        except Refused as refused:
            print(f"too_early: refused as expected - {refused}")
        plan = [
            lambda: cell.walk_to("rehearsal", "walk_out", humanoid, "inspection"),
            lambda: cell.pick_and_place("rehearsal", "pick_1", arm, parts[0], bins[0]),
            lambda: cell.pick_and_place("rehearsal", "pick_2", arm, parts[1], bins[1]),
            lambda: cell.walk_to("rehearsal", "walk_dock", humanoid, "dock"),
        ]
        for step in plan:
            failed += wait(step())["status"] != "succeeded"
        print(json.dumps(cell.observe()["measured"], indent=2))
    finally:
        cell.close()
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("prepare", help="create a fresh private run and scoped CAO profiles")
    setup.add_argument("--run-dir", type=Path, required=True)
    setup.add_argument("--request", required=True, help="goal to send unchanged to the supervisor")
    setup.add_argument("--scene", type=Path, default=HERE / "cell.json")
    setup.add_argument("--port", type=int, default=8767)
    setup.add_argument("--provider", choices=sorted(CAO_PROVIDERS), default="claude_code")
    serve_parser = sub.add_parser("serve", help="own the simulator outside the CAO agents")
    serve_parser.add_argument("--run-dir", type=Path, required=True)
    serve_parser.add_argument("--allow-motion", action="store_true")
    serve_parser.add_argument("--backend", choices=["strands", "fake"], default="strands")
    rehearse_parser = sub.add_parser("rehearse", help="run the default job without CAO or an LLM")
    rehearse_parser.add_argument("--scene", type=Path, default=HERE / "cell.json")
    rehearse_parser.add_argument("--backend", choices=["strands", "fake"], default="strands")
    for command in ("status", "stop"):
        sub.add_parser(command).add_argument("--run-dir", type=Path, required=True)
    sub.add_parser("connect", help="stdio MCP relay; used by generated profiles").add_argument(
        "credential", type=Path
    )
    args = parser.parse_args()
    if args.command == "prepare":
        if not args.request.strip():
            setup.error("--request must not be blank")
        if args.provider in UNSUPPORTED_PROVIDERS:
            setup.error(
                f"--provider {args.provider} is not supported: {UNSUPPORTED_PROVIDERS[args.provider]}"
            )
        manifest = prepare(args.run_dir, args.scene, port=args.port, provider=args.provider)
        if args.provider not in NATIVE_ENFORCEMENT:
            print(
                f"warning: {args.provider} does not enforce a profile's tool allowlist; "
                "use it only for a trusted local demo.",
                file=sys.stderr,
            )
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
                    f"cao-cell-{manifest['run_id'][:12]}",
                    "--working-directory",
                    str(HERE),
                    "--",
                    args.request,
                ]
            )
        )
    elif args.command == "serve":
        serve(args.run_dir.resolve(), allow_motion=args.allow_motion, backend=args.backend)
    elif args.command == "rehearse":
        raise SystemExit(rehearse(args.scene, backend=args.backend))
    elif args.command == "connect":
        credentials = json.loads(args.credential.read_text(encoding="utf-8"))
        proxy = create_proxy(
            controller_client(credentials["url"], credentials["token"]), name="cell-sim"
        )
        proxy.run(transport="stdio", show_banner=False)
    else:
        tool = "stop_cell" if args.command == "stop" else "observe"
        print(json.dumps(asyncio.run(operator_call(args.run_dir, tool)), indent=2))


if __name__ == "__main__":
    main()
