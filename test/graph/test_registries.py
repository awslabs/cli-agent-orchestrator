"""Tests for the GraphProvider/GraphSink registries (U1)."""

import pytest

from cli_agent_orchestrator.graph.models import GraphView
from cli_agent_orchestrator.graph.providers.base import (
    GraphProvider,
    get_provider,
    list_providers,
    register_provider,
)
from cli_agent_orchestrator.graph.sinks.base import GraphSink, get_sink, list_sinks, register_sink


class TestProviderRegistry:
    """Tests for register_provider/get_provider/list_providers."""

    def test_register_and_resolve_round_trip(self):
        @register_provider("test-provider-happy")
        class HappyProvider(GraphProvider):
            async def project(self, **filters):
                return GraphView(nodes=[], edges=[])

        resolved = get_provider("test-provider-happy")

        assert isinstance(resolved, HappyProvider)
        assert "test-provider-happy" in list_providers()

    def test_duplicate_registration_raises_value_error(self):
        @register_provider("test-provider-dup")
        class FirstProvider(GraphProvider):
            async def project(self, **filters):
                return GraphView(nodes=[], edges=[])

        with pytest.raises(ValueError):

            @register_provider("test-provider-dup")
            class SecondProvider(GraphProvider):
                async def project(self, **filters):
                    return GraphView(nodes=[], edges=[])

    def test_unregistered_name_raises_key_error(self):
        with pytest.raises(KeyError):
            get_provider("no-such-provider")


class TestSinkRegistry:
    """Tests for register_sink/get_sink/list_sinks."""

    def test_register_and_resolve_round_trip(self):
        @register_sink("test-sink-happy")
        class HappySink(GraphSink):
            def export(self, view, dest, **options):
                return [dest]

        resolved = get_sink("test-sink-happy")

        assert isinstance(resolved, HappySink)
        assert "test-sink-happy" in list_sinks()

    def test_duplicate_registration_raises_value_error(self):
        @register_sink("test-sink-dup")
        class FirstSink(GraphSink):
            def export(self, view, dest, **options):
                return [dest]

        with pytest.raises(ValueError):

            @register_sink("test-sink-dup")
            class SecondSink(GraphSink):
                def export(self, view, dest, **options):
                    return [dest]

    def test_unregistered_name_raises_key_error(self):
        with pytest.raises(KeyError):
            get_sink("no-such-sink")
