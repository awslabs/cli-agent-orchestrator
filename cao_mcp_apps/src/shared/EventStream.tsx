// EventStream — the governance ticker rendering normalized fleet events.
//
// Each event's kind drives a color class; all event metadata is rendered as
// escaped React children (never innerHTML), so a malicious detail field cannot
// inject markup (XSS safety).

import React from "react";
import type { CaoEvent, CaoEventKind } from "./types";

const KIND_LABEL: Record<CaoEventKind, string> = {
  launch: "launch",
  handoff: "handoff",
  a2a_delegation: "a2a",
  file_mod: "file",
  completion: "done",
  error: "error",
  other: "other",
};

function summarize(event: CaoEvent): string {
  const target = event.terminal_id ?? event.session_name ?? "";
  return target
    ? `${KIND_LABEL[event.kind]} · ${target}`
    : KIND_LABEL[event.kind];
}

export interface EventStreamProps {
  events: CaoEvent[];
  emptyLabel?: string;
}

export function EventStream({
  events,
  emptyLabel,
}: EventStreamProps): JSX.Element {
  if (events.length === 0) {
    return (
      <div
        className="cao-events cao-events-empty"
        data-testid="event-stream-empty"
      >
        {emptyLabel ?? "No events yet"}
      </div>
    );
  }
  return (
    <ul className="cao-events" data-testid="event-stream">
      {events.map((event) => (
        <li
          key={event.id}
          className={`cao-event cao-event-${event.kind}`}
          data-testid="event-row"
          data-kind={event.kind}
        >
          <span className="cao-event-kind">
            {KIND_LABEL[event.kind] ?? "other"}
          </span>
          <span className="cao-event-summary">{summarize(event)}</span>
          <time className="cao-event-time">{event.timestamp}</time>
        </li>
      ))}
    </ul>
  );
}
