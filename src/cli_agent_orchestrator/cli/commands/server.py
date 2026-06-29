"""``cao server`` — manage the background cao-server daemon without leaving cao.

The daemon is a single FastAPI process (``cao-server``); the CLI, MCP servers,
and Web UI are all thin REST clients of it. These subcommands fold the manual
start/stop/restart/status steps (previously done with ``cao-server`` by hand,
``ss -ltnp`` to find the PID, and ``kill`` to restart) back under ``cao``.
"""

import click

from cli_agent_orchestrator.constants import (
    SERVER_HOST,
    SERVER_PIDFILE,
    SERVER_PORT,
    SERVER_VERSION,
)
from cli_agent_orchestrator.utils.server_process import (
    clear_pidfile,
    health,
    is_server_running,
    read_pidfile,
    start_server_detached,
    stop_server,
)


@click.group()
def server():
    """Manage the cao-server background daemon."""


@server.command("start")
@click.option("--host", default=None, help=f"Server host (default: {SERVER_HOST})")
@click.option("--port", default=None, type=int, help=f"Server port (default: {SERVER_PORT})")
@click.option(
    "--foreground",
    is_flag=True,
    help="Run cao-server in the foreground (blocking) instead of detaching.",
)
def start(host, port, foreground):
    """Start the cao-server daemon (no-op if already running)."""
    # Single-instance guard: if /health already answers, report and exit 0.
    if is_server_running():
        pid = read_pidfile()
        pid_str = f" (pid {pid})" if pid else ""
        click.echo(f"cao-server already running on {SERVER_HOST}:{SERVER_PORT}{pid_str}")
        return

    if foreground:
        # Defer to the real entry point in-process so the user gets live logs.
        import sys

        from cli_agent_orchestrator.api.main import main as server_main

        argv = ["cao-server"]
        if host:
            argv += ["--host", host]
        if port:
            argv += ["--port", str(port)]
        sys.argv = argv
        server_main()
        return

    try:
        pid = start_server_detached(host=host, port=port)
    except RuntimeError as e:
        raise click.ClickException(str(e))

    click.echo(f"cao-server started on {host or SERVER_HOST}:{port or SERVER_PORT} (pid {pid})")


@server.command("stop")
def stop():
    """Stop the running cao-server daemon."""
    if not is_server_running() and read_pidfile() is None:
        click.echo("cao-server is not running.")
        clear_pidfile()
        return

    if stop_server():
        click.echo("cao-server stopped.")
    else:
        raise click.ClickException(
            "Failed to stop cao-server — it may still be running. "
            f"Check the pidfile at {SERVER_PIDFILE}."
        )


@server.command("restart")
@click.option("--host", default=None, help=f"Server host (default: {SERVER_HOST})")
@click.option("--port", default=None, type=int, help=f"Server port (default: {SERVER_PORT})")
def restart(host, port):
    """Restart the cao-server daemon (stop, then start)."""
    if is_server_running() or read_pidfile() is not None:
        if not stop_server():
            raise click.ClickException("Failed to stop the running cao-server before restart.")
        click.echo("cao-server stopped.")
    try:
        pid = start_server_detached(host=host, port=port)
    except RuntimeError as e:
        raise click.ClickException(str(e))
    click.echo(f"cao-server restarted on {host or SERVER_HOST}:{port or SERVER_PORT} (pid {pid})")


@server.command("status")
def status():
    """Show whether the cao-server daemon is running and its health."""
    info = health()
    pid = read_pidfile()
    if info is None:
        click.echo("cao-server: not running")
        if pid is not None:
            click.echo(f"  (stale pidfile {SERVER_PIDFILE} → pid {pid})")
        return

    click.echo(f"cao-server: running on {SERVER_HOST}:{SERVER_PORT}")
    if pid is not None:
        click.echo(f"  pid:     {pid}")
    click.echo(f"  version: {SERVER_VERSION}")
    backend = info.get("terminal_backend")
    if backend:
        click.echo(f"  backend: {backend}")
    components = info.get("components") or {}
    if components:
        click.echo("  components:")
        for name, state in components.items():
            mark = "✓" if state == "ok" else "✗"
            click.echo(f"    {mark} {name}: {state}")
