"""U5 — OkfGraphSink tests: happy path, traversal rejection, missing-attrs edge."""

import os

import pytest

from cli_agent_orchestrator.graph.models import Edge, EdgeType, GraphView, Node
from cli_agent_orchestrator.graph.providers.stub import StubGraphProvider
from cli_agent_orchestrator.graph.sinks.okf import OkfGraphSink


def _traversal_dest(base) -> str:
    """A path that, after realpath normalization, escapes into a blocked dir.

    Enough ``..`` segments to walk past the filesystem root (which clamps at
    ``/``) then into ``/etc`` — whose nearest existing ancestor is a blocked
    system directory, so resolve_and_validate_path raises ValueError.
    """
    return os.path.join(str(base), *([".."] * 40), "etc", "cao-okf-traversal")


@pytest.mark.asyncio
async def test_okf_export_stub_bundle(tmp_path):
    """Exporting the stub provider's view produces a well-formed OKF bundle."""
    view = await StubGraphProvider().project()
    dest = tmp_path / "bundle"

    written = OkfGraphSink().export(view, str(dest))

    # index.md + manifest.md + one .md per node.
    assert (dest / "index.md").exists()
    assert (dest / "manifest.md").exists()
    node_files = sorted(p.name for p in dest.glob("*.md"))
    assert node_files == ["index.md", "manifest.md", "stub-a.md", "stub-b.md", "stub-c.md"]
    assert len(written) == len(view.nodes) + 2

    # index is deterministic: node links appear in slug-sorted order.
    index_text = (dest / "index.md").read_text(encoding="utf-8")
    assert (
        index_text.index("stub-a.md")
        < index_text.index("stub-b.md")
        < index_text.index("stub-c.md")
    )
    # The edge stub-a -> stub-b surfaces as a See Also wikilink in stub-a's note.
    assert "[[stub-b]]" in (dest / "stub-a.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_okf_bundle_shape_matches_across_providers(tmp_path):
    """The stub bundle is structurally identical in FORM to any GraphView bundle.

    A hand-built view with the same node/edge counts yields the same set of
    bundle files (per-node .md + index.md + manifest.md) — the sink is
    provider-agnostic (AC-1).
    """
    stub_view = await StubGraphProvider().project()
    other_view = GraphView(
        nodes=[Node(id=f"topic-{i}", kind="topic", label=f"T{i}") for i in range(3)],
        edges=[Edge(source="topic-0", target="topic-1", type=EdgeType.RELATES_TO)],
    )

    a = OkfGraphSink().export(stub_view, str(tmp_path / "a"))
    b = OkfGraphSink().export(other_view, str(tmp_path / "b"))

    # Same file COUNT and same reserved-file structure.
    assert len(a) == len(b)
    assert {os.path.basename(p) for p in a} >= {"index.md", "manifest.md"}
    assert {os.path.basename(p) for p in b} >= {"index.md", "manifest.md"}


def test_okf_traversal_rejected_before_write(tmp_path):
    """A dest resolving into a blocked system dir raises ValueError, writes nothing."""
    view = GraphView(nodes=[Node(id="n1", kind="stub", label="N1")], edges=[])
    dest = _traversal_dest(tmp_path)

    with pytest.raises(ValueError):
        OkfGraphSink().export(view, dest)

    # Nothing landed anywhere under the (allowed) tmp base.
    assert list(tmp_path.rglob("*.md")) == []


def test_okf_node_without_attrs_does_not_crash(tmp_path):
    """A node missing optional attrs keys exports cleanly (no Attributes section)."""
    view = GraphView(nodes=[Node(id="bare", kind="stub", label="Bare")], edges=[])
    dest = tmp_path / "bundle"

    OkfGraphSink().export(view, str(dest))

    text = (dest / "bare.md").read_text(encoding="utf-8")
    assert "# Bare" in text
    assert "## Attributes" not in text  # empty attrs -> section omitted
