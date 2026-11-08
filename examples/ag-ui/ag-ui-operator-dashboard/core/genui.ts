/**
 * Generative UI panel: a pure reducer from ambient-stream frames to the
 * entries the panel lists, in arrival order, deduped by event id.
 *
 * Props are agent-authored. They are kept as data and rendered as text by
 * the view; nothing here interprets them.
 */

import type { Frame } from "./types.ts";

/** The frozen allow-list, equal to `GENERATIVE_UI_COMPONENTS` in `agui_stream.py`. */
export const ALLOWED_COMPONENTS = [
  "agent_card",
  "approval_card",
  "choice_prompt",
  "diff_summary",
  "metric",
  "progress",
] as const;

export type Component = (typeof ALLOWED_COMPONENTS)[number];

const ALLOWED: ReadonlySet<string> = new Set(ALLOWED_COMPONENTS);

export const GENERATIVE_UI_EVENT = "cao.generative_ui";

/** Who an entry belongs to: a subagent when the stream names one, else the raw sender label. */
export interface Owner {
  subagentRunId?: string;
  sender?: string;
}

export type Entry =
  | { kind: "component"; eventId: string; component: Component; props: Record<string, unknown>; owner: Owner }
  | { kind: "refused"; eventId: string; component: string; owner: Owner };

export interface Panel {
  entries: Entry[];
  seen: ReadonlySet<string>;
}

export function emptyPanel(): Panel {
  return { entries: [], seen: new Set() };
}

function caoMeta(frame: Frame): Record<string, unknown> {
  const metadata = frame.metadata as { cao?: Record<string, unknown> } | undefined;
  return metadata?.cao ?? {};
}

function eventIdOf(frame: Frame, fallback: unknown): string {
  const id = caoMeta(frame).eventId ?? fallback;
  return typeof id === "string" ? id : "";
}

function ownerOf(frame: Frame): Owner {
  const owner: Owner = {};
  if (typeof frame.subagentRunId === "string") owner.subagentRunId = frame.subagentRunId;
  const sender = caoMeta(frame).terminalId;
  if (typeof sender === "string") owner.sender = sender;
  return owner;
}

/** Plain-text owner label. A sender that is not a subagent is shown as such. */
export function ownerText(owner: Owner): string {
  if (owner.subagentRunId) return owner.subagentRunId;
  if (owner.sender) return `sender ${owner.sender} (not a subagent)`;
  return "unattributed";
}

/** Map one frame to a panel entry, or `null` when it is not a generative-UI frame. */
export function entryFor(frame: Frame): Entry | null {
  if (frame.type === "CUSTOM" && frame.name === GENERATIVE_UI_EVENT) {
    const value = (frame.value ?? {}) as Record<string, unknown>;
    const component = typeof value.component === "string" ? value.component : "";
    const eventId = eventIdOf(frame, value.event_id);
    if (!ALLOWED.has(component)) return { kind: "refused", eventId, component, owner: ownerOf(frame) };
    const props = value.props && typeof value.props === "object" ? (value.props as Record<string, unknown>) : {};
    return { kind: "component", eventId, component: component as Component, props, owner: ownerOf(frame) };
  }
  if (frame.type === "RAW") {
    const event = (frame.event ?? {}) as Record<string, unknown>;
    if (typeof event.rejected_component !== "string") return null;
    return {
      kind: "refused",
      eventId: eventIdOf(frame, event.event_id),
      component: event.rejected_component,
      owner: ownerOf(frame),
    };
  }
  return null;
}

/** Add one frame. A repeated event id (a `<rid>.<i>` cursor replays a whole record) is dropped. */
export function reduceFrame(panel: Panel, frame: Frame): Panel {
  const entry = entryFor(frame);
  if (!entry) return panel;
  if (entry.eventId && panel.seen.has(entry.eventId)) return panel;
  const seen = new Set(panel.seen);
  if (entry.eventId) seen.add(entry.eventId);
  return { entries: [...panel.entries, entry], seen };
}

/** The props of an ambient `approval_card` that names an interrupt, for a hint only. */
export function approvalHintProps(frame: Frame): Record<string, unknown> | null {
  const entry = entryFor(frame);
  if (entry?.kind !== "component" || entry.component !== "approval_card") return null;
  return typeof entry.props.interrupt_id === "string" ? entry.props : null;
}

/** Props as `[key, text]` rows. Non-string values are JSON text, never markup. */
export function propRows(props: Record<string, unknown>, maxChars = 2000): Array<[string, string]> {
  return Object.entries(props).map(([key, value]) => {
    const text = typeof value === "string" ? value : JSON.stringify(value) ?? String(value);
    return [key, text.length > maxChars ? `${text.slice(0, maxChars)}…` : text];
  });
}
