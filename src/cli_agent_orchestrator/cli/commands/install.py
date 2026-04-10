"""Install command for CLI Agent Orchestrator."""

import click

from cli_agent_orchestrator.constants import CAO_ENV_FILE, DEFAULT_PROVIDER, PROVIDERS
from cli_agent_orchestrator.services.install_service import install_agent, parse_env_assignment


@click.command()
@click.argument("agent_source")
@click.option(
    "--provider",
    type=click.Choice(PROVIDERS),
    default=DEFAULT_PROVIDER,
    help=f"Provider to use (default: {DEFAULT_PROVIDER})",
)
@click.option(
    "--env",
    "env_vars",
    multiple=True,
    help=(
        "Set env vars before installing the agent. Values are stored in "
        "~/.aws/cli-agent-orchestrator/.env and can be referenced in profiles as ${VAR}. "
        "Repeatable: --env KEY=VALUE. Example: --env API_TOKEN=my-secret-token."
    ),
)
def install(agent_source: str, provider: str, env_vars: tuple[str, ...]) -> None:
    """
    Install an agent from local store, built-in store, URL, or file path.

    AGENT_SOURCE can be:
    - Agent name (e.g., 'developer', 'code_supervisor')
    - File path (e.g., './my-agent.md', '/path/to/agent.md')
    - URL (e.g., 'https://example.com/agent.md')

    Profiles can reference values from ~/.aws/cli-agent-orchestrator/.env using ${VAR}
    placeholders in frontmatter or markdown content. Use `cao env set KEY VALUE` to
    manage those values separately, or pass `--env KEY=VALUE` during install to write
    them before the profile is loaded.

    Example:
    \b
        cao install ./service-agent.md --provider claude_code \
          --env API_TOKEN=my-secret-token \
          --env SERVICE_URL=http://127.0.0.1:27124
    """
    try:
        parsed_env = dict(parse_env_assignment(env_assignment) for env_assignment in env_vars)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--env") from exc

    result = install_agent(agent_source, provider, parsed_env or None)

    if not result.success:
        click.echo(f"Error: {result.message}", err=True)
        return

    click.echo(f"✓ Agent '{result.agent_name}' installed successfully")
    if parsed_env:
        click.echo(f"✓ Set {len(parsed_env)} env var(s) in {CAO_ENV_FILE}")
    if result.unresolved_vars:
        click.echo(f"⚠ Unresolved env var(s): {', '.join(result.unresolved_vars)}", err=True)
    if result.context_file:
        click.echo(f"✓ Context file: {result.context_file}")
    if result.agent_file:
        click.echo(f"✓ {provider} agent: {result.agent_file}")
