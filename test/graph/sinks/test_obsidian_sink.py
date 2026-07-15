"""U6 — ObsidianGraphSink tests: happy path, export-root confinement, escaping."""

import os

import pytest
import yaml

from cli_agent_orchestrator.graph.models import Edge, EdgeType, GraphView, Node
from cli_agent_orchestrator.graph.sinks.obsidian import ObsidianGraphSink


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
            Edge(source="a", target="c", type=EdgeType.CONTRADICTION),
        ],
    )


def test_obsidian_export_vault(export_root):
    """A small view exports N notes with wikilinks; contradiction edge suffixed."""
    written = ObsidianGraphSink().export(_view(), "vault")

    dest = os.path.join(export_root, "vault")
    notes = sorted(os.path.basename(p) for p in written)
    assert notes == ["a.md", "b.md", "c.md"]
    assert len(written) == 3
    assert not os.path.exists(os.path.join(dest, ".obsidian"))
    for p in written:
        assert os.path.realpath(p).startswith(export_root + os.sep)

    a_text = open(os.path.join(dest, "a.md"), encoding="utf-8").read()
    assert "## Links" in a_text
    assert "- [[b]]" in a_text
    assert "- [[c]] (contradiction)" in a_text

    front = a_text.split("---")[1]
    parsed = yaml.safe_load(front)
    assert parsed["kind"] == "topic"
    assert parsed["status"] == "active"
    assert parsed["attrs"] == {"weight": 3}

    b_text = open(os.path.join(dest, "b.md"), encoding="utf-8").read()
    assert "## Links" not in b_text


def test_obsidian_relative_traversal_escape_rejected(export_root):
    """A relative dest escaping the export root is rejected; nothing written."""
    with pytest.raises(ValueError):
        ObsidianGraphSink().export(_view(), os.path.join("..", "..", "etc", "cao-obs-escape"))
    assert list(_all_files(export_root)) == []


def test_obsidian_absolute_dest_outside_root_rejected(export_root, tmp_path):
    """An absolute dest outside the root -> ValueError, writes nothing (MUST-FIX #2)."""
    outside = tmp_path / "outside-root" / "vault"
    with pytest.raises(ValueError):
        ObsidianGraphSink().export(_view(), str(outside))
    assert not outside.exists()
    assert list(_all_files(export_root)) == []


def test_obsidian_unsafe_label_sanitized(export_root):
    """A node id with filename-unsafe chars sanitizes to a writable file, no crash."""
    view = GraphView(nodes=[Node(id="a/b:c", kind="topic", label="Weird a/b:c")], edges=[])
    written = ObsidianGraphSink().export(view, "vault")

    assert len(written) == 1
    name = os.path.basename(written[0])
    assert name.endswith(".md")
    assert "/" not in name and ":" not in name
    assert os.path.exists(written[0])


def test_obsidian_filename_collision_raises(export_root):
    """Two distinct ids slugging to the same filename raise ValueError."""
    view = GraphView(
        nodes=[
            Node(id="a/b", kind="topic", label="One"),
            Node(id="a:b", kind="topic", label="Two"),
        ],
        edges=[],
    )
    with pytest.raises(ValueError, match="collision"):
        ObsidianGraphSink().export(view, "vault")


def test_obsidian_label_injection_escaped(export_root):
    """A label with a newline heading + link chars is emitted safely (S3)."""
    view = GraphView(
        nodes=[Node(id="n1", kind="topic", label="Safe\n# Injected\n[[evil]]")],
        edges=[],
    )
    written = ObsidianGraphSink().export(view, "vault")

    text = open(written[0], encoding="utf-8").read()
    h1 = next(line for line in text.splitlines() if line.startswith("# "))
    assert "Injected" in h1  # collapsed onto the H1, not a standalone heading
    # No injected heading or wikilink on its own line in the body.
    assert "\n# Injected" not in text
    assert "[[evil]]" not in text  # brackets escaped


def _all_files(root):
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            yield os.path.join(dirpath, f)
