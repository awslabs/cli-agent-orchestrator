"""Same-user local decision controls on the operations stdio surface only."""

from typing import Any, Callable

from cli_agent_orchestrator.decisions import operator, settings


def _result(action: Callable[[], Any]) -> dict[str, Any]:
    try:
        return {"success": True, "data": action()}
    except ValueError as error:
        return {"success": False, "message": str(error)}
    except Exception:
        return {
            "success": False,
            "message": "Decision operation failed; check local settings and database",
        }


def register_decision_tools(mcp: Any) -> None:
    @mcp.tool
    def decisions_status() -> dict[str, Any]:
        """Read effective local decision settings."""
        return _result(operator.status)

    @mcp.tool
    def decisions_set_point(point: str, state: str, decider: str | None = None) -> dict[str, Any]:
        """Set a point's state and installed decider name."""
        return _result(lambda: settings.set_point(point, state, decider))

    @mcp.tool
    def decisions_set_tier(provider: str, tier: str, model: str | None = None) -> dict[str, Any]:
        """Set a tier mapping; omit model to unset it."""
        return _result(lambda: settings.set_tier(provider, tier, model))

    @mcp.tool
    def decisions_set_exclusions(
        add: str | None = None, remove: str | None = None
    ) -> dict[str, Any]:
        """Add or remove one pinned installed profile."""
        return _result(lambda: settings.set_exclusions(add=add, remove=remove))

    @mcp.tool
    def decisions_list(
        point: str | None = None, since: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        """List content-free decision records."""
        return _result(lambda: operator.records(point=point, since=since, limit=limit))

    @mcp.tool
    def decisions_purge(
        before: str | None = None, all_records: bool = False, rotate_key: bool = False
    ) -> dict[str, Any]:
        """Purge by cutoff or all, optionally rotating the message hash key."""
        return _result(
            lambda: operator.purge(before=before, all_records=all_records, rotate=rotate_key)
        )
