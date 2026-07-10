"""Graph domain contract: Node, Edge, GraphView, and their enums.

Design: Issue #348 (graph-layer epic); design record:
aidlc/spaces/default/intents/260709-graph-layer/ (AIDLC intent, not shipped
with the package). Per-symbol codes below (U1, FR-3, etc.) resolve through
this anchor.
"""

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class NodeStatus(str, Enum):
    """Domain lifecycle state of the thing a node represents."""

    ACTIVE = "active"
    PROPOSAL = "proposal"
    OBSERVATION = "observation"
    SUPERSEDED = "superseded"


class EdgeType(str, Enum):
    """Closed edge-type taxonomy, organized by family (FR-3)."""

    # Topical family
    RELATES_TO = "relates_to"
    # Lint-derived family
    CONTRADICTION = "contradiction"
    # Lifecycle family — reserved, unpopulated by any provider in this deliverable
    SUPERSEDES = "supersedes"


class Node(BaseModel):
    """A single graph node projected by a GraphProvider.

    ``id``, ``label``, and ``attrs`` are provider-supplied and untrusted —
    a GraphProvider may project data originating outside this process.
    Renderers/sinks that emit these values into HTML, DOM, or similar
    output MUST escape on output; this contract does not sanitize them.
    """

    id: str
    kind: str
    label: str
    status: NodeStatus = NodeStatus.ACTIVE
    attrs: dict[str, Any] = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def validate_kind_shape(cls, value: str) -> str:
        if not _KIND_PATTERN.fullmatch(value):
            raise ValueError(f"kind must be a non-empty lowercase-snake-case string, got {value!r}")
        return value


class Edge(BaseModel):
    """A single graph edge projected by a GraphProvider."""

    source: str
    target: str
    type: EdgeType
    attrs: dict[str, Any] = Field(default_factory=dict)


class GraphView(BaseModel):
    """A snapshot of nodes, edges, and metadata returned by GraphProvider.project().

    No size cap (node/edge count, attrs/meta payload size) is enforced in
    this deliverable — providers are trusted local code today. Revisit if
    a provider ever projects untrusted or unbounded upstream data.
    """

    nodes: list[Node]
    edges: list[Edge]
    meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_edge_endpoints(self) -> "GraphView":
        seen: set[str] = set()
        dupes: set[str] = set()
        for node in self.nodes:
            if node.id in seen:
                dupes.add(node.id)
            seen.add(node.id)
        if dupes:
            raise ValueError(f"duplicate node ids: {sorted(dupes)!r}")
        node_ids = seen
        for edge in self.edges:
            if edge.source not in node_ids:
                raise ValueError(f"edge source {edge.source!r} is not a known node id")
            if edge.target not in node_ids:
                raise ValueError(f"edge target {edge.target!r} is not a known node id")
        return self

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire shape consumed by U4's routes and U8's renderer."""
        return self.model_dump(mode="json")
