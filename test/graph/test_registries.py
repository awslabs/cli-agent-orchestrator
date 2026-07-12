"""Tests for the GraphProvider/GraphSink registries (U1)."""

import pytest

from cli_agent_orchestrator.graph.models import GraphView, Node
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


class TestSinkQueryCapabilityGate:
    """Tests for GraphSink.query() capability gating (FR-5)."""

    def test_sink_without_query_capability_raises_not_implemented(self):
        @register_sink("test-sink-no-query")
        class NoQuerySink(GraphSink):
            def export(self, view, dest, **options):
                return [dest]

        sink = get_sink("test-sink-no-query")

        with pytest.raises(NotImplementedError):
            sink.query()

    def test_sink_declaring_query_capability_can_override(self):
        @register_sink("test-sink-with-query")
        class QuerySink(GraphSink):
            capabilities = {"query"}

            def export(self, view, dest, **options):
                return [dest]

            def query(self, *args, **kwargs):
                return "queried"

        sink = get_sink("test-sink-with-query")

        assert sink.query() == "queried"

    def test_sink_declaring_query_capability_without_override_raises_not_implemented(self):
        @register_sink("test-sink-declared-not-overridden")
        class DeclaredOnlySink(GraphSink):
            capabilities = {"query"}

            def export(self, view, dest, **options):
                return [dest]

        sink = get_sink("test-sink-declared-not-overridden")

        with pytest.raises(NotImplementedError, match="did not override"):
            sink.query()

    def test_sink_export_writes_and_returns_paths(self):
        @register_sink("test-sink-export")
        class ExportSink(GraphSink):
            def export(self, view, dest, **options):
                return [dest]

        sink = get_sink("test-sink-export")

        assert sink.export(GraphView(nodes=[], edges=[]), "out.json") == ["out.json"]


class TestProviderAsyncContract:
    """Tests exercising the async project() contract end-to-end (ADR-7)."""

    @pytest.mark.asyncio
    async def test_project_is_awaited_and_returns_empty_graph_view(self):
        @register_provider("test-provider-async")
        class EmptyProvider(GraphProvider):
            async def project(self, **filters):
                return GraphView(nodes=[], edges=[])

        provider = get_provider("test-provider-async")
        gv = await provider.project()

        assert gv.nodes == []
        assert gv.edges == []

    @pytest.mark.asyncio
    async def test_project_returns_provided_nodes(self):
        @register_provider("test-provider-async-nodes")
        class OneNodeProvider(GraphProvider):
            async def project(self, **filters):
                return GraphView(nodes=[Node(id="n1", kind="topic", label="Foo")], edges=[])

        provider = get_provider("test-provider-async-nodes")
        gv = await provider.project()

        assert [node.id for node in gv.nodes] == ["n1"]
