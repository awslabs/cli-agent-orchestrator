#!/usr/bin/env python3
"""Print the skill catalog exactly as the runtime providers receive it.

Mirrors the launch-time call in
``cli_agent_orchestrator/services/terminal_service.py`` for
``RUNTIME_SKILL_PROMPT_PROVIDERS`` (claude_code among them):
``build_skill_catalog(profile.skills)``. The profile is loaded with the same
loader cao-server uses, from the local agent store inside ``CAO_HOME_DIR`` —
run.sh installs the bundled profile there before calling this script, and
exports a temporary ``CAO_HOME_DIR`` so the operator's own profile store is
never read.

``profile.skills`` is passed through verbatim, exactly like production:
``None`` (no ``skills:`` key) means the full catalog, ``[]`` advertises
nothing, and a non-empty list filters by name pattern.

Exit codes: 0 the catalog block (possibly empty) was printed; 1 loading the
profile failed.
"""

from __future__ import annotations

import sys

from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.skills import build_skill_catalog

PROFILE_NAME = "skills_lifecycle_reviewer"


def main() -> int:
    try:
        profile = load_agent_profile(PROFILE_NAME)
    except Exception as exc:
        print(f"error: could not load profile '{PROFILE_NAME}': {exc}", file=sys.stderr)
        return 1
    print(build_skill_catalog(profile.skills))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
