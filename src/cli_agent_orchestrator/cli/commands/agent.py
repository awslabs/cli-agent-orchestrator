"""Agent orchestration commands for the CLI Agent Orchestrator CLI (issue #616).

CLI equivalents of the in-session MCP orchestration tools -- ``assign``,
``handoff``, ``send_message``, and ``delete_terminal`` -- plus ``status``/
``result`` for checking on a worker. Motivating case: an agent's
``cao-mcp-server`` child process has died (or was never wired up) but the
terminal's own shell is still healthy -- these commands are the escape hatch
back to orchestration without a live MCP connection.

Every subcommand is a thin wrapper around ``utils.orchestration`` -- the SAME
module the MCP tools in ``mcp_server/server.py`` call -- so behavior can never
drift between "orchestrate via MCP" and "orchestrate via this CLI". The
caller's own terminal is inferred from ``CAO_TERMINAL_ID`` (see
``_current_terminal_id`` in that module, invoked internally by assign/handoff/
send-message exactly as the MCP tools already do), never a CLI flag.
"""

import asyncio
import json as _json
import sys
import threading

import click

from cli_agent_orchestrator.utils.orchestration import (
    _assign_impl,
    _cancel_impl,
    _handoff_impl,
    _result_impl,
    _send_message_impl,
    _status_impl,
)

# Heartbeat cadence (seconds) for `cao agent handoff`'s progress ticker -- see
# handoff_cmd's docstring for why this can't be true incremental progress.
_HANDOFF_HEARTBEAT_INTERVAL_S = 30


def _machine_mode(as_json: bool) -> bool:
    """Whether output should be a single stable JSON object.

    Mirrors ``cli/commands/workflow.py``'s ``_machine_mode``: on when --json
    is set OR stdout is not a TTY, so a piped/CI invocation gets a parseable
    result instead of a human progress stream.
    """
    return bool(as_json) or not sys.stdout.isatty()


def _emit(result: dict, as_json: bool) -> bool:
    """Render an impl-function result dict (human or --json) and return its success flag.

    Deliberately generic: assign/send-message/status/result/cancel each
    return a differently-shaped dict -- no MCP tool ever standardized one
    envelope across all of them -- so this renders whatever keys are present
    rather than hard-coding per-command fields. ``output`` (a worker's full
    response text) gets its own multi-line block; every other key is one
    ``key: value`` line. ``success`` itself is never printed as a line -- the
    command's exit code already carries it.
    """
    if as_json:
        click.echo(_json.dumps(result, indent=2))
        return bool(result.get("success"))
    for key, value in result.items():
        if key == "success" or value is None:
            continue
        if key == "output":
            click.echo("output:")
            click.echo(value)
        else:
            click.echo(f"{key}: {value}")
    return bool(result.get("success"))


@click.group()
def agent():
    """Orchestrate other agents from the shell.

    CLI equivalents of the cao-mcp-server orchestration tools (assign,
    handoff, send_message, delete_terminal) plus status/result for checking
    on a worker -- for use when a terminal's MCP connection is unavailable.
    The caller's own terminal is inferred from the CAO_TERMINAL_ID
    environment variable, exactly like the MCP tools.
    """


@agent.command(name="assign")
@click.argument("agent_profile")
@click.argument("message")
@click.option(
    "--working-directory",
    default=None,
    help="Working directory for the worker (defaults to the caller's cwd).",
)
@click.option("--engine", default=None, help="Explicit Kiro engine for the worker (v2 or kas).")
@click.option(
    "--model", default=None, help="Model override for the worker (not honored by every provider)."
)
@click.option(
    "--use-worktree",
    is_flag=True,
    default=False,
    help="Give the worker its own git worktree instead of sharing the caller's checkout.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the result as JSON.")
def assign_cmd(agent_profile, message, working_directory, engine, model, use_worktree, as_json):
    """Assign a task to a new worker terminal without blocking.

    Equivalent to the MCP `assign` tool. Must run from inside a CAO terminal
    (CAO_TERMINAL_ID set) so the worker joins the caller's session and its
    results can route back -- the sender's terminal id and callback
    instructions are appended to MESSAGE automatically, same as the MCP tool.
    The worker can reply with `cao agent send-message` (omit --to to route
    back to this terminal), or you can poll it with `cao agent status` /
    `cao agent result`. Clean it up with `cao agent cancel --delete
    TERMINAL_ID` once you no longer need it.
    """
    result = _assign_impl(
        agent_profile,
        message,
        working_directory,
        engine=engine,
        model=model,
        use_worktree=use_worktree,
    )
    if not _emit(result, as_json):
        raise click.exceptions.Exit(1)


@agent.command(name="handoff")
@click.argument("agent_profile")
@click.argument("message")
@click.option(
    "--timeout",
    type=click.IntRange(1, 3600),
    default=600,
    show_default=True,
    help="Maximum seconds to wait for the worker to complete.",
)
@click.option(
    "--working-directory",
    default=None,
    help="Working directory for the worker (defaults to the caller's cwd).",
)
@click.option("--engine", default=None, help="Explicit Kiro engine for the worker (v2 or kas).")
@click.option(
    "--model", default=None, help="Model override for the worker (not honored by every provider)."
)
@click.option(
    "--use-worktree",
    is_flag=True,
    default=False,
    help="Give the worker its own git worktree instead of sharing the caller's checkout.",
)
@click.option(
    "--no-wait",
    "no_wait",
    is_flag=True,
    default=False,
    help="Return immediately after creating the worker; don't wait for it to complete.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the result as JSON.")
def handoff_cmd(
    agent_profile,
    message,
    timeout,
    working_directory,
    engine,
    model,
    use_worktree,
    no_wait,
    as_json,
):
    """Hand off a task to a worker terminal and BLOCK until it completes.

    Equivalent to the MCP `handoff` tool: creates a worker, sends MESSAGE,
    waits up to --timeout seconds, and prints its output. The worker is torn
    down automatically on success. Works outside a CAO terminal too (a fresh
    session is created for it), but run this from inside one so the worker
    inherits the caller's session and tool restrictions.

    Recovering from a kill: the worker's terminal_id is printed to stderr as
    soon as the worker exists -- before the wait for completion, not just at
    the end -- so `Ctrl-C`-ing this command still leaves you a handle: check
    on it with `cao agent status TERMINAL_ID`, read whatever it produced with
    `cao agent result TERMINAL_ID`, or free it with `cao agent cancel --delete
    TERMINAL_ID`.

    That handle is a MANUAL recovery route, not an automatic one: re-running
    this command after a kill creates a NEW worker. Retry-safety needs a
    durable run record so a retry can return the existing run instead of
    starting the task again -- tracked in #715, which is also what closes
    #616's "killing the CLI process does not lose the job or its result".

    --no-wait: returns as soon as the worker exists and has been sent MESSAGE,
    without waiting for -- or extracting -- its result, and without tearing it
    down. Prints the terminal_id and exits 0 immediately; poll it with `cao
    agent status`/`result` and clean it up with `cao agent cancel --delete`
    same as above. Use this for a task you don't want to block your shell on.

    Progress: absent --no-wait, this is ONE blocking call to cao-server for
    its whole duration -- unlike `cao workflow run`, there is no server-side
    run id to poll incrementally, so a heartbeat line prints every 30s on a
    TTY (elapsed time only) so a long wait doesn't look hung. Suppressed
    under --json or when stdout is not a TTY.

    Exit codes:
      0    the worker completed successfully (or was created, under --no-wait)
      1    the worker failed, errored, or the request timed out
      130  interrupted (Ctrl-C) -- the request may still be running on
           cao-server. Check the terminal_id printed to stderr above (if any
           was printed yet) with `cao agent status`, or `cao session list` to
           find and clean up an orphaned worker if not.
    """
    machine = _machine_mode(as_json)
    if not machine and not no_wait:
        click.echo(f"Waiting for '{agent_profile}' to complete (timeout {timeout}s)...")

    def _report_terminal_id(terminal_id: str) -> None:
        click.echo(f"terminal_id: {terminal_id}", err=True)

    stop_heartbeat = threading.Event()

    def _heartbeat() -> None:
        elapsed = 0
        while not stop_heartbeat.wait(_HANDOFF_HEARTBEAT_INTERVAL_S):
            elapsed += _HANDOFF_HEARTBEAT_INTERVAL_S
            click.echo(f"  ... still waiting ({elapsed}s elapsed)", err=True)

    heartbeat_thread = None
    if not machine and not no_wait:
        heartbeat_thread = threading.Thread(target=_heartbeat, daemon=True)
        heartbeat_thread.start()

    try:
        result = asyncio.run(
            _handoff_impl(
                agent_profile,
                message,
                timeout,
                working_directory,
                engine=engine,
                model=model,
                use_worktree=use_worktree,
                on_terminal_id=_report_terminal_id,
                wait=not no_wait,
            )
        )
    except KeyboardInterrupt:
        click.echo(
            "\nInterrupted -- the handoff request may still be running on cao-server. "
            "If a terminal_id was printed above, check it with `cao agent status`; "
            "otherwise check `cao session list` to find and clean up an orphaned worker.",
            err=True,
        )
        sys.exit(130)
    finally:
        stop_heartbeat.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1.0)

    if not _emit(result.model_dump(), as_json):
        raise click.exceptions.Exit(1)


@agent.command(name="send-message")
@click.argument("message")
@click.option(
    "--to",
    "receiver_id",
    default=None,
    help=(
        "Target terminal ID. Omit to reply to the terminal that assigned/handed off to "
        "this one (the recorded caller)."
    ),
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the result as JSON.")
def send_message_cmd(message, receiver_id, as_json):
    """Send a message to another terminal's inbox.

    Equivalent to the MCP `send_message` tool. Delivered when the destination
    terminal is IDLE. Omit --to to reply to the recorded caller -- the
    terminal that created this one via assign/handoff, resolved from
    CAO_TERMINAL_ID -- the reliable way to send results back to a supervisor.
    """
    result = _send_message_impl(receiver_id, message)
    if not _emit(result, as_json):
        raise click.exceptions.Exit(1)


@agent.command(name="status")
@click.argument("terminal_id")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the result as JSON.")
def status_cmd(terminal_id, as_json):
    """Show a worker terminal's current status (idle, processing, ...).

    No MCP tool exposes this today -- an assign caller normally learns
    completion from the worker's own send_message callback. This is the
    poll-style check for when that callback hasn't arrived yet (or the
    worker was never told to send one).
    """
    result = _status_impl(terminal_id)
    if not _emit(result, as_json):
        raise click.exceptions.Exit(1)


@agent.command(name="result")
@click.argument("terminal_id")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the result as JSON.")
def result_cmd(terminal_id, as_json):
    """Show a worker terminal's last response.

    The tail of its most recent turn -- the CLI counterpart of what a
    supervisor would otherwise learn from a worker's send_message callback.
    """
    result = _result_impl(terminal_id)
    if not _emit(result, as_json):
        raise click.exceptions.Exit(1)


@agent.command(name="cancel")
@click.argument("terminal_id")
@click.option(
    "--delete",
    "delete_flag",
    is_flag=True,
    default=False,
    help=(
        "Free the terminal entirely (same as the delete_terminal MCP tool), "
        "instead of just interrupting its current turn."
    ),
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the result as JSON.")
def cancel_cmd(terminal_id, delete_flag, as_json):
    """Stop a worker terminal's current turn.

    Default: sends an interrupt (C-c) -- cooperative, the terminal survives
    so it can be reassigned (same spirit as `cao workflow cancel`). --delete
    instead frees the terminal entirely, equivalent to the delete_terminal
    MCP tool -- use it once you are done with a worker (assign's own success
    message points here for cleanup).
    """
    result = _cancel_impl(terminal_id, delete=delete_flag)
    if not _emit(result, as_json):
        raise click.exceptions.Exit(1)
