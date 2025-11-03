"""Template variable replacement utility."""

import re
from typing import Any, Dict


def render_template(template: str, variables: Dict[str, Any]) -> str:
    """Replace [[key]] with values from variables dict.

    Args:
        template: String with [[variable]] placeholders
        variables: Dictionary of variable values

    Returns:
        Rendered template string

    Raises:
        ValueError: If any required variable is missing from variables dict
    """
    pattern = r"\[\[(\w+)\]\]"
    required_vars = set(re.findall(pattern, template))
    missing_vars = required_vars - set(variables.keys())

    if missing_vars:
        raise ValueError(f"Missing template variables: {', '.join(sorted(missing_vars))}")

    def replace(match: re.Match[str]) -> str:
        return str(variables[match.group(1)])

    return re.sub(pattern, replace, template)
