#!/usr/bin/env python3
"""Validate maintained local Markdown links from the repository root."""

from __future__ import annotations

import sys
from pathlib import Path

from cli_agent_orchestrator.utils.markdown_links import (
    format_errors,
    validate_markdown_links,
)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    errors = validate_markdown_links(repo_root)
    if errors:
        print(format_errors(errors), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
