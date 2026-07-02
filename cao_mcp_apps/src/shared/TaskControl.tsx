// TaskControl — mutation gesture buttons, each mapped to exactly ONE
// SubmitCommandKind, all routed through the single `submit_command` choke point.
//
// - Each button maps to exactly one SubmitCommandKind.
// - Destructive kinds require window.confirm() before submitting (wired in
//   Phase III; the confirm hook is already here).
// - The free-text message input is rendered as a controlled value and echoed
//   only as escaped React children (XSS safety).

import React, { useState } from "react";
import { buildGesturePayload, DRAG_REASSIGN_KIND } from "./mcpApp";
import type { SubmitCommandKind, SubmitCommandResult } from "./types";

/** Each control maps to exactly one command kind. */
interface ControlDef {
  kind: SubmitCommandKind;
  label: string;
  destructive?: boolean;
  /** Scope required by the choke point — used to gate rendering when known. */
  requiredScope: "cao:write" | "cao:admin";
}

const CONTROLS: ControlDef[] = [
  { kind: "send_message", label: "Send", requiredScope: "cao:write" },
  { kind: "assign", label: "Assign", requiredScope: "cao:write" },
  { kind: "interrupt", label: "Interrupt", requiredScope: "cao:write" },
  { kind: "pause", label: "Pause", requiredScope: "cao:write" },
  { kind: "resume", label: "Resume", requiredScope: "cao:write" },
  {
    kind: "shutdown_session",
    label: "Shutdown",
    destructive: true,
    requiredScope: "cao:admin",
  },
];

const MAX_PAYLOAD_CHARS = 4000;

export interface TaskControlProps {
  /** Routes a command through the single choke point. */
  onSubmit: (
    kind: SubmitCommandKind,
    payload: Record<string, unknown>,
  ) => Promise<SubmitCommandResult>;
  /** Target terminal/session the gesture applies to. */
  target?: string;
  /** Granted scopes; controls whose required scope is absent are hidden. */
  scopes?: string[];
  /** Confirm hook for destructive kinds (defaults to window.confirm). */
  confirm?: (message: string) => boolean;
}

export function TaskControl({
  onSubmit,
  target,
  scopes,
  confirm,
}: TaskControlProps): JSX.Element {
  const [message, setMessage] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const confirmFn =
    confirm ??
    ((m: string) => (typeof window !== "undefined" ? window.confirm(m) : true));

  const canUse = (scope: string): boolean => {
    // Default-off: when scopes is undefined/empty the UI shows everything
    // (matches the server's full-scope default). When a non-empty set is
    // provided, gate on membership.
    if (!scopes || scopes.length === 0) return true;
    return scopes.includes(scope);
  };

  async function run(def: ControlDef): Promise<void> {
    setError(null);
    if (def.kind === "send_message" || def.kind === "assign") {
      if (!message.trim()) {
        setError("Message must not be empty");
        return;
      }
      if (message.length > MAX_PAYLOAD_CHARS) {
        setError(`Message must be ${MAX_PAYLOAD_CHARS} characters or fewer`);
        return;
      }
    }
    if (
      def.destructive &&
      !confirmFn(`Really ${def.label.toLowerCase()} ${target ?? "this"}?`)
    ) {
      return;
    }
    // Map the gesture's target to the field the server route reads
    // (terminal_id / session_name) and attach the gesture-specific message.
    const extras: Record<string, unknown> = {};
    if (def.kind === "send_message" || def.kind === "assign")
      extras.message = message;
    const payload = buildGesturePayload(def.kind, target, extras);

    setBusy(true);
    try {
      const result = await onSubmit(def.kind, payload);
      if (!result.success) setError(result.error ?? "Command failed");
      else if (def.kind === "send_message" || def.kind === "assign")
        setMessage("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Command failed");
    } finally {
      setBusy(false);
    }
  }

  /**
   * Drag-and-drop reassignment: dropping a task payload onto this
   * control reassigns it to `target` via the single `assign` Command_Kind.
   * The dropped data is read from the `text/plain` drag payload (the task text).
   */
  async function handleDrop(e: React.DragEvent<HTMLDivElement>): Promise<void> {
    e.preventDefault();
    setError(null);
    const dropped = e.dataTransfer.getData("text/plain").trim();
    if (!dropped) return;
    if (dropped.length > MAX_PAYLOAD_CHARS) {
      setError(`Message must be ${MAX_PAYLOAD_CHARS} characters or fewer`);
      return;
    }
    const payload = buildGesturePayload(DRAG_REASSIGN_KIND, target, {
      message: dropped,
    });
    setBusy(true);
    try {
      const result = await onSubmit(DRAG_REASSIGN_KIND, payload);
      if (!result.success) setError(result.error ?? "Command failed");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Command failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="cao-taskcontrol"
      data-testid="task-control"
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => void handleDrop(e)}
    >
      <textarea
        className="cao-taskcontrol-input"
        data-testid="task-input"
        placeholder="Message…"
        value={message}
        onChange={(e) => setMessage(e.target.value)}
        rows={2}
      />
      <div className="cao-taskcontrol-buttons">
        {CONTROLS.filter((c) => canUse(c.requiredScope)).map((def) => (
          <button
            key={def.kind}
            type="button"
            className={`cao-btn${def.destructive ? " cao-btn-danger" : ""}`}
            data-testid={`btn-${def.kind}`}
            data-kind={def.kind}
            disabled={busy}
            onClick={() => void run(def)}
          >
            {def.label}
          </button>
        ))}
      </div>
      {error && (
        <p
          className="cao-taskcontrol-error"
          data-testid="task-error"
          role="alert"
        >
          {error}
        </p>
      )}
    </div>
  );
}
