"""Graph sink seam: ABC and name-keyed registry."""

from cli_agent_orchestrator.graph.sinks.base import (
    GraphSink,
    get_sink,
    list_sinks,
    register_sink,
)

__all__ = ["GraphSink", "register_sink", "get_sink", "list_sinks"]
