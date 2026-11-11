/**
 * Session timeline (L3): a pure reducer from ambient-stream frames to one
 * entry per orchestration call, in arrival order.
 *
 * The wire (`services/agui/ambient_run.py`) turns a supervisor's `assign` or
 * `handoff` into `TOOL_CALL_START` and closes that call with `TOOL_CALL_END`
 * when the receiver's work ends (`metadata.cao.closedBy: "completion"`). A call
 * is keyed by `toolCallId`.
 *
 * The wire sends three `metadata.cao.closedBy` values on a `TOOL_CALL_END`:
 * - `"completion"`: the receiver's work ended. The call is "completed".
 * - `"superseded"`: a newer call opened for the same receiver, so the server ended
 *   this one (`_open_call`). It will send no other end and replay no start for it.
 *   The call is "superseded": ended, not completed.
 * - `"stream_closed"`: the stream itself ended (the server stopped, or a client
 *   asked for `maxSeconds`). That says the stream ended, not that the work did,
 *   and the next stream announces the call again, so this end is ignored and the
 *   call stays open.
 * Any other value is treated as final too ("ended", with the value kept as text),
 * so a value added later cannot leave a call shown as running.
 *
 * Only metadata is on the wire, so an entry shows who called whom and how far the
 * call got, never a message body.
 *
 * Every string is agent-influenced. It is kept as data, capped, and rendered
 * as text by the view; nothing here interprets it.
 *
 * Reconnects: each new stream is a fresh run on the server, and its replay
 * (`since` or `Last-Event-ID`) does not close a call whose start came before the
 * replay window, so an end that fell inside the gap is never delivered (the end
 * lost across a reconnect gap: #1292). When the
 * stream opens again, {@link markReconnect} turns every call still open into
 * "unconfirmed": its end was not seen across a reconnect. Nothing is guessed. An
 * unconfirmed call completes (or ends) if a `TOOL_CALL_END` for it arrives later.
 * A call whose start arrives in the replay is a new, open entry only when it is not
 * already listed: a listed unconfirmed call whose replay holds its start but no end
 * stays unconfirmed.
 *
 * Bound: when the timeline must drop an entry, it drops the oldest finished one
 * (completed, superseded or ended) before any open or unconfirmed one, so history
 * goes before live calls.
 *
 * Out-of-order input: a `TOOL_CALL_END` for a call this timeline has no entry
 * for is ignored, and is not remembered. The server emits a call's start before
 * its end, and a replay keeps that order. An end can only arrive alone when its
 * start was dropped from the bounded timeline or predates the replay window, and
 * an entry with no start would only mislead.
 */

import type { Frame } from "./types.ts";
import type { Owner } from "./genui.ts";

export const TOOL_CALL_START = "TOOL_CALL_START";
export const TOOL_CALL_END = "TOOL_CALL_END";

/** The `metadata.cao.closedBy` that completes a call. */
export const CLOSED_BY_COMPLETION = "completion";
/** The `metadata.cao.closedBy` of a call ended because a newer one opened for its receiver. */
export const CLOSED_BY_SUPERSEDED = "superseded";
/** The `metadata.cao.closedBy` of a stream that ended: ignored, the call stays open. */
export const CLOSED_BY_STREAM_CLOSED = "stream_closed";

/** The timeline keeps the newest this many entries; older ones are dropped. */
export const MAX_TIMELINE_ENTRIES = 200;

/**
 * The newest this many dedupe keys are remembered. It is larger than
 * {@link MAX_TIMELINE_ENTRIES}, so a replay after a reconnect does not bring
 * back a call that was dropped from the timeline a little earlier.
 */
export const MAX_TIMELINE_SEEN = 1000;

/** The longest string kept from the wire; longer ones are cut, with an ellipsis. */
export const MAX_FIELD_CHARS = 200;

export interface TimelineEntry {
  toolCallId: string;
  /**
   * "open": started, no end seen. "unconfirmed": was open when the stream reconnected,
   * and its end was not seen across the reconnect. "completed": the receiver's work
   * ended. "superseded": a newer call to the same receiver ended it. "ended": an end
   * with another `closedBy` (kept in `closedBy`).
   */
  state: "open" | "unconfirmed" | "completed" | "superseded" | "ended";
  /** The `closedBy` of an "ended" call, as capped text. */
  closedBy?: string;
  /** `metadata.cao.orchestrationType`, such as "assign" or "handoff". */
  orchestrationType?: string;
  /** The wire's `toolCallName`. */
  name?: string;
  sender?: string;
  receiver?: string;
  sessionName?: string;
  /** The subagent the call is attributed to (#1232), else only `sender` is known. */
  owner: Owner;
  startedAt?: number;
  endedAt?: number;
}

export interface Timeline {
  entries: TimelineEntry[];
  seen: ReadonlySet<string>;
}

export function emptyTimeline(): Timeline {
  return { entries: [], seen: new Set() };
}

function caoMeta(frame: Frame): Record<string, unknown> {
  const metadata = frame.metadata as { cao?: Record<string, unknown> } | undefined;
  return metadata?.cao ?? {};
}

function text(value: unknown): string | undefined {
  if (typeof value !== "string" || value === "") return undefined;
  return value.length > MAX_FIELD_CHARS ? `${value.slice(0, MAX_FIELD_CHARS)}…` : value;
}

function ownerOf(frame: Frame, sender: string | undefined): Owner {
  const owner: Owner = {};
  const run = text(frame.subagentRunId);
  if (run) owner.subagentRunId = run;
  if (sender) owner.sender = sender;
  return owner;
}

function startEntry(frame: Frame, toolCallId: string): TimelineEntry {
  const cao = caoMeta(frame);
  const sender = text(cao.sender);
  const entry: TimelineEntry = { toolCallId, state: "open", owner: ownerOf(frame, sender) };
  const fields = {
    orchestrationType: text(cao.orchestrationType),
    name: text(frame.toolCallName),
    sender,
    receiver: text(cao.receiver),
    sessionName: text(cao.sessionName),
  };
  for (const [key, value] of Object.entries(fields)) {
    if (value !== undefined) (entry as unknown as Record<string, string>)[key] = value;
  }
  if (typeof frame.timestamp === "number") entry.startedAt = frame.timestamp;
  return entry;
}

function remember(seen: ReadonlySet<string>, key: string): Set<string> {
  const next = new Set(seen);
  next.add(key);
  // A Set iterates in insertion order, so the first key is the oldest.
  for (const old of next) {
    if (next.size <= MAX_TIMELINE_SEEN) break;
    next.delete(old);
  }
  return next;
}

/** Whether a call has ended for good (anything but open or unconfirmed). */
export function isFinished(entry: TimelineEntry): boolean {
  return entry.state !== "open" && entry.state !== "unconfirmed";
}

/**
 * Keep at most {@link MAX_TIMELINE_ENTRIES}. Entries are added one at a time, so at
 * most one has to go: the oldest finished entry, or the oldest live one when every
 * entry is still open or unconfirmed.
 */
function bounded(entries: TimelineEntry[]): TimelineEntry[] {
  if (entries.length <= MAX_TIMELINE_ENTRIES) return entries;
  const finished = entries.findIndex(isFinished);
  const drop = finished < 0 ? 0 : finished;
  return entries.filter((_, i) => i !== drop);
}

/** The state an end with this `closedBy` gives a call, or `null` when the end is ignored. */
function endState(closedBy: unknown): Pick<TimelineEntry, "state" | "closedBy"> | null {
  if (closedBy === CLOSED_BY_COMPLETION) return { state: "completed" };
  if (closedBy === CLOSED_BY_SUPERSEDED) return { state: "superseded" };
  if (closedBy === CLOSED_BY_STREAM_CLOSED) return null;
  return { state: "ended", closedBy: text(closedBy) ?? "unknown" };
}

/**
 * Add one frame. A start opens an entry; a repeated start (a replay, or the same
 * `metadata.cao.eventId` again) is dropped. An end closes the entry with its
 * `toolCallId` once (see the header for the `closedBy` values); an end for an
 * unknown or already finished call, or one that only closes a stream, changes nothing.
 */
export function reduceTimeline(timeline: Timeline, frame: Frame): Timeline {
  const toolCallId = text(frame.toolCallId);
  if (!toolCallId) return timeline;
  if (frame.type === TOOL_CALL_START) {
    const key = text(caoMeta(frame).eventId) ?? toolCallId;
    if (timeline.seen.has(key) || timeline.entries.some((e) => e.toolCallId === toolCallId)) return timeline;
    return {
      entries: bounded([...timeline.entries, startEntry(frame, toolCallId)]),
      seen: remember(timeline.seen, key),
    };
  }
  if (frame.type === TOOL_CALL_END) {
    const at = timeline.entries.findIndex((e) => e.toolCallId === toolCallId);
    const ended = endState(caoMeta(frame).closedBy);
    if (!ended || at < 0 || isFinished(timeline.entries[at])) return timeline;
    const closed: TimelineEntry = { ...timeline.entries[at], ...ended };
    if (typeof frame.timestamp === "number") closed.endedAt = frame.timestamp;
    return { entries: timeline.entries.map((e, i) => (i === at ? closed : e)), seen: timeline.seen };
  }
  return timeline;
}

/**
 * The stream opened again after a drop: every call still open becomes "unconfirmed".
 * Returns the same timeline when nothing is open.
 */
export function markReconnect(timeline: Timeline): Timeline {
  if (!timeline.entries.some((e) => e.state === "open")) return timeline;
  return {
    entries: timeline.entries.map((e) => (e.state === "open" ? { ...e, state: "unconfirmed" } : e)),
    seen: timeline.seen,
  };
}

/** The panel's text for a call's state. */
export function stateLabel(entry: TimelineEntry): string {
  if (entry.state === "unconfirmed") return "open; its end was not seen across a reconnect";
  if (entry.state === "superseded") return "ended: superseded by a newer call to the same receiver";
  if (entry.state === "ended") return `ended: ${entry.closedBy}`;
  return entry.state;
}

/** One line for the panel: "assign: <sender> → <receiver>", with "?" for a missing side. */
export function callText(entry: TimelineEntry): string {
  const kind = entry.orchestrationType ?? entry.name ?? "call";
  return `${kind}: ${entry.sender ?? "?"} → ${entry.receiver ?? "?"}`;
}

/** A wire timestamp (epoch ms) as ISO text, or "unknown" when it is not a usable time. */
export function timeText(ms: number | undefined): string {
  if (ms === undefined || !Number.isFinite(ms)) return "unknown";
  const date = new Date(ms);
  return Number.isNaN(date.getTime()) ? "unknown" : date.toISOString();
}
