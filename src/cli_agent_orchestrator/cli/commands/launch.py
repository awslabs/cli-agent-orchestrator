"""Launch command for CLI Agent Orchestrator CLI."""

import os
import time

import click
import requests

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.constants import (
    API_BASE_URL,
    DEFAULT_PROVIDER,
    PROVIDERS,
    SERVER_HOST,
    SERVER_PORT,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils.terminal import (
    poll_until_done,
    sync_backend_from_server,
    wait_until_terminal_status,
)

# Providers that require workspace folder access
PROVIDERS_REQUIRING_WORKSPACE_ACCESS = {
    "antigravity_cli",
    "claude_code",
    "codex",
    "copilot_cli",
    "cursor_cli",
    "hermes",
    "kimi_cli",
    "kiro_cli",
    "opencode_cli",
}

# Validation constraints for ``--env`` forwarded vars (mirrored server-side
# in ``TmuxClient._merge_extra_env``). See issue #248.
_FORWARDED_ENV_BLOCKED_PREFIXES = ("CLAUDE", "CODEX_", "__MISE_")
_FORWARDED_ENV_PREFIX_ALLOWLIST = frozenset(
    {
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
        "CLAUDE_CODE_SKIP_VERTEX_AUTH",
        "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
    }
)
_FORWARDED_ENV_MAX_VALUE_BYTES = 2048


def _parse_env_pairs(pairs):
    """Parse repeated ``KEY=VALUE`` entries into a dict, validating each.

    Mirrors the constraints applied to inherited env in TmuxClient so a
    forwarded var that would be silently dropped server-side is rejected at
    the CLI boundary with a clear error message instead.
    """
    result: dict[str, str] = {}
    for raw in pairs:
        if "=" not in raw:
            raise click.ClickException(
                f"--env expects KEY=VALUE (got {raw!r}); did you forget the '='?"
            )
        key, value = raw.split("=", 1)
        # POSIX env names: leading letter/underscore, then alnum/underscore.
        # Stricter than ``str.isidentifier`` only in that it forbids non-ASCII.
        if (
            not key
            or not (key[0].isalpha() or key[0] == "_")
            or not all(c.isalnum() or c == "_" for c in key)
            or not key.isascii()
        ):
            raise click.ClickException(f"--env key must match [A-Za-z_][A-Za-z0-9_]* (got {key!r})")
        if key not in _FORWARDED_ENV_PREFIX_ALLOWLIST and any(
            key.startswith(p) for p in _FORWARDED_ENV_BLOCKED_PREFIXES
        ):
            raise click.ClickException(
                f"--env key {key!r} uses a blocked prefix "
                f"({', '.join(_FORWARDED_ENV_BLOCKED_PREFIXES)}) reserved for provider env"
            )
        if len(value.encode("utf-8")) >= _FORWARDED_ENV_MAX_VALUE_BYTES:
            raise click.ClickException(
                f"--env value for {key!r} exceeds {_FORWARDED_ENV_MAX_VALUE_BYTES} bytes "
                "(tmux argv limit, PR #246)"
            )
        result[key] = value
    return result


@click.command()
@click.argument("message", required=False, default=None)
@click.option("--agents", required=True, help="Agent profile to launch")
@click.option("--session-name", help="Name of the session (default: auto-generated)")
@click.option("--headless", is_flag=True, help="Launch in detached mode")
@click.option(
    "--provider",
    default=None,
    help=f"Provider to use (default: profile provider or {DEFAULT_PROVIDER})",
)
@click.option(
    "--allowed-tools",
    multiple=True,
    help="Override allowedTools (CAO format: execute_bash, fs_read, @cao-mcp-server). Repeatable.",
)
@click.option(
    "--async",
    "is_async",
    is_flag=True,
    help="Send message and return immediately without waiting for completion",
)
@click.option(
    "--auto-approve",
    is_flag=True,
    help="Skip confirmation prompt (restrictions still enforced).",
)
@click.option(
    "--yolo",
    is_flag=True,
    help="[DANGEROUS] Unrestricted tool access AND skip confirmation prompts. "
    "Agent can execute ANY command including aws, rm, curl.",
)
@click.option(
    "--working-directory",
    default=None,
    help="Working directory for the session (default: current directory)",
)
@click.option(
    "--memory",
    "memory",
    is_flag=True,
    help="Also launch a context-manager (memory_manager) terminal for curated memory injection.",
)
@click.option(
    "--env",
    "env_pairs",
    multiple=True,
    metavar="KEY=VALUE",
    help="Forward an env var to the supervisor AND every worker spawned later "
    "in the same session. Repeatable. Values travel in the request body, not "
    "the URL. Blocked prefixes (CLAUDE/CODEX_/__MISE_) and >=2048-byte values "
    "are rejected. See issue #248.",
)
def launch(
    message,
    agents,
    session_name,
    headless,
    is_async,
    provider,
    allowed_tools,
    auto_approve,
    yolo,
    working_directory,
    memory,
    env_pairs,
):
    """Launch cao session with specified agent profile."""
    try:
        display_dir = working_directory or os.path.realpath(os.getcwd())
        explicit_provider = provider is not None  # True only when --provider was passed
        forwarded_env = _parse_env_pairs(env_pairs) if env_pairs else {}

        # Resolve allowedTools: --yolo > --allowed-tools CLI > profile/role defaults
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
        from cli_agent_orchestrator.utils.tool_mapping import (
            format_tool_summary,
            get_disallowed_tools,
            resolve_allowed_tools,
        )

        resolved_allowed_tools = None
        no_role_set = False
        if yolo:
            resolved_allowed_tools = ["*"]
        elif allowed_tools:
            resolved_allowed_tools = list(allowed_tools)
        else:
            # Load profile to get role-based defaults
            try:
                profile = load_agent_profile(agents)
                mcp_server_names = list(profile.mcpServers.keys()) if profile.mcpServers else None
                no_role_set = not profile.role and not profile.allowedTools
                resolved_allowed_tools = resolve_allowed_tools(
                    profile.allowedTools, profile.role, mcp_server_names
                )
            except (FileNotFoundError, RuntimeError):
                # Profile not found — use developer defaults (backward compatible)
                no_role_set = True
                resolved_allowed_tools = resolve_allowed_tools(None, None, None)

        # Honour profile.provider whenever the user did not pass --provider
        # explicitly. This runs regardless of which permission-resolution
        # branch above fired — provider selection ("which CLI runs this
        # agent?") is orthogonal to tool restrictions ("what is the agent
        # allowed to do?"). Previously this lookup lived inside the ``else``
        # branch and ``--yolo`` / ``--allowed-tools`` silently bypassed the
        # profile's ``provider:`` field, breaking heterogeneous-panel
        # workflows. See issue #239. ``resolve_provider`` falls back to
        # ``DEFAULT_PROVIDER`` when the profile is missing or has no
        # ``provider`` key, so the trailing fallback is no longer needed.
        if provider is None:
            from cli_agent_orchestrator.utils.agent_profiles import resolve_provider

            provider = resolve_provider(agents, DEFAULT_PROVIDER)

        # Validate provider
        if provider not in PROVIDERS:
            raise click.ClickException(
                f"Invalid provider '{provider}'. Available providers: {', '.join(PROVIDERS)}"
            )
        # Confirmation / warning prompts
        if provider in PROVIDERS_REQUIRING_WORKSPACE_ACCESS:
            if yolo:
                # --yolo: warn but don't block
                click.echo(click.style("\n[WARNING] --yolo mode enabled", fg="yellow", bold=True))
                click.echo(
                    f"  Agent '{agents}' launching UNRESTRICTED on {provider}.\n"
                    f"  Agent can execute ANY command (aws, rm, curl, read credentials).\n"
                    f"  Directory: {display_dir}\n"
                )
                if provider == "kiro_cli":
                    # kiro-cli 2.0.1 TUI blocks on an interactive "Yes, I accept"
                    # consent dialog when --trust-all-tools is set. CAO cannot
                    # answer it headlessly, so yolo launches use --legacy-ui.
                    click.echo(
                        "  Note: kiro_cli will launch in --legacy-ui mode so "
                        "--trust-all-tools can be applied non-interactively.\n"
                    )
                elif provider == "opencode_cli":
                    # opencode's TUI has no runtime skip-permissions flag
                    # (tracked upstream in sst/opencode#8463). Permissions are
                    # install-time only, so --yolo cannot loosen them here.
                    click.echo(
                        click.style(
                            "  Note: --yolo has no runtime effect on opencode_cli.\n"
                            "  Permissions are set at cao install time. To get unrestricted\n"
                            "  access, set 'allowedTools: [\"*\"]' in the profile and re-run\n"
                            "  'cao install'. See docs/opencode-cli.md for details.\n",
                            fg="yellow",
                        )
                    )
            else:
                # Normal launch: show tool summary and confirm
                tool_summary = format_tool_summary(resolved_allowed_tools)
                blocked = get_disallowed_tools(provider, resolved_allowed_tools)
                blocked_summary = ", ".join(blocked) if blocked else "(none)"

                click.echo(
                    f"\nAgent '{agents}' launching on {provider}:\n"
                    f"  Allowed:  {tool_summary}\n"
                    f"  Blocked:  {blocked_summary}\n"
                    f"  Directory: {display_dir}\n"
                )
                if no_role_set:
                    click.echo(
                        "  Note: No role or allowedTools set — defaulting to 'developer'.\n"
                        "  Add 'role' or 'allowedTools' to your agent profile to control tool access.\n"
                        "  Docs: https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/tool-restrictions.md\n"
                    )
                click.echo(
                    "  To skip this prompt next time, relaunch with --auto-approve\n"
                    "  To remove all restrictions, relaunch with --yolo\n"
                )
                if not auto_approve and not click.confirm("Proceed?", default=True):
                    raise click.ClickException("Launch cancelled by user")

        # Call API to create session — pass working_directory only if explicitly
        # provided. When omitted, the server defaults to its own CWD.
        url = f"http://{SERVER_HOST}:{SERVER_PORT}/sessions"
        params = {
            "agent_profile": agents,
            "working_directory": working_directory or os.getcwd(),
        }
        if explicit_provider:
            params["provider"] = provider
        if session_name:
            params["session_name"] = session_name
        if resolved_allowed_tools:
            # Pass as comma-separated string for query param
            params["allowed_tools"] = ",".join(resolved_allowed_tools)
        if memory:
            params["memory_manager"] = "true"

        # Forwarded env vars travel in the JSON body so values (which may
        # contain secrets) don't end up in cao-server's HTTP access log.
        # See issue #248.
        request_timeout = get_server_settings()["mcp_request_timeout"]
        post_kwargs: dict = {"params": params, "timeout": request_timeout}
        if forwarded_env:
            post_kwargs["json"] = {"env_vars": forwarded_env}

        response = requests.post(url, **post_kwargs)
        response.raise_for_status()

        terminal = response.json()

        click.echo(f"Session created: {terminal['session_name']}")
        click.echo(f"Terminal created: {terminal['name']}")

        # Attach to tmux session unless headless. Wait for the provider to
        # finish initializing first — otherwise tmux attach races with the
        # TUI's input handler wiring, resizes the pty mid-init, and the TUI
        # silently drops keystrokes. See issue #220. The wait is advisory:
        # if it times out we still attach so the user can inspect the
        # half-initialized session rather than orphan it in tmux.
        if not headless:
            # Align the CLI's backend singleton with the running server.
            # Without this, ``cao-server --terminal herdr`` + no config.json
            # entry causes the CLI to default to tmux. See issue #308.
            sync_backend_from_server()
            ready = wait_until_terminal_status(
                terminal["id"],
                {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
                timeout=120,
            )
            if not ready:
                click.echo(
                    click.style(
                        f"  Warning: {terminal['id']} did not reach idle within 120s — "
                        "attaching anyway; input may be unreliable until init completes.",
                        fg="yellow",
                    )
                )
            get_backend().attach_session(terminal["session_name"])
        elif message:
            ready = wait_until_terminal_status(
                terminal["id"],
                {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
                timeout=120,
            )
            if not ready:
                raise click.ClickException(
                    f"Conductor {terminal['id']} did not become ready within 120s"
                )
            request_timeout = get_server_settings()["mcp_request_timeout"]
            response = requests.post(
                f"{API_BASE_URL}/terminals/{terminal['id']}/input",
                params={"message": message},
                timeout=request_timeout,
            )
            response.raise_for_status()
            time.sleep(3)
            if is_async:
                click.echo(f"Message sent to {terminal['name']}. Running in background.")
                return
            poll_until_done(terminal["id"], timeout=300)
            request_timeout = get_server_settings()["mcp_request_timeout"]
            output_resp = requests.get(
                f"{API_BASE_URL}/terminals/{terminal['id']}/output",
                params={"mode": "last"},
                timeout=request_timeout,
            )
            output_resp.raise_for_status()
            output = output_resp.json().get("output", "")
            if output:
                click.echo(output)

    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"Failed to connect to cao-server: {str(e)}")
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e))
