#!/usr/bin/env python3
"""Print the installed skill's full Markdown body.

This is the same retrieval path the ``load_skill`` MCP tool serves: the tool
(``cli_agent_orchestrator/mcp_server/server.py`` ``_load_skill_impl``) fetches
``GET /skills/{name}`` from cao-server, and the server-side handler returns
``load_skill_content(name)`` from ``cli_agent_orchestrator.utils.skills``.
Without a running cao-server this script exercises that same function
directly, which is why run.sh's deterministic (server-free) mode can prove
the complete body is retrievable.

Exit codes: 0 the body was printed; 1 the skill is missing or cannot load.
"""

from __future__ import annotations

import sys

from cli_agent_orchestrator.utils.skills import load_skill_content

SKILL_NAME = "python-review-checklist"


def main() -> int:
    try:
        print(load_skill_content(SKILL_NAME))
    except Exception as exc:
        print(f"error: could not load skill '{SKILL_NAME}': {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
