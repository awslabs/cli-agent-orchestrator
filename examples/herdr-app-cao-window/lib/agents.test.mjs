// lib/agents.js: the view models of the left sidebar's two blocks, the Sessions line and the Agents
// tree (specs cao-window-sessions and cao-window-honesty-labels; design D25 and D26; tasks 18.1 to 18.4).
//
// The module is imported as a namespace so a function that is not written yet fails its own case with
// "x is not a function" instead of stopping the whole file at the import line.
import { assert, finish, test } from "../test/harness.mjs";
import { HONESTY } from "./honesty.js";
import { readFileSync } from "node:fs";
import { buildModel, classify, filterSessions, isReplied, knownGapNote, observe, initialObs, quietHint, sortSessions, statusShape } from "./logic.js";
import * as A from "./agents.js";

const DOT = "\u00b7"; // the middle dot of the product copy, kept as an escape so this file stays ASCII
const MIN = 60_000;

// A normalized terminal, as termOf produces it.
const T = (id, session, over = {}) => ({
  address: `cao/${id}`, id, session, window: id, profile: "conductor", caller: undefined, state: "idle",
  kind: undefined, msg: undefined, statusMs: undefined, turn: 0, seen: undefined, createdMs: 1, ...over,
});
const worker = (id, session, caller = "c1", over = {}) => T(id, session, { profile: "developer", caller, createdMs: 5, ...over });
const rootWorker = (id, session, over = {}) => T(id, session, { profile: "developer", caller: undefined, createdMs: 5, ...over });
const never = () => false;
const nothing = (v) => v === null || v === undefined; // "returns nothing" is null or undefined; the spec does not say which
const sess = (name, terms, props = {}, drafts = []) => ({ name, terms, ...classify(terms, props, drafts) });

// Deep freeze, so a function that mutates its argument throws in strict mode (ES modules are strict).
const freeze = (v) => {
  if (v && typeof v === "object" && !Object.isFrozen(v)) { Object.freeze(v); for (const k of Object.keys(v)) freeze(v[k]); }
  return v;
};

// ---- 18.1: sessionLine ----

test("One line per session: name, an extra mark and the state of the first talkable agent, and no agent rows, no pill", () => {
  const a = freeze(sess("cao-a", [T("c1", "cao-a", { state: "working" }), worker("w1", "cao-a", "c1", { state: "blocked" })]));
  const b = freeze(sess("cao-b", [T("c2", "cao-b", { state: "idle" })]));
  const la = A.sessionLine(a, { replied: never, quiet: null });
  const lb = A.sessionLine(b, { replied: never, quiet: null });
  assert.equal(la.name, "cao-a");
  assert.equal(la.extra, "bang", "a blocked worker: the needs-you square beside the conductor's own mark");
  assert.equal(la.tip, "cao-a: working, needs you");
  assert.equal(la.shape, "spinner");
  assert.equal(lb.name, "cao-b");
  assert.equal(lb.extra, null);
  assert.equal(lb.shape, "ring");
  for (const line of [la, lb]) {
    assert.deepEqual(Object.keys(line).sort(), ["badge", "draft", "extra", "hint", "name", "shape", "tip"], "no pill field");
    assert.ok(Object.values(line).every((v) => !Array.isArray(v)), "no list of agents or workers");
  }
});

test("Selecting a session: the line carries the session's name and the state of its first talkable agent, not of a worker listed before it", () => {
  const s = freeze(sess("cao-a", [worker("w0", "cao-a", "c1", { state: "working", createdMs: 1 }), T("c1", "cao-a", { state: "idle", createdMs: 2 })]));
  assert.deepEqual(s.talkable.map((t) => t.id), ["c1"]);
  const line = A.sessionLine(s, { replied: never, quiet: null });
  assert.equal(line.name, "cao-a");
  assert.deepEqual([line.shape, line.tip], ["ring", "cao-a: idle"]);
  const two = freeze(sess("cao-b", [T("c1", "cao-b", { state: "done" }), T("c2", "cao-b", { state: "working" })]));
  assert.deepEqual([A.sessionLine(two, { replied: never }).shape, A.sessionLine(two, { replied: never }).tip], ["dot", "cao-b: done"]);
});

test("Asking session: the asking session gets the needs-you mark, and sorts by its age like any other, not above the older working one", () => {
  const sa = freeze(sess("A", [T("a1", "A", { state: "working", createdMs: 1 })]));
  const sb = freeze(sess("B", [T("b1", "B", { state: "idle", createdMs: 2 }), worker("b2", "B", "b1", { state: "blocked" })]));
  const sorted = sortSessions([sb, sa]);
  assert.deepEqual(sorted.map((s) => s.name), ["A", "B"]);
  const lines = sorted.map((s) => A.sessionLine(s, { replied: never, quiet: null }));
  assert.deepEqual(lines.map((l) => l.extra), [null, "bang"], "the mark carries the attention, the order does not");
});

test("Needs you in the tooltip with the kind: 'needs you: <kind>' with the blocked record's kind, and plain 'needs you' without one; no mark carries a word", () => {
  const withKind = freeze(sess("s", [T("c1", "s"), worker("w1", "s", "c1", { state: "blocked", kind: "permission" })]));
  assert.equal(A.sessionLine(withKind, { replied: never }).tip, "s: idle, needs you: permission");
  assert.equal(A.sessionLine(withKind, { replied: never }).extra, "bang");
  const noKind = freeze(sess("s", [T("c1", "s"), worker("w1", "s", "c1", { state: "blocked" })]));
  assert.equal(A.sessionLine(noKind, { replied: never }).tip, "s: idle, needs you");
  const blank = freeze(sess("s", [T("c1", "s"), worker("w1", "s", "c1", { state: "blocked", kind: "  " })]));
  assert.equal(A.sessionLine(blank, { replied: never }).tip, "s: idle, needs you");
  // only the blocked terminal's kind counts, not a kind another terminal reports
  const other = freeze(sess("s", [T("c1", "s", { kind: "permission" }), worker("w1", "s", "c1", { state: "blocked" })]));
  assert.equal(A.sessionLine(other, { replied: never }).tip, "s: idle, needs you");
  // the first talkable agent itself blocked: its own mark is the square, so there is no second one, and the kind is in its words
  const own = freeze(sess("s", [T("c1", "s", { state: "blocked", kind: "question" })]));
  assert.deepEqual([A.sessionLine(own, { replied: never }).shape, A.sessionLine(own, { replied: never }).extra, A.sessionLine(own, { replied: never }).tip], ["bang", null, "s: needs you: question"]);
});

test("Draft author session: a recorded draft shows the draft tag and no extra mark; an operator's cao-draft-x is an ordinary session", () => {
  const terms = [T("d1", "cao-draft-flow-0a1b2c3d", { state: "blocked" })];
  const draft = freeze(sess("cao-draft-flow-0a1b2c3d", terms, {}, ["d1"]));
  const line = A.sessionLine(draft, { replied: () => true, quiet: null });
  assert.equal(line.draft, true);
  assert.equal(line.extra, null, "the tag takes the place of the mark");
  const done = freeze(sess("cao-draft-flow-0a1b2c3d", [T("d1", "cao-draft-flow-0a1b2c3d", { state: "done", turn: 2 })], {}, ["d1"]));
  assert.equal(A.sessionLine(done, { replied: () => true }).extra, null, "a recorded draft is never replied");
  const own = freeze(sess("cao-draft-x", [T("o1", "cao-draft-x", { state: "done", turn: 2, seen: 1 })]));
  const ol = A.sessionLine(own, { replied: () => true });
  assert.equal(ol.draft, false);
  assert.deepEqual([ol.shape, ol.extra, ol.tip], ["dot-hi", null, "cao-draft-x: replied"]);
});

test("Quiet worker: the line carries 'stuck?' labelled 'heuristic' when the window saw the worker go quiet", () => {
  const s = freeze(sess("cao-a", [T("c1", "cao-a"), worker("w1", "cao-a", "c1", { state: "working" })]));
  const obs = observe(initialObs(0), s.terms, 0);
  const hint = quietHint(s, obs, 10, 11 * MIN);
  assert.ok(hint, "fixture: the worker is quiet after 11 minutes");
  const line = A.sessionLine(s, { replied: never, quiet: hint });
  assert.equal(line.hint.text, "stuck?");
  assert.equal(line.hint.label, "heuristic");
  assert.equal(line.hint.label, HONESTY.heuristic);
  assert.ok(nothing(A.sessionLine(s, { replied: never, quiet: null }).hint));
  assert.ok(nothing(A.sessionLine(s, { replied: never }).hint), "no quiet in the context is no hint");
  assert.ok(Object.values(line).every((v) => !Array.isArray(v)), "still no agent rows");
});

test("Replied mark: a talkable agent that replied draws the done dot in the highlight colour, a second mark appears only if the first agent's own mark is not that dot, and a blocked terminal wins the extra slot", () => {
  const s = freeze(sess("cao-a", [T("c1", "cao-a", { state: "done", turn: 3, seen: 2 })]));
  const line = A.sessionLine(s, { replied: (t) => t.id === "c1" });
  assert.deepEqual([line.shape, line.extra, line.tip], ["dot-hi", null, "cao-a: replied"], "the first agent's own mark is the highlighted dot: no second mark");
  assert.deepEqual([A.sessionLine(s, { replied: never }).shape, A.sessionLine(s, { replied: never }).extra, A.sessionLine(s, { replied: never }).tip], ["dot", null, "cao-a: done"]);
  const both = freeze(sess("cao-a", [T("c1", "cao-a", { state: "done", turn: 3, seen: 2 }), worker("w1", "cao-a", "c1", { state: "blocked" })]));
  const asked = A.sessionLine(both, { replied: () => true });
  assert.deepEqual([asked.shape, asked.extra, asked.tip], ["dot-hi", "bang", "cao-a: replied, needs you"], "the square is the extra mark; the reply stays in the agent's own");
});

test("Replied mark comes from a talkable agent: a worker that replied gives the session no mark, nor changes its glyph", () => {
  // spec "Session line": replied when a talkable agent has replied
  const s = freeze(sess("cao-a", [T("c1", "cao-a", { state: "idle", turn: 3, seen: 3 }), worker("w1", "cao-a", "c1", { state: "done", turn: 3, seen: 1 })]));
  assert.deepEqual(s.talkable.map((t) => t.id), ["c1"]);
  const line = A.sessionLine(s, { replied: (t) => t.id === "w1" });
  assert.equal(line.extra, null, "only the worker replied");
  assert.deepEqual([line.shape, line.tip], ["ring", "cao-a: idle"]);
  // a second talkable agent that replied does give it, though the first is not the one
  const two = freeze(sess("cao-b", [T("c1", "cao-b", { state: "idle", createdMs: 1 }), T("c2", "cao-b", { state: "done", turn: 2, seen: 1, createdMs: 2 })]));
  const second = A.sessionLine(two, { replied: (t) => t.id === "c2" });
  assert.deepEqual([second.shape, second.extra, second.tip], ["ring", "dot-hi", "cao-b: idle, replied"], "the glyph is the first talkable agent's, the highlighted dot beside it says another agent replied, and the tooltip says both");
});

// ---- 18.2: agentRows ----

const ids = (view) => view.rows.map((r) => r.id);
const depths = (view) => view.rows.map((r) => r.depth);
// n workers of `caller`, created after everything else (ids f00, f01, ...): enough of them make a large session (more than 12 agents).
const fill = (n, caller = "c1") => Array.from({ length: n }, (_, i) => worker(`f${String(i).padStart(2, "0")}`, "s", caller, { createdMs: 100 + i }));

test("Indentation shows who started whom: c1 at 0, w1 at 1, w2 at 2, and no row prints a terminal id as 'started by'", () => {
  const s = freeze(sess("s", [T("c1", "s", { createdMs: 1 }), worker("w1", "s", "c1", { createdMs: 2 }), worker("w2", "s", "w1", { createdMs: 3 })]));
  const view = A.agentRows(s, []);
  assert.deepEqual(ids(view), ["c1", "w1", "w2"]);
  assert.deepEqual(depths(view), [0, 1, 2]);
  const text = JSON.stringify(view.rows.map((r) => [r.tip, r.parentNote, r.name]));
  assert.doesNotMatch(text, /started by/i);
  assert.doesNotMatch(text, /\b(c1|w1|w2)\b/, "no terminal id is printed");
});

test("No fold: a session of 14 agents shows every one, each under its real parent, and a pin changes no row (design D42 C)", () => {
  const s = freeze(sess("s", [T("c1", "s"), worker("w1", "s", "c1", { createdMs: 2 }), worker("w2", "s", "w1", { createdMs: 3 }), ...fill(11)]));
  const all = ["c1", "w1", "w2", ...fill(11).map((w) => w.id)];
  const view = A.agentRows(s, ["w2"]);
  assert.deepEqual(ids(view), all);
  assert.deepEqual(depths(view), [0, 1, 2, ...Array(11).fill(1)]);
  assert.deepEqual(Object.keys(view), ["rows"], "the view is the rows alone: no fold line");
  for (const pins of [[], ["w1", "w2"], ["f03"]]) assert.deepEqual(ids(A.agentRows(s, pins)), all, `pins ${JSON.stringify(pins)} hide nothing`);
  const deep = freeze(sess("s", [T("c1", "s"), worker("wa", "s", "c1", { createdMs: 2 }), worker("wb", "s", "wa", { createdMs: 3 }), worker("wc", "s", "wb", { createdMs: 4 }), ...fill(11)]));
  assert.deepEqual(depths(A.agentRows(deep, [])).slice(0, 4), [0, 1, 2, 3], "three levels of workers keep their real steps");
  const blocked = freeze(sess("s", [T("c1", "s"), worker("w1", "s", "c1", { createdMs: 2 }), worker("w2", "s", "w1", { createdMs: 3, state: "blocked" }), ...fill(11)]));
  assert.deepEqual([ids(A.agentRows(blocked, [])).length, A.agentRows(blocked, []).rows[2].shape], [14, "bang"]);
});

test("Parent not listed: a worker whose caller is not among the session's terminals is indented one step with the words", () => {
  const s = freeze(sess("s", [T("c1", "s"), worker("w9", "s", "ghost", { createdMs: 2 }), worker("w10", "s", "w9", { createdMs: 3 }), rootWorker("r1", "s", { createdMs: 4 })]));
  const view = A.agentRows(s, ["w9", "w10", "r1"]);
  const by = Object.fromEntries(view.rows.map((r) => [r.id, r]));
  assert.deepEqual([by.w9.depth, by.w9.parentNote], [1, "parent not listed"]);
  assert.deepEqual([by.w10.depth, by.w10.parentNote], [2, null], "its own caller is listed, so only the step shows");
  assert.deepEqual([by.r1.depth, by.r1.parentNote], [0, null], "a root worker has no parent to miss");
  assert.deepEqual([by.c1.depth, by.c1.parentNote], [0, null]);
});

test("Talkable child with unlisted parent: an allowlisted terminal whose caller is in another session is indented one step with the words", () => {
  const props = { talkableProfiles: ["thread-coordinator"] };
  const s = freeze(sess("s", [T("c1", "s", { createdMs: 1 }), T("tc", "s", { profile: "thread-coordinator", caller: "t7-of-another-session", createdMs: 2 })], props));
  assert.deepEqual(s.talkable.map((t) => t.id), ["c1", "tc"]);
  const view = A.agentRows(s, []);
  assert.deepEqual(ids(view), ["c1", "tc"]);
  const tc = view.rows[1];
  assert.deepEqual([tc.depth, tc.parentNote, tc.worker], [1, "parent not listed", false]);
});

test("Cycle is cut at the oldest: w1 created first has no parent and w2 sits one step under it", () => {
  const s = freeze(sess("s", [T("c1", "s", { createdMs: 1 }), worker("w1", "s", "w2", { createdMs: 2 }), worker("w2", "s", "w1", { createdMs: 3 })]));
  const view = A.agentRows(s, []);
  assert.deepEqual(ids(view), ["c1", "w1", "w2"]);
  assert.deepEqual(depths(view), [0, 0, 1]);
  assert.ok(view.rows.every((r) => r.parentNote === null), "the cut is not 'parent not listed'");
  // the oldest is by creation time, not by id
  const rev = freeze(sess("s", [T("c1", "s"), worker("wa", "s", "wb", { createdMs: 9 }), worker("wb", "s", "wa", { createdMs: 4 })]));
  const rv = A.agentRows(rev, []);
  assert.deepEqual([ids(rv), depths(rv)], [["c1", "wb", "wa"], [0, 0, 1]]);
  // the same creation time: the lowest id is the root
  const tie = freeze(sess("s", [T("c1", "s"), worker("wb", "s", "wa", { createdMs: 4 }), worker("wa", "s", "wb", { createdMs: 4 })]));
  const tv = A.agentRows(tie, []);
  assert.deepEqual([ids(tv), depths(tv)], [["c1", "wa", "wb"], [0, 0, 1]]);
  // a longer cycle with a tail hanging off it
  const ring = freeze(sess("s", [T("c1", "s"), worker("a", "s", "c", { createdMs: 6 }), worker("b", "s", "a", { createdMs: 2 }), worker("c", "s", "b", { createdMs: 7 }), worker("d", "s", "c", { createdMs: 8 })]));
  const rg = A.agentRows(ring, []);
  assert.deepEqual([ids(rg), depths(rg)], [["c1", "b", "c", "a", "d"], [0, 0, 1, 2, 2]], "b is oldest and has no parent: c names b, a names c, d names c");
});

test("Conductor with workers: every agent is shown, however many, with no line to fold or unfold them (design D42 C)", () => {
  const s = freeze(sess("s", [T("c1", "s"), worker("w1", "s"), worker("w2", "s"), worker("w3", "s")]));
  const small = A.agentRows(s, []);
  assert.deepEqual(ids(small), ["c1", "w1", "w2", "w3"]);
  assert.deepEqual(Object.keys(small), ["rows"]);
  for (const n of [11, 12, 40]) {
    const big = A.agentRows(freeze(sess("s", [T("c1", "s"), ...fill(n)])), []);
    assert.deepEqual(ids(big), ["c1", ...fill(n).map((w) => w.id)], `${n + 1} agents: all shown`);
    assert.deepEqual(Object.keys(big), ["rows"], `${n + 1} agents: no fold line`);
  }
  assert.deepEqual(ids(A.agentRows(freeze(sess("s", [T("c1", "s", { createdMs: 1 }), T("c2", "s", { createdMs: 2 }), ...fill(11)])), [])).length, 13, "13 agents with two talkable: all shown");
  assert.deepEqual(ids(A.agentRows(freeze(sess("s", [T("c1", "s")])), [])), ["c1"], "no workers, one row");
});

test("A blocked worker among many is its own row with the needs-you square and its words in the tooltip, until it leaves blocked", () => {
  const mk = (state, kind) => freeze(sess("s", [T("c1", "s"), worker("w1", "s", "c1", { createdMs: 2, state, kind }), ...fill(11)]));
  const asking = A.agentRows(mk("blocked"), []);
  assert.equal(asking.rows.length, 13, "every agent is a row");
  assert.equal(asking.rows[1].id, "w1");
  assert.equal(asking.rows[1].shape, "bang");
  assert.match(asking.rows[1].tip, /^developer: needs you, turn/);
  assert.ok(!("pill" in asking.rows[1]), "no pill field");
  assert.match(A.agentRows(mk("blocked", "permission"), []).rows[1].tip, /^developer: needs you: permission, turn/);
  assert.equal(asking.rows[0].shape, "ring");
  const after = A.agentRows(mk("working"), []);
  assert.equal(after.rows.length, 13, "it stays a row when it leaves blocked");
  assert.notEqual(after.rows[1].shape, "bang");
});

test("No talkable agent left: the older of two workers is shown, labelled 'best guess'", () => {
  const s = freeze(sess("s", [worker("w2", "s", "gone", { createdMs: 3, profile: "reviewer" }), worker("w1", "s", "gone", { createdMs: 2 })]));
  assert.equal(s.bestGuess, true);
  const view = A.agentRows(s, []);
  assert.deepEqual(ids(view), ["w1", "w2"], "the other worker is shown too: the whole tree is");
  assert.equal(view.rows[0].label, "best guess");
  assert.equal(view.rows[0].label, HONESTY.bestGuess);
  assert.equal(view.rows[0].worker, false);
  assert.deepEqual([view.rows[0].depth, view.rows[0].parentNote], [1, "parent not listed"], "its caller was the deleted root");
  assert.deepEqual([view.rows[1].worker, view.rows[1].label], [true, null]);
  assert.ok(!("fold" in view));
  const rootless = /* two root memory managers: hidden, so nobody is talkable */ freeze(sess("s", [rootWorker("w1", "s", { createdMs: 2, profile: "memory_manager" }), rootWorker("w2", "s", { createdMs: 3, profile: "memory_manager" })]));
  assert.deepEqual([ids(A.agentRows(rootless, [])), A.agentRows(rootless, []).rows[0].label], [["w1", "w2"], "best guess"]);
  const normal = A.agentRows(freeze(sess("s", [T("c1", "s")])), []);
  assert.ok(nothing(normal.rows[0].label), "only a best guess carries the label");
});

test("No agent is listed for no session or a session that is gone: agentRows takes what agentsHeader takes and returns no rows", () => {
  // spec "Selected session gone" and "Nothing selected": the header says so and no agent is listed
  for (const none of [null, undefined, { kind: "session", name: "cao-a" }, { name: "cao-a" }, {}, [], "cao-a", 5]) {
    const view = A.agentRows(freeze(none), []);
    assert.deepEqual(view, { rows: [] }, JSON.stringify(none));
    assert.deepEqual(A.agentRows(freeze(none), ["w1"], { now: 1, replied: () => true }), { rows: [] }, `${JSON.stringify(none)} with pins and a clock`);
  }
});

test("Best guess labels the one terminal: only the best guess carries the label", () => {
  const s = freeze(sess("s", [worker("w2", "s", "gone", { createdMs: 3, profile: "reviewer" }), worker("w1", "s", "gone", { createdMs: 2 })]));
  assert.equal(s.bestGuess, true);
  const view = A.agentRows(s, []);
  assert.deepEqual(ids(view), ["w1", "w2"]);
  assert.deepEqual(view.rows.map((r) => [r.id, r.worker, r.label]), [["w1", false, HONESTY.bestGuess], ["w2", true, null]]);
});

test("A worker row never shows replied: only a talkable agent's reply shows, whatever the predicate says of a worker", () => {
  const s = freeze(sess("s", [T("c1", "s", { state: "done", turn: 3, seen: 1 }), worker("w1", "s", "c1", { state: "done", turn: 3, seen: 1 })]));
  const [c, w] = A.agentRows(s, [], { now: 1, replied: () => true }).rows;
  assert.deepEqual([c.id, c.shape], ["c1", "dot-hi"]);
  assert.deepEqual([w.id, w.shape], ["w1", "dot"]);
  assert.match(c.tip, /^conductor: replied/);
  assert.match(w.tip, /^developer: done/);
});

test("Self-parent: a terminal that names itself as caller has no parent and does not loop", () => {
  const s = freeze(sess("s", [T("c1", "s"), worker("w1", "s", "w1", { createdMs: 2 })]));
  const view = A.agentRows(s, ["w1"]);
  assert.deepEqual([ids(view), depths(view)], [["c1", "w1"], [0, 0]]);
  assert.equal(view.rows[1].parentNote, null);
});

test("Sibling order: by creation time, then by id, children right after their parent", () => {
  const s = freeze(sess("s", [
    T("c1", "s", { createdMs: 4 }),
    worker("wa", "s", "c1", { createdMs: 7 }), worker("wz", "s", "c1", { createdMs: 5 }), worker("wb", "s", "c1", { createdMs: 5 }),
    worker("wx", "s", "wz", { createdMs: 1 }),
  ]));
  const view = A.agentRows(s, []);
  assert.deepEqual(ids(view), ["c1", "wb", "wz", "wx", "wa"]);
  assert.deepEqual(depths(view), [0, 1, 1, 2, 1]);
  // two talkable roots: the earlier created first, whatever the order of the terminals
  const two = freeze(sess("s", [T("late", "s", { createdMs: 9 }), T("early", "s", { createdMs: 3 })]));
  assert.deepEqual(ids(A.agentRows(two, [])), ["early", "late"]);
});

test("Tooltip of a row: the name, then state and time in state, turn, talkable or worker, seen state of a talkable agent, and Herdr's message while working", () => {
  const now = 100 * MIN;
  const rows = (terms, ctx, pins = []) => A.agentRows(freeze(sess("s", terms)), pins, ctx).rows;
  const [conductor, w] = rows([
    T("c1", "s", { state: "done", turn: 3, statusMs: now - 5 * MIN }),
    worker("w1", "s", "c1", { state: "working", turn: 1, statusMs: now - 2 * MIN, msg: "Writing a file" }),
  ], { now, replied: never });
  assert.equal(conductor.tip, "conductor: done for 5 minutes, turn 3, talkable, seen: unknown");
  assert.equal(w.tip, "developer: working for 2 minutes, turn 1, worker, Writing a file");
  const [replied] = rows([T("c1", "s", { state: "done", turn: 3, seen: 2, statusMs: now - 5 * MIN })], { now, replied: () => true });
  assert.equal(replied.tip, "conductor: replied for 5 minutes, turn 3, talkable, seen: replied");
  assert.equal(replied.shape, "dot-hi");
  const [seen] = rows([T("c1", "s", { state: "idle", turn: 3, seen: 3, statusMs: now - 5 * MIN })], { now, replied: never });
  assert.equal(seen.tip, "conductor: idle for 5 minutes, turn 3, talkable, seen: seen");
  const [, idleWorker] = rows([T("c1", "s"), worker("w1", "s", "c1", { state: "idle", turn: 2, statusMs: now - 2 * MIN, msg: "stale words" })], { now, replied: never });
  assert.equal(idleWorker.tip, "developer: idle for 2 minutes, turn 2, worker", "the message shows only while working");
  const [working] = rows([T("c1", "s", { state: "working", turn: 0, statusMs: now - 1000 })], { now, replied: never });
  assert.equal(working.tip, "conductor: working, just now, turn 0, talkable", "a working agent has no seen state");
  const [bare] = rows([T("c1", "s", { state: "working" })], {});
  assert.equal(bare.tip, "conductor: working, turn 0, talkable", "no clock given, no time in state");
  assert.equal(A.agentRows(freeze(sess("s", [T("c1", "s", { state: "working", msg: "hi" })])), []).rows[0].tip, "conductor: working, turn 0, talkable, hi", "the three-argument call still works");
  const [asking] = rows([T("c1", "s", { state: "blocked", turn: 4, statusMs: now - 3 * MIN })], { now, replied: never });
  assert.match(asking.tip, /^conductor: needs you for 3 minutes, turn 4/, "Herdr's own words for a blocked agent");
});

test("Seen state unknown: a done talkable agent with no herdr.seen and no memory reads 'seen: unknown'; the window's own memory resolves it", () => {
  const s = freeze(sess("s", [T("c1", "s", { state: "done", turn: 3 })]));
  assert.match(A.agentRows(s, [], { replied: never }).rows[0].tip, /seen: unknown$/);
  assert.match(A.agentRows(s, [], { replied: never, local: { c1: 3 } }).rows[0].tip, /seen: seen$/);
});

test("Row fields: name, glyph, pin flag and tooltip, the terminal for the click, and no state word, profile or muted line", () => {
  const s = freeze(sess("s", [T("c1", "s", { state: "working" }), worker("w1", "s", "c1", { state: "idle" }), worker("w2", "s", "c1", { profile: undefined })]));
  const view = A.agentRows(s, ["w1"]);
  const [c, w1, w2] = view.rows;
  assert.deepEqual([c.name, c.shape, c.worker, c.pinned], ["conductor", "spinner", false, false]);
  assert.deepEqual([w1.name, w1.shape, w1.worker, w1.pinned], ["developer", "ring", true, true]);
  assert.equal(w2.name, "w2", "no tab name and no profile: a short id stands in, as the last resort");
  assert.equal(c.term, s.terms[0]);
  for (const r of view.rows) {
    assert.deepEqual(Object.keys(r).sort(), ["age", "depth", "icon", "id", "label", "main", "name", "parentNote", "pinTip", "pinned", "shape", "slot", "term", "tip", "worker"], r.id);
    assert.ok(!("state" in r) && !("muted" in r) && !("profile" in r), "the row has no state word, no second line and no profile field");
  }
});

// ---- the glyph of a state (owner review, changes A and H): Herdr's own marks, one per Herdr status, no word ----
//
// What Herdr's shipped Agents list draws (mods/agents/main.js `status()`, lines 212-219 of the 0.1.17 mod): working a
// spinner (spinner.svg, 12 px, t.working), idle a ring (7 px, 1.5 border, t.idle), done a filled dot (7 px, t.done),
// blocked a 14 px square tinted t.blocked with a "!", and anything else a transparent 7 px slot. It has no mark of
// its own for the states only this window knows: error counts as blocked there (line 60), and a state it does not know draws nothing.

const STATE_FIXTURES = [
  // [display state, terminal over, replied?, mark, tooltip word]
  ["working", { state: "working" }, false, "spinner", "working"],
  ["idle", { state: "idle" }, false, "ring", "idle"],
  ["done", { state: "done", turn: 2, seen: 2 }, false, "dot", "done"],
  ["replied", { state: "done", turn: 2, seen: 1 }, true, "dot-hi", "replied"],
  ["blocked", { state: "blocked" }, false, "bang", "needs you"],
  ["error", { state: "error" }, false, "bang", "error"],
  ["starting", { state: "unknown" }, false, "none", "starting"],
];

test("Glyph per state: each state gets Herdr's mark in the session line and the agent row alike, and its words only in the tooltip", () => {
  for (const [name, over, replied, shape, word] of STATE_FIXTURES) {
    const s = freeze(sess("cao-a", [T("c1", "cao-a", over)]));
    const line = A.sessionLine(s, { replied: () => replied });
    assert.equal(line.shape, shape, `session line, ${name}`);
    assert.equal(line.tip, `cao-a: ${word}`, `session tooltip, ${name}`);
    const row = A.agentRows(s, [], { replied: () => replied }).rows[0];
    assert.equal(row.shape, shape, `agent row, ${name}`);
    assert.match(row.tip, new RegExp(`^conductor: ${word}\\b`), `row tooltip, ${name}`);
  }
  assert.equal(statusShape("quiet"), "spinner", "quiet is a working agent that went silent; the 'stuck?' hint says so");
});

test("Herdr's four marks are distinct, and the states only this window knows borrow the mark of the Herdr status they refine", () => {
  const mark = Object.fromEntries(["working", "idle", "done", "blocked"].map((s) => [s, statusShape(s)]));
  assert.deepEqual(mark, { working: "spinner", idle: "ring", done: "dot", blocked: "bang" });
  assert.equal(new Set(Object.values(mark)).size, 4, "working, idle, done and needs-you look different");
  assert.equal(statusShape("replied"), "dot-hi", "a reply is a finished turn: the done dot, in a highlight colour and with no word");
  assert.notEqual(statusShape("replied"), mark.done, "told apart from the plain done dot by its colour");
  assert.equal(statusShape("error"), mark.blocked, "Herdr counts an error as blocked (mods/agents/main.js:60)");
  assert.equal(statusShape("quiet"), mark.working, "a quiet worker still works, as far as Herdr's status knows");
  assert.equal(statusShape("starting"), "none", "Herdr draws a transparent slot for a state it does not know");
  for (const junk of ["nonsense", "", undefined, null, 7]) assert.equal(statusShape(junk), "none", String(junk));
});

test("No state word on the row: the session line and the agent row draw marks and a name, no pill, and the words are in the tooltip only", () => {
  const WORDS = /\b(done|idle|working|starting|blocked|error|quiet|replied)\b/;
  for (const [name, over, replied] of STATE_FIXTURES) {
    const s = freeze(sess("cao-a", [T("c1", "cao-a", over), worker("w1", "cao-a", "c1", { state: "working" })]));
    const line = A.sessionLine(s, { replied: (t) => replied && t.id === "c1" });
    const { tip: lineTip, ...lineDrawn } = line;
    assert.doesNotMatch(JSON.stringify(lineDrawn), WORDS, `session line, ${name}`);
    assert.doesNotMatch(JSON.stringify(lineDrawn), /Asking|Replied|needs you|pill/i, `no pill text in the session line, ${name}`);
    assert.match(lineTip, /^cao-a: \S/, "the tooltip says the state in words");
    const row = A.agentRows(s, [], { now: 10 * MIN, replied: (t) => replied && t.id === "c1" }).rows;
    for (const r of row) {
      const { tip, term, ...drawn } = r;
      assert.doesNotMatch(JSON.stringify(drawn), WORDS, `agent row ${r.id}, ${name}`);
      assert.doesNotMatch(JSON.stringify(drawn), /Asking|Replied|needs you|pill/i, `no pill text in agent row ${r.id}, ${name}`);
      assert.ok(tip.length > r.name.length, "the tooltip holds the words");
    }
  }
  // the attention signal is a mark and a colour: the square for needs you, the highlighted dot for a reply
  const asking = freeze(sess("cao-a", [T("c1", "cao-a"), worker("w1", "cao-a", "c1", { state: "blocked" })]));
  assert.equal(A.sessionLine(asking, { replied: never }).extra, "bang");
  const repliedSession = freeze(sess("cao-b", [T("c2", "cao-b", { state: "done", turn: 2, seen: 1 })]));
  assert.equal(A.sessionLine(repliedSession, { replied: () => true }).shape, "dot-hi");
});

test("The marks are drawn as Herdr's Agents list draws them: the same spinner file, sizes, spacing and theme tokens, and nothing of our own", () => {
  const src = readFileSync(new URL("./ui.js", import.meta.url), "utf8");
  const body = src.slice(src.indexOf("export function shapeMark"), src.indexOf("// The dialog's lease key"));
  assert.ok(body.length > 300, "fixture: found the function");
  assert.doesNotMatch(body, /#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(/, "no hex, rgb or hsl colour literal");
  const tokens = [...new Set([...body.matchAll(/\bc\.([a-z_0-9]+)/g)].map((m) => m[1]))].sort();
  assert.deepEqual(tokens, ["ansi2", "blocked", "dim", "done", "idle", "working"], "the tokens Herdr's status() reads, and ansi2 for the replied dot, and no other");
  // working: mods/agents/main.js:214
  assert.match(body, /uiSvg\("mods\/workspace-chrome\/spinner\.svg"\)\.size\(12\)\.mx\(1\)\.flex_none\(\)\.text_color\(c\.working\)\.spin\(900\)/);
  // blocked, compact: :215
  assert.match(body, /size\(14\)\.flex_none\(\)\.rounded\(4\)\.bg\(c\.blocked \+ "26"\)\.text_size\(10\.5\)\.font_bold\(\)\.text_color\(c\.blocked\)/);
  // idle: :218, done: :219
  assert.match(body, /size\(7\)\.mx\(3\)\.flex_none\(\)\.rounded\(4\)\.border\(1\.5\)\.border_color\(c\.idle \?\? c\.dim\)/);
  assert.match(body, /size\(7\)\.mx\(3\)\.flex_none\(\)\.rounded\(4\)\.bg\(c\.done \?\? c\.dim\)/);
  // replied: the done dot's shape (:219) in the highlight token ansi2, the one addition of this window (D39 D)
  assert.match(body, /"dot-hi": \(\) => uiDiv\(\)\.size\(7\)\.mx\(3\)\.flex_none\(\)\.rounded\(4\)\.bg\(c\.ansi2 \?\? c\.done \?\? c\.dim\)/);
  for (const own of ["\\u2713", "check", "dashed", "dotted", "ansi1", "accent", "muted"]) assert.ok(!body.includes(own), `no mark of our own: ${own}`);
});

// ---- the name of an agent (owner review, change C): the tab name CAO sets, else the profile, else a short id ----

test("Name of an agent: the Herdr tab name CAO sets (the cao.window label), else the profile, else a short id, and never a raw id while a name exists", () => {
  const n = (over) => A.agentName(T("464f5cb7", "s", over));
  assert.equal(n({ window: "conductor-774e", profile: "conductor" }), "conductor-774e", "tab name first");
  assert.equal(n({ window: "sherlock-8826", profile: undefined }), "sherlock-8826", "a tab name with no profile label yet");
  assert.equal(n({ window: undefined, profile: "sherlock" }), "sherlock", "no tab name: the profile");
  assert.equal(n({ window: "", profile: "sherlock" }), "sherlock", "an empty tab name is none");
  assert.equal(n({ window: "   ", profile: "sherlock" }), "sherlock", "a blank tab name is none");
  assert.equal(n({ window: "464f5cb7", profile: "sherlock" }), "sherlock", "a tab name that is the terminal id is the raw id, not a name");
  assert.equal(n({ window: undefined, profile: undefined }), "464f5cb7", "last resort: the id");
  assert.equal(A.agentName(T("464f5cb7aa11bb22", "s", { window: undefined, profile: undefined })), "464f5cb7", "a long id is cut to eight characters");
  assert.equal(A.agentName(T(undefined, "s", { window: undefined, profile: undefined })), "", "nothing at all is an empty name, not 'undefined'");
});

test("Name on the rows and the tabs: the agent row, and the top bar's tab, show the same name", () => {
  const s = freeze(sess("s", [T("c1", "s", { window: "conductor-774e", profile: "conductor" }), worker("w1", "s", "c1", { window: "sherlock-8826", profile: "sherlock" }), worker("w2", "s", "c1", { window: undefined, profile: "reviewer" })]));
  const view = A.agentRows(s, []);
  assert.deepEqual(view.rows.map((r) => r.name), ["conductor-774e", "sherlock-8826", "reviewer"]);
  assert.deepEqual(view.rows.map((r) => A.agentName(r.term)), view.rows.map((r) => r.name));
  for (const r of view.rows) assert.ok(r.tip.startsWith(`${r.name}: `), r.id);
});

// ---- the age of an agent (change D): compact, 3h, 12m ----

test("Age: seconds read 'now', minutes '12m', hours '3h', days '2d', and no clock or no time in state is no age", () => {
  assert.equal(A.ageShort(0), "now");
  assert.equal(A.ageShort(59_999), "now");
  assert.equal(A.ageShort(60_000), "1m");
  assert.equal(A.ageShort(12 * MIN), "12m");
  assert.equal(A.ageShort(59 * MIN + 59_000), "59m");
  assert.equal(A.ageShort(60 * MIN), "1h");
  assert.equal(A.ageShort(3 * 60 * MIN + 20 * MIN), "3h");
  assert.equal(A.ageShort(23 * 60 * MIN + 59 * MIN), "23h");
  assert.equal(A.ageShort(24 * 60 * MIN), "1d");
  assert.equal(A.ageShort(50 * 60 * MIN), "2d");
  for (const bad of [NaN, Infinity, undefined, null, "5", -1]) assert.equal(A.ageShort(bad), null, String(bad));
});

test("Age on the row: the time in the current state, from the clock the caller gives; none without a clock or a start", () => {
  const now = 1000 * MIN;
  const mk = (over) => freeze(sess("s", [T("c1", "s", over)]));
  assert.equal(A.agentRows(mk({ statusMs: now - 3 * 60 * MIN }), [], { now }).rows[0].age, "3h");
  assert.equal(A.agentRows(mk({ statusMs: now - 12 * MIN }), [], { now }).rows[0].age, "12m");
  assert.equal(A.agentRows(mk({ statusMs: now - 2000 }), [], { now }).rows[0].age, "now");
  assert.equal(A.agentRows(mk({ statusMs: now - 12 * MIN }), []).rows[0].age, null, "no clock");
  assert.equal(A.agentRows(mk({ statusMs: undefined }), [], { now }).rows[0].age, null, "no start");
});

// ---- the pin marker (change D) ----

test("Pin marker: a worker row says 'click to pin' or 'pinned'; a talkable row has no pin", () => {
  const s = freeze(sess("s", [T("c1", "s"), worker("w1", "s", "c1"), worker("w2", "s", "c1")]));
  const [c, w1, w2] = A.agentRows(s, ["w1"]).rows;
  assert.deepEqual([c.pinTip, c.pinned], [null, false]);
  assert.deepEqual([w1.pinTip, w1.pinned], ["pinned", true]);
  assert.deepEqual([w2.pinTip, w2.pinned], ["click to pin", false]);
});

// ---- the focused terminal's row (owner review, change G) ----
// The row whose terminal Herdr has focused takes the selected highlight, whichever way the focus moved (a click on the row or on a
// top-bar tab, agent.next, space.select). The Agents block follows clicks for WHICH SESSION it shows, not for this.

test("Focused row: the row whose terminal is the focused one is the one to highlight, and no other", () => {
  const s = freeze(sess("s", [T("c1", "s", { createdMs: 1 }), worker("w1", "s", "c1", { createdMs: 2 }), worker("w2", "s", "c1", { createdMs: 3 })]));
  const view = freeze(A.agentRows(s, []));
  assert.deepEqual(A.focusedRow(view, { terminal: "cao/c1" }), "c1");
  assert.deepEqual(A.focusedRow(view, { terminal: "cao/w2" }), "w2");
  assert.deepEqual(A.focusedRow(view, { terminal: "w1" }), "w1", "an address without its machine still finds the row");
  assert.deepEqual(A.focusedRow(view, { terminal: "c" }), null, "a part of an id is not an address");
  assert.deepEqual(A.focusedRow(view, { terminal: "cao/c11" }), null, "nor is another id that begins the same");
});

test("Focused row: the agent the focused pane shows stands in when no terminal is focused, and a terminal wins over it", () => {
  const s = freeze(sess("s", [T("c1", "s"), worker("w1", "s", "c1", { createdMs: 2 })]));
  const view = freeze(A.agentRows(s, []));
  assert.deepEqual(A.focusedRow(view, { terminal: null, agent: "cao/w1" }), "w1");
  assert.deepEqual(A.focusedRow(view, { terminal: "cao/c1", agent: "cao/w1" }), "c1");
  assert.deepEqual(A.focusedRow(view, { terminal: "", agent: "cao/w1" }), "w1", "an empty terminal is none");
});

test("Focused row: a focused terminal that is not in this session, no focus at all, and no rows highlight nothing", () => {
  const s = freeze(sess("s", [T("c1", "s")]));
  const view = freeze(A.agentRows(s, []));
  for (const focus of [{ terminal: "cao/other-session-terminal" }, { terminal: "cd/t5" }, { terminal: null, agent: null }, {}, null, undefined, { terminal: 5 }, "cao/c1"]) {
    assert.deepEqual(A.focusedRow(view, focus), null, JSON.stringify(focus));
  }
  for (const none of [null, undefined, { rows: [] }, {}]) assert.deepEqual(A.focusedRow(none, { terminal: "cao/c1" }), null, JSON.stringify(none));
  assert.deepEqual(A.focusedRow(A.agentRows(null, []), { terminal: "cao/c1" }), null, "no session");
});

test("Focused row: a worker of a large session has its own row, so the focus on it marks that row and nothing else (design D42 C)", () => {
  const s = freeze(sess("s", [T("c1", "s"), worker("w1", "s", "c1", { createdMs: 2 }), worker("w2", "s", "c1", { createdMs: 3 }), ...fill(11)]));
  const view = freeze(A.agentRows(s, []));
  assert.equal(view.rows.length, 14);
  assert.equal(A.focusedRow(view, { terminal: "cao/w2" }), "w2");
  assert.equal(A.focusedRow(view, { terminal: "cao/f10" }), "f10");
  assert.equal(A.focusedRow(view, { terminal: "cao/not-in-this-session" }), null);
  assert.equal(typeof A.focusedRow(view, { terminal: "cao/c1" }), "string", "the answer is a row id, not a record with a fold flag");
});

// ---- 18.3: sessionSelection and agentsHeader ----

test("Stored selection of another kind: a flow, a workflow, a form kind or a malformed value reads as no selection", () => {
  const stale = [
    { kind: "flow", name: "daily" }, { kind: "workflow", name: "w" }, { kind: "new-session" }, { kind: "new-flow" },
    { kind: "edit-flow", name: "daily" }, { kind: "new-workflow" }, { kind: "edit-workflow", name: "w" },
    { kind: "Session", name: "cao-a" }, { kind: "session" }, { kind: "session", name: "" }, { kind: "session", name: 5 },
    { kind: "session", name: null }, {}, [], ["session", "cao-a"], "cao-a", 5, true, null, undefined,
  ];
  for (const raw of stale) assert.ok(nothing(A.sessionSelection(freeze(raw))), JSON.stringify(raw));
});

test("Stored session selection kept: {kind: session, name} stays, whatever else the record holds", () => {
  assert.deepEqual(A.sessionSelection(freeze({ kind: "session", name: "cao-a" })), { kind: "session", name: "cao-a" });
  assert.deepEqual(A.sessionSelection(freeze({ kind: "session", name: "cao-b", extra: 1 })), { kind: "session", name: "cao-b" });
});

const FLOWS = freeze([{ name: "daily-report", schedule: "0 9 * * *", enabled: true }, { name: "other", schedule: "weird", enabled: false }]);
const TAIL = "\u203b";
const mkA = () => freeze(sess("cao-a", [T("c1", "cao-a", { state: "idle", turn: 2 }), worker("w1", "cao-a", "c1")]));
const NO_FLOWS = freeze([]);

test("Selected session only: the header reads Agents - cao-a and the tree holds cao-a's agents only", () => {
  const a = mkA();
  const b = freeze(sess("cao-b", [T("c9", "cao-b"), worker("w9", "cao-b", "c9", { state: "blocked" })]));
  const head = A.agentsHeader(a, NO_FLOWS, {}, {});
  assert.equal(head.title, `Agents ${DOT} cao-a`);
  assert.ok(nothing(head.note));
  const rows = A.agentRows(a, []).rows.map((r) => r.id);
  assert.deepEqual(rows, ["c1", "w1"]);
  assert.ok(!rows.includes("w9"), "cao-b's blocked worker is not in cao-a's tree");
  assert.equal(A.agentsHeader(b, NO_FLOWS, {}, {}).title, `Agents ${DOT} cao-b`);
});

test("Nothing selected: the header reads 'Select a session' and carries no lines", () => {
  for (const none of [null, undefined]) {
    const head = A.agentsHeader(none, FLOWS, { contextPercentRegex: "ctx (\\d+)%" }, { text: `${TAIL} recap: x\nctx 4%` });
    assert.equal(head.title, "Select a session");
    assert.deepEqual([head.note, head.flow, head.recap, head.context].map((v) => v ?? null), [null, null, null, null]);
  }
});

test("Selected session gone: a stored selection no session answers to says it is not listed and may have ended", () => {
  const head = A.agentsHeader(freeze({ kind: "session", name: "cao-a" }), FLOWS, {}, { text: `${TAIL} recap: x` });
  assert.equal(head.title, `Agents ${DOT} cao-a`);
  assert.match(head.note, /cao-a/);
  assert.match(head.note, /not listed/);
  assert.match(head.note, /may have ended/);
  assert.deepEqual([head.flow, head.recap, head.context].map((v) => v ?? null), [null, null, null], "no lines for a session that is not there");
});

test("Flow session: 'Started by schedule daily-report', labelled, with the schedule in words; no such flow, no line", () => {
  const s = freeze(sess("cao-flow-daily-report", [T("c1", "cao-flow-daily-report")]));
  const head = A.agentsHeader(s, FLOWS, {}, {});
  assert.deepEqual(head.flow, { flow: "daily-report", text: "Started by schedule daily-report", label: "inferred from the session name", schedule: "daily at 09:00" });
  assert.equal(head.flow.label, HONESTY.flowInferred);
  const odd = A.agentsHeader(freeze(sess("cao-flow-other", [T("c1", "cao-flow-other")])), FLOWS, {}, {});
  assert.equal(odd.flow.schedule, "weird", "a schedule that has no words is shown as the flow gives it");
  assert.ok(nothing(A.agentsHeader(freeze(sess("cao-flow-gone", [T("c1", "cao-flow-gone")])), FLOWS, {}, {}).flow), "the flow does not exist");
  assert.ok(nothing(A.agentsHeader(mkA(), FLOWS, {}, {}).flow), "not a flow session");
});

test("Recap present: the root's recap line is shown labelled as the agent's own words", () => {
  const head = A.agentsHeader(mkA(), NO_FLOWS, {}, { text: `some output\n${TAIL} recap: Tests pass; waiting for review.\n> ` });
  assert.deepEqual(head.recap, { text: "Tests pass; waiting for review.", label: "the agent's own words" });
  assert.equal(head.recap.label, HONESTY.recap);
});

test("No recap: no recap line, and nothing is inferred from other text", () => {
  for (const reads of [{ text: "Tests pass; waiting for review.\n> " }, { text: "" }, {}, undefined]) {
    assert.ok(nothing(A.agentsHeader(mkA(), NO_FLOWS, {}, reads).recap), JSON.stringify(reads));
  }
});

test("Context percentage off by default: shown, as a heuristic, only when contextPercentRegex is set and matches", () => {
  const reads = { text: "ctx 42% used" };
  assert.ok(nothing(A.agentsHeader(mkA(), NO_FLOWS, {}, reads).context), "no regex configured");
  assert.ok(nothing(A.agentsHeader(mkA(), NO_FLOWS, undefined, reads).context), "no props at all");
  assert.ok(nothing(A.agentsHeader(mkA(), NO_FLOWS, { contextPercentRegex: "nothing (\\d+)" }, reads).context), "regex does not match");
  assert.deepEqual(A.agentsHeader(mkA(), NO_FLOWS, { contextPercentRegex: "ctx (\\d+)%" }, reads).context, { percent: 42, label: "heuristic" });
});

// Owner review of the live window (27.19, D37 R): "I am not sure showing the directory in the Agents list is helpful".
test("No folder line: the header has no working directory, whatever was read, and keeps its title, note, flow, recap and context", () => {
  const reads = { cwd: "/work/repo", text: `${TAIL} recap: Done.\nctx 40%` };
  const flowSession = freeze(sess("cao-flow-daily-report", [T("c1", "cao-flow-daily-report")]));
  const gone = freeze({ kind: "session", name: "cao-a" });
  for (const [what, session] of [["a session", mkA()], ["a flow session", flowSession], ["a session that is gone", gone], ["no session", null]]) {
    const head = A.agentsHeader(session, FLOWS, { contextPercentRegex: "ctx (\\d+)%" }, reads);
    assert.deepEqual(Object.keys(head).sort(), ["context", "flow", "note", "recap", "title"], `${what}: the fields of the header`);
    assert.ok(!JSON.stringify(head).includes("/work/repo"), `${what}: the folder is nowhere in the header`);
  }
  const head = A.agentsHeader(flowSession, FLOWS, { contextPercentRegex: "ctx (\\d+)%" }, reads);
  assert.equal(head.title, `Agents ${DOT} cao-flow-daily-report`);
  assert.equal(head.flow.text, "Started by schedule daily-report");
  assert.deepEqual(head.recap, { text: "Done.", label: HONESTY.recap });
  assert.deepEqual(head.context, { percent: 40, label: "heuristic" });
});

const everything = () => {
  const s = freeze(sess("cao-flow-daily-report", [T("c1", "cao-flow-daily-report", { state: "done", turn: 2 }), worker("w1", "cao-flow-daily-report", "c1", { state: "blocked" })]));
  const head = A.agentsHeader(s, FLOWS, { contextPercentRegex: "ctx (\\d+)%" }, { text: `${TAIL} recap: Done.\nctx 40%` });
  const text = JSON.stringify([head, A.agentRows(s, [], { now: 1, replied: () => true })]);
  assert.ok(text.includes("Started by schedule daily-report") && text.includes("recap") && text.includes("40"), "fixture check: the header did build its lines");
  return text;
};

test("No notes in the block: the view models carry no 'done means the turn ended' sentence", () => {
  const text = everything();
  assert.doesNotMatch(text, /turn ended/i);
  assert.doesNotMatch(text, /doneMeans/);
  assert.ok(!text.includes(knownGapNote().doneMeans));
});

test("Known gap is not in the Agents block: neither the sentence nor its label appears in the header or the tree", () => {
  const text = everything();
  assert.ok(!text.includes(knownGapNote().text));
  assert.ok(!text.includes(HONESTY.knownGap));
  assert.doesNotMatch(text, /known gap|prose/i);
});

// ---- 18.4: sessionsHeader and foldSummary ----

// 13 sessions: cao-0 has a blocked worker, cao-1 a working conductor, the rest idle conductors.
const thirteen = () => {
  const out = [];
  for (let i = 0; i < 13; i += 1) {
    const n = `cao-${i}`;
    const terms = [T(`c${i}`, n, { state: i === 1 ? "working" : "idle" })];
    if (i === 0) terms.push(worker("w0", n, "c0", { state: "blocked" }));
    out.push(sess(n, terms));
  }
  return freeze(out);
};
const draftSession = () => freeze(sess("cao-draft-flow-0a1b2c3d", [T("d1", "cao-draft-flow-0a1b2c3d", { state: "blocked" })], {}, ["d1"]));

test("Filter: with a filter on the header reads 'Sessions M of S - filter: name' with a clear control", () => {
  const all = thirteen();
  const asking = filterSessions(all, "asking", never);
  assert.equal(asking.length, 1, "fixture: one asking session");
  const head = A.sessionsHeader(all, asking, "asking");
  assert.equal(head.text, `Sessions 1 of 13 ${DOT} filter: asking`);
  assert.equal(head.clearable, true);
  const working = filterSessions(all, "working", never);
  assert.equal(A.sessionsHeader(all, working, "working").text, `Sessions 1 of 13 ${DOT} filter: working`);
  const replied = filterSessions(all, "replied", (t) => t.id === "c2");
  assert.equal(A.sessionsHeader(all, replied, "replied").text, `Sessions 1 of 13 ${DOT} filter: replied`);
  assert.equal(A.sessionsHeader(all, [], "replied").text, `Sessions 0 of 13 ${DOT} filter: replied`, "a filter that matches nothing still shows its header and control");
  assert.equal(A.sessionsHeader(all, [], "replied").clearable, true);
});

test("Clear control: with no filter the header reads 'Sessions' and has no clear control", () => {
  const all = thirteen();
  for (const off of [null, undefined, "", "nonsense"]) {
    const head = A.sessionsHeader(all, all, off);
    assert.deepEqual([head.text, head.clearable], ["Sessions", false], JSON.stringify(off));
  }
  assert.deepEqual([A.sessionsHeader([], [], null).text, A.sessionsHeader([], [], null).clearable], ["Sessions", false]);
});

test("Draft sessions are not in the header count: 13 sessions plus a draft, asking filter on, reads 'Sessions 1 of 13 - filter: asking'", () => {
  const all = freeze([...thirteen(), draftSession()]);
  assert.equal(all.length, 14);
  const listed = [...filterSessions(all, "asking", never), draftSession()];
  assert.equal(A.sessionsHeader(all, listed, "asking").text, `Sessions 1 of 13 ${DOT} filter: asking`, "the draft stays listed and is not counted");
  assert.equal(A.sessionsHeader(all, filterSessions(all, "asking", never), "asking").text, `Sessions 1 of 13 ${DOT} filter: asking`);
  assert.equal(A.sessionsHeader(all, all, null).text, "Sessions");
});

test("Folded summaries: Sessions '13' or '13, 1 asking', Agents the selected name or 'none selected'", () => {
  const all = thirteen();
  assert.equal(A.foldSummary("sessions", all), "13, 1 asking");
  const calm = freeze(all.slice(1));
  assert.equal(A.foldSummary("sessions", calm), "12");
  assert.equal(A.foldSummary("sessions", freeze([...all.slice(0, 1), ...all.slice(2), ...all.slice(1, 2)])), "13, 1 asking");
  assert.equal(A.foldSummary("sessions", freeze([...calm, draftSession()])), "12", "a recorded draft is not counted, nor is it asking");
  assert.equal(A.foldSummary("sessions", []), "0");
  assert.equal(A.foldSummary("agents", "cao-a"), "cao-a");
  assert.equal(A.foldSummary("agents", null), "none selected");
  assert.equal(A.foldSummary("agents", undefined), "none selected");
  assert.equal(A.foldSummary("agents", ""), "none selected");
});

// ---- the session's letter badge (owner review, task 27.12): the spaces list of Herdr's own window, copied ----
// Herdr's spaces mod (mods/spaces/main.js) draws each space as an 18 px badge with rounded 5, 10.5 px bold white letter, the first
// letter of the name (:502), filled with a colour that is NOT a hash of the name: a stored assignment per space, the palette colour
// used least so far (render(), :527-537), shown as the hex of its MARK table (:61), `ansi6` when none is stored (:455). The window
// has no Herdr space per session, so the same rule runs over the session names, stored under its own shared key.

test("Badge letter: the first character of the name, upper-cased as Herdr does ([...name][0]?.toUpperCase() ?? '?'), a code point and not a code unit", () => {
  const letters = [["supervisor", "S"], ["caomail", "C"], ["cao", "C"], ["\u00e9lan", "\u00c9"], ["\u{1f600}x", "\u{1f600}"], ["\u00df", "SS"], ["9", "9"], [" x", " "]];
  for (const [name, letter] of letters) assert.equal(A.badgeLetter(name), letter, JSON.stringify(name));
  for (const v of ["", undefined, null, 5, {}]) assert.equal(A.badgeLetter(v), "?", `${JSON.stringify(v)} has no letter`);
});

// ---- refinement 1 (task 29.1): every session name starts with `cao-`, so the badge letter skips that prefix ----

test("Badge letter skips a leading `cao-`: the first letter or digit after it, upper-cased (owner review, D39 A)", () => {
  const cases = [
    ["cao-aria-orchestration-lineage-lld", "A"], ["cao-ws-x", "W"], ["cao-sci-x", "S"], ["cao-ml-x", "M"], ["cao-cr-x", "C"],
    ["cao-a", "A"], ["CAO-aria", "A"], ["Cao-Ws", "W"], ["cAo-ml", "M"],
    ["cao-9lives", "9"], ["cao--x", "X"], ["cao-_-x", "X"], ["cao- x", "X"], ["cao-\u00e9lan", "\u00c9"], ["cao-\u00df", "SS"], ["cao-flow-nightly", "F"],
  ];
  for (const [name, letter] of cases) assert.equal(A.badgeLetter(name), letter, JSON.stringify(name));
});

test("Badge letter fallbacks: `cao-` alone, or nothing alphanumeric after it, uses the old rule on the whole name; a name without the prefix is unchanged", () => {
  const cases = [
    ["cao-", "C"], ["CAO-", "C"], ["cao--", "C"], ["cao-___", "C"], ["cao- ", "C"], ["cao-\u{1f600}", "C"],
    ["caoa", "C"], ["cao_a", "C"], ["xcao-a", "X"], [" cao-a", " "], ["ao-a", "A"], ["-cao-a", "-"], ["aria", "A"], ["\u00e9lan", "\u00c9"], ["9", "9"],
  ];
  for (const [name, letter] of cases) assert.equal(A.badgeLetter(name), letter, JSON.stringify(name));
  for (const v of ["", undefined, null, 5, {}]) assert.equal(A.badgeLetter(v), "?", `${JSON.stringify(v)} has no letter`);
});

test("sessionLine's badge letter is the one after `cao-`, and the stored colour does not depend on it", () => {
  const s = freeze(sess("cao-ws-notes", [T("c1", "cao-ws-notes", { state: "idle" })]));
  const line = A.sessionLine(s, { replied: never, quiet: null, colors: { "cao-ws-notes": "ansi3" } });
  assert.deepEqual(line.badge, { letter: "W", color: "ansi3" });
  assert.deepEqual(A.sessionLine(s, { replied: never, quiet: null }).badge, { letter: "W", color: "ansi6" });
  assert.deepEqual(A.badgeColors({}, ["cao-a-1", "cao-a-2", "cao-b-1"]).colors, { "cao-a-1": "ansi6", "cao-a-2": "ansi3", "cao-b-1": "ansi4" }, "the colour is the least-used one per session, whatever the letter");
});

test("Badge colours: a new session takes the palette colour used least, the first in palette order on a tie; a known one keeps its colour", () => {
  assert.deepEqual(A.BADGE_PALETTE, ["ansi6", "ansi3", "ansi4", "ansi5", "ansi2", "ansi1"], "Herdr's palette, in its order (spaces/main.js:527)");
  const seven = A.badgeColors({}, ["a", "b", "c", "d", "e", "f", "g"]);
  assert.deepEqual(seven.colors, { a: "ansi6", b: "ansi3", c: "ansi4", d: "ansi5", e: "ansi2", f: "ansi1", g: "ansi6" });
  assert.equal(seven.changed, true);
  assert.deepEqual(A.badgeColors({ a: "ansi3" }, ["a", "b"]).colors, { a: "ansi3", b: "ansi6" }, "ansi6 is used least (never), and comes first");
  assert.deepEqual(A.badgeColors({ a: "ansi6", b: "ansi6", c: "ansi3" }, ["a", "b", "c", "d"]).colors, { a: "ansi6", b: "ansi6", c: "ansi3", d: "ansi4" }, "ansi3 is used once, ansi4 never, and ansi4 is the first with none");
  assert.deepEqual(A.badgeColors({ a: "ansi6", b: "ansi3", c: "ansi4", d: "ansi5", e: "ansi2", f: "ansi1", g: "ansi6" }, ["a", "b", "c", "d", "e", "f", "g", "h"]).colors.h, "ansi3", "ansi6 twice, the rest once: the first with one use");
  const same = A.badgeColors({ a: "ansi3", b: "ansi5" }, ["a", "b"]);
  assert.deepEqual([same.colors, same.changed], [{ a: "ansi3", b: "ansi5" }, false], "nothing new, nothing gone: unchanged");
  assert.deepEqual(A.badgeColors({ a: "ansi3" }, ["b", "a"]).colors, { a: "ansi3", b: "ansi6" }, "the order of the names is the order of assignment, and a kept colour is kept whatever the order");
});

test("Badge colours: a session that is gone loses its colour, a list with no session prunes nothing, and a stored value that is no colour is replaced", () => {
  const gone = A.badgeColors({ a: "ansi3", old: "ansi4" }, ["a"]);
  assert.deepEqual([gone.colors, gone.changed], [{ a: "ansi3" }, true]);
  const lost = freeze({ a: "ansi3", b: "ansi4" });
  for (const names of [[], undefined, null]) {
    const r = A.badgeColors(lost, names);
    assert.deepEqual([r.colors, r.changed], [{ a: "ansi3", b: "ansi4" }, false], `a lost connection (${JSON.stringify(names)}) empties the list: the colours survive it`);
  }
  for (const stored of [undefined, null, [], "ansi3", 5]) assert.deepEqual(A.badgeColors(stored, ["a"]).colors, { a: "ansi6" }, `${JSON.stringify(stored)} holds no colours`);
  assert.deepEqual(A.badgeColors({ a: 5, b: "" }, ["a", "b"]).colors, { a: "ansi6", b: "ansi3" }, "a number and an empty string are no colour");
  assert.deepEqual(A.badgeColors({ a: "#336699" }, ["a", "b"]).colors, { a: "#336699", b: "ansi6" }, "a stored hex is kept, as Herdr keeps it (:456), and counts for no palette colour");
  const input = freeze({ a: "ansi3" });
  A.badgeColors(input, ["a", "b"]);   // frozen: a write would throw
});

test("Badge fill: the hex of Herdr's MARK table for a palette colour, a stored hex as it is, another name from the theme, ansi6 when nothing fits", () => {
  const t = { accent: "#aabbcc", ansi4: "#000004" };
  assert.deepEqual(Object.fromEntries(A.BADGE_PALETTE.map((c) => [c, A.badgeFill(c, t)])), {
    ansi6: "#22a99a", ansi3: "#e3814f", ansi4: "#5b8def", ansi5: "#8b7cf6", ansi2: "#3fae6a", ansi1: "#d9649a",   // mods/spaces/main.js:61, and the theme's own ansi4 does not win
  });
  assert.equal(A.badgeFill("#123456", t), "#123456");
  assert.equal(A.badgeFill("accent", t), "#aabbcc");
  for (const v of ["nothing", "", undefined, null, 5]) assert.equal(A.badgeFill(v, t), "#22a99a", `${JSON.stringify(v)}`);
});

test("Session line: the badge is the name's letter and the stored colour name (ansi6 when none is stored), and nothing else of the line changes", () => {
  const s = freeze(sess("cao-a", [T("c1", "cao-a", { state: "working" })]));
  assert.deepEqual(A.sessionLine(s, { replied: never, colors: { "cao-a": "ansi3", other: "ansi4" } }).badge, { letter: "A", color: "ansi3" });
  assert.deepEqual(A.sessionLine(s, { replied: never }).badge, { letter: "A", color: "ansi6" });
  assert.deepEqual(A.sessionLine(s, { replied: never, colors: {} }).badge, { letter: "A", color: "ansi6" });
  assert.deepEqual(A.sessionLine(s, { replied: never, colors: null }).badge, { letter: "A", color: "ansi6" });
  const draft = freeze({ ...sess("author-1", [T("c9", "author-1")]), draft: true });
  assert.deepEqual(A.sessionLine(draft, { replied: never, colors: { "author-1": "ansi5" } }).badge, { letter: "A", color: "ansi5" }, "a draft session has its badge too");
});

// ---- the agent's program icon (owner review, task 27.13): the logo of the kind of agent, as Herdr's lists draw it ----
// Herdr's Agents list and tabs mod draw `program.icon` of the terminal's record, a path in the home folder (`icons/claude.svg`), by
// `p.tint ? svg(p.icon).size(n).text_color(c) : image(p.icon).w(n).h(n)` (mods/agents/main.js:78-81; mods/tabs/main.js:120), and
// draw nothing when the record has no icon. Herdr's own table maps the agent's reported `app` (`claude-code`, `kiro-cli`, `codex`) to that
// program, so the window reads the program the record already carries and keeps no table of its own. When a record has no icon the window
// draws Herdr's generic agent glyph, `icons/agent.svg` (a tinted bot), from the same folder, instead of an empty slot.

test("Program icon: the path and tint Herdr resolved are used as they are", () => {
  const claude = { key: "claude", name: "Claude Code", icon: "icons/claude.svg", tint: false };
  assert.deepEqual(A.programIcon(claude), { path: "icons/claude.svg", tint: false });
  assert.deepEqual(A.programIcon({ icon: "icons/kiro.svg", tint: false }), { path: "icons/kiro.svg", tint: false });
  assert.deepEqual(A.programIcon({ icon: "icons/codex.svg", tint: false }), { path: "icons/codex.svg", tint: false });
  assert.deepEqual(A.programIcon({ icon: "icons/agent.svg", tint: true }), { path: "icons/agent.svg", tint: true });
  assert.deepEqual(A.programIcon({ icon: "/opt/herdr-icons/mine.svg", tint: false }), { path: "/opt/herdr-icons/mine.svg", tint: false }, "programs.json may name an absolute path");
  assert.equal(A.programIcon({ icon: "icons/x.svg", tint: 1 }).tint, true, "a truthy tint tints, as `p.tint ?` does");
  assert.equal(A.programIcon({ icon: "icons/x.svg" }).tint, false, "no tint is a picture");
  const frozen = freeze({ icon: "icons/claude.svg", tint: false });
  assert.notEqual(A.programIcon(frozen), frozen, "a new object, the input untouched");
});

test("Program icon: no program, or one with no usable icon, falls back to Herdr's generic agent glyph, tinted", () => {
  const generic = { path: "icons/agent.svg", tint: true };
  for (const v of [undefined, null, {}, [], "claude", 5, { icon: null }, { icon: "" }, { icon: "   " }, { icon: 7 }, { icon: {}, tint: false }]) {
    assert.deepEqual(A.programIcon(v), generic, JSON.stringify(v));
  }
});

test("Row and tab icons: the model gives each agent the icon of its program, and the generic glyph when the record has none", () => {
  const claude = { icon: "icons/claude.svg", tint: false };
  const kiro = { icon: "icons/kiro.svg", tint: false };
  const s = freeze(sess("s", [T("c1", "s", { program: claude }), worker("w1", "s", "c1", { program: kiro }), worker("w2", "s", "c1", { createdMs: 6 })]));
  const rows = A.agentRows(s, []).rows;
  assert.deepEqual(rows.map((r) => r.icon), [{ path: "icons/claude.svg", tint: false }, { path: "icons/kiro.svg", tint: false }, { path: "icons/agent.svg", tint: true }]);
  const tabs = A.tabViews(s, ["w1", "w2"]);
  assert.deepEqual(tabs.map((t) => t.icon), rows.map((r) => r.icon), "a tab and its row show the same logo");
});

// ---- the pin (owner review, tasks 27.10 and 27.14): a pushed-in pin when pinned, a tilted outline when not ----
// Herdr's own pin (mods/workspace-chrome/pin.svg, drawn by mods/agents/main.js:343) is an outline in both states, and svg() takes a
// colour, not a fill, so the window ships two small drawings of its own in its side mod's folder and finds them by path as Herdr's tabs
// mod finds its own win-*.svg (mods/tabs/main.js:153): pinned is a filled upright thumbtack (head up, point down) in the accent;
// unpinned is the same thumbtack turned 45 degrees, outline only, muted, so the state reads from the shape as well as the colour.

const PIN_DIR = "../mods/cao-window-side/";
const pinSvg = (name) => readFileSync(new URL(`${PIN_DIR}${name}`, import.meta.url), "utf8");
// The points of every path of a drawing that uses only absolute M, L and Z, in order: [[x, y], ...].
function pinPoints(svg) {
  const points = [];
  for (const [, d] of svg.matchAll(/<path\b[^>]*\sd="([^"]*)"/g)) {
    assert.match(d, /^[MLZ0-9.,\s-]+$/, `only absolute M, L and Z commands, so the points can be checked: ${d}`);
    for (const m of d.matchAll(/(-?\d+(?:\.\d+)?)[\s,]+(-?\d+(?:\.\d+)?)/g)) points.push([Number(m[1]), Number(m[2])]);
  }
  return points;
}

test("Pin mark: pinned is the filled upright pin in the accent, unpinned the tilted outline pin, muted; anything else is unpinned", () => {
  assert.deepEqual(A.pinMark(true), { icon: "mods/cao-window-side/pin-filled.svg", tone: "accent" });
  assert.deepEqual(A.pinMark(false), { icon: "mods/cao-window-side/pin-tilted.svg", tone: "dim" });
  assert.notEqual(A.pinMark(true).icon, A.pinMark(false).icon, "the state shows in the shape");
  for (const v of [undefined, null, "yes", 1, {}]) assert.deepEqual(A.pinMark(v), A.pinMark(false), `${String(v)} is not a pin`);
});

test("Pin drawings: the pinned one is filled with the current colour and has no outline-only shape; the unpinned one is outline only", () => {
  const filled = pinSvg("pin-filled.svg");
  const tilted = pinSvg("pin-tilted.svg");
  for (const [name, svg] of [["pin-filled.svg", filled], ["pin-tilted.svg", tilted]]) {
    assert.match(svg, /^<svg [^>]*viewBox="0 0 24 24"/, `${name} is drawn on the 24 unit grid Herdr's icons use`);
    assert.match(svg, /xmlns="http:\/\/www\.w3\.org\/2000\/svg"/);
    assert.doesNotMatch(svg, /<(script|image|use|style|text)\b|\bhref=|#[0-9a-fA-F]{3,8}\b|rgba?\(/, `${name}: a path and the current colour only: no script, image, reference or colour literal`);
    assert.ok(svg.length < 800, `${name} is a small drawing (${svg.length} bytes)`);
  }
  assert.match(filled, /fill="currentColor"/, "filled: svg() colours it with the token the row passes");
  assert.doesNotMatch(filled, /fill="none"/, "the pinned pin is not an outline with an accent stroke");
  assert.match(tilted, /fill="none"/);
  assert.match(tilted, /stroke="currentColor"/);
  assert.doesNotMatch(tilted, /fill="currentColor"/, "the unpinned pin is outline only");
});

test("Pin drawings: the pinned pin stands upright (a flat head on top, the point straight down), and the unpinned one is that shape turned 45 degrees", () => {
  const up = pinPoints(pinSvg("pin-filled.svg"));
  assert.ok(up.length >= 8, `${up.length} points`);
  const top = Math.min(...up.map((p) => p[1]));
  const bottom = Math.max(...up.map((p) => p[1]));
  assert.equal(up.filter((p) => p[1] === top).length, 2, "the head's top edge is flat: two points share the top");
  assert.deepEqual(up.filter((p) => p[1] === bottom), [[12, bottom]], "one point is lowest, and it is on the centre line: the needle points straight down");
  assert.ok(bottom - top >= 18 && bottom <= 24 && top >= 0, "it fills the grid's height and stays inside it");
  assert.ok(Math.min(...up.map((p) => p[0])) >= 0 && Math.max(...up.map((p) => p[0])) <= 24);
  const tilted = pinPoints(pinSvg("pin-tilted.svg"));
  const c = Math.cos(Math.PI / 4);
  const turned = up.map(([x, y]) => [12 + (x - 12) * c - (y - 12) * c, 12 + (x - 12) * c + (y - 12) * c]);   // 45 degrees clockwise about the centre
  assert.equal(tilted.length, turned.length, "the same points");
  tilted.forEach((p, i) => assert.ok(Math.abs(p[0] - turned[i][0]) < 0.02 && Math.abs(p[1] - turned[i][1]) < 0.02, `point ${i}: ${p} is not ${turned[i].map((v) => v.toFixed(2))}`));
  assert.ok(Math.min(...tilted.flat()) >= 0 && Math.max(...tilted.flat()) <= 24, "the tilted one stays inside the grid");
});

test("Pin mark: no diamond or other text glyph is left in the library or the mods", () => {
  const files = ["lib/agents.js", "lib/ui.js", "lib/logic.js", "mods/cao-window-side/main.js", "mods/cao-window-tabs/main.js"];
  for (const f of files) {
    const src = readFileSync(new URL(`../${f}`, import.meta.url), "utf8");
    assert.doesNotMatch(src, /\\u25c[678]|\\u2666|\\u2756|\\u\{1f4cc\}|[\u25c6\u25c7\u25c8\u2666\u2756\u{1f4cc}]/iu, `${f} has a diamond or a pushpin glyph, written or escaped`);
  }
});

// ---- the top bar's tabs (owner review, task 27.11): every agent by its name, and no tag ----

test("Tabs: every tab is named by agentName, the conductor like the rest, and a tab carries no worker or talkable tag", () => {
  const s = freeze(sess("s", [
    T("c1", "s", { window: "conductor-1bd7", state: "working" }),
    T("tc", "s", { window: "thread-coordinator-678f", profile: "thread-coordinator", caller: "c1", state: "idle" }),
    worker("w1", "s", "c1", { window: "developer-ab12", state: "idle" }),
  ], { talkableProfiles: ["thread-coordinator"] }));
  const tabs = A.tabViews(s, ["w1"]);
  assert.deepEqual(tabs.map((t) => t.name), ["conductor-1bd7", "thread-coordinator-678f", "developer-ab12"]);
  assert.deepEqual(tabs.map((t) => t.name), tabs.map((t) => A.agentName(t.term)), "the rule is agentName, as the Agents rows use");
  const rows = A.agentRows(s, ["w1"]).rows;
  assert.deepEqual(tabs.map((t) => t.name), rows.map((r) => r.name), "a tab and its row in the Agents block read alike");
  for (const t of tabs) {
    assert.deepEqual(Object.keys(t).sort(), ["icon", "id", "main", "name", "term", "tip"], `${t.id}: a tab has a program icon, a name, the crown flag and a tooltip, and no tag field and no state mark`);
    assert.doesNotMatch(JSON.stringify({ id: t.id, name: t.name }), /worker|talkable/i, "no word of the role on a tab");
  }
  assert.equal(tabs[0].term, s.terms[0], "the click focuses the terminal");
});

test("Tabs: the name falls back as it does on a row (profile, then a short id), the same for a talkable agent and a pinned worker", () => {
  const s = freeze(sess("s", [
    T("c1", "s", { window: "c1", profile: "conductor" }),
    worker("464f5cb7aa11", "s", "c1", { window: "464f5cb7aa11", profile: undefined }),
    worker("w2", "s", "c1", { window: undefined, profile: "reviewer" }),
  ]));
  assert.deepEqual(A.tabViews(s, ["464f5cb7aa11", "w2"]).map((t) => t.name), ["conductor", "464f5cb7", "reviewer"]);
});

test("Tabs: one shown agent gives one tab, named like its Agents row and crowned; none shown gives none; the glyph is the row's glyph (a reply marks a talkable agent only)", () => {
  const one = freeze(sess("s", [T("c1", "s")]));
  const row = A.agentRows(one, []).rows[0];
  assert.deepEqual(A.tabViews(one, []).map((t) => [t.id, t.name, t.icon, t.tip, t.main]), [["c1", row.name, row.icon, row.tip, true]], "the row's name, logo and tooltip, and the crown");
  assert.deepEqual(A.tabViews(one, ["c1", "ghost"]).map((t) => t.id), ["c1"], "a pin of a talkable or unknown id adds none");
  assert.deepEqual(A.tabViews(freeze({ name: "s", terms: [], talkable: [], workers: [] }), []), [], "no shown agent: no tab");
  const lone = freeze(sess("s", [T("c1", "s"), worker("w1", "s", "c1")]));
  assert.deepEqual(A.tabViews(lone, []).map((t) => t.id), ["c1"], "an unpinned worker adds none");
  const two = freeze(sess("s", [T("c1", "s", { state: "idle" }), worker("w1", "s", "c1", { state: "idle" })]));
  const tipsOf = (opts) => A.agentRows(two, ["w1"], opts).rows.map((r) => r.tip);
  assert.deepEqual(A.tabViews(two, ["w1"], { replied: () => true }).map((t) => t.tip), tipsOf({ replied: () => true }), "replied: the tooltips are the rows'");
  assert.match(A.tabViews(two, ["w1"], { replied: () => true })[0].tip, /^conductor: replied\b/, "a reply marks a talkable agent only");
  assert.deepEqual(A.tabViews(two, ["w1"]).map((t) => t.tip), tipsOf(undefined), "no predicate: nothing replied");
  assert.match(A.tabViews(two, ["w1"])[0].tip, /^conductor: idle\b/);
  const busy = freeze(sess("s", [T("c1", "s", { state: "working" }), worker("w1", "s", "c1", { state: "blocked" })]));
  assert.deepEqual(A.tabViews(busy, ["w1"]).map((t) => t.tip), A.agentRows(busy, ["w1"]).rows.map((r) => r.tip), "a blocked worker's tab says so in its tooltip");
});

// ---- no state mark on a tab (owner review, task 27.21): the owner found the top bar's status dot not helpful ----
// The state stays drawn on the Agents row and the Sessions line. A tab carries no mark of it; its tooltip is its Agents row's, so the
// state is still there in words.
test("Tabs: a tab view carries no state mark, and its tooltip is its Agents row's, the state in words", () => {
  for (const [name, over, replied, , word] of STATE_FIXTURES) {
    const s = freeze(sess("s", [T("c1", "s", over), worker("w1", "s", "c1", { state: "working" })]));
    const opts = { replied: (t) => replied && t.id === "c1" };
    const tabs = A.tabViews(s, ["w1"], opts);
    const rows = A.agentRows(s, ["w1"], opts).rows;
    for (const t of tabs) {
      assert.deepEqual(Object.keys(t).sort(), ["icon", "id", "main", "name", "term", "tip"], `${name}, ${t.id}: no shape on a tab`);
      assert.equal(t.tip, rows.find((r) => r.id === t.id).tip, `${name}, ${t.id}: the tooltip is the row's`);
    }
    assert.match(tabs[0].tip, new RegExp(`^conductor: ${word}\\b`), `${name}: the tab's tooltip says the state in words`);
    assert.notEqual(rows[0].shape, undefined, `${name}: the Agents row keeps its mark`);
  }
  const waiter = freeze(sess("s", [T("c1", "s", { state: "idle", turn: 4, seen: 3, waiting: 2 }), worker("w1", "s", "c1", { state: "working" })]));
  const waiting = A.tabViews(waiter, ["w1"]);
  assert.match(waiting[0].tip, /^conductor: waiting on 2 workers\b/, "a waiting agent's tab says so in its tooltip");
  assert.ok(waiting.every((t) => !("shape" in t)), "and draws no mark");
});

// ---- the top bar's order (owner review, task 27.16): the tabs run in the Agents block's tree order ----
// The tabs are the shown agents the bar picks (the talkable agents and the pinned workers, tabsFor), in the order the Agents block lists
// them (agentRows: the conductor first, each agent's children under it in creation order, then by id). CAO sets the order of Herdr's own
// tabs inside a space and undoes a manual drag, but this bar is a row of the window's own, so its order is the window's to set.

const TABS_PROPS = { talkableProfiles: ["thread-coordinator"] };
const coord = (id, session, caller, over = {}) => T(id, session, { profile: "thread-coordinator", caller, ...over });
const tabIds = (s, pins, opts) => A.tabViews(s, pins, opts).map((t) => t.id);
const rowOrder = (s, pins) => A.agentRows(s, pins).rows.map((r) => r.id);

test("Tab order: the conductor first, then its children as the Agents block lists them, not the talkable agents before the pinned workers", () => {
  const s = freeze(sess("s", [T("c1", "s", { createdMs: 1 }), worker("w1", "s", "c1", { createdMs: 2 }), coord("tc", "s", "c1", { createdMs: 3 })], TABS_PROPS));
  assert.deepEqual(s.talkable.map((t) => t.id), ["c1", "tc"], "fixture: two talkable agents");
  assert.deepEqual(rowOrder(s, ["w1"]), ["c1", "w1", "tc"], "fixture: the block lists the pinned worker before the later coordinator");
  assert.deepEqual(tabIds(s, ["w1"]), ["c1", "w1", "tc"]);
});

test("Tab order: a pinned worker sits under its parent, after it and before the parent's later sibling", () => {
  const s = freeze(sess("s", [
    T("c1", "s", { createdMs: 1 }), coord("t2", "s", "c1", { createdMs: 2 }), worker("w1", "s", "t2", { createdMs: 3 }), coord("t3", "s", "c1", { createdMs: 4 }),
  ], TABS_PROPS));
  assert.deepEqual(tabIds(s, ["w1"]), ["c1", "t2", "w1", "t3"]);
  assert.deepEqual(tabIds(s, ["w1"]), rowOrder(s, ["w1"]), "the same order as the block");
});

test("Tab order: the order of the pins does not change it", () => {
  const s = freeze(sess("s", [T("c1", "s", { createdMs: 1 }), worker("w1", "s", "c1", { createdMs: 2 }), worker("w2", "s", "c1", { createdMs: 3 }), worker("w3", "s", "c1", { createdMs: 4 })]));
  for (const pins of [["w1", "w2"], ["w2", "w1"], ["w2", "ghost", "w1"]]) assert.deepEqual(tabIds(s, pins), ["c1", "w1", "w2"], JSON.stringify(pins));
  assert.deepEqual(tabIds(s, ["w3", "w1"]), ["c1", "w1", "w3"], "a worker created later comes later whatever was pinned first");
});

test("Tab order: an agent whose parent is not listed takes its place in the block's tree, after the tree of the conductor created before it", () => {
  const s = freeze(sess("s", [T("c1", "s", { createdMs: 1 }), worker("w1", "s", "c1", { createdMs: 3 }), worker("w9", "s", "ghost", { createdMs: 2 })]));
  assert.deepEqual(rowOrder(s, ["w1", "w9"]), ["c1", "w1", "w9"], "fixture: the block lists the orphan after the conductor's whole tree");
  assert.deepEqual(tabIds(s, ["w1", "w9"]), ["c1", "w1", "w9"]);
});

test("Tab order: a worker that is blocked or not pinned is no tab, and the others keep the block's order; one shown agent is one tab", () => {
  const s = freeze(sess("s", [T("c1", "s", { createdMs: 1 }), worker("w1", "s", "c1", { createdMs: 2, state: "blocked" }), worker("w2", "s", "c1", { createdMs: 3 }), worker("w3", "s", "c1", { createdMs: 4 })]));
  assert.deepEqual(rowOrder(s, ["w3"]), ["c1", "w1", "w2", "w3"], "fixture: the block shows every worker, the blocked one among them");
  assert.deepEqual(tabIds(s, ["w3"]), ["c1", "w3"], "the blocked worker is a row and not a tab");
  assert.deepEqual(tabIds(s, []), ["c1"], "one shown agent: its one tab");
});

test("Tab order: the bar follows the block's tree in a session of more than 12 agents too: a pinned worker hangs under its parent (design D42 C)", () => {
  const s = freeze(sess("s", [
    T("c1", "s", { createdMs: 1 }), worker("h", "s", "c1", { createdMs: 5 }), worker("p", "s", "h", { createdMs: 9 }), worker("q", "s", "c1", { createdMs: 7 }), ...fill(10),
  ]));
  const own = (ids2) => ids2.filter((id) => !id.startsWith("f"));
  assert.deepEqual(own(rowOrder(s, ["p", "q"])), ["c1", "h", "p", "q"], "p hangs under its parent h, which was created before q");
  assert.deepEqual(tabIds(s, ["p", "q"]), ["c1", "p", "q"]);
  assert.deepEqual(tabIds(s, ["q", "p"]), ["c1", "p", "q"], "the order of the pins changes nothing");
});

// ---- the main agent's crown (owner review, task 27.15) ----
// The main agent of a session is the one the Sessions row speaks for: its first talkable agent (`session.talkable[0]`, which sessionLine
// reads), the conductor or coordinator the user talks to. Its Agents row and its top-bar tab carry a small crown; no other agent does,
// a second talkable agent (a coordinator an allow-list made talkable) and every worker included. The Sessions row's letter badge is
// unchanged.

test("Crown mark: a filled drawing of the window's own in the side mod's folder, in the accent colour", () => {
  assert.deepEqual(A.CROWN_MARK, { icon: "mods/cao-window-side/crown.svg", tone: "accent" });
  const svg = pinSvg("crown.svg");
  assert.match(svg, /^<svg [^>]*viewBox="0 0 24 24"/, "the 24 unit grid of Herdr's icons");
  assert.match(svg, /fill="currentColor"/, "svg() colours it with the token the row passes");
  assert.doesNotMatch(svg, /fill="none"/);
  assert.doesNotMatch(svg, /<(script|image|use|style|text)\b|\bhref=|#[0-9a-fA-F]{3,8}\b|rgba?\(/, "a path and the current colour only");
  assert.ok(svg.length < 600, `a small drawing (${svg.length} bytes)`);
  const points = pinPoints(svg);
  assert.ok(points.length >= 9, `${points.length} points`);
  assert.ok(points.flat().every((v) => v >= 0 && v <= 24), "inside the grid");
  const top = Math.min(...points.map((p) => p[1]));
  assert.deepEqual(points.filter((p) => p[1] === top), [[12, top]], "the middle peak is the tallest, on the centre line");
  assert.ok(points.filter((p) => p[1] <= 9).length >= 3, "three peaks");
  assert.ok(Math.max(...points.map((p) => p[0])) - Math.min(...points.map((p) => p[0])) >= 16, "wider than tall: a crown and not a spike");
});

test("Main agent: the first talkable agent is main; a second talkable agent, a worker and a pinned worker are not", () => {
  const s = freeze(sess("s", [T("c1", "s", { createdMs: 1 }), coord("tc", "s", "c1", { createdMs: 2 }), worker("w1", "s", "c1", { createdMs: 3 })], TABS_PROPS));
  assert.deepEqual(s.talkable.map((t) => t.id), ["c1", "tc"], "fixture: two talkable agents");
  const rows = A.agentRows(s, ["w1"]).rows;
  assert.deepEqual(rows.map((r) => [r.id, r.main]), [["c1", true], ["tc", false], ["w1", false]]);
  assert.equal(rows.find((r) => r.main).id, s.talkable[0].id, "the one the Sessions row speaks for");
  assert.deepEqual(A.tabViews(s, ["w1"]).map((t) => [t.id, t.main]), [["c1", true], ["tc", false], ["w1", false]]);
  assert.deepEqual(A.agentRows(s, []).rows.map((r) => r.main), [true, false, false], "one crown, on the first talkable agent, whatever the rows");
});

test("Main agent: the talkable agent is main whatever its caller (the only one an allow-list made talkable), and a hidden root is not", () => {
  const only = freeze(sess("s", [coord("tc", "s", "ghost", { createdMs: 2 }), worker("w1", "s", "tc", { createdMs: 3 })], TABS_PROPS));
  assert.deepEqual(only.talkable.map((t) => t.id), ["tc"], "fixture: the coordinator is the only talkable agent, and it has a caller");
  assert.deepEqual(A.agentRows(only, []).rows.map((r) => [r.id, r.main]), [["tc", true], ["w1", false]]);
  const hidden = freeze(sess("s", [T("m1", "s", { profile: "memory_manager", createdMs: 1 }), T("c1", "s", { createdMs: 2 })]));
  assert.deepEqual(hidden.talkable.map((t) => t.id), ["c1"], "fixture: a hidden root profile is no talkable agent");
  assert.deepEqual(A.agentRows(hidden, []).rows.map((r) => [r.id, r.main]), [["m1", false], ["c1", true]]);
});

test("Main agent: a session with no talkable agent has no crown, and the guessed root of a session with none still wears it beside its label", () => {
  const w = worker("w1", "s", "c1");
  const none = freeze({ name: "s", terms: [w], talkable: [], workers: [w], bestGuess: false });
  assert.deepEqual(A.agentRows(none, ["w1"]).rows.map((r) => r.main), [false]);
  assert.deepEqual(A.agentRows(freeze({ name: "s", terms: [], talkable: [], workers: [] }), []).rows, []);
  const guessed = freeze(sess("s", [worker("a", "s", "x", { createdMs: 1 }), worker("b", "s", "x", { createdMs: 2 })]));
  assert.equal(guessed.bestGuess, true, "fixture: every terminal has a caller, so the oldest is guessed to be the root");
  const [a, b] = A.agentRows(guessed, []).rows;
  assert.deepEqual([a.id, a.main, a.label], ["a", true, HONESTY.bestGuess], "the guess keeps its honesty label");
  assert.equal(b.main, false);
});

// ---- 28.5: an agent that waits on its workers (spec cao-window-sessions "An agent that waits on its workers shows as waiting"; design D38) ----
//
// The state comes from the `waiting` count termOf reads from the cao.waiting label. The mark is Herdr's working spinner in the muted
// colour (`spinner-dim`), the words are `waiting on N workers` in the tooltip only, and a waiting agent has not replied.

const WAITING_MARK = "spinner-dim";
const conductorWaiting = (n, over = {}) => sess("cao-a", [T("c1", "cao-a", { state: "idle", turn: 4, seen: 3, waiting: n, ...over }), worker("w1", "cao-a", "c1", { state: "working" })]);
const repliedOf = (t) => isReplied(t, {});

test("A session whose conductor waits shows the waiting mark and the words in its tooltip, and no extra mark", () => {
  for (const [n, words] of [[1, "waiting on 1 worker"], [2, "waiting on 2 workers"], [999, "waiting on 999 workers"]]) {
    const line = A.sessionLine(freeze(conductorWaiting(n)), { replied: repliedOf });
    assert.equal(line.shape, WAITING_MARK, `${n}`);
    assert.equal(line.tip, `cao-a: ${words}`, `${n}`);
    assert.equal(line.extra, null, "a waiting agent has not replied");
  }
});
test("done and idle both wait; a waiting done conductor that would be replied shows no replied mark, even if a predicate says replied", () => {
  const done = freeze(sess("cao-a", [T("c1", "cao-a", { state: "done", turn: 4, seen: 3, waiting: 2 })]));
  for (const replied of [repliedOf, () => true]) {
    const line = A.sessionLine(done, { replied });
    assert.deepEqual([line.shape, line.extra, line.tip], [WAITING_MARK, null, "cao-a: waiting on 2 workers"]);
  }
});
test("The agent row of a waiting agent carries the waiting mark and its tab none, and the words are only in the tooltips", () => {
  const s = freeze(conductorWaiting(2));
  const rows = A.agentRows(s, ["w1"], { now: 10 * MIN, replied: repliedOf }).rows;
  const c = rows.find((r) => r.id === "c1");
  assert.equal(c.shape, WAITING_MARK);
  assert.match(c.tip, /^conductor: waiting on 2 workers\b/);
  assert.doesNotMatch(c.tip, /seen:/, "a waiting agent has no seen state to show");
  const { tip, term, ...drawn } = c;
  assert.doesNotMatch(JSON.stringify(drawn), /waiting/i, "no state word in the drawn fields");
  assert.equal(rows.find((r) => r.id === "w1").shape, "spinner", "the working worker is unchanged");
  const tabs = A.tabViews(s, ["w1"], { replied: repliedOf });
  assert.deepEqual(tabs.map((t) => [t.id, "shape" in t]), [["c1", false], ["w1", false]], "the top-bar tab draws no mark (task 27.21)");
  assert.match(tabs[0].tip, /^conductor: waiting on 2 workers\b/, "its tooltip says it waits");
});
test("The tooltip says how long it has waited as it does for the other states, and 1 worker in the singular", () => {
  const s = freeze(sess("cao-a", [T("c1", "cao-a", { state: "idle", waiting: 1, statusMs: 0 })]));
  assert.match(A.agentRows(s, [], { now: 5 * MIN }).rows[0].tip, /^conductor: waiting on 1 worker for 5 minutes, turn 0, talkable/);
});
test("A worker that itself waits on a sub-worker shows the waiting mark too", () => {
  const s = freeze(sess("cao-a", [T("c1", "cao-a", { state: "working" }), worker("w1", "cao-a", "c1", { state: "idle", waiting: 1 })]));
  const w = A.agentRows(s, ["w1"], { replied: repliedOf }).rows.find((r) => r.id === "w1");
  assert.deepEqual([w.shape, w.worker], [WAITING_MARK, true]);
  assert.match(w.tip, /^developer: waiting on 1 worker\b/);
});
test("Working, blocked and error keep their marks and words whatever the label says", () => {
  for (const [state, shape, word] of [["working", "spinner", "working"], ["blocked", "bang", "needs you"], ["error", "bang", "error"]]) {
    const s = freeze(sess("cao-a", [T("c1", "cao-a", { state, waiting: 2 })]));
    assert.equal(A.sessionLine(s, { replied: repliedOf }).shape, shape, state);
    assert.equal(A.sessionLine(s, { replied: repliedOf }).tip, `cao-a: ${word}`, state);
    assert.equal(A.agentRows(s, [], { replied: repliedOf }).rows[0].shape, shape, state);
  }
});
test("No label, a zero count or a count termOf rejected: the agent is drawn exactly as before", () => {
  for (const waiting of [undefined, 0]) {
    const idle = freeze(sess("cao-a", [T("c1", "cao-a", { state: "idle", waiting })]));
    assert.deepEqual([A.sessionLine(idle, { replied: repliedOf }).shape, A.sessionLine(idle, { replied: repliedOf }).tip], ["ring", "cao-a: idle"]);
    const replied = freeze(sess("cao-b", [T("c2", "cao-b", { state: "done", turn: 4, seen: 3, waiting })]));
    const line = A.sessionLine(replied, { replied: repliedOf });
    assert.deepEqual([line.shape, line.extra, line.tip], ["dot-hi", null, "cao-b: replied"]);
  }
});
test("The reply arrives: the count goes, the conductor works and then shows the highlighted done dot", () => {
  const line = (over) => A.sessionLine(freeze(sess("cao-a", [T("c1", "cao-a", { turn: 5, seen: 3, ...over })])), { replied: repliedOf });
  assert.equal(line({ state: "done", waiting: 1 }).shape, WAITING_MARK);
  assert.equal(line({ state: "working", waiting: 0 }).shape, "spinner");
  assert.equal(line({ state: "done", waiting: 0 }).shape, "dot-hi");
});
test("A worker that went idle without replying needs nothing new: it is an ordinary idle agent", () => {
  const s = freeze(sess("cao-a", [T("c1", "cao-a", { state: "done", turn: 5, seen: 3 }), worker("w1", "cao-a", "c1", { state: "idle" })]));
  const w = A.agentRows(s, ["w1"], { replied: repliedOf }).rows.find((r) => r.id === "w1");
  assert.equal(w.shape, "ring");
  assert.match(w.tip, /^developer: idle\b/);
  assert.deepEqual([A.sessionLine(s, { replied: repliedOf }).shape, A.sessionLine(s, { replied: repliedOf }).tip], ["dot-hi", "cao-a: replied"]);
});
test("The waiting mark is Herdr's working spinner in the muted token: the same file, size and spin, the colour dim, and no token or literal of our own", () => {
  const src = readFileSync(new URL("./ui.js", import.meta.url), "utf8");
  const body = src.slice(src.indexOf("export function shapeMark"), src.indexOf("// The dialog's lease key"));
  const working = body.match(/spinner: \(\) => (uiSvg\("mods\/workspace-chrome\/spinner\.svg"\)[^\n]*?\.text_color\(c\.working\)\.spin\(900\)),/);
  assert.ok(working, "fixture: the working spinner line");
  assert.ok(body.includes(`"spinner-dim": () => ${working[1].replace("c.working", "c.dim")},`), "the same drawing with c.dim");
  assert.equal(statusShape("waiting"), WAITING_MARK);
});

// ---- 31.1: the full tree by default (spec cao-window-sessions "Agents block"; design D41 A) ----
// The Agents block shows every agent of the selected session as an indented tree. Since design D42 C nothing folds: a session of more than
// 12 agents shows all of them too (D41 A had folded its deepest workers behind an "N workers" line), and the block scrolls.
// ROOTB is the owner's real tree (conductor-61d0 > supervisor-d6ac > five workers, socrates working), as an old server shows it.

const ROOTB = (over = {}) => freeze(sess("ROOTB", [
  T("c61", "ROOTB", { window: "conductor-61d0", createdMs: 1, state: "done", turn: 3 }),
  worker("s6d", "ROOTB", "c61", { window: "supervisor-d6ac", profile: "supervisor", createdMs: 2, state: "idle", turn: 1 }),
  worker("she", "ROOTB", "s6d", { window: "sherlock-1111", profile: "sherlock", createdMs: 3, state: "done" }),
  worker("apo", "ROOTB", "s6d", { window: "apollo-2222", profile: "apollo", createdMs: 4, state: "done" }),
  worker("tit", "ROOTB", "s6d", { window: "titan-3333", profile: "titan", createdMs: 5, state: "idle" }),
  worker("leo", "ROOTB", "s6d", { window: "leonardo-4444", profile: "leonardo", createdMs: 6, state: "done" }),
  worker("soc", "ROOTB", "s6d", { window: "socrates-5555", profile: "socrates", createdMs: 7, state: "working" }),
], over));

test("Full tree: the ROOTB session lists all seven agents at depths 0, 1, 2, 2, 2, 2, 2 with no pin and no fold line", () => {
  const view = A.agentRows(ROOTB(), []);
  assert.deepEqual(view.rows.map((r) => [r.name, r.depth, r.worker]), [
    ["conductor-61d0", 0, false], ["supervisor-d6ac", 1, true], ["sherlock-1111", 2, true], ["apollo-2222", 2, true],
    ["titan-3333", 2, true], ["leonardo-4444", 2, true], ["socrates-5555", 2, true],
  ]);
  assert.deepEqual(Object.keys(view), ["rows"], "no fold line");
  assert.deepEqual(view.rows.map((r) => r.main), [true, false, false, false, false, false, false], "one crown");
  assert.deepEqual(A.agentRows(ROOTB(), ["she", "soc"]).rows.map((r) => r.id), view.rows.map((r) => r.id), "a pin changes no row");
});

test("Full tree: 12, 13 and 41 agents all show, whatever the shape", () => {
  const flat = (n) => freeze(sess("s", [T("c1", "s", { createdMs: 1 }), ...Array.from({ length: n }, (_, i) => worker(`w${String(i).padStart(2, "0")}`, "s", "c1", { createdMs: 10 + i }))]));
  for (const n of [11, 12, 40]) {
    const view = A.agentRows(flat(n), []);
    assert.deepEqual([view.rows.length, "fold" in view], [n + 1, false], `${n + 1} agents`);
    assert.deepEqual(view.rows.map((r) => r.depth), [0, ...Array(n).fill(1)]);
  }
});

test("Full tree: 1 + 3 + 9 agents show all 13 in their three levels, and neither a pin nor a blocked worker changes which rows show", () => {
  const sups = ["a", "b", "c"].map((x, i) => worker(`s${x}`, "s", "c1", { createdMs: 2 + i }));
  const kids = ["a", "b", "c"].flatMap((x, i) => [1, 2, 3].map((n) => worker(`k${x}${n}`, "s", `s${x}`, { createdMs: 10 + i * 3 + n })));
  const s = freeze(sess("s", [T("c1", "s", { createdMs: 1 }), ...sups, ...kids]));
  assert.equal(s.terms.length, 13);
  const view = A.agentRows(s, []);
  assert.deepEqual(view.rows.map((r) => [r.id, r.depth]), [
    ["c1", 0], ["sa", 1], ["ka1", 2], ["ka2", 2], ["ka3", 2], ["sb", 1], ["kb1", 2], ["kb2", 2], ["kb3", 2], ["sc", 1], ["kc1", 2], ["kc2", 2], ["kc3", 2],
  ]);
  assert.deepEqual(A.agentRows(s, ["kb2"]).rows.map((r) => r.id), view.rows.map((r) => r.id), "a pin hides and moves nothing");
  const asking = freeze(sess("s", [T("c1", "s", { createdMs: 1 }), ...sups, ...kids.map((k) => (k.id === "kc3" ? { ...k, state: "blocked" } : k))]));
  assert.deepEqual(A.agentRows(asking, []).rows.map((r) => r.id), view.rows.map((r) => r.id), "a blocked worker is one row among the others");
});

test("Full tree: a chain of 12 workers shows every level at its real depth, and 13 talkable agents all show", () => {
  const chain = Array.from({ length: 12 }, (_, i) => worker(`d${String(i).padStart(2, "0")}`, "s", i === 0 ? "c1" : `d${String(i - 1).padStart(2, "0")}`, { createdMs: 2 + i }));
  const s = freeze(sess("s", [T("c1", "s", { createdMs: 1 }), ...chain]));
  assert.deepEqual(A.agentRows(s, []).rows.map((r) => [r.id, r.depth]), [["c1", 0], ...Array.from({ length: 12 }, (_, i) => [`d${String(i).padStart(2, "0")}`, i + 1])], "13 agents, the deepest at depth 12");
  const roots = freeze(sess("s", Array.from({ length: 13 }, (_, i) => T(`r${String(i).padStart(2, "0")}`, "s", { createdMs: 1 + i }))));
  assert.equal(roots.talkable.length, 13);
  assert.deepEqual(Object.keys(A.agentRows(roots, [])), ["rows"]);
  assert.equal(A.agentRows(roots, []).rows.length, 13);
});

test("Full tree: the top bar's tabs are the talkable agents and the pinned workers, in the tree's order, and an unpinned worker has no tab however the tree shows it", () => {
  assert.deepEqual(A.tabViews(ROOTB(), []).map((t) => t.name), ["conductor-61d0"]);
  assert.deepEqual(A.tabViews(ROOTB(), ["soc", "s6d"]).map((t) => t.name), ["conductor-61d0", "supervisor-d6ac", "socrates-5555"]);
  const sups = ["a", "b", "c"].map((x, i) => worker(`s${x}`, "s", "c1", { createdMs: 2 + i }));
  const kids = ["a", "b", "c"].flatMap((x, i) => [1, 2, 3].map((n) => worker(`k${x}${n}`, "s", `s${x}`, { createdMs: 10 + i * 3 + n })));
  const big = freeze(sess("s", [T("c1", "s", { createdMs: 1 }), ...sups, ...kids]));
  assert.deepEqual(A.tabViews(big, ["kb2", "ka1"]).map((t) => t.id), ["c1", "ka1", "kb2"], "a pinned worker of a large session is a tab, in the tree's order");
});

test("Full tree: a worker of a talkable-less session is still shown, the guessed root first", () => {
  const s = freeze(sess("s", [worker("w2", "s", "gone", { createdMs: 3, profile: "reviewer" }), worker("w1", "s", "gone", { createdMs: 2 })]));
  assert.deepEqual(A.agentRows(s, []).rows.map((r) => [r.id, r.worker, r.depth]), [["w1", false, 1], ["w2", true, 1]]);
});

// ---- 31.2: waiting derived from the tree, drawn (spec cao-window-sessions "An agent that waits on its workers shows as waiting"; design D41 B) ----
// The model gives each agent that waits on workers below it a `waiting` count, so the rows, the Sessions line, the tabs and the status bar
// draw it like a cao.waiting label: the muted spinner, "waiting on N workers", and no reply.

const rawRb = (id, ms, status, extra = {}) => ({
  address: `cao/${id}`, created_ms: ms, status, turn: extra.turn ?? 0,
  labels: { "cao.terminal_id": id, "cao.session": "ROOTB", "cao.window": extra.window ?? id, ...(extra.labels ?? {}) },
});
const rootbModel = (soc = "working") => buildModel([
  rawRb("c61", 1, "done", { window: "conductor-61d0", turn: 3, labels: { "herdr.seen": "2" } }),
  rawRb("s6d", 2, "idle", { window: "supervisor-d6ac" }),
  rawRb("she", 3, "done", { window: "sherlock-1111" }),
  rawRb("soc", 7, soc, { window: "socrates-5555" }),
], [], { machine: "m" }, { cache: {
  c61: { state: "ok", agent_profile: "conductor", caller_id: null }, s6d: { state: "ok", agent_profile: "supervisor", caller_id: "c61" },
  she: { state: "ok", agent_profile: "sherlock", caller_id: "s6d" }, soc: { state: "ok", agent_profile: "socrates", caller_id: "s6d" },
} });

test("Waiting from the tree: ROOTB's conductor and supervisor draw the muted spinner and 'waiting on N workers' while socrates works, with no server label", () => {
  const m = rootbModel();
  const session = m.sessions[0];
  const rows = A.agentRows(session, [], { replied: m.replied }).rows;
  assert.deepEqual(rows.map((r) => [r.name, r.shape]), [["conductor-61d0", "spinner-dim"], ["supervisor-d6ac", "spinner-dim"], ["sherlock-1111", "dot"], ["socrates-5555", "spinner"]]);
  assert.match(rows[0].tip, /^conductor-61d0: waiting on 2 workers, turn 3, talkable$/);
  assert.match(rows[1].tip, /^supervisor-d6ac: waiting on 1 worker, turn 0, worker$/);
  assert.doesNotMatch(rows[0].tip, /seen:/, "a waiting agent has no seen state");
  const line = A.sessionLine(session, { replied: m.replied });
  assert.deepEqual([line.shape, line.extra, line.tip], ["spinner-dim", null, "ROOTB: waiting on 2 workers"], "the Sessions row speaks for its first talkable agent, the conductor; it has not replied");
  const tabs = A.tabViews(session, ["soc"], { replied: m.replied });
  assert.match(tabs[0].tip, /^conductor-61d0: waiting on 2 workers\b/);
  assert.ok(tabs.every((t) => !("shape" in t)));
});

test("Waiting from the tree: when the last worker finishes the wait ends and the conductor's reply shows", () => {
  const m = rootbModel("done");
  const rows = A.agentRows(m.sessions[0], [], { replied: m.replied }).rows;
  assert.deepEqual(rows.map((r) => r.shape), ["dot-hi", "ring", "dot", "dot"]);
  assert.deepEqual([A.sessionLine(m.sessions[0], { replied: m.replied }).shape, A.sessionLine(m.sessions[0], { replied: m.replied }).tip], ["dot-hi", "ROOTB: replied"]);
});

// ---- 31.3: a lost answer does not draw the tree flat (spec cao-window-sessions "Agents block"; design D41 C) ----
// A terminal whose profile and caller are still unresolved after the lookups (a lost or failed GET /terminals/{id}) is not a root: it is drawn
// one step down with the muted words "parent not listed", unless it is the session's main agent, and what hangs on it stays under it.

test("Lost answers: with no answer for any ROOTB terminal the conductor is the one root and the rest are one step down with the note, not all at depth 0", () => {
  const bare = [rawRb("c61", 1, "done", { window: "conductor-61d0" }), rawRb("s6d", 2, "idle", { window: "supervisor-d6ac" }), rawRb("she", 3, "done", { window: "sherlock-1111" }), rawRb("soc", 7, "working", { window: "socrates-5555" })];
  const m = buildModel(bare, [], { machine: "m" }, { cache: {} });
  const rows = A.agentRows(m.sessions[0], []).rows;
  assert.deepEqual(rows.map((r) => [r.name, r.depth, r.parentNote, r.main]), [
    ["conductor-61d0", 0, null, true], ["supervisor-d6ac", 1, "parent not listed", false], ["sherlock-1111", 1, "parent not listed", false], ["socrates-5555", 1, "parent not listed", false],
  ]);
});
test("Lost answers: a terminal that was answered keeps its place, and one that was not is one step down while its answered children stay under it", () => {
  const bare = [rawRb("c61", 1, "done"), rawRb("s6d", 2, "idle"), rawRb("she", 3, "done"), rawRb("soc", 7, "working")];
  const cache = { c61: { state: "ok", agent_profile: "conductor", caller_id: null }, she: { state: "ok", agent_profile: "sherlock", caller_id: "s6d" }, soc: { state: "failed", at: 1 } };
  const m = buildModel(bare, [], { machine: "m" }, { cache });
  assert.deepEqual(A.agentRows(m.sessions[0], []).rows.map((r) => [r.id, r.depth, r.parentNote]), [
    ["c61", 0, null], ["s6d", 1, "parent not listed"], ["she", 2, null], ["soc", 1, "parent not listed"],
  ]);
  const answered = buildModel(bare, [], { machine: "m" }, { cache: { ...cache, s6d: { state: "ok", agent_profile: "supervisor", caller_id: "c61" }, soc: { state: "ok", agent_profile: "socrates", caller_id: "s6d" } } });
  assert.deepEqual(A.agentRows(answered.sessions[0], []).rows.map((r) => [r.id, r.depth, r.parentNote]), [["c61", 0, null], ["s6d", 1, null], ["she", 2, null], ["soc", 2, null]], "the answer arrives: the tree is whole");
});

// ---- 31.4: the crown moves to the pin's slot on the Agents row (spec cao-window-sessions "Agents block"; design D41 D) ----
// The right-hand slot of a row (the pin's 18 px) holds the pin of a worker, the crown of the session's main agent, or nothing (another
// talkable agent), so the status marks stay in one column and the name keeps the room. The top-bar tab keeps its crown after the name.

test("Crown slot: a worker row's slot is its pin, the main agent's row's is the crown, and any other talkable agent's is blank", () => {
  const s = freeze(sess("s", [T("c1", "s", { createdMs: 1 }), coord("tc", "s", "c1", { createdMs: 2 }), worker("w1", "s", "c1", { createdMs: 3 })], TABS_PROPS));
  const rows = A.agentRows(s, ["w1"]).rows;
  assert.deepEqual(rows.map((r) => [r.id, r.slot, r.main, r.pinTip]), [["c1", "crown", true, null], ["tc", "blank", false, null], ["w1", "pin", false, "pinned"]]);
  assert.deepEqual(A.agentRows(ROOTB(), []).rows.map((r) => r.slot), ["crown", "pin", "pin", "pin", "pin", "pin", "pin"]);
  const none = freeze({ name: "s", terms: [worker("w1", "s", "c1")], talkable: [], workers: [worker("w1", "s", "c1")], bestGuess: false });
  assert.deepEqual(A.agentRows(none, []).rows.map((r) => r.slot), ["pin"], "a session with no talkable agent crowns none");
  const guessed = freeze(sess("s", [worker("a", "s", "x", { createdMs: 1 }), worker("b", "s", "x", { createdMs: 2 })]));
  assert.deepEqual(A.agentRows(guessed, []).rows.map((r) => [r.id, r.slot]), [["a", "crown"], ["b", "pin"]], "the guessed root wears it beside its label");
});
test("Crown slot: one crown at most per session, a tab still says main, and the tab view carries no slot", () => {
  const s = freeze(sess("s", [T("c1", "s", { createdMs: 1 }), coord("tc", "s", "c1", { createdMs: 2 }), worker("w1", "s", "c1", { createdMs: 3 })], TABS_PROPS));
  assert.equal(A.agentRows(s, ["w1"]).rows.filter((r) => r.slot === "crown").length, 1);
  assert.deepEqual(A.tabViews(s, ["w1"]).map((t) => [t.id, t.main]), [["c1", true], ["tc", false], ["w1", false]]);
  for (const t of A.tabViews(s, ["w1"])) assert.ok(!("slot" in t), "the bar keeps the crown after the name, as before");
});

// ---- 32.2: the goal text, the agent's recap, readable: wrapped up to three lines (design D42 B) ----
// The toolkit has no line clamp (no `line_clamp`, `max_lines` or multi-line `text_ellipsis` in gpui-kit.d.ts: `truncate()` is one line), so the
// clamp is a pure function: a character budget per line from the block's width (`goalChars`), a greedy word wrap into that budget, at most
// three lines, and an ellipsis at the end of the third when words are left over (`goalLines`). The view draws each line on its own.

test("Goal budget: characters per line from the block's width, 7 px a character after 40 px of padding and scrollbar, never under 8", () => {
  assert.deepEqual([268, 200, 160, 1000, 47, 40, 0, -5].map((w) => A.goalChars(w)), [32, 22, 17, 137, 8, 8, 8, 8]);
  assert.deepEqual([undefined, null, NaN, Infinity, "268", {}].map((w) => A.goalChars(w)), [32, 32, 32, 32, 32, 32], "an unknown width is the sidebar's default 268 px");
});

test("Goal lines: three lines at 32 characters with the ellipsis after the third, the words not cut", () => {
  const goal = "We're getting the first draft of the migration plan reviewed and then the workers start on the schema changes while socrates reviews the tests";
  assert.deepEqual(A.goalLines(goal, 268), ["We're getting the first draft of", "the migration plan reviewed and", "then the workers start on the\u2026"]);
  assert.deepEqual(A.goalLines(goal, 200), ["We're getting the", "first draft of the", "migration plan\u2026"], "a narrower block gives other lines at 22 characters");
});

test("Goal lines: exact clamp values at a budget of 10 and 9 characters", () => {
  const at = (text, perLine) => A.goalLines(text, 40 + 7 * perLine).slice();   // the width that gives `perLine`
  assert.equal(A.goalChars(40 + 7 * 10), 10);
  assert.deepEqual(at("aaaa bbbb cccc dddd eeee ffff gggg", 10), ["aaaa bbbb", "cccc dddd", "eeee ffff\u2026"], "a fourth line is cut; the ellipsis fits after the third");
  assert.deepEqual(at("aaaa bbbb cccc dddd eeee ffff", 10), ["aaaa bbbb", "cccc dddd", "eeee ffff"], "exactly three lines: no ellipsis");
  assert.deepEqual(at("aaaa bbbb cccc dddd eeee ffff gggg", 9).length, 3);
  assert.equal(A.goalChars(40 + 7 * 9), 9);
  assert.deepEqual(at("aaaa bbbb cccc dddd eeee ffff gggg", 9), ["aaaa bbbb", "cccc dddd", "eeee fff\u2026"], "a full third line gives up its last character for the ellipsis");
});

test("Goal lines: a word longer than a line is broken at the budget, a short text is one line, blanks fold", () => {
  assert.deepEqual(A.goalLines("abcdefghijkl", 40 + 7 * 8), ["abcdefgh", "ijkl"], "a long word breaks at 8");
  assert.deepEqual(A.goalLines("x".repeat(100), 40 + 7 * 10), ["xxxxxxxxxx", "xxxxxxxxxx", "xxxxxxxxx\u2026"], "a word of 100 characters: three lines and the ellipsis");
  assert.deepEqual(A.goalLines("Tests pass; waiting for review.", 268), ["Tests pass; waiting for review."]);
  assert.deepEqual(A.goalLines("  a \n b\t\tc  ", 268), ["a b c"]);
});

test("Goal lines: nothing to show is no line, and the text is not changed", () => {
  for (const v of ["", "   ", "\n", undefined, null, 5, {}, []]) assert.deepEqual(A.goalLines(v, 268), [], JSON.stringify(v));
  assert.equal(A.goalLines("a b", 268).length, 1);
});

test("Goal lines: never more than three lines and no line over the budget, whatever the text", () => {
  const words = ["a", "bb", "ccc", "dddd", "eeeee", "ffffff", "ggggggg", "x".repeat(40)];
  for (let n = 1; n <= 60; n += 7) {
    const text = Array.from({ length: n }, (_, i) => words[(i * 5) % words.length]).join(" ");
    for (const width of [160, 200, 268, 400]) {
      const lines = A.goalLines(text, width);
      assert.ok(lines.length >= 1 && lines.length <= 3, `${n} words at ${width}: ${lines.length} lines`);
      for (const l of lines) assert.ok([...l].length <= A.goalChars(width), `a line of ${[...l].length} characters at ${width}`);
    }
  }
});

await finish();
