"""Every MCP tool's ``engine`` field describes the same values (#1167 Phase 1)."""

import asyncio

from cli_agent_orchestrator.mcp_server import server

ENGINE_DESCRIPTION = "Explicit Kiro engine for the worker (v2 or v3; kas is an alias of v3)"


def test_every_mcp_engine_field_names_v3_and_the_kas_alias():
    tools = asyncio.run(server.mcp._list_tools())
    engines = {
        tool.name: tool.parameters["properties"]["engine"]
        for tool in tools
        if "engine" in tool.parameters.get("properties", {})
    }
    assert {"handoff", "assign", "assign_elastic"} <= set(engines)
    for name, field in engines.items():
        assert field.get("description") == ENGINE_DESCRIPTION, name
