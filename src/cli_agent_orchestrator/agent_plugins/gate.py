"""Ship-gate for the agent-plugin management surface (Requirement 16.5).

Requirement 16.5 says the management surface SHALL NOT ship to end users until
maintainer decision M1 (the naming question) is settled. Omitting the Click group
from ``cao --help`` satisfied "not advertised" but not "does not execute" — Click's
``hidden=True`` suppresses help text only, and the HTTP routes were unconditional.
This module is the execution gate: one predicate that the API routes and the CLI
group both consult, so they cannot disagree about whether the surface is live.

Modelled on the AG-UI precedent (``services/agui_enablement.py`` plus
``api.main._require_agui_enabled``): default-off, single env var, same truthy
spelling as ``CAO_AGUI_ENABLED`` and ``CAO_EAGER_INBOX_DELIVERY``. With the flag
unset the surface is absent — the routes 404 and the CLI group refuses to run any
subcommand.

Env-only on purpose. This is a release gate, not a user preference, so there is
deliberately no ``settings.json`` knob: a setting would invite operators to
persist an opt-in to a surface whose public naming is still unresolved. Read at
call time rather than import time so tests (and an operator exporting the
variable into an already-running shell) see the current value.

This gate carries no opinion about the verb or the route paths — resolving M1 is
a separate change that flips the default here and drops the guard.
"""

from __future__ import annotations

import os

#: Environment variable that enables the agent-plugin management surface.
ENV_VAR = "CAO_AGENT_PLUGINS_ENABLED"

_TRUTHY = ("1", "true", "yes")


def agent_plugins_surface_enabled() -> bool:
    """Return whether the agent-plugin management surface should be live.

    Default-off: only an explicitly truthy ``CAO_AGENT_PLUGINS_ENABLED`` opens it.
    """

    return os.environ.get(ENV_VAR, "").strip().lower() in _TRUTHY
