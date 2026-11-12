/**
 * The fleet: the sessions and terminals one `cao-server` can see, folded from the
 * ambient stream's `STATE_SNAPSHOT` and `STATE_DELTA` frames, and grouped by the
 * runner each terminal runs on.
 *
 * A *runner* is a remote execution unit. A terminal whose `runner_id` is `null`, or
 * absent (every row from a server that predates runners), is local. The fleet is the
 * whole set, local and remote together.
 *
 * Pure: no I/O, no DOM. Everything a frame carries is kept as data; nothing here
 * evaluates or interprets it. The only fields kept are the ones the server documents
 * for a terminal row and for a catalog entry, so a field the server never promised
 * cannot reach the view.
 *
 * The delta applier handles only the operations the server sends (whole-key `replace`
 * of `/terminals`, `/sessions` and `/profiles`, and per-key `add`, `replace` and
 * `remove` under `/counts`). It is all-or-nothing: if any operation in a delta is not
 * understood, none is applied, the fleet keeps what it had and is marked `stale` until
 * the next `STATE_SNAPSHOT`. It never throws and never applies half a patch.
 */

import type { Frame } from "./types.ts";

/** What the local group is called. */
export const LOCAL_LABEL = "local";

export interface FleetTerminal {
  id: string;
  sessionName: string;
  provider: string;
  agentProfile: string | null;
  window: string | null;
  status: string | null;
  lastActive: string | null;
  /** The runner the terminal runs on; `null` is local. */
  runnerId: string | null;
}

export interface FleetSession {
  id: string;
  name: string;
  status: string;
}

/** One entry of the read-only profile catalog. These four fields are the whole wire shape. */
export interface ProfileEntry {
  name: string;
  source: string;
  role: string | null;
  loadable: boolean;
}

export interface Fleet {
  /** A `STATE_SNAPSHOT` has been folded. Deltas before that are ignored. */
  hydrated: boolean;
  /** A delta could not be applied, so this may not match the server until the next snapshot. */
  stale: boolean;
  sessions: FleetSession[];
  terminals: FleetTerminal[];
  counts: Record<string, number>;
  profiles: ProfileEntry[];
}

export function emptyFleet(): Fleet {
  return { hydrated: false, stale: false, sessions: [], terminals: [], counts: {}, profiles: [] };
}

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const str = (v: unknown, fallback = ""): string => (typeof v === "string" ? v : fallback);
const strOrNull = (v: unknown): string | null => (typeof v === "string" ? v : null);

function toTerminal(row: unknown): FleetTerminal | null {
  if (!isObject(row) || typeof row.id !== "string") return null;
  return {
    id: row.id,
    sessionName: str(row.session_name),
    provider: str(row.provider),
    agentProfile: strOrNull(row.agent_profile),
    window: strOrNull(row.window),
    status: strOrNull(row.status),
    lastActive: strOrNull(row.last_active),
    runnerId: strOrNull(row.runner_id),
  };
}

function toSession(row: unknown): FleetSession | null {
  if (!isObject(row) || typeof row.id !== "string") return null;
  return { id: row.id, name: str(row.name, row.id), status: str(row.status, "unknown") };
}

function toProfile(row: unknown): ProfileEntry | null {
  if (!isObject(row) || typeof row.name !== "string") return null;
  return { name: row.name, source: str(row.source), role: strOrNull(row.role), loadable: row.loadable === true };
}

function listOf<T>(value: unknown, convert: (row: unknown) => T | null): T[] {
  if (!Array.isArray(value)) return [];
  return value.map(convert).filter((row): row is T => row !== null);
}

function toCounts(value: unknown): Record<string, number> {
  if (!isObject(value)) return {};
  const counts: Record<string, number> = {};
  for (const [key, n] of Object.entries(value)) if (typeof n === "number" && Number.isFinite(n)) counts[key] = n;
  return counts;
}

function fromSnapshot(snapshot: Record<string, unknown>): Fleet {
  return {
    hydrated: true,
    stale: false,
    sessions: listOf(snapshot.sessions, toSession),
    terminals: listOf(snapshot.terminals, toTerminal),
    counts: toCounts(snapshot.counts),
    profiles: listOf(snapshot.profiles, toProfile),
  };
}

/** RFC 6901: `~1` is `/` and `~0` is `~`, in that order. */
const unescapeToken = (token: string): string => token.replace(/~1/g, "/").replace(/~0/g, "~");

/** Apply one operation to `fleet` (already a private copy), or return false if it is not understood. */
function applyOp(fleet: Fleet, op: unknown): boolean {
  if (!isObject(op) || typeof op.path !== "string" || !op.path.startsWith("/")) return false;
  if (op.op !== "add" && op.op !== "replace" && op.op !== "remove") return false;
  const [head, ...rest] = op.path.slice(1).split("/").map(unescapeToken);
  const remove = op.op === "remove";
  if (rest.length === 0) {
    switch (head) {
      case "terminals":
        fleet.terminals = remove ? [] : listOf(op.value, toTerminal);
        return true;
      case "sessions":
        fleet.sessions = remove ? [] : listOf(op.value, toSession);
        return true;
      case "profiles":
        fleet.profiles = remove ? [] : listOf(op.value, toProfile);
        return true;
      case "counts":
        fleet.counts = remove ? {} : toCounts(op.value);
        return true;
      case "scopes":
        return true; // The granted scope set. The dashboard has no use for it.
      default:
        return false;
    }
  }
  if (head === "counts" && rest.length === 1) {
    if (remove) {
      delete fleet.counts[rest[0]];
      return true;
    }
    if (typeof op.value !== "number" || !Number.isFinite(op.value)) return false;
    fleet.counts[rest[0]] = op.value;
    return true;
  }
  return false;
}

function applyDelta(fleet: Fleet, ops: unknown): Fleet {
  if (!fleet.hydrated) return fleet;
  if (!Array.isArray(ops)) return { ...fleet, stale: true };
  const next: Fleet = { ...fleet, counts: { ...fleet.counts } };
  for (const op of ops) if (!applyOp(next, op)) return { ...fleet, stale: true };
  return next;
}

/** Fold one ambient frame. Frames that are not fleet state return the same object. */
export function reduceFleet(fleet: Fleet, frame: Frame): Fleet {
  if (frame.type === "STATE_SNAPSHOT") return isObject(frame.snapshot) ? fromSnapshot(frame.snapshot) : fleet;
  if (frame.type === "STATE_DELTA") return applyDelta(fleet, frame.delta);
  return fleet;
}

export interface SessionGroup {
  name: string;
  status: string;
  terminals: FleetTerminal[];
}

export interface RunnerGroup {
  /** `null` is the local group. */
  runnerId: string | null;
  label: string;
  terminals: FleetTerminal[];
  /** Terminals waiting on an answer. */
  waiting: number;
  /** Keyed by (runner, session name), so two runners may both own a session called `main`. */
  sessions: SessionGroup[];
}

// Only called with two different keys: the groups are a Map keyed by runner id.
const byRunner = (a: string | null, b: string | null): number => {
  if (a === null) return -1;
  if (b === null) return 1;
  return a < b ? -1 : 1;
};

/** The terminals grouped by runner: local first, then by id. An empty fleet has no groups. */
export function groupByRunner(fleet: Fleet): RunnerGroup[] {
  const groups = new Map<string | null, RunnerGroup>();
  const status = new Map(fleet.sessions.map((s) => [s.name, s.status]));
  for (const t of fleet.terminals) {
    let group = groups.get(t.runnerId);
    if (!group) {
      group = { runnerId: t.runnerId, label: t.runnerId ?? LOCAL_LABEL, terminals: [], waiting: 0, sessions: [] };
      groups.set(t.runnerId, group);
    }
    group.terminals.push(t);
    if (t.status === "waiting_user_answer") group.waiting += 1;
    let session = group.sessions.find((s) => s.name === t.sessionName);
    if (!session) {
      session = { name: t.sessionName, status: status.get(t.sessionName) ?? "unknown", terminals: [] };
      group.sessions.push(session);
    }
    session.terminals.push(t);
  }
  return [...groups.values()].sort((a, b) => byRunner(a.runnerId, b.runnerId));
}

/**
 * The `agent_card` props for a terminal, composed here from the terminal and the profile
 * catalog. The card is built on the client from the server's own snapshot, not injected
 * as an agent-authored frame, so it can only say what CAO's catalog says and the
 * `emit_ui` allow-list stays as it is. Values are text; nothing is markup.
 */
export function agentCardFor(terminal: FleetTerminal, profiles: ProfileEntry[]): Record<string, string> {
  const card: Record<string, string> = {
    name: terminal.agentProfile ?? terminal.id,
    provider: terminal.provider,
    status: terminal.status ?? "unknown",
  };
  const entry = terminal.agentProfile === null ? undefined : profiles.find((p) => p.name === terminal.agentProfile);
  if (entry) {
    card.profile = entry.name;
    if (entry.role !== null) card.role = entry.role;
    card.source = entry.source;
    card.loadable = entry.loadable ? "yes" : "no";
  }
  return card;
}
