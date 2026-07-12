"""OKF (Obsidian Knowledge Format) GraphSink (U5, Issue #348).

Generalizes the #345 memory-export bundle shape
(``services/memory_archive/okf.py``) to an arbitrary GraphView: one
markdown file per node plus a deterministic ``index.md`` and a
``manifest.md`` provenance note. The byte-compare-before-write discipline
(``_write_if_changed``) is adopted as a PATTERN here — this module does
NOT import the memory-archive exporter.

Security contract: ``dest`` is confined via
``utils.path_validation.resolve_and_validate_path`` (ADR: sink-side
defense in depth, even though the U4 route validates first). No
``secret_gate`` call inside ``export()`` — the route scans the serialized
view before dispatch (ADR-5); the sink assumes clean content.
"""

import json
import re
from pathlib import Path
from typing import Any

from cli_agent_orchestrator.graph.models import Edge, GraphView, Node
from cli_agent_orchestrator.graph.sinks.base import GraphSink, register_sink
from cli_agent_orchestrator.utils.path_validation import resolve_and_validate_path

# Filesystem-unsafe characters collapsed to '-' so a node id/label maps to a
# stable, portable filename. Empty results fall back to a fixed token.
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value: str) -> str:
    """Map an arbitrary node id to a safe, deterministic filename stem.

    Collapses runs of filesystem-unsafe characters to a single '-', strips
    leading/trailing separators, and falls back to "node" for a value that
    reduces to empty (so a label like "///" still yields a writable file).
    """
    slug = _UNSAFE_CHARS.sub("-", value).strip("-._")
    return slug or "node"


@register_sink("okf")
class OkfGraphSink(GraphSink):
    """Export a GraphView as an OKF-shaped markdown bundle.

    Structurally identical in FORM regardless of provider: the stub
    provider's GraphView produces the same bundle layout (per-node ``.md``
    + ``index.md`` + ``manifest.md``) as the memory provider's.
    """

    def export(self, view: GraphView, dest: str, **options: Any) -> list[str]:
        # Defense in depth: confine dest even though U4 already validated it.
        # dest is a DIRECTORY (allow_file defaults to False); traversal /
        # symlink / blocked-system-path -> ValueError -> route maps to 400.
        dest_real = resolve_and_validate_path(
            dest, allow_create=True, description="OKF export destination"
        )
        dest_dir = Path(dest_real)
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Group outgoing edges by source id for the per-node "See Also"
        # section — a single pass so a large edge list is not re-scanned per node.
        outgoing: dict[str, list[Edge]] = {}
        for edge in view.edges:
            outgoing.setdefault(edge.source, []).append(edge)

        written: list[str] = []

        # One markdown file per node. Sorted by slug so a same-content view
        # always writes files in the same order (deterministic output).
        #
        # NOTE: unlike the Obsidian sink, OKF does NOT guard against two ids
        # slugging to the same stem (last write wins). This asymmetry is
        # deliberate: OKF's AC (U6) does not mandate collision detection —
        # only the Obsidian sink's does. Left as-is by design, not oversight.
        for node in sorted(view.nodes, key=lambda n: _slug(n.id)):
            filename = _slug(node.id) + ".md"
            content = self._render_node(node, outgoing.get(node.id, []))
            path = dest_dir / filename
            self._write_if_changed(path, content)
            written.append(str(path))

        index_path = dest_dir / "index.md"
        self._write_if_changed(index_path, self._render_index(view.nodes))
        written.append(str(index_path))

        manifest_path = dest_dir / "manifest.md"
        self._write_if_changed(manifest_path, self._render_manifest(view))
        written.append(str(manifest_path))

        return written

    @staticmethod
    def _render_node(node: Node, edges: list[Edge]) -> str:
        """Serialize one node: frontmatter + H1 + attrs block + See Also.

        Frontmatter carries kind, status, and attrs (attrs JSON-encoded so
        quotes/colons in a value cannot break the YAML flow). LF endings,
        single trailing newline — nothing run-varying.
        """
        lines = [
            "---",
            f"kind: {node.kind}",
            f"status: {node.status.value}",
            # json.dumps yields an escape-safe scalar for the whole attrs map.
            f"attrs: {json.dumps(node.attrs, sort_keys=True)}",
            "---",
            "",
            f"# {node.label}",
        ]

        if node.attrs:
            lines.extend(["", "## Attributes", ""])
            for key in sorted(node.attrs):
                lines.append(f"- **{key}**: {node.attrs[key]}")

        if edges:
            lines.extend(["", "## See Also", ""])
            # Sorted by target so the section is deterministic.
            for edge in sorted(edges, key=lambda e: (e.target, e.type.value)):
                lines.append(f"- [[{_slug(edge.target)}]] ({edge.type.value})")

        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _render_index(nodes: list[Node]) -> str:
        """Regenerate ``index.md`` — a pure, sorted function of the node set."""
        lines = ["# Index", ""]
        for node in sorted(nodes, key=lambda n: _slug(n.id)):
            lines.append(f"- [{node.label}]({_slug(node.id)}.md)")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _render_manifest(view: GraphView) -> str:
        """Provenance note rendered from ``view.meta`` (deterministic).

        No timestamps or run-varying data — meta keys are emitted in sorted
        order so a same-view export produces byte-identical output.
        """
        lines = [
            "# CAO Graph Export",
            "",
            "Generated by CAO — edits here are not synced back.",
            "",
            "- format: okf",
            f"- nodes: {len(view.nodes)}",
            f"- edges: {len(view.edges)}",
        ]
        for key in sorted(view.meta):
            lines.append(f"- {key}: {view.meta[key]}")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _write_if_changed(path: Path, content: str) -> None:
        """Write ``content`` unless the file already holds those exact bytes."""
        data = content.encode("utf-8")
        try:
            if path.exists() and path.read_bytes() == data:
                return
        except OSError:
            # Unreadable existing file: fall through and rewrite it.
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
