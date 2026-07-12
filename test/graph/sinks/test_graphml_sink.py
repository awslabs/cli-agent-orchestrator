"""U7 — GraphMLGraphSink tests: round-trip, no third-party XML, traversal, attrs."""

import ast
import os

import pytest

from cli_agent_orchestrator.graph.models import Edge, EdgeType, GraphView, Node
from cli_agent_orchestrator.graph.sinks import graphml as graphml_module
from cli_agent_orchestrator.graph.sinks.graphml import GraphMLGraphSink

networkx = pytest.importorskip("networkx", reason="networkx needed for GraphML round-trip")


def _traversal_dest(base) -> str:
    return os.path.join(str(base), *([".."] * 40), "etc", "cao-graphml-traversal.graphml")


def _view() -> GraphView:
    return GraphView(
        nodes=[
            Node(id="a", kind="topic", label="Alpha", attrs={"weight": 3}),
            Node(id="b", kind="topic", label="Beta"),
            Node(id="c", kind="topic", label="Gamma"),
        ],
        edges=[
            Edge(source="a", target="b", type=EdgeType.RELATES_TO),
            Edge(source="b", target="c", type=EdgeType.CONTRADICTION),
        ],
    )


def test_graphml_roundtrips_counts(tmp_path):
    """The .graphml parses with networkx and round-trips node/edge counts (AC-1)."""
    view = _view()
    dest = tmp_path / "graph.graphml"
    written = GraphMLGraphSink().export(view, str(dest))

    assert written == [str(dest)]
    assert dest.exists()

    g = networkx.read_graphml(str(dest))
    assert g.number_of_nodes() == len(view.nodes)
    assert g.number_of_edges() == len(view.edges)
    # A node's fixed <data> keys survive the round-trip.
    assert g.nodes["a"]["kind"] == "topic"
    assert g.nodes["a"]["label"] == "Alpha"


def test_graphml_no_third_party_xml_import():
    """graphml.py imports no third-party XML library (C-2).

    Parse the module's AST and assert every imported top-level module is
    either stdlib xml.* or an in-repo/stdlib module — never lxml/xmltodict.
    """
    src = ast.parse(open(graphml_module.__file__).read())
    imported: list[str] = []
    for node in ast.walk(src):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    forbidden = ("lxml", "xmltodict", "untangle", "xmlschema")
    for mod in imported:
        assert not any(mod == f or mod.startswith(f + ".") for f in forbidden), mod
    # Positive: the stdlib XML module is what's used.
    assert any(m.startswith("xml.etree") for m in imported)


def test_graphml_traversal_rejected(tmp_path):
    """A traversal dest is rejected before any file is written."""
    with pytest.raises(ValueError):
        GraphMLGraphSink().export(_view(), _traversal_dest(tmp_path))
    assert list(tmp_path.rglob("*.graphml")) == []


def test_graphml_non_native_attrs_stringified(tmp_path):
    """A non-native attrs value is json-stringified into the data text, not dropped."""
    view = GraphView(
        nodes=[Node(id="a", kind="topic", label="A", attrs={"nested": {"k": [1, 2, 3]}})],
        edges=[],
    )
    dest = tmp_path / "graph.graphml"
    GraphMLGraphSink().export(view, str(dest))

    g = networkx.read_graphml(str(dest))
    # The attrs map is preserved as a JSON string (data survives, not silently lost).
    attrs_text = g.nodes["a"]["attrs"]
    assert "nested" in attrs_text and "1" in attrs_text
