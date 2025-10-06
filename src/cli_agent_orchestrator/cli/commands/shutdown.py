"""Shutdown command for CLI Agent Orchestrator."""

import click
from cli_agent_orchestrator.services.session_service import list_sessions, delete_session


@click.command()
@click.option('--all', 'shutdown_all', is_flag=True, help='Shutdown all cao sessions')
@click.option('--session', help='Shutdown specific session')
def shutdown(shutdown_all, session):
    """Shutdown tmux sessions and cleanup terminal records."""
    
    if not shutdown_all and not session:
        raise click.ClickException("Must specify either --all or --session")
    
    if shutdown_all and session:
        raise click.ClickException("Cannot use --all and --session together")
    
    # Determine sessions to shutdown
    sessions_to_shutdown = []
    
    if shutdown_all:
        sessions = list_sessions()
        sessions_to_shutdown = [s['id'] for s in sessions]
    else:
        sessions_to_shutdown = [session]
    
    if not sessions_to_shutdown:
        click.echo("No cao sessions found to shutdown")
        return
    
    # Shutdown each session
    for session_name in sessions_to_shutdown:
        try:
            delete_session(session_name)
            click.echo(f"✓ Shutdown session '{session_name}'")
        except Exception as e:
            click.echo(f"Error shutting down session '{session_name}': {e}", err=True)
