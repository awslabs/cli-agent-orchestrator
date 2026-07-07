"""Agent profile lifecycle management commands.

Provides list/show/validate/remove for installed CAO agent profiles.
Ref: https://github.com/awslabs/cli-agent-orchestrator/issues/340
"""

import json
import sys
from importlib import resources as importlib_resources
from pathlib import Path
from typing import Optional

import click
import frontmatter
from jsonschema import Draft202012Validator, ValidationError

from cli_agent_orchestrator.constants import LOCAL_AGENT_STORE_DIR
from cli_agent_orchestrator.utils.agent_profiles import (
    list_agent_profiles,
    parse_agent_profile_text,
)

# Known deprecated frontmatter fields that should trigger warnings.
_DEPRECATED_FIELDS = {"autoApproveTools"}

# Valid CAO tool vocabulary (from constants.ROLE_TOOL_DEFAULTS + tool_mapping.py).
_VALID_TOOL_VOCAB = {
    "execute_bash",
    "fs_read",
    "fs_write",
    "fs_list",
    "fs_*",
    "web_fetch",
    "@builtin",
    "@cao-mcp-server",
}


def _load_schema() -> dict:
    """Load the agent profile JSON-Schema from package resources."""
    # __file__ is cli/commands/agents.py → go up 3 levels to package root
    schema_path = (
        Path(__file__).resolve().parent.parent.parent / "schemas" / "agent_profile.schema.json"
    )
    return json.loads(schema_path.read_text(encoding="utf-8"))


def _resolve_profile_path(name_or_path: str) -> Optional[Path]:
    """Resolve an agent name or file path to a profile .md file.

    Accepts:
    - A file path (absolute or relative, must end in .md and exist)
    - A bare agent name (looked up in LOCAL_AGENT_STORE_DIR)

    Returns the resolved Path, or None if not found.
    """
    # File path?
    if name_or_path.endswith(".md"):
        p = Path(name_or_path).expanduser()
        if p.exists():
            return p
        return None

    # Bare name: look in local store
    candidate = LOCAL_AGENT_STORE_DIR / f"{name_or_path}.md"
    if candidate.exists():
        return candidate

    return None


def _validate_frontmatter(metadata: dict) -> list[str]:
    """Validate frontmatter dict against schema and CAO conventions.

    Returns a list of error/warning messages (empty = valid).
    """
    messages: list[str] = []

    # 1. JSON-Schema structural validation
    schema = _load_schema()
    validator = Draft202012Validator(schema)
    for error in sorted(validator.iter_errors(metadata), key=lambda e: list(e.path)):
        path = ".".join(str(p) for p in error.absolute_path) or "(root)"
        messages.append(f"[error] {path}: {error.message}")

    # 2. Deprecated field check (not caught by additionalProperties because
    #    the schema uses strict mode -- but we also check explicitly in case
    #    the field arrives via extra=ignore from Pydantic parsing elsewhere).
    for field in _DEPRECATED_FIELDS:
        if field in metadata:
            messages.append(
                f"[warn] '{field}' is deprecated and silently ignored by CAO 2.2+. "
                f"Use 'allowedTools' instead."
            )

    # 3. allowedTools vocabulary check (advisory, not blocking)
    allowed = metadata.get("allowedTools")
    if allowed and isinstance(allowed, list):
        for tool in allowed:
            if tool not in _VALID_TOOL_VOCAB:
                messages.append(
                    f"[warn] allowedTools entry '{tool}' is not in CAO's recognized "
                    f"vocabulary. It may be silently ignored by some providers."
                )

    return messages


@click.group()
def agents():
    """Manage installed agent profiles."""


@agents.command("list")
def list_cmd():
    """List all available agent profiles."""
    profiles = list_agent_profiles()
    if not profiles:
        click.echo("No agent profiles found.")
        return

    # Header
    click.echo(f"{'NAME':<30} {'SOURCE':<12} {'DESCRIPTION'}")
    click.echo(f"{'─' * 30} {'─' * 12} {'─' * 40}")

    for p in sorted(profiles.values() if isinstance(profiles, dict) else profiles,
                    key=lambda x: x.get("name", "")):
        name = p.get("name", "?")
        source = p.get("source", "?")
        desc = p.get("description", "")[:40]
        click.echo(f"{name:<30} {source:<12} {desc}")

    click.echo(f"\n{len(profiles)} profile(s) found.")


@agents.command("show")
@click.argument("name_or_path")
def show_cmd(name_or_path: str):
    """Show details of an agent profile.

    NAME_OR_PATH can be a profile name (looked up in the local store)
    or a path to a .md file.
    """
    path = _resolve_profile_path(name_or_path)
    if path is None:
        click.echo(f"Error: Profile '{name_or_path}' not found.", err=True)
        raise SystemExit(1)

    try:
        post = frontmatter.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        click.echo(f"Error reading profile: {e}", err=True)
        raise SystemExit(1)

    meta = post.metadata

    click.echo(f"Profile: {path}")
    click.echo(f"{'─' * 60}")
    click.echo(f"  name:         {meta.get('name', '(missing)')}")
    click.echo(f"  description:  {meta.get('description', '(none)')}")
    click.echo(f"  role:         {meta.get('role', '(none)')}")
    click.echo(f"  provider:     {meta.get('provider', '(none)')}")

    allowed = meta.get("allowedTools")
    if allowed:
        click.echo(f"  allowedTools: {', '.join(allowed)}")

    mcp = meta.get("mcpServers")
    if mcp:
        click.echo(f"  mcpServers:   {', '.join(mcp.keys())}")

    model = meta.get("model")
    if model:
        click.echo(f"  model:        {model}")

    # Prompt length
    body_len = len(post.content) if post.content else 0
    click.echo(f"  prompt:       {body_len} chars")


@agents.command("validate")
@click.argument("name_or_path")
def validate_cmd(name_or_path: str):
    """Validate an agent profile against the CAO schema.

    NAME_OR_PATH can be a profile name (looked up in the local store)
    or a path to a .md file.

    Checks:
    - Required fields (name)
    - Deprecated fields (autoApproveTools)
    - Unknown frontmatter keys
    - Invalid role values
    - Unrecognized allowedTools vocabulary
    """
    path = _resolve_profile_path(name_or_path)
    if path is None:
        click.echo(f"Error: Profile '{name_or_path}' not found.", err=True)
        raise SystemExit(1)

    try:
        post = frontmatter.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        click.echo(f"Error reading profile: {e}", err=True)
        raise SystemExit(1)

    messages = _validate_frontmatter(post.metadata)

    if not messages:
        click.echo(f"✓ {name_or_path}: valid")
        return

    click.echo(f"✗ {name_or_path}: {len(messages)} issue(s)")
    for msg in messages:
        click.echo(f"  {msg}")

    # Exit non-zero if any errors (not just warnings)
    if any(msg.startswith("[error]") for msg in messages):
        raise SystemExit(1)


@agents.command("remove")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
def remove_cmd(name: str, yes: bool):
    """Remove an agent profile from the local store.

    Only removes profiles from ~/.aws/cli-agent-orchestrator/agent-store/.
    Does not affect built-in or provider-managed profiles.
    """
    target = LOCAL_AGENT_STORE_DIR / f"{name}.md"

    if not target.exists():
        click.echo(f"Error: Profile '{name}' not found in local store.", err=True)
        click.echo(f"  (looked in: {LOCAL_AGENT_STORE_DIR})")
        raise SystemExit(1)

    if not yes:
        click.confirm(f"Remove profile '{name}' from local store?", abort=True)

    target.unlink()
    click.echo(f"✓ Removed '{name}' from {LOCAL_AGENT_STORE_DIR}")


@agents.command("templates")
def templates_cmd():
    """List available agent templates for scaffolding."""
    from cli_agent_orchestrator.services.agent_scaffold import list_templates

    templates = list_templates()
    if not templates:
        click.echo("No templates found.")
        return

    click.echo(f"{'TEMPLATE':<30} {'DESCRIPTION'}")
    click.echo(f"{'─' * 30} {'─' * 50}")
    for t in templates:
        click.echo(f"{t['name']:<30} {t['description'][:50]}")

    click.echo(f"\n{len(templates)} template(s) available.")
    click.echo("Use: cao agents create --template <name> --config <file>")


@agents.command("create")
@click.option(
    "--template", "-t", required=True,
    help="Template name (e.g., 'aws/stepfunction'). Run 'cao agents templates' to list.",
)
@click.option(
    "--config", "-c", "config_path", required=True,
    type=click.Path(exists=True),
    help="Path to config.json with user values.",
)
@click.option(
    "--output-dir", "-o", "output_dir",
    type=click.Path(),
    default=".",
    help="Output directory for the generated profile (default: current dir).",
)
def create_cmd(template: str, config_path: str, output_dir: str):
    """Generate an agent profile from a template.

    Renders a Jinja2 template with values from your config.json to produce
    a ready-to-install .md agent profile.

    Examples:

        cao agents create --template aws/stepfunction --config my-config.json

        cao agents create -t aws/sqs-monitor -c config.json -o ./agents/
    """
    from cli_agent_orchestrator.services.agent_scaffold import (
        render_template,
        validate_config,
    )

    # Load config
    config_file = Path(config_path)
    try:
        config = json.loads(config_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        click.echo(f"Error: Invalid JSON in {config_path}: {e}", err=True)
        raise SystemExit(1)

    # Validate config against template schema
    errors = validate_config(template, config)
    if errors:
        click.echo(f"✗ Config validation failed for template '{template}':", err=True)
        for err in errors:
            click.echo(f"  - {err}", err=True)
        raise SystemExit(1)

    # Render template
    try:
        rendered = render_template(template, config)
    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

    # Determine output filename from template name
    template_basename = template.split("/")[-1]
    output_filename = f"{template_basename}-agent.md"
    output_path = Path(output_dir) / output_filename

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")

    click.echo(f"✓ Generated: {output_path}")
    click.echo(f"  Template:  {template}")
    click.echo(f"  Config:    {config_path}")
    click.echo(f"\nInstall with: cao install {output_path}")
