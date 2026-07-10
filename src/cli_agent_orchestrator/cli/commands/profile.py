"""Agent profile lifecycle management commands.

Provides list/show/validate/remove/create/templates for installed CAO agent profiles.
Ref: https://github.com/awslabs/cli-agent-orchestrator/issues/340
"""

import json
from pathlib import Path
from typing import Optional

import click
import frontmatter
from jsonschema import Draft202012Validator

from cli_agent_orchestrator.constants import LOCAL_AGENT_STORE_DIR, ROLE_TOOL_DEFAULTS
from cli_agent_orchestrator.utils.agent_profiles import (
    list_agent_profiles,
)

# Known deprecated frontmatter fields that should trigger warnings.
_DEPRECATED_FIELDS = {"autoApproveTools"}

# Derive valid tool vocabulary from constants (single source of truth).
_VALID_TOOL_VOCAB: set[str] = set()
for _tools in ROLE_TOOL_DEFAULTS.values():
    _VALID_TOOL_VOCAB.update(_tools)


def _load_schema() -> dict:
    """Load the agent profile JSON-Schema from package resources."""
    schema_path = (
        Path(__file__).resolve().parent.parent.parent / "schemas" / "agent_profile.schema.json"
    )
    return json.loads(schema_path.read_text(encoding="utf-8"))


def _resolve_profile_path(name_or_path: str) -> Optional[Path]:
    """Resolve an agent name or file path to a local profile .md file.

    Returns the resolved Path for file paths and local-store profiles,
    or None if the profile is built-in/provider-managed (resolved via
    _read_profile_text instead) or not found at all.
    """
    if name_or_path.endswith(".md"):
        p = Path(name_or_path).expanduser().resolve()
        if p.exists():
            return p
        return None

    # Bare name: use the shared lookup that searches all stores
    from cli_agent_orchestrator.utils.agent_profiles import _read_agent_profile_source

    try:
        _read_agent_profile_source(name_or_path)
    except (FileNotFoundError, ValueError):
        return None

    # The shared lookup found it. Now find the actual path for display.
    # Check local store first (most common case for user-installed profiles)
    store_root = LOCAL_AGENT_STORE_DIR.resolve()
    candidate = (LOCAL_AGENT_STORE_DIR / f"{name_or_path}.md").resolve()
    if candidate.is_relative_to(store_root) and candidate.exists():
        return candidate

    # For built-in/provider profiles, return None — caller uses _read_profile_text.
    return None


def _read_profile_text(name_or_path: str) -> Optional[str]:
    """Read profile text by name or path. Returns None if not found."""
    if name_or_path.endswith(".md"):
        p = Path(name_or_path).expanduser().resolve()
        if p.exists():
            return p.read_text(encoding="utf-8")
        return None

    # Bare name: use shared lookup
    from cli_agent_orchestrator.utils.agent_profiles import _read_agent_profile_source

    try:
        return _read_agent_profile_source(name_or_path)
    except (FileNotFoundError, ValueError):
        return None


def _validate_frontmatter(metadata: dict) -> list[str]:
    """Validate frontmatter dict against schema and CAO conventions.

    Returns a list of error/warning messages (empty = valid).
    """
    messages: list[str] = []

    # 1. Check deprecated fields first (before schema rejects them via
    #    additionalProperties:false, which gives a less helpful message).
    for field in _DEPRECATED_FIELDS:
        if field in metadata:
            messages.append(
                f"[warn] '{field}' is deprecated and rejected by CAO 2.2+. "
                f"Use 'allowedTools' instead."
            )

    # 2. JSON-Schema structural validation
    schema = _load_schema()
    validator = Draft202012Validator(schema)
    for error in sorted(validator.iter_errors(metadata), key=lambda e: list(e.path)):
        path = ".".join(str(p) for p in error.absolute_path) or "(root)"
        messages.append(f"[error] {path}: {error.message}")

    # 3. allowedTools vocabulary check (advisory, not blocking)
    allowed = metadata.get("allowedTools")
    if allowed and isinstance(allowed, list):
        for tool in allowed:
            if tool not in _VALID_TOOL_VOCAB:
                messages.append(
                    f"[warn] allowedTools entry '{tool}' is not in CAO's recognized "
                    f"vocabulary. It may be silently ignored by some providers."
                )

    # 4. Role check (advisory — custom roles are valid but worth flagging)
    _BUILTIN_ROLES = set(ROLE_TOOL_DEFAULTS.keys())
    role = metadata.get("role")
    if role and role not in _BUILTIN_ROLES:
        messages.append(
            f"[warn] role '{role}' is not a built-in CAO role "
            f"({', '.join(sorted(_BUILTIN_ROLES))}). "
            f"Ensure it is defined in your settings.json custom roles."
        )

    return messages


@click.group()
def profile():
    """Manage agent profiles."""


@profile.command("list")
def list_cmd():
    """List all available agent profiles."""
    profiles = list_agent_profiles()
    if not profiles:
        click.echo("No agent profiles found.")
        return

    click.echo(f"{'NAME':<30} {'SOURCE':<12} {'DESCRIPTION'}")
    click.echo(f"{'─' * 30} {'─' * 12} {'─' * 40}")

    for p in sorted(profiles, key=lambda x: x.get("name", "")):
        name = p.get("name", "?")
        source = p.get("source", "?")
        desc = (p.get("description") or "")[:108]
        click.echo(f"{name:<30} {source:<12} {desc}")

    click.echo(f"\n{len(profiles)} profile(s) found.")


@profile.command("show")
@click.argument("name_or_path")
def show_cmd(name_or_path: str):
    """Show details of an agent profile.

    NAME_OR_PATH can be a profile name (looked up in the local store)
    or a path to a .md file.
    """
    path = _resolve_profile_path(name_or_path)
    if path is not None:
        profile_text = path.read_text(encoding="utf-8")
        source_display = str(path)
    else:
        profile_text = _read_profile_text(name_or_path)
        if profile_text is None:
            raise click.ClickException(f"Profile '{name_or_path}' not found.")
        source_display = f"{name_or_path} (built-in/provider)"

    try:
        post = frontmatter.loads(profile_text)
    except Exception as e:
        raise click.ClickException(f"Error reading profile: {e}")

    meta = post.metadata

    click.echo(f"Profile: {source_display}")
    click.echo(f"{'─' * 60}")
    click.echo(f"  name:         {meta.get('name', '(missing)')}")
    click.echo(f"  description:  {(meta.get('description') or '(none)')}")
    click.echo(f"  role:         {meta.get('role', '(none)')}")
    click.echo(f"  provider:     {meta.get('provider', '(none)')}")

    allowed = meta.get("allowedTools")
    if allowed and isinstance(allowed, list):
        click.echo(f"  allowedTools: {', '.join(allowed)}")

    mcp = meta.get("mcpServers")
    if mcp and isinstance(mcp, dict):
        click.echo(f"  mcpServers:   {', '.join(mcp.keys())}")

    model = meta.get("model")
    if model:
        click.echo(f"  model:        {model}")

    body_len = len(post.content) if post.content else 0
    click.echo(f"  prompt:       {body_len} chars")


@profile.command("validate")
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
    if path is not None:
        profile_text = path.read_text(encoding="utf-8")
    else:
        profile_text = _read_profile_text(name_or_path)
        if profile_text is None:
            raise click.ClickException(f"Profile '{name_or_path}' not found.")

    try:
        post = frontmatter.loads(profile_text)
    except Exception as e:
        raise click.ClickException(f"Error reading profile: {e}")

    messages = _validate_frontmatter(post.metadata)

    if not messages:
        click.echo(f"✓ {name_or_path}: valid")
        return

    click.echo(f"✗ {name_or_path}: {len(messages)} issue(s)")
    for msg in messages:
        click.echo(f"  {msg}")

    if any(msg.startswith("[error]") for msg in messages):
        raise click.exceptions.Exit(1)


@profile.command("remove")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
def remove_cmd(name: str, yes: bool):
    """Remove an agent profile from the local store.

    Only removes profiles from ~/.aws/cli-agent-orchestrator/agent-store/.
    Does not affect built-in or provider-managed profiles.
    """
    store_root = LOCAL_AGENT_STORE_DIR.resolve()
    target = (LOCAL_AGENT_STORE_DIR / f"{name}.md").resolve()

    # Containment check
    if not target.is_relative_to(store_root):
        raise click.ClickException(f"Invalid profile name '{name}'.")

    if not target.exists():
        raise click.ClickException(
            f"Profile '{name}' not found in local store.\n"
            f"  (looked in: {LOCAL_AGENT_STORE_DIR})"
        )

    if not yes:
        click.confirm(f"Remove profile '{name}' from local store?", abort=True)

    target.unlink()
    click.echo(f"✓ Removed '{name}' from {LOCAL_AGENT_STORE_DIR}")


@profile.command("templates")
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
        click.echo(f"{t['name']:<30} {t['description'][:108]}")

    click.echo(f"\n{len(templates)} template(s) available.")
    click.echo("Use: cao profile create --template <name> --config <file>")


@profile.command("create")
@click.option(
    "--template",
    "-t",
    required=True,
    help="Template name (e.g., 'aws/stepfunction'). Run 'cao profile templates' to list.",
)
@click.option(
    "--config",
    "-c",
    "config_path",
    required=True,
    type=click.Path(exists=True),
    help="Path to config.json with user values.",
)
@click.option(
    "--output-dir",
    "-o",
    "output_dir",
    type=click.Path(),
    default=".",
    help="Output directory for the generated profile (default: current dir).",
)
def create_cmd(template: str, config_path: str, output_dir: str):
    """Generate an agent profile from a template.

    Renders a Jinja2 template with values from your config.json to produce
    a ready-to-install .md agent profile.

    Examples:

        cao profile create --template aws/stepfunction --config my-config.json

        cao profile create -t aws/sqs-monitor -c config.json -o ./agents/
    """
    from cli_agent_orchestrator.services.agent_scaffold import (
        render_template,
    )

    # Load config
    config_file = Path(config_path)
    try:
        config = json.loads(config_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise click.ClickException(f"Invalid JSON in {config_path}: {e}")

    # Render template (validates config internally)
    try:
        rendered = render_template(template, config)
    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    except ValueError as e:
        raise click.ClickException(str(e))

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
