"""OKF (Obsidian Knowledge Format) GraphSink (U5, Issue #348).

Generalizes the #345 memory-export bundle shape
(``services/memory_archive/okf.py``) to an arbitrary GraphView: one
markdown file per node plus a deterministic ``index.md`` and a
``manifest.md`` provenance note. The byte-compare-before-write discipline
(``_write_if_changed``) is adopted as a PATTERN here — this module does
NOT import the memory-archive exporter.

Security contract: ``dest`` is confined UNDER the configured graph-export
root (``CAO_GRAPH_EXPORT_ROOT``) via
``base.confine_under_export_root`` — ``dest`` is a path relative to that
root (an absolute ``dest`` is accepted only when it already resolves under
the root), and ``safe_join_under_base`` guarantees the write stays inside
it. The sink owns this confinement — the U4 route does NOT pre-validate
``dest``. No ``secret_gate`` call inside ``export()`` — the route scans the
serialized view before dispatch (ADR-5); the sink assumes clean content.
"""

import json
import re
from pathlib import Path
from typing import Any

from cli_agent_orchestrator.graph.models import Edge, GraphView, Node
from cli_agent_orchestrator.graph.sinks.base import (
    GraphSink,
    confine_under_export_root,
    register_sink,
)

# Filesystem-unsafe characters collapsed to '-' so a node id/label maps to a
# stable, portable filename. Empty results fall back to a fixed token.
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

# Reserved bundle-file stems. A node whose id slugs to one of these would
# otherwise be silently overwritten by the generated index.md / manifest.md.
_RESERVED_STEMS = frozenset({"index", "manifest"})


def _slug(value: str) -> str:
    """Map an arbitrary node id to a safe, deterministic filename stem.

    Collapses runs of filesystem-unsafe characters to a single '-', strips
    leading/trailing separators, and falls back to "node" for a value that
    reduces to empty (so a label like "///" still yields a writable file).
    """
    slug = _UNSAFE_CHARS.sub("-", value).strip("-._")
    return slug or "node"


def _md_inline(value: Any) -> str:
    """Escape an untrusted value for safe single-line markdown emission.

    label/attrs are untrusted (Node docstring: sinks MUST escape on output).
    Newlines/CRs are collapsed to a space so an LLM summary cannot inject a
    new ``#`` heading, list item, or ``[[wikilink]]`` on its own line; the
    markdown-structural characters that open links/wikilinks/emphasis
    (``[]()*_`\\`` plus ``#``) are backslash-escaped so they render literally.
    """
    text = str(value)
    # Collapse any line break (LF, CR, CRLF, and unicode line/para separators)
    # to a single space so nothing can start a new markdown block.
    text = re.sub(r"[\r\n\u2028\u2029]+", " ", text)
    return re.sub(r"([\\`*_\[\]()#])", r"\\\1", text)


@register_sink("okf")
class OkfGraphSink(GraphSink):
    """Export a GraphView as an OKF-shaped markdown bundle.

    Structurally identical in FORM regardless of provider: the stub
    provider's GraphView produces the same bundle layout (per-node ``.md``
    + ``index.md`` + ``manifest.md``) as the memory provider's.
    """

    def export(self, view: GraphView, dest: str, **options: Any) -> list[str]:
        # Confine dest UNDER the configured graph-export root. dest is a
        # DIRECTORY treated as relative to CAO_GRAPH_EXPORT_ROOT; traversal /
        # absolute-escape / symlink-escape -> ValueError -> route maps to 400.
        # The route does NOT pre-validate dest — this sink owns confinement.
        dest_dir = confine_under_export_root(dest, description="OKF export destination")
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Group outgoing edges by source id for the per-node "See Also"
        # section — a single pass so a large edge list is not re-scanned per node.
        outgoing: dict[str, list[Edge]] = {}
        for edge in view.edges:
            outgoing.setdefault(edge.source, []).append(edge)

        written: list[str] = []
        # Fail loudly (matching the Obsidian sink's posture) instead of
        # last-write-wins: a node id that slugs to a reserved stem
        # ("index"/"manifest") would be clobbered by the generated
        # index.md/manifest.md, and two ids slugging to the same stem would
        # clobber each other. Both are detected before any per-node write.
        stems: dict[str, str] = {}

        # One markdown file per node. Sorted by slug so a same-content view
        # always writes files in the same order (deterministic output).
        for node in sorted(view.nodes, key=lambda n: _slug(n.id)):
            slug = _slug(node.id)
            if slug in _RESERVED_STEMS:
                raise ValueError(
                    f"node id {node.id!r} slugs to reserved bundle stem {slug!r}.md "
                    f"(collides with the generated {slug}.md)"
                )
            existing = stems.get(slug)
            if existing is not None and existing != node.id:
                raise ValueError(
                    f"filename collision: nodes {existing!r} and {node.id!r} "
                    f"both map to {slug!r}.md"
                )
            stems[slug] = node.id

            filename = slug + ".md"
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
            # label is untrusted (may be an LLM summary): escape so a newline
            # or markdown-structural char cannot inject a heading/link/list.
            f"# {_md_inline(node.label)}",
        ]

        if node.attrs:
            lines.extend(["", "## Attributes", ""])
            for key in sorted(node.attrs):
                lines.append(f"- **{_md_inline(key)}**: {_md_inline(node.attrs[key])}")

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
            # _slug already yields a safe filename stem; the label is untrusted
            # link text and is escaped so it cannot break the link syntax.
            lines.append(f"- [{_md_inline(node.label)}]({_slug(node.id)}.md)")
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
            # meta is provider-supplied; escape both key and value on output.
            lines.append(f"- {_md_inline(key)}: {_md_inline(view.meta[key])}")
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
