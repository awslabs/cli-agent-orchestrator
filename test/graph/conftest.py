"""Shared fixtures for test/graph — registry isolation between tests."""

import pytest

from cli_agent_orchestrator.graph.providers import base as providers_base
from cli_agent_orchestrator.graph.providers import memory as memory_provider
from cli_agent_orchestrator.graph.sinks import base as sinks_base


@pytest.fixture(autouse=True)
def _isolate_graph_registries():
    """Snapshot and restore the provider/sink registries around every test.

    Registration is a module-level side effect (register_provider/register_sink
    decorators mutate _REGISTRY/_SINK_REGISTRY). Without teardown, names
    registered by one test leak into the next and a same-process rerun hits
    the duplicate-registration ValueError spuriously.
    """
    provider_snapshot = dict(providers_base._REGISTRY)
    sink_snapshot = dict(sinks_base._SINK_REGISTRY)
    yield
    providers_base._REGISTRY.clear()
    providers_base._REGISTRY.update(provider_snapshot)
    sinks_base._SINK_REGISTRY.clear()
    sinks_base._SINK_REGISTRY.update(sink_snapshot)


@pytest.fixture(autouse=True)
def _clear_graph_cache():
    """Drop the module-level MemoryGraphProvider cache around every test.

    The cache is keyed by (provider, scope, scope_id); most tests project
    ("memory","global",None), so without a reset one test's projection would
    be served to the next (and the stubbed run_lint of the previous test would
    win). Clear before AND after so ordering can't leak either direction.
    """
    memory_provider._CACHE.clear()
    yield
    memory_provider._CACHE.clear()
