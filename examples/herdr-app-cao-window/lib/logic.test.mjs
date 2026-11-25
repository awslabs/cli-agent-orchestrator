// lib/logic.js: the window's pure rules for sessions, pins, seen state, status counts, failure states and
// the hand-off toast.
//
// The module is imported as a namespace so a function that is not written yet fails its own case with
// "x is not a function" instead of stopping the whole file at the import line.
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { assert, finish, test } from "../test/harness.mjs";
import { auditHonesty } from "../test/honesty-audit.mjs";
import { HONESTY, HONESTY_PENDING, HONESTY_SOURCES } from "./honesty.js";
import * as L from "./logic.js";

const {
  applyOutcome, ageWords, askingKind, barText, buildModel, caoTerms, classify, contextPercent, displayState,
  fallbackPlan, fallbackResult, filterSessions, flowOfSession, headerNote, initialObs,
  initialToast, isReplied, knownGapNote, markPending, navStep, nextFilter, observe, parseRecap, prunePins,
  quietHint, rememberFinishedSeen, rememberSeen, sectionSummary, seenState, seenVerify, seenWrite, sessionsFrom, sortSessions,
  statusCounts, statusShape, tabsFor, termOf, toastStep, withFallback, writesDisabled,
  holdModel, sharedCache, rowShowsReplied, repaintDue, readOfThisTurn, shownBlock,
  dialogKey, unchosenProfileText,
} = L;

const DOT = "\u00b7"; // the middle dot of the product copy, kept as an escape so this file stays ASCII

// A normalized terminal, as termOf produces it. Every function below sees only these fields.
const T = (id, session, over = {}) => ({
  address: `cao/${id}`, id, session, window: id, profile: "conductor", caller: undefined, state: "idle",
  kind: undefined, msg: undefined, statusMs: 0, turn: 0, seen: undefined, createdMs: 1, ...over,
});
const worker = (id, session, caller = "c1", over = {}) => T(id, session, { profile: "developer", caller, createdMs: 5, ...over });
const never = () => false;
const nothing = (v) => v === null || v === undefined; // "returns nothing" is null or undefined; the spec does not say which

// Deep freeze, so a function that mutates its argument throws in strict mode (ES modules are strict).
const freeze = (v) => {
  if (v && typeof v === "object" && !Object.isFrozen(v)) { Object.freeze(v); for (const k of Object.keys(v)) freeze(v[k]); }
  return v;
};
const sess = (name, terms, over = {}) => ({ name, terms, ...classify(terms, {}, []), ...over });

// ---- the terminal adapter (the shape of terminals() is assumed here, not documented) ----
test("termOf reads labels in object form and in array form", () => {
  const a = termOf({ address: "cao/t1", labels: { "cao.terminal_id": "t1", "cao.session": "cao-a", "cao.profile": "conductor", "herdr.seen": "3" }, status: "idle", turn: 4, created_ms: 9, status_ms: 7 });
  assert.deepEqual([a.id, a.session, a.profile, a.caller, a.seen, a.turn, a.createdMs, a.statusMs], ["t1", "cao-a", "conductor", undefined, 3, 4, 9, 7]);
  const b = termOf({ address: "cao/t2", labels: ["cao.terminal_id=t2", "cao.session=cao-a", "cao.caller=t1"], status: "working" });
  assert.deepEqual([b.id, b.caller, b.profile, b.state], ["t2", "t1", undefined, "working"]);
});
test("termOf reads a status object, and report, for state, kind and msg; no status is unknown", () => {
  const a = termOf({ address: "x", labels: {}, status: { state: "blocked", kind: "permission", msg: "Run ls?" } });
  assert.deepEqual([a.state, a.kind, a.msg], ["blocked", "permission", "Run ls?"]);
  const b = termOf({ address: "x", labels: {}, status: "done", report: { msg: "Writing a file" } });
  assert.deepEqual([b.state, b.msg], ["done", "Writing a file"]);
  assert.equal(termOf({ address: "x", labels: {} }).state, "unknown");
});
test("termOf: a herdr.seen label that is not a number is no seen value, and a missing turn is 0", () => {
  for (const bad of ["", "abc", "NaN", "  "]) assert.equal(termOf({ address: "x", labels: { "herdr.seen": bad } }).seen, undefined, JSON.stringify(bad));
  assert.equal(termOf({ address: "x", labels: { "herdr.seen": "0" } }).seen, 0);
  assert.equal(termOf({ address: "x", labels: {} }).turn, 0);
});
test("termOf carries the program Herdr resolved for the terminal (its icon and whether it is tinted), and none when Herdr gave none", () => {
  const claude = { key: "claude", name: "Claude Code", icon: "icons/claude.svg", tint: false, tile: null, color: null, shell: false, agent: true };
  assert.deepEqual(termOf({ address: "x", labels: {}, program: claude }).program, { icon: "icons/claude.svg", tint: false });
  assert.deepEqual(termOf({ address: "x", labels: {}, program: { icon: "icons/agent.svg", tint: true } }).program, { icon: "icons/agent.svg", tint: true });
  assert.deepEqual(termOf({ address: "x", labels: {}, program: { icon: null, tint: false } }).program, { icon: undefined, tint: false }, "a program with no icon says so");
  for (const none of [undefined, null, "claude", 5, [], true]) assert.equal(termOf({ address: "x", labels: {}, program: none }).program, undefined, `${JSON.stringify(none)} is no program`);
  assert.equal(termOf({ address: "x", labels: {} }).program, undefined);
});
test("termOf and caoTerms survive junk records", () => {
  for (const junk of [null, undefined, 7, "x", [], {}, { labels: null }, { labels: 5, status: 5 }]) assert.doesNotThrow(() => termOf(junk));
  assert.deepEqual(caoTerms(null), []);
  assert.deepEqual(caoTerms("nope"), []);
});
test("caoTerms keeps only terminals with a CAO session and terminal id", () => {
  const raw = [{ address: "a", labels: { "cao.session": "s", "cao.terminal_id": "t1" } }, { address: "b", labels: {} }, { address: "c", labels: { "cao.session": "s" } }];
  assert.deepEqual(caoTerms(raw).map((t) => t.id), ["t1"]);
});

// ---- classify: talkable agents and workers ----
test("Conductor with workers: the root is talkable, three workers fold into one line", () => {
  const s = classify([T("c1", "s"), worker("w1", "s"), worker("w2", "s"), worker("w3", "s")], {}, []);
  assert.deepEqual(s.talkable.map((t) => t.id), ["c1"]);
  assert.deepEqual(s.workers.map((t) => t.id), ["w1", "w2", "w3"]);
  assert.equal(s.label, null, "a session with a talkable root carries no best-guess label");
});
test("Allowlisted child is talkable, and a child outside the allowlist is a worker", () => {
  const terms = [T("c1", "s"), T("tc", "s", { profile: "thread-coordinator", caller: "c1" }), worker("w1", "s")];
  const s = classify(terms, { talkableProfiles: ["thread-coordinator"] }, []);
  assert.deepEqual(s.talkable.map((t) => t.id), ["c1", "tc"]);
  assert.deepEqual(s.workers.map((t) => t.id), ["w1"]);
});
test("Memory sidecar hidden: the conductor is talkable, the root memory_manager counts as a worker", () => {
  const s = classify([T("c1", "s"), T("m1", "s", { profile: "memory_manager" })], {}, []);
  assert.deepEqual([s.talkable.map((t) => t.id), s.workers.map((t) => t.id)], [["c1"], ["m1"]]);
});
test("hiddenRootProfiles replaces the default, and an allowlisted hidden profile is talkable again", () => {
  const terms = [T("c1", "s", { profile: "conductor" }), T("m1", "s", { profile: "memory_manager" })];
  const custom = classify(terms, { hiddenRootProfiles: ["conductor"] }, []);
  assert.deepEqual(custom.talkable.map((t) => t.id), ["m1"], "the default memory_manager is no longer hidden");
  const both = classify(terms, { talkableProfiles: ["memory_manager"] }, []);
  assert.deepEqual(both.talkable.map((t) => t.id), ["c1", "m1"], "the allowlist wins over the hidden list");
});
test("No talkable agent left: the oldest survivor is shown, labelled best guess", () => {
  const s = classify([worker("w2", "s", "gone", { createdMs: 9 }), worker("w1", "s", "gone", { createdMs: 3 })], {}, []);
  assert.deepEqual(s.talkable.map((t) => t.id), ["w1"]);
  assert.deepEqual(s.workers.map((t) => t.id), ["w2"]);
  assert.equal(s.label, HONESTY.bestGuess);
});
test("a session of only a hidden root shows that root as the best guess", () => {
  const s = classify([T("m1", "s", { profile: "memory_manager" })], {}, []);
  assert.deepEqual(s.talkable.map((t) => t.id), ["m1"]);
  assert.equal(s.label, HONESTY.bestGuess);
});
test("Workflow step session: one developer root is an ordinary talkable agent with no label and no draft mark", () => {
  const s = classify([T("a1", "cao-1a2b3c4d", { profile: "developer" })], {}, []);
  assert.deepEqual([s.talkable.length, s.draft, s.label], [1, false, null]);
});
test("Draft author session: only a recorded terminal id marks it", () => {
  const author = [T("d1", "cao-draft-flow-0a1b2c3d", { profile: "author" })];
  assert.equal(classify(author, {}, ["d1"]).draft, true);
  assert.equal(classify(author, {}, []).draft, false, "no record: an ordinary session, the safe direction");
  assert.equal(classify(author, {}, undefined).draft, false, "drafts omitted");
});
test("Operator session named draft: the name alone never marks a draft", () => {
  assert.equal(classify([T("c9", "cao-draft-x")], {}, []).draft, false);
  assert.equal(classify([T("c9", "cao-draft-flow-0a1b2c3d")], {}, ["someone-else"]).draft, false);
});
test("a terminal with no profile label is not guessed at: the session shows exactly one terminal until the fallback answers", () => {
  const s = classify([T("t1", "s", { profile: undefined, createdMs: 1 }), T("t2", "s", { profile: undefined, createdMs: 2 })], {}, []);
  assert.deepEqual(s.talkable.map((t) => t.id), ["t1"], "its first terminal");
  assert.equal(s.workers.length, 1);
});
test("classification uses the cached profile values once the fallback resolved a terminal", () => {
  const terms = [T("t1", "s", { profile: undefined }), T("t2", "s", { profile: undefined, createdMs: 2 })];
  const cache = {
    t1: { state: "ok", agent_profile: "conductor", caller_id: null },
    t2: { state: "ok", agent_profile: "developer", caller_id: "t1" },
  };
  const s = classify(withFallback(terms, cache), {}, []);
  assert.deepEqual([s.talkable.map((t) => t.id), s.workers.map((t) => t.id)], [["t1"], ["t2"]]);
});
test("classify: empty session and a duplicate terminal id do not throw", () => {
  assert.doesNotThrow(() => classify([], {}, []));
  assert.deepEqual(classify([], {}, []).talkable, []);
  assert.doesNotThrow(() => classify([T("a", "s"), T("a", "s")], {}, []));
  assert.doesNotThrow(() => classify([T("a", "s")], undefined, undefined));
});
test("classify: a large session classifies in well under a second", () => {
  const terms = [T("c1", "s")];
  for (let i = 0; i < 5000; i++) terms.push(worker(`w${i}`, "s"));
  const t0 = Date.now();
  const s = classify(terms, {}, []);
  assert.equal(s.workers.length, 5000);
  assert.ok(Date.now() - t0 < 1000);
});
test("classify does not mutate its arguments", () => {
  const terms = freeze([T("c1", "s"), worker("w1", "s")]);
  assert.doesNotThrow(() => classify(terms, freeze({ talkableProfiles: ["x"] }), freeze(["d"])));
});

// ---- sessions, order, fold ----
test("sessionsFrom groups by session in first-seen order and ignores nothing", () => {
  const out = sessionsFrom([T("a", "s2"), T("b", "s1"), T("c", "s2")]);
  assert.deepEqual(out.map((s) => [s.name, s.terms.map((t) => t.id)]), [["s2", ["a", "c"]], ["s1", ["b"]]]);
  assert.deepEqual(sessionsFrom([]), []);
});
test("sessionsFrom keeps unicode session names apart, and groups equal ones", () => {
  const composed = "cao-caf\u00e9"; const decomposed = "cao-cafe\u0301"; const han = "cao-\u65e5\u672c";
  const out = sessionsFrom([T("a", composed), T("b", composed), T("c", decomposed), T("d", han)]);
  assert.deepEqual(out.map((s) => s.terms.length), [2, 1, 1], "CAO compares names as given, so the two spellings are two sessions");
});
// ---- agent tabs ----
test("One shown agent: one tab, the conductor's", () => {
  const s = classify([T("c1", "s"), worker("w1", "s")], {}, []);
  assert.deepEqual(tabsFor(s, []).map((t) => t.id), ["c1"]);
});
test("No shown agent: no tab", () => {
  assert.deepEqual(tabsFor({ talkable: [], workers: [] }, []), []);
  assert.deepEqual(tabsFor({ talkable: [], workers: [worker("w1", "s")] }, []), [], "an unpinned worker is not shown");
});
test("Two shown agents: a conductor and a pinned worker give two tabs, in that order", () => {
  const s = classify([T("c1", "s"), worker("w1", "s"), worker("w2", "s", "c1", { state: "blocked" })], {}, []);
  assert.deepEqual(tabsFor(s, ["w1"]).map((t) => t.id), ["c1", "w1"]);
  assert.deepEqual(tabsFor(s, []).map((t) => t.id), ["c1"], "a blocked but unpinned worker is not a tab");
});
test("tabsFor: two talkable agents are two tabs; a pin of an unknown or talkable id adds none", () => {
  const two = classify([T("c1", "s"), T("tc", "s", { profile: "thread-coordinator", caller: "c1" })], { talkableProfiles: ["thread-coordinator"] }, []);
  assert.deepEqual(tabsFor(two, []).map((t) => t.id), ["c1", "tc"]);
  const one = classify([T("c1", "s")], {}, []);
  assert.deepEqual(tabsFor(one, ["c1", "ghost"]).map((t) => t.id), ["c1"]);
  assert.deepEqual(tabsFor(classify([T("c1", "s"), worker("w1", "s")], {}, []), ["w1", "w1"]).map((t) => t.id), ["c1", "w1"], "a duplicate pin is one tab");
});

// ---- pins ----
test("Pin survives a reconnect: an empty, missing or disconnected snapshot never prunes", () => {
  for (const snap of [[], null, undefined]) assert.deepEqual(prunePins(["w1"], snap), { pins: ["w1"], changed: false });
});
test("Pin of a deleted worker: a non-empty snapshot without its id removes only that pin", () => {
  assert.deepEqual(prunePins(["w1", "w2"], [T("c1", "s"), T("w2", "s")]), { pins: ["w2"], changed: true });
  assert.deepEqual(prunePins(["w2"], [T("c1", "s"), T("w2", "s")]), { pins: ["w2"], changed: false });
});
test("prunePins: a snapshot that lists terminals but none of the pins removes every pin", () => {
  assert.deepEqual(prunePins(["a", "b"], [T("c1", "s")]), { pins: [], changed: true });
});
test("prunePins: no pins, and a non-array snapshot, change nothing and do not throw", () => {
  assert.deepEqual(prunePins([], [T("c1", "s")]), { pins: [], changed: false });
  assert.deepEqual(prunePins(["w1"], { not: "a list" }), { pins: ["w1"], changed: false });
  assert.doesNotThrow(() => prunePins(undefined, [T("c1", "s")]));
});
test("prunePins does not mutate its arguments and scales to ten thousand pins", () => {
  const pins = []; const snap = [];
  for (let i = 0; i < 10000; i++) { pins.push(`p${i}`); if (i % 2 === 0) snap.push(T(`p${i}`, "s")); }
  const t0 = Date.now();
  const r = prunePins(freeze(pins), freeze(snap));
  assert.equal(r.pins.length, 5000);
  assert.ok(Date.now() - t0 < 1000);
});

// ---- quiet worker hint ----
const MIN = 60_000;
const quietSession = (state = "working") => ({ workers: [worker("w1", "s", "c1", { state })] });
test("Quiet worker: 'stuck?' with the label heuristic once a working worker has been quiet for quietMinutes", () => {
  const obs = { subscribedAt: 0, at: {} };
  assert.ok(nothing(quietHint(quietSession(), obs, 10, 10 * MIN - 1)), "just under ten minutes");
  const hint = quietHint(quietSession(), obs, 10, 11 * MIN);
  assert.deepEqual([hint.text, hint.label, hint.terminalId], ["stuck?", HONESTY.heuristic, "w1"]);
});
test("quietHint counts from the last event the window observed, not from the first sight", () => {
  const obs = { subscribedAt: 0, at: { w1: 20 * MIN } };
  assert.ok(nothing(quietHint(quietSession(), obs, 10, 29 * MIN)));
  assert.ok(quietHint(quietSession(), obs, 10, 31 * MIN));
});
test("quietHint is never shown for an event that predates the window's subscription", () => {
  const obs = { subscribedAt: 100 * MIN, at: { w1: 1 * MIN } };
  assert.ok(nothing(quietHint(quietSession(), obs, 10, 105 * MIN)), "five minutes after subscribing");
  assert.ok(nothing(quietHint(quietSession(), { subscribedAt: 100 * MIN, at: {} }, 10, 105 * MIN)));
  assert.ok(quietHint(quietSession(), { subscribedAt: 100 * MIN, at: {} }, 10, 111 * MIN), "ten minutes after subscribing counts");
});
test("quietHint: only a working worker is quiet; idle, done, blocked and error workers are not", () => {
  const obs = { subscribedAt: 0, at: {} };
  for (const state of ["idle", "done", "blocked", "error", "weird"]) assert.ok(nothing(quietHint(quietSession(state), obs, 10, 99 * MIN)), state);
});
test("quietHint: a working talkable agent is not a quiet worker, and quietMinutes is honoured", () => {
  const obs = { subscribedAt: 0, at: {} };
  assert.ok(nothing(quietHint({ workers: [], talkable: [T("c1", "s", { state: "working" })] }, obs, 10, 99 * MIN)));
  assert.ok(quietHint(quietSession(), obs, 1, 2 * MIN));
  assert.ok(nothing(quietHint(quietSession(), obs, 30, 29 * MIN)));
});
test("quietHint survives clock skew, a session with no workers and a missing observation", () => {
  assert.ok(nothing(quietHint(quietSession(), { subscribedAt: 50 * MIN, at: {} }, 10, 0)), "now before the subscription");
  assert.ok(nothing(quietHint({ workers: [] }, { subscribedAt: 0, at: {} }, 10, 99 * MIN)));
  assert.doesNotThrow(() => quietHint(quietSession(), { subscribedAt: 0 }, 10, 99 * MIN));
});
test("quietHint names the first quiet worker among several", () => {
  const session = { workers: [worker("a", "s", "c1", { state: "working" }), worker("b", "s", "c1", { state: "working" })] };
  const hint = quietHint(session, { subscribedAt: 0, at: { a: 9 * MIN } }, 10, 11 * MIN);
  assert.equal(hint.terminalId, "b", "a was seen at 9 minutes, so only b has been quiet for 10");
});
test("observe records when a status changed, never the first snapshot, and feeds quietHint", () => {
  let o = initialObs(100);
  o = observe(o, [worker("w1", "s", "c1", { state: "working", msg: "a" })], 100);
  assert.deepEqual(o.at, {}, "an event that predates the subscription is not known");
  o = observe(o, [worker("w1", "s", "c1", { state: "working", msg: "b" })], 5_000);
  assert.equal(o.at.w1, 5_000);
  o = observe(o, [worker("w1", "s", "c1", { state: "working", msg: "b" })], 9_000);
  assert.equal(o.at.w1, 5_000, "an unchanged record keeps its time");
  const session = { workers: [worker("w1", "s", "c1", { state: "working" })] };
  assert.ok(nothing(quietHint(session, o, 10, 5_000 + 10 * MIN - 1)));
  assert.ok(quietHint(session, o, 10, 5_000 + 10 * MIN + 1));
});
test("observe does not mutate its arguments", () => {
  const o = freeze(initialObs(0));
  assert.doesNotThrow(() => observe(o, freeze([worker("w1", "s")]), 5));
});

// ---- section summaries (three peer sidebar sections) ----
test("Fold and restore: a folded Flows section shows '5, all off' and 'none yet' when empty", () => {
  assert.equal(sectionSummary("flows", [{ enabled: false }, { enabled: false }, { enabled: false }, { enabled: false }, { enabled: false }]), "5, all off");
  for (const kind of ["flows", "sessions", "workflows"]) assert.equal(sectionSummary(kind, []), "none yet", kind);
});
test("sectionSummary counts what is on, asking or running", () => {
  assert.equal(sectionSummary("flows", [{ enabled: true }, { enabled: false }]), "2, 1 on");
  const asking = sectionSummary("sessions", [{ terms: [T("a", "s", { state: "blocked" })] }, { terms: [T("b", "s")] }]);
  assert.match(asking, /^2\b.*1 asking/);
  const running = sectionSummary("workflows", [{ state: "running" }, { state: "never run" }]);
  assert.match(running, /^2\b.*1 running/);
  assert.doesNotMatch(sectionSummary("sessions", [{ terms: [T("b", "s")] }]), /asking/);
});

// ---- "Started by schedule X" (session details; the label comes from the honesty table) ----
test("Flow session: cao-flow-<name> for an existing flow reads 'Started by schedule <name>' with its label", () => {
  const r = flowOfSession("cao-flow-daily-report", ["daily-report", "other"]);
  assert.deepEqual([r.flow, r.text, r.label], ["daily-report", "Started by schedule daily-report", HONESTY.flowInferred]);
});
test("flowOfSession is null when the flow does not exist, the prefix differs, or the match is only partial", () => {
  assert.equal(flowOfSession("cao-flow-gone", ["other"]), null);
  assert.equal(flowOfSession("cao-dev", ["dev"]), null);
  assert.equal(flowOfSession("cao-flow-a-b", ["a"]), null, "the whole remainder is the flow name");
  assert.equal(flowOfSession("cao-flow-", [""]), null);
  assert.equal(flowOfSession("CAO-FLOW-daily", ["daily"]), null);
  assert.equal(flowOfSession("cao-flow-daily", []), null);
});
test("flowOfSession survives missing input and regex characters in names", () => {
  assert.equal(flowOfSession(undefined, ["x"]), null);
  assert.equal(flowOfSession("cao-flow-x", undefined), null);
  assert.equal(flowOfSession("cao-flow-a.b", ["a.b"]).flow, "a.b");
  assert.equal(flowOfSession("cao-flow-axb", ["a.b"]), null, "a name is not a pattern");
  assert.equal(flowOfSession("cao-flow-日本", ["日本"]).text, "Started by schedule 日本");
});

// ---- status shapes: every state has its Herdr mark ----
const EIGHT = ["working", "idle", "done", "replied", "blocked", "quiet", "starting", "error"];
// The marks are Herdr's own, so the states Herdr has no mark for share the mark of the
// Herdr status they refine (replied is done, error is blocked, quiet is working); lib/agents.test.mjs pins the mapping and the drawing.
test("Every state has its Herdr mark: working, idle, done and blocked have four different marks, and the other four borrow one", () => {
  const shapes = EIGHT.map(statusShape);
  assert.deepEqual(shapes, ["spinner", "ring", "dot", "dot-hi", "bang", "spinner", "none", "bang"]);
  assert.equal(new Set(["working", "idle", "done", "blocked"].map(statusShape)).size, 4);
  for (const s of shapes) assert.ok(typeof s === "string" && s !== "");
});
test("statusShape gives an unknown state no mark without throwing", () => {
  for (const junk of ["nonsense", "", undefined, null, 7]) assert.doesNotThrow(() => statusShape(junk));
  assert.equal(statusShape("nonsense"), "none");
});
test("displayState folds a record, replied and quiet into one of the eight states", () => {
  assert.equal(displayState(T("a", "s", { state: "error" }), {}), "error");
  assert.equal(displayState(T("a", "s", { state: "blocked" }), {}), "blocked");
  assert.equal(displayState(T("a", "s", { state: "working" }), { quiet: true }), "quiet");
  assert.equal(displayState(T("a", "s", { state: "working" }), {}), "working");
  assert.equal(displayState(T("a", "s", { state: "done" }), { replied: true }), "replied");
  assert.equal(displayState(T("a", "s", { state: "done" }), {}), "done");
  assert.equal(displayState(T("a", "s", { state: "idle" }), {}), "idle");
  assert.equal(displayState(T("a", "s", { state: "weird" }), {}), "starting");
});
test("displayState: an error never shows as blocked, and replied never overrides blocked", () => {
  assert.equal(displayState(T("a", "s", { state: "error" }), { replied: true, quiet: true }), "error");
  assert.equal(displayState(T("a", "s", { state: "blocked" }), { replied: true }), "blocked");
  assert.ok(EIGHT.includes(displayState(T("a", "s", { state: undefined }), undefined)));
});

// ---- the fallback to CAO's own data when the labels are absent ----
const unlabelled = (id, over = {}) => T(id, "s", { profile: undefined, ...over });
test("Older server: each unlabelled terminal is fetched once, labelled ones never", () => {
  const terms = [unlabelled("t1"), unlabelled("t2"), T("t3", "s")];
  assert.deepEqual(fallbackPlan({}, terms, 1000), ["t1", "t2"]);
  const cache = markPending({}, ["t1", "t2"], 1000);
  assert.deepEqual(fallbackPlan(cache, terms, 2000), [], "in flight: not fetched twice");
});
test("Older server: a fetched terminal is never fetched again while it lives", () => {
  const terms = [unlabelled("t1")];
  const cache = fallbackResult(markPending({}, ["t1"]), "t1", { kind: "ok", json: { agent_profile: "dev", caller_id: "c1" } }, 3000);
  assert.deepEqual(fallbackPlan(cache, terms, 3000 + 24 * 3600_000), []);
  const out = withFallback(terms, cache);
  assert.deepEqual([out[0].profile, out[0].caller], ["dev", "c1"]);
});
test("CAO unreachable during fallback: the retry waits at least 30 s, then is allowed", () => {
  const terms = [unlabelled("t1"), unlabelled("t2")];
  let cache = markPending({}, ["t1", "t2"], 1000);
  cache = fallbackResult(cache, "t1", { kind: "ok", json: { agent_profile: "dev", caller_id: "c1" } }, 3000);
  cache = fallbackResult(cache, "t2", { kind: "unreachable" }, 3000);
  assert.deepEqual(fallbackPlan(cache, terms, 3000 + 29_999), []);
  assert.deepEqual(fallbackPlan(cache, terms, 3000 + 30_000), ["t2"]);
  assert.deepEqual(fallbackPlan(cache, terms, 3000 + 300_000), ["t2"], "and stays due until it is fetched again");
});
test("fallbackPlan: any failed outcome, not only unreachable, waits 30 s before the retry", () => {
  const terms = [unlabelled("t1")];
  for (const outcome of [{ kind: "error", status: 404, detail: "x" }, { kind: "auth", status: 401 }, { kind: "ok" }, { kind: "ok", json: null }]) {
    const cache = fallbackResult(markPending({}, ["t1"]), "t1", outcome, 0);
    assert.deepEqual(fallbackPlan(cache, terms, 29_999), [], JSON.stringify(outcome));
    assert.deepEqual(fallbackPlan(cache, terms, 30_000), ["t1"], JSON.stringify(outcome));
  }
});
test("fallbackPlan fetches an id once even when two records carry it, and ignores a terminal that has its label", () => {
  assert.deepEqual(fallbackPlan({}, [unlabelled("t1"), unlabelled("t1", { address: "cao/other" })], 0), ["t1"]);
  assert.deepEqual(fallbackPlan({}, [], 0), []);
});
test("withFallback fills profile and caller only for unlabelled terminals, and leaves unresolved ones alone", () => {
  const cache = { t1: { state: "ok", agent_profile: "dev", caller_id: "c1" }, t3: { state: "ok", agent_profile: "x", caller_id: null }, t4: { state: "failed", at: 1 } };
  const out = withFallback([unlabelled("t1"), T("t3", "s"), unlabelled("t4"), unlabelled("t5")], cache);
  assert.equal(out[0].profile, "dev");
  assert.equal(out[1].profile, "conductor", "a labelled terminal is untouched");
  assert.equal(out[2].profile, undefined);
  assert.equal(out[3].profile, undefined);
});
test("the fallback functions do not mutate their arguments", () => {
  const terms = freeze([unlabelled("t1")]); const cache = freeze({});
  assert.doesNotThrow(() => { fallbackPlan(cache, terms, 0); markPending(cache, ["t1"], 0); fallbackResult(cache, "t1", { kind: "unreachable" }, 0); withFallback(terms, cache); });
});

// ---- keyboard ----
const ITEMS = [
  { kind: "header", section: "sessions" }, { kind: "row", section: "sessions", id: "s1", worker: false },
  { kind: "row", section: "sessions", id: "w1", worker: true }, { kind: "header", section: "flows" },
];
test("Keyboard: Up and Down move between rows and headers and stop at both ends", () => {
  const s = { items: ITEMS, index: 0, folded: {} };
  assert.equal(navStep(s, "down").index, 1);
  assert.equal(navStep(s, "up").index, 0);
  assert.equal(navStep({ ...s, index: 3 }, "ArrowDown").index, 3);
  assert.equal(navStep({ ...s, index: 2 }, "ArrowUp").index, 1);
});
test("Keyboard: Left moves a row to its header and folds a header; Right unfolds it", () => {
  const s = { items: ITEMS, index: 2, folded: {} };
  assert.equal(navStep(s, "left").index, 0);
  const folded = navStep({ ...s, index: 0 }, "left");
  assert.deepEqual([folded.folded.sessions, folded.action], [true, { type: "fold", section: "sessions", folded: true }]);
  const open = navStep({ ...s, index: 0, folded: { sessions: true } }, "right");
  assert.deepEqual([open.folded.sessions, open.action], [false, { type: "fold", section: "sessions", folded: false }]);
});
test("Keyboard: Enter opens a row and toggles a header; P pins only a worker row", () => {
  const s = { items: ITEMS, index: 1, folded: {} };
  assert.deepEqual(navStep(s, "enter").action, { type: "open", id: "s1", section: "sessions" });
  assert.deepEqual(navStep({ ...s, index: 0 }, "enter").action, { type: "fold", section: "sessions", folded: true });
  assert.deepEqual(navStep({ ...s, index: 0, folded: { sessions: true } }, "enter").action, { type: "fold", section: "sessions", folded: false });
  assert.ok(nothing(navStep(s, "p").action), "a talkable row is not pinned");
  assert.ok(nothing(navStep({ ...s, index: 0 }, "p").action), "a header is not pinned");
  assert.deepEqual(navStep({ ...s, index: 2 }, "P").action, { type: "pin", id: "w1" });
  assert.deepEqual(navStep({ ...s, index: 2 }, "p").action, { type: "pin", id: "w1" }, "the key is case-insensitive");
});
test("Keyboard: other keys, an empty list and an out-of-range index change nothing and do not throw", () => {
  const s = { items: ITEMS, index: 1, folded: {} };
  const same = navStep(s, "x");
  assert.deepEqual([same.index, same.folded, nothing(same.action)], [1, {}, true]);
  for (const key of ["down", "up", "left", "right", "enter", "p", undefined, null, ""]) {
    assert.doesNotThrow(() => navStep({ items: [], index: 0, folded: {} }, key), String(key));
    assert.doesNotThrow(() => navStep({ items: ITEMS, index: 99, folded: {} }, key), String(key));
  }
});
test("navStep does not mutate its state", () => {
  assert.doesNotThrow(() => navStep(freeze({ items: ITEMS.map((i) => ({ ...i })), index: 0, folded: {} }), "left"));
});

// ---- recap, context percentage, known gap (session details) ----
test("Recap present: the line is returned as the agent's own words, with its label", () => {
  const r = parseRecap("x\n※ recap: Tests pass; waiting for review.\nmore");
  assert.deepEqual([r.text, r.label], ["Tests pass; waiting for review.", HONESTY.recap]);
});
test("Recap present: a later recap line on the screen wins (assumed: one recap line is shown)", () => {
  assert.equal(parseRecap("※ recap: first\nmore\n※ recap: second\n").text, "second");
});
test("No recap: nothing is inferred from text that is not a recap line", () => {
  for (const text of ["no recap here", "recap: not it", "※ recap:", "※ recap:   ", "", undefined, null, 7, {}]) assert.equal(parseRecap(text), null, JSON.stringify(text));
});
test("parseRecap reads Windows line endings, indentation and unicode, and trims the text", () => {
  assert.equal(parseRecap("a\r\n   ※ recap:   Done ✓  \r\nb").text, "Done ✓");
});
test("parseRecap handles a very long screen and a very long recap quickly", () => {
  const t0 = Date.now();
  assert.equal(parseRecap(`${"x\n".repeat(200_000)}※ recap: end`).text, "end");
  assert.equal(parseRecap(`※ recap: ${"a".repeat(200_000)}`).text.length, 200_000);
  assert.ok(Date.now() - t0 < 1000);
});
test("Context percentage off by default: no regex, an empty regex or a non-string regex show nothing", () => {
  for (const regex of [undefined, null, "", 5, {}]) assert.equal(contextPercent("ctx 42%", regex), null, String(regex));
});
test("contextPercent shows the first capture group as a number, labelled heuristic, only when the regex matches", () => {
  assert.deepEqual(contextPercent("ctx 42% used", "ctx (\\d+)%"), { percent: 42, label: HONESTY.heuristic });
  assert.equal(contextPercent("nothing", "ctx (\\d+)%"), null);
  assert.equal(contextPercent("ctx 0%", "ctx (\\d+)%").percent, 0);
  assert.equal(contextPercent("ctx 100%", "ctx (\\d+)%").percent, 100);
});
test("contextPercent: an invalid regex, a non-text screen and a number outside 0 to 100 read as off (assumed range)", () => {
  assert.equal(contextPercent("x", "("), null);
  assert.equal(contextPercent(undefined, "ctx (\\d+)%"), null);
  assert.equal(contextPercent("ctx 420%", "ctx (\\d+)%"), null);
  assert.equal(contextPercent("ctx abc%", "ctx (\\w+)%"), null);
});
test("Known gap: the note carries its label and says that done means the turn ended", () => {
  const n = knownGapNote();
  assert.equal(n.label, HONESTY.knownGap);
  assert.match(JSON.stringify(n), /turn ended/i);
  assert.match(JSON.stringify(n), /prose/i);
  assert.deepEqual(knownGapNote(), n, "the same note every time");
});

// ---- seen: who writes herdr.seen, and how "replied" clears ----
const REPLIED = T("c1", "s", { state: "done", turn: 10, seen: 9 });
const FOCUSED = "cao/c1";
const talk = (t) => ({ ...t, talkable: true });

test("exports: SEEN_VERIFY_MS is 5 s and the default mode is write", () => {
  assert.equal(L.SEEN_VERIFY_MS, 5000);
  assert.equal(L.SEEN_MODE_DEFAULT, "write");
});
test("Replied: a done or idle terminal whose numeric seen is below its turn", () => {
  assert.equal(isReplied(REPLIED, {}), true);
  assert.equal(isReplied({ ...REPLIED, state: "idle" }, {}), true);
  for (const state of ["working", "blocked", "error", "starting", "unknown"]) assert.equal(isReplied({ ...REPLIED, state }, {}), false, state);
  assert.equal(isReplied({ ...REPLIED, seen: 10 }, {}), false, "seen equals turn");
  assert.equal(isReplied({ ...REPLIED, seen: 12 }, {}), false, "seen ahead of turn");
});
test("Seen state unknown: no herdr.seen label and no per-window value is never replied", () => {
  const fresh = { ...REPLIED, seen: undefined };
  assert.equal(isReplied(fresh, {}), false);
  assert.equal(isReplied(fresh), false, "no map at all");
  assert.equal(isReplied({ ...fresh, turn: 0 }, {}), false);
});
test("isReplied compares the turn with the larger of herdr.seen and the per-window map", () => {
  assert.equal(isReplied(REPLIED, { c1: 10 }), false, "the map is ahead of the label");
  assert.equal(isReplied(REPLIED, { c1: 4 }), true, "the label is ahead of the map");
  assert.equal(isReplied({ ...REPLIED, seen: undefined }, { c1: 4 }), true, "the map alone can say replied");
  assert.equal(isReplied(REPLIED, { other: 99 }), true, "another terminal's value does not count");
});
test("isReplied ignores a per-window value that is not a number", () => {
  for (const bad of [NaN, "10", null, undefined, Infinity * 0]) assert.equal(isReplied(REPLIED, { c1: bad }), true, String(bad));
});
test("seenState: replied, seen or unknown for a finished turn, never replied or unknown while working", () => {
  const fresh = { ...REPLIED, seen: undefined };
  assert.equal(seenState(fresh, {}), "unknown");
  assert.equal(seenState(REPLIED, {}), "replied");
  assert.equal(seenState({ ...REPLIED, seen: 10 }, {}), "seen");
  assert.equal(seenState(fresh, { c1: 10 }), "seen");
  assert.equal(seenState(fresh, { c1: 4 }), "replied");
  assert.equal(seenState({ ...REPLIED, state: "idle", seen: undefined }, {}), "unknown", "idle counts like done");
  for (const state of ["replied", "unknown"]) assert.notEqual(seenState({ ...fresh, state: "working" }, {}), state);
});
test("seenState and isReplied never disagree about replied", () => {
  for (const state of ["done", "idle", "working", "blocked", "error"]) for (const seen of [undefined, 0, 5, 10, 11]) for (const local of [{}, { c1: 3 }, { c1: 10 }]) {
    const t = { ...REPLIED, state, seen };
    assert.equal(seenState(t, local) === "replied", isReplied(t, local), JSON.stringify([state, seen, local]));
  }
});
test("Replied clears on focus: remembering the turn clears replied without a label", () => {
  const local = rememberSeen({}, REPLIED);
  assert.deepEqual(local, { c1: 10 });
  assert.equal(isReplied(REPLIED, local), false);
  assert.equal(seenState(REPLIED, local), "seen");
});
test("rememberSeen returns the same map when nothing changes, never goes backwards, and does not mutate", () => {
  const a = rememberSeen({}, REPLIED);
  assert.equal(rememberSeen(a, REPLIED), a);
  assert.equal(rememberSeen(a, { ...REPLIED, turn: 4 }), a, "an older turn does not lower it");
  assert.deepEqual(rememberSeen(a, { ...REPLIED, turn: 11 }), { c1: 11 });
  assert.doesNotThrow(() => rememberSeen(freeze({}), REPLIED));
});

test("remember at working, finish unseen: replied", () => {
  const working = T("c1", "s", { state: "working", turn: 11, seen: 10 });
  const local = rememberFinishedSeen({}, working);
  assert.deepEqual(local, {}, "a turn that has not ended is not remembered");
  assert.equal(isReplied({ ...working, state: "done" }, local), true, "it finishes unseen: replied");
});
test("remember when the turn has ended: done and idle are remembered, working, blocked and error are not", () => {
  for (const state of ["done", "idle"]) {
    const local = rememberFinishedSeen({}, { ...REPLIED, state });
    assert.deepEqual(local, { c1: 10 }, state);
    assert.equal(isReplied({ ...REPLIED, state }, local), false, state);
  }
  for (const state of ["working", "blocked", "error"]) assert.deepEqual(rememberFinishedSeen({}, { ...REPLIED, state }), {}, state);
  const a = { c1: 10 };
  assert.equal(rememberFinishedSeen(a, REPLIED), a, "nothing new: the same map");
  assert.doesNotThrow(() => rememberFinishedSeen(freeze({}), { ...REPLIED, state: "working" }));
});

test("seenWrite: the label command for a focused, replied talkable agent in an active window", () => {
  assert.deepEqual(seenWrite(talk(REPLIED), FOCUSED, true, "cao", "write"), ["label", "cao/c1", "herdr.seen=10"]);
});
test("seenWrite: the mode defaults to write, and a bare address gets the machine prefix", () => {
  assert.deepEqual(seenWrite(talk(REPLIED), FOCUSED, true, "cao"), ["label", "cao/c1", "herdr.seen=10"]);
  assert.deepEqual(seenWrite(talk({ ...REPLIED, address: "c1" }), "c1", true, "boxA", "write"), ["label", "boxA/c1", "herdr.seen=10"]);
});
test("seenWrite: an agent whose seen label is missing is still written, so its unknown state resolves", () => {
  const unknown = talk({ ...REPLIED, seen: undefined, turn: 3 });
  assert.deepEqual(seenWrite(unknown, FOCUSED, true, "cao", "write"), ["label", "cao/c1", "herdr.seen=3"]);
});
test("seenWrite writes nothing when the window is not active, the terminal is not the focused one, or it is not talkable", () => {
  const t = talk(REPLIED);
  assert.ok(nothing(seenWrite(t, FOCUSED, false, "cao", "write")));
  assert.ok(nothing(seenWrite(t, "cao/other", true, "cao", "write")));
  assert.ok(nothing(seenWrite(t, undefined, true, "cao", "write")));
  assert.ok(nothing(seenWrite({ ...t, talkable: false }, FOCUSED, true, "cao", "write")));
  assert.ok(nothing(seenWrite(REPLIED, FOCUSED, true, "cao", "write")), "no talkable flag: a worker is never written");
});
test("seenWrite writes nothing when the agent is already seen, or is not at a finished turn", () => {
  const t = talk(REPLIED);
  assert.ok(nothing(seenWrite({ ...t, seen: 10 }, FOCUSED, true, "cao", "write")));
  assert.ok(nothing(seenWrite({ ...t, seen: 11 }, FOCUSED, true, "cao", "write")));
  assert.ok(nothing(seenWrite({ ...t, state: "working" }, FOCUSED, true, "cao", "write")));
});
test("seenWrite writes nothing in off mode (Herdr writes it) or local mode (the write did not land), or an unknown mode", () => {
  const t = talk(REPLIED);
  for (const mode of ["off", "local", "bogus", ""]) assert.ok(nothing(seenWrite(t, FOCUSED, true, "cao", mode)), mode);
});
test("seenWrite: the turn is written as a whole number, and an unusable turn writes nothing", () => {
  assert.deepEqual(seenWrite(talk({ ...REPLIED, turn: 2 ** 40, seen: 1 }), FOCUSED, true, "cao", "write")[2], `herdr.seen=${2 ** 40}`);
  for (const turn of [NaN, undefined, Infinity]) assert.ok(nothing(seenWrite(talk({ ...REPLIED, turn }), FOCUSED, true, "cao", "write")), String(turn));
});
test("seenWrite does not mutate the terminal", () => {
  assert.doesNotThrow(() => seenWrite(freeze(talk(REPLIED)), FOCUSED, true, "cao", "write"));
});

const PENDING = { mode: "write", pending: { id: "c1", turn: 10, at: 1000 } };
test("seenVerify: the pushed value within 5 s verifies the write and keeps writing", () => {
  assert.deepEqual(seenVerify(PENDING, { id: "c1", seen: 10 }, 2000), { mode: "write", pending: null });
  assert.deepEqual(seenVerify(PENDING, { id: "c1", seen: "10" }, 2000), { mode: "write", pending: null }, "labels arrive as strings");
});
test("Label write does not land: silence for 5 s switches to the local map", () => {
  assert.deepEqual(seenVerify(PENDING, null, 1000 + 4999), PENDING, "still waiting");
  assert.deepEqual(seenVerify(PENDING, null, 1000 + 5001), { mode: "local", pending: null });
});
test("seenVerify: a push with the old value, or for another terminal, does not verify", () => {
  assert.deepEqual(seenVerify(PENDING, { id: "c1", seen: 9 }, 1000 + 5001), { mode: "local", pending: null });
  assert.deepEqual(seenVerify(PENDING, { id: "c1", seen: 9 }, 2000), PENDING, "wrong value inside the window keeps waiting");
  assert.deepEqual(seenVerify(PENDING, { id: "other", seen: 10 }, 2000), PENDING);
  assert.deepEqual(seenVerify(PENDING, { id: "other", seen: 10 }, 1000 + 5001), { mode: "local", pending: null });
});
test("seenVerify: a failed hc call switches to local at once", () => {
  assert.deepEqual(seenVerify(PENDING, { failed: true }, 1100), { mode: "local", pending: null });
});
test("seenVerify: with nothing pending the state is returned as it is, in any mode", () => {
  for (const mode of ["write", "off", "local"]) {
    const idle = { mode, pending: null };
    assert.deepEqual(seenVerify(idle, null, 99_999), idle, mode);
    assert.deepEqual(seenVerify(idle, { failed: true }, 99_999), idle, `${mode} with a stray failure`);
  }
});
test("seenVerify does not mutate its state", () => {
  assert.doesNotThrow(() => seenVerify(freeze({ mode: "write", pending: { id: "c1", turn: 1, at: 0 } }), { id: "c1", seen: 1 }, 5));
});
test("Label write does not land: the pill still clears through the map, and the window writes no more this run", () => {
  const t = talk(REPLIED);
  assert.deepEqual(seenWrite(t, FOCUSED, true, "cao", "write"), ["label", "cao/c1", "herdr.seen=10"]);
  const state = seenVerify({ mode: "write", pending: { id: "c1", turn: 10, at: 0 } }, null, 5001);
  assert.equal(state.mode, "local");
  const local = rememberSeen({}, REPLIED);
  assert.equal(isReplied(REPLIED, local), false, "Replied pill clears in this window");
  assert.ok(nothing(seenWrite(talk({ ...REPLIED, turn: 11 }), FOCUSED, true, "cao", state.mode)), "no further label writes");
});
test("Seen state unknown: the session is not counted as replied, and a per-window value that says replied changes the count but not the order", () => {
  const unknown = sess("u", [T("cu", "u", { state: "done", turn: 3, seen: undefined, createdMs: 2 })]);
  const working = sess("w", [T("w1", "w", { state: "working", createdMs: 1 })]);
  assert.deepEqual(statusCounts([working, unknown], (t) => isReplied(t, {})).replied, 0);
  assert.deepEqual(statusCounts([working, unknown], (t) => isReplied(t, { cu: 1 })).replied, 1, "a smaller per-window value says replied");
  assert.deepEqual(sortSessions([unknown, working]).map((s) => s.name), ["w", "u"], "by age either way");
});

// ---- the kind of a blocked terminal, the words of its tooltip ("needs you: <kind>"; no pill) ----
test("Asking kind: the recorded blocked record gives its kind, or none", () => {
  const record = JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", "test", "fixtures", "blocked-record.json"), "utf8"));
  const t = termOf(record);
  assert.equal(t.state, "blocked");
  assert.equal(askingKind(t), t.kind ? t.kind.trim() : null);
});
test("Asking kind: kind permission is permission; without a kind it is null", () => {
  assert.equal(askingKind({ state: "blocked", kind: "permission" }), "permission");
  assert.equal(askingKind({ state: "blocked" }), null);
});
test("Asking kind: an empty, blank or non-text kind is no kind", () => {
  for (const kind of ["", "   ", null, 0, 5, {}, []]) assert.equal(askingKind({ state: "blocked", kind }), null, JSON.stringify(kind));
  assert.doesNotThrow(() => askingKind(undefined));
  assert.doesNotThrow(() => askingKind(null));
});
test("Asking kind: the three kinds Herdr names, and a unicode kind, are given as they are", () => {
  for (const kind of ["permission", "question", "auth", "autorisation-é"]) assert.equal(askingKind({ state: "blocked", kind }), kind);
});

// ---- buildModel, the model the side, tabs and status mods share ----
const raw = (id, session, extra = {}) => ({
  address: `cao/${id}`,
  labels: { "cao.terminal_id": id, "cao.session": session, "cao.profile": extra.profile ?? "conductor", ...(extra.caller ? { "cao.caller": extra.caller } : {}), ...(extra.labels ?? {}) },
  status: extra.status ?? "idle", turn: extra.turn ?? 0,
});
const CTX = { cache: {}, drafts: [], pins: [], local: {}, sha256hex: () => "0".repeat(64) };
test("buildModel composes terminals into sorted sessions with the talkable flag set", () => {
  const m = buildModel(
    [raw("c1", "quiet"), raw("c2", "busy", { status: "working" }), raw("w9", "busy", { profile: "developer", caller: "c2", status: "blocked" })],
    [], { machine: "boxA" }, CTX);
  assert.equal(m.machine, "boxA");
  assert.deepEqual(m.sessions.map((s) => s.name), ["busy", "quiet"], "by name here: no terminal has a creation time, and the blocked worker lifts nothing");
  assert.equal(m.sessions[0].talkable[0].talkable, true, "seenWrite reads this flag");
  assert.ok(!m.sessions[0].workers[0].talkable, "a worker is not flagged");
  assert.equal(m.terms.length, 3);
  assert.equal(typeof m.replied, "function");
});
test("buildModel: the replied predicate reads the per-window map and the pins and props are handed through", () => {
  const rec = raw("c1", "s", { status: "done", turn: 5, labels: { "herdr.seen": "2" } });
  const open = buildModel([rec], [], { machine: "m" }, CTX);
  assert.equal(open.replied(open.terms[0]), true);
  const seen = buildModel([rec], [], { machine: "m" }, { ...CTX, local: { c1: 5 }, pins: ["w1"] });
  assert.equal(seen.replied(seen.terms[0]), false);
  assert.deepEqual([seen.pins, seen.local], [["w1"], { c1: 5 }]);
});
test("buildModel: recorded drafts, the profile cache, other machines' terminals and junk are handled", () => {
  const m = buildModel(
    [raw("d1", "cao-draft-flow-1", { status: "blocked" }), raw("u1", "old", { labels: { "cao.profile": "" } }), { address: "x", labels: {} }, null],
    [], { machine: "m" }, { ...CTX, drafts: ["d1"] });
  assert.equal(m.sessions.find((s) => s.name === "cao-draft-flow-1").draft, true);
  assert.equal(m.terms.length, 2, "a terminal with no CAO labels is not a CAO terminal");
});
test("buildModel finds the CAO machine from a machine-prefixed address when the props do not name it", () => {
  assert.equal(buildModel([{ ...raw("t1", "s"), address: "boxB/t1" }], [], {}, CTX).machine, "boxB");
  assert.equal(buildModel([], [], {}, CTX).machine, "cao");
});
test("buildModel: nothing in, nothing out, and the inputs are not mutated", () => {
  const empty = buildModel(null, null, {}, CTX);
  assert.deepEqual([empty.sessions, empty.terms], [[], []]);
  assert.doesNotThrow(() => buildModel(freeze([raw("c1", "s"), raw("w1", "s", { profile: "developer", caller: "c1" })]), freeze([]), freeze({ machine: "m" }), freeze({ ...CTX, cache: {}, drafts: [], pins: [], local: {} })));
});

// ---- failure states keep the last good view ----
const OK = (json) => ({ kind: "ok", status: 200, json });
test("Stale data: a failure keeps the last good data and its age is told in minutes", () => {
  const t0 = Date.UTC(2026, 10, 18, 10, 0, 0);
  let view = applyOutcome({}, OK([{ name: "f1" }]), t0);
  assert.deepEqual(view.data, [{ name: "f1" }]);
  view = applyOutcome(view, { kind: "unreachable" }, t0 + 5 * MIN);
  assert.deepEqual(view.data, [{ name: "f1" }], "the 10:00 rows stay");
  assert.equal(view.unreachable, true);
  assert.equal(ageWords(t0 + 5 * MIN - view.at), "5 minutes");
});
test("a later success replaces the data and clears every failure flag", () => {
  let view = applyOutcome({}, OK([1]), 1);
  for (const bad of [{ kind: "unreachable" }, { kind: "auth", status: 401 }, { kind: "error", status: 500, detail: "boom" }]) view = applyOutcome(view, bad, 2);
  view = applyOutcome(view, OK([]), 3);
  assert.deepEqual([view.data, view.unreachable, view.auth, view.error], [[], false, false, null]);
  assert.equal(view.at, 3);
});
test("an HTTP error keeps the data and records the server's message", () => {
  const view = applyOutcome({ data: [1], at: 5 }, { kind: "error", status: 500, detail: "boom" }, 9);
  assert.deepEqual(view.data, [1]);
  assert.match(String(view.error), /boom/);
  assert.equal(view.at, 5, "the data is still from time 5");
});
test("Auth required: write controls are disabled, the data is kept, and a good reply re-enables them", () => {
  const view = applyOutcome({ data: [1], at: 1 }, { kind: "auth", status: 401 }, 2);
  assert.equal(writesDisabled(view), true);
  assert.deepEqual(view.data, [1]);
  assert.equal(writesDisabled(applyOutcome(view, OK([]), 3)), false);
});
test("Approval refusal is not auth: a 403 approval_required disables nothing and keeps the data", () => {
  const view = applyOutcome({ data: [1], at: 1 }, { kind: "approval_required", status: 403, planId: "wf:abc", message: "" }, 2);
  assert.equal(writesDisabled(view), false);
  assert.deepEqual(view.data, [1]);
  assert.ok(!view.auth);
  assert.ok(!view.unreachable);
});
test("refused-before-send outcomes (too large, env, bad request) keep the data and do not disable writes", () => {
  for (const kind of ["too_large", "env_refused", "refused"]) {
    const view = applyOutcome({ data: [1], at: 1 }, { kind, message: "no" }, 2);
    assert.deepEqual([view.data, writesDisabled(view), !!view.unreachable], [[1], false, false], kind);
  }
});
test("applyOutcome with no outcome, an unknown kind, or no starting view neither throws nor loses the data", () => {
  const base = { data: [1], at: 1 };
  for (const o of [undefined, null, {}, { kind: "weird" }, 7]) assert.deepEqual(applyOutcome(base, o, 2).data, [1], JSON.stringify(o));
  assert.doesNotThrow(() => applyOutcome(undefined, { kind: "unreachable" }, 2));
  assert.equal(applyOutcome(undefined, { kind: "unreachable" }, 2).unreachable, true);
});
test("applyOutcome does not mutate the view or the outcome", () => {
  assert.doesNotThrow(() => applyOutcome(freeze({ data: [1], at: 1 }), freeze(OK([2])), 3));
  assert.doesNotThrow(() => applyOutcome(freeze({ data: [1], at: 1 }), freeze({ kind: "auth", status: 403 }), 3));
});
test("writesDisabled is false without a view and true only for an auth failure", () => {
  assert.equal(writesDisabled(undefined), false);
  assert.equal(writesDisabled({}), false);
  assert.equal(writesDisabled({ unreachable: true, error: "x" }), false);
  assert.equal(writesDisabled({ auth: true }), true);
});
test("ageWords tells seconds, minutes and hours, singular at one", () => {
  assert.equal(ageWords(59_000), "59 seconds");
  assert.equal(ageWords(MIN), "1 minute");
  assert.equal(ageWords(5 * MIN), "5 minutes");
  assert.equal(ageWords(60 * MIN), "1 hour");
  assert.equal(ageWords(120 * MIN), "2 hours");
});
test("ageWords never prints NaN or a negative age (clock skew, no data)", () => {
  for (const ms of [-5000, -1, 0, NaN, undefined]) assert.doesNotMatch(String(ageWords(ms)), /NaN|-|undefined/, String(ms));
});
test("CAO unreachable during fallback: the header says so", () => {
  assert.equal(headerNote({ connected: true, unreachable: true }), "CAO unreachable, retrying");
  assert.ok(nothing(headerNote({ connected: true, unreachable: false })));
});
test("a machine that is not connected is said so in the header, even when CAO also fails (assumed precedence)", () => {
  assert.match(headerNote({ connected: false, unreachable: false }), /not connected/i);
  assert.match(headerNote({ connected: false, unreachable: true }), /not connected/i);
});

// ---- status bar counts, text and filters ----
const BAR = (counts, opts = {}) => barText(counts, opts);
test("Counts: 13 sessions, one blocked worker, one replied coordinator, five working workers in one session", () => {
  const list = [];
  for (let i = 0; i < 10; i++) list.push(sess(`s${i}`, [T(`c${i}`, `s${i}`)]));
  list.push(sess("ask", [T("ca", "ask"), worker("wa", "ask", "ca", { state: "blocked" })]));
  list.push(sess("rep", [T("cr", "rep", { state: "done", turn: 3, seen: 2 })]));
  list.push(sess("busy", [T("cb", "busy"), ...[1, 2, 3, 4, 5].map((i) => worker(`wb${i}`, "busy", "cb", { state: "working" }))]));
  assert.equal(BAR(statusCounts(list, (t) => t.id === "cr")), `cao ${DOT} 13 sessions ${DOT} 1 asking ${DOT} 1 replied ${DOT} 1 working`);
});
test("the counts hold the five numbers of the bar under the names the bar gives them", () => {
  const list = [sess("ask", [T("ca", "ask"), worker("wa", "ask", "ca", { state: "blocked" })]), sess("rest", [T("r", "rest")])];
  assert.deepEqual(statusCounts(list, never), { sessions: 2, asking: 1, replied: 0, repliedUnknown: 0, working: 0 });
});
test("a zero count is hidden except sessions, which shows even at zero", () => {
  const two = [sess("a", [T("a1", "a")]), sess("b", [T("b1", "b")])];
  assert.equal(BAR(statusCounts(two, never)), `cao ${DOT} 2 sessions`);
  assert.equal(BAR(statusCounts([], never)), `cao ${DOT} 0 sessions`);
});
test("A counts blocked terminals, workers and agents alike, one each", () => {
  const list = [sess("a", [T("c", "a", { state: "blocked" }), worker("w1", "a", "c", { state: "blocked" }), worker("w2", "a", "c", { state: "blocked" })]), sess("b", [T("d", "b")])];
  assert.match(BAR(statusCounts(list, never)), new RegExp(`2 sessions ${DOT} 3 asking$`));
});
test("R counts replied talkable agents only; a replied worker is not counted", () => {
  const list = [sess("a", [T("c", "a"), worker("w", "a", "c", { state: "done", turn: 2, seen: 1 })]), sess("b", [T("d", "b")])];
  assert.match(BAR(statusCounts(list, () => true)), new RegExp(`2 sessions ${DOT} 2 replied$`), "the two talkable agents, not the worker");
  assert.equal(BAR(statusCounts(list, (t) => t.id === "w")), `cao ${DOT} 2 sessions`, "only the worker is replied: nothing to count");
  assert.match(BAR(statusCounts(list, (t) => t.id === "d")), new RegExp(`2 sessions ${DOT} 1 replied$`));
});
test("W counts sessions that have any working terminal, not terminals", () => {
  const list = [sess("a", [T("c", "a", { state: "working" }), worker("w1", "a", "c", { state: "working" })]), sess("b", [T("d", "b", { state: "working" })]), sess("e", [T("f", "e")])];
  assert.match(BAR(statusCounts(list, never)), new RegExp(`3 sessions ${DOT} 2 working$`));
});
test("unknown seen states show as 'R replied (U unknown)', even when R is zero", () => {
  // A done or idle agent with no herdr.seen label and no per-window value is unknown, so the other
  // session's agent works: it is neither replied nor unknown.
  const list = [sess("u", [T("cu", "u", { state: "done", turn: 2, seen: undefined })]), sess("p", [T("p1", "p", { state: "working" })])];
  const unknown = (t) => seenState(t, {}) === "unknown";
  assert.equal(BAR(statusCounts(list, never, unknown)), `cao ${DOT} 2 sessions ${DOT} 0 replied (1 unknown) ${DOT} 1 working`);
  const withReplied = [...list, sess("r", [T("cr", "r", { state: "done", turn: 3, seen: 1 })])];
  assert.equal(BAR(statusCounts(withReplied, (t) => t.id === "cr", unknown)), `cao ${DOT} 3 sessions ${DOT} 1 replied (1 unknown) ${DOT} 1 working`);
});
test("an agent with an unknown seen state is never counted as replied", () => {
  const list = [sess("u", [T("cu", "u", { state: "done", turn: 2, seen: undefined })]), sess("p", [T("p1", "p", { state: "working" })])];
  const c = statusCounts(list, (t) => isReplied(t, {}), (t) => seenState(t, {}) === "unknown");
  assert.deepEqual([c.replied, c.repliedUnknown], [0, 1]);
});
test("a worker with an unknown seen state is not counted in U: only talkable agents are", () => {
  const list = [sess("a", [T("c", "a"), worker("w", "a", "c", { state: "done", turn: 2 })]), sess("b", [T("d", "b")])];
  assert.doesNotMatch(BAR(statusCounts(list, never, (t) => t.id === "w")), /unknown/);
});
test("Error is not asking: an error terminal adds no asking count and is not working", () => {
  const list = [sess("e", [T("e1", "e", { state: "error" })]), sess("f", [T("f1", "f")])];
  assert.equal(BAR(statusCounts(list, never)), `cao ${DOT} 2 sessions`);
});
test("recorded draft author sessions are excluded from every count; an operator's draft-x session is counted", () => {
  const author = sess("cao-draft-flow-1", [T("d1", "cao-draft-flow-1", { state: "blocked" })], { draft: true });
  const authorWorking = sess("cao-draft-flow-2", [T("d2", "cao-draft-flow-2", { state: "working" })], { draft: true });
  const operator = sess("cao-draft-x", [T("o1", "cao-draft-x", { state: "done", turn: 2, seen: 1 })]);
  const plain = sess("plain", [T("p1", "plain")]);
  assert.equal(BAR(statusCounts([author, authorWorking, operator, plain], (t) => t.id === "o1" || t.id === "d1")), `cao ${DOT} 2 sessions ${DOT} 1 replied`);
});
test("CAO down: the counts keep their values and the bar adds 'CAO unreachable'", () => {
  const list = [sess("a", [T("c", "a"), worker("w", "a", "c", { state: "blocked" })]), sess("b", [T("d", "b")])];
  const counts = statusCounts(list, never);
  const down = BAR(counts, { unreachable: true });
  assert.ok(down.startsWith(BAR(counts)), "the counts are unchanged");
  assert.match(down, /CAO unreachable/);
  assert.doesNotMatch(BAR(counts, { unreachable: false }), /unreachable/);
  assert.doesNotMatch(BAR(counts), /unreachable/);
});
test("statusCounts: a very large list counts in well under a second, and does not mutate its input", () => {
  const list = [];
  for (let i = 0; i < 20000; i++) list.push(sess(`s${i}`, [T(`c${i}`, `s${i}`, { state: i % 3 === 0 ? "working" : "idle" })]));
  const t0 = Date.now();
  const c = statusCounts(freeze(list), never);
  assert.equal(c.sessions, 20000);
  assert.equal(c.working, 6667);
  assert.ok(Date.now() - t0 < 1000);
});

// Built inside each case, so a missing classify fails that case and not the whole file.
const filterFixture = () => ({
  asking: sess("ask", [T("ca", "ask"), worker("wa", "ask", "ca", { state: "blocked" })]),
  rest: sess("rest", [T("r", "rest")]),
  rep: sess("rep", [T("cr", "rep", { state: "done", turn: 3, seen: 2 })]),
  wrk: sess("wrk", [T("cw", "wrk", { state: "working" })]),
});
const isRep = (t) => t.id === "cr";
test("Filter: clicking a count lists only the matching sessions, in their order", () => {
  const { asking, rest, rep, wrk } = filterFixture();
  const all = [rest, asking, rep, wrk];
  assert.deepEqual(filterSessions(all, "asking", isRep).map((s) => s.name), ["ask"]);
  assert.deepEqual(filterSessions(all, "replied", isRep).map((s) => s.name), ["rep"]);
  assert.deepEqual(filterSessions(all, "working", isRep).map((s) => s.name), ["wrk"]);
  assert.deepEqual(filterSessions(all, null, isRep).map((s) => s.name), ["rest", "ask", "rep", "wrk"], "no filter lists everything");
});
test("Filter: clicking the active count again clears it, clicking another switches", () => {
  assert.equal(nextFilter(null, "asking"), "asking");
  assert.equal(nextFilter("asking", "asking"), null);
  assert.equal(nextFilter("asking", "working"), "working");
  assert.equal(nextFilter(undefined, "replied"), "replied");
});
test("Filter agrees with the count: the sessions listed are the sessions the count was made from", () => {
  const { asking, rest, rep, wrk } = filterFixture();
  const all = [rest, asking, rep, wrk];
  const text = BAR(statusCounts(all, isRep));
  assert.match(text, /1 asking/); assert.equal(filterSessions(all, "asking", isRep).length, 1);
  assert.match(text, /1 replied/); assert.equal(filterSessions(all, "replied", isRep).length, 1);
  assert.match(text, /1 working/); assert.equal(filterSessions(all, "working", isRep).length, 1);
});
test("Filter: a recorded draft session stays listed under any filter, in its place", () => {
  const draft = (state) => sess("d", [T("d1", "d", { state })], { draft: true });
  assert.deepEqual(filterSessions([draft("blocked")], "asking", never).map((s) => s.name), ["d"]);
  assert.deepEqual(filterSessions([draft("idle")], "asking", never).map((s) => s.name), ["d"], "a draft that does not match is listed too");
  assert.deepEqual(filterSessions([draft("working")], "working", never).map((s) => s.name), ["d"]);
  assert.deepEqual(filterSessions([draft("done")], "replied", () => true).map((s) => s.name), ["d"]);
  const { asking, rest } = filterFixture();
  assert.deepEqual(filterSessions([rest, draft("idle"), asking], "asking", never).map((s) => s.name), ["d", "ask"], "matching sessions and drafts keep their order");
});
test("Filter: an error session does not match 'asking', and an unknown filter name does not throw", () => {
  const { rest } = filterFixture();
  const err = sess("e", [T("e1", "e", { state: "error" })]);
  assert.deepEqual(filterSessions([err], "asking", never), []);
  assert.ok(Array.isArray(filterSessions([rest], "bogus", never)));
  assert.deepEqual(filterSessions([], "asking", never), []);
});
test("filterSessions does not mutate its input", () => {
  const { asking, rest } = filterFixture();
  assert.doesNotThrow(() => filterSessions(freeze([rest, asking]), "asking", never));
});

// ---- the hand-off toast reducer ----
// toastStep(state, event, now) -> [nextState, effects]. The state and event shapes are fixed by the
// released-toast fixture in lib/honesty.js (queue items with key, kind, session, terminalId, heldAt,
// releaseAt, later, notification; a focus event with terminal, agentState and busy).
const note = (title, terminal) => ({ title, body: "b", action: { name: "agent.focus", args: { terminal } } });
const info = (over = {}) => ({ cao: true, session: "cao-b", terminalId: "t9", kind: "asking", draft: false, focusedSession: "cao-a", ...over });
const notify = (n, i, busy = true) => ({ type: "notify", notification: n, info: i, busy });
const focusEv = (agentState, busy = true, terminal = "cao/c1") => ({ type: "focus", terminal, agentState, busy });
const shows = (fx) => fx.filter((e) => e.type === "show");
const step = (s, ev, now) => toastStep(s, ev, now);
// A state whose focused agent has been focused and idle since t=0, so a hand-off is possible from 5 s on.
const idleAt0 = (cfg) => step(initialToast(cfg), focusEv("idle"), 0)[0];
// One hand-off: the focused agent is idle, then works.
const handOff = (s, now) => { const [a] = step(s, focusEv("idle"), now); return step(a, focusEv("working"), now + 1); };
const hold = (s, n, i, now) => step(s, notify(n, i), now)[0];

test("initialToast: nothing is held, the hold limit is 120 s, and pass-through is off", () => {
  const s = initialToast();
  assert.deepEqual(s.queue, []);
  const held = hold(idleAt0(), note("b", "cao/t9"), info(), 1000);
  assert.deepEqual(step(held, { type: "tick" }, 1000 + 119_999)[1], []);
  assert.equal(shows(step(held, { type: "tick" }, 1000 + 120_000)[1]).length, 1);
});
test("Held ask released on hand-off: one toast, labelled, with Open and Later, naming the session that asked", () => {
  let s = idleAt0({ holdMaxSeconds: 120 });
  let fx;
  [s, fx] = step(s, notify(note("b needs you", "cao/t9"), info()), 1000);
  assert.deepEqual(fx, [], "held, nothing shown while busy");
  assert.equal(s.queue.length, 1);
  [s, fx] = step(s, focusEv("idle"), 4000);
  assert.deepEqual(fx, [], "still idle: no hand-off");
  [s, fx] = step(s, focusEv("working"), 10_000);
  assert.equal(shows(fx).length, 1);
  const n = shows(fx)[0].notification;
  assert.equal(n.label, HONESTY.handOff);
  assert.deepEqual(n.actions, ["Open", "Later"]);
  assert.deepEqual([n.title, n.body, n.action], ["b needs you", "b", note("x", "cao/t9").action], "the original notification is kept");
  assert.equal(s.queue.length, 0);
});
test("Held ask released on hand-off: Open focuses the terminal the notification named", () => {
  let s = hold(idleAt0(), note("b needs you", "cao/t9"), info(), 1000);
  const [s2, fx] = handOff(s, 9000);
  const key = shows(fx)[0].key;
  assert.equal(key, "t9:asking");
  const [, out] = step(s2, { type: "open", key }, 12_000);
  assert.deepEqual(out, [{ type: "focus", terminal: "cao/t9" }]);
});
test("Open for a key that is not shown, or a notification with no action, does nothing and does not throw", () => {
  const s = idleAt0();
  assert.deepEqual(step(s, { type: "open", key: "nope" }, 1)[1], []);
  assert.deepEqual(step(s, { type: "later", key: "nope" }, 1)[1], []);
  const bare = hold(s, { title: "t", body: "b" }, info(), 100);
  const [s2, fx] = handOff(bare, 9000);
  assert.deepEqual(step(s2, { type: "open", key: shows(fx)[0].key }, 9500)[1], []);
});
test("a transition within 5 s of focus is not a hand-off; at 5 s it is", () => {
  let [s] = step(initialToast(), focusEv("idle"), 0);
  s = hold(s, note("x", "cao/t9"), info(), 100);
  assert.deepEqual(step(s, focusEv("working"), 4999)[1], []);
  assert.equal(shows(step(s, focusEv("working"), 5000)[1]).length, 1);
});
test("a hand-off is a move from done or idle to working; other moves are not", () => {
  for (const [from, expected] of [["idle", 1], ["done", 1], ["working", 0], ["blocked", 0], ["error", 0], ["starting", 0]]) {
    const [s0] = step(initialToast(), focusEv(from), 0);
    const s = hold(s0, note("x", "cao/t9"), info(), 100);
    assert.equal(shows(step(s, focusEv("working"), 60_000)[1]).length, expected, from);
  }
});
test("focus resets when another terminal is focused: the 5 s count starts again", () => {
  let [s] = step(initialToast(), focusEv("idle", true, "cao/c1"), 0);
  s = hold(s, note("x", "cao/t9"), info(), 100);
  [s] = step(s, focusEv("idle", true, "cao/c2"), 10_000);
  assert.deepEqual(step(s, focusEv("working", true, "cao/c2"), 12_000)[1], [], "two seconds after switching");
  assert.equal(shows(step(s, focusEv("working", true, "cao/c2"), 15_000)[1]).length, 1);
});
test("a focus change to a terminal that is already working is not a hand-off", () => {
  let [s] = step(initialToast(), focusEv("idle", true, "cao/c1"), 0);
  s = hold(s, note("x", "cao/t9"), info(), 100);
  assert.deepEqual(step(s, focusEv("working", true, "cao/c2"), 60_000)[1], []);
});
test("when the window is not busy a working agent releases nothing and the queue stays", () => {
  let [s] = step(initialToast(), focusEv("idle"), 0);
  s = hold(s, note("x", "cao/t9"), info(), 100);
  const [after, fx] = step(s, focusEv("working", false), 60_000);
  assert.deepEqual(fx, []);
  assert.equal(after.queue.length, 1);
});
test("a hand-off with nothing held shows nothing", () => {
  assert.deepEqual(handOff(idleAt0(), 9000)[1], []);
});
test("Hold limit: an asking item is shown after holdMaxSeconds without a hand-off, and holdMaxSeconds is honoured", () => {
  const s = hold(idleAt0({ holdMaxSeconds: 30 }), note("b", "cao/t9"), info(), 1000);
  assert.deepEqual(step(s, { type: "tick" }, 1000 + 29_999)[1], []);
  const [after, fx] = step(s, { type: "tick" }, 1000 + 30_000);
  assert.equal(shows(fx)[0].notification.label, HONESTY.handOff);
  assert.equal(after.queue.length, 0);
});
test("a held replied item has no timer: a tick an hour later shows nothing, the next hand-off shows it", () => {
  const s = hold(idleAt0(), note("r", "cao/t8"), info({ kind: "replied", terminalId: "t8" }), 1000);
  assert.deepEqual(step(s, { type: "tick" }, 3_600_000)[1], []);
  assert.equal(shows(handOff(s, 3_600_000)[1]).length, 1);
});
test("Condition cleared while held: the item is dropped and no toast follows", () => {
  let s = hold(idleAt0(), note("b", "cao/t9"), info(), 1000);
  [s] = step(s, { type: "clear", terminalId: "t9", kind: "asking" }, 2000);
  assert.equal(s.queue.length, 0);
  assert.deepEqual(handOff(s, 10_000)[1], []);
  assert.deepEqual(step(s, { type: "tick" }, 10_000_000)[1], []);
});
test("clear drops only the named terminal and kind", () => {
  let s = idleAt0();
  s = hold(s, note("a", "cao/t9"), info(), 1000);
  s = hold(s, note("r", "cao/t9"), info({ kind: "replied" }), 1100);
  s = hold(s, note("o", "cao/t7"), info({ terminalId: "t7" }), 1200);
  [s] = step(s, { type: "clear", terminalId: "t9", kind: "asking" }, 2000);
  assert.deepEqual(s.queue.map((q) => q.key).sort(), ["t7:asking", "t9:replied"]);
  assert.deepEqual(step(s, { type: "clear", terminalId: "ghost", kind: "asking" }, 2100)[0].queue.length, 2);
});
test("Later twice: held again with a fresh timer once, shown again by it, then dropped by the second Later", () => {
  let s = hold(idleAt0({ holdMaxSeconds: 120 }), note("b", "cao/t9"), info(), 1000);
  let fx;
  [s, fx] = handOff(s, 6000);
  assert.equal(shows(fx).length, 1);
  [s] = step(s, { type: "later", key: "t9:asking" }, 7000);
  assert.equal(s.queue.length, 1, "back in the hold queue");
  assert.deepEqual(step(s, { type: "tick" }, 7000 + 119_999)[1], [], "the timer restarted at the Later click");
  [s, fx] = step(s, { type: "tick" }, 7000 + 120_000);
  assert.equal(shows(fx).length, 1, "shown again by its fresh timer");
  [s] = step(s, { type: "later", key: "t9:asking" }, 128_000);
  assert.equal(s.queue.length, 0, "dropped on the second Later");
  assert.deepEqual(step(s, { type: "tick" }, 99_000_000)[1], []);
  assert.deepEqual(handOff(s, 99_000_000)[1], []);
});
test("Later puts the item at the end of the queue: the next hand-off shows the other asking item first", () => {
  let s = idleAt0();
  s = hold(s, note("first", "cao/t1"), info({ terminalId: "t1" }), 1000);
  s = hold(s, note("second", "cao/t2"), info({ terminalId: "t2" }), 2000);
  let fx;
  [s, fx] = handOff(s, 10_000);
  assert.equal(shows(fx)[0].notification.title, "first");
  [s] = step(s, { type: "later", key: "t1:asking" }, 11_000);
  [, fx] = handOff(s, 12_000);
  assert.equal(shows(fx)[0].notification.title, "second");
});
test("Later on a replied item holds it again with no timer, and a second Later drops it", () => {
  let s = hold(idleAt0(), note("r", "cao/t8"), info({ kind: "replied", terminalId: "t8" }), 1000);
  let fx;
  [s, fx] = handOff(s, 9000);
  [s] = step(s, { type: "later", key: shows(fx)[0].key }, 9500);
  assert.equal(s.queue.length, 1);
  assert.deepEqual(step(s, { type: "tick" }, 99_000_000)[1], []);
  [s, fx] = handOff(s, 100_000_000);
  assert.equal(shows(fx).length, 1);
  [s] = step(s, { type: "later", key: shows(fx)[0].key }, 100_000_500);
  assert.equal(s.queue.length, 0);
});
test("Unrelated notification: not CAO, the focused session, a recorded draft, or an idle window pass through unchanged", () => {
  const n = note("hello", "cao/t9");
  const cases = [["not CAO", info({ cao: false }), true], ["focused session", info({ session: "cao-a" }), true], ["recorded draft", info({ draft: true }), true], ["window not busy", info(), false], ["no info", undefined, true], ["null info", null, true]];
  for (const [name, i, busy] of cases) {
    const [s, fx] = step(idleAt0(), notify(n, i, busy), 0);
    assert.deepEqual(fx, [{ type: "show", notification: n }], name);
    assert.equal(s.queue.length, 0, name);
    assert.ok(!JSON.stringify(fx).includes(HONESTY.handOff), `${name}: the pass-through toast is not labelled as held`);
  }
});
test("a notification that passes through is returned unchanged and in the same state", () => {
  const base = idleAt0();
  const [s] = step(base, notify(note("x", "cao/t9"), info({ cao: false })), 5);
  assert.deepEqual(s, base);
});
test("pass-through configured (the shipped default): every notification is shown at once and nothing is held", () => {
  const base = initialToast({ passThrough: true });
  const n = note("b", "cao/t9");
  const [s, fx] = step(base, notify(n, info()), 0);
  assert.deepEqual(fx, [{ type: "show", notification: n }]);
  assert.equal(s.queue.length, 0);
  const [a] = step(s, focusEv("idle"), 0);
  assert.deepEqual(step(a, focusEv("working"), 60_000)[1], []);
  assert.deepEqual(step(a, { type: "tick" }, 99_000_000)[1], []);
});
test("release order: asking before replied, oldest first, one toast for each hand-off", () => {
  let s = idleAt0();
  s = hold(s, note("replied-old", "cao/t1"), info({ kind: "replied", terminalId: "t1" }), 1000);
  s = hold(s, note("ask-new", "cao/t2"), info({ terminalId: "t2" }), 2000);
  s = hold(s, note("ask-old", "cao/t3"), info({ terminalId: "t3" }), 1500);
  s = hold(s, note("replied-new", "cao/t4"), info({ kind: "replied", terminalId: "t4" }), 3000);
  const titles = [];
  for (let i = 0; i < 4; i++) {
    const [next, fx] = handOff(s, 10_000 + i * 10);
    assert.equal(shows(fx).length, 1, `hand-off ${i} shows exactly one toast`);
    titles.push(shows(fx)[0].notification.title);
    s = next;
  }
  assert.deepEqual(titles, ["ask-old", "ask-new", "replied-old", "replied-new"]);
  assert.equal(s.queue.length, 0);
});
test("several asking items due at once are all shown by ticks, oldest first, replied items never", () => {
  let s = idleAt0({ holdMaxSeconds: 10 });
  s = hold(s, note("a2", "cao/t2"), info({ terminalId: "t2" }), 2000);
  s = hold(s, note("a1", "cao/t1"), info({ terminalId: "t1" }), 1000);
  s = hold(s, note("r", "cao/t3"), info({ kind: "replied", terminalId: "t3" }), 500);
  const titles = [];
  for (let i = 0; i < 5; i++) { const [next, fx] = step(s, { type: "tick" }, 1_000_000 + i); s = next; titles.push(...shows(fx).map((e) => e.notification.title)); }
  assert.deepEqual(titles, ["a1", "a2"]);
  assert.deepEqual(s.queue.map((q) => q.key), ["t3:replied"]);
});
test("a repeated notification for the same terminal and kind is held once, keeping its place in line", () => {
  let s = idleAt0();
  s = hold(s, note("first", "cao/t9"), info(), 1000);
  s = hold(s, note("second", "cao/t9"), info(), 2000);
  assert.equal(s.queue.length, 1);
  const [, fx] = handOff(s, 10_000);
  assert.equal(shows(fx)[0].notification.title, "second", "the newest wording is shown");
  const both = hold(hold(idleAt0(), note("a", "cao/t9"), info(), 1), note("r", "cao/t9"), info({ kind: "replied" }), 2);
  assert.equal(both.queue.length, 2, "asking and replied for one terminal are two items");
});
test("a held item whose hold timer started at the first notification is not extended by a repeat", () => {
  let s = idleAt0({ holdMaxSeconds: 10 });
  s = hold(s, note("first", "cao/t9"), info(), 1000);
  s = hold(s, note("second", "cao/t9"), info(), 9000);
  assert.equal(shows(step(s, { type: "tick" }, 11_000)[1]).length, 1, "ten seconds after the first one");
});
test("toastStep ignores an unknown event and tolerates an unknown kind; it never mutates its state", () => {
  const s = idleAt0();
  assert.deepEqual(step(s, { type: "bogus" }, 1), [s, []]);
  assert.doesNotThrow(() => handOff(hold(s, note("x", "cao/t9"), info({ kind: "weird" }), 1), 9000));
  const frozen = freeze(hold(idleAt0(), note("b", "cao/t9"), info(), 1000));
  assert.doesNotThrow(() => { step(frozen, { type: "tick" }, 1_000_000); handOff(frozen, 9000); step(frozen, { type: "clear", terminalId: "t9", kind: "asking" }, 5); });
});
test("a thousand held items release the oldest asking one first, and quickly", () => {
  let s = idleAt0();
  for (let i = 999; i >= 0; i--) s = hold(s, note(`n${i}`, `cao/t${i}`), info({ terminalId: `t${i}` }), 1000 + i);
  const t0 = Date.now();
  const [, fx] = handOff(s, 20_000);
  assert.equal(shows(fx)[0].notification.title, "n0");
  assert.ok(Date.now() - t0 < 1000);
});
test("unicode and very long titles are carried through the hold unchanged", () => {
  const title = `é日本 ${"x".repeat(100_000)}`;
  const [, fx] = handOff(hold(idleAt0(), note(title, "cao/t9"), info(), 1), 9000);
  assert.equal(shows(fx)[0].notification.title, title);
});

// ---- more cases the first list missed ----
test("a worker first seen after the first snapshot is not stuck until quietMinutes after it was first seen", () => {
  let o = initialObs(0);
  o = observe(o, [worker("w1", "s", "c1", { state: "working" })], 0);
  o = observe(o, [worker("w1", "s", "c1", { state: "working" }), worker("w2", "s", "c1", { state: "working" })], 30 * MIN);
  const late = { workers: [worker("w2", "s", "c1", { state: "working" })] };
  assert.ok(nothing(quietHint(late, o, 10, 30 * MIN)), "just appeared: not quiet yet");
  assert.ok(nothing(quietHint(late, o, 10, 40 * MIN - 1)));
  const hint = quietHint(late, o, 10, 40 * MIN + 1);
  assert.deepEqual([hint?.text, hint?.terminalId, hint?.label], ["stuck?", "w2", HONESTY.heuristic]);
});
test("a terminal first seen late keeps its first-seen time when the same record is observed again", () => {
  let o = observe(initialObs(0), [worker("w1", "s", "c1", { state: "working" })], 0);
  o = observe(o, [worker("w1", "s", "c1", { state: "working" }), worker("w2", "s", "c1", { state: "working" })], 30 * MIN);
  o = observe(o, [worker("w1", "s", "c1", { state: "working" }), worker("w2", "s", "c1", { state: "working" })], 35 * MIN);
  assert.ok(quietHint({ workers: [worker("w2", "s", "c1", { state: "working" })] }, o, 10, 40 * MIN + 1), "the repeat did not restart its clock");
});
test("a terminal that is in the first snapshot still counts from the subscription, not from first sight", () => {
  const o = observe(initialObs(0), [worker("w1", "s", "c1", { state: "working" })], 0);
  const session = { workers: [worker("w1", "s", "c1", { state: "working" })] };
  assert.ok(nothing(quietHint(session, o, 10, 10 * MIN - 1)));
  assert.ok(quietHint(session, o, 10, 10 * MIN + 1));
});
test("toastStep takes a focus event with no terminal as no previous focus and does not throw", () => {
  let s = initialToast();
  let fx;
  assert.doesNotThrow(() => { [s, fx] = toastStep(s, { type: "focus", agentState: "idle", busy: false }, 0); });
  assert.deepEqual(fx, []);
});
test("a focus event with no terminal is not a hand-off, and a later working focus is not one either", () => {
  let [s] = step(initialToast(), { type: "focus", agentState: "idle", busy: false }, 0);
  s = hold(s, note("x", "cao/t9"), info(), 100);
  assert.deepEqual(step(s, { type: "focus", agentState: "working", busy: true }, 60_000)[1], [], "no terminal both times");
  assert.deepEqual(step(s, focusEv("working"), 60_000)[1], [], "the first focus on a terminal has no previous focus to hand off from");
  assert.equal(shows(handOff(step(s, focusEv("idle"), 61_000)[0], 70_000)[1]).length, 1, "a real hand-off still works afterwards");
});
test("a pushed value that arrives after the 5 s limit does not verify the write: the window goes local", () => {
  const pending = { mode: "write", pending: { id: "c1", turn: 10, at: 1000 } };
  assert.deepEqual(seenVerify(pending, { id: "c1", seen: 10 }, 7500), { mode: "local", pending: null });
  assert.deepEqual(seenVerify(pending, { id: "c1", seen: "10" }, 1000 + 5001), { mode: "local", pending: null });
  assert.deepEqual(seenVerify(pending, { id: "c1", seen: 10 }, 1000 + 4999), { mode: "write", pending: null }, "inside the limit it still verifies");
});
test("TOAST_PASS_THROUGH_DEFAULT is true (holding was never tried against a real Herdr)", () => {
  assert.equal(L.TOAST_PASS_THROUGH_DEFAULT, true);
});
test("shippedToastState passes every notification through, as the toast mod ships", () => {
  const n = note("b needs you", "cao/t9");
  for (const cfg of [undefined, {}, { holdMaxSeconds: 30 }]) {
    const [s, fx] = step(L.shippedToastState(cfg), notify(n, info()), 0);
    assert.deepEqual(fx, [{ type: "show", notification: n }], JSON.stringify(cfg));
    assert.equal(s.queue.length, 0);
  }
  const [a] = step(L.shippedToastState(), focusEv("idle"), 0);
  assert.deepEqual(step(a, focusEv("working"), 60_000)[1], []);
});
test("shippedToastState lets the configuration turn holding on and set the hold limit", () => {
  let s = step(L.shippedToastState({ passThrough: false, holdMaxSeconds: 30 }), focusEv("idle"), 0)[0];
  s = hold(s, note("b", "cao/t9"), info(), 1000);
  assert.equal(s.queue.length, 1, "held, not shown");
  assert.deepEqual(step(s, { type: "tick" }, 1000 + 29_999)[1], []);
  assert.equal(shows(step(s, { type: "tick" }, 1000 + 30_000)[1]).length, 1);
});
test("shippedToastState: a passThrough that is undefined or null still passes everything through", () => {
  const n = note("b needs you", "cao/t9");
  for (const passThrough of [undefined, null]) {
    const state = L.shippedToastState({ passThrough });
    assert.equal(state.cfg.passThrough, true, String(passThrough));
    const [s, fx] = step(state, notify(n, info()), 0);
    assert.deepEqual(fx, [{ type: "show", notification: n }], String(passThrough));
    assert.equal(s.queue.length, 0);
  }
});
test("shippedToastState: a holdMaxSeconds that is undefined or null keeps the default hold limit of 120 s", () => {
  for (const holdMaxSeconds of [undefined, null]) {
    const state = L.shippedToastState({ passThrough: false, holdMaxSeconds });
    assert.equal(state.cfg.holdMaxSeconds, 120, String(holdMaxSeconds));
    let s = step(state, focusEv("idle"), 0)[0];
    s = hold(s, note("b", "cao/t9"), info(), 1000);
    assert.deepEqual(step(s, { type: "tick" }, 1000 + 119_999)[1], [], String(holdMaxSeconds));
    assert.equal(shows(step(s, { type: "tick" }, 1000 + 120_000)[1]).length, 1, String(holdMaxSeconds));
  }
});
test("shippedToastState: an explicit passThrough false turns holding on, and true stays pass-through", () => {
  assert.equal(L.shippedToastState({ passThrough: false }).cfg.passThrough, false);
  assert.equal(L.shippedToastState({ passThrough: true }).cfg.passThrough, true);
  const [s, fx] = step(L.shippedToastState({ passThrough: false }), notify(note("b", "cao/t9"), info()), 0);
  assert.deepEqual(fx, []);
  assert.equal(s.queue.length, 1);
});
test("writesDisabled is true when any of the views passed is in the auth state", () => {
  const ok = { data: [1], at: 1 };
  const auth = { data: [], at: 1, auth: true };
  assert.equal(writesDisabled(auth, ok, ok), true, "a 401 on flows disables the other sections' writes");
  assert.equal(writesDisabled(ok, ok, auth), true);
  assert.equal(writesDisabled(ok, ok, ok), false);
  assert.equal(writesDisabled(ok, undefined, null), false, "a view not loaded yet is not an auth failure");
  assert.equal(writesDisabled(undefined, auth), true);
  assert.equal(writesDisabled(), false);
});
test("writesDisabled with several views: an approval refusal or an unreachable view disables nothing", () => {
  const approval = applyOutcome({ data: [1], at: 1 }, { kind: "approval_required", status: 403, planId: "wf:abc" }, 2);
  const down = applyOutcome({ data: [1], at: 1 }, { kind: "unreachable" }, 2);
  assert.equal(writesDisabled(approval, down, { data: [] }), false);
  assert.equal(writesDisabled(approval, applyOutcome({}, { kind: "auth", status: 401 }, 3)), true);
});
test("Asking kind: a kind with spaces or newlines around it is trimmed", () => {
  assert.equal(askingKind({ state: "blocked", kind: " permission " }), "permission");
  assert.equal(askingKind({ state: "blocked", kind: "\tauth\n" }), "auth");
});

// ---- honesty: the surfaces of these rules are produced by the registered functions ----
const MINE = ["bestGuess", "stuckHint", "contextPercent", "knownGap", "recap", "startedByFlow", "releasedToast"];
test("the session rules and the toast are built: their seven surfaces are no longer pending", () => {
  for (const surface of MINE) assert.ok(!Object.hasOwn(HONESTY_PENDING, surface), `${surface} is still listed as pending`);
});
test("the audit calls classify, quietHint, contextPercent, knownGapNote, parseRecap, flowOfSession and toastStep and finds each label", async () => {
  const r = await auditHonesty({ expected: MINE, honesty: HONESTY, sources: HONESTY_SOURCES, pending: {}, load: (m) => import(new URL(m, import.meta.url).href) });
  assert.deepEqual(r.problems, []);
});

// =====================================================================================================
// Decisions the mods share: holdModel, sharedCache, rowShowsReplied, repaintDue, readOfThisTurn and
// shownBlock. The cases come from the intended behaviour (failure states keep the last good
// view, the session order and needs-you marking, the session details, the fallback to CAO when
// labels are absent), not from the code of these functions.
// =====================================================================================================

// A model as the mods build it: buildModel over raw terminal records, no CAO machine description needed.
const modelOf = (records, ctx = {}) => buildModel(records, [], { machine: "cao" }, { ...CTX, ...ctx });
// Two sessions. alpha: a conductor and a worker, both at a finished turn 5 that herdr.seen 2 has not seen.
// beta: a working conductor and a working worker. alpha sorts first (replied) and beta second.
const heldBefore = (ctx = {}) => modelOf([
  raw("c1", "alpha", { status: "done", turn: 5, labels: { "herdr.seen": "2" } }),
  raw("w1", "alpha", { profile: "developer", caller: "c1", status: "done", turn: 5, labels: { "herdr.seen": "2" } }),
  raw("c2", "beta", { status: "working" }),
  raw("w2", "beta", { profile: "developer", caller: "c2", status: "working" }),
], ctx);
// What the mods draw for a terminal row: the display state with the replied mark gated by rowShowsReplied.
const drawnState = (m, x) => {
  const s = m.sessions.find((y) => y.terms.includes(x));
  return displayState(x, { replied: rowShowsReplied(s.talkable.includes(x), m.replied(x)), quiet: false });
};
const termById = (m, id) => m.terms.find((x) => x.id === id);

// ---- holdModel (Failure states: "keep the last list dimmed"; Pins: "an empty or disconnected snapshot") ----
test("holdModel: a machine that is away with an empty snapshot keeps drawing the previous list, dimmed", () => {
  const prev = freeze(heldBefore());
  const away = freeze(modelOf([]));
  const r = holdModel(prev, away, false);
  assert.equal(r.dimmed, true);
  assert.deepEqual(r.model.sessions.map((s) => s.name), ["alpha", "beta"]);
  assert.equal(r.model.terms.length, 4);
  assert.equal(r.model.connected, false, "the Sessions header reads connected from the model, to say the machine is away");
  assert.equal(headerNote({ connected: r.model.connected }), "CAO machine not connected");
});
test("holdModel: a held list never counts as '0 sessions' in the status bar", () => {
  const r = holdModel(freeze(heldBefore()), freeze(modelOf([])), false);
  const bar = barText(statusCounts(r.model.sessions, r.model.replied));
  assert.ok(bar.startsWith(`cao ${DOT} 2 sessions`), bar);
  assert.ok(!bar.includes("0 sessions"), bar);
});
test("holdModel: a connected machine with an empty snapshot shows the empty state, not dimmed", () => {
  const r = holdModel(freeze(heldBefore()), freeze(modelOf([])), true);
  assert.equal(r.dimmed, false);
  assert.deepEqual(r.model.sessions, []);
  assert.deepEqual(r.model.terms, []);
  assert.equal(r.model.connected, true);
  assert.ok(barText(statusCounts(r.model.sessions, r.model.replied)).startsWith(`cao ${DOT} 0 sessions`));
});
test("holdModel: with no previous list there is nothing to hold, so an empty snapshot is drawn empty", () => {
  for (const previous of [null, undefined]) {
    const r = holdModel(previous, freeze(modelOf([])), false);
    assert.deepEqual(r.model.sessions, [], String(previous));
    assert.deepEqual(r.model.terms, [], String(previous));
    assert.equal(r.model.connected, false, String(previous));
  }
});
test("holdModel: a previous model without sessions is not held either; the snapshot is drawn", () => {
  const stale = freeze(buildModel([], [], { machine: "old-machine" }, CTX));
  const snap = freeze(modelOf([]));
  assert.equal(stale.machine, "old-machine");
  const r = holdModel(stale, snap, false);
  assert.equal(r.model.machine, "cao", "the snapshot's own model is drawn, not an empty list that was never shown");
});
test("holdModel: a previous model with sessions is still held for an empty snapshot while the machine is away", () => {
  const prev = freeze(heldBefore());
  const r = holdModel(prev, freeze(modelOf([])), false);
  assert.equal(r.dimmed, true);
  assert.equal(r.model.sessions.length, 2);
  assert.equal(r.model.machine, prev.machine);
});
test("holdModel: a non-empty snapshot is drawn as it is, dimmed only while the machine is away", () => {
  const prev = freeze(heldBefore());
  const snap = freeze(modelOf([raw("c9", "gamma", { status: "working" })]));
  for (const connected of [true, false]) {
    const r = holdModel(prev, snap, connected);
    assert.deepEqual(r.model.sessions.map((s) => s.name), ["gamma"], `connected ${connected}: the previous list is not mixed in`);
    assert.equal(r.model.connected, connected);
    assert.equal(r.dimmed, !connected, `connected ${connected}`);
  }
});
test("holdModel: with no previous list, a non-empty snapshot is drawn the same way", () => {
  const snap = freeze(modelOf([raw("c9", "gamma", { status: "working" })]));
  assert.deepEqual(holdModel(null, snap, true), { model: { ...snap, connected: true }, dimmed: false });
  assert.deepEqual(holdModel(undefined, snap, false), { model: { ...snap, connected: false }, dimmed: true });
});
test("holdModel: the held list carries the snapshot's current pins, local map and replied test", () => {
  const prev = freeze(heldBefore({ pins: ["old"], local: {} }));
  const snap = freeze(modelOf([], { pins: ["w2"], local: { c1: 5 } }));
  const r = holdModel(prev, snap, false);
  assert.deepEqual(r.model.pins, ["w2"], "not the pins the list was stored with");
  assert.deepEqual(r.model.local, { c1: 5 });
  const c1 = termById(r.model, "c1");
  assert.equal(prev.replied(c1), true, "the stored list says replied");
  assert.equal(r.model.replied(c1), false, "the current seen map says it was looked at");
  // the rest of the held list is the previous one
  assert.deepEqual(r.model.sessions.map((s) => s.name), ["alpha", "beta"]);
  assert.equal(r.model.machine, prev.machine);
  assert.deepEqual(r.model.props, prev.props);
  assert.equal(r.dimmed, true);
});
test("holdModel: a pin click while the list is held shows at once, as an agent tab", () => {
  const prev = freeze(heldBefore({ pins: [] }));
  const beta = (m) => m.sessions.find((s) => s.name === "beta");
  assert.deepEqual(tabsFor(beta(prev), prev.pins).map((x) => x.id), ["c2"], "one shown agent: its one tab");
  const clicked = freeze(modelOf([], { pins: ["w2"] }));
  const r = holdModel(prev, clicked, false);
  assert.deepEqual(tabsFor(beta(r.model), r.model.pins).map((x) => x.id), ["c2", "w2"]);
});
test("holdModel: focusing a replied agent while the list is held clears its Replied mark at once, and a worker never had one", () => {
  const prev = freeze(heldBefore());
  const noMark = holdModel(prev, freeze(modelOf([])), false).model;
  assert.equal(drawnState(noMark, termById(noMark, "c1")), "replied");
  assert.equal(drawnState(noMark, termById(noMark, "w1")), "done", "a worker at the same unseen turn is not drawn replied");
  assert.equal(statusCounts(noMark.sessions, noMark.replied).replied, 1);
  const marked = holdModel(prev, freeze(modelOf([], { local: { c1: 5, w1: 5 } })), false).model;
  assert.equal(drawnState(marked, termById(marked, "c1")), "done");
  assert.equal(statusCounts(marked.sessions, marked.replied).replied, 0);
});
test("holdModel: neither input is changed, and the held model is a new object", () => {
  const prev = freeze(heldBefore());
  const snap = freeze(modelOf([], { pins: ["w2"] }));
  const r = holdModel(prev, snap, false);
  assert.notEqual(r.model, prev);
  assert.ok(!("connected" in prev), "the stored list is not stamped");
  assert.deepEqual(prev.pins, []);
});

// ---- sharedCache (it caches agent_profile and caller_id only; an empty snapshot prunes nothing) ----
const answered = (profile, caller, extra = {}) => ({ state: "ok", agent_profile: profile, caller_id: caller, description: "free text from the server", ...extra });
test("sharedCache: an answered entry is published with only state, agent_profile and caller_id", () => {
  const { published } = sharedCache(freeze({ t1: answered("conductor", null), t2: answered("developer", "t1", { at: 7, secret: "x" }) }), ["t1", "t2"]);
  assert.deepEqual(published, {
    t1: { state: "ok", agent_profile: "conductor", caller_id: null },
    t2: { state: "ok", agent_profile: "developer", caller_id: "t1" },
  });
  for (const id of ["t1", "t2"]) assert.deepEqual(Object.keys(published[id]).sort(), ["agent_profile", "caller_id", "state"], id);
  assert.ok(!JSON.stringify(published).includes("free text"), "the CAO description is never published");
});
test("sharedCache: an entry that is not yet answered is not published (pending, failed), and its id gets no key", () => {
  const cache = freeze({ t1: { state: "pending" }, t2: { state: "failed", at: 5 }, t3: answered("conductor", null) });
  const { published } = sharedCache(cache, ["t1", "t2", "t3"]);
  assert.deepEqual(Object.keys(published), ["t3"]);
});
test("sharedCache: a failed entry stays in the cache with its time, so the 30 s retry wait still holds", () => {
  const { cache } = sharedCache(freeze({ t2: { state: "failed", at: 5 } }), ["t2"]);
  assert.deepEqual(cache.t2, { state: "failed", at: 5 });
  const terms = [unlabelled("t2")];
  assert.deepEqual(fallbackPlan(cache, terms, 5 + 29_999), []);
  assert.deepEqual(fallbackPlan(cache, terms, 5 + 30_000), ["t2"]);
});
test("sharedCache: entries are pruned to the terminals of a non-empty snapshot, in the cache and in what is published", () => {
  const cache = freeze({ t1: answered("conductor", null), t2: answered("developer", "t1"), t3: { state: "failed", at: 1 } });
  const r = sharedCache(cache, ["t1", "t9"]);
  assert.deepEqual(Object.keys(r.cache), ["t1"]);
  assert.deepEqual(Object.keys(r.published), ["t1"]);
});
test("sharedCache: an empty snapshot prunes nothing (a reconnect must not refetch every terminal)", () => {
  const cache = freeze({ t1: answered("conductor", null), t2: answered("developer", "t1"), t3: { state: "failed", at: 1 } });
  for (const live of [[], undefined, null]) {
    const r = sharedCache(cache, live);
    assert.deepEqual(Object.keys(r.cache).sort(), ["t1", "t2", "t3"], JSON.stringify(live));
    assert.deepEqual(Object.keys(r.published).sort(), ["t1", "t2"], JSON.stringify(live));
  }
});
test("sharedCache: a finished fetch is added to the cache and published, and replaces an older failed entry", () => {
  const cache = freeze({ t1: { state: "failed", at: 1 } });
  const r = sharedCache(cache, ["t1"], freeze({ t1: answered("developer", "c1") }));
  assert.deepEqual(r.published, { t1: { state: "ok", agent_profile: "developer", caller_id: "c1" } });
  assert.equal(r.cache.t1.state, "ok");
});
test("sharedCache: a fetch that finishes while the snapshot is empty is kept, so the id is fetched once while it lives", () => {
  const r = sharedCache({}, [], freeze({ t1: answered("conductor", null) }));
  assert.deepEqual(Object.keys(r.published), ["t1"]);
  assert.deepEqual(fallbackPlan(r.cache, [unlabelled("t1")], 999_999), [], "not asked for again");
});
test("sharedCache: a fetch for a terminal that is gone from a non-empty snapshot is dropped", () => {
  const r = sharedCache({}, ["t2"], freeze({ t1: answered("conductor", null) }));
  assert.deepEqual([r.cache, r.published], [{}, {}]);
});
test("sharedCache: no cache and no fetches give two empty maps", () => {
  for (const cache of [undefined, null, {}]) assert.deepEqual(sharedCache(cache, ["t1"]), { cache: {}, published: {} }, String(cache));
});
test("sharedCache: what it publishes classifies the same terminals as the full cache does", () => {
  const cache = freeze({ t1: answered("conductor", null), t2: answered("developer", "t1") });
  const terms = [unlabelled("t1"), unlabelled("t2")];
  const { cache: kept, published } = sharedCache(cache, ["t1", "t2"]);
  const view = (c) => withFallback(terms, c).map((x) => [x.id, x.profile, x.caller]);
  assert.deepEqual(view(published), view(kept));
  assert.deepEqual(view(published), [["t1", "conductor", undefined], ["t2", "developer", "t1"]]);
});

// ---- rowShowsReplied (a worker never draws the replied shape) ----
test("rowShowsReplied: only a talkable agent that has replied shows the mark", () => {
  assert.equal(rowShowsReplied(true, true), true);
  assert.equal(rowShowsReplied(true, false), false, "talkable but already seen");
  assert.equal(rowShowsReplied(false, true), false, "a worker at an unseen turn");
  assert.equal(rowShowsReplied(false, false), false);
});
test("rowShowsReplied: a worker at an unseen finished turn draws done, its conductor draws replied", () => {
  const m = heldBefore();
  assert.equal(m.replied(termById(m, "w1")), true, "the unseen-turn test alone would mark the worker");
  assert.equal(drawnState(m, termById(m, "c1")), "replied");
  assert.equal(drawnState(m, termById(m, "w1")), "done");
  const idle = modelOf([raw("c1", "s", { status: "idle", turn: 3, labels: { "herdr.seen": "1" } }), raw("w1", "s", { profile: "developer", caller: "c1", status: "idle", turn: 3, labels: { "herdr.seen": "1" } })]);
  assert.deepEqual([drawnState(idle, termById(idle, "c1")), drawnState(idle, termById(idle, "w1"))], ["replied", "idle"]);
});

// ---- repaintDue (the details panel repaints once per period; it used to repaint every second) ----
test("repaintDue: due once a whole period has passed, including exactly at the period", () => {
  assert.equal(repaintDue(1000, 1000 + 30_000, 30_000), true, "exactly equal");
  assert.equal(repaintDue(1000, 1000 + 30_001, 30_000), true);
  assert.equal(repaintDue(1000, 1000 + 29_999, 30_000), false, "one millisecond short");
  assert.equal(repaintDue(1000, 1000, 30_000), false, "just painted");
  assert.equal(repaintDue(1000, 1000 + 1000, 30_000), false, "one tick later");
});
test("repaintDue: a caller that stamps paintedAt when it repaints repaints once per period, not every second", () => {
  const start = 1_700_000_000_000;
  let paintedAt = start;
  const painted = [];
  for (let sec = 1; sec <= 95; sec += 1) {
    const now = start + sec * 1000;
    if (repaintDue(paintedAt, now, 30_000)) { paintedAt = now; painted.push(sec); }
  }
  assert.deepEqual(painted, [30, 60, 90]);
});
test("repaintDue: a clock that steps back is not due, and a painted-at of 0 (long ago) is due", () => {
  assert.equal(repaintDue(100_000, 40_000, 30_000), false, "a step back of more than a period is not a period passed");
  assert.equal(repaintDue(0, 1_700_000_000_000, 30_000), true);
});
test("repaintDue: a period of 0 is due on every tick, even in the same millisecond", () => {
  assert.equal(repaintDue(5, 5, 0), true);
  assert.equal(repaintDue(5, 6, 0), true);
});

// ---- readOfThisTurn (Session details: the recap and context show "after its turn ends"; "No recap": nothing is inferred) ----
const READ = { turn: 5, recap: { text: "Tests pass; waiting for review.", label: "the agent's own words" }, context: null };
test("readOfThisTurn: the read is returned for a done or idle terminal at the turn it was read", () => {
  for (const state of ["done", "idle"]) assert.deepEqual(readOfThisTurn(T("c1", "s", { state, turn: 5 }), READ), READ, state);
});
test("readOfThisTurn: nothing while the terminal works, is blocked, errored or unknown, even at the same turn", () => {
  for (const state of ["working", "blocked", "error", "unknown", "starting"]) {
    assert.equal(readOfThisTurn(T("c1", "s", { state, turn: 5 }), READ), null, state);
  }
});
test("readOfThisTurn: a read of another turn is not shown, one turn behind or ahead", () => {
  for (const state of ["done", "idle"]) {
    assert.equal(readOfThisTurn(T("c1", "s", { state, turn: 6 }), READ), null, `${state}: read is a turn behind`);
    assert.equal(readOfThisTurn(T("c1", "s", { state, turn: 4 }), READ), null, `${state}: read is a turn ahead`);
  }
});
test("readOfThisTurn: a missing read is null, not undefined", () => {
  const done = T("c1", "s", { state: "done", turn: 0 });
  for (const read of [undefined, null, {}]) assert.equal(readOfThisTurn(done, read), null, JSON.stringify(read));
  assert.equal(readOfThisTurn(T("c1", "s", { state: "working", turn: 0 }), undefined), null);
});
test("readOfThisTurn: the recap of one turn is not shown during the next, and is read again at its end", () => {
  const read3 = { turn: 3, recap: parseRecap("log\n\u203b recap: Tests pass; waiting for review.\n"), context: null };
  const at = (state, turn) => T("c1", "s", { state, turn });
  assert.equal(readOfThisTurn(at("done", 3), read3).recap.text, "Tests pass; waiting for review.");
  assert.equal(readOfThisTurn(at("working", 3), read3), null, "the agent is working again");
  assert.equal(readOfThisTurn(at("working", 4), read3), null);
  assert.equal(readOfThisTurn(at("done", 4), read3), null, "the old recap is not shown for the new turn");
  const read4 = { turn: 4, recap: parseRecap("log\nno recap line here\n"), context: null };
  assert.equal(readOfThisTurn(at("done", 4), read4).recap, null, "a turn with no recap line shows no recap block");
});

// ---- shownBlock (a hidden block fetches nothing; a hidden sidebar hides everything in it) ----
const blk = (id, over = {}) => ({ id, type: "block", ...over });
const col = (id, children, over = {}) => ({ id, type: "column", children, ...over });
test("shownBlock: finds the root itself, and a block nested at any depth, and returns that node", () => {
  const target = blk("target", { size: { px: 34 } });
  const tree = freeze(col("root", [blk("a"), col("b", [blk("c"), col("d", [target])])]));
  assert.equal(shownBlock(tree, "root"), tree);
  assert.equal(shownBlock(tree, "target"), target);
  assert.equal(shownBlock(tree, "target").size.px, 34);
  assert.equal(shownBlock(tree, "c").id, "c");
});
test("shownBlock: a block that is hidden is not shown", () => {
  const tree = freeze(col("root", [blk("a", { hidden: true }), blk("b")]));
  assert.equal(shownBlock(tree, "a"), null);
  assert.equal(shownBlock(tree, "b").id, "b");
  assert.equal(shownBlock(freeze(blk("only", { hidden: true })), "only"), null, "a hidden root");
});
test("shownBlock: a block inside a hidden ancestor is not shown, at any depth, even when it is not hidden itself", () => {
  const tree = freeze(col("root", [col("side", [col("mid", [blk("deep")])], { hidden: true }), blk("other")]));
  assert.equal(shownBlock(tree, "deep"), null);
  assert.equal(shownBlock(tree, "mid"), null);
  assert.equal(shownBlock(tree, "side"), null);
  assert.equal(shownBlock(tree, "other").id, "other", "a hidden branch does not hide its siblings");
});
test("shownBlock: hidden false or absent is shown; only a hidden block's own subtree is cut", () => {
  const tree = freeze(col("root", [blk("a", { hidden: false }), col("b", [blk("c")], { hidden: false })], { hidden: false }));
  assert.equal(shownBlock(tree, "a").id, "a");
  assert.equal(shownBlock(tree, "c").id, "c");
});
test("shownBlock: when the same id is in a hidden copy and a shown copy, the shown one is found, whichever comes first", () => {
  const hiddenCopy = col("side-a", [blk("sessions", { size: { px: 1 } })], { hidden: true });
  const shownCopy = col("side-b", [blk("sessions", { size: { px: 2 } })]);
  assert.equal(shownBlock(freeze(col("root", [hiddenCopy, shownCopy])), "sessions").size.px, 2);
  assert.equal(shownBlock(freeze(col("root", [shownCopy, hiddenCopy])), "sessions").size.px, 2);
  assert.equal(shownBlock(freeze(col("root", [hiddenCopy, hiddenCopy])), "sessions"), null);
});
test("shownBlock: an id that is not in the tree is null", () => {
  const tree = freeze(col("root", [blk("a"), col("b", [blk("c")])]));
  for (const id of ["nope", "", undefined, null]) assert.equal(shownBlock(tree, id), null, String(id));
});
test("shownBlock: a missing or malformed tree is null, and a node with odd children does not throw", () => {
  for (const node of [null, undefined, "root", 7, true]) assert.equal(shownBlock(node, "root"), null, String(node));
  const odd = freeze({ id: "root", children: { id: "a" } });
  assert.doesNotThrow(() => shownBlock(odd, "a"));
  assert.equal(shownBlock(odd, "a"), null);
  assert.equal(shownBlock(freeze({ id: "root", children: null }), "a"), null);
  assert.equal(shownBlock(freeze({ id: "root", children: "abc" }), "a"), null);
  assert.equal(shownBlock(freeze(col("root", [null, undefined, 3, blk("a")])), "a").id, "a", "junk children are skipped");
});
test("shownBlock: reads the real cao-window layout: the tab row and the sessions section are shown, the hidden files panel and the right sidebar are not", () => {
  const layout = JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", "layouts", "cao-window.json"), "utf8"));
  assert.equal(shownBlock(layout, "cao-window-tabs").size.px, 34);
  assert.equal(shownBlock(layout, "cao-sessions").id, "cao-sessions");
  assert.equal(shownBlock(layout, "panel"), null, "the files panel starts hidden");
  assert.equal(shownBlock(layout, "files"), null, "and so does everything inside it");
  assert.ok(JSON.stringify(layout).includes('"id":"cao-window-panel"'), "the right sidebar is in the layout");
  assert.equal(shownBlock(layout, "cao-window-panel"), null, "the right sidebar starts hidden, so it is not a shown block");
});

// ---- the keys of a confirm dialog ----
// The contract is that discarding a filled form asks first and that a YOLO launch needs its typed
// confirmation, plus each function's comment in lib/logic.js; a case says which default it pins.
const CONFIRM = freeze({ confirmEnabled: true });
const CONFIRM_OFF = freeze({ confirmEnabled: false });

test("dialogKey: Esc cancels from wherever the focus is, even when Confirm is disabled", () => {
  for (const focus of ["cancel", "confirm", "input"]) {
    for (const key of ["Escape", "Esc", "escape", "ESC"]) {
      assert.deepEqual(dialogKey({ focus }, CONFIRM, key), { focus, action: "cancel" }, `${focus} ${key}`);
      assert.deepEqual(dialogKey({ focus }, CONFIRM_OFF, key), { focus, action: "cancel" }, `${focus} ${key} disabled`);
    }
  }
});

test("dialogKey: Enter acts on what has the focus: Cancel cancels, an enabled Confirm confirms, a disabled one does nothing", () => {
  for (const enter of ["Enter", "enter", "Return", "return", "ENTER"]) {
    assert.deepEqual(dialogKey({ focus: "cancel" }, CONFIRM, enter), { focus: "cancel", action: "cancel" }, enter);
    assert.deepEqual(dialogKey({ focus: "cancel" }, CONFIRM_OFF, enter), { focus: "cancel", action: "cancel" }, `${enter}, Confirm disabled`);
    assert.deepEqual(dialogKey({ focus: "confirm" }, CONFIRM, enter), { focus: "confirm", action: "confirm" }, enter);
    assert.deepEqual(dialogKey({ focus: "confirm" }, CONFIRM_OFF, enter), { focus: "confirm", action: null }, `${enter}, Confirm disabled`);
  }
});

test("YOLO typed confirmation: Enter in the typed field confirms only while the typed name matches (the confirm button is disabled otherwise)", () => {
  assert.deepEqual(dialogKey({ focus: "input" }, CONFIRM, "Enter"), { focus: "input", action: "confirm" });
  assert.deepEqual(dialogKey({ focus: "input" }, CONFIRM_OFF, "Enter"), { focus: "input", action: null });
  assert.deepEqual(dialogKey({ focus: "input" }, CONFIRM_OFF, "Escape"), { focus: "input", action: "cancel" });
});

test("dialogKey: Tab and the arrows move the focus between the two buttons, and do no action", () => {
  for (const key of ["Tab", "tab", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Left", "Right", "Up", "Down", "left", "DOWN"]) {
    assert.deepEqual(dialogKey({ focus: "cancel" }, CONFIRM, key), { focus: "confirm", action: null }, `cancel ${key}`);
    assert.deepEqual(dialogKey({ focus: "confirm" }, CONFIRM, key), { focus: "cancel", action: null }, `confirm ${key}`);
    assert.deepEqual(dialogKey({ focus: "confirm" }, CONFIRM_OFF, key), { focus: "cancel", action: null }, `confirm (disabled) ${key}`);
  }
});

test("dialogKey: from the typed field, a move lands on Cancel, never straight on the dangerous Confirm", () => {
  for (const key of ["Tab", "ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp"]) {
    assert.deepEqual(dialogKey({ focus: "input" }, CONFIRM, key), { focus: "cancel", action: null }, key);
  }
});

test("dialogKey: any other key leaves the focus where it is and does nothing, so typing in the field is left to the field", () => {
  for (const focus of ["cancel", "confirm", "input"]) {
    for (const key of ["a", "Z", " ", "Backspace", "Shift", "Delete", "PageDown", "Home", "End", "", null, undefined, 7, "Entering", "Tabs", "DownArrow", "ArrowArrowDown"]) {
      assert.deepEqual(dialogKey({ focus }, CONFIRM, key), { focus, action: null }, `${focus} ${JSON.stringify(key)}`);
    }
  }
});

test("dialogKey: a state it does not understand starts on Cancel, so Enter can never confirm from it", () => {
  for (const state of [undefined, null, {}, { focus: "" }, { focus: "bogus" }, { focus: "CONFIRM" }, { focus: 7 }, "confirm"]) {
    assert.deepEqual(dialogKey(state, CONFIRM, "Enter"), { focus: "cancel", action: "cancel" }, JSON.stringify(state));
    assert.deepEqual(dialogKey(state, CONFIRM, "Tab"), { focus: "confirm", action: null }, JSON.stringify(state));
  }
});

test("dialogKey: a confirm is never the answer to Cancel focus, a disabled Confirm, or a key that is not Enter", () => {
  const keys = ["Enter", "Return", "Escape", "Tab", "ArrowRight", "a", " ", "", null, undefined];
  for (const focus of ["cancel", "confirm", "input", "bogus", undefined]) {
    for (const spec of [CONFIRM, CONFIRM_OFF]) {
      for (const key of keys) {
        const r = dialogKey(focus === undefined ? undefined : { focus }, spec, key);
        if (r.action !== "confirm") continue;
        const enter = ["Enter", "Return"].includes(key);
        assert.ok(enter && spec.confirmEnabled === true && (focus === "confirm" || focus === "input"), `${focus} ${JSON.stringify(key)} ${spec.confirmEnabled}`);
      }
    }
  }
});

test("dialogKey: a walk through a dialog: Tab to Confirm, Tab back, Enter cancels; the state and spec it is given are not changed", () => {
  const spec = freeze({ confirmEnabled: true });
  let st = freeze({ focus: "cancel" });
  const a = dialogKey(st, spec, "Tab");
  assert.equal(a.focus, "confirm");
  assert.notEqual(a, st);
  assert.equal(st.focus, "cancel");
  const b = dialogKey(a, spec, "Tab");
  assert.equal(b.focus, "cancel");
  assert.deepEqual(dialogKey(b, spec, "Enter"), { focus: "cancel", action: "cancel" });
  assert.deepEqual(dialogKey(a, spec, "Enter"), { focus: "confirm", action: "confirm" });
});


// ---- Confirm needs confirmEnabled to be true (YOLO safety): a spec that does not say so never confirms ----
test("dialogKey: Enter on Confirm or in the typed field confirms only when confirmEnabled is true", () => {
  for (const focus of ["confirm", "input"]) {
    for (const enter of ["Enter", "Return", "enter"]) {
      assert.deepEqual(dialogKey({ focus }, { confirmEnabled: true }, enter), { focus, action: "confirm" }, `${focus} ${enter}`);
    }
  }
});

test("dialogKey: a spec without confirmEnabled, or with anything but true, never confirms on Enter", () => {
  const specs = [undefined, null, {}, "yes", 7, true, [], { confirmEnabled: undefined }, { confirmEnabled: null }, { confirmEnabled: false },
    { confirmEnabled: "yes" }, { confirmEnabled: "true" }, { confirmEnabled: 1 }, { confirmEnabled: 0 }, { confirmEnabled: "" }, { confirmEnabled: [] }, { confirmEnabled: {} }, { confirmEnabled: [true] }, { enabled: true }, { ConfirmEnabled: true }];
  for (const focus of ["confirm", "input"]) {
    for (const spec of specs) {
      for (const enter of ["Enter", "Return"]) assert.deepEqual(dialogKey({ focus }, spec, enter), { focus, action: null }, `${focus} ${enter} ${JSON.stringify(spec)}`);
    }
  }
});

test("dialogKey: without confirmEnabled true, Cancel and Esc still work and nothing else confirms", () => {
  for (const spec of [undefined, null, {}, { confirmEnabled: "yes" }, { confirmEnabled: 1 }, { confirmEnabled: false }]) {
    assert.deepEqual(dialogKey({ focus: "cancel" }, spec, "Enter"), { focus: "cancel", action: "cancel" }, JSON.stringify(spec));
    for (const focus of ["cancel", "confirm", "input"]) assert.equal(dialogKey({ focus }, spec, "Escape").action, "cancel", `${focus} ${JSON.stringify(spec)}`);
    for (const focus of ["cancel", "confirm", "input", undefined]) {
      for (const key of ["Enter", "Return", "Escape", "Tab", "ArrowRight", "a", " ", "", null, undefined]) {
        const r = dialogKey(focus === undefined ? undefined : { focus }, spec, key);
        assert.notEqual(r.action, "confirm", `${focus} ${JSON.stringify(key)} ${JSON.stringify(spec)}`);
      }
    }
  }
});

test("dialogKey: a spec that is a frozen object, or gets its flag later, is read at the key press", () => {
  const spec = { confirmEnabled: false };
  assert.equal(dialogKey({ focus: "input" }, spec, "Enter").action, null);
  spec.confirmEnabled = true;
  assert.equal(dialogKey({ focus: "input" }, spec, "Enter").action, "confirm", "the typed name now matches");
  assert.equal(dialogKey({ focus: "input" }, freeze({ confirmEnabled: true }), "Enter").action, "confirm");
});

// ---- typed profile text counts as typed ----
test("unchosenProfileText: text in the profile field that is not the profile the form holds is something typed", () => {
  assert.equal(unchosenProfileText("dev", ""), true, "typed, nothing chosen");
  assert.equal(unchosenProfileText("dev", "developer"), true, "typed over a chosen profile");
  assert.equal(unchosenProfileText("Developer", "developer"), true, "case differs: not the chosen name");
  assert.equal(unchosenProfileText("developer ", "developer"), true, "a trailing space was typed");
  assert.equal(unchosenProfileText(" ", ""), true, "only a space was typed");
  assert.equal(unchosenProfileText("日本", ""), true);
  assert.equal(unchosenProfileText("x".repeat(100000), "x"), true);
});

test("unchosenProfileText: the chosen name shown in the field, or an empty field with nothing chosen, is not typed text", () => {
  assert.equal(unchosenProfileText("developer", "developer"), false);
  assert.equal(unchosenProfileText("", ""), false);
  for (const nothing of [null, undefined]) {
    assert.equal(unchosenProfileText(nothing, ""), false, `${nothing} text, nothing chosen`);
    assert.equal(unchosenProfileText("", nothing), false, `empty text, ${nothing} chosen`);
    assert.equal(unchosenProfileText(nothing, nothing), false);
  }
  assert.equal(unchosenProfileText(undefined, "developer"), true, "the field says nothing but a profile is chosen: not the same");
  assert.equal(unchosenProfileText("日本", "日本"), false);
});

// ---- an agent that waits on its workers shows as waiting ----
//
// CAO keeps the label `cao.waiting` (a count of open delegation edges) on the caller; the window reads it from the record's labels.
const waitingRec = (value, over = {}) => ({ address: "cao/t1", labels: { "cao.terminal_id": "t1", "cao.session": "cao-a", "cao.waiting": value }, status: "idle", ...over });

test("termOf reads cao.waiting as a count: a plain decimal integer from 1 to 999, in either label form", () => {
  for (const [value, count] of [["1", 1], ["2", 2], ["10", 10], ["999", 999], ["0", 0]]) assert.equal(termOf(waitingRec(value)).waiting, count, value);
  assert.equal(termOf({ address: "cao/t1", labels: ["cao.terminal_id=t1", "cao.session=cao-a", "cao.waiting=3"], status: "idle" }).waiting, 3, "array form");
  assert.equal(termOf({ address: "cao/t1", labels: ["cao.terminal_id=t1", "cao.session=cao-a", "cao.waiting=abc"], status: "idle" }).waiting, 0, "array form, malformed");
});
test("termOf: a missing label, text, negative, fractional, signed, spaced, zero-padded, empty, huge or non-text value is no count", () => {
  const malformed = ["abc", "-1", "-0", "2.5", "2.0", " 2", "2 ", "+2", "02", "00", "", "1000", "99999999999999999999", "1e2", "0x10", "２", "NaN", "Infinity", "2,5", "2\n"];
  for (const value of malformed) assert.equal(termOf(waitingRec(value)).waiting, 0, JSON.stringify(value));
  assert.equal(termOf({ address: "cao/t1", labels: { "cao.terminal_id": "t1", "cao.session": "cao-a" }, status: "idle" }).waiting, 0, "no label");
  assert.equal(termOf({ address: "cao/t1", labels: { "cao.terminal_id": "t1", "cao.session": "cao-a", "cao.waiting": 2 }, status: "idle" }).waiting, 0, "a number, not the text the core sends");
  assert.equal(termOf({ address: "cao/t1" }).waiting, 0, "no labels at all");
  assert.equal(termOf(null).waiting, 0, "no record");
});
test("a terminal waits only while its turn is over: idle or done, with a count", () => {
  assert.equal(L.waitingCount(T("c1", "s", { state: "idle", waiting: 2 })), 2);
  assert.equal(L.waitingCount(T("c1", "s", { state: "done", waiting: 1 })), 1);
  for (const state of ["working", "blocked", "error", "unknown"]) assert.equal(L.waitingCount(T("c1", "s", { state, waiting: 2 })), 0, state);
  for (const waiting of [0, undefined, -1, 2.5, NaN, "2", null]) assert.equal(L.waitingCount(T("c1", "s", { state: "idle", waiting })), 0, String(waiting));
});
test("displayState: idle or done with a count is waiting, whatever replied says; a zero or missing count changes nothing", () => {
  for (const state of ["idle", "done"]) {
    const t = T("c1", "s", { state, waiting: 2, turn: 4, seen: 3 });
    assert.equal(displayState(t, {}), "waiting", state);
    assert.equal(displayState(t, { replied: true }), "waiting", `${state}, replied would say replied`);
    assert.equal(displayState({ ...t, waiting: 0 }, {}), state, `${state} with 0`);
    assert.equal(displayState({ ...t, waiting: undefined }, {}), state, `${state} with no count`);
    assert.equal(displayState({ ...t, waiting: 0 }, { replied: true }), "replied", `${state} with 0, replied`);
  }
});
test("displayState: working, blocked and error keep their state with a count", () => {
  assert.equal(displayState(T("c1", "s", { state: "working", waiting: 1 }), {}), "working");
  assert.equal(displayState(T("c1", "s", { state: "working", waiting: 1 }), { quiet: true }), "quiet");
  assert.equal(displayState(T("c1", "s", { state: "blocked", waiting: 1 }), {}), "blocked");
  assert.equal(displayState(T("c1", "s", { state: "error", waiting: 1 }), {}), "error");
  assert.equal(displayState(T("c1", "s", { state: "unknown", waiting: 1 }), {}), "starting");
});
test("waiting has Herdr's spinner in the muted colour: not the done dot, the idle ring or the working spinner's colour", () => {
  assert.equal(statusShape("waiting"), "spinner-dim");
  for (const other of ["working", "idle", "done", "replied", "blocked", "quiet", "starting", "error"]) assert.notEqual(statusShape("waiting"), statusShape(other), other);
});

const waitingSession = (name = "cao-a", over = {}) => sess(name, [T(`c-${name}`, name, { state: "done", turn: 4, seen: 3, waiting: 1, ...over })]);
const realReplied = (t) => isReplied(t, {});
const unknownSeen = (t) => seenState(t, {}) === "unknown";

test("a waiting terminal has not replied: isReplied is false and seenState is null, until the count goes", () => {
  const t = T("c1", "s", { state: "done", turn: 4, seen: 3, waiting: 2 });
  assert.equal(isReplied(t, {}), false);
  assert.equal(seenState(t, {}), null);
  assert.equal(isReplied({ ...t, waiting: 0 }, {}), true, "the reply is there once the count is gone");
  assert.equal(seenState({ ...t, waiting: 0 }, {}), "replied");
  const unseen = T("c1", "s", { state: "idle", turn: 4, waiting: 1 });
  assert.equal(seenState(unseen, {}), null, "no seen label: not unknown while it waits");
  assert.equal(seenState({ ...unseen, waiting: 0 }, {}), "unknown");
});
test("the status bar counts a waiting session as working, and as neither replied nor unknown", () => {
  const counts = statusCounts([waitingSession()], realReplied, unknownSeen);
  assert.deepEqual(counts, { sessions: 1, asking: 0, replied: 0, repliedUnknown: 0, working: 1 });
  assert.equal(BAR(counts), `cao ${DOT} 1 session ${DOT} 1 working`);
  const noSeen = sess("cao-b", [T("c-b", "cao-b", { state: "idle", turn: 2, waiting: 1 })]);
  assert.deepEqual(statusCounts([noSeen], realReplied, unknownSeen), { sessions: 1, asking: 0, replied: 0, repliedUnknown: 0, working: 1 });
});
test("a session that waits and also works is one working session, and a blocked terminal still asks", () => {
  const both = sess("cao-a", [T("c1", "cao-a", { state: "idle", waiting: 2 }), worker("w1", "cao-a", "c1", { state: "working" })]);
  assert.equal(statusCounts([both], never).working, 1);
  const asking = sess("cao-b", [T("c2", "cao-b", { state: "idle", waiting: 2 }), worker("w2", "cao-b", "c2", { state: "blocked" })]);
  assert.deepEqual(statusCounts([asking], never), { sessions: 1, asking: 1, replied: 0, repliedUnknown: 0, working: 1 });
});
test("a waiting session with a zero count is counted as before", () => {
  const counts = statusCounts([waitingSession("cao-a", { waiting: 0 })], realReplied, unknownSeen);
  assert.deepEqual(counts, { sessions: 1, asking: 0, replied: 1, repliedUnknown: 0, working: 0 });
});
test("the working filter lists a waiting session and the replied filter does not", () => {
  const rest = sess("cao-r", [T("r1", "cao-r", { state: "idle" })]);
  const list = [waitingSession(), rest];
  assert.deepEqual(filterSessions(list, "working", realReplied).map((s) => s.name), ["cao-a"]);
  assert.deepEqual(filterSessions(list, "replied", realReplied).map((s) => s.name), []);
});
test("a waiting session counts as working in the bar and the filter, and keeps its place by age", () => {
  const idle = sess("A", [T("a1", "A", { state: "idle", createdMs: 3 })]);
  const waits = sess("B", [T("b1", "B", { state: "done", turn: 4, seen: 3, waiting: 2, createdMs: 2 })]);
  const replied = sess("C", [T("c1", "C", { state: "done", turn: 4, seen: 3, createdMs: 1 })]);
  assert.deepEqual(sortSessions([idle, waits, replied]).map((s) => s.name), ["C", "B", "A"], "oldest first");
  assert.deepEqual(sortSessions([replied, waits, idle].reverse()).map((s) => s.name), ["C", "B", "A"]);
  assert.deepEqual(filterSessions([replied, waits, idle], "working", realReplied).map((s) => s.name), ["B"]);
});
test("through buildModel: a done conductor with cao.waiting is waiting, then replied once the label is removed", () => {
  const waiting = raw("c1", "s", { status: "done", turn: 5, labels: { "herdr.seen": "2", "cao.waiting": "1" } });
  const gone = raw("c1", "s", { status: "done", turn: 5, labels: { "herdr.seen": "2" } });
  const a = buildModel([waiting], [], { machine: "m" }, CTX);
  assert.equal(a.terms[0].waiting, 1);
  assert.equal(a.replied(a.terms[0]), false);
  assert.deepEqual(statusCounts(a.sessions, a.replied), { sessions: 1, asking: 0, replied: 0, repliedUnknown: 0, working: 1 });
  const b = buildModel([gone], [], { machine: "m" }, CTX);
  assert.equal(b.replied(b.terms[0]), true);
  assert.equal(statusCounts(b.sessions, b.replied).replied, 1);
});
test("the input is not changed: a frozen waiting session goes through the counts, the filter and the sort", () => {
  const list = freeze([waitingSession("cao-a"), waitingSession("cao-b")]);
  assert.doesNotThrow(() => { statusCounts(list, realReplied, unknownSeen); filterSessions(list, "working", realReplied); sortSessions(list); });
});

// The window reads the label from the record and nowhere else: no request for delegations and no second reader of the label.
const sourcesOf = (dir) => readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
  const path = join(dir, e.name);
  if (e.isDirectory()) return sourcesOf(path);
  return /\.(js|json)$/.test(e.name) && !/\.test\.mjs$/.test(e.name) ? [{ path, text: readFileSync(path, "utf8") }] : [];
});
test("source scan: cao.waiting is read once, in termOf, from the labels; nothing asks CAO for delegations", () => {
  const root = join(dirname(fileURLToPath(import.meta.url)), "..");
  const files = [...sourcesOf(join(root, "lib")), ...sourcesOf(join(root, "mods"))];
  assert.ok(files.length > 10, "fixture: found the sources");
  const readers = files.filter((f) => f.text.includes("cao.waiting")).map((f) => f.path.slice(root.length + 1));
  assert.deepEqual(readers, ["lib/logic.js"], "only logic.js names the label");
  assert.equal(readFileSync(join(root, "lib/logic.js"), "utf8").split('"cao.waiting"').length - 1, 1, "the label name is a string in one place");
  const body = readFileSync(join(root, "lib/logic.js"), "utf8");
  const termOfBody = body.slice(body.indexOf("export function termOf"), body.indexOf("// The CAO terminals of a terminals() list"));
  assert.match(termOfBody, /labelValue\(labels, "cao\.waiting"\)/, "from the labels");
  for (const f of files) assert.doesNotMatch(f.text, /delegation/i, `${f.path} asks for no delegation data`);
});

// ---- the tree of who started whom (the Agents block) ----

test("No worker fold: lib/logic.js has no fold rule, and the tree has no level to fold by", () => {
  assert.equal(L.foldWorkers, undefined);
  assert.equal(L.FOLD_OVER, undefined);
  const s = sess("s", [T("c1", "s", { createdMs: 1 }), ...Array.from({ length: 30 }, (_, i) => worker(`w${i}`, "s", "c1", { createdMs: 10 + i }))]);
  assert.deepEqual(Object.keys(L.sessionTree(s)).sort(), ["byId", "parent", "terms", "unlisted"], "the tree of a session of 31 agents: no level, nothing hidden");
  assert.equal(L.sessionTree(s).terms.length, 31);
});

test("sessionTree: a caller that is not listed marks its agent, and a cycle is cut at the oldest", () => {
  const s = sess("s", [T("c1", "s", { createdMs: 1 }), worker("w1", "s", "c1", { createdMs: 2 }), worker("w2", "s", "w1", { createdMs: 3 }), worker("w9", "s", "ghost", { createdMs: 4 }), worker("w10", "s", "w9", { createdMs: 5 }),
    worker("x", "s", "y", { createdMs: 6 }), worker("y", "s", "x", { createdMs: 7 })]);
  const t = L.sessionTree(s);
  assert.deepEqual([...t.unlisted], ["w9"]);
  assert.deepEqual([...t.parent], [["w1", "c1"], ["w2", "w1"], ["w10", "w9"], ["y", "x"]]);
});

// ---- waiting derived from the tree ----
// A worker is finished when it is idle or done and every worker below it is finished. An agent waits when its own state is idle or done and
// at least one worker below it is unfinished. Unfinished and counted: working, blocked, or idle/done with something unfinished below (or with
// a cao.waiting label above 0). An agent whose own state is unknown, starting or error is never counted, and so never makes its parent
// wait by itself. N is the number of counted workers below, direct or transitive. Only callers inside the session make a parent.

const { treeWaiting } = L;
const rb = (over = {}) => [
  T("c61", "ROOTB", { createdMs: 1, state: "done", ...(over.c61 ?? {}) }),
  worker("s6d", "ROOTB", "c61", { createdMs: 2, state: "idle", ...(over.s6d ?? {}) }),
  worker("she", "ROOTB", "s6d", { createdMs: 3, state: "done", ...(over.she ?? {}) }),
  worker("apo", "ROOTB", "s6d", { createdMs: 4, state: "done", ...(over.apo ?? {}) }),
  worker("tit", "ROOTB", "s6d", { createdMs: 5, state: "idle", ...(over.tit ?? {}) }),
  worker("leo", "ROOTB", "s6d", { createdMs: 6, state: "done", ...(over.leo ?? {}) }),
  worker("soc", "ROOTB", "s6d", { createdMs: 7, state: "working", ...(over.soc ?? {}) }),
];
const waits = (terms) => Object.fromEntries(treeWaiting(terms));

test("treeWaiting: in ROOTB the conductor waits on 2 workers (the supervisor and socrates) and the supervisor on 1, while socrates works", () => {
  assert.deepEqual(waits(freeze(rb())), { c61: 2, s6d: 1 });
});
test("treeWaiting: a session whose workers all finished waits on nothing, and a working agent is not waiting", () => {
  assert.deepEqual(waits(freeze(rb({ soc: { state: "done" } }))), {});
  assert.deepEqual(waits(freeze(rb({ c61: { state: "working" }, s6d: { state: "working" } }))), {}, "working agents are not waiting");
  assert.deepEqual(waits([T("c1", "s")]), {}, "a lone agent");
  assert.deepEqual(waits([]), {});
});
test("treeWaiting: DIGEST, a supervisor waits on 1 while forge works", () => {
  const d = [T("sup", "DIGEST", { createdMs: 1, state: "idle" }), worker("forge", "DIGEST", "sup", { createdMs: 2, state: "working" })];
  assert.deepEqual(waits(freeze(d)), { sup: 1 });
});
test("treeWaiting: blocked counts as unfinished, error, starting and unknown do not", () => {
  const under = (state) => waits(freeze([T("c1", "s"), worker("w1", "s", "c1", { state })]));
  assert.deepEqual(under("blocked"), { c1: 1 });
  assert.deepEqual(under("working"), { c1: 1 });
  for (const state of ["error", "starting", "unknown", "initializing", ""]) assert.deepEqual(under(state), {}, `a ${JSON.stringify(state)} worker never makes its parent wait`);
});
test("treeWaiting: an unknown agent between does not count itself, but its working worker still makes both ancestors wait", () => {
  const t = freeze([T("c1", "s", { createdMs: 1 }), worker("m", "s", "c1", { createdMs: 2, state: "unknown" }), worker("w", "s", "m", { createdMs: 3, state: "working" })]);
  assert.deepEqual(waits(t), { c1: 1 }, "m is not idle or done, so it is not waiting; c1 counts w only");
});
test("treeWaiting: a deleted or unknown caller, and a caller in another session, create no waiting", () => {
  const ghost = freeze([T("c1", "s", { createdMs: 1 }), worker("w9", "s", "ghost", { createdMs: 2, state: "working" })]);
  assert.deepEqual(waits(ghost), {}, "w9's caller is not listed: it has no parent");
  assert.deepEqual(waits(freeze([T("c1", "s"), worker("w", "s", "c1", { state: "done" }), worker("x", "s", "elsewhere/t7", { state: "working" })])), {});
});
test("treeWaiting: an idle agent that carries a cao.waiting label is unfinished for its callers, and the label does not change its own entry", () => {
  const t = freeze([T("c1", "s", { createdMs: 1 }), worker("w1", "s", "c1", { createdMs: 2, state: "idle", waiting: 1 })]);
  assert.deepEqual(waits(t), { c1: 1 }, "w1 waits by its label, so c1 waits on it; w1's own tree rule gives nothing");
});
test("treeWaiting: cycles and a self-caller end, the older member of a cycle is the parent, and the input is not changed", () => {
  const t = freeze([T("c1", "s", { createdMs: 1, state: "done" }), worker("x", "s", "y", { createdMs: 2, state: "done" }), worker("y", "s", "x", { createdMs: 3, state: "working" }), worker("z", "s", "z", { createdMs: 4, state: "working" })]);
  assert.deepEqual(waits(t), { x: 1 }, "x is cut as the root of the cycle and y hangs under it; z has no parent");
  assert.deepEqual(waits(freeze([T("c1", "s", { state: "idle" })])), {});
});
test("treeWaiting: the count is of workers, direct or transitive, and a sibling that finished adds none", () => {
  const t = freeze([T("c1", "s", { createdMs: 1 }), worker("a", "s", "c1", { createdMs: 2, state: "working" }), worker("b", "s", "c1", { createdMs: 3, state: "working" }), worker("c", "s", "c1", { createdMs: 4, state: "done" }), worker("d", "s", "b", { createdMs: 5, state: "working" })]);
  assert.deepEqual(waits(t), { c1: 3 });
});

const rawAt = (id, session, ms, extra = {}) => ({ ...raw(id, session, extra), created_ms: ms });
const rootbRaw = (over = {}) => [
  rawAt("c61", "ROOTB", 1, { status: "done", turn: 3, labels: { "herdr.seen": "2", "cao.window": "conductor-61d0" } }),
  rawAt("s6d", "ROOTB", 2, { profile: "supervisor", caller: "c61", status: "idle" }),
  rawAt("she", "ROOTB", 3, { profile: "sherlock", caller: "s6d", status: "done" }),
  rawAt("soc", "ROOTB", 7, { profile: "socrates", caller: "s6d", status: over.soc ?? "working" }),
];
test("buildModel: the conductor and supervisor of ROOTB wait while socrates works, with the count in `waiting`, and the session's first agent is the conductor", () => {
  const m = buildModel(rootbRaw(), [], { machine: "m" }, CTX);
  const by = Object.fromEntries(m.terms.map((t) => [t.id, t]));
  assert.deepEqual([by.c61.waiting, by.s6d.waiting, by.she.waiting, by.soc.waiting], [2, 1, 0, 0]);
  assert.deepEqual([displayState(by.c61), displayState(by.s6d), displayState(by.she), displayState(by.soc)], ["waiting", "waiting", "done", "working"]);
  assert.equal(m.replied(by.c61), false, "turn 3 is above its seen 2, but a waiting agent has not replied");
  assert.equal(m.sessions[0].talkable[0].waiting, 2, "the session's terms are the model's: the Sessions row reads the same count");
  const done = buildModel(rootbRaw({ soc: "done" }), [], { machine: "m" }, CTX);
  assert.deepEqual(done.terms.map((t) => t.waiting), [0, 0, 0, 0]);
  assert.equal(done.replied(done.terms[0]), true, "all finished: the reply shows again");
});
test("buildModel: an old server (no profile or caller label) waits the same once the fallback cache has answered, and not before", () => {
  const bare = rootbRaw().map((r) => ({ ...r, labels: Object.fromEntries(Object.entries(r.labels).filter(([k]) => k !== "cao.profile" && k !== "cao.caller")) }));
  const cache = freeze(Object.fromEntries([["c61", answered("conductor", null)], ["s6d", answered("supervisor", "c61")], ["she", answered("sherlock", "s6d")], ["soc", answered("socrates", "s6d")]]));
  const m = buildModel(bare, [], { machine: "m" }, { ...CTX, cache });
  assert.deepEqual(m.terms.map((t) => t.waiting), [2, 1, 0, 0]);
  const none = buildModel(bare, [], { machine: "m" }, CTX);
  assert.deepEqual(none.terms.map((t) => t.waiting), [0, 0, 0, 0], "no caller is known, so nobody waits");
});
test("buildModel: the cao.waiting label wins over the tree count, and either one makes an agent wait", () => {
  const withLabel = rootbRaw().map((r) => (r.labels["cao.terminal_id"] === "c61" ? { ...r, labels: { ...r.labels, "cao.waiting": "1" } } : r));
  assert.deepEqual(buildModel(withLabel, [], { machine: "m" }, CTX).terms.map((t) => t.waiting), [1, 1, 0, 0]);
  const labelOnly = [rawAt("c1", "s", 1, { status: "idle", labels: { "cao.waiting": "3" } })];
  assert.equal(buildModel(labelOnly, [], { machine: "m" }, CTX).terms[0].waiting, 3);
});
test("buildModel and the status bar: a waiting conductor counts as working even when the only worker below it is blocked", () => {
  const m = buildModel([rawAt("c1", "s", 1, { status: "idle" }), rawAt("w1", "s", 2, { profile: "developer", caller: "c1", status: "blocked" })], [], { machine: "m" }, CTX);
  const counts = statusCounts(m.sessions, m.replied);
  assert.deepEqual([counts.working, counts.asking, counts.replied], [1, 1, 0]);
  assert.equal(filterSessions(m.sessions, "working", m.replied).length, 1);
  const quiet = buildModel([rawAt("c1", "s", 1, { status: "idle" })], [], { machine: "m" }, CTX);
  assert.equal(statusCounts(quiet.sessions, quiet.replied).working, 0);
});
test("buildModel: the terminals handed in are not changed, and a terminal that waits by the tree is a copy", () => {
  const input = freeze(rootbRaw());
  assert.doesNotThrow(() => buildModel(input, [], { machine: "m" }, CTX));
});
test("the tree rule adds no poll and no HTTP call: treeWaiting reads the terms it is given, and the mods keep their one fetch and one timer each", () => {
  const dir = join(dirname(fileURLToPath(import.meta.url)), "..");
  const logic = readFileSync(join(dir, "lib", "logic.js"), "utf8");
  const start = logic.indexOf("export function treeWaiting");
  assert.ok(start > 0, "treeWaiting is in lib/logic.js");
  const body = logic.slice(start, logic.indexOf("\n}\n", start));
  assert.doesNotMatch(body, /callCao|exec\(|fetch|timer|setInterval|setTimeout|hc\(|shared\(/, "pure: no call out");
  const side = readFileSync(join(dir, "mods", "cao-window-side", "main.js"), "utf8");
  assert.equal((side.match(/callCao\(/g) ?? []).length, 2, "the terminal fetch and the health probe, as before");
  assert.equal((side.match(/timer\.every\(/g) ?? []).length, 2, "one tick per copy, as before");
  const buildBody = logic.slice(logic.indexOf("export function buildModel"), logic.indexOf("// ---------- failure states"));
  assert.match(buildBody, /treeWaiting\(/, "buildModel feeds every mod");
});

// ---- the cache cannot leave a flat tree (the fallback to CAO when labels are absent) ----

test("PENDING_EXPIRE_MS is 15 s, FALLBACK_RETRY_MS stays 30 s, and markPending stamps the pending entry with the time", () => {
  assert.equal(L.PENDING_EXPIRE_MS, 15_000);
  assert.equal(L.FALLBACK_RETRY_MS, 30_000);
  assert.deepEqual(markPending({ t0: answered("conductor", null) }, ["t1", "t2"], 1234), { t0: answered("conductor", null), t1: { state: "pending", at: 1234 }, t2: { state: "pending", at: 1234 } });
});
test("fallbackPlan: a pending entry whose answer never came is fetched again after 15 s, like a failure, and not before", () => {
  const terms = [unlabelled("t1")];
  const cache = freeze(markPending({}, ["t1"], 1000));
  assert.deepEqual(fallbackPlan(cache, terms, 1000), [], "in flight");
  assert.deepEqual(fallbackPlan(cache, terms, 1000 + 14_999), []);
  assert.deepEqual(fallbackPlan(cache, terms, 1000 + 15_000), ["t1"]);
  assert.deepEqual(fallbackPlan(cache, terms, 1000 + 600_000), ["t1"], "and stays due until it is fetched again");
  const again = markPending(cache, ["t1"], 1000 + 15_000);
  assert.deepEqual(fallbackPlan(again, terms, 1000 + 15_000 + 14_999), [], "a retry in flight is not fetched twice");
  assert.deepEqual(fallbackPlan(again, terms, 1000 + 30_000), ["t1"]);
});
test("fallbackPlan: a pending entry with no time is never stuck: it is due at once; an answered one is never fetched again, and a failure still waits 30 s", () => {
  const terms = [unlabelled("t1"), unlabelled("t2"), unlabelled("t3")];
  const cache = freeze({ t1: { state: "pending" }, t2: answered("developer", "c1"), t3: { state: "failed", at: 5000 } });
  assert.deepEqual(fallbackPlan(cache, terms, 5000 + 29_999), ["t1"]);
  assert.deepEqual(fallbackPlan(cache, terms, 5000 + 30_000), ["t1", "t3"]);
});
test("Lost answer: a fetch whose callback never completes leaves the tree flat for 15 s at most, then the terminal is asked again and the tree is right", () => {
  const raws = [raw("c1", "s"), { ...raw("w1", "s", { profile: "developer", caller: "c1" }), labels: { "cao.terminal_id": "w1", "cao.session": "s" } }];
  const model = (cache) => buildModel(raws, [], { machine: "m" }, { ...CTX, cache });
  let cache = markPending({}, ["w1"], 0);
  assert.equal(fallbackPlan(cache, model(cache).terms, 14_999).length, 0, "still waiting for the first answer");
  assert.deepEqual(fallbackPlan(cache, model(cache).terms, 15_000), ["w1"], "the answer was lost: ask again");
  cache = markPending(cache, ["w1"], 15_000);
  cache = fallbackResult(cache, "w1", { kind: "ok", json: { agent_profile: "developer", caller_id: "c1" } }, 15_500);
  assert.deepEqual(model(cache).terms.map((t) => [t.id, t.caller]), [["c1", undefined], ["w1", "c1"]]);
  assert.deepEqual(fallbackPlan(cache, model(cache).terms, 99_000_000), [], "answered: never again");
});

const published = (o) => Object.fromEntries(Object.entries(o).map(([id, [p, c]]) => [id, { state: "ok", agent_profile: p, caller_id: c }]));
test("sharedCache: what another copy published is merged in, never overwritten: two partial caches give their union", () => {
  const mine = freeze({ t1: answered("conductor", null), t2: answered("developer", "t1") });
  const theirs = freeze(published({ t2: ["developer", "t1"], t3: ["reviewer", "t1"] }));
  const r = sharedCache(mine, ["t1", "t2", "t3"], {}, theirs);
  assert.deepEqual(r.published, published({ t1: ["conductor", null], t2: ["developer", "t1"], t3: ["reviewer", "t1"] }));
  assert.deepEqual(Object.keys(r.cache).sort(), ["t1", "t2", "t3"], "and this copy adopts what it lacked");
  assert.deepEqual(r.cache.t3, { state: "ok", agent_profile: "reviewer", caller_id: "t1" });
  const other = sharedCache({ t3: answered("reviewer", "t1") }, ["t1", "t2", "t3"], {}, published({ t1: ["conductor", null], t2: ["developer", "t1"] }));
  assert.deepEqual(other.published, r.published, "whichever copy writes last, the same union");
});
test("sharedCache: an answered shared entry replaces this copy's pending or failed one, and this copy's own answer is kept over the shared one", () => {
  const r = sharedCache(freeze({ t1: { state: "pending", at: 1 }, t2: { state: "failed", at: 2 }, t3: answered("developer", "c1") }), ["t1", "t2", "t3"], {}, freeze(published({ t1: ["a", null], t2: ["b", "t1"], t3: ["other", "x"] })));
  assert.deepEqual(r.cache.t1, { state: "ok", agent_profile: "a", caller_id: null });
  assert.deepEqual(r.cache.t2, { state: "ok", agent_profile: "b", caller_id: "t1" });
  assert.equal(r.cache.t3.agent_profile, "developer");
  assert.equal(r.published.t3.agent_profile, "developer");
});
test("sharedCache: the merge is pruned to the live terminals, an empty snapshot prunes nothing, and a stored value that is not a cache is ignored", () => {
  const stored = freeze(published({ t1: ["conductor", null], gone: ["x", null] }));
  assert.deepEqual(Object.keys(sharedCache({}, ["t1", "t2"], {}, stored).published), ["t1"]);
  assert.deepEqual(Object.keys(sharedCache({}, [], {}, stored).published).sort(), ["gone", "t1"], "a lost connection keeps what was published");
  for (const junk of [null, undefined, 5, "x", [], [published({ t1: ["a", null] })], { t1: null }, { t1: { state: "failed" } }, { t1: { state: "ok" } }, { t1: { state: "ok", agent_profile: "" } }, { t1: { state: "ok", agent_profile: 7 } }]) {
    assert.deepEqual(sharedCache({ t1: answered("conductor", null) }, ["t1"], {}, junk).published, published({ t1: ["conductor", null] }), JSON.stringify(junk));
  }
  assert.deepEqual(sharedCache({}, ["t1"], {}, { t1: { state: "ok", agent_profile: "dev", secret: "x" } }).published, published({ t1: ["dev", null] }), "only the three fields are taken, and a missing caller is none");
});
test("sharedCache: the stored value and the cache handed in are not changed, and the cache key is the same for the same entries in any key order", () => {
  const stored = freeze(published({ t1: ["conductor", null] }));
  const mine = freeze({ t2: answered("developer", "t1") });
  sharedCache(mine, ["t1", "t2"], {}, stored);
  assert.equal(L.cacheKey({ b: { state: "ok", agent_profile: "x", caller_id: null }, a: { state: "ok", agent_profile: "y", caller_id: "b" } }), L.cacheKey({ a: { caller_id: "b", agent_profile: "y", state: "ok" }, b: { agent_profile: "x", state: "ok", caller_id: null } }));
  assert.notEqual(L.cacheKey(published({ a: ["x", null] })), L.cacheKey(published({ a: ["x", "b"] })));
  assert.equal(L.cacheKey(null), L.cacheKey({}), "no cache is an empty one");
});

const lostFirst = (over = {}) => classify([
  T("c61", "s", { profile: undefined, createdMs: 1, ...over.c61 }), T("s6d", "s", { profile: undefined, createdMs: 2, ...over.s6d }), T("she", "s", { profile: undefined, createdMs: 3, ...over.she }),
], {}, []);
test("sessionTree: a terminal whose profile and caller are unresolved is not a root: it is one step down, with the 'parent not listed' note, unless it is the session's main agent", () => {
  const session = { name: "s", terms: [], ...lostFirst() };
  session.terms = [...session.talkable, ...session.workers];
  assert.deepEqual(session.talkable.map((t) => t.id), ["c61"], "fixture: the first unresolved terminal is shown as the provisional main agent");
  const t = L.sessionTree(session);
  assert.deepEqual([...t.unlisted], ["s6d", "she"]);
});
test("sessionTree: the children of an unresolved terminal stay under it, a resolved root stays a root, and a resolved caller that is not listed is as before", () => {
  const terms = [T("c1", "s", { createdMs: 1 }), T("u", "s", { profile: undefined, createdMs: 2 }), worker("k", "s", "u", { createdMs: 3 }), worker("g", "s", "ghost", { createdMs: 4 }), T("r2", "s", { createdMs: 5 })];
  const session = { name: "s", terms, ...classify(terms, {}, []) };
  const t = L.sessionTree(session);
  assert.deepEqual([...t.unlisted].sort(), ["g", "u"]);
});

// ---- a stable session order, oldest first ----
// A session's age is the creation time of its oldest terminal; the oldest session is first, ties go by session name, and recorded draft
// sessions are last in the order they were given. A state never moves a row: marks, counts and filters carry the attention.

const aged = (name, ...ms) => sess(name, ms.map((m, i) => T(`${name}${i}`, name, { createdMs: m })));
const orderOf = (list) => sortSessions(list).map((x) => x.name);
test("Order by age: the oldest session first, a session's age being its oldest terminal's, whatever order they come in", () => {
  const list = freeze([aged("c", 30), aged("a", 50, 10, 40), aged("b", 20)]);
  assert.deepEqual(orderOf(list), ["a", "b", "c"], "a's oldest terminal is 10");
  assert.deepEqual(orderOf(freeze([aged("b", 20), aged("c", 30), aged("a", 50, 10, 40)])), ["a", "b", "c"]);
  assert.equal(sortSessions.length, 1, "the replied predicate is not a parameter any more");
});
test("Order by age: equal ages go by session name", () => {
  assert.deepEqual(orderOf(freeze([aged("cao-b", 5), aged("cao-a", 5), aged("cao-C", 5), aged("cao-a2", 5)])), ["cao-C", "cao-a", "cao-a2", "cao-b"], "code unit order, so it does not depend on the locale");
});
test("Order by age: states changing never reorder: asking, replied, working, waiting and idle sessions keep their places", () => {
  const states = [{ state: "idle" }, { state: "working" }, { state: "blocked" }, { state: "done", turn: 3, seen: 1 }, { state: "idle", waiting: 2 }, { state: "error" }];
  const base = (over) => [aged("old", 1), aged("mid", 2), aged("new", 3)].map((x) => (x.name === "mid" ? sess("mid", [T("m0", "mid", { createdMs: 2, ...over }), worker("m1", "mid", "m0", { createdMs: 9, ...over })]) : x));
  for (const over of states) assert.deepEqual(orderOf(base(over)), ["old", "mid", "new"], JSON.stringify(over));
  const blockedOld = sess("old", [T("o0", "old", { createdMs: 1, state: "blocked" })]);
  assert.deepEqual(orderOf([aged("new", 3), blockedOld, aged("mid", 2)]), ["old", "mid", "new"], "an asking session is not lifted above a younger one");
});
test("Order by age: a session that appears lands last, wherever it is in the input", () => {
  const before = [aged("a", 10), aged("b", 20)];
  assert.deepEqual(orderOf([...before, aged("fresh", 1_700_000_000_000)]), ["a", "b", "fresh"]);
  assert.deepEqual(orderOf([aged("fresh", 1_700_000_000_000), ...before]), ["a", "b", "fresh"]);
});
test("Order by age: recorded draft sessions are last, in the order given, whatever their age or state", () => {
  const draft = (name, ms) => sess(name, [T(`${name}0`, name, { createdMs: ms, state: "blocked" })], { draft: true });
  assert.deepEqual(orderOf([draft("d-late", 99), aged("a", 50), draft("d-early", 1), aged("b", 60)]), ["a", "b", "d-late", "d-early"]);
  assert.deepEqual(orderOf([draft("d2", 5), draft("d1", 5)]), ["d2", "d1"], "stable among themselves, not by name");
  const operator = sess("cao-draft-x", [T("o1", "cao-draft-x", { createdMs: 1, state: "done" })]);
  assert.deepEqual(orderOf([aged("a", 5), operator]), ["cao-draft-x", "a"], "an operator's session named draft is an ordinary one");
});
test("Order by age: a terminal with no creation time does not make a session the oldest, and a session with none sorts after the dated ones, by name", () => {
  assert.deepEqual(orderOf([aged("zero", 0), aged("dated", 7)]), ["dated", "zero"]);
  assert.deepEqual(orderOf([sess("mixed", [T("x0", "mixed", { createdMs: 0 }), T("x1", "mixed", { createdMs: 9 })]), aged("early", 5), aged("late", 12)]), ["early", "mixed", "late"], "the undated terminal is ignored: mixed is 9 old");
  assert.deepEqual(orderOf([aged("b", 0), aged("a", 0)]), ["a", "b"]);
  assert.deepEqual(orderOf([sess("empty", []), aged("a", 3)]), ["a", "empty"]);
});
test("Order by age: empty and odd input, and the input is not reordered or changed", () => {
  assert.deepEqual(sortSessions([]), []);
  for (const bad of [null, undefined, "x", 5, {}]) assert.deepEqual(sortSessions(bad), [], String(bad));
  const list = freeze([aged("b", 2), aged("a", 1)]);
  assert.deepEqual(orderOf(list), ["a", "b"]);
  assert.deepEqual(list.map((x) => x.name), ["b", "a"]);
});
test("buildModel lists the sessions by age: the oldest terminal's created_ms, not the state, and the filter keeps that order", () => {
  const recs = [rawAt("n1", "new", 300, { status: "blocked" }), rawAt("o1", "old", 100, { status: "idle" }), rawAt("m1", "mid", 200, { status: "working" }), rawAt("o2", "old", 400, { status: "working" })];
  const m = buildModel(recs, [], { machine: "m" }, CTX);
  assert.deepEqual(m.sessions.map((x) => x.name), ["old", "mid", "new"]);
  assert.deepEqual(filterSessions(m.sessions, "working", m.replied).map((x) => x.name), ["old", "mid"]);
  const flipped = recs.map((r) => ({ ...r, status: r.status === "blocked" ? "idle" : r.status === "idle" ? "blocked" : r.status }));
  assert.deepEqual(buildModel(flipped, [], { machine: "m" }, CTX).sessions.map((x) => x.name), ["old", "mid", "new"]);
});
test("The order rule reads no state and no replied predicate: sortSessions is called with the sessions alone", () => {
  const dir = join(dirname(fileURLToPath(import.meta.url)), "..");
  const logic = readFileSync(join(dir, "lib", "logic.js"), "utf8");
  const start = logic.indexOf("export function sortSessions");
  const body = logic.slice(start, logic.indexOf("\n}\n", start));
  assert.doesNotMatch(body, /\.state|isWorking|isReplied|replied|waiting|blocked/);
  assert.match(logic, /sortSessions\(sessionsFrom\(terms\)\.map\([^\n]*\)\)[;,]/, "buildModel passes the sessions alone");
});

await finish();
