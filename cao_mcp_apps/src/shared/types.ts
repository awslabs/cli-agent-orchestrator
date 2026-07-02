// Shared types for the CAO MCP App views.
//
// These mirror the Python snapshot shape produced by
// `services/ui_state_service.build_dashboard_snapshot` and the six-primitive
// event vocabulary from `services/event_primitives.py`. Keep them in sync: the
// iframe renders exactly what the backend projects.

/**
 * The closed event vocabulary the governance ticker renders. Mirrors
 * `event_primitives.PRIMITIVES` plus the reserved pass-through `"other"`.
 */
export type CaoEventKind =
  | "launch"
  | "handoff"
  | "a2a_delegation"
  | "file_mod"
  | "completion"
  | "error"
  | "other";

/** A normalized fleet event, as returned by `cao_fetch_history` / `/events`. */
export interface CaoEvent {
  id: string;
  kind: CaoEventKind;
  terminal_id: string | null;
  session_name: string | null;
  timestamp: string;
  /** Metadata only — never message bodies (privacy boundary). */
  detail: Record<string, unknown>;
}

/** A session row in the dashboard snapshot. */
export interface SessionView {
  id: string;
  name: string;
  status: string;
}

/** A terminal (agent) row in the dashboard snapshot. */
export interface TerminalView {
  id: string;
  session_name: string;
  provider: string;
  agent_profile: string | null;
  window: string | null;
  status: string | null;
  last_active: string | null;
}

/** The pure projection produced by `build_dashboard_snapshot`. */
export interface DashboardSnapshot {
  sessions: SessionView[];
  terminals: TerminalView[];
  counts: { sessions: number; terminals: number };
  /** Granted scope set (default-off => full taxonomy). */
  scopes: string[];
}

/** The per-agent detail snapshot from `build_agent_detail_snapshot`. */
export interface AgentDetailSnapshot {
  terminal_id: string;
  session_name: string;
  provider: string;
  agent_profile: string | null;
  status: string | null;
  last_active: string | null;
  output_tail: string;
  scopes: string[];
}

/**
 * The command kinds the single mutation choke point (`submit_command`) accepts.
 * Mirrors STANDARD ∪ LIFECYCLE ∪ DESTRUCTIVE in `mcp_server/app_tools.py`.
 */
export type SubmitCommandKind =
  // STANDARD
  | "send_message"
  | "assign"
  | "create_session"
  // LIFECYCLE
  | "interrupt"
  | "pause"
  | "resume"
  // DESTRUCTIVE
  | "shutdown_session";

/** Structured result returned by `submit_command`. */
export interface SubmitCommandResult {
  success: boolean;
  error?: string;
  kind?: string;
  required_scope?: string;
  [key: string]: unknown;
}

/** A single RFC 6902 JSON Patch operation (the subset we emit/apply). */
export interface JsonPatchOp {
  op: "add" | "remove" | "replace" | "move" | "copy" | "test";
  path: string;
  value?: unknown;
  from?: string;
}
