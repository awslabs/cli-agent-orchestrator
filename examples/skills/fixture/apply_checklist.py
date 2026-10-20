#!/usr/bin/env python3
"""Apply the installed skill's guidance — the example's fixture task.

This script stands in for an agent: it loads the skill through the same
retrieval function the ``load_skill`` MCP tool serves, parses the checklist
items out of the guidance body, and reports them as applied. Its contract line
is the example's observable proof that the skill engaged:

    Checklist applied: <applied>/<total> items

If the skill is not installed the load fails and the script exits non-zero —
that is the example's documented failure behaviour, and the reason the
contract line means anything when it does appear.

Exit codes: 0 checklist applied; 1 skill missing or unusable; 2 skill loaded
but contains no checklist items.
"""

from __future__ import annotations

import sys

from cli_agent_orchestrator.utils.skills import load_skill_content

SKILL_NAME = "python-review-checklist"


def main() -> int:
    try:
        content = load_skill_content(SKILL_NAME)
    except Exception as exc:
        print(f"error: skill '{SKILL_NAME}' is not available: {exc}", file=sys.stderr)
        return 1

    items = [line.strip() for line in content.splitlines() if line.strip().startswith("- [ ]")]
    if not items:
        print("error: skill body contains no checklist items", file=sys.stderr)
        return 2

    print(f"Checklist applied: {len(items)}/{len(items)} items")
    for item in items:
        print(f"  {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
