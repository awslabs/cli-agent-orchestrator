"""Agent profile scaffolding engine.

Renders Jinja2 templates into concrete agent profiles using a user-provided
config.json. Validates config against per-template schemas before rendering.

Ref: https://github.com/awslabs/cli-agent-orchestrator/issues/340
"""

import json
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader
from jsonschema import Draft202012Validator

# Templates live under src/cli_agent_orchestrator/templates/
_TEMPLATES_ROOT = Path(__file__).resolve().parent.parent / "templates"


def _check_containment(path: Path, root: Path) -> None:
    """Raise FileNotFoundError if resolved path escapes root."""
    resolved = path.resolve()
    root_resolved = root.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise FileNotFoundError(f"Template path escapes templates root: {path}")


def list_templates() -> list[dict]:
    """List available templates.

    Returns a list of dicts with keys: name, description, path.
    """
    templates = []
    if not _TEMPLATES_ROOT.exists():
        return templates

    for category_dir in sorted(_TEMPLATES_ROOT.iterdir()):
        if not category_dir.is_dir():
            continue
        for template_dir in sorted(category_dir.iterdir()):
            if not template_dir.is_dir():
                continue
            template_file = template_dir / "template.md.j2"
            if not template_file.exists():
                continue

            # Read description from schema if available
            schema_file = template_dir / "schema.json"
            description = ""
            if schema_file.exists():
                try:
                    schema = json.loads(schema_file.read_text(encoding="utf-8"))
                    description = schema.get("description", "")
                except (json.JSONDecodeError, OSError):
                    pass

            templates.append(
                {
                    "name": f"{category_dir.name}/{template_dir.name}",
                    "description": description,
                    "path": str(template_dir),
                }
            )

    return templates


def get_template_schema(template_name: str) -> Optional[dict]:
    """Load the JSON-Schema for a template.

    template_name: category/name format (e.g., 'aws/stepfunction').
    Returns the schema dict, or None if not found.
    """
    schema_path = (_TEMPLATES_ROOT / template_name / "schema.json").resolve()
    _check_containment(schema_path, _TEMPLATES_ROOT)
    if not schema_path.exists():
        return None
    return json.loads(schema_path.read_text(encoding="utf-8"))


def validate_config(template_name: str, config: dict) -> list[str]:
    """Validate a config dict against a template's schema.

    Returns a list of error messages (empty = valid).
    """
    schema = get_template_schema(template_name)
    if schema is None:
        return [f"No schema found for template '{template_name}'"]

    errors = []
    validator = Draft202012Validator(schema)
    for error in sorted(validator.iter_errors(config), key=lambda e: list(e.path)):
        path = ".".join(str(p) for p in error.absolute_path) or "(root)"
        errors.append(f"{path}: {error.message}")

    return errors


def render_template(template_name: str, config: dict) -> str:
    """Render a template with the given config.

    template_name: category/name format (e.g., 'aws/stepfunction').
    config: dict of user values (flat keys matching the template schema).

    Returns the rendered markdown string.
    Raises FileNotFoundError if template doesn't exist or escapes root.
    Raises ValueError if config fails validation.
    """
    template_dir = (_TEMPLATES_ROOT / template_name).resolve()
    _check_containment(template_dir, _TEMPLATES_ROOT)
    template_file = template_dir / "template.md.j2"

    if not template_file.exists():
        raise FileNotFoundError(f"Template '{template_name}' not found at {template_dir}")

    # Validate config against schema (if schema exists)
    errors = validate_config(template_name, config)
    if errors:
        raise ValueError(
            f"Config validation failed for '{template_name}':\n"
            + "\n".join(f"  - {e}" for e in errors)
        )

    # Render with Jinja2 defaults. Templates use {{ config.x }} for values
    # and bash ${VAR} passes through unchanged (not Jinja2 syntax).
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        keep_trailing_newline=True,
    )
    template = env.get_template("template.md.j2")

    return template.render(config=config)
