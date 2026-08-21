"""Tests for graph.models: Node, Edge, GraphView contract (U1)."""

import pytest
from pydantic import ValidationError

from cli_agent_orchestrator.graph.models import Edge, EdgeType, GraphView, Node, NodeStatus


class TestNode:
    """Tests for the Node model."""

    def test_constructs_with_valid_fields(self):
        node = Node(id="topic:foo", kind="topic", label="Foo")

        assert node.id == "topic:foo"
        assert node.kind == "topic"
        assert node.label == "Foo"
        assert node.status == NodeStatus.ACTIVE
        assert node.attrs == {}

    @pytest.mark.parametrize(
        "kind",
        ["", "Topic", "topic-name", "1topic", "topic name", "topic.", "topic\n"],
    )
    def test_rejects_invalid_kind_shape(self, kind):
        with pytest.raises(ValidationError):
            Node(id="n1", kind=kind, label="Foo")

    def test_rejects_invalid_status_literal(self):
        with pytest.raises(ValidationError):
            Node(id="n1", kind="topic", label="Foo", status="deleted")

    def test_accepts_all_defined_status_values(self):
        for status in NodeStatus:
            node = Node(id="n1", kind="topic", label="Foo", status=status)
            assert node.status == status


class TestEdge:
    """Tests for the Edge model."""

    def test_constructs_with_valid_fields(self):
        edge = Edge(source="n1", target="n2", type=EdgeType.RELATES_TO)

        assert edge.source == "n1"
        assert edge.target == "n2"
        assert edge.type == EdgeType.RELATES_TO
        assert edge.attrs == {}

    def test_rejects_unrecognized_type_literal(self):
        with pytest.raises(ValidationError):
            Edge(source="n1", target="n2", type="unknown_type")


class TestGraphView:
    """Tests for the GraphView model."""

    def test_constructs_with_nodes_and_edges_and_serializes(self):
        view = GraphView(
            nodes=[
                Node(id="n1", kind="topic", label="Foo"),
                Node(id="n2", kind="topic", label="Bar"),
            ],
            edges=[Edge(source="n1", target="n2", type=EdgeType.RELATES_TO)],
            meta={"provider": "stub"},
        )

        result = view.to_dict()

        assert result["nodes"] == [
            {"id": "n1", "kind": "topic", "label": "Foo", "status": "active", "attrs": {}},
            {"id": "n2", "kind": "topic", "label": "Bar", "status": "active", "attrs": {}},
        ]
        assert result["edges"] == [
            {"source": "n1", "target": "n2", "type": "relates_to", "attrs": {}}
        ]
        assert result["meta"] == {"provider": "stub"}

    def test_constructs_empty_view(self):
        view = GraphView(nodes=[], edges=[])

        assert view.to_dict() == {"nodes": [], "edges": [], "meta": {}}

    def test_rejects_edge_with_dangling_source(self):
        with pytest.raises(ValidationError):
            GraphView(
                nodes=[Node(id="n1", kind="topic", label="Foo")],
                edges=[Edge(source="missing", target="n1", type=EdgeType.RELATES_TO)],
            )

    def test_rejects_edge_with_dangling_target(self):
        with pytest.raises(ValidationError):
            GraphView(
                nodes=[Node(id="n1", kind="topic", label="Foo")],
                edges=[Edge(source="n1", target="missing", type=EdgeType.RELATES_TO)],
            )

    def test_rejects_duplicate_node_ids(self):
        with pytest.raises(ValidationError):
            GraphView(
                nodes=[
                    Node(id="n1", kind="topic", label="Foo"),
                    Node(id="n1", kind="topic", label="Bar"),
                ],
                edges=[],
            )

    def test_to_dict_serializes_non_default_status_and_populated_attrs(self):
        view = GraphView(
            nodes=[
                Node(
                    id="n1",
                    kind="topic",
                    label="Foo",
                    status=NodeStatus.PROPOSAL,
                    attrs={"weight": 3},
                ),
            ],
            edges=[],
            meta={"generated_by": "stub"},
        )

        result = view.to_dict()

        assert result["nodes"] == [
            {
                "id": "n1",
                "kind": "topic",
                "label": "Foo",
                "status": "proposal",
                "attrs": {"weight": 3},
            }
        ]
        assert result["meta"] == {"generated_by": "stub"}
