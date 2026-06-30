// AgentStatus — a single agent (terminal) card with a status badge.
//
// Status text/provider/profile are rendered as escaped React children. The
// status string is also normalized to a CSS modifier class for the badge color.

import React from "react";
import type { TerminalView } from "./types";

const KNOWN_STATUSES = new Set([
  "idle",
  "processing",
  "completed",
  "waiting_user_answer",
  "error",
  "stopped",
  "unknown",
]);

export interface AgentStatusProps {
  terminal: TerminalView;
  onOpen?: (terminalId: string) => void;
}

export function AgentStatus({
  terminal,
  onOpen,
}: AgentStatusProps): JSX.Element {
  const status = (terminal.status ?? "unknown").toLowerCase();
  const statusClass = KNOWN_STATUSES.has(status) ? status : "unknown";
  return (
    <div
      className="cao-card"
      data-testid="agent-card"
      data-terminal-id={terminal.id}
      role={onOpen ? "button" : undefined}
      tabIndex={onOpen ? 0 : undefined}
      onClick={onOpen ? () => onOpen(terminal.id) : undefined}
    >
      <div className="cao-card-head">
        <span className="cao-card-title">
          {terminal.agent_profile ?? terminal.id}
        </span>
        <span
          className={`cao-status cao-status-${statusClass}`}
          data-testid="status-badge"
        >
          {status}
        </span>
      </div>
      <dl className="cao-card-meta">
        <div>
          <dt>provider</dt>
          <dd>{terminal.provider}</dd>
        </div>
        <div>
          <dt>session</dt>
          <dd>{terminal.session_name}</dd>
        </div>
      </dl>
    </div>
  );
}
