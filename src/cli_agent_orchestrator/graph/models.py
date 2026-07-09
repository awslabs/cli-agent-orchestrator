"""Graph domain contract: Node, Edge, GraphView, and their enums (U1)."""

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
    """A single graph node projected by a GraphProvider."""

    id: str
    kind: str
    label: str
    status: NodeStatus = NodeStatus.ACTIVE
    attrs: dict[str, Any] = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def validate_kind_shape(cls, value: str) -> str:
        if not _KIND_PATTERN.match(value):
            raise ValueError(f"kind must be a non-empty lowercase-snake-case string, got {value!r}")
        return value


class Edge(BaseModel):
    """A single graph edge projected by a GraphProvider."""

    source: str
    target: str
    type: EdgeType
    attrs: dict[str, Any] = Field(default_factory=dict)


class GraphView(BaseModel):
    """A snapshot of nodes, edges, and metadata returned by GraphProvider.project()."""

    nodes: list[Node]
    edges: list[Edge]
    meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_edge_endpoints(self) -> "GraphView":
        node_ids = {node.id for node in self.nodes}
        for edge in self.edges:
            if edge.source not in node_ids:
                raise ValueError(f"edge source {edge.source!r} is not a known node id")
            if edge.target not in node_ids:
                raise ValueError(f"edge target {edge.target!r} is not a known node id")
        return self

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire shape consumed by U4's routes and U8's renderer."""
        return {
            "nodes": [
                {
                    "id": node.id,
                    "kind": node.kind,
                    "label": node.label,
                    "status": node.status.value,
                    "attrs": node.attrs,
                }
                for node in self.nodes
            ],
            "edges": [
                {
                    "source": edge.source,
                    "target": edge.target,
                    "type": edge.type.value,
                    "attrs": edge.attrs,
                }
                for edge in self.edges
            ],
            "meta": self.meta,
        }
