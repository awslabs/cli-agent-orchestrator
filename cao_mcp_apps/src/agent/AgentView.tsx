// Agent detail view (ui://cao/agent).
//
// Renders a single agent's status + a tail of its terminal output, and exposes
// the per-agent TaskControl (the choke-point gestures). Hydrates from the
// initial tool result and re-fetches on re-mount.

import React, { useEffect, useRef, useState } from "react";
import { HeaderBar } from "../shared/HeaderBar";
import { describeGesture, McpApp } from "../shared/mcpApp";
import { TaskControl } from "../shared/TaskControl";
import type { AgentDetailSnapshot, SubmitCommandKind } from "../shared/types";

const POLL_INTERVAL_MS = 30_000;

export interface AgentViewProps {
  app?: McpApp;
  terminalId?: string;
  initialSnapshot?: AgentDetailSnapshot;
}

export function AgentView({
  app,
  terminalId,
  initialSnapshot,
}: AgentViewProps): JSX.Element {
  const [snapshot, setSnapshot] = useState<AgentDetailSnapshot | null>(
    initialSnapshot ?? null,
  );
  const tidRef = useRef(terminalId ?? initialSnapshot?.terminal_id);

  useEffect(() => {
    if (!app) return;
    let stop: (() => void) | undefined;

    app.onToolResult((result) => {
      const snap = (result?.structuredContent ?? result) as
        | AgentDetailSnapshot
        | undefined;
      if (snap && snap.terminal_id) {
        tidRef.current = snap.terminal_id;
        setSnapshot(snap);
      }
    });

    void app.connect().then(() => {
      const tid = tidRef.current;
      if (!tid) return;
      stop = app.startPolling(
        "render_agent_view",
        POLL_INTERVAL_MS,
        (snap) => {
          if (snap && (snap as AgentDetailSnapshot).terminal_id) {
            setSnapshot(snap as AgentDetailSnapshot);
          }
        },
        { terminal_id: tid },
      );
    });

    return () => {
      if (stop) stop();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [app, terminalId]);

  async function handleSubmit(
    kind: SubmitCommandKind,
    payload: Record<string, unknown>,
  ) {
    if (!app) return { success: false, error: "not connected" };
    // TaskControl already builds the correct payload (terminal_id / session_name)
    // via buildGesturePayload, so route it through the choke point as-is.
    const result = await app.submitCommand(kind, payload);
    if (result.success) {
      // Exactly one token-efficient, body-free note per material action,
      // described with a Semantic_Primitive. silentlyNoteToModel never throws,
      // so a failed note cannot block the iframe.
      void app.silentlyNoteToModel(
        describeGesture(kind, tidRef.current ?? undefined),
      );
    }
    return result;
  }

  if (!snapshot) {
    return (
      <div className="cao-root">
        <HeaderBar title="Agent" />
        <div className="cao-events-empty" data-testid="agent-loading">
          Loading agent…
        </div>
      </div>
    );
  }

  return (
    <div className="cao-root">
      <HeaderBar title={snapshot.agent_profile ?? snapshot.terminal_id} />
      <div className="cao-card" data-testid="agent-detail">
        <div className="cao-card-head">
          <span className="cao-card-title">{snapshot.terminal_id}</span>
          <span
            className={`cao-status cao-status-${(snapshot.status ?? "unknown").toLowerCase()}`}
            data-testid="agent-status"
          >
            {snapshot.status ?? "unknown"}
          </span>
        </div>
        <pre className="cao-output" data-testid="agent-output">
          {snapshot.output_tail}
        </pre>
      </div>
      <TaskControl
        onSubmit={handleSubmit}
        target={snapshot.terminal_id}
        scopes={snapshot.scopes}
      />
    </div>
  );
}
