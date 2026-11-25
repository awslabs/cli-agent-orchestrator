// Pure rules for the CAO window. No Herdr imports, so `node lib/logic.test.mjs` runs it. No function
// mutates its arguments.
//
// Top-level names are unique across lib/*.js, because bundle mode puts every shared module and the mod's own code in one
// scope. Helpers below carry names no other module or mod is likely to reuse. Source stays ASCII: the
// middle dot and the recap mark are written as escapes.
import { HONESTY } from "./honesty.js";

// curl run with `-w "\n%{http_code}"` prints the body, a newline, then the code. A missing or
// non-numeric code line, and curl's "000", mean no HTTP response arrived: code 0.
export function splitStatus(stdout) {
  const text = (stdout ?? "").replace(/\n+$/, "");
  const i = text.lastIndexOf("\n");
  const last = text.slice(i + 1).trim();
  if (!/^\d{3}$/.test(last)) return { body: text, code: 0 };
  return { body: i < 0 ? "" : text.slice(0, i), code: Number(last) };
}

// A terminal label's value from labels in either form the core may report: {key: value} or
// ["key=value"] (split at the first "="). undefined when there is no such key or no string value.
export function labelValue(labels, key) {
  if (Array.isArray(labels)) {
    const hit = labels.find((l) => typeof l === "string" && l.startsWith(`${key}=`));
    return hit === undefined ? undefined : hit.slice(key.length + 1);
  }
  if (!labels || typeof labels !== "object" || !Object.hasOwn(labels, key)) return undefined;
  return typeof labels[key] === "string" ? labels[key] : undefined;
}

// The Herdr space id the herdr-app backend gives a CAO session (backends/herdr_app_backend.py
// space_id_for). The hash is passed in so this module stays free of runtime imports.
export const spaceIdFor = (name, sha256hex) => `sp${sha256hex(`cao:${name}`).slice(0, 32)}`;

const localId = (id) => id.slice(id.lastIndexOf("/") + 1);
const isString = (v) => typeof v === "string" && v !== "";

// The machine cao-server runs on, first match wins:
//   1. props.machine, when it is a non-empty string;
//   2. the machine of a space whose local id (after any "<machine>/") is a known session's space id;
//   3. the machine a terminals() entry with a non-empty cao.session label names in its
//      "<machine>/" address prefix (a bare address is this computer and is skipped);
//   4. "cao".
// No step trusts a space id's shape: Herdr gives every space an "sp" + 32-hex id, so only a session
// name that hashes to the id says the space is CAO's.
export function caoMachine(spaceList, sessionNames, props, sha256hex, terminalList = []) {
  if (isString(props?.machine)) return props.machine;
  const remote = (Array.isArray(spaceList) ? spaceList : []).filter((s) => typeof s?.id === "string" && isString(s.machine));
  const ids = new Set((sessionNames ?? []).map((n) => spaceIdFor(n, sha256hex)));
  const known = remote.find((s) => ids.has(localId(s.id)));
  if (known) return known.machine;
  for (const t of Array.isArray(terminalList) ? terminalList : []) {
    if (!isString(labelValue(t?.labels, "cao.session")) || typeof t.address !== "string") continue;
    const slash = t.address.indexOf("/");
    if (slash > 0) return t.address.slice(0, slash);
  }
  return "cao";
}

// ---------- the terminal adapter ----------

const plainObject = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : null);
const textOrUndefined = (v) => (typeof v === "string" && v !== "" ? v : undefined);

// A finite number from a number or a plain decimal string; undefined for anything else (a blank string,
// "NaN", null and booleans included).
function finiteNumber(v) {
  if (typeof v === "number") return Number.isFinite(v) ? v : undefined;
  if (typeof v !== "string" || !/^\s*-?\d+(\.\d+)?\s*$/.test(v)) return undefined;
  return Number(v);
}

// The count in CAO's `cao.waiting` label: how many workers the terminal waits on. CAO writes a plain decimal integer
// and removes the label when it is 0, so only that shape counts: 1 to 999, no sign, space, leading zero, fraction or exponent.
// Anything else, a missing label included, is 0, so a value this window does not understand never makes an agent look busy.
const WAITING_COUNT = /^[1-9]\d{0,2}$/;
const waitingFrom = (value) => (typeof value === "string" && WAITING_COUNT.test(value) ? Number(value) : 0);

// The program Herdr resolved for a terminal (its `program` field: the app's table maps the agent's reported `app`, such as
// `claude-code`, `kiro-cli` or `codex`, to a name and an icon) -> {icon, tint}, the two things the window draws; undefined when the
// record has none. `icon` is a path in the home folder, and `tint` says it is a one-colour glyph (Herdr's Program type).
function programOf(p) {
  const o = plainObject(p);
  return o === null ? undefined : { icon: textOrUndefined(o.icon), tint: Boolean(o.tint) };
}

// One pushed terminals() record -> the fields this module uses. `state` is the Herdr word (working,
// blocked, idle, done, error ...); `kind` and `msg` come from the agent's status report when present;
// `waiting` is the count in the `cao.waiting` label (waitingFrom), 0 when there is none.
// Herdr's record is documented two ways (a state word in `status`, or an object there or in `report`),
// so both are read. An empty cao.profile or cao.caller label counts as absent.
export function termOf(raw) {
  const labels = raw?.labels;
  const fromStatus = plainObject(raw?.status);
  const fromReport = plainObject(raw?.report);
  const text = (key) => [fromReport?.[key], fromStatus?.[key]].find((v) => typeof v === "string");
  const word = typeof raw?.status === "string" && raw.status !== "" ? raw.status : text("state");
  return {
    address: raw?.address,
    id: labelValue(labels, "cao.terminal_id"),
    session: labelValue(labels, "cao.session"),
    window: labelValue(labels, "cao.window"),
    profile: textOrUndefined(labelValue(labels, "cao.profile")),
    caller: textOrUndefined(labelValue(labels, "cao.caller")),
    state: word ?? "unknown",
    kind: text("kind"),
    msg: text("msg"),
    statusMs: finiteNumber(raw?.status_ms),
    turn: finiteNumber(raw?.turn) ?? 0,
    seen: finiteNumber(labelValue(labels, "herdr.seen")),
    createdMs: finiteNumber(raw?.created_ms) ?? 0,
    program: programOf(raw?.program),
    waiting: waitingFrom(labelValue(labels, "cao.waiting")),
  };
}

// The CAO terminals of a terminals() list: those with a session and a terminal id label.
export const caoTerms = (list) => (Array.isArray(list) ? list : []).map(termOf).filter((t) => t.session && t.id);

// ---------- talkable agents and workers ----------

const oldestOf = (list) => list.reduce((a, b) => (b.createdMs < a.createdMs ? b : a));
const listOrDefault = (v, fallback) => (Array.isArray(v) ? v : fallback);

// One session's terminals -> who is talkable, who is a worker, and whether it is a recorded draft
// author session. `drafts` is the list of terminal ids this window recorded when its own Draft launch
// returned 201; a name never marks a draft. A terminal with no profile label yet is not guessed at:
// with no other talkable agent the session shows its first such terminal (`provisional`) until the profile
// fallback (fetching the terminal from CAO) answers.
export function classify(terms, props = {}, drafts = []) {
  const list = Array.isArray(terms) ? terms : [];
  const hidden = listOrDefault(props?.hiddenRootProfiles, ["memory_manager"]);
  const allow = listOrDefault(props?.talkableProfiles, []);
  const recorded = new Set(listOrDefault(drafts, []));
  const unresolved = list.filter((t) => t.profile === undefined);
  let talkable = list.filter((t) => t.profile !== undefined && ((!t.caller && !hidden.includes(t.profile)) || allow.includes(t.profile)));
  let bestGuess = false;
  let provisional = false;
  if (talkable.length === 0 && unresolved.length > 0) {
    talkable = [unresolved[0]];
    provisional = true;
  } else if (talkable.length === 0 && list.length > 0) {
    talkable = [oldestOf(list)];
    bestGuess = true;
  }
  const chosen = new Set(talkable);
  const workers = list.filter((t) => !chosen.has(t));
  const draft = list.length > 0 && list.every((t) => recorded.has(t.id));
  return { talkable, workers, bestGuess, provisional, draft, label: bestGuess ? HONESTY.bestGuess : null };
}

// CAO terminals -> [{name, terms}], sessions in the order their first terminal appears.
export function sessionsFrom(terms) {
  const by = new Map();
  for (const t of Array.isArray(terms) ? terms : []) {
    if (!by.has(t.session)) by.set(t.session, []);
    by.get(t.session).push(t);
  }
  return [...by].map(([name, ts]) => ({ name, terms: ts }));
}

// Oldest session first: a session's age is the creation time of its oldest terminal that has one, so the order is the order
// the sessions were started in and no state moves a row (the marks, the status bar counts and the filters carry what needs attention). Equal
// ages go by session name (code unit order); a session none of whose terminals has a creation time comes after the dated ones, by name. A
// recorded draft session is last, in the order given. The input array is not changed.
export function sortSessions(sessions) {
  const ageOf = (s) => {
    const known = s.terms.map((t) => t.createdMs).filter((ms) => Number.isFinite(ms) && ms > 0);
    return known.length > 0 ? Math.min(...known) : Infinity;
  };
  const byName = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
  const rows = (Array.isArray(sessions) ? sessions : []).map((s, i) => ({ s, i, age: ageOf(s) }));
  const order = (a, b) => {
    if (a.s.draft || b.s.draft) return (a.s.draft ? 1 : 0) - (b.s.draft ? 1 : 0) || a.i - b.i;
    return (a.age < b.age ? -1 : a.age > b.age ? 1 : 0) || byName(a.s.name, b.s.name) || a.i - b.i;
  };
  return rows.sort(order).map((x) => x.s);
}

// ---------- the tree of who started whom ----------

// Older first: by creation time, then by id.
export const olderFirst = (a, b) => a.createdMs - b.createdMs || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);

// The terminals by id and each terminal's parent: the terminal its caller names, when that one is among them. A cycle (a terminal that
// names itself included) is cut at its member created first, then the lowest id, which gets no parent, so no walk up the tree loops.
export function callerTree(terms) {
  const byId = new Map(terms.map((t) => [t.id, t]));
  const parent = new Map();
  for (const t of terms) if (t.caller && byId.has(t.caller)) parent.set(t.id, t.caller);
  const done = new Set();
  for (const t of terms) {
    const path = [];
    const at = new Map();
    let id = t.id;
    while (id !== undefined && !done.has(id) && !at.has(id)) {
      at.set(id, path.length);
      path.push(id);
      id = parent.get(id);
    }
    if (id !== undefined && at.has(id)) {
      const oldest = path.slice(at.get(id)).map((x) => byId.get(x)).reduce((a, b) => (olderFirst(b, a) < 0 ? b : a));
      parent.delete(oldest.id);
    }
    for (const x of path) done.add(x);
  }
  return { byId, parent };
}

// The tree of one session: callerTree, `unlisted` (the ids whose parent is not known, which the Agents block marks "parent not listed": a
// caller that is not among the session's terminals, or a terminal whose profile and caller are still unresolved after the lookups, the
// session's main agent excepted, so that a lost answer does not draw it, and what hangs on it, as a root).
export function sessionTree(session) {
  const terms = Array.isArray(session?.terms) ? session.terms : [...listOrDefault(session?.talkable, []), ...listOrDefault(session?.workers, [])];
  const { byId, parent } = callerTree(terms);
  const mainId = listOrDefault(session?.talkable, [])[0]?.id;
  const unlisted = new Set(terms.filter((t) => !parent.has(t.id) && (t.caller ? !byId.has(t.caller) : t.profile === undefined && t.id !== mainId)).map((t) => t.id));
  return { byId, parent, unlisted, terms };
}

// The agent tabs: the talkable agents plus pinned workers; a pin decides the tab and nothing else: the Agents block lists every worker.
// One shown agent is one tab; none shown, none.
export function tabsFor(session, pins = []) {
  const pinned = new Set(listOrDefault(pins, []));
  return [...session.talkable, ...session.workers.filter((w) => pinned.has(w.id))];
}

// ---------- pins (the window's own rule) ----------

// A pin goes only when a snapshot that lists at least one CAO terminal no longer lists its id. An
// empty, missing or disconnected snapshot never prunes.
export function prunePins(pins, snapshot) {
  const own = listOrDefault(pins, []);
  if (!Array.isArray(snapshot) || snapshot.length === 0) return { pins: own, changed: false };
  const live = new Set(snapshot.map((t) => t.id));
  const next = own.filter((id) => live.has(id));
  return { pins: next, changed: next.length !== own.length };
}

// ---------- quiet workers ----------

export const initialObs = (now) => ({ subscribedAt: now, seeded: false, sig: {}, at: {} });

// Record, per terminal, the time this window saw its status change. The first call seeds: it records
// signatures only, so an event that predates this subscription is not known and never counts (a
// terminal in that snapshot counts from the subscription). Later calls also stamp a terminal that was
// absent from the previous snapshot with `now`, so a worker first seen late counts from when it
// appeared; a repeat of the same record keeps its time.
export function observe(obs, terms, now) {
  const sig = {};
  const at = { ...obs.at };
  for (const t of terms) {
    sig[t.id] = `${t.state}|${t.msg ?? ""}|${t.turn}`;
    if (!obs.seeded) continue;
    if (obs.sig?.[t.id] === undefined || obs.sig[t.id] !== sig[t.id]) at[t.id] = now;
  }
  return { subscribedAt: obs.subscribedAt, seeded: true, sig, at };
}

// "stuck?" when a working worker has had no status change observed by this window for quietMinutes,
// counted from the later of its last observed change and this window's subscription.
export function quietHint(session, obs, quietMinutes, now) {
  const limit = quietMinutes * 60_000;
  const quiet = (session?.workers ?? []).find(
    (w) => w.state === "working" && now - Math.max(obs?.at?.[w.id] ?? 0, obs?.subscribedAt ?? 0) >= limit,
  );
  return quiet ? { text: "stuck?", label: HONESTY.heuristic, terminalId: quiet.id } : null;
}

// ---------- status shapes ----------

// The mark of a display state: Herdr's own marks, drawn by shapeMark in lib/ui.js the way the shipped Agents
// list draws them. Herdr has four: a spinner for working, a ring for idle, a dot for done, and a "!" square for blocked.
// The states only this window knows borrow the mark of the Herdr status they refine: replied is a finished turn (the done dot, in a
// highlight token of its own, `dot-hi`, because no word tells it apart any more), error counts as blocked in Herdr's own list (mods/agents/main.js:60), a quiet worker is
// still working as far as Herdr knows (the "stuck?" hint says otherwise), and a state nobody knows draws nothing, as in Herdr.
// An agent that waits on its workers is idle to Herdr but busy to the user: Herdr's working spinner, in the muted colour.
const STATUS_SHAPES = { working: "spinner", idle: "ring", done: "dot", replied: "dot-hi", blocked: "bang", quiet: "spinner", starting: "none", error: "bang", waiting: "spinner-dim" };
export const statusShape = (state) => (Object.hasOwn(STATUS_SHAPES, state) ? STATUS_SHAPES[state] : STATUS_SHAPES.starting);

// One of the nine display states from a terminal record plus what the caller knows about it. An agent whose turn ended while it
// waits on workers is `waiting`, and a waiting agent has not replied; blocked, error and working win over the label.
export function displayState(t, { replied = false, quiet = false } = {}) {
  if (t.state === "error") return "error";
  if (t.state === "blocked") return "blocked";
  if (t.state === "working") return quiet ? "quiet" : "working";
  if (t.state === "done" || t.state === "idle") return waitingCount(t) > 0 ? "waiting" : replied ? "replied" : t.state;
  return "starting";
}

// ---------- section summaries and flow links ----------

// Draft author sessions are listed but never count as needing the user.
export function sectionSummary(kind, items) {
  const list = Array.isArray(items) ? items : [];
  if (!list.length) return "none yet";
  const n = list.length;
  if (kind === "flows") {
    const on = list.filter((f) => f.enabled).length;
    return on === 0 ? `${n}, all off` : `${n}, ${on} on`;
  }
  if (kind === "sessions") {
    const asking = list.filter((s) => !s.draft && s.terms.some((t) => t.state === "blocked")).length;
    return asking ? `${n}, ${asking} asking` : `${n}`;
  }
  const running = list.filter((w) => w.state === "running").length;
  return running ? `${n}, ${running} running` : `${n}`;
}

const FLOW_SESSION_PREFIX = "cao-flow-";

// "Started by schedule X" for a session named cao-flow-<X> when CAO's flow X exists (a guess from the name). The person reads "schedule"; the
// session name and the route are CAO's own.
export function flowOfSession(sessionName, flowNames) {
  if (typeof sessionName !== "string" || !sessionName.startsWith(FLOW_SESSION_PREFIX)) return null;
  const flow = sessionName.slice(FLOW_SESSION_PREFIX.length);
  if (flow === "" || !listOrDefault(flowNames, []).includes(flow)) return null;
  return { flow, text: `Started by schedule ${flow}`, label: HONESTY.flowInferred };
}

// ---------- the fallback for terminals without a cao.profile label ----------

export const FALLBACK_RETRY_MS = 30_000;
// A fetch that has not answered after this long is taken as lost (a callback that never completes would leave the entry pending for good, and the
// terminal unresolved, so its place in the tree unknown) and the id is fetched again like a failure.
export const PENDING_EXPIRE_MS = 15_000;

// Ids to fetch now: unlabelled, not already fetched or in flight, a failure waits 30 s, and a fetch in flight is given 15 s (a pending entry
// with no time is due at once).
export function fallbackPlan(cache, terms, now) {
  const known = cache ?? {};
  const ids = new Set();
  for (const t of terms) {
    if (t.profile !== undefined || ids.has(t.id)) continue;
    const c = known[t.id];
    if (c?.state === "ok") continue;
    if (c?.state === "pending" && now - c.at < PENDING_EXPIRE_MS) continue;
    if (c?.state === "failed" && now - c.at < FALLBACK_RETRY_MS) continue;
    ids.add(t.id);
  }
  return [...ids];
}
export const markPending = (cache, ids, now) => ({ ...cache, ...Object.fromEntries(ids.map((id) => [id, { state: "pending", at: now }])) });

// An outcome of GET /terminals/{id} -> the cache with that id resolved, or failed at `now`. A reply
// without a profile name resolves nothing and is retried like any other failure.
export function fallbackResult(cache, id, outcome, now) {
  const json = outcome?.kind === "ok" ? plainObject(outcome.json) : null;
  if (json && typeof json.agent_profile === "string" && json.agent_profile !== "") {
    return { ...cache, [id]: { state: "ok", agent_profile: json.agent_profile, caller_id: json.caller_id ?? null } };
  }
  return { ...cache, [id]: { state: "failed", at: now } };
}
export function withFallback(terms, cache) {
  const known = cache ?? {};
  return terms.map((t) => {
    const c = known[t.id];
    if (t.profile !== undefined || c?.state !== "ok") return t;
    return { ...t, profile: c.agent_profile, caller: c.caller_id || undefined };
  });
}

// ---------- keyboard ----------

// state: {items: visible rows and headers in order, index, folded: {section: bool}}. Returns the new
// index and fold map plus the action the mod should run (open, pin, fold) or null.
export function navStep(state, key) {
  const k = String(key).toLowerCase().replace(/^arrow/, "");
  const { items = [], index, folded = {} } = state;
  const here = items[index];
  const headerOf = (section) => items.findIndex((i) => i.kind === "header" && i.section === section);
  const keep = { index, folded, action: null };
  if (!here) return keep;
  if (k === "down") return { ...keep, index: Math.min(index + 1, items.length - 1) };
  if (k === "up") return { ...keep, index: Math.max(index - 1, 0) };
  if (k === "left") {
    if (here.kind === "row") return { ...keep, index: Math.max(headerOf(here.section), 0) };
    return { index, folded: { ...folded, [here.section]: true }, action: { type: "fold", section: here.section, folded: true } };
  }
  if (k === "right") {
    if (here.kind !== "header") return keep;
    return { index, folded: { ...folded, [here.section]: false }, action: { type: "fold", section: here.section, folded: false } };
  }
  if (k === "enter") {
    if (here.kind === "row") return { ...keep, action: { type: "open", id: here.id, section: here.section } };
    const next = !folded[here.section];
    return { index, folded: { ...folded, [here.section]: next }, action: { type: "fold", section: here.section, folded: next } };
  }
  if (k === "p" && here.kind === "row" && here.worker) return { ...keep, action: { type: "pin", id: here.id } };
  return keep;
}

// ---------- screen-tail readers ----------

const RECAP_LINE = /\u203b\s*recap:(.*)$/s;

// The agent's recap line (a recap mark, then `recap:`) (the last non-empty one on the screen), labelled as its own words.
// Text without such a line, or with an empty one, gives null: nothing is inferred.
export function parseRecap(text) {
  if (typeof text !== "string") return null;
  let found = null;
  for (const line of text.split("\n")) {
    const m = RECAP_LINE.exec(line);
    const recap = m ? m[1].trim() : "";
    if (recap !== "") found = recap;
  }
  return found ? { text: found, label: HONESTY.recap } : null;
}

// A context percentage from the screen tail, only when the operator configured a regex (its first
// capture group is the number, or the whole match without a group). Off by default; a bad regex or a
// number outside 0-100 reads as off.
export function contextPercent(text, regex) {
  if (typeof regex !== "string" || regex === "" || typeof text !== "string") return null;
  let re;
  try { re = new RegExp(regex); } catch { return null; }
  const m = re.exec(text);
  const n = m ? finiteNumber(m[1] ?? m[0]) : undefined;
  return n !== undefined && n >= 0 && n <= 100 ? { percent: n, label: HONESTY.heuristic } : null;
}

// The known-gap note for session details: "done" means the turn ended.
export function knownGapNote() {
  return {
    label: HONESTY.knownGap,
    doneMeans: "\"Done\" means the agent's turn ended.",
    text: "An agent that finished by asking you something in prose is not counted as asking.",
  };
}

// ---------- seen: "replied" and the herdr.seen label ----------

export const SEEN_VERIFY_MS = 5000;
// "write" (default): write herdr.seen on focus and verify it. "off": stock Herdr writes it.
// "local": the write did not land; only the per-window map clears "replied".
export const SEEN_MODE_DEFAULT = "write";

const turnEnded = (t) => t.state === "done" || t.state === "idle";

// How many workers a terminal waits on while its turn is over (done or idle); 0 for any other state, a missing count or a bad one.
// A working, blocked or erroring terminal is not idle, so the label does not change what it shows.
export const waitingCount = (t) => (turnEnded(t) && Number.isInteger(t.waiting) && t.waiting > 0 ? t.waiting : 0);

// Whether a terminal counts as working for the session order, the working count and the working filter: it works, or it is idle
// only because it waits on its workers (counted as working, with no count of its own).
const isWorking = (t) => t.state === "working" || waitingCount(t) > 0;

// The agents of one session that wait on the workers below them, from who started whom alone, so a server that writes no
// `cao.waiting` label shows the wait too. A worker is finished when it is idle or done and every worker below it (whose caller chain reaches it)
// is finished. An agent waits when its own state is idle or done and at least one worker below it is not finished. Not finished, and so counted:
// a worker that works or is blocked, and an idle or done worker that waits itself, by the tree or by a `cao.waiting` label above 0 (the label
// is the terminal's own `waiting`). An agent whose own state is anything else (error, starting, unknown) is never counted, so it never makes its
// parent wait by itself, and a caller that is not among the terms gives no parent. `terms` are the terminals of ONE session with their callers
// resolved (withFallback). -> Map: id -> N, the number of counted workers below it, direct or transitive; only the agents that wait are in it.
export function treeWaiting(terms) {
  const list = Array.isArray(terms) ? terms : [];
  const { parent } = callerTree(list);
  const children = new Map();
  for (const t of list) if (parent.has(t.id)) children.set(parent.get(t.id), [...(children.get(parent.get(t.id)) ?? []), t]);
  const counted = new Map();
  const below = new Map();
  const visit = (t) => {
    if (below.has(t.id)) return;
    let n = 0;
    for (const c of children.get(t.id) ?? []) {
      visit(c);
      n += below.get(c.id) + (counted.get(c.id) ? 1 : 0);
    }
    below.set(t.id, n);
    const ended = t.state === "idle" || t.state === "done";
    counted.set(t.id, t.state === "working" || t.state === "blocked" || (ended && (n > 0 || (Number.isInteger(t.waiting) && t.waiting > 0))));
  };
  for (const t of list) visit(t);
  return new Map(list.filter((t) => (t.state === "idle" || t.state === "done") && below.get(t.id) > 0).map((t) => [t.id, below.get(t.id)]));
}

// The turn this window knows to be seen: the larger of the herdr.seen label and the per-window map's
// value for the terminal. undefined when neither is a number.
function seenMark(t, localSeen) {
  const known = [t.seen, localSeen?.[t.id]].filter((v) => Number.isFinite(v));
  return known.length > 0 ? Math.max(...known) : undefined;
}

// A talkable agent has replied when its turn ended (done or idle) and its numeric herdr.seen, or the
// turn this window remembers looking at, is lower than its turn. No numeric seen and no memory: never
// replied (see seenState). An agent that waits on its workers (waitingCount) has not replied yet.
export function isReplied(t, localSeen = {}) {
  const mark = turnEnded(t) && waitingCount(t) === 0 ? seenMark(t, localSeen) : undefined;
  return mark !== undefined && t.turn > mark;
}

// "replied", "seen", or "unknown" (no herdr.seen label and no per-window value) for a done or idle
// terminal; null while it works or while it waits on its workers. Session details show "seen: unknown"; the bar counts it separately.
export function seenState(t, localSeen = {}) {
  if (!turnEnded(t) || waitingCount(t) > 0) return null;
  const mark = seenMark(t, localSeen);
  if (mark === undefined) return "unknown";
  return t.turn > mark ? "replied" : "seen";
}

// The per-window map of "the turn I looked at"; returns the same object when nothing changes.
export function rememberSeen(localSeen, t) {
  const map = localSeen ?? {};
  if (!Number.isFinite(t.turn) || (Number.isFinite(map[t.id]) && map[t.id] >= t.turn)) return map;
  return { ...map, [t.id]: t.turn };
}

// What a focused agent's looking counts as seen: only a turn that has ended. An agent still working at
// turn 11 has not shown you turn 11's reply yet; remembering 11 now would hide that reply when it finishes.
export function rememberFinishedSeen(localSeen, t) {
  return turnEnded(t) ? rememberSeen(localSeen, t) : (localSeen ?? {});
}

// The hc arguments that mark a focused talkable agent's finished turn as seen, or null. `focused` is the
// focused terminal address. The command names <machine>/<terminal> whether or not Herdr already
// prefixed the address. An agent with no herdr.seen label is written too, so its "unknown" resolves;
// a worker never is (the caller sets `talkable` on talkable agents only).
export function seenWrite(t, focused, windowActive, machine, mode = SEEN_MODE_DEFAULT) {
  if (mode !== "write" || !windowActive || !t.talkable) return null;
  if (typeof focused !== "string" || t.address !== focused || !turnEnded(t) || !Number.isFinite(t.turn)) return null;
  if (Number.isFinite(t.seen) && t.seen >= t.turn) return null;
  const local = String(t.address).slice(String(t.address).lastIndexOf("/") + 1);
  return ["label", `${machine}/${local}`, `herdr.seen=${t.turn}`];
}

// state {mode, pending: null | {id, turn, at}}; ev: a pushed {id, seen}, {failed: true}, or null (a tick).
// A pushed value that reaches the written turn within 5 s verifies the write. A failed call, or 5 s
// without that value (a value that arrives later does not count), switches to the local map for the
// rest of the run.
export function seenVerify(state, ev, now) {
  const p = state.pending;
  if (!p) return state;
  if (ev?.failed) return { ...state, mode: "local", pending: null };
  if (now - p.at >= SEEN_VERIFY_MS) return { ...state, mode: "local", pending: null };
  const landed = finiteNumber(ev?.seen);
  if (ev && ev.id === p.id && landed !== undefined && landed >= p.turn) return { ...state, pending: null };
  return state;
}

const MIDDLE_DOT = "\u00b7";

// ---------- the kind of a blocked terminal ----------

// The kind of what a blocked terminal asks, when Herdr reports one: the words "needs you: <kind>" of its row's tooltip. There is
// no pill any more. A kind that is empty, blank or not text is no kind: null.
export function askingKind(t) {
  return typeof t?.kind === "string" && t.kind.trim() !== "" ? t.kind.trim() : null;
}

// ---------- the model every mod draws from ----------

// Everything a mod draws from, recomputed from push data on every render and tick (no HTTP):
// normalized terminals with the profile cache applied, sessions classified and sorted, the machine, and the
// predicate for "replied". ctx: {cache, drafts, pins, local, sha256hex}; every part may be missing.
// Sets `talkable: true` on each talkable terminal because seenWrite reads it. The terminals are built
// here from the raw records, so no argument is mutated.
export function buildModel(rawTerminals, spaceList, props, ctx) {
  const c = ctx ?? {};
  const raw = Array.isArray(rawTerminals) ? rawTerminals : [];
  const machine = caoMachine(spaceList, [], props, c.sha256hex, raw);
  const resolved = withFallback(caoTerms(raw), c.cache);
  // An agent waits when its `cao.waiting` label says so, else when the tree says so (treeWaiting): the label count, or the tree's, is its `waiting`.
  const byTree = new Map(sessionsFrom(resolved).flatMap((s) => [...treeWaiting(s.terms)]));
  const terms = resolved.map((t) => (t.waiting > 0 || !byTree.has(t.id) ? t : { ...t, waiting: byTree.get(t.id) }));
  const local = c.local ?? {};
  const replied = (t) => isReplied(t, local);
  const sessions = sortSessions(sessionsFrom(terms).map((s) => ({ ...s, ...classify(s.terms, props, c.drafts) })));
  for (const s of sessions) for (const t of s.talkable) t.talkable = true;
  return { machine, terms, sessions, pins: c.pins ?? [], local, replied, props };
}

// ---------- failure states: keep the last good view ----------

// view {data, at, auth, unreachable, error} + one callCao outcome -> the next view. Nothing but a
// successful reply replaces the data. An auth, unreachable or error outcome only sets its flag. An
// approval refusal, a request refused before it was sent, and anything unrecognised leave the view as
// it is: the caller shows that outcome's own message, and no write control is disabled by it.
// A reply of any kind from the server (auth, error, approval) shows it is reachable.
export function applyOutcome(view, outcome, now) {
  const base = view ?? {};
  switch (outcome?.kind) {
    case "ok": return { data: outcome.json, at: now, auth: false, unreachable: false, error: null };
    case "unreachable": return { ...base, unreachable: true };
    case "auth": return { ...base, auth: true, unreachable: false };
    case "approval_required": return { ...base, unreachable: false };
    case "error": return { ...base, unreachable: false, error: outcome.detail ?? outcome.message ?? `HTTP ${outcome.status ?? "?"}` };
    default: return base;
  }
}

// Only an auth failure disables write controls (a 401, or a 403 that is not an approval refusal).
// Given several views (flows, workflows, profiles), one in the auth state disables writes for all.
export const writesDisabled = (...views) => views.some((v) => v?.auth === true);

// "just now" under 5 s (and for a negative, missing or non-finite age), then seconds, minutes, hours.
export function ageWords(ms) {
  if (!Number.isFinite(ms) || ms < 5000) return "just now";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s} seconds`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} minute${m === 1 ? "" : "s"}`;
  const h = Math.floor(m / 60);
  return `${h} hour${h === 1 ? "" : "s"}`;
}

// The Sessions header's note. A machine that is not connected is said first: nothing else is knowable.
export function headerNote({ connected, unreachable } = {}) {
  if (connected === false) return "CAO machine not connected";
  return unreachable ? "CAO unreachable, retrying" : null;
}

// ---------- decisions the mods share (kept out of the mod code so they can be tested) ----------

// The node `id` of a layout tree when no node on the way to it is hidden; null when it is absent or hidden.
export function shownBlock(node, id) {
  if (!node || typeof node !== "object" || node.hidden === true) return null;
  if (node.id === id) return node;
  for (const child of listOrDefault(node.children, [])) {
    const hit = shownBlock(child, id);
    if (hit) return hit;
  }
  return null;
}

// Which model to draw and whether to dim it: an empty snapshot while the machine is away keeps the
// previous non-empty model, dimmed, with the snapshot's current pins, seen map and replied test (so a pin
// click shows at once); anything else draws the snapshot, dimmed only while the machine is away.
export function holdModel(previous, snapshot, connected) {
  const away = !connected;
  if (snapshot.sessions.length === 0 && away && previous?.sessions?.length > 0) {
    return { model: { ...previous, pins: snapshot.pins, local: snapshot.local, replied: snapshot.replied, connected }, dimmed: true };
  }
  return { model: { ...snapshot, connected }, dimmed: away };
}

// The answered entries of a stored cache value (another copy's published one), as {state, agent_profile, caller_id}; anything that is not
// such an entry, or a value that is not a plain object, gives none.
function answeredOf(stored) {
  if (stored === null || typeof stored !== "object" || Array.isArray(stored)) return {};
  return Object.fromEntries(Object.entries(stored)
    .filter(([, e]) => e !== null && typeof e === "object" && e.state === "ok" && typeof e.agent_profile === "string" && e.agent_profile !== "")
    .map(([id, e]) => [id, { state: "ok", agent_profile: e.agent_profile, caller_id: typeof e.caller_id === "string" ? e.caller_id : null }]));
}

// The same cache value as one string whatever the order of its keys, so a copy can tell whether what it would publish differs from what is
// stored. A value that is not a plain object is an empty cache.
export function cacheKey(cache) {
  const o = cache !== null && typeof cache === "object" && !Array.isArray(cache) ? cache : {};
  return JSON.stringify(Object.keys(o).sort().map((id) => [id, o[id] !== null && typeof o[id] === "object" ? Object.keys(o[id]).sort().map((k) => [k, o[id][k]]) : o[id]]));
}

// The fallback cache after new fetches, a snapshot and what is stored under the shared key, and what the other mods may read of it. Two copies
// of the side mod each hold a cache and only one fetches, so this copy never publishes over what another published: `stored` (the value read
// from the shared key just before) is merged in first. An answered stored entry fills an id this copy has no answer for (a pending or failed
// entry included) and this copy's own answer is kept; then the entries of the terminals in the snapshot only are kept (an empty snapshot
// prunes nothing), and what is published is the answered ones, as {state, agent_profile, caller_id}.
export function sharedCache(cache, liveIds, fetched = {}, stored = {}) {
  const merged = { ...(cache ?? {}), ...fetched };
  for (const [id, e] of Object.entries(answeredOf(stored))) if (merged[id]?.state !== "ok") merged[id] = e;
  const live = new Set(listOrDefault(liveIds, []));
  const kept = live.size === 0 ? merged : Object.fromEntries(Object.entries(merged).filter(([id]) => live.has(id)));
  const published = Object.fromEntries(Object.entries(kept).filter(([, c]) => c.state === "ok")
    .map(([id, c]) => [id, { state: "ok", agent_profile: c.agent_profile, caller_id: c.caller_id }]));
  return { cache: kept, published };
}

// Whether a row shows the replied mark: only a talkable agent that has replied.
export const rowShowsReplied = (talkable, replied) => talkable === true && replied === true;

// Whether the details panel repaints now: a whole period has passed since it last painted.
export const repaintDue = (paintedAt, now, periodMs) => now - paintedAt >= periodMs;

// The screen text read for a terminal, when it may be shown: its turn has ended and the read was made at
// this same turn; null otherwise (the previous turn's text is not shown while the agent works on the next).
export function readOfThisTurn(t, read) {
  return turnEnded(t) && read?.turn === t.turn ? read : null;
}

// ---------- status bar ----------

// S sessions, A asking terminals, R replied talkable agents, W working sessions (one with a waiting terminal counts); recorded draft
// author sessions are in none of them, and an `error` terminal is not asking.
// `unknown(t)` is true for a talkable agent whose seen state is unknown (seenState); those are
// counted apart, never as replied.
export function statusCounts(sessions, replied, unknown = () => false) {
  const live = (Array.isArray(sessions) ? sessions : []).filter((s) => !s.draft);
  const total = (count) => live.reduce((n, s) => n + count(s), 0);
  return {
    sessions: live.length,
    asking: total((s) => s.terms.filter((t) => t.state === "blocked").length),
    replied: total((s) => s.talkable.filter((t) => replied(t)).length),
    repliedUnknown: total((s) => s.talkable.filter((t) => unknown(t)).length),
    working: total((s) => (s.terms.some(isWorking) ? 1 : 0)),
  };
}

// "cao, S sessions, A asking, R replied (U unknown), W working" joined by middle dots: a zero count is hidden except
// sessions, and the replied part shows whenever R or U is not zero.
export function barText(counts, { unreachable = false } = {}) {
  const parts = ["cao", `${counts.sessions} session${counts.sessions === 1 ? "" : "s"}`];
  if (counts.asking) parts.push(`${counts.asking} asking`);
  if (counts.replied || counts.repliedUnknown) {
    parts.push(`${counts.replied} replied${counts.repliedUnknown ? ` (${counts.repliedUnknown} unknown)` : ""}`);
  }
  if (counts.working) parts.push(`${counts.working} working`);
  if (unreachable) parts.push("CAO unreachable");
  return parts.join(` ${MIDDLE_DOT} `);
}

// filter: "asking" | "replied" | "working" | null. A recorded draft author session is never a match and never
// hidden: it stays listed, in its place, whatever the filter. An unknown filter name lists everything.
export function filterSessions(sessions, filter, replied) {
  if (!filter) return sessions;
  const match = {
    asking: (s) => s.terms.some((t) => t.state === "blocked"),
    replied: (s) => s.talkable.some((t) => replied(t)),
    working: (s) => s.terms.some(isWorking),
  }[filter];
  return match ? sessions.filter((s) => s.draft || match(s)) : sessions;
}

// Clicking the active count clears the filter; clicking another switches to it.
export const nextFilter = (current, clicked) => (current === clicked ? null : clicked);

// ---------- hand-off toast ----------

export const HAND_OFF_FOCUS_MS = 5000;
// Holding a notification needs a copy of the toast mod that outlives the toast, and that has not been built
// or tried against a real Herdr, so the shipped toast mod passes every notification through (cfg.passThrough)
// unless configured otherwise. initialToast itself defaults to holding, which is what the reducer's
// own cases exercise; the toast mod starts from shippedToastState.
export const TOAST_PASS_THROUGH_DEFAULT = true;

const TOAST_HOLD_SECONDS = 120;
// Only these two kinds are held. A CAO notification of any other kind is shown at once.
const TOAST_KIND_RANK = { asking: 0, replied: 1 };

// The state the shipped toast mod starts from: pass-through unless the configuration says otherwise
// (an explicit false turns holding on; a missing or null value keeps the default).
// The toast mod calls this, not initialToast.
export const shippedToastState = (cfg) => initialToast({
  passThrough: cfg?.passThrough ?? TOAST_PASS_THROUGH_DEFAULT,
  holdMaxSeconds: cfg?.holdMaxSeconds ?? TOAST_HOLD_SECONDS,
});

export function initialToast({ holdMaxSeconds = TOAST_HOLD_SECONDS, passThrough = false } = {}) {
  const hold = Number.isFinite(holdMaxSeconds) && holdMaxSeconds >= 0 ? holdMaxSeconds : TOAST_HOLD_SECONDS;
  return { cfg: { holdMaxSeconds: hold, passThrough: passThrough === true }, queue: [], focus: null, shown: {} };
}

// Asking before replied, then the longest held.
function mostUrgentHeld(queue) {
  return queue.reduce((best, q) => {
    const a = TOAST_KIND_RANK[q.kind];
    const b = TOAST_KIND_RANK[best.kind];
    return a < b || (a === b && q.heldAt < best.heldAt) ? q : best;
  });
}

function releaseHeld(state, item) {
  const notification = { ...item.notification, label: HONESTY.handOff, actions: ["Open", "Later"] };
  return [
    { ...state, queue: state.queue.filter((q) => q !== item), shown: { ...state.shown, [item.key]: item } },
    [{ type: "show", key: item.key, notification }],
  ];
}

// Does a notification wait for a hand-off? Only a CAO notification of a held kind, from a session other
// than the focused one, that is not a recorded draft author session, while the window is busy.
const isHeldNotification = (info, busy) =>
  busy && info?.cao === true && !info.draft && info.session !== info.focusedSession
  && Object.hasOwn(TOAST_KIND_RANK, info.kind) && typeof info.terminalId === "string";

// The pure core of the toast mod. state: {cfg, queue, focus, shown}. event:
//   {type: "notify", notification, info: {cao, session, terminalId, kind, draft, focusedSession}, busy}
//   {type: "focus", terminal, agentState, busy}   the focused terminal and its state
//   {type: "tick"}  {type: "clear", terminalId, kind}  {type: "later", key}  {type: "open", key}
// -> [nextState, effects]; an effect is {type: "show", key?, notification} or {type: "focus", terminal}.
export function toastStep(state, event, now) {
  const hold = state.cfg.holdMaxSeconds * 1000;
  const releaseAtFor = (kind, from) => (kind === "asking" ? from + hold : null);
  switch (event?.type) {
    case "notify": {
      const { notification, info, busy } = event;
      if (state.cfg.passThrough || !isHeldNotification(info, busy)) return [state, [{ type: "show", notification }]];
      const key = `${info.terminalId}:${info.kind}`;
      const held = state.queue.find((q) => q.key === key);
      if (held) return [{ ...state, queue: state.queue.map((q) => (q === held ? { ...held, notification } : q)) }, []];
      const item = { key, kind: info.kind, session: info.session, terminalId: info.terminalId, notification, heldAt: now, releaseAt: releaseAtFor(info.kind, now), later: 0 };
      return [{ ...state, queue: [...state.queue, item] }, []];
    }
    case "focus": {
      const prev = state.focus;
      // A focus event with no terminal means nothing is focused: there is no previous focus after it.
      const focusedNow = typeof event.terminal === "string" && event.terminal !== "";
      const sameTerminal = focusedNow && prev !== null && prev !== undefined && prev.terminal === event.terminal;
      const focus = focusedNow ? { terminal: event.terminal, state: event.agentState, since: sameTerminal ? prev.since : now } : null;
      const next = { ...state, focus };
      const handOff = event.busy === true && sameTerminal && (prev.state === "done" || prev.state === "idle")
        && event.agentState === "working" && now - prev.since >= HAND_OFF_FOCUS_MS;
      return handOff && next.queue.length > 0 ? releaseHeld(next, mostUrgentHeld(next.queue)) : [next, []];
    }
    case "tick": {
      const due = state.queue.filter((q) => q.releaseAt !== null && q.releaseAt <= now);
      return due.length ? releaseHeld(state, due.reduce((a, b) => (b.releaseAt < a.releaseAt ? b : a))) : [state, []];
    }
    case "clear": {
      const queue = state.queue.filter((q) => !(q.terminalId === event.terminalId && (event.kind === undefined || q.kind === event.kind)));
      return [{ ...state, queue }, []];
    }
    case "later": {
      const item = state.shown[event.key];
      if (!item) return [state, []];
      const shown = { ...state.shown };
      delete shown[event.key];
      if (item.later + 1 >= 2) return [{ ...state, shown }, []];   // a second Later drops it
      const again = { ...item, later: item.later + 1, heldAt: now, releaseAt: releaseAtFor(item.kind, now) };
      return [{ ...state, shown, queue: [...state.queue.filter((q) => q.key !== item.key), again] }, []];
    }
    case "open": {
      const item = state.shown[event.key];
      if (!item) return [state, []];
      const shown = { ...state.shown };
      delete shown[event.key];
      const terminal = item.notification?.action?.args?.terminal;
      return [{ ...state, shown }, terminal ? [{ type: "focus", terminal }] : []];
    }
    default:
      return [state, []];
  }
}

// ---------- the keys of a confirm dialog ----------

// Keys inside a confirm dialog. state {focus: "cancel" | "confirm" | "input"}; spec {confirmEnabled}. Esc
// cancels; Enter acts on what has the focus (Cancel cancels; Confirm acts only when the spec says confirmEnabled === true); Tab and the
// arrows move the focus between the two buttons. Returns the next focus and the action to run, or null.
export function dialogKey(state, spec, key) {
  const k = String(key ?? "").toLowerCase().replace(/^arrow/, "");
  const focus = state?.focus === "confirm" || state?.focus === "input" ? state.focus : "cancel";
  if (k === "escape" || k === "esc") return { focus, action: "cancel" };
  if (k === "enter" || k === "return") {
    if (focus === "cancel") return { focus, action: "cancel" };
    return { focus, action: spec?.confirmEnabled === true ? "confirm" : null };
  }
  if (k === "tab" || k === "left" || k === "right" || k === "up" || k === "down") return { focus: focus === "cancel" ? "confirm" : "cancel", action: null };
  return { focus, action: null };
}

// ---------- what counts as typed in a form ----------

// Whether text typed in a profile field is not the profile the form holds (typed, but not chosen): it counts
// as something typed when the form is left.
export const unchosenProfileText = (text, chosen) => String(text ?? "") !== String(chosen ?? "");
