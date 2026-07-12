"""Graph sink seam: ABC, name-keyed registry, and built-in sinks."""

from cli_agent_orchestrator.graph.sinks.base import (
    GraphSink,
    get_sink,
    list_sinks,
    register_sink,
)

# Import-time registration: importing this package registers the built-in
# sinks ("okf", "obsidian", "graphml") via their @register_sink decorators.
from cli_agent_orchestrator.graph.sinks.graphml import GraphMLGraphSink
from cli_agent_orchestrator.graph.sinks.obsidian import ObsidianGraphSink
from cli_agent_orchestrator.graph.sinks.okf import OkfGraphSink

__all__ = [
    "GraphSink",
    "register_sink",
    "get_sink",
    "list_sinks",
    "OkfGraphSink",
    "ObsidianGraphSink",
    "GraphMLGraphSink",
]
