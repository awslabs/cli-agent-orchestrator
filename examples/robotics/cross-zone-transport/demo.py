"""Prepare, serve, connect to, or independently stop the transport example."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import os
import secrets
import shlex
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from fastmcp.server import create_proxy
from recorder import Recorder, RecordingError, ensure_empty_directory
from simulation import Scene, World
from transport_mcp import controller_client, make_server

HERE = Path(__file__).resolve().parent
# The committed agent profile of each role. prepare() writes run copies of them.
PROFILE_SOURCES = {
    "supervisor": HERE / "transport_supervisor.md",
    "zone": HERE / "transport_zone_worker.md",
    "checker": HERE / "transport_checker.md",
}
# Every CAO provider (cli_agent_orchestrator.models.provider.ProviderType),
# without the test-only mock_cli. CAO installs an unknown provider name as
# kiro_cli, so prepare() refuses a name that is not in this set.
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
# The providers whose runtime refuses the tools that a profile does not allow
# (level NATIVE in cli_agent_orchestrator.utils.enforcement). With the other
# providers, the allowlist of a profile is only an instruction.
NATIVE_ENFORCEMENT = frozenset(
    {"claude_code", "copilot_cli", "grok_cli", "kiro_cli", "opencode_cli"}
)
# Each agent must get its own transport-sim server with its own credential.
# For opencode_cli, cao install writes the MCP servers of every profile into one
# shared OpenCode configuration, keyed by server name, and a name collision
# replaces the earlier entry (utils/opencode_config.upsert_mcp_server). All the
# agents would then use the credential of the last installed profile.
# For hermes, CAO writes no MCP configuration: Hermes reads MCP servers only
# from its own Hermes profile (docs/hermes.md, "MCP Configuration").
# For antigravity_cli, CAO writes the MCP servers of every terminal into one
# shared ~/.gemini/config/mcp_config.json, keyed "<server>-<terminal id>", and
# each agy process reads the whole file (providers/antigravity_cli.py). A worker
# would also start the servers of the supervisor and of other zones.
UNSUPPORTED_PROVIDERS = {
    "antigravity_cli": (
        "Antigravity CLI reads the MCP servers of all CAO terminals from one shared "
        "file, so an agent could use the simulator credential and the CAO terminal "
        "of another agent"
    ),
    "cursor_cli": (
        "CAO launches Cursor CLI without the instructions of the agent profile, so the "
        "CAO supervisor would get neither its workflow nor its run bindings"
    ),
    "hermes": (
        "Hermes reads MCP servers only from its own Hermes profile, not from the CAO "
        "agent profile, so the agents would get no simulator server"
    ),
    "opencode_cli": (
        "OpenCode keeps the MCP servers of all agents in one shared configuration, "
        "so each agent cannot have its own simulator credential"
    ),
}
SUPPORTED_PROVIDERS = CAO_PROVIDERS - frozenset(UNSUPPORTED_PROVIDERS)


def read_profile(path: Path) -> tuple[dict, str]:
    """Return the frontmatter and the instructions of a committed agent profile."""
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
    sources = {role: read_profile(path) for role, path in PROFILE_SOURCES.items()}
    # A random suffix gives each run its own installed profile names. handoff
    # starts each worker from its installed profile when the supervisor calls
    # it, so a later run must not replace the profiles of a running run.
    profiles = {
        actor: f"{sources[metadata['role']][0]['name']}_{uuid.uuid4().hex}"
        for actor, metadata in actors.items()
        if actor != "operator"
    }
    zone_profiles = {zone: profiles[f"zone_{zone}"] for zone in scene.zones}
    for actor, metadata in actors.items():
        credential_path = run_dir / "credentials" / f"{actor}.json"
        write_private(credential_path, {"url": url, "token": secrets.token_urlsafe(32)})
        if actor == "operator":
            continue
        source, instructions = sources[metadata["role"]]
        description = source["description"]
        if "zone" in metadata:
            description = f"{description} (zone {metadata['zone']})"
        profile: dict = {
            "name": profiles[actor],
            "description": description,
            "provider": provider,
            **{
                key: copy.deepcopy(value)
                for key, value in source.items()
                if key not in ("name", "description", "provider")
            },
        }
        # The committed entry is a placeholder. Bind this run's interpreter and
        # this agent's own credential file.
        profile["mcpServers"]["transport-sim"] = {
            "type": "stdio",
            "command": sys.executable,
            "args": [str(HERE / "demo.py"), "connect", str(credential_path)],
        }
        bindings = {
            "run_id": run_id,
            "your_zone": metadata.get("zone"),
            "zone_profiles": zone_profiles,
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
    write_run_env(run_dir / "run.env", manifest, scene)
    return manifest


def write_run_env(path: Path, manifest: dict, scene: Scene) -> None:
    """Write shell variables for the README commands: one profile name per agent."""
    variables = {
        "RUN_ID": manifest["run_id"],
        "SESSION": f"cao-transport-{manifest['run_id']}",
        "SUPERVISOR": manifest["profiles"]["supervisor"],
        "CHECKER": manifest["profiles"]["checker"],
    }
    for zone in scene.zones:
        variable = "ZONE_" + zone.upper().replace("-", "_")
        if variable in variables:
            raise ValueError(f"zones {zone!r} and another zone both map to {variable}")
        variables[variable] = manifest["profiles"][f"zone_{zone}"]
    text = "".join(f"{name}={shlex.quote(value)}\n" for name, value in variables.items())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)


def serve(run_dir: Path, *, allow_motion: bool, record: Path | None = None) -> None:
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
    if record is not None:
        # Before started.json: a refused frames directory must not consume the run.
        ensure_empty_directory(record)
    # Restarting would lose command history while old workers still hold credentials.
    write_private(run_dir / "started.json", {"pid": os.getpid(), "run_id": manifest["run_id"]})
    world = World(scene, allow_motion=allow_motion, run_id=manifest["run_id"])
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("transport").warning(
        "SIMULATION ONLY: kinematic carrying; motion %s",
        "approved for this scene" if allow_motion else "DISABLED (no --allow-motion)",
    )
    server = make_server(world, tokens, run_dir / "last-state.json")
    recorder = Recorder(world, record) if record is not None else None
    if recorder is not None:
        recorder.start()
        logging.getLogger("transport").info("Recording frames of the world to %s", record)
    try:
        server.run(
            transport="http",
            host="127.0.0.1",
            port=urlsplit(manifest["url"]).port,
            stateless_http=True,
            show_banner=False,
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Ctrl+C is the documented stop. Uvicorn has already shut down cleanly and
        # re-raises the signal; under anyio it can arrive as CancelledError.
        pass
    finally:
        if recorder is not None:
            try:
                frames = recorder.stop()
            except (TimeoutError, RecordingError) as error:
                logging.getLogger("transport").error(
                    "Recording is not complete: %s. The files in %s can be incomplete.",
                    error,
                    record,
                )
            else:
                if frames:
                    logging.getLogger("transport").info(
                        "Recorded %d frame(s); open %s", len(frames), Path(record) / "index.html"
                    )
                else:
                    logging.getLogger("transport").warning(
                        "Recorded no frames. See the README section 'See the robots move'."
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
    setup.add_argument("--request", required=True, help="goal to send unchanged to the supervisor")
    setup.add_argument("--scene", type=Path, default=HERE / "site.json")
    setup.add_argument("--port", type=int, default=8766)
    setup.add_argument("--provider", choices=sorted(CAO_PROVIDERS), default="copilot_cli")
    serve_parser = sub.add_parser("serve", help="own the persistent simulator outside CAO workers")
    serve_parser.add_argument("--run-dir", type=Path, required=True)
    serve_parser.add_argument("--allow-motion", action="store_true")
    serve_parser.add_argument(
        "--record",
        type=Path,
        metavar="DIR",
        help="write PNG frames of the MuJoCo world to DIR (needs an OpenGL backend)",
    )
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
                f"--provider {args.provider} is not supported: "
                f"{UNSUPPORTED_PROVIDERS[args.provider]}"
            )
        manifest = prepare(args.run_dir, args.scene, port=args.port, provider=args.provider)
        if args.provider not in NATIVE_ENFORCEMENT:
            print(
                f"warning: {args.provider} does not enforce the tool allowlist of a profile. "
                "The zone workers and the checker can then use tools that their profile does "
                "not allow, for example a shell, and read the credential files of other "
                f"zones. Use {args.provider} only for a trusted local demo.",
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
                    f"cao-transport-{manifest['run_id']}",
                    "--working-directory",
                    str(HERE),
                    "--",
                    args.request,
                ]
            )
        )
    elif args.command == "serve":
        serve(
            args.run_dir.resolve(),
            allow_motion=args.allow_motion,
            record=args.record.resolve() if args.record else None,
        )
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
