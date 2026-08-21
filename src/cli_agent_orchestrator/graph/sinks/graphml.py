"""GraphML GraphSink (U7, Issue #348).

Exports a GraphView as a single ``.graphml`` XML file using ONLY the
standard library (``xml.etree.ElementTree``) per constraint C-2 — no
lxml, xmltodict, or any third-party XML dependency.

Security contract: ``dest`` is a FILE here, confined UNDER the configured
graph-export root (``CAO_GRAPH_EXPORT_ROOT``) via
``base.confine_under_export_root``. ``dest`` is treated as a file path
relative to that root (an absolute ``dest`` is accepted only when it already
resolves under the root); ``safe_join_under_base`` guarantees the ``.graphml``
file is written inside it — this is REAL confinement, not the
``resolve_and_validate_path`` blocklist (which permits e.g. ``~/.ssh`` and
would let ``allow_file=True`` overwrite arbitrary existing files). The sink
owns this confinement — the U4 route does NOT pre-validate ``dest``. No
``secret_gate`` call (route already scanned, ADR-5).
"""

import json
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element, ElementTree, SubElement

from cli_agent_orchestrator.graph.models import GraphView
from cli_agent_orchestrator.graph.sinks.base import (
    GraphSink,
    confine_under_export_root,
    register_sink,
)

_GRAPHML_NS = "http://graphml.graphdrawing.org/xmlns"

# Six fixed <key> declarations in a hardcoded, deterministic order. Each
# tuple is (key-id, domain, attr-name, attr-type). Emitting them in a fixed
# order means a same-view export produces byte-identical XML.
_KEY_DECLS: list[tuple[str, str, str, str]] = [
    ("d_node_kind", "node", "kind", "string"),
    ("d_node_label", "node", "label", "string"),
    ("d_node_status", "node", "status", "string"),
    ("d_node_attrs", "node", "attrs", "string"),
    ("d_edge_type", "edge", "type", "string"),
    ("d_edge_attrs", "edge", "attrs", "string"),
]


def _xml_scalar(value: Any) -> str:
    """Coerce a value to an XML-safe text scalar.

    Strings pass through; everything else is json.dumps-stringified so an
    attrs value that is not natively serializable is preserved as text
    rather than silently dropped. A value that json cannot encode is
    re-raised as ValueError so it maps to the route's 400 branch (the
    route's own json.dumps(view.to_dict()) would normally fail first, but
    keeping the error type consistent avoids a stray 500 if it ever leaks).
    """
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True)
    except TypeError as e:
        raise ValueError(f"attribute value is not JSON-encodable: {e}")


@register_sink("graphml")
class GraphMLGraphSink(GraphSink):
    """Export a GraphView to a single GraphML (.graphml) XML file."""

    def export(self, view: GraphView, dest: str, **options: Any) -> list[str]:
        # dest is a FILE confined UNDER the configured graph-export root:
        # treated as a file path relative to CAO_GRAPH_EXPORT_ROOT (the .graphml
        # leaf is the last segment). This confinement — not the blocklist —
        # is what stops an allow_file overwrite of an arbitrary existing file.
        # The sink owns confinement — the U4 route does NOT pre-validate dest.
        dest_path = confine_under_export_root(dest, description="GraphML export destination")
        dest_real = str(dest_path)

        root = Element("graphml", xmlns=_GRAPHML_NS)

        # Fixed <key> declarations, emitted in the hardcoded order above.
        # ``for`` is a Python keyword, so the attrib dict is built explicitly
        # rather than passed as **kwargs.
        for key_id, domain, attr_name, attr_type in _KEY_DECLS:
            SubElement(
                root,
                "key",
                {
                    "id": key_id,
                    "for": domain,
                    "attr.name": attr_name,
                    "attr.type": attr_type,
                },
            )

        graph_el = SubElement(root, "graph", {"id": "G", "edgedefault": "directed"})

        # Nodes in view.nodes list order (NOT re-sorted — element order
        # mirrors the provider's projection order).
        for node in view.nodes:
            node_el = SubElement(graph_el, "node", {"id": node.id})
            self._data(node_el, "d_node_kind", node.kind)
            self._data(node_el, "d_node_label", node.label)
            self._data(node_el, "d_node_status", node.status.value)
            self._data(node_el, "d_node_attrs", _xml_scalar(node.attrs))

        # Edges in view.edges list order.
        for edge in view.edges:
            edge_el = SubElement(graph_el, "edge", {"source": edge.source, "target": edge.target})
            self._data(edge_el, "d_edge_type", edge.type.value)
            self._data(edge_el, "d_edge_attrs", _xml_scalar(edge.attrs))

        # Ensure the parent directory exists (dest is a not-yet-created file).
        Path(dest_real).parent.mkdir(parents=True, exist_ok=True)
        ElementTree(root).write(dest_real, encoding="utf-8", xml_declaration=True)

        return [str(dest_real)]

    @staticmethod
    def _data(parent: Element, key: str, value: str) -> None:
        """Append a ``<data key=...>value</data>`` child to ``parent``."""
        data_el = SubElement(parent, "data", {"key": key})
        data_el.text = value
