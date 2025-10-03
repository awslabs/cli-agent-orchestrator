"""Launch command for CLI Agent Orchestrator CLI."""

import subprocess
import click

from cli_agent_orchestrator.services.terminal_service import create_terminal
from cli_agent_orchestrator.constants import PROVIDERS
from cli_agent_orchestrator.utils.terminal import generate_session_name


@click.command()
@click.option('--agents', required=True, help='Agent profile to launch')
@click.option('--session-name', help='Name of the session (default: auto-generated)')
@click.option('--headless', is_flag=True, help='Launch in detached mode')
@click.option('--provider', default='q_cli', help='Provider to use (default: q_cli)')
def launch(agents, session_name, headless, provider):
    """Launch cao session with specified agent profile."""
    try:
        # Validate provider
        if provider not in PROVIDERS:
            raise click.ClickException(f"Invalid provider '{provider}'. Available providers: {', '.join(PROVIDERS)}")
        
        # Generate session name if not provided
        if not session_name:
            session_name = generate_session_name()
        
        # Create session with terminal
        terminal = create_terminal(
            session_name=session_name,
            provider=provider,
            agent_profile=agents,
            new_session=True
        )
        
        click.echo(f"Session created: {terminal.session_name}")
        click.echo(f"Terminal created: {terminal.name}")
        
        # Attach to tmux session unless headless
        if not headless:
            subprocess.run(["tmux", "attach-session", "-t", terminal.session_name])
            
    except Exception as e:
        raise click.ClickException(str(e))
