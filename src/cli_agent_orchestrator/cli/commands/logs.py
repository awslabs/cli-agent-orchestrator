"""``cao logs`` — read CAO logs without leaving the tool.

Replaces hand-rolled ``grep``/``tail`` over ``~/.aws/cli-agent-orchestrator/logs``.
``--server`` (default) tails the latest ``cao_*.log``; ``--terminal <id>`` tails
that terminal's per-terminal log.
"""

import time
from pathlib import Path
from typing import Optional

import click

from cli_agent_orchestrator.constants import TERMINAL_LOG_DIR
from cli_agent_orchestrator.utils.logging import latest_server_log_path

# Number of trailing lines printed by default (without --follow).
_TAIL_LINES = 200


def _print_tail(path: Path, lines: int) -> None:
    """Print the last ``lines`` lines of ``path``."""
    try:
        content = path.read_text(errors="replace").splitlines()
    except OSError as e:
        raise click.ClickException(f"Could not read {path}: {e}")
    for line in content[-lines:]:
        click.echo(line)


def _follow(path: Path) -> None:
    """Stream new lines appended to ``path`` (tail -f), until Ctrl-C."""
    try:
        with path.open("r", errors="replace") as fh:
            fh.seek(0, 2)  # end of file
            while True:
                line = fh.readline()
                if line:
                    click.echo(line.rstrip("\n"))
                else:
                    time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    except OSError as e:
        raise click.ClickException(f"Could not follow {path}: {e}")


@click.command()
@click.option("--server", "server", is_flag=True, help="Tail the latest server log (default).")
@click.option("--terminal", "terminal_id", default=None, help="Tail a specific terminal's log.")
@click.option("--follow", "-f", is_flag=True, help="Follow the log (tail -f).")
@click.option(
    "--lines",
    "-n",
    default=_TAIL_LINES,
    show_default=True,
    help="Number of trailing lines to print (ignored with --follow start).",
)
def logs(server, terminal_id, follow, lines):
    """Print or follow CAO logs (server log by default)."""
    path: Optional[Path]
    if terminal_id:
        path = TERMINAL_LOG_DIR / f"{terminal_id}.log"
        if not path.exists():
            raise click.ClickException(
                f"No log for terminal {terminal_id} at {path}. " "Has it produced output yet?"
            )
    else:
        # --server is the default even when the flag is omitted.
        path = latest_server_log_path()
        if path is None:
            raise click.ClickException(
                "No server logs found. Start the server with: cao server start"
            )

    click.echo(click.style(f"# {path}", dim=True))
    _print_tail(path, lines)
    if follow:
        _follow(path)
