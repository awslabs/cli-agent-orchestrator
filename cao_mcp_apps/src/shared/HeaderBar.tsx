// HeaderBar — title + fleet counts for the dashboard / views.
//
// All dynamic text is rendered as React children, which escapes it by default
// (no `dangerouslySetInnerHTML`), satisfying the escaped-string requirement.

import React from "react";

export interface HeaderBarProps {
  title: string;
  sessions?: number;
  terminals?: number;
}

export function HeaderBar({
  title,
  sessions,
  terminals,
}: HeaderBarProps): JSX.Element {
  return (
    <header className="cao-header">
      <h1 className="cao-header-title">{title}</h1>
      {(sessions !== undefined || terminals !== undefined) && (
        <div className="cao-header-counts" data-testid="header-counts">
          {sessions !== undefined && (
            <span className="cao-badge" data-testid="count-sessions">
              {sessions} sessions
            </span>
          )}
          {terminals !== undefined && (
            <span className="cao-badge" data-testid="count-terminals">
              {terminals} agents
            </span>
          )}
        </div>
      )}
    </header>
  );
}
