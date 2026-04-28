"""OpenCode CLI agent configuration model."""

from typing import Dict, Literal, Optional

from pydantic import BaseModel


class OpenCodeAgentConfig(BaseModel):
    """OpenCode agent frontmatter configuration.

    Serialized via ``frontmatter.dumps(frontmatter.Post(body, **config.model_dump(exclude_none=True)))``
    to produce a valid OpenCode ``.md`` agent file.

    The system-prompt body is *not* a field here; it is passed separately as the
    ``Post`` body at install time.
    """

    description: str
    mode: Literal["all", "primary", "subagent"] = "all"
    # Flat {tool: "allow"|"deny"} covers all translator output. Widen to
    # Dict[str, Union[str, Dict[str, str]]] if granular per-command bash patterns
    # (e.g. bash: {"git status": "allow"}) are later needed.
    permission: Optional[Dict[str, str]] = None
