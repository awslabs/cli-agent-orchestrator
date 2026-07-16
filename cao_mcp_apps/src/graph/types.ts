// Shared types for the ui://cao/graph view.
//
// Mirrors `graph/models.py`'s `GraphView.to_dict()` wire shape VERBATIM — no
// reshaping in the frontend. `attrs` stays `Record<string, unknown>` to match
// Python's `dict[str, Any]`; read known keys (`is_hub`, `is_orphan`) with
// `Boolean(node.attrs.is_hub)` truthy checks rather than trusting a type.

/** Domain lifecycle state of the thing a node represents (mirrors NodeStatus). */
export type NodeStatusValue =
  "active" | "proposal" | "observation" | "superseded";

/** Closed edge-type taxonomy, organized by family (mirrors EdgeType). */
export type EdgeTypeValue = "relates_to" | "contradiction" | "supersedes";

/** A single graph node projected by a GraphProvider (mirrors Node). */
export interface GraphNodeData {
  id: string;
  kind: string;
  label: string;
  status: NodeStatusValue;
  attrs: Record<string, unknown>;
}

/** A single graph edge projected by a GraphProvider (mirrors Edge). */
export interface GraphEdgeData {
  source: string;
  target: string;
  type: EdgeTypeValue;
  attrs: Record<string, unknown>;
}

/** The pure projection produced by GraphProvider.project() (mirrors GraphView). */
export interface GraphViewData {
  nodes: GraphNodeData[];
  edges: GraphEdgeData[];
  meta: Record<string, unknown>;
}
