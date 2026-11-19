"""Reversible patches to Starlette application state."""

from typing import Any

from starlette.applications import Starlette


def app_state_items(app: Starlette) -> dict[str, Any]:
    # State attributes live here, not in the instance __dict__ that pytest restores.
    return app.state._state
