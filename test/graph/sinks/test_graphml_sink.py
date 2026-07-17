"""U7 — GraphMLGraphSink tests: round-trip, no third-party XML, export-root confinement."""

import ast
import os

import pytest

from cli_agent_orchestrator.graph.models import Edge, EdgeType, GraphView, Node
from cli_agent_orchestrator.graph.sinks import graphml as graphml_module
from cli_agent_orchestrator.graph.sinks.graphml import GraphMLGraphSink

networkx = pytest.importorskip("networkx", reason="networkx needed for GraphML round-trip")


@pytest.fixture
def export_root(tmp_path, monkeypatch):
    """Point CAO_GRAPH_EXPORT_ROOT at a tmp dir; return its realpath."""
    root = tmp_path / "export-root"
    root.mkdir()
    monkeypatch.setenv("CAO_GRAPH_EXPORT_ROOT", str(root))
    return os.path.realpath(str(root))


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


def test_graphml_roundtrips_counts(export_root):
    """The .graphml parses with networkx and round-trips node/edge counts (AC-1)."""
    view = _view()
    written = GraphMLGraphSink().export(view, "graph.graphml")

    dest = os.path.join(export_root, "graph.graphml")
    assert [os.path.realpath(p) for p in written] == [os.path.realpath(dest)]
    assert os.path.exists(dest)
    assert os.path.realpath(written[0]).startswith(export_root + os.sep)

    g = networkx.read_graphml(dest)
    assert g.number_of_nodes() == len(view.nodes)
    assert g.number_of_edges() == len(view.edges)
    assert g.nodes["a"]["kind"] == "topic"
    assert g.nodes["a"]["label"] == "Alpha"


def test_graphml_subdir_dest_confined(export_root):
    """A relative dest with a subdir is created under the root."""
    written = GraphMLGraphSink().export(_view(), os.path.join("sub", "graph.graphml"))
    assert os.path.realpath(written[0]).startswith(export_root + os.sep)
    assert os.path.exists(os.path.join(export_root, "sub", "graph.graphml"))


def test_graphml_no_third_party_xml_import():
    """graphml.py imports no third-party XML library (C-2)."""
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
    assert any(m.startswith("xml.etree") for m in imported)


def test_graphml_relative_traversal_escape_rejected(export_root):
    """A relative dest escaping the export root is rejected before any write."""
    with pytest.raises(ValueError):
        GraphMLGraphSink().export(
            _view(), os.path.join("..", "..", "etc", "cao-graphml-escape.graphml")
        )
    assert list(_all_files(export_root)) == []


def test_graphml_absolute_overwrite_outside_root_rejected(export_root, tmp_path):
    """An absolute dest at an existing sensitive-looking file OUTSIDE the root is rejected.

    Regression guard for MUST-FIX #2: with allow_file=True the old blocklist
    let an authed writer overwrite nearly any existing file (e.g.
    ~/.ssh/authorized_keys). Confinement rejects it before any write, so the
    victim file's bytes are untouched.
    """
    victim = tmp_path / "victim" / "authorized_keys"
    victim.parent.mkdir()
    victim.write_text("ORIGINAL", encoding="utf-8")

    with pytest.raises(ValueError):
        GraphMLGraphSink().export(_view(), str(victim))

    # The out-of-root file was never touched.
    assert victim.read_text(encoding="utf-8") == "ORIGINAL"
    assert list(_all_files(export_root)) == []


def test_graphml_non_native_attrs_stringified(export_root):
    """A non-native attrs value is json-stringified into the data text, not dropped."""
    view = GraphView(
        nodes=[Node(id="a", kind="topic", label="A", attrs={"nested": {"k": [1, 2, 3]}})],
        edges=[],
    )
    written = GraphMLGraphSink().export(view, "graph.graphml")

    g = networkx.read_graphml(written[0])
    attrs_text = g.nodes["a"]["attrs"]
    assert "nested" in attrs_text and "1" in attrs_text


def _all_files(root):
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            yield os.path.join(dirpath, f)
