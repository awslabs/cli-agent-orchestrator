"""Resume command for CLI Agent Orchestrator CLI."""

import logging
import shutil
import subprocess
from datetime import datetime
from typing import Any, Dict, List, Optional

import click
import requests

from cli_agent_orchestrator.constants import SERVER_HOST, SERVER_PORT, SESSION_PREFIX

logger = logging.getLogger(__name__)


def _format_last_active(last_active: Optional[str]) -> str:
    """Format last_active timestamp for display."""
    if not last_active:
        return "N/A"
    try:
        dt = datetime.fromisoformat(last_active.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return last_active[:16] if len(last_active) > 16 else last_active


def _get_sessions() -> List[Dict]:
    """Fetch all CAO sessions from the API."""
    response = requests.get(f"http://{SERVER_HOST}:{SERVER_PORT}/sessions", timeout=5)
    response.raise_for_status()
    return response.json()


def _get_session_terminals(session_name: str) -> List[Dict]:
    """Fetch terminals for a specific session."""
    response = requests.get(
        f"http://{SERVER_HOST}:{SERVER_PORT}/sessions/{session_name}/terminals", timeout=5
    )
    response.raise_for_status()
    return response.json()


def _get_terminal(terminal_id: str) -> Dict:
    """Fetch a specific terminal by ID."""
    response = requests.get(
        f"http://{SERVER_HOST}:{SERVER_PORT}/terminals/{terminal_id}", timeout=5
    )
    response.raise_for_status()
    return response.json()


def _list_sessions_detailed():
    """Fetch and display sessions with terminal details."""
    try:
        sessions = _get_sessions()

        if not sessions:
            click.echo("No active CAO sessions found.")
            click.echo("\nStart a new session with: cao launch --agents <profile>")
            return

        click.echo(f"\n{'='*70}")
        click.echo("CAO Sessions")
        click.echo(f"{'='*70}\n")

        for session in sessions:
            session_name = session.get("id", session.get("name", "unknown"))
            window_count = session.get("window_count", "?")

            try:
                terminals = _get_session_terminals(session_name)
            except Exception:
                terminals = []

            click.echo(f"Session: {session_name} ({len(terminals)} terminal(s))")
            click.echo("-" * 60)

            if terminals:
                click.echo(
                    f"  {'ID':<12} {'Agent':<15} {'Provider':<12} {'Status':<12} {'Last Active':<16}"
                )
                for t in terminals:
                    tid = t.get("id", "?")[:10]
                    agent = t.get("agent_profile", "?")[:14]
                    provider = t.get("provider", "?")[:11]
                    status = t.get("status", "?")[:11]
                    last_active = _format_last_active(t.get("last_active"))
                    click.echo(f"  {tid:<12} {agent:<15} {provider:<12} {status:<12} {last_active:<16}")
            else:
                click.echo("  (no terminals found)")

            click.echo()

        click.echo(f"{'='*70}")
        click.echo("Commands:")
        click.echo(f"  cao resume <session-name>     Attach to session")
        click.echo(f"  cao resume -t <terminal-id>   Attach to specific terminal")
        click.echo(f"  cao resume --cleanup          Remove stale entries")

    except requests.exceptions.ConnectionError:
        raise click.ClickException(
            "Cannot connect to cao-server. Is it running?\n"
            "Start with: cao-server"
        )
    except requests.exceptions.Timeout:
        raise click.ClickException("Connection to cao-server timed out.")
    except Exception as e:
        raise click.ClickException(f"Failed to list sessions: {str(e)}")


def _check_tmux_available() -> bool:
    """Check if tmux is available on the system."""
    return shutil.which("tmux") is not None


def _attach_session(session_name: str):
    """Attach to a CAO session."""
    # Check tmux availability
    if not _check_tmux_available():
        raise click.ClickException(
            "tmux is not installed or not in PATH.\n"
            "Install tmux to use CAO session management."
        )

    # Normalize session name (add prefix if missing)
    if not session_name.startswith(SESSION_PREFIX):
        full_session_name = f"{SESSION_PREFIX}{session_name}"
    else:
        full_session_name = session_name

    # Verify session exists via API
    api_verified = False
    try:
        response = requests.get(
            f"http://{SERVER_HOST}:{SERVER_PORT}/sessions/{full_session_name}", timeout=5
        )
        response.raise_for_status()
        api_verified = True
    except requests.exceptions.HTTPError as e:
        if hasattr(e, "response") and e.response is not None and e.response.status_code == 404:
            # Try without prefix modification
            if full_session_name != session_name:
                try:
                    response = requests.get(
                        f"http://{SERVER_HOST}:{SERVER_PORT}/sessions/{session_name}", timeout=5
                    )
                    response.raise_for_status()
                    full_session_name = session_name
                    api_verified = True
                except requests.exceptions.HTTPError:
                    pass

            if not api_verified:
                raise click.ClickException(
                    f"Session '{session_name}' not found.\n"
                    "Run 'cao resume --list' to see available sessions."
                )
        else:
            raise click.ClickException(f"Failed to verify session: {str(e)}")
    except requests.exceptions.ConnectionError:
        # Server not running - try direct tmux attach
        click.echo("Warning: cao-server not running, attempting direct tmux attach...")
        logger.warning("cao-server not running, attempting direct tmux attach")
    except requests.exceptions.Timeout:
        click.echo("Warning: cao-server timed out, attempting direct tmux attach...")
        logger.warning("cao-server timed out, attempting direct tmux attach")

    # Attach to tmux session
    click.echo(f"Attaching to session: {full_session_name}")
    try:
        result = subprocess.run(["tmux", "attach-session", "-t", full_session_name])
        if result.returncode != 0:
            raise click.ClickException(
                f"Failed to attach to session '{full_session_name}'.\n"
                "Is tmux running? Try: tmux ls"
            )
    except FileNotFoundError:
        raise click.ClickException(
            "tmux command not found.\n"
            "Install tmux to use CAO session management."
        )


def _attach_terminal(terminal_id: str):
    """Attach to session and focus specific terminal window."""
    # Check tmux availability
    if not _check_tmux_available():
        raise click.ClickException(
            "tmux is not installed or not in PATH.\n"
            "Install tmux to use CAO session management."
        )

    try:
        terminal = _get_terminal(terminal_id)
    except requests.exceptions.HTTPError as e:
        if hasattr(e, "response") and e.response is not None and e.response.status_code == 404:
            raise click.ClickException(
                f"Terminal '{terminal_id}' not found.\n"
                "Run 'cao resume --list' to see available terminals."
            )
        raise click.ClickException(f"Failed to get terminal: {str(e)}")
    except requests.exceptions.ConnectionError:
        raise click.ClickException(
            "Cannot connect to cao-server. Is it running?\n"
            "Start with: cao-server"
        )
    except requests.exceptions.Timeout:
        raise click.ClickException("Connection to cao-server timed out.")

    # Safely extract terminal info with fallbacks
    session_name = terminal.get("session_name") or terminal.get("tmux_session")
    window_name = terminal.get("name") or terminal.get("tmux_window")
    agent_profile = terminal.get("agent_profile", "unknown")

    if not session_name or not window_name:
        raise click.ClickException(
            f"Terminal '{terminal_id}' has invalid session/window data.\n"
            "Run 'cao resume --cleanup' to remove stale entries."
        )

    click.echo(f"Attaching to terminal: {terminal_id} ({agent_profile})")
    try:
        result = subprocess.run(["tmux", "attach-session", "-t", f"{session_name}:{window_name}"])
        if result.returncode != 0:
            raise click.ClickException(
                f"Failed to attach to terminal.\n"
                f"Try attaching to the session instead: cao resume {session_name}"
            )
    except FileNotFoundError:
        raise click.ClickException(
            "tmux command not found.\n"
            "Install tmux to use CAO session management."
        )


def _cleanup_stale_entries(dry_run: bool = False):
    """Remove database entries for non-existent tmux windows."""
    from cli_agent_orchestrator.clients.database import get_all_terminals, delete_terminal
    from cli_agent_orchestrator.clients.tmux import tmux_client

    try:
        terminals = get_all_terminals()
    except Exception as e:
        raise click.ClickException(f"Failed to read database: {str(e)}")

    orphaned = []

    for terminal in terminals:
        session_name = terminal.get("tmux_session")
        window_name = terminal.get("tmux_window")

        if not session_name or not window_name:
            orphaned.append(terminal)
            continue

        # Check if tmux window exists
        if not tmux_client.session_exists(session_name):
            orphaned.append(terminal)
        elif not tmux_client.window_exists(session_name, window_name):
            orphaned.append(terminal)

    if not orphaned:
        click.echo("No stale entries found. Database is in sync with tmux.")
        return

    click.echo(f"Found {len(orphaned)} stale entries:\n")
    for t in orphaned:
        tid = t.get("id", "?")
        agent = t.get("agent_profile", "?")
        session = t.get("tmux_session", "?")
        click.echo(f"  - {tid} ({agent}) in session {session}")

    if dry_run:
        click.echo("\n[Dry run - no changes made]")
        click.echo("Run without --dry-run to remove these entries.")
        return

    click.echo()
    for t in orphaned:
        try:
            delete_terminal(t["id"])
        except Exception as e:
            click.echo(f"  Warning: Failed to delete {t['id']}: {e}")

    click.echo(f"\nCleaned up {len(orphaned)} stale entries.")


def _reconcile_state():
    """Report discrepancies between database and tmux."""
    from cli_agent_orchestrator.clients.database import get_all_terminals
    from cli_agent_orchestrator.clients.tmux import tmux_client

    try:
        db_terminals = get_all_terminals()
    except Exception as e:
        raise click.ClickException(f"Failed to read database: {str(e)}")

    try:
        tmux_sessions = tmux_client.list_sessions()
    except Exception as e:
        raise click.ClickException(f"Failed to list tmux sessions: {str(e)}")

    # Find DB entries without tmux windows
    orphaned_db = []
    for t in db_terminals:
        session_name = t.get("tmux_session")
        window_name = t.get("tmux_window")

        if not session_name or not tmux_client.session_exists(session_name):
            orphaned_db.append(t)
        elif window_name and not tmux_client.window_exists(session_name, window_name):
            orphaned_db.append(t)

    # Find tmux CAO sessions not in DB
    db_sessions = set(t.get("tmux_session") for t in db_terminals if t.get("tmux_session"))
    untracked_tmux = [
        s for s in tmux_sessions
        if s.get("id", "").startswith(SESSION_PREFIX) and s.get("id") not in db_sessions
    ]

    click.echo("\n" + "=" * 50)
    click.echo("Reconciliation Report")
    click.echo("=" * 50 + "\n")

    if orphaned_db:
        click.echo(f"DB entries without tmux windows ({len(orphaned_db)}):")
        for t in orphaned_db:
            click.echo(f"  - {t.get('id', '?')} ({t.get('agent_profile', '?')})")
        click.echo("\n  → Run 'cao resume --cleanup' to remove these\n")

    if untracked_tmux:
        click.echo(f"Tmux CAO sessions not in database ({len(untracked_tmux)}):")
        for s in untracked_tmux:
            click.echo(f"  - {s.get('id', '?')}")
        click.echo("\n  → These may be manually created or from old CAO versions\n")

    if not orphaned_db and not untracked_tmux:
        click.echo("No discrepancies found.")
        click.echo("Database and tmux are in sync.")

    click.echo()


@click.command()
@click.argument("session_name", required=False)
@click.option("--list", "-l", "list_sessions", is_flag=True, help="List all CAO sessions")
@click.option("--terminal", "-t", help="Attach to specific terminal by ID")
@click.option("--cleanup", is_flag=True, help="Remove stale database entries")
@click.option("--reconcile", is_flag=True, help="Report DB/tmux discrepancies")
@click.option("--dry-run", is_flag=True, help="Preview cleanup without making changes")
def resume(session_name, list_sessions, terminal, cleanup, reconcile, dry_run):
    """Resume or manage CAO sessions.

    Without arguments, lists all active CAO sessions.
    With SESSION_NAME, attaches to that session.

    Examples:

        cao resume                    List all sessions

        cao resume my-project         Attach to session

        cao resume -t abc123          Attach to specific terminal

        cao resume --cleanup          Remove stale DB entries

        cao resume --reconcile        Check DB/tmux sync
    """
    try:
        # Validate mutually exclusive options
        options_set = sum([
            bool(cleanup),
            bool(reconcile),
            bool(terminal),
            bool(session_name),
            bool(list_sessions),
        ])

        if options_set > 1 and not (cleanup and dry_run and options_set == 2):
            # Allow cleanup + dry_run together
            if not (cleanup and options_set == 1):
                raise click.ClickException(
                    "Options --cleanup, --reconcile, --terminal, --list, and SESSION_NAME "
                    "are mutually exclusive.\n"
                    "Use only one at a time (--dry-run can be combined with --cleanup)."
                )

        # Warn if --dry-run used without --cleanup
        if dry_run and not cleanup:
            click.echo("Warning: --dry-run has no effect without --cleanup", err=True)

        # Handle options in priority order
        if cleanup:
            logger.info("Running cleanup with dry_run=%s", dry_run)
            _cleanup_stale_entries(dry_run)
        elif reconcile:
            logger.info("Running reconcile")
            _reconcile_state()
        elif terminal:
            logger.info("Attaching to terminal: %s", terminal)
            _attach_terminal(terminal)
        elif session_name:
            logger.info("Attaching to session: %s", session_name)
            _attach_session(session_name)
        else:
            # Default: list sessions
            logger.info("Listing sessions")
            _list_sessions_detailed()

    except click.ClickException:
        raise
    except KeyboardInterrupt:
        click.echo("\nOperation cancelled.", err=True)
        raise SystemExit(130)
    except Exception as e:
        logger.exception("Unexpected error in resume command")
        raise click.ClickException(f"Unexpected error: {str(e)}")
