"""Install command for CLI Agent Orchestrator."""

import re
from pathlib import Path
from typing import Optional

import click

from cli_agent_orchestrator.constants import (
    CAO_ENV_FILE,
    DEFAULT_PROVIDER,
    LOCAL_AGENT_STORE_DIR,
    PROVIDERS,
)
from cli_agent_orchestrator.services.install_service import install_agent, parse_env_assignment

# Profile names are used as filesystem path segments; this matches the stricter
# validator inside install_service.py (kept duplicated deliberately — the CLI
# owes the service layer clean input, not the other way around).
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _copy_local_profile_to_store(agent_source: str) -> Optional[str]:
    """If ``agent_source`` is a local ``.md`` file, copy it into the agent store.

    Returns the validated stem on success, or ``None`` if the input is not a
    file-path shape (so the caller should pass ``agent_source`` through as a
    bare name or URL instead).

    File-handling deliberately lives in the CLI rather than ``install_service``:
    only the CLI has legitimate filesystem trust, and keeping
    ``Path(user_input)`` out of the HTTP-reachable service layer is what
    closes the CodeQL ``py/path-injection`` taint flow on the install API.
    """
    # Not a file-path shape at all — let the service treat it as a name or URL.
    if agent_source.startswith(("http://", "https://")):
        return None
    if not agent_source.endswith(".md"):
        return None

    source_path = Path(agent_source).expanduser()
    if not source_path.exists():
        # The user typed a `.md`-suffixed string but no such file exists.
        # Raise instead of silently forwarding to the name branch — the error
        # message is clearer this way.
        raise click.ClickException(f"File not found: {agent_source}")

    stem = source_path.stem
    if not _PROFILE_NAME_RE.fullmatch(stem):
        raise click.ClickException(
            f"Profile filename stem '{stem}' must match [A-Za-z0-9_-]{{1,64}}."
        )

    LOCAL_AGENT_STORE_DIR.mkdir(parents=True, exist_ok=True)
    # Build the destination from the validated stem, not from source_path.name,
    # so nothing from the user-provided string flows into the dest Path.
    dest_file = LOCAL_AGENT_STORE_DIR / f"{stem}.md"
    dest_file.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")
    return stem


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

    # Handle the file-path shape here in the CLI. If it was a local .md file,
    # it's now copied into the agent store and `service_source` is just the
    # bare stem — which install_agent() accepts through its safe "name" branch.
    copied_from_file = False
    try:
        copied_stem = _copy_local_profile_to_store(agent_source)
    except click.ClickException:
        raise
    if copied_stem is not None:
        service_source = copied_stem
        copied_from_file = True
    else:
        service_source = agent_source

    result = install_agent(service_source, provider, parsed_env or None)

    if not result.success:
        click.echo(f"Error: {result.message}", err=True)
        return

    if copied_from_file:
        click.echo("✓ Copied agent from file to local store")
    elif result.source_kind == "url":
        click.echo("✓ Downloaded agent from URL to local store")
    click.echo(f"✓ Agent '{result.agent_name}' installed successfully")
    if env_vars:
        click.echo(f"✓ Set {len(env_vars)} env var(s) in {CAO_ENV_FILE}")
    if result.unresolved_vars:
        click.echo(
            f"⚠ Unresolved env var(s) in profile: {', '.join(result.unresolved_vars)}. "
            "Set them with `cao env set` or pass --env KEY=VALUE.",
            err=True,
        )
    if result.context_file:
        click.echo(f"✓ Context file: {result.context_file}")
    if result.agent_file:
        click.echo(f"✓ {provider} agent: {result.agent_file}")
