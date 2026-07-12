"""U6 — ObsidianGraphSink tests: happy path, traversal rejection, unsafe-label edge."""

import os

import pytest
import yaml

from cli_agent_orchestrator.graph.models import Edge, EdgeType, GraphView, Node
from cli_agent_orchestrator.graph.sinks.obsidian import ObsidianGraphSink


def _traversal_dest(base) -> str:
    return os.path.join(str(base), *([".."] * 40), "etc", "cao-obsidian-traversal")


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


def test_obsidian_export_vault(tmp_path):
    """A small view exports N notes with wikilinks; contradiction edge suffixed."""
    dest = tmp_path / "vault"
    written = ObsidianGraphSink().export(_view(), str(dest))

    notes = sorted(p.name for p in dest.glob("*.md"))
    assert notes == ["a.md", "b.md", "c.md"]
    assert len(written) == 3
    # No .obsidian/ config directory is written.
    assert not (dest / ".obsidian").exists()

    a_text = (dest / "a.md").read_text(encoding="utf-8")
    assert "## Links" in a_text
    assert "- [[b]]" in a_text
    assert "- [[c]] (contradiction)" in a_text

    # Frontmatter is valid YAML round-trips (PyYAML, not hand-rolled).
    front = a_text.split("---")[1]
    parsed = yaml.safe_load(front)
    assert parsed["kind"] == "topic"
    assert parsed["status"] == "active"
    assert parsed["attrs"] == {"weight": 3}

    # A node with no outgoing edges omits the Links heading entirely.
    b_text = (dest / "b.md").read_text(encoding="utf-8")
    assert "## Links" not in b_text


def test_obsidian_traversal_rejected(tmp_path):
    """A traversal dest is rejected via the shared path_validation utility."""
    with pytest.raises(ValueError):
        ObsidianGraphSink().export(_view(), _traversal_dest(tmp_path))
    assert list(tmp_path.rglob("*.md")) == []


def test_obsidian_unsafe_label_sanitized(tmp_path):
    """A node id with filename-unsafe chars sanitizes to a writable file, no crash."""
    view = GraphView(
        nodes=[Node(id="a/b:c", kind="topic", label="Weird a/b:c")],
        edges=[],
    )
    dest = tmp_path / "vault"
    written = ObsidianGraphSink().export(view, str(dest))

    assert len(written) == 1
    # The slug collapses unsafe chars; the file exists and has no path separators.
    name = os.path.basename(written[0])
    assert name.endswith(".md")
    assert "/" not in name and ":" not in name
    assert os.path.exists(written[0])


def test_obsidian_filename_collision_raises(tmp_path):
    """Two distinct ids slugging to the same filename raise ValueError."""
    view = GraphView(
        nodes=[
            Node(id="a/b", kind="topic", label="One"),
            Node(id="a:b", kind="topic", label="Two"),  # slugs to the same 'a-b'
        ],
        edges=[],
    )
    with pytest.raises(ValueError, match="collision"):
        ObsidianGraphSink().export(view, str(tmp_path / "vault"))
