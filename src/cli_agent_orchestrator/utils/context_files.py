"""Utilities for agent context files (frontmatter-backed)."""

from pathlib import Path
from typing import Optional

import frontmatter

from cli_agent_orchestrator.constants import AGENT_CONTEXT_DIR


def write_context_with_provider(source_file: Path, provider: str, dest_file: Path) -> None:
    """Copy a context file to destination and set provider in frontmatter."""
    post = frontmatter.loads(source_file.read_text())
    post.metadata["provider"] = provider
    dest_file.write_text(frontmatter.dumps(post))


def get_context_provider(agent_profile: str) -> Optional[str]:
    """Return provider from agent context frontmatter if present."""
    file_path = AGENT_CONTEXT_DIR / f"{agent_profile}.md"
    if not file_path.exists():
        return None

    try:
        post = frontmatter.loads(file_path.read_text())
        provider = post.metadata.get("provider")
        if isinstance(provider, str):
            return provider
    except Exception:
        return None

    return None
