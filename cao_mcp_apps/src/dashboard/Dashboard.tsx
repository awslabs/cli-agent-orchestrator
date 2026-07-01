// Dashboard view (ui://cao/dashboard).
//
// Hydrates from the initial tool result the host delivers via
// `ui/notifications/tool-result`, then polls `render_dashboard` on an interval
// and applies RFC-6902 deltas (clientDiff + applyPatch) so the view updates
// without full re-renders. Re-mounting re-hydrates from scratch (Req: re-mount
// idempotence) because each mount re-registers handlers and re-fetches.

import React, { useEffect, useRef, useState } from "react";
import { AgentStatus } from "../shared/AgentStatus";
import { HeaderBar } from "../shared/HeaderBar";
import { describeGesture, McpApp } from "../shared/mcpApp";
import { applyPatch, clientDiff } from "../shared/patch";
import type { DashboardSnapshot, SubmitCommandKind } from "../shared/types";

const POLL_INTERVAL_MS = 30_000;

// CAO's bundled browser Web UI. The dashboard offers a host-delegated link to
// it (SEP-1865 `ui/open-link`) so an operator can pop the full UI out of the
// chat host when the host advertises the `openLinks` capability.
const WEB_UI_URL = "http://127.0.0.1:9889";

/** Heuristic: a terminal whose profile marks it as the fleet coordinator. */
function isSupervisorTerminal(t: { agent_profile: string | null }): boolean {
  return (t.agent_profile ?? "").toLowerCase().includes("supervisor");
}

/** Supervisors first (stable), so the coordinator leads the fleet grid. */
function orderFleet<T extends { agent_profile: string | null }>(
  terminals: T[],
): T[] {
  return [...terminals].sort(
    (a, b) =>
      (isSupervisorTerminal(b) ? 1 : 0) - (isSupervisorTerminal(a) ? 1 : 0),
  );
}

const EMPTY_SNAPSHOT: DashboardSnapshot = {
  sessions: [],
  terminals: [],
  counts: { sessions: 0, terminals: 0 },
  scopes: [],
};

export interface DashboardProps {
  app?: McpApp;
  initialSnapshot?: DashboardSnapshot;
  onOpenAgent?: (terminalId: string) => void;
}

export function Dashboard({
  app,
  initialSnapshot,
  onOpenAgent,
}: DashboardProps): JSX.Element {
  const [snapshot, setSnapshot] = useState<DashboardSnapshot>(
    initialSnapshot ?? EMPTY_SNAPSHOT,
  );
  const [unreachable, setUnreachable] = useState(false);
  const [canOpenWebUi, setCanOpenWebUi] = useState(false);
  const snapshotRef = useRef(snapshot);
  snapshotRef.current = snapshot;

  // Apply a freshly-polled snapshot as a delta over the current one.
  function applyDelta(next: DashboardSnapshot): void {
    const ops = clientDiff(snapshotRef.current, next);
    if (ops.length === 0) return;
    setSnapshot(applyPatch(snapshotRef.current, ops) as DashboardSnapshot);
  }

  useEffect(() => {
    if (!app) return;
    let stop: (() => void) | undefined;

    // Register handlers BEFORE connect (lifecycle invariant).
    app.onToolResult((result) => {
      const snap = (result?.structuredContent ?? result) as
        DashboardSnapshot | undefined;
      if (snap && Array.isArray(snap.terminals)) applyDelta(snap);
    });

    void app.connect().then(() => {
      // Surface the host-delegated Web UI link only when the host can open links.
      setCanOpenWebUi(app.canOpenLinks());
      stop = app.startPolling(
        "render_dashboard",
        POLL_INTERVAL_MS,
        (snap) => {
          if (snap && Array.isArray(snap.terminals)) {
            setUnreachable(false);
            applyDelta(snap as DashboardSnapshot);
          }
        },
        {},
        // A failed poll surfaces the retry control.
        () => setUnreachable(true),
      );
    });

    return () => {
      if (stop) stop();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [app]);

  async function handleSubmit(
    kind: SubmitCommandKind,
    payload: Record<string, unknown>,
  ) {
    if (!app) return { success: false, error: "not connected" };
    try {
      const result = await app.submitCommand(kind, payload);
      if (result.success) {
        // One token-efficient, body-free model-context note per material
        // fleet action; never blocks the iframe on failure.
        const target =
          (payload.session_name as string | undefined) ??
          (payload.terminal_id as string | undefined);
        void app.silentlyNoteToModel(describeGesture(kind, target));
      }
      return result;
    } catch (e) {
      setUnreachable(true);
      return {
        success: false,
        error: e instanceof Error ? e.message : "failed",
      };
    }
  }

  // Exposed for fleet-level gestures wired in Phase III (drag-and-drop assign,
  // etc.). Referenced here so the handler is part of the component's surface.
  void handleSubmit;

  return (
    <div className="cao-root">
      <HeaderBar
        title="CAO Fleet"
        sessions={snapshot.counts.sessions}
        terminals={snapshot.counts.terminals}
      />
      {canOpenWebUi && (
        <div className="cao-toolbar" data-testid="webui-toolbar">
          <button
            type="button"
            className="cao-btn"
            data-testid="open-webui"
            onClick={() => {
              if (app) void app.openLink(WEB_UI_URL).catch(() => undefined);
            }}
          >
            Open full Web UI ↗
          </button>
        </div>
      )}
      {unreachable && (
        <div
          className="cao-taskcontrol-error"
          role="alert"
          data-testid="retry-banner"
        >
          Backplane unreachable.{" "}
          <button
            type="button"
            className="cao-btn"
            data-testid="retry-button"
            onClick={() => {
              if (app) {
                void app
                  .callServerTool("render_dashboard")
                  .then((s) => {
                    setUnreachable(false);
                    applyDelta(s as DashboardSnapshot);
                  })
                  .catch(() => setUnreachable(true));
              }
            }}
          >
            Retry
          </button>
        </div>
      )}
      {snapshot.terminals.length === 0 ? (
        <div className="cao-events-empty" data-testid="empty-placeholder">
          No active agents
        </div>
      ) : (
        <div className="cao-grid" data-testid="agent-grid">
          {orderFleet(snapshot.terminals).map((terminal) => (
            <AgentStatus
              key={terminal.id}
              terminal={terminal}
              onOpen={onOpenAgent}
              isSupervisor={isSupervisorTerminal(terminal)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
