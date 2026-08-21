"""U5 — OkfGraphSink tests: happy path, export-root confinement, collisions, escaping."""

import os

import pytest

from cli_agent_orchestrator.graph.models import Edge, EdgeType, GraphView, Node
from cli_agent_orchestrator.graph.providers.stub import StubGraphProvider
from cli_agent_orchestrator.graph.sinks.okf import OkfGraphSink


@pytest.fixture
def export_root(tmp_path, monkeypatch):
    """Point CAO_GRAPH_EXPORT_ROOT at a tmp dir; return its realpath.

    Every sink now confines dest under this root, so tests must configure it
    (default lives under ~/.aws/... which we never touch in tests).
    """
    root = tmp_path / "export-root"
    root.mkdir()
    monkeypatch.setenv("CAO_GRAPH_EXPORT_ROOT", str(root))
    return os.path.realpath(str(root))


@pytest.mark.asyncio
async def test_okf_export_stub_bundle(export_root):
    """Exporting the stub provider's view produces a well-formed OKF bundle.

    dest is RELATIVE to the configured export root; every written path stays
    under the resolved root.
    """
    view = await StubGraphProvider().project()

    written = OkfGraphSink().export(view, "bundle")

    dest = os.path.join(export_root, "bundle")
    # index.md + manifest.md + one .md per node, all under the root.
    assert os.path.exists(os.path.join(dest, "index.md"))
    assert os.path.exists(os.path.join(dest, "manifest.md"))
    node_files = sorted(os.path.basename(p) for p in written)
    assert node_files == ["index.md", "manifest.md", "stub-a.md", "stub-b.md", "stub-c.md"]
    assert len(written) == len(view.nodes) + 2
    for p in written:
        assert os.path.realpath(p).startswith(export_root + os.sep)

    index_text = open(os.path.join(dest, "index.md"), encoding="utf-8").read()
    assert (
        index_text.index("stub-a.md")
        < index_text.index("stub-b.md")
        < index_text.index("stub-c.md")
    )
    assert "[[stub-b]]" in open(os.path.join(dest, "stub-a.md"), encoding="utf-8").read()


@pytest.mark.asyncio
async def test_okf_bundle_shape_matches_across_providers(export_root):
    """The stub bundle is structurally identical in FORM to any GraphView bundle."""
    stub_view = await StubGraphProvider().project()
    other_view = GraphView(
        nodes=[Node(id=f"topic-{i}", kind="topic", label=f"T{i}") for i in range(3)],
        edges=[Edge(source="topic-0", target="topic-1", type=EdgeType.RELATES_TO)],
    )

    a = OkfGraphSink().export(stub_view, "a")
    b = OkfGraphSink().export(other_view, "b")

    assert len(a) == len(b)
    assert {os.path.basename(p) for p in a} >= {"index.md", "manifest.md"}
    assert {os.path.basename(p) for p in b} >= {"index.md", "manifest.md"}


def test_okf_relative_traversal_escape_rejected(export_root):
    """A relative dest that escapes the export root -> ValueError, writes nothing."""
    view = GraphView(nodes=[Node(id="n1", kind="stub", label="N1")], edges=[])

    with pytest.raises(ValueError):
        OkfGraphSink().export(view, os.path.join("..", "..", "etc", "cao-okf-escape"))

    # Nothing landed under the root.
    assert list(_all_files(export_root)) == []


def test_okf_absolute_dest_outside_root_rejected(export_root, tmp_path):
    """An ABSOLUTE dest outside the export root -> ValueError, writes nothing.

    Regression guard for MUST-FIX #2: the old resolve_and_validate_path
    blocklist let ~/.ssh-style paths through; confinement rejects any absolute
    path not already under the configured root.
    """
    view = GraphView(nodes=[Node(id="n1", kind="stub", label="N1")], edges=[])
    outside = tmp_path / "outside-root" / "bundle"

    with pytest.raises(ValueError):
        OkfGraphSink().export(view, str(outside))

    assert not outside.exists()
    assert list(_all_files(export_root)) == []


def test_okf_absolute_dest_under_root_accepted(export_root):
    """An ABSOLUTE dest that already resolves under the root is accepted."""
    view = GraphView(nodes=[Node(id="n1", kind="stub", label="N1")], edges=[])
    under = os.path.join(export_root, "abs-bundle")

    written = OkfGraphSink().export(view, under)

    assert written
    for p in written:
        assert os.path.realpath(p).startswith(export_root + os.sep)


def test_okf_node_without_attrs_does_not_crash(export_root):
    """A node missing optional attrs keys exports cleanly (no Attributes section)."""
    view = GraphView(nodes=[Node(id="bare", kind="stub", label="Bare")], edges=[])

    written = OkfGraphSink().export(view, "bundle")

    bare = next(p for p in written if os.path.basename(p) == "bare.md")
    text = open(bare, encoding="utf-8").read()
    assert "# Bare" in text
    assert "## Attributes" not in text


@pytest.mark.parametrize("reserved", ["index", "manifest"])
def test_okf_reserved_stem_collision_raises(export_root, reserved):
    """A node id slugging to a reserved bundle stem (index/manifest) -> ValueError (S1).

    Previously the node's .md was silently clobbered by the generated
    index.md/manifest.md; now it fails loudly like the Obsidian collision.
    """
    view = GraphView(nodes=[Node(id=reserved, kind="stub", label="X")], edges=[])
    with pytest.raises(ValueError, match="reserved"):
        OkfGraphSink().export(view, "bundle")


def test_okf_same_stem_collision_raises(export_root):
    """Two ids slugging to the same stem -> ValueError (S1), not last-write-wins."""
    view = GraphView(
        nodes=[
            Node(id="a/b", kind="stub", label="One"),
            Node(id="a:b", kind="stub", label="Two"),  # both slug to 'a-b'
        ],
        edges=[],
    )
    with pytest.raises(ValueError, match="collision"):
        OkfGraphSink().export(view, "bundle")


def test_okf_label_injection_escaped(export_root):
    """A label with a newline + markdown-structural chars is emitted safely (S3)."""
    view = GraphView(
        nodes=[Node(id="n1", kind="stub", label="Safe\n# Injected heading\n](evil)")],
        edges=[],
    )
    written = OkfGraphSink().export(view, "bundle")

    node_file = next(p for p in written if os.path.basename(p) == "n1.md")
    text = open(node_file, encoding="utf-8").read()
    # The H1 line carries the whole (escaped, single-line) label; no injected
    # heading appears on its own line, and the link-opening chars are escaped.
    h1 = next(line for line in text.splitlines() if line.startswith("# "))
    assert "Injected heading" in h1  # collapsed onto the heading line, not a new one
    assert "\n# Injected heading" not in text
    assert "](evil)" not in text  # the ] ( ) were backslash-escaped


def _all_files(root):
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            yield os.path.join(dirpath, f)
