"""Obsidian vault GraphSink (U6, Issue #348).

Exports a GraphView as an Obsidian-openable vault: one markdown note per
node, wiki-linked (``[[target]]``) per outgoing edge. Frontmatter is
serialized with PyYAML (NOT hand-rolled) so quotes/colons/newlines in an
attrs value cannot break the YAML block.

Security contract: ``dest`` is confined UNDER the configured graph-export
root (``CAO_GRAPH_EXPORT_ROOT``) via
``base.confine_under_export_root`` — ``dest`` is treated as a path relative
to that root (an absolute ``dest`` is accepted only when it already resolves
under the root), and ``safe_join_under_base`` guarantees the write stays
inside it. The sink owns this confinement — the U4 route does NOT
pre-validate ``dest``. No ``secret_gate`` call (route already scanned,
ADR-5). No ``.obsidian/`` config directory is written.
"""

import re
from typing import Any

import yaml

from cli_agent_orchestrator.graph.models import Edge, EdgeType, GraphView, Node
from cli_agent_orchestrator.graph.sinks.base import (
    GraphSink,
    confine_under_export_root,
    register_sink,
)

# Filesystem-unsafe characters collapsed to '-'. A label like "a/b:c" must
# sanitize to a writable filename, not crash the export.
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value: str) -> str:
    """Map a node id to a safe, deterministic note filename stem.

    Falls back to "node" when the value reduces to empty so a label built
    entirely from unsafe characters still yields a writable filename.
    """
    slug = _UNSAFE_CHARS.sub("-", value).strip("-._")
    return slug or "node"


def _md_inline(value: Any) -> str:
    """Escape an untrusted value for safe single-line markdown emission.

    label/attrs are untrusted (Node docstring: sinks MUST escape on output).
    Newlines are collapsed so an LLM summary cannot inject a new heading or
    ``[[wikilink]]`` on its own line, and markdown-structural characters are
    backslash-escaped so they render literally in the ``# <label>`` heading.
    """
    text = re.sub(r"[\r\n\u2028\u2029]+", " ", str(value))
    return re.sub(r"([\\`*_\[\]()#])", r"\\\1", text)


@register_sink("obsidian")
class ObsidianGraphSink(GraphSink):
    """Export a GraphView as an Obsidian vault of wiki-linked notes."""

    def export(self, view: GraphView, dest: str, **options: Any) -> list[str]:
        # Confine dest UNDER the configured graph-export root; dest is a
        # DIRECTORY treated as relative to CAO_GRAPH_EXPORT_ROOT. The sink
        # owns confinement — the U4 route does NOT pre-validate dest.
        dest_dir = confine_under_export_root(dest, description="Obsidian vault destination")
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Group outgoing edges by source id (single pass over the edge list).
        outgoing: dict[str, list[Edge]] = {}
        for edge in view.edges:
            outgoing.setdefault(edge.source, []).append(edge)

        written: list[str] = []
        # Two distinct node ids that slug to the same filename would silently
        # clobber each other; detect the collision and fail loudly instead.
        # Single-pass: a collision raised mid-loop may leave earlier notes on
        # disk (documented, accepted — the route maps ValueError to 400).
        filenames: dict[str, str] = {}

        for node in view.nodes:
            slug = _slug(node.id)
            existing = filenames.get(slug)
            if existing is not None and existing != node.id:
                raise ValueError(
                    f"filename collision: nodes {existing!r} and {node.id!r} "
                    f"both map to {slug!r}.md"
                )
            filenames[slug] = node.id

            content = self._render_note(node, outgoing.get(node.id, []))
            path = dest_dir / (slug + ".md")
            path.write_text(content, encoding="utf-8")
            written.append(str(path))

        return written

    @staticmethod
    def _render_note(node: Node, edges: list[Edge]) -> str:
        """Serialize one node as a YAML-frontmatter + H1 + Links note.

        The "## Links" heading is omitted entirely when the node has no
        outgoing edges. A contradiction edge is suffixed " (contradiction)".
        """
        # PyYAML handles all escaping for kind/status/attrs — never hand-rolled.
        front = {
            "kind": node.kind,
            "status": node.status.value,
            "attrs": node.attrs,
        }
        frontmatter = yaml.safe_dump(front, sort_keys=True, allow_unicode=True).rstrip()

        # label is untrusted (may be an LLM summary): escape so a newline or
        # markdown-structural char cannot inject a heading/wikilink/list item.
        lines = ["---", frontmatter, "---", "", f"# {_md_inline(node.label)}"]

        if edges:
            lines.extend(["", "## Links", ""])
            for edge in edges:
                link = f"- [[{_slug(edge.target)}]]"
                if edge.type == EdgeType.CONTRADICTION:
                    link += " (contradiction)"
                lines.append(link)

        return "\n".join(lines).rstrip() + "\n"
