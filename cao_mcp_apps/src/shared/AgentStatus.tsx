// AgentStatus — a single agent (terminal) card with a status badge.
//
// Status text/provider/profile are rendered as escaped React children. The
// status string is also normalized to a CSS modifier class for the badge color.

import React from "react";
import type { TerminalView } from "./types";
import { STATUS } from "./status.generated";

// The known status taxonomy is generated from the shared SSOT
// (design-tokens/status.json) via `node design-tokens/gen.mjs`.
const KNOWN_STATUSES = new Set(Object.keys(STATUS));

export interface AgentStatusProps {
  terminal: TerminalView;
  onOpen?: (terminalId: string) => void;
  /** Render a "supervisor" role badge and accent (the fleet coordinator). */
  isSupervisor?: boolean;
}

export function AgentStatus({
  terminal,
  onOpen,
  isSupervisor = false,
}: AgentStatusProps): JSX.Element {
  const status = (terminal.status ?? "unknown").toLowerCase();
  const statusClass = KNOWN_STATUSES.has(status) ? status : "unknown";
  // Label/role/pulse are derived from the generated status SSOT; the color is
  // applied through the cao-status-<status> class, which resolves to the
  // role's host-overridable CSS variable in styles.css/tokens.generated.css.
  const semantics = STATUS[statusClass];
  const pulseClass = semantics?.pulse ? " cao-status-pulse" : "";
  return (
    <div
      className={`cao-card${isSupervisor ? " cao-card-supervisor" : ""}`}
      data-testid="agent-card"
      data-terminal-id={terminal.id}
      role={onOpen ? "button" : undefined}
      tabIndex={onOpen ? 0 : undefined}
      onClick={onOpen ? () => onOpen(terminal.id) : undefined}
    >
      <div className="cao-card-head">
        <span className="cao-card-title">
          {terminal.agent_profile ?? terminal.id}
          {isSupervisor && (
            <span className="cao-role-badge" data-testid="role-badge">
              supervisor
            </span>
          )}
        </span>
        <span
          className={`cao-status cao-status-${statusClass}${pulseClass}`}
          data-testid="status-badge"
          title={semantics?.label}
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
