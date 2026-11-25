// lib/workflows-view.js: what the Workflows section and the workflow views of the panel decide (spec
// cao-window-workflows; cao-window-honesty-labels "inputsUndescribed"; cao-window-sessions "Failure states";
// design D15 to D17; tasks 11.2 to 11.4). The test list comes from the specs; the code was read afterwards
// for edge cases.
//
// Defaults chosen where the specs are silent (each has a test that says so):
//  - a tie on the start time of two runs: the one listed first (the server lists newest first) is the newest;
//  - a write check with no run list at all sends (nothing contradicts the snapshot); with a list it must agree;
//  - an unroutable name or run id is left out of rows and gives no route;
//  - a Python source over both server caps names the character cap first;
//  - a free input row with a name and an empty value sends the name with empty text.
//
// Every function is called inside a test case through the namespace, so a function that does not exist
// yet fails its own case with a TypeError, and the rest of the file still runs.
import { assert, finish, test } from "../test/harness.mjs";
import { HARD_WORDS, globDir, shellArgv } from "../test/shell-argv.mjs";
import { callCao, curlRequest, outcomeFrom } from "./cao.js";
import { applyOutcome, dialogKey } from "./logic.js";
import { saveRequest, submitRequest, saveEnabled } from "./workflows.js";
import { TEMPLATES } from "../mods/cao-window-panel/templates.js";
import * as v from "./workflows-view.js";

const AUTH = "CAO requires auth - not supported by this window yet";
const NOW = Date.parse("2026-11-19T10:10:00Z");
const MIN = 60_000;

const run = (state, over = {}) => ({
  run_id: "r1", workflow_name: "wf", state, tier: "yaml", started_at: "2026-11-19T10:00:00Z", finished_at: null, current_step_id: null, ...over,
});
const row = (name, over = {}) => ({ name, source_path: `/srv/specs/${name}.yaml`, mode: "sequential", step_count: 2, description: "", ...over });
const reply = (code, body) => outcomeFrom({ stdout: `${typeof body === "string" ? body : JSON.stringify(body)}\n${String(code).padStart(3, "0")}` });
const UNREACHABLE = outcomeFrom({ stdout: "\n000", stderr: "refused" });
const approvalOutcome = (planId = "wf:abc123", message = "approve it") => reply(403, { detail: { kind: "approval_required", plan_id: planId, message } });

// A frozen copy: a function that writes to its input throws in strict mode (ES modules), so passing this
// proves "nothing mutates its input" without a before/after diff.
function deepFreeze(x) {
  if (x && typeof x === "object" && !Object.isFrozen(x)) { Object.freeze(x); for (const k of Object.keys(x)) deepFreeze(x[k]); }
  return x;
}
const frozen = (x) => deepFreeze(structuredClone(x));

const HOSTILE_NAMES = [
  "", ".", "..", "../x", "..\\x", "a/b", "/a", "a b", " a", "a ", "a\n", "\na", "a\r\n", "a\tb", "a\u0000", "a?x=1", "a#h", "a%2Fb", "%2e%2e",
  "a:b", "a;b", "a.yaml", "é", "٣", "日本", "\u{1F600}", "a*", "a[0]", "a{b}", "a‮b", "a​", "runs:submit",
];
const NOT_STRINGS = [undefined, null, 0, 5, true, {}, [], ["a"], { toString() { return "a"; } }, Symbol("a"), 5n];
const SAFE = /^\/workflows\/[A-Za-z0-9_-]+$/;

// ---- routes: one safe path segment, or nothing ----------------------------------------------------

test("workflowRoute: a bare name gives /workflows/<name>, including names that are odd as property keys", () => {
  for (const n of ["wf", "my-flow_2", "A1", "__proto__", "constructor", "a".repeat(64)]) assert.equal(v.workflowRoute(n), `/workflows/${n}`, n);
});

test("workflowRoute: anything that is not one bare segment gives null, never another path", () => {
  for (const n of [...HOSTILE_NAMES, ...NOT_STRINGS]) assert.equal(v.workflowRoute(n), null, String(typeof n === "symbol" ? "symbol" : typeof n === "bigint" ? "bigint" : JSON.stringify(n)));
});

test("workflowRoute: a trailing newline does not slip past the name rule", () => {
  assert.equal(v.workflowRoute("wf\n"), null);
  assert.equal(v.workflowRoute("wf\n/../x"), null);
});

test("runRoute: cancel and resume give /workflows/runs/<id>/<action> for a plain run id", () => {
  for (const id of ["r1", "a1b2c3d4e5f6", "123e4567-e89b-12d3-a456-426614174000"]) {
    assert.equal(v.runRoute("cancel", id), `/workflows/runs/${id}/cancel`);
    assert.equal(v.runRoute("resume", id), `/workflows/runs/${id}/resume`);
  }
});

test("runRoute: no other action has a route, so the approve route cannot be built", () => {
  for (const a of ["approve", "plans/approve", "CANCEL", "Resume", "cancel/", "cancel ", " cancel", "cancel\n", "", "delete", "__proto__", "constructor", "toString", null, undefined, 5, {}, ["cancel"]]) {
    assert.equal(v.runRoute(a, "r1"), null, JSON.stringify(a));
  }
});

test("runRoute: a run id that is not one bare segment gives null for both actions", () => {
  for (const id of [...HOSTILE_NAMES, ...NOT_STRINGS]) {
    assert.equal(v.runRoute("cancel", id), null);
    assert.equal(v.runRoute("resume", id), null);
  }
});

test("no route this module can build is the plan-approval route", () => {
  const routes = [];
  for (const n of ["plans", "approve", "plans/approve", "runs"]) routes.push(v.workflowRoute(n));
  for (const a of ["approve", "cancel", "resume", "plans/approve"]) for (const id of ["plans", "approve", "x"]) routes.push(v.runRoute(a, id));
  for (const r of routes.filter((x) => x !== null)) {
    assert.notEqual(r, "/workflows/plans/approve");
    assert.ok(!r.endsWith("/plans/approve"), r);
    assert.match(r, /^\/workflows\/(runs\/[A-Za-z0-9_-]+\/(cancel|resume)|[A-Za-z0-9_-]+)$/);
  }
});

// ---- runsOfWorkflow and workflowRows ----------------------------------------------------------------

test("runsOfWorkflow: keeps the runs of that workflow in the server's order, in a new array", () => {
  const runs = [run("running", { run_id: "a" }), run("failed", { run_id: "b", workflow_name: "other" }), run("completed", { run_id: "c" })];
  const got = v.runsOfWorkflow(frozen(runs), "wf");
  assert.deepEqual(got.map((r) => r.run_id), ["a", "c"]);
  assert.notEqual(got, runs);
});

test("runsOfWorkflow: a list that is not an array, and entries that are not objects, give no runs", () => {
  for (const bad of [null, undefined, "runs", 5, {}, { length: 1, 0: run("running") }]) assert.deepEqual(v.runsOfWorkflow(bad, "wf"), []);
  assert.deepEqual(v.runsOfWorkflow([null, 5, "x", undefined, [], run("running")], "wf").map((r) => r.state), ["running"]);
});

test("runsOfWorkflow: a workflow named like an object property only gets its own runs", () => {
  const runs = [run("running", { workflow_name: "__proto__" }), run("failed", { workflow_name: "wf" })];
  assert.deepEqual(v.runsOfWorkflow(runs, "__proto__").map((r) => r.state), ["running"]);
});

test("workflowRows: one row per workflow, with the state of its newest run in the spec's five words", () => {
  const list = [row("never"), row("run1"), row("ok"), row("bad"), row("stopped")];
  const runs = [
    run("running", { workflow_name: "run1", run_id: "1" }), run("completed", { workflow_name: "ok", run_id: "2" }),
    run("failed", { workflow_name: "bad", run_id: "3" }), run("cancelled", { workflow_name: "stopped", run_id: "4" }),
  ];
  assert.deepEqual(v.workflowRows(frozen(list), frozen(runs)), [
    { name: "never", state: "never run" }, { name: "run1", state: "running" }, { name: "ok", state: "succeeded" },
    { name: "bad", state: "failed" }, { name: "stopped", state: "cancelled" },
  ]);
});

test("workflowRows: the newest run by start time decides, not its place in the list", () => {
  const older = run("running", { run_id: "o", started_at: "2026-11-19T10:00:00Z" });
  const newer = run("failed", { run_id: "n", started_at: "2026-11-19T10:05:00Z" });
  assert.equal(v.workflowRows([row("wf")], [older, newer])[0].state, "failed");
  assert.equal(v.workflowRows([row("wf")], [newer, older])[0].state, "failed");
});

test("workflowRows: two runs started at the same time, the one listed first (newest first) decides", () => {
  const a = run("completed", { run_id: "a" });
  const b = run("failed", { run_id: "b" });
  assert.equal(v.workflowRows([row("wf")], [a, b])[0].state, "succeeded");
  assert.equal(v.workflowRows([row("wf")], [b, a])[0].state, "failed");
});

test("workflowRows: a run whose start time cannot be read ranks older than one that can", () => {
  const bad = run("running", { run_id: "x", started_at: "not a time" });
  const good = run("completed", { run_id: "y", started_at: "2026-11-19T09:00:00Z" });
  assert.equal(v.workflowRows([row("wf")], [bad, good])[0].state, "succeeded");
  assert.equal(v.workflowRows([row("wf")], [good, bad])[0].state, "succeeded");
});

test("workflowRows: runs of another workflow never change a row", () => {
  const rows = v.workflowRows([row("a"), row("b")], [run("running", { workflow_name: "b" })]);
  assert.deepEqual(rows, [{ name: "a", state: "never run" }, { name: "b", state: "running" }]);
});

test("workflowRows: no workflows, or a list that is not an array, gives no rows; a runs read that has not answered gives 'unknown', never 'never run'", () => {
  assert.deepEqual(v.workflowRows([], [run("running")]), []);
  for (const bad of [null, undefined, "x", 5, {}]) assert.deepEqual(v.workflowRows(bad, []), []);
  for (const bad of [null, undefined, "x", {}, 0]) assert.deepEqual(v.workflowRows([row("wf")], bad), [{ name: "wf", state: "unknown" }], String(bad));
  assert.deepEqual(v.workflowRows([row("wf")], []), [{ name: "wf", state: "never run" }]);
});

test("workflowRows: a row whose name could not be put in a path is left out, and every row is routable", () => {
  const names = [...HOSTILE_NAMES.filter((n) => n !== ""), "ok", "__proto__"];
  const list = [...names.map((n) => row(n)), null, 5, "x", {}, { name: 7 }];
  const rows = v.workflowRows(frozen(list), []);
  assert.deepEqual(rows.map((r) => r.name), ["ok", "__proto__"]);
  for (const r of rows) assert.notEqual(v.workflowRoute(r.name), null, r.name);
});

test("workflowRows: a row carries a name and a state only; the server's file layout is never copied", () => {
  const rows = v.workflowRows([row("wf", { source_path: "/srv/specs/SECRET-LAYOUT.yaml", description: "d" })], []);
  assert.deepEqual(Object.keys(rows[0]).sort(), ["name", "state"]);
  assert.ok(!JSON.stringify(rows).includes("SECRET-LAYOUT"));
});

test("workflowRows: a run state that names an object property is shown as text, not looked up", () => {
  for (const s of ["toString", "__proto__", "constructor", "hasOwnProperty"]) {
    assert.equal(v.workflowRows([row("wf")], [run(s)])[0].state, s);
  }
  assert.equal(v.workflowRows([row("wf")], [run(undefined)])[0].state, "unknown");
});

// ---- polling ----------------------------------------------------------------------------------------

const due = (over = {}, now = NOW) => v.workflowsPollDue({ shown: true, inFlight: false, lastAt: now - 1, runs: [], stamp: 1, seenStamp: 1, ...over }, now);

test("workflowsPollDue: a section that is not shown, or has a fetch in flight, never fetches", () => {
  for (const over of [{ shown: false }, { shown: undefined }, { shown: "yes" }, { shown: 1 }, { inFlight: true }]) {
    assert.equal(due({ lastAt: undefined, stamp: 2, ...over }), false, JSON.stringify(over));
  }
  assert.equal(v.workflowsPollDue(undefined, NOW), false);
});

test("workflowsPollDue: the first fetch (no usable last time) is due at once", () => {
  for (const lastAt of [undefined, null, NaN, Infinity, "10:00", {}]) assert.equal(due({ lastAt }), true, String(lastAt));
});

test("workflowsPollDue: refreshes every 60 s: 59999 ms is not due, 60000 ms is", () => {
  assert.equal(due({ lastAt: NOW - 59_999 }), false);
  assert.equal(due({ lastAt: NOW - 60_000 }), true);
  assert.equal(due({ lastAt: NOW - 60_001 }), true);
});

test("Active run speeds up polling: with a running run, 4999 ms is not due and 5000 ms is", () => {
  const runs = [run("running")];
  assert.equal(due({ runs, lastAt: NOW - 4_999 }), false);
  assert.equal(due({ runs, lastAt: NOW - 5_000 }), true);
});

test("Active run speeds up polling: any listed run counts, and the pace returns to 60 s when none is running", () => {
  const mixed = [run("completed", { run_id: "1" }), run("failed", { run_id: "2" }), run("running", { run_id: "3", workflow_name: "other" })];
  assert.equal(due({ runs: mixed, lastAt: NOW - 5_000 }), true);
  const none = mixed.map((r) => ({ ...r, state: r.state === "running" ? "completed" : r.state }));
  assert.equal(due({ runs: none, lastAt: NOW - 5_000 }), false);
  assert.equal(due({ runs: none, lastAt: NOW - 59_999 }), false);
  assert.equal(due({ runs: none, lastAt: NOW - 60_000 }), true);
});

test("workflowsPollDue: runs that are not a list give the slow pace", () => {
  for (const runs of [null, undefined, "running", { state: "running" }]) {
    assert.equal(due({ runs, lastAt: NOW - 5_000 }), false);
    assert.equal(due({ runs, lastAt: NOW - 60_000 }), true);
  }
});

test("workflowsPollDue: a write that moved the refresh stamp makes it due now, once it is shown and idle", () => {
  assert.equal(due({ stamp: 2, seenStamp: 1 }), true);
  assert.equal(due({ stamp: undefined, seenStamp: undefined }), false);
  assert.equal(due({ stamp: 2, seenStamp: 1, inFlight: true }), false);
  assert.equal(due({ stamp: 2, seenStamp: 1, shown: false }), false);
});

const plan = (over = {}, now = NOW) => v.workflowDetailPlan({
  shown: true, inFlight: false, lastAt: now - 60_000, runs: [], flowsAt: now - 1, specOk: true, stamp: 1, seenStamp: 1, ...over,
}, now);

test("workflowDetailPlan: the first poll asks for the list and runs, the flows and the spec", () => {
  assert.deepEqual(plan({ lastAt: undefined, flowsAt: undefined, specOk: undefined }), { poll: true, flows: true, spec: true });
});

test("workflowDetailPlan: nothing is fetched when the poll is not due", () => {
  assert.deepEqual(plan({ lastAt: NOW - 59_999, flowsAt: undefined, specOk: false }), { poll: false, flows: false, spec: false });
  assert.deepEqual(plan({ inFlight: true, flowsAt: undefined, specOk: false }), { poll: false, flows: false, spec: false });
  assert.deepEqual(plan({ shown: false, flowsAt: undefined, specOk: false }), { poll: false, flows: false, spec: false });
});

test("workflowDetailPlan: the flows are fetched at most every 30 s: 29999 ms no, 30000 ms yes", () => {
  assert.equal(plan({ flowsAt: NOW - 29_999 }).flows, false);
  assert.equal(plan({ flowsAt: NOW - 30_000 }).flows, true);
});

test("workflowDetailPlan: with a running run the poll is 5 s but the flows still wait 30 s", () => {
  const p = plan({ runs: [run("running")], lastAt: NOW - 5_000, flowsAt: NOW - 5_000 });
  assert.deepEqual(p, { poll: true, flows: false, spec: false });
});

test("workflowDetailPlan: the spec is read until one read succeeded, and read again after a write", () => {
  assert.equal(plan({ specOk: false }).spec, true);
  assert.equal(plan({ specOk: undefined }).spec, true);
  assert.equal(plan({ specOk: "yes" }).spec, true);
  assert.equal(plan({ specOk: true }).spec, false);
  assert.deepEqual(plan({ specOk: true, stamp: 2, seenStamp: 1 }), { poll: true, flows: true, spec: true });
});

// ---- notes under the header, start times -------------------------------------------------------------

test("workflowsNotes: nothing failed, nothing to say; views that are not objects do not throw", () => {
  assert.deepEqual(v.workflowsNotes({ data: [], at: NOW }, { data: [], at: NOW }, NOW), []);
  for (const bad of [null, undefined, "x", 5]) assert.deepEqual(v.workflowsNotes(bad, bad, NOW), []);
});

test("Auth required: the list's auth failure shows the auth message once, and a runs read failing the same way adds nothing", () => {
  assert.deepEqual(v.workflowsNotes({ auth: true }, { auth: true }, NOW), [AUTH]);
  assert.deepEqual(v.workflowsNotes({ auth: true }, { data: [], at: NOW }, NOW), [AUTH]);
});

test("workflowsNotes: a failure only the runs read has is marked 'Runs:' and comes after the list's own lines", () => {
  assert.deepEqual(v.workflowsNotes({ data: [], at: NOW }, { auth: true }, NOW), [`Runs: ${AUTH}`]);
  assert.deepEqual(v.workflowsNotes({ unreachable: true }, { auth: true }, NOW), ["CAO unreachable, retrying", `Runs: ${AUTH}`]);
  assert.deepEqual(v.workflowsNotes({ data: [], at: NOW }, { unreachable: true }, NOW), ["Runs: CAO unreachable, retrying"]);
});

test("Stale data: rows kept from 5 minutes ago say how old they are; fresh ones say 'just now'; no rows, no age", () => {
  const stale = v.workflowsNotes({ data: [row("wf")], at: NOW - 5 * MIN, unreachable: true }, null, NOW);
  assert.deepEqual(stale, ["CAO unreachable, retrying", "Showing the last list, 5 minutes old"]);
  assert.deepEqual(v.workflowsNotes({ data: [], at: NOW - 1000, unreachable: true }, null, NOW), ["CAO unreachable, retrying", "Showing the last list, just now"]);
  assert.deepEqual(v.workflowsNotes({ at: NOW - 5 * MIN, unreachable: true }, null, NOW), ["CAO unreachable, retrying"]);
});

test("workflowsNotes: the server's error words are clipped to 200 characters", () => {
  const long = v.workflowsNotes({ error: "x".repeat(300) }, null, NOW)[0];
  assert.equal(long, `CAO answered with an error: ${"x".repeat(200)}`);
  assert.equal(v.workflowsNotes({ error: "x".repeat(200) }, null, NOW)[0].length, "CAO answered with an error: ".length + 200);
});

test("startedWords: the times of the spec's words, at their boundaries", () => {
  const at = (ms) => v.startedWords(new Date(NOW - ms).toISOString(), NOW);
  assert.equal(at(0), "started just now");
  assert.equal(at(4_999), "started just now");
  assert.equal(at(5_000), "started 5 seconds ago");
  assert.equal(at(59_999), "started 59 seconds ago");
  assert.equal(at(60_000), "started 1 minute ago");
  assert.equal(at(5 * MIN), "started 5 minutes ago");
  assert.equal(at(59 * MIN + 59_999), "started 59 minutes ago");
  assert.equal(at(60 * MIN), "started 1 hour ago");
  assert.equal(at(3 * 60 * MIN), "started 3 hours ago");
});

test("startedWords: a start time in the future (clock skew) reads 'just now'", () => {
  assert.equal(v.startedWords("2026-11-19T10:20:00Z", NOW), "started just now");
  assert.equal(v.startedWords("+275760-09-13T00:00:00.000Z", NOW), "started just now");
});

test("startedWords: text that is not a time is echoed; no text at all reads 'start time unknown'", () => {
  assert.equal(v.startedWords("yesterday-ish", NOW), "started yesterday-ish");
  for (const bad of ["", null, undefined, 1_700_000_000, 5, 12, {}, [], true]) assert.equal(v.startedWords(bad, NOW), "start time unknown", String(bad));
});

// ---- details of one workflow --------------------------------------------------------------------------

const FLOWS = [
  { name: "nightly", schedule: "0 9 * * mon-fri", enabled: true, prompt_template: "Run `cao workflow run daily-report` and summarize" },
  { name: "paused", schedule: "*/7 3 * * *", enabled: false, prompt_template: "cao workflow run daily-report" },
  { name: "other", schedule: "0 1 * * *", enabled: true, prompt_template: "cao workflow run daily-report-2" },
  { name: "plain", schedule: "0 2 * * *", enabled: true, prompt_template: "say hello" },
];
const detailsOf = (over = {}) => v.workflowDetailsView(frozen({
  name: "daily-report", row: row("daily-report", { description: "Daily report", mode: "pipeline", step_count: 4 }), runs: [], flows: FLOWS, now: NOW, ...over,
}));

test("workflowDetailsView: the description, mode and step count are the list row's; the server's file layout is not shown", () => {
  const d = detailsOf({ row: row("daily-report", { description: "Daily report", mode: "pipeline", step_count: 4, source_path: "/srv/SECRET-LAYOUT.yaml" }) });
  assert.equal(d.name, "daily-report");
  assert.equal(d.description, "Daily report");
  assert.equal(d.mode, "pipeline");
  assert.equal(d.stepCount, 4);
  assert.equal(d.stepText, "4");
  assert.equal(d.listed, true);
  assert.ok(!JSON.stringify(d).includes("SECRET-LAYOUT"));
});

test("workflowDetailsView: the details say workflows have no scheduler of their own", () => {
  assert.match(detailsOf().schedulerNote, /no scheduler of their own/);
});

test("workflowDetailsView: a step count that is not a whole number reads 'unknown'; zero reads '0'", () => {
  for (const bad of [undefined, null, "3", 2.5, NaN]) assert.equal(detailsOf({ row: row("daily-report", { step_count: bad }) }).stepText, "unknown", String(bad));
  assert.equal(detailsOf({ row: row("daily-report", { step_count: 0 }) }).stepText, "0");
});

test("workflowDetailsView: a workflow the list no longer holds is not 'listed', and still shows its runs", () => {
  const d = detailsOf({ row: null, runs: [run("running", { workflow_name: "daily-report" })] });
  assert.equal(d.listed, false);
  assert.equal(d.name, "daily-report");
  assert.equal(d.runs.length, 1);
  for (const bad of [undefined, 5, "x"]) assert.doesNotThrow(() => detailsOf({ row: bad }));
});

test("Schedule link: 'Scheduled by' lists the flows whose prompt runs this workflow, with the schedule in words and the enabled state", () => {
  assert.deepEqual(detailsOf().scheduledBy, [
    { name: "nightly", cron: "0 9 * * mon-fri", words: "weekdays at 09:00", enabled: true, enabledText: "enabled" },
    { name: "paused", cron: "*/7 3 * * *", words: "*/7 3 * * *", enabled: false, enabledText: "disabled" },
  ]);
});

test("workflowDetailsView: a flow counts as enabled only when the server says exactly true, and the words agree", () => {
  for (const enabled of [true, false, 1, "true", "yes", null, undefined]) {
    const flows = [{ name: "f", schedule: "0 9 * * *", enabled, prompt_template: "cao workflow run daily-report" }];
    const [f] = detailsOf({ flows }).scheduledBy;
    assert.equal(f.enabled, enabled === true, String(enabled));
    assert.equal(f.enabledText, enabled === true ? "enabled" : "disabled", String(enabled));
  }
});

test("workflowDetailsView: a flow for a workflow with a longer name, or an unrelated flow, is not listed", () => {
  const d = detailsOf({ name: "daily", row: row("daily") });
  assert.deepEqual(d.scheduledBy, []);
  assert.deepEqual(detailsOf({ flows: null }).scheduledBy, []);
  assert.deepEqual(detailsOf({ flows: [null, 5, "x", {}] }).scheduledBy, []);
});

test("workflowDetailsView: a workflow name with regex characters matches only itself", () => {
  const flows = [{ name: "f", schedule: "0 9 * * *", enabled: true, prompt_template: "cao workflow run axb" }];
  assert.deepEqual(detailsOf({ name: "a.b", row: null, flows }).scheduledBy, []);
});

test("Cancel is shown for a running run and Resume for a cancelled one, nothing else", () => {
  const states = ["running", "completed", "failed", "cancelled", "queued", undefined];
  const d = detailsOf({ runs: states.map((s, i) => run(s, { run_id: `r${i}`, workflow_name: "daily-report" })) });
  assert.deepEqual(d.runs.map((r) => [r.state, r.cancel, r.resume]), [
    ["running", true, false], ["completed", false, false], ["failed", false, false], ["cancelled", false, true], ["queued", false, false], [undefined, false, false],
  ]);
});

test("workflowDetailsView: a run shows its state words, start time, current step, and keeps the server's order", () => {
  const d = detailsOf({
    runs: [
      run("running", { run_id: "newer", workflow_name: "daily-report", started_at: new Date(NOW - 5 * MIN).toISOString(), current_step_id: "review" }),
      run("completed", { run_id: "older", workflow_name: "daily-report", started_at: "2026-11-19T08:00:00Z" }),
      run("running", { run_id: "x", workflow_name: "someone-else" }),
    ],
  });
  assert.deepEqual(d.runs.map((r) => r.id), ["newer", "older"]);
  assert.equal(d.runs[0].stateText, "running");
  assert.equal(d.runs[0].startedText, "started 5 minutes ago");
  assert.equal(d.runs[0].step, "review");
  assert.equal(d.runs[1].stateText, "succeeded");
  assert.equal(d.runs[1].step, null);
});

test("workflowDetailsView: a current step that is not text reads as no step", () => {
  for (const bad of ["", null, 3, {}]) assert.equal(detailsOf({ runs: [run("running", { workflow_name: "daily-report", current_step_id: bad })] }).runs[0].step, null, JSON.stringify(bad));
});

test("workflowDetailsView: the run id is shortened after 12 characters (12 stay whole, 13 become 8 and '...')", () => {
  const ids = (id) => detailsOf({ runs: [run("running", { run_id: id, workflow_name: "daily-report" })] }).runs[0];
  assert.equal(ids("abcdefghijkl").shortId, "abcdefghijkl");
  assert.equal(ids("abcdefghijklm").shortId, "abcdefgh...");
  assert.equal(ids("abcdefghijklm").id, "abcdefghijklm");
});

test("workflowDetailsView: a run that cannot be addressed offers neither Cancel nor Resume", () => {
  for (const id of ["a b", "../x", "a\n", "", undefined, null, 7]) {
    for (const state of ["running", "cancelled"]) {
      const r = detailsOf({ runs: [run(state, { run_id: id, workflow_name: "daily-report" })] }).runs[0];
      assert.equal(r.cancel, false, JSON.stringify(id));
      assert.equal(r.resume, false, JSON.stringify(id));
    }
  }
});

test("workflowDetailsView: a name that is not text falls back to the row's name", () => {
  assert.equal(detailsOf({ name: 5 }).name, "daily-report");
  assert.doesNotThrow(() => v.workflowDetailsView());
});

// ---- the dialogs of the writes -------------------------------------------------------------------------

test("runDialogSpec: Cancel and Resume start on the safe button, name the run, and have a Confirm that works", () => {
  for (const action of ["cancel", "resume"]) {
    const s = v.runDialogSpec(action, { run_id: "abcdefghijklm" });
    assert.equal(s.defaultFocus, "cancel", action);
    assert.equal(s.confirmEnabled, true, action);
    assert.notEqual(s.confirmLabel, s.cancelLabel);
    assert.ok(s.title.includes("abcdefgh..."), s.title);
    assert.ok(!s.title.includes("abcdefghijklm"), s.title);
  }
  assert.equal(v.runDialogSpec("cancel", { id: "r1" }).title, "Cancel run r1?");
  assert.equal(v.runDialogSpec("resume", { run_id: "r1" }).title, "Resume run r1?");
});

test("runDialogSpec: Cancel is marked destructive; Resume says its agents work with nobody watching", () => {
  assert.equal(v.runDialogSpec("cancel", { run_id: "r1" }).danger, true);
  assert.match(v.runDialogSpec("resume", { run_id: "r1" }).body, /nobody watching/);
});

test("runDialogSpec: no other write can be dressed as a run write, and a missing run does not throw", () => {
  for (const a of ["approve", "plans/approve", "delete", "CANCEL", "", "__proto__", "constructor", "hasOwnProperty", "toString", null, undefined, 5, {}]) {
    assert.throws(() => v.runDialogSpec(a, { run_id: "r1" }), /no dialog/, JSON.stringify(a));
  }
  assert.equal(v.runDialogSpec("cancel", undefined).title, "Cancel run ?");
});

test("runDialogSpec: each call returns its own object", () => {
  const a = v.runDialogSpec("cancel", { run_id: "r1" });
  a.title = "changed";
  a.confirmEnabled = false;
  const b = v.runDialogSpec("cancel", { run_id: "r1" });
  assert.equal(b.title, "Cancel run r1?");
  assert.equal(b.confirmEnabled, true);
});

test("every dialog of this module has confirmEnabled === true and Enter on its first focus does not confirm", () => {
  const specs = [v.runDialogSpec("cancel", { run_id: "r1" }), v.runDialogSpec("resume", { run_id: "r1" }), v.workflowDialogSpec("reload"), v.workflowDialogSpec("template")];
  for (const s of specs) {
    assert.equal(s.confirmEnabled, true, s.title);
    assert.equal(s.defaultFocus, "cancel", s.title);
    assert.equal(dialogKey({ focus: s.defaultFocus }, s, "Enter").action, "cancel", s.title);
    assert.equal(dialogKey({ focus: "confirm" }, s, "Enter").action, "confirm", s.title);
  }
});

// ---- the check before a run write, and its outcome ------------------------------------------------------

const SNAP = { run_id: "r1", state: "running" };
const live = (state, over = {}) => [run("running", { run_id: "other" }), run(state, { run_id: "r1", ...over })];

test("runWriteCheck: Cancel is sent while the run is still running in the latest list", () => {
  assert.deepEqual(v.runWriteCheck("cancel", frozen(SNAP), frozen(live("running"))), { send: true, reason: null });
});

test("runWriteCheck: Cancel is not sent when the run has moved on, and the reason names where it is now", () => {
  for (const [state, word] of [["completed", "succeeded"], ["failed", "failed"], ["cancelled", "cancelled"]]) {
    const r = v.runWriteCheck("cancel", SNAP, live(state));
    assert.equal(r.send, false, state);
    assert.ok(r.reason.includes(`The run is ${word} now`), r.reason);
    assert.match(r.reason, /nothing was sent/);
  }
});

test("runWriteCheck: Resume is sent only while the run is still cancelled", () => {
  const snap = { run_id: "r1", state: "cancelled" };
  assert.equal(v.runWriteCheck("resume", snap, live("cancelled")).send, true);
  for (const state of ["running", "completed", "failed", "queued"]) assert.equal(v.runWriteCheck("resume", snap, live(state)).send, false, state);
});

test("runWriteCheck: when the live state differs from the snapshot nothing is sent, for every state pair", () => {
  const states = ["running", "completed", "failed", "cancelled"];
  for (const [action, need] of [["cancel", "running"], ["resume", "cancelled"]]) {
    for (const liveState of states) {
      const r = v.runWriteCheck(action, { run_id: "r1", state: need }, live(liveState));
      assert.equal(r.send, liveState === need, `${action} snapshot ${need} live ${liveState}`);
      assert.equal(r.reason === null, r.send);
    }
  }
});

test("runWriteCheck: a run that is not in the latest list is not written to", () => {
  const r = v.runWriteCheck("cancel", SNAP, [run("running", { run_id: "other" }), null, 5, "r1"]);
  assert.equal(r.send, false);
  assert.match(r.reason, /not in the list any more/);
  assert.equal(v.runWriteCheck("cancel", SNAP, []).send, false);
});

test("runWriteCheck: entries in the list that are not objects are skipped, not fatal", () => {
  assert.equal(v.runWriteCheck("cancel", SNAP, [null, 5, "x", undefined, ...live("running")]).send, true);
});

test("runWriteCheck: a run that cannot be addressed, or an action that is not cancel or resume, is never sent", () => {
  const list = [run("running", { run_id: "r1" })];
  for (const a of ["approve", "plans/approve", "delete", "", null, undefined, "__proto__"]) assert.equal(v.runWriteCheck(a, SNAP, list).send, false, String(a));
  for (const snap of [null, undefined, {}, { run_id: "a b" }, { run_id: "../x" }, { run_id: 5 }, "r1"]) {
    const r = v.runWriteCheck("cancel", snap, [run("running", { run_id: "a b" }), ...list]);
    assert.equal(r.send, false, JSON.stringify(snap));
    assert.match(r.reason, /cannot be addressed/);
  }
});

test("runWriteCheck: with no run list at all there is nothing to contradict the snapshot, so it is sent", () => {
  for (const none of [null, undefined, "running", {}]) assert.deepEqual(v.runWriteCheck("cancel", SNAP, none), { send: true, reason: null });
  assert.equal(v.runWriteCheck("cancel", { run_id: "a b" }, null).send, false);
});

test("Cancel a run: a requested cancel says the run shows 'cancelled' after the next refresh", () => {
  const r = v.runWriteOutcome("cancel", reply(200, { ok: true }));
  assert.equal(r.type, "done");
  assert.match(r.text, /Cancel requested/);
  assert.match(r.text, /cancelled/);
  assert.match(r.text, /next refresh/);
});

test("runWriteOutcome: a requested resume says so and does not claim a new state", () => {
  const r = v.runWriteOutcome("resume", reply(200, {}));
  assert.equal(r.type, "done");
  assert.match(r.text, /Resume requested/);
  assert.match(r.text, /next refresh/);
});

test("Resume fails: an error answer is shown as the server's words, as a failure, with no claim that anything was requested", () => {
  const r = v.runWriteOutcome("resume", reply(409, { detail: "run is not cancelled" }));
  assert.deepEqual(r, { type: "failed", text: "run is not cancelled" });
  for (const code of [400, 404, 422, 500, 503]) assert.equal(v.runWriteOutcome("resume", reply(code, { detail: `e${code}` })).type, "failed");
});

test("runWriteOutcome: a write's own 401 or 403 is shown as the auth message, as its own type", () => {
  for (const code of [401, 403]) assert.deepEqual(v.runWriteOutcome("cancel", reply(code, { detail: "no" })), { type: "auth", text: AUTH });
});

test("runWriteOutcome: no answer says the run may have changed and to wait before trying again", () => {
  const r = v.runWriteOutcome("cancel", UNREACHABLE);
  assert.equal(r.type, "failed");
  assert.match(r.text, /did not answer/);
  assert.match(r.text, /wait for the next refresh before trying again/);
});

test("runWriteOutcome: an outcome without words still fails with some words; the approve action has no outcome", () => {
  for (const o of [null, undefined, {}, { kind: "weird" }, { kind: "error", status: 500 }]) {
    const r = v.runWriteOutcome("cancel", o);
    assert.equal(r.type, "failed");
    assert.ok(r.text.length > 0, JSON.stringify(o));
  }
  assert.throws(() => v.runWriteOutcome("approve", reply(200, {})), /no outcome/);
  assert.throws(() => v.runWriteOutcome("__proto__", reply(200, {})), /no outcome/);
});

test("readFailureText: CAO not answering, the auth message, the server's words, and a fallback that names what was read", () => {
  assert.equal(v.readFailureText(UNREACHABLE, "the workflow list"), "CAO did not answer");
  assert.equal(v.readFailureText(reply(401, {}), "the workflow list"), AUTH);
  assert.equal(v.readFailureText({ kind: "auth" }, "the workflow list"), AUTH);
  assert.equal(v.readFailureText(reply(404, { detail: "unknown workflow 'x'" }), "the workflow"), "unknown workflow 'x'");
  assert.equal(v.readFailureText({ kind: "error", status: 500 }, "the workflow list"), "the workflow list could not be read");
  assert.equal(v.readFailureText(null, "the workflow"), "the workflow could not be read");
});

// ---- Run: the inputs form -----------------------------------------------------------------------------

test("freeInputs: named rows become inputs, values stay text, a name is trimmed and a value is not", () => {
  const r = v.freeInputs(frozen([{ key: " n ", value: "5" }, { key: "flag", value: "true" }, { key: "t", value: "  x\n" }]));
  assert.deepEqual(r.inputs, { n: "5", flag: "true", t: "  x\n" });
  assert.deepEqual(r.errors, {});
  assert.deepEqual(r.rowErrors, {});
});

test("freeInputs: an empty row is ignored; a key with an empty value is sent as empty text", () => {
  const r = v.freeInputs([{ key: "", value: "" }, { key: "   ", value: "" }, {}, null, 5, "x", { key: "a", value: "" }, { key: "b", value: null }]);
  assert.deepEqual(r.inputs, { a: "", b: "" });
  assert.deepEqual(r.rowErrors, {});
});

test("freeInputs: a value with no name is a problem at its row's index, counting ignored rows", () => {
  const r = v.freeInputs([{ key: "", value: "" }, { key: "", value: "x" }, { key: "  ", value: "y" }, { key: 5, value: "z" }]);
  assert.deepEqual(r.rowErrors, { 1: "needs a name", 2: "needs a name", 3: "needs a name" });
  assert.deepEqual(r.inputs, {});
});

test("freeInputs: a name used twice (also once trimmed) is a problem at the second row and the first value stays", () => {
  const r = v.freeInputs([{ key: "a", value: "1" }, { key: " a ", value: "2" }, { key: "A", value: "3" }]);
  assert.deepEqual(r.rowErrors, { 1: '"a" is used twice' });
  assert.deepEqual(r.inputs, { a: "1", A: "3" });
  const gap = v.freeInputs([{ key: "a", value: "1" }, { key: "", value: "" }, { key: "a", value: "2" }]);
  assert.deepEqual(gap.rowErrors, { 2: '"a" is used twice' }, "the index counts the ignored row");
});

test("freeInputs: a hostile name is an own entry and changes no prototype", () => {
  const r = v.freeInputs([{ key: "__proto__", value: "polluted" }, { key: "constructor", value: "c" }, { key: "toString", value: "t" }, { key: "hasOwnProperty", value: "h" }]);
  assert.equal(Object.getPrototypeOf(r.inputs), Object.prototype);
  assert.equal(Object.hasOwn(r.inputs, "__proto__"), true);
  assert.deepEqual(Object.keys(r.inputs), ["__proto__", "constructor", "toString", "hasOwnProperty"]);
  assert.equal(JSON.parse(JSON.stringify(r.inputs)).__proto__, "polluted");
  assert.equal(({}).polluted, undefined);
  const twice = v.freeInputs([{ key: "__proto__", value: "1" }, { key: "__proto__", value: "2" }]);
  assert.deepEqual(twice.rowErrors, { 1: '"__proto__" is used twice' });
  assert.equal(Object.getPrototypeOf(twice.rowErrors), Object.prototype);
});

test("freeInputs: unicode names and a very long value are kept whole", () => {
  const big = "v".repeat(200_000);
  const r = v.freeInputs([{ key: "日本\u{1F600}", value: big }]);
  assert.equal(r.inputs["日本\u{1F600}"].length, 200_000);
});

test("freeInputs: rows that are not a list give no inputs and no problems", () => {
  for (const bad of [null, undefined, "a=b", 5, {}]) assert.deepEqual(v.freeInputs(bad), { inputs: {}, errors: {}, rowErrors: {} });
});

const TYPED = {
  free: false, label: null,
  fields: [
    { name: "count", type: "int", required: true, default: null }, { name: "verbose", type: "bool", required: false, default: null },
    { name: "title", type: "string", required: false, default: null }, { name: "dir", type: "path", required: false, default: null },
  ],
};

test("runInputs: typed fields are sent as their declared type", () => {
  const r = v.runInputs(frozen(TYPED), frozen({ count: "5", verbose: "true", title: " a b ", dir: "/p" }), []);
  assert.deepEqual(r.inputs, { count: 5, verbose: true, title: " a b ", dir: "/p" });
  assert.deepEqual(r.errors, {});
  assert.deepEqual(r.rowErrors, {});
  assert.equal(v.runInputsOk(r), true);
});

test("runInputs: a value of the wrong type, or a missing required one, is a problem at its field and nothing is sendable", () => {
  const r = v.runInputs(TYPED, { count: "5.5", verbose: "yes" }, []);
  assert.deepEqual(r.errors, { count: "must be a whole number", verbose: "must be true or false" });
  assert.equal(v.runInputsOk(r), false);
  const missing = v.runInputs(TYPED, {}, []);
  assert.deepEqual(missing.errors, { count: "is required" });
  assert.equal(v.runInputsOk(missing), false);
});

test("runInputs: whole numbers are exact up to the largest safe integer", () => {
  const n = (s) => v.runInputs(TYPED, { count: s }, []);
  assert.equal(n("9007199254740991").inputs.count, 9007199254740991);
  assert.equal(n("-5").inputs.count, -5);
  for (const bad of ["9007199254740992", "+5", " 5", "5 ", "1e3", "0x10", "٣"]) assert.deepEqual(n(bad).errors, { count: "must be a whole number" }, bad);
});

test("runInputs: an empty optional field is left out; undeclared values are not sent", () => {
  const r = v.runInputs(TYPED, { count: "1", title: "", extra: "x", verbose: undefined }, []);
  assert.deepEqual(r.inputs, { count: 1 });
});

test("runInputs: a field named like an object property reads only an own value", () => {
  const form = { free: false, fields: [{ name: "constructor", type: "string", required: true }, { name: "__proto__", type: "string", required: false }] };
  const none = v.runInputs(form, {}, []);
  assert.deepEqual(none.errors, { constructor: "is required" });
  const own = JSON.parse('{"constructor":"c","__proto__":"p"}');
  const r = v.runInputs(form, own, []);
  assert.equal(Object.hasOwn(r.inputs, "__proto__"), true);
  assert.equal(r.inputs.constructor, "c");
  assert.equal(Object.getPrototypeOf(r.inputs), Object.prototype);
});

test("Inputs not readable: free rows are used, with their problems by row and none by field", () => {
  const form = { free: true, fields: [], label: "inputs not described by the spec" };
  const r = v.runInputs(form, { ignored: "x" }, [{ key: "a", value: "1" }, { key: "", value: "x" }]);
  assert.deepEqual(r.inputs, { a: "1" });
  assert.deepEqual(r.errors, {});
  assert.deepEqual(r.rowErrors, { 1: "needs a name" });
  assert.equal(v.runInputsOk(r), false);
  assert.equal(v.runInputsOk(v.runInputs(form, {}, [{ key: "a", value: "1" }])), true);
});

test("runInputs: a typed form ignores free rows; a form that is not an object has no inputs", () => {
  assert.deepEqual(v.runInputs(TYPED, { count: "1" }, [{ key: "x", value: "y" }]).inputs, { count: 1 });
  for (const bad of [null, undefined, "x", {}, { fields: "x" }, { fields: {} }, { fields: 5 }]) assert.deepEqual(v.runInputs(bad, { a: "1" }, []), { inputs: {}, errors: {}, rowErrors: {} });
});

test("runInputsOk: true only when there is no field problem and no row problem", () => {
  assert.equal(v.runInputsOk({ errors: {}, rowErrors: {} }), true);
  assert.equal(v.runInputsOk({ errors: { a: "x" }, rowErrors: {} }), false);
  assert.equal(v.runInputsOk({ errors: {}, rowErrors: { 0: "x" } }), false);
});

// ---- Run: the answer and plan approval -----------------------------------------------------------------

test("submitOutcomeView: a 202 is a started run, with its id when the answer names one as text", () => {
  assert.deepEqual(v.submitOutcomeView(reply(202, { run_id: "r9" })), { type: "started", runId: "r9" });
  for (const body of [{}, { run_id: 5 }, { run_id: null }, "ok"]) assert.deepEqual(v.submitOutcomeView(reply(202, body)), { type: "started", runId: null });
});

test("Approval refusal is not auth: a 403 approval_required is the approval type with no auth words", () => {
  const r = v.submitOutcomeView(approvalOutcome());
  assert.deepEqual(r, { type: "approval" });
  assert.notEqual(v.submitOutcomeView(reply(403, { detail: "forbidden" })).type, "approval");
});

test("submitOutcomeView: 401, and a 403 that is not an approval refusal, are the auth message", () => {
  for (const o of [reply(401, {}), reply(403, { detail: "no" }), reply(403, { detail: { kind: "other" } })]) assert.deepEqual(v.submitOutcomeView(o), { type: "auth", text: AUTH });
});

test("submitOutcomeView: no answer says the run may have started and to look in the runs list first", () => {
  const r = v.submitOutcomeView(UNREACHABLE);
  assert.equal(r.type, "unreachable");
  assert.match(r.text, /may have started/);
  assert.match(r.text, /runs list before you submit again/);
});

test("submitOutcomeView: an error answer is the server's words; an outcome without words still fails with words", () => {
  assert.deepEqual(v.submitOutcomeView(reply(422, { detail: [{ loc: ["body", "name_or_path"], msg: "field required" }] })), { type: "failed", text: "body.name_or_path: field required" });
  assert.deepEqual(v.submitOutcomeView(reply(404, { detail: "unknown workflow 'x'" })), { type: "failed", text: "unknown workflow 'x'" });
  for (const o of [null, undefined, {}]) assert.deepEqual(v.submitOutcomeView(o), { type: "failed", text: "failed" });
});

const stateFor = (planId, message) => {
  const o = approvalOutcome(planId, message);
  return { outcome: o, record: v.approvalRecord(null, o, { name: "wf", inputs: { a: "1" }, workingDirectory: "/p" }) };
};

test("approvalView: nothing is drawn unless approval is needed", () => {
  for (const s of [null, undefined, {}, { needed: false }, "needed", 5]) assert.equal(v.approvalView(s), null, String(JSON.stringify(s)));
});

test("First run needs approval: the view has the headline, the plan id, the approve command with Copy, and the submit-again action", () => {
  const a = v.approvalView(stateFor("wf:abc123").record.state);
  assert.equal(a.headline, "Plan approval needed. Approve it in a terminal");
  assert.equal(a.planText, "Plan id: wf:abc123");
  assert.deepEqual(shellArgv(a.command, "cao"), ["workflow", "approve", "wf:abc123"]);
  assert.ok(a.command.startsWith("cao workflow approve "), a.command);
  assert.equal(a.copyLabel, "Copy");
  assert.equal(a.submitAgainLabel, "I approved it: submit again");
  assert.deepEqual(a.localRun, { status: "not submitted", note: "approval needed" });
});

test("approvalView: the window offers no control that calls the approve route", () => {
  const a = v.approvalView(frozen(stateFor("wf:abc123").record.state));
  assert.ok(!JSON.stringify(a).includes("plans/approve"));
  for (const val of Object.values(a)) assert.notEqual(typeof val, "function");
  assert.deepEqual(Object.keys(a).sort(), ["command", "copyLabel", "headline", "localRun", "planText", "serverMessage", "submitAgainLabel"]);
});

test("approvalView: the command is safe to paste whatever plan id the server sends", () => {
  const cwd = globDir();
  for (const id of [...HARD_WORDS, "wf:abc", "a b", "x\ny", "it's", "$(id)", "é 日本", "\u{1F600}"]) {
    const a = v.approvalView(stateFor(id).record.state);
    assert.deepEqual(shellArgv(a.command, "cao", { cwd }), ["workflow", "approve", id], JSON.stringify(id));
  }
  for (const id of ["--help", "-x", "-rf"]) {
    assert.deepEqual(shellArgv(v.approvalView(stateFor(id).record.state).command, "cao").slice(-2), ["--", id], id);
  }
});

test("approvalView: with no plan id it says so and has no command to copy", () => {
  for (const id of [null, "", 7, {}]) {
    const a = v.approvalView(stateFor(id).record.state);
    assert.equal(a.planText, "The server gave no plan id.", String(JSON.stringify(id)));
    assert.equal(a.command, null);
  }
  const none = v.approvalRecord(null, reply(403, { detail: { kind: "approval_required" } }), {}).state;
  assert.equal(v.approvalView(none).planText, "The server gave no plan id.");
  assert.equal(v.approvalView(none).command, null);
});

test("approvalView: the server's own words are clipped to 300 characters", () => {
  const msg = (n) => v.approvalView(stateFor("p", "m".repeat(n)).record.state).serverMessage;
  assert.equal(msg(300).length, 300);
  assert.equal(msg(301).length, 300);
  assert.equal(msg(0), "");
  assert.equal(v.approvalView(stateFor("p", 5).record.state).serverMessage, "");
});

test("approvalRecord: an approval refusal keeps the submission as sent, ready to go again", () => {
  const submission = { name: "wf", inputs: { a: "1", n: 2 }, workingDirectory: "/p" };
  const rec = v.approvalRecord(null, approvalOutcome("wf:abc"), submission);
  assert.equal(rec.state.needed, true);
  assert.equal(rec.state.planId, "wf:abc");
  assert.equal(rec.sending, false);
  assert.deepEqual(rec.submission, submission);
});

test("Submit again after approval: the same inputs go again as the same request, and a 202 drops the local row", () => {
  const submission = { name: "wf", inputs: { a: "1", n: 2 }, workingDirectory: "/p" };
  const first = submitRequest(submission.name, submission.inputs, submission.workingDirectory);
  const rec = v.approvalRecord(null, approvalOutcome("wf:abc"), submission);
  const again = submitRequest(rec.submission.name, rec.submission.inputs, rec.submission.workingDirectory);
  assert.deepEqual(again, first);
  assert.equal(v.approvalRecord(rec, reply(202, { run_id: "r1" }), submission), null);
});

test("approvalRecord: a second refusal replaces the first, and clears 'sending'", () => {
  const rec1 = { ...v.approvalRecord(null, approvalOutcome("wf:one"), { name: "wf", inputs: {}, workingDirectory: "" }), sending: true };
  const sub2 = { name: "wf", inputs: { b: "2" }, workingDirectory: "" };
  const rec2 = v.approvalRecord(rec1, approvalOutcome("wf:two"), sub2);
  assert.equal(rec2.state.planId, "wf:two");
  assert.equal(rec2.sending, false);
  assert.deepEqual(rec2.submission, sub2);
});

test("approvalRecord: any other answer leaves the row as it was; with no row there is still none", () => {
  const rec = v.approvalRecord(null, approvalOutcome("wf:one"), { name: "wf", inputs: {}, workingDirectory: "" });
  for (const o of [reply(500, { detail: "x" }), UNREACHABLE, reply(401, {}), reply(403, { detail: "no" }), reply(422, { detail: "bad" }), null, {}]) {
    assert.equal(v.approvalRecord(rec, o, { name: "wf" }), rec);
    assert.equal(v.approvalRecord(null, o, { name: "wf" }), null);
  }
});

test("approvalRecord: nothing it is given is changed", () => {
  const prior = frozen(v.approvalRecord(null, approvalOutcome("wf:one"), { name: "wf", inputs: { a: "1" }, workingDirectory: "" }));
  assert.doesNotThrow(() => v.approvalRecord(prior, frozen(approvalOutcome("wf:two")), frozen({ name: "wf", inputs: {}, workingDirectory: "" })));
  assert.doesNotThrow(() => v.approvalRecord(prior, frozen(reply(202, {})), {}));
});

// ---- the editor's form ------------------------------------------------------------------------------------

test("emptyWorkflowForm: a YAML form with no name and no text, a new object each time", () => {
  assert.deepEqual(v.emptyWorkflowForm(), { name: "", tier: "yaml", source: "" });
  const a = v.emptyWorkflowForm();
  a.source = "typed";
  assert.equal(v.emptyWorkflowForm().source, "");
});

test("workflowEditorDirty: any change to the name, tier or text is dirty; typing and undoing it is not", () => {
  const initial = v.emptyWorkflowForm();
  assert.equal(v.workflowEditorDirty(v.emptyWorkflowForm(), initial), false);
  assert.equal(v.workflowEditorDirty({ ...initial, name: "a" }, initial), true);
  assert.equal(v.workflowEditorDirty({ ...initial, tier: "python" }, initial), true);
  assert.equal(v.workflowEditorDirty({ ...initial, source: " " }, initial), true);
  const typed = { ...initial, source: "abc" };
  assert.equal(v.workflowEditorDirty({ ...typed, source: "" }, initial), false);
});

test("workflowValidationKey: a validate answer belongs to its exact tier and text, down to one space", () => {
  const k = v.workflowValidationKey;
  assert.equal(k("yaml", "a"), k("yaml", "a"));
  assert.notEqual(k("yaml", "a"), k("yaml", "a "));
  assert.notEqual(k("yaml", "a"), k("yaml", "A"));
  assert.notEqual(k("yaml", "a"), k("python", "a"));
  assert.notEqual(k("yaml", "ab"), k("yaml", "a\nb"));
  assert.equal(k("yaml", null), k("yaml", ""));
  assert.equal(k("yaml", 5), k("yaml", ""));
  assert.equal(typeof k("python", "x"), "string");
});

test("workflowNameProblem: a bare name (letters, digits, - and _) has no problem", () => {
  for (const n of ["wf", "my-flow_2", "A1", "-x", "_", "__proto__", "a".repeat(64), "9"]) assert.equal(v.workflowNameProblem(n), null, n);
});

test("workflowNameProblem: a path, an extension, a space, a newline, a non-ASCII or non-text name is refused with the same words", () => {
  for (const n of [...HOSTILE_NAMES, ...NOT_STRINGS]) {
    const p = v.workflowNameProblem(n);
    assert.equal(typeof p, "string", String(typeof n === "string" ? JSON.stringify(n) : typeof n));
    assert.match(p, /bare name/);
    assert.match(p, /no path and no extension/);
  }
});

test("workflowNameProblem: every name it accepts is a route", () => {
  for (const n of [...HOSTILE_NAMES, "ok", "a-b_c", "x\n"]) assert.equal(v.workflowNameProblem(n) === null, v.workflowRoute(n) !== null, JSON.stringify(n));
});

// ---- what validate said -------------------------------------------------------------------------------------

test("validateReport: a clean YAML answer has nothing to show", () => {
  assert.deepEqual(v.validateReport({ status: "pass", reserved_notes: [], errors: [] }), { status: "pass", blocking: [], warnings: [] });
});

test("Warnings only: reserved-construct notes and warning findings are warnings and block nothing", () => {
  const r = v.validateReport(frozen({
    status: "pass_reserved", reserved_notes: ["mode parallel is reserved: not built yet"], errors: [],
    findings: [{ line: 7, severity: "warning", rule_id: "r", message: "careful" }],
  }));
  assert.deepEqual(r.blocking, []);
  assert.deepEqual(r.warnings, [{ line: 7, text: "careful" }, { line: null, text: "mode parallel is reserved: not built yet" }]);
});

test("validateReport: a blocking finding is shown at its line, and the Python tier's mirrored error string is not shown twice", () => {
  const r = v.validateReport({
    status: "fail", errors: ["line 3: [no_eval] eval is not allowed", "line 9: [syntax] bad"],
    findings: [{ line: 3, severity: "error", rule_id: "no_eval", message: "eval is not allowed" }, { line: 9, severity: "error", rule_id: "syntax", message: "bad" }],
  });
  assert.deepEqual(r.blocking, [{ line: 3, text: "eval is not allowed" }, { line: 9, text: "bad" }]);
  assert.equal(v.findingWords(r.blocking[0]), "line 3: eval is not allowed");
});

test("validateReport: a YAML failure's error strings are blocking, with the line read from 'line N:' when it has one", () => {
  const r = v.validateReport({ status: "fail", errors: ["workflow must declare at least one step", "line 4: step id 'x y' is invalid"] });
  assert.deepEqual(r.blocking, [{ line: null, text: "workflow must declare at least one step" }, { line: 4, text: "line 4: step id 'x y' is invalid" }]);
});

test("validateReport: an error string that no finding says is added after the findings", () => {
  const r = v.validateReport({ status: "fail", errors: ["line 2: [r] x", "profile 'p' is not installed"], findings: [{ line: 2, severity: "error", message: "x" }] });
  assert.deepEqual(r.blocking, [{ line: 2, text: "x" }, { line: null, text: "profile 'p' is not installed" }]);
});

test("validateReport: a failure with nothing to show still blocks, with a general line", () => {
  assert.deepEqual(v.validateReport({ status: "fail" }).blocking, [{ line: null, text: "The server says this source does not pass validation." }]);
  assert.equal(v.validateReport({ status: "fail", errors: [], findings: [] }).blocking.length, 1);
  assert.equal(v.validateReport({ status: "pass" }).blocking.length, 0);
});

test("validateReport: an answer that is not an object says nothing and has no status", () => {
  for (const bad of [null, undefined, [], "pass", 5, true]) assert.deepEqual(v.validateReport(bad), { status: null, blocking: [], warnings: [] }, String(bad));
});

test("validateReport: entries of the wrong kind are skipped and a line that is not a whole number is no line", () => {
  const r = v.validateReport({
    status: "fail", errors: [5, null, {}, "bad one"],
    findings: [null, 5, "x", [], { severity: "info", message: "ignored" }, { severity: "error", line: "3", message: "a" }, { severity: "error", line: 3.5, message: "b" }, { severity: "error", line: null }, { severity: "warning", line: 2, message: "w" }],
    reserved_notes: [1, null, "n"],
  });
  assert.deepEqual(r.blocking, [{ line: null, text: "a" }, { line: null, text: "b" }, { line: null, text: "" }, { line: null, text: "bad one" }]);
  assert.deepEqual(r.warnings, [{ line: 2, text: "w" }, { line: null, text: "n" }]);
  for (const bad of ["x", 5, {}, null]) assert.deepEqual(v.validateReport({ status: "pass", findings: bad, errors: bad, reserved_notes: bad }).blocking, []);
});

test("findingWords: a finding with no line is its text; with a line it is 'line N: text'", () => {
  assert.equal(v.findingWords({ line: null, text: "x" }), "x");
  assert.equal(v.findingWords({ line: 12, text: "x" }), "line 12: x");
});

const RESULTS = [
  { status: "pass", errors: [], findings: [] },
  { status: "pass_reserved", errors: [], reserved_notes: ["n"], findings: [] },
  { status: "pass", errors: [], findings: [{ line: 1, severity: "warning", message: "w" }] },
  { status: "pass", errors: [], findings: [{ line: 1, severity: "error", message: "e" }] },
  { status: "pass", errors: ["bad"], findings: [] },
  { status: "pass", errors: [5], findings: [] },
  { status: "pass", errors: "bad" },
  { status: "fail", errors: [], findings: [] },
  { status: "fail", errors: ["line 1: [r] e"], findings: [{ line: 1, severity: "error", message: "e" }] },
  { status: "maybe", errors: [] },
  { errors: [] },
  {}, null, undefined, [], "pass", 5,
];

test("whatever validate said, a blocking finding never leaves Save enabled, and warnings alone never turn it off", () => {
  for (const res of RESULTS) {
    const key = v.workflowValidationKey("yaml", "t");
    const report = v.validateReport(res);
    const saveOk = saveEnabled(key, key, res);
    if (report.blocking.length > 0) assert.equal(saveOk, false, JSON.stringify(res));
    if (saveOk) assert.deepEqual(report.blocking, [], JSON.stringify(res));
  }
  const warnOnly = { status: "pass", errors: [], findings: [{ line: 1, severity: "warning", message: "w" }] };
  assert.equal(saveEnabled("k", "k", warnOnly), true);
  assert.equal(v.validateReport(warnOnly).warnings.length, 1);
});

test("validateOutcomeNote: an answer has no note; each way of not getting one has its own", () => {
  assert.equal(v.validateOutcomeNote(reply(200, { status: "pass" })), null);
  assert.match(v.validateOutcomeNote(UNREACHABLE), /CAO did not answer, so the source could not be checked/);
  assert.equal(v.validateOutcomeNote(reply(401, {})), AUTH);
  assert.equal(v.validateOutcomeNote(reply(500, { detail: "boom" })), "CAO could not check the workflow: boom");
  assert.equal(v.validateOutcomeNote({ kind: "error" }), "CAO could not check the workflow: error");
  assert.equal(v.validateOutcomeNote(null), "CAO could not check the workflow: error");
});

test("Source too large in argument mode: a request refused before it was sent says why, in its own words", () => {
  let called = 0;
  const big = { name: "wf", source: "a".repeat(130_000), tier: "yaml" };
  return callCao(() => { called += 1; return { stdout: "\n200" }; }, { machine: "cao", port: 9889, stdin: false }, { method: "POST", path: "/workflows/validate", body: big, cli: "cao workflow create --from-file" }).then((o) => {
    assert.equal(called, 0, "no request is sent");
    const note = v.validateOutcomeNote(o);
    assert.match(note, /too large to send from this window/);
    assert.match(note, /122880/);
    assert.match(note, /cao workflow create --from-file/);
    for (const kind of ["refused", "env_refused"]) assert.equal(v.validateOutcomeNote({ kind, message: "no go" }), "no go");
    assert.equal(v.validateOutcomeNote({ kind: "too_large" }), "the request was not sent");
  });
});

// ---- Validate and Save ---------------------------------------------------------------------------------------

const SRC = "name: wf\nsteps: []\n";
const PASS = { status: "pass", errors: [], findings: [] };
const gate = (over = {}) => v.workflowEditorGate(frozen({
  mode: "new", gate: { disabled: false }, check: { ok: true }, validating: false, sending: false, loading: false, name: "wf", tier: "yaml",
  source: SRC, validatedKey: v.workflowValidationKey("yaml", SRC), result: PASS, expectedHash: undefined, ...over,
}));

test("workflowEditorGate: a valid name, a clean validate of this exact text and no request out enables Save", () => {
  assert.deepEqual(gate(), { nameProblem: null, validate: true, save: true });
});

test("Edit after validate: changing the text keeps Validate on and turns Save off until the new text is validated", () => {
  const key = v.workflowValidationKey("yaml", SRC);
  assert.deepEqual(gate({ source: `${SRC} `, validatedKey: key }), { nameProblem: null, validate: true, save: false });
  const next = `${SRC}# more\n`;
  assert.equal(gate({ source: next, validatedKey: v.workflowValidationKey("yaml", next) }).save, true);
});

test("workflowEditorGate: changing the tier after a validate needs a new validate", () => {
  assert.equal(gate({ tier: "python" }).save, false);
  assert.equal(gate({ tier: "python" }).validate, true);
  const key = v.workflowValidationKey("python", SRC);
  assert.equal(gate({ tier: "python", validatedKey: key }).save, true);
});

test("workflowEditorGate: a changed name after a validate does not need a new validate (validate sends source and tier)", () => {
  assert.equal(gate({ name: "another" }).save, true);
});

test("workflowEditorGate: Save needs a validate answer that exists and for this text", () => {
  for (const validatedKey of [undefined, null, "", "other", 5]) assert.equal(gate({ validatedKey }).save, false, String(validatedKey));
  assert.equal(gate({ validatedKey: undefined }).validate, true);
});

test("Warnings only: an answer with warnings and no blocking finding leaves Save enabled", () => {
  const result = { status: "pass_reserved", reserved_notes: ["parallel is reserved"], errors: [], findings: [{ line: 2, severity: "warning", message: "w" }] };
  assert.equal(gate({ result }).save, true);
});

test("workflowEditorGate: a blocking finding, a failed or unknown answer, or no answer leaves Save off", () => {
  const bad = [
    { status: "pass", errors: [], findings: [{ line: 1, severity: "error", message: "e" }] }, { status: "fail", errors: ["x"] }, { status: "pass", errors: ["x"] },
    { status: "pass", errors: "x" }, { status: "maybe" }, {}, [], null, undefined, "pass",
  ];
  for (const result of bad) {
    const g = gate({ result });
    assert.equal(g.save, false, JSON.stringify(result));
    assert.equal(g.validate, true);
  }
});

test("workflowEditorGate: while a request is out, loading, or writes are disabled by auth, nothing works", () => {
  for (const over of [{ validating: true }, { sending: true }, { loading: true }, { gate: { disabled: true } }]) {
    assert.deepEqual(gate(over), { nameProblem: null, validate: false, save: false }, JSON.stringify(over));
  }
  assert.equal(gate({ gate: { disabled: "yes" } }).validate, true);
});

test("workflowEditorGate: a tier that is neither python nor yaml, or text that is empty or blank, cannot be validated", () => {
  for (const tier of ["ruby", "", undefined, null, "YAML"]) assert.deepEqual(gate({ tier, validatedKey: v.workflowValidationKey(tier, SRC) }), { nameProblem: null, validate: false, save: false }, String(tier));
  for (const source of ["", "  \n\t ", null, undefined, 5]) {
    const g = gate({ source, validatedKey: v.workflowValidationKey("yaml", source) });
    assert.equal(g.validate, false, JSON.stringify(source));
    assert.equal(g.save, false);
  }
});

test("workflowEditorGate: a check that is missing, or not exactly ok, disables both", () => {
  for (const check of [undefined, null, {}, { ok: false }, { ok: "yes" }, { ok: 1 }]) assert.deepEqual(gate({ check }), { nameProblem: null, validate: false, save: false }, JSON.stringify(check));
});

test("Source too large in argument mode: 125 KiB of encoded YAML disables Validate and Save", () => {
  const body = { name: "wf", source: "", tier: "yaml" };
  const overhead = Buffer.byteLength(JSON.stringify(body));
  const source = "a".repeat(125 * 1024 - overhead);
  const check = v.editorSizeCheck({ name: "wf", tier: "yaml", source }, { stdin: false });
  assert.equal(check.ok, false);
  assert.deepEqual(gate({ check, source, validatedKey: v.workflowValidationKey("yaml", source) }), { nameProblem: null, validate: false, save: false });
});

test("Source too large in stdin mode: 133120 characters of Python disables Validate and Save", () => {
  const source = "x".repeat(133_120);
  const check = v.editorSizeCheck({ name: "wf", tier: "python", source }, { stdin: true });
  assert.equal(check.ok, false);
  assert.deepEqual(gate({ check, tier: "python", source, validatedKey: v.workflowValidationKey("python", source) }), { nameProblem: null, validate: false, save: false });
});

test("workflowEditorGate: a new workflow needs a bare name to Save, but not to Validate", () => {
  for (const name of [...HOSTILE_NAMES, null, undefined, 5]) {
    const g = gate({ name });
    assert.equal(g.save, false, JSON.stringify(name));
    assert.equal(g.validate, true);
    assert.match(g.nameProblem, /bare name/);
  }
});

test("workflowEditorGate: an edit needs the hash read when editing began to Save, and does not ask for a name", () => {
  for (const expectedHash of [undefined, null, "", 5, {}]) {
    const g = gate({ mode: "edit", name: "../x", expectedHash });
    assert.equal(g.save, false, JSON.stringify(expectedHash));
    assert.equal(g.validate, true);
    assert.equal(g.nameProblem, null);
  }
  assert.deepEqual(gate({ mode: "edit", name: "../x", expectedHash: "abc" }), { nameProblem: null, validate: true, save: true });
});

test("workflowEditorGate: Save is never on while Validate is off, in any mix of conditions", () => {
  const bools = [true, false];
  for (const validating of bools) for (const sending of bools) for (const loading of bools) for (const disabled of bools) for (const ok of bools) for (const mode of ["new", "edit"]) {
    const g = gate({ validating, sending, loading, gate: { disabled }, check: { ok }, mode, expectedHash: "h" });
    if (g.save) assert.equal(g.validate, true);
    assert.equal(g.validate, !validating && !sending && !loading && !disabled && ok);
  }
});

// ---- Save: its answer and what the editor does with it -----------------------------------------------------

const STALE_DETAIL = "workflow 'wf' has changed on disk since it was read (expected source hash aaa, found bbb); re-read the spec and re-apply your changes";

test("workflowSaveResult: a created or updated workflow is saved, with the name the server gives when it is text", () => {
  assert.deepEqual(v.workflowSaveResult(reply(201, { name: "wf", path: "/x" }), "POST"), { type: "saved", name: "wf" });
  assert.deepEqual(v.workflowSaveResult(reply(200, { name: "wf", status: "pass" }), "PUT"), { type: "saved", name: "wf" });
  for (const body of [{}, { name: 5 }, "ok"]) assert.deepEqual(v.workflowSaveResult(reply(201, body), "POST"), { type: "saved", name: null });
});

test("Concurrent change: a PUT refused because the hash no longer matches says the workflow changed elsewhere, keeps the text and offers reload", () => {
  const r = v.workflowSaveResult(reply(409, { detail: STALE_DETAIL }), "PUT");
  assert.equal(r.type, "stale");
  assert.match(r.message, /changed elsewhere/);
  assert.equal(r.keepText, true);
  assert.equal(r.offerReload, true);
});

test("workflowSaveResult: any other 409 on a PUT shows the server's reason and offers no reload", () => {
  const detail = "workflow 'wf' is a Python workflow; update it with tier 'python'";
  const r = v.workflowSaveResult(reply(409, { detail }), "PUT");
  assert.equal(r.type, "conflict");
  assert.equal(r.message, detail);
  assert.equal(r.offerReload, false);
  assert.equal(v.workflowSaveResult({ kind: "error", status: 409, detail: "" }, "PUT").message, "The server refused the update.");
});

test("workflowSaveResult: a 409 on a POST says the name exists, even when its words look like a stale hash", () => {
  assert.equal(v.workflowSaveResult(reply(409, { detail: "workflow 'wf' already exists" }), "POST").type, "exists");
  assert.equal(v.workflowSaveResult(reply(409, { detail: STALE_DETAIL }), "POST").type, "exists");
  assert.match(v.workflowSaveResult(reply(409, { detail: "x" }), "POST").message, /name exists/);
});

test("workflowSaveResult: a 409 for any other method is not a conflict of the editor", () => {
  for (const m of ["GET", undefined, "DELETE"]) assert.deepEqual(v.workflowSaveResult(reply(409, { detail: "x" }), m), { type: "failed", text: "x" });
});

test("workflowSaveResult: other refusals show the server's words and are plain failures", () => {
  assert.deepEqual(v.workflowSaveResult(reply(404, { detail: "unknown workflow 'wf'" }), "PUT"), { type: "failed", text: "unknown workflow 'wf'" });
  assert.deepEqual(v.workflowSaveResult(reply(400, { detail: "bad name" }), "POST"), { type: "failed", text: "bad name" });
  assert.deepEqual(v.workflowSaveResult(reply(500, { detail: "storage failed" }), "PUT"), { type: "failed", text: "storage failed" });
  assert.deepEqual(v.workflowSaveResult(null, "POST"), { type: "failed", text: "failed" });
});

test("workflowSaveResult: a 401 or 403 is auth; no answer says the save may have happened", () => {
  for (const c of [401, 403]) assert.deepEqual(v.workflowSaveResult(reply(c, { detail: "no" }), "POST"), { type: "auth", text: AUTH });
  const u = v.workflowSaveResult(UNREACHABLE, "PUT");
  assert.equal(u.type, "unreachable");
  assert.match(u.text, /may have happened/);
  assert.match(u.text, /Workflows list before you save again/);
});

test("workflowSaveResult: a request refused before it was sent is shown with its own words", async () => {
  const o = await callCao(() => { throw new Error("must not run"); }, { machine: "cao", port: 9889, stdin: false }, {
    method: "POST", path: "/workflows", body: { name: "wf", source: "a".repeat(130_000), tier: "yaml" }, cli: "cao workflow create --from-file",
  });
  const r = v.workflowSaveResult(o, "POST");
  assert.equal(r.type, "failed");
  assert.match(r.text, /too large to send from this window/);
});

test("workflowSaveEffects: a saved workflow closes the editor and selects it, by the server's name or else the typed one", () => {
  assert.deepEqual(v.workflowSaveEffects({ type: "saved", name: "wf" }, { name: "typed" }), { done: true, selectName: "wf" });
  assert.deepEqual(v.workflowSaveEffects({ type: "saved", name: null }, { name: "typed" }), { done: true, selectName: "typed" });
});

test("workflowSaveEffects: a name that exists is said at the name, with no banner", () => {
  const e = v.workflowSaveEffects(v.workflowSaveResult(reply(409, { detail: "exists" }), "POST"), { name: "wf", authText: AUTH });
  assert.deepEqual(e, { done: false, banner: null, nameError: "A workflow with that name exists.", offerReload: false });
});

test("Concurrent change: the editor stays open, says it in a banner and offers Reload", () => {
  const e = v.workflowSaveEffects(v.workflowSaveResult(reply(409, { detail: STALE_DETAIL }), "PUT"), { name: "wf", authText: AUTH });
  assert.equal(e.done, false);
  assert.match(e.banner, /changed elsewhere/);
  assert.equal(e.nameError, null);
  assert.equal(e.offerReload, true);
});

test("workflowSaveEffects: every answer but a save keeps the editor open, and only a stale hash offers Reload", () => {
  const answers = [
    [reply(409, { detail: "x" }), "PUT"], [reply(500, { detail: "x" }), "POST"], [reply(401, {}), "POST"], [UNREACHABLE, "PUT"], [null, "POST"],
  ];
  for (const [o, m] of answers) {
    const e = v.workflowSaveEffects(v.workflowSaveResult(o, m), { name: "wf", authText: AUTH });
    assert.equal(e.done, false);
    assert.equal(e.offerReload, false);
    assert.equal(typeof e.banner, "string");
    assert.ok(e.banner.length > 0);
  }
  assert.equal(v.workflowSaveEffects({ type: "auth" }, { authText: AUTH }).banner, AUTH);
  assert.deepEqual(v.workflowSaveEffects(undefined), { done: false, banner: "failed", nameError: null, offerReload: false });
});

// ---- Edit: what the document gives -----------------------------------------------------------------------------

const YAML_DOC = {
  name: "wf", description: "d", mode: "sequential", inputs: {}, steps: [{ id: "a", provider: "claude_code", agent: "developer", prompt: "p" }],
  path: "/srv/specs/wf.yaml", source: "name: wf\nsteps: []\n", content_hash: "9f86d081884c7d659a2feaa0c55ad015",
};

test("editFromDocument: a YAML workflow gives its text and the hash that its PUT sends", () => {
  assert.deepEqual(v.editFromDocument(frozen(YAML_DOC), "wf"), { ok: true, tier: "yaml", source: "name: wf\nsteps: []\n", hash: "9f86d081884c7d659a2feaa0c55ad015" });
  assert.equal(v.editFromDocument({ ...YAML_DOC, path: "/srv/specs/wf.yml" }, "wf").ok, true);
});

test("editFromDocument: a document without usable text, hash or tier is not edited here, and the reason says so without sending the user to the CLI", () => {
  const bad = [null, undefined, "x", 5, [], {}, { ...YAML_DOC, source: undefined }, { ...YAML_DOC, source: 5 }, { ...YAML_DOC, content_hash: "" }, { ...YAML_DOC, content_hash: undefined }, { ...YAML_DOC, content_hash: 7 }];
  for (const doc of bad) {
    const r = v.editFromDocument(doc, "wf");
    assert.equal(r.ok, false, JSON.stringify(doc));
    assert.match(r.reason, /for wf /);
    assert.match(r.reason, /cannot be edited here/);
    assert.doesNotMatch(r.reason, /cao workflow update|--from-file|YAML specs only/);
  }
  const unnamed = v.editFromDocument(null, undefined).reason;
  assert.match(unnamed, /for this workflow /);
  assert.doesNotMatch(unnamed, /undefined/);
});

// What GET /workflows/{name} returns for a Python workflow: ScriptSpec.model_dump() (models/workflow.py), which
// carries `source` and `content_hash` as well, with no `steps` and no `mode`.
const PY_DOC = { name: "wf", path: "/srv/specs/wf.py", source: "print(1)\n", content_hash: "9f86d081884c7d659a2feaa0c55ad015", findings: [], inputs: {} };

test("Edit is for every workflow: a Python workflow's document gives tier python, its source and the hash its PUT sends", () => {
  const r = v.editFromDocument(frozen(PY_DOC), "wf");
  assert.deepEqual(r, { ok: true, tier: "python", source: "print(1)\n", hash: "9f86d081884c7d659a2feaa0c55ad015" });
});

// ---- templates, hints ------------------------------------------------------------------------------------------

const TPLS = [{ id: "p", tier: "python", name: "tp", source: "print('a')\n" }, { id: "y", tier: "yaml", name: "ty", source: "name: ty\n" }];

test("applyTemplate: the template's tier and text, and its name only when none is typed; the old form is unchanged", () => {
  const empty = frozen(v.emptyWorkflowForm());
  assert.deepEqual(v.applyTemplate(empty, TPLS[0]), { name: "tp", tier: "python", source: "print('a')\n" });
  assert.deepEqual(v.applyTemplate({ name: "mine", tier: "yaml", source: "typed" }, TPLS[1]), { name: "mine", tier: "yaml", source: "name: ty\n" });
  assert.deepEqual(v.applyTemplate({ name: " ", tier: "yaml", source: "" }, TPLS[1]).name, " ");
  assert.deepEqual(v.applyTemplate({ name: "", tier: "yaml", source: "", kept: 1 }, TPLS[1]), { name: "ty", tier: "yaml", source: "name: ty\n", kept: 1 });
  const out = v.applyTemplate(empty, TPLS[1]);
  assert.notEqual(out, empty);
});

test("templateNeedsConfirm: only text the user typed is replaced after asking", () => {
  const f = (source) => ({ name: "", tier: "yaml", source });
  assert.equal(v.templateNeedsConfirm(f(""), TPLS), false);
  assert.equal(v.templateNeedsConfirm(f("  \n\t"), TPLS), false);
  assert.equal(v.templateNeedsConfirm(f(TPLS[0].source), TPLS), false);
  assert.equal(v.templateNeedsConfirm(f(TPLS[1].source), TPLS), false);
  assert.equal(v.templateNeedsConfirm(f(`${TPLS[0].source} `), TPLS), true);
  assert.equal(v.templateNeedsConfirm(f("typed"), TPLS), true);
  assert.equal(v.templateNeedsConfirm(f("typed"), []), true);
  for (const bad of [null, undefined, "x", {}]) assert.equal(v.templateNeedsConfirm(f("typed"), bad), true);
  for (const bad of [null, undefined, {}, { source: 5 }, "typed"]) assert.equal(v.templateNeedsConfirm(bad, TPLS), false);
});

test("the shipped templates: a Python and a sequential YAML one, each a valid name and small enough to save from the window", () => {
  assert.deepEqual(TEMPLATES.map((t) => t.tier).sort(), ["python", "yaml"]);
  for (const t of TEMPLATES) {
    const form = v.applyTemplate(v.emptyWorkflowForm(), t);
    assert.equal(v.workflowNameProblem(form.name), null, t.id);
    assert.equal(form.tier, t.tier);
    assert.equal(v.editorSizeCheck(form, { stdin: false }).ok, true, t.id);
    assert.equal(v.editorSizeCheck(form, { stdin: true }).ok, true, t.id);
    assert.equal(v.templateNeedsConfirm(form, TEMPLATES), false, t.id);
    for (const other of TEMPLATES) assert.equal(v.templateNeedsConfirm(v.applyTemplate(form, other), TEMPLATES), false);
  }
  assert.match(TEMPLATES.find((t) => t.tier === "yaml").source, /mode: sequential/);
});

test("No file option: the one line names the CLI for a Python file and says a YAML spec is pasted into the editor", () => {
  const hint = v.workflowFileHint();
  assert.ok(hint.includes("`cao workflow create <name> --from-file <path>`"), hint);
  assert.match(hint, /Python only/);
  assert.match(hint, /YAML spec is pasted into the editor/);
  assert.ok(!hint.includes("\n"), "one line");
  assert.doesNotMatch(hint, /browse|choose a file|enter a path|upload/i);
});

test("TEMPLATE_NOTE: profile names in a template are placeholders to replace before saving", () => {
  assert.match(v.TEMPLATE_NOTE, /placeholders/);
  assert.match(v.TEMPLATE_NOTE, /installed on the CAO machine/);
  assert.match(v.TEMPLATE_NOTE, /before you save/);
});

test("workflowDialogSpec: Reload and Replace start on the safe button, warn that typed text is lost, and have a Confirm that works", () => {
  for (const k of ["reload", "template"]) {
    const s = v.workflowDialogSpec(k);
    assert.equal(s.defaultFocus, "cancel", k);
    assert.equal(s.confirmEnabled, true, k);
    assert.equal(s.cancelLabel, "Keep my text", k);
    assert.equal(s.danger, true, k);
    assert.match(s.body, /lost/, k);
  }
  assert.match(v.workflowDialogSpec("reload").body, /changed elsewhere/);
});

test("workflowDialogSpec: no other dialog exists, and each call returns its own object", () => {
  for (const k of ["delete", "approve", "", "__proto__", "constructor", "toString", null, undefined, 5, "RELOAD"]) assert.throws(() => v.workflowDialogSpec(k), /no dialog/, String(k));
  const a = v.workflowDialogSpec("reload");
  a.confirmEnabled = false;
  assert.equal(v.workflowDialogSpec("reload").confirmEnabled, true);
});

// ---- the size line of the editor ------------------------------------------------------------------------------------

const KIB = 1024;
const bytesOf = (body) => Buffer.byteLength(JSON.stringify(body));
// An ASCII source whose POST body (new) or PUT body (edit) is exactly `n` bytes.
const sourceForBody = (n, { name = "wf", tier = "yaml", hash } = {}) => {
  const base = bytesOf(hash ? { source: "", expected_hash: hash, tier } : { name, source: "", tier });
  return "a".repeat(n - base);
};

test("Source too large in argument mode: 125 KiB of encoded YAML is refused, naming the 120 KiB limit and the CLI", () => {
  const source = sourceForBody(125 * KIB);
  const r = v.editorSizeCheck({ name: "wf", tier: "yaml", source }, { stdin: false });
  assert.equal(r.ok, false);
  assert.equal(r.limit, 122_880);
  assert.match(r.message, /122880 bytes \(120 KiB\)/);
  assert.match(r.message, /Paste a smaller spec or use the CLI/);
  assert.equal(r.sizeText, `${125 * KIB} of 122880 bytes`);
});

test("Body at the limit: exactly 122880 encoded bytes is accepted and 122881 is not", () => {
  const edge = sourceForBody(122_880);
  assert.equal(bytesOf({ name: "wf", source: edge, tier: "yaml" }), 122_880);
  assert.equal(v.editorSizeCheck({ name: "wf", tier: "yaml", source: edge }, {}).ok, true);
  assert.equal(v.editorSizeCheck({ name: "wf", tier: "yaml", source: `${edge}a` }, {}).ok, false);
  const pyEdge = sourceForBody(122_880, { tier: "python" });
  assert.equal(bytesOf({ name: "wf", source: pyEdge, tier: "python" }), 122_880);
  assert.equal(v.editorSizeCheck({ name: "wf", tier: "python", source: pyEdge }, {}).ok, true);
  assert.equal(v.editorSizeCheck({ name: "wf", tier: "python", source: `${pyEdge}a` }, {}).ok, false);
});

test("editorSizeCheck: the size line of a source that fits shows the encoded size against the 120 KiB limit", () => {
  const r = v.editorSizeCheck({ name: "wf", tier: "yaml", source: "a".repeat(1000) }, {});
  assert.equal(r.ok, true);
  assert.equal(r.message, null);
  assert.equal(r.sizeText, `${(bytesOf({ name: "wf", source: "a".repeat(1000), tier: "yaml" }) / KIB).toFixed(1)} KiB of 120 KiB`);
});

test("editorSizeCheck: it is the encoded body that counts, so escaping and multi-byte text push a source over", () => {
  const over = (source) => v.editorSizeCheck({ name: "wf", tier: "yaml", source }, {}).ok === false;
  assert.equal(over("\n".repeat(61_440)), true, "each newline is 2 bytes");
  assert.equal(over('"'.repeat(61_440)), true, "each quote is 2 bytes");
  assert.equal(over("\u0001".repeat(20_481)), true, "each control character is 6 bytes");
  assert.equal(over("é".repeat(61_440)), true, "each é is 2 bytes in UTF-8");
  assert.equal(over("\u{1F600}".repeat(30_000)), false, "120000 bytes plus the frame fits");
  assert.equal(over("é".repeat(61_000)), false);
});

test("Concurrent change: an edit's size is that of the PUT body, so the name does not count and the hash does", () => {
  const hash = "h".repeat(64);
  const source = sourceForBody(122_880, { hash });
  assert.equal(bytesOf({ source, expected_hash: hash, tier: "yaml" }), 122_880);
  const longName = "n".repeat(100);
  assert.equal(v.editorSizeCheck({ name: longName, tier: "yaml", source }, { expectedHash: hash }).ok, true);
  assert.equal(v.editorSizeCheck({ name: longName, tier: "yaml", source: `${source}a` }, { expectedHash: hash }).ok, false);
  assert.equal(v.editorSizeCheck({ name: longName, tier: "yaml", source }, {}).ok, false, "the same text as a new workflow is bigger");
  assert.equal(v.editorSizeCheck({ name: longName, tier: "yaml", source }, { expectedHash: "" }).ok, false, "no hash yet: a POST body");
});

test("Source too large in stdin mode: 133120 characters of Python names the 131072-character (128 Ki) cap", () => {
  const r = v.editorSizeCheck({ name: "wf", tier: "python", source: "x".repeat(133_120) }, { stdin: true });
  assert.equal(r.ok, false);
  assert.equal(r.unit, "characters");
  assert.equal(r.limit, 131_072);
  assert.match(r.message, /131072 characters \(128 Ki\)/);
  assert.match(r.message, /133120 characters/);
  assert.equal(r.sizeText, "133120 of 131072 characters");
});

test("editorSizeCheck: in stdin mode the Python character cap is 131072 exactly, and astral characters count as one", () => {
  const py = (source) => v.editorSizeCheck({ name: "wf", tier: "python", source }, { stdin: true });
  assert.equal(py("x".repeat(131_072)).ok, true);
  assert.equal(py("x".repeat(131_073)).ok, false);
  assert.equal(py("é".repeat(131_072)).ok, true, "262144 bytes is the byte cap exactly");
  assert.equal(py("\u{1F600}".repeat(65_536)).ok, true, "65536 characters, 262144 bytes");
  const astral = py("\u{1F600}".repeat(100_000));
  assert.equal(astral.ok, false, "100000 characters is under the character cap, 400000 bytes is over the byte cap");
  assert.equal(astral.unit, "bytes");
  assert.match(astral.message, /262144 bytes \(256 KiB\)/);
  assert.doesNotMatch(astral.message, /characters/);
});

test("editorSizeCheck: in stdin mode the byte cap is 262144 for a spec and for Python, and the character cap is Python's only", () => {
  const yaml = (source) => v.editorSizeCheck({ name: "wf", tier: "yaml", source }, { stdin: true });
  assert.equal(yaml("a".repeat(262_144)).ok, true);
  const refused = yaml("a".repeat(262_145));
  assert.equal(refused.ok, false);
  assert.match(refused.message, /262144 bytes \(256 KiB\)/);
  assert.match(refused.message, /262145 bytes/);
  assert.equal(refused.sizeText, "262145 of 262144 bytes");
  assert.equal(yaml("a".repeat(200_000)).ok, true, "200000 characters: no character cap on a spec");
  assert.equal(v.editorSizeCheck({ name: "wf", tier: "python", source: "a".repeat(200_000) }, { stdin: true }).ok, false);
  const euro = v.editorSizeCheck({ name: "wf", tier: "python", source: "€".repeat(87_382) }, { stdin: true });
  assert.equal(euro.ok, false, "87382 characters of 3 bytes is 262146 bytes");
  assert.equal(euro.unit, "bytes");
  assert.equal(euro.sizeText, "262146 of 262144 bytes");
});

test("editorSizeCheck: a Python source over both caps names the character cap first", () => {
  const r = v.editorSizeCheck({ name: "wf", tier: "python", source: "é".repeat(200_000) }, { stdin: true });
  assert.equal(r.ok, false);
  assert.equal(r.unit, "characters");
  assert.match(r.message, /131072 characters/);
});

test("editorSizeCheck: an accepted Python source shows the cap it is closer to by share, the character cap winning a tie", () => {
  const py = (source) => v.editorSizeCheck({ name: "wf", tier: "python", source }, { stdin: true });
  assert.equal(py("a".repeat(100)).sizeText, "100 of 131072 characters (128 Ki)");
  assert.equal(py("a".repeat(100)).unit, "characters");
  const tie = py("é".repeat(40_000));
  assert.equal(tie.unit, "characters", "40000 of 131072 and 80000 of 262144 are the same share");
  assert.equal(tie.sizeText, "40000 of 131072 characters (128 Ki)");
  const bytes = py("€".repeat(40_000));
  assert.equal(bytes.unit, "bytes");
  assert.equal(bytes.limit, 262_144);
  assert.equal(bytes.sizeText, `${(120_000 / KIB).toFixed(1)} KiB of 256 KiB`);
  assert.equal(py("é".repeat(131_072)).unit, "characters", "both caps full: a tie");
});

test("editorSizeCheck: a YAML spec in stdin mode shows its size against 256 KiB", () => {
  assert.equal(v.editorSizeCheck({ name: "wf", tier: "yaml", source: "a".repeat(2048) }, { stdin: true }).sizeText, "2.0 KiB of 256 KiB");
});

test("editorSizeCheck: stdin mode is on only when it is exactly true", () => {
  const source = "a".repeat(130_000);
  for (const stdin of [false, undefined, null, "true", 1, "yes"]) assert.equal(v.editorSizeCheck({ name: "wf", tier: "yaml", source }, { stdin }).ok, false, String(stdin));
  assert.equal(v.editorSizeCheck({ name: "wf", tier: "yaml", source }, { stdin: true }).ok, true);
});

test("editorSizeCheck: a tier that is neither python nor yaml, or no form, is not a size answer and does not throw", () => {
  for (const form of [{ name: "wf", tier: "ruby", source: "x" }, { name: "wf", source: "x" }, null, undefined, "x"]) {
    const r = v.editorSizeCheck(form, {});
    assert.equal(r.ok, false);
    assert.match(r.message, /python or yaml/);
  }
  assert.equal(v.editorSizeCheck({ name: "wf", tier: "yaml" }, {}).ok, true, "no text is small");
  assert.doesNotThrow(() => v.editorSizeCheck({ name: "wf", tier: "yaml", source: "x" }));
});

// ---- the server's name rule: 1 to 64 characters, and not the one name a fixed route shadows -------------------
// The server's WORKFLOW_NAME_RE is ^[A-Za-z0-9_-]{1,64}$, case-sensitive (constants.py), and FastAPI matches a
// path literally, so only the exact word `runs` is read by GET /workflows/runs (the run list, declared before
// GET /workflows/{name}). `Runs` and `RUNS` are other names. Of the other fixed routes under /workflows,
// POST /workflows/validate is POST only, POST /workflows/plans/approve and /workflows/runs/... have two or more
// segments, and `runs:submit` is not a name.

test("Name length: 1 and 64 characters are names, 65 and the empty name are not, with their own words", () => {
  for (const n of ["a", "a".repeat(63), "a".repeat(64), "aB3_-".repeat(12) + "aB3_"]) {
    assert.equal(v.workflowNameProblem(n), null, `${n.length} characters`);
    assert.equal(v.workflowRoute(n), `/workflows/${n}`, `${n.length} characters`);
  }
  for (const n of ["a".repeat(65), "a".repeat(100), "a".repeat(100_000)]) {
    assert.match(v.workflowNameProblem(n), /at most 64 characters/, `${n.length} characters`);
    assert.equal(v.workflowRoute(n), null, `${n.length} characters`);
  }
  assert.match(v.workflowNameProblem(""), /bare name/);
  assert.equal(v.workflowRoute(""), null);
});

test("Name length: a name's length is in characters of the allowed set, so no multi-byte text slips under 64", () => {
  assert.match(v.workflowNameProblem("é".repeat(10)), /bare name/);
  assert.match(v.workflowNameProblem(`${"a".repeat(64)}\n`), /bare name/);
});

test("runs: the name the run list shadows is refused with words that say why, and has no route", () => {
  const p = v.workflowNameProblem("runs");
  assert.match(p, /"runs"/);
  assert.match(p, /server uses for its own routes/);
  assert.match(p, /Pick another/);
  assert.equal(v.workflowRoute("runs"), null);
});

test("runs: only the exact word is refused; other cases and longer or shorter names are free (the server's rule is case-sensitive)", () => {
  for (const n of ["Runs", "RUNS", "rUnS", "runs-2", "runs_", "_runs", "-runs", "myruns", "run", "rune", "runs1", "r"]) {
    assert.equal(v.workflowNameProblem(n), null, n);
    assert.equal(v.workflowRoute(n), `/workflows/${n}`, n);
  }
});

test("runs: a name that only looks like a route is a name (validate, plans, approve and the rest)", () => {
  for (const n of ["validate", "plans", "approve", "submit", "cancel", "resume", "events", "result", "status", "new", "edit", "workflows", "steps", "compare", "diagnostics", "artifacts", "plan"]) {
    assert.equal(v.workflowNameProblem(n), null, n);
    assert.equal(v.workflowRoute(n), `/workflows/${n}`, n);
  }
  assert.equal(v.workflowRoute("plans/approve"), null);
  assert.equal(v.workflowRoute("runs:submit"), null);
});

test("workflowNameProblem and workflowRoute agree on every name of a grid, so a name the editor takes always has a route", () => {
  const grid = [
    ...HOSTILE_NAMES, "runs", "Runs", "RUNS", "ok", "validate", "plans", "__proto__", "constructor", "a-b_c", "x\n", ...NOT_STRINGS,
    ...Array.from({ length: 72 }, (_, i) => "a".repeat(i)),
    ...["-", "_", "0", "9".repeat(64), "-".repeat(64), "_".repeat(65)],
  ];
  for (const n of grid) {
    assert.equal(v.workflowNameProblem(n) === null, v.workflowRoute(n) !== null, typeof n === "string" ? `${JSON.stringify(n.slice(0, 20))} (${n.length})` : typeof n);
  }
  assert.equal(grid.filter((n) => v.workflowNameProblem(n) === null).length > 40, true, "the grid holds plenty of accepted names");
});

test("the name rule is for workflow names only: a run id of any length that is one segment keeps its route", () => {
  assert.equal(v.runRoute("cancel", "a".repeat(100)), `/workflows/runs/${"a".repeat(100)}/cancel`);
  assert.equal(v.runRoute("resume", "runs"), "/workflows/runs/runs/resume");
});

test("workflowRows: a server name that fails the rule (65 characters, runs) is left out; 64 characters and Runs stay", () => {
  const list = [row("a".repeat(65)), row("runs"), row("a".repeat(64)), row("Runs"), row("validate")];
  assert.deepEqual(v.workflowRows(list, []).map((r) => r.name), ["a".repeat(64), "Runs", "validate"]);
});

test("workflowEditorGate: a new workflow named runs, or 65 characters long, cannot be saved but can be validated; an edit does not look at the name", () => {
  for (const name of ["runs", "a".repeat(65)]) {
    const g = gate({ name });
    assert.equal(g.save, false, name.slice(0, 10));
    assert.equal(g.validate, true);
    assert.equal(typeof g.nameProblem, "string");
  }
  assert.equal(gate({ name: "a".repeat(64) }).save, true);
  assert.equal(gate({ name: "Runs" }).save, true);
  assert.equal(gate({ mode: "edit", name: "runs", expectedHash: "h" }).save, true);
});

// ---- a failure with a blank detail still says why ---------------------------------------------------------------

const BLANKS = ["", " ", "   ", "\n", "\t \n"];
const errorOf = (detail, status = 502) => ({ kind: "error", status, ...(detail === undefined ? {} : { detail }) });
const WHAT = "the workflow list";
// Each function that turns an outcome without words into a line: [name, call(outcome) -> text, named text for status 502, bare text].
const WORDERS = [
  ["runWriteOutcome cancel", (o) => v.runWriteOutcome("cancel", o).text, "Cancel failed", "failed"],
  ["runWriteOutcome resume", (o) => v.runWriteOutcome("resume", o).text, "Resume failed", "failed"],
  ["readFailureText", (o) => v.readFailureText(o, WHAT), `${WHAT} could not be read`, `${WHAT} could not be read`],
  ["submitOutcomeView", (o) => v.submitOutcomeView(o).text, "The run was not submitted", "failed"],
  ["workflowSaveResult POST", (o) => v.workflowSaveResult(o, "POST").text, "The save failed", "failed"],
  ["workflowSaveResult PUT", (o) => v.workflowSaveResult(o, "PUT").text, "The save failed", "failed"],
  ["validateOutcomeNote", (o) => v.validateOutcomeNote(o), "CAO could not check the workflow: error", "CAO could not check the workflow: error"],
];

test("A blank detail (empty, spaces, a newline) names what failed with the exact HTTP status, in every function that shows a failure", () => {
  for (const [name, call, named] of WORDERS) {
    for (const blank of BLANKS) {
      for (const status of [400, 404, 500, 502, 503]) {
        const text = call(errorOf(blank, status));
        const expected = name === "validateOutcomeNote" ? `${named} (HTTP ${status})` : `${named} (HTTP ${status})`;
        assert.equal(text, expected, `${name}, ${JSON.stringify(blank)}, ${status}`);
      }
    }
  }
});

test("A blank detail with no status says what failed without a status", () => {
  for (const [name, call, named] of WORDERS) {
    for (const blank of BLANKS) {
      assert.equal(call({ kind: "error", detail: blank }), named, `${name}, ${JSON.stringify(blank)}`);
      assert.equal(call({ kind: "error", status: "502", detail: blank }), named, `${name}, a status that is not a number`);
    }
  }
});

test("A blank detail from a real answer: an empty body and a blank 'detail' both name the action and the status", () => {
  for (const o of [reply(502, ""), reply(502, { detail: "" }), reply(502, { detail: "   " }), reply(502, "  \n ")]) {
    assert.equal(o.kind, "error");
    assert.equal(v.runWriteOutcome("cancel", o).text, "Cancel failed (HTTP 502)");
    assert.equal(v.readFailureText(o, "the workflow"), "the workflow could not be read (HTTP 502)");
    assert.equal(v.submitOutcomeView(o).text, "The run was not submitted (HTTP 502)");
    assert.equal(v.workflowSaveResult(o, "POST").text, "The save failed (HTTP 502)");
    assert.match(v.validateOutcomeNote(o), /HTTP 502/);
  }
});

test("A failure whose answer carried no text at all keeps the plain fallback, with no status", () => {
  for (const [name, call, , bare] of WORDERS) {
    for (const o of [errorOf(undefined), { kind: "error", status: 502, detail: null }, { kind: "error", status: 502, detail: null, message: null }, { kind: "weird" }, {}, null, undefined]) {
      assert.equal(call(o), bare, `${name}, ${JSON.stringify(o)}`);
    }
  }
});

test("Words win over the fallback: a detail with text is shown as it came, a blank detail yields to the message", () => {
  for (const [name, call] of WORDERS) {
    const out = (o) => call(o).replace("CAO could not check the workflow: ", "");
    assert.equal(out(errorOf("  boom ")), "  boom ", `${name}: verbatim`);
    assert.equal(out({ kind: "error", status: 502, detail: "d", message: "m" }), "d", `${name}: detail first`);
    assert.equal(out({ kind: "error", status: 502, detail: "  ", message: "m" }), "m", `${name}: blank detail`);
    assert.equal(out({ kind: "error", status: 502, detail: "", message: "  " }), name === "validateOutcomeNote" ? "error (HTTP 502)" : call({ kind: "error", status: 502, detail: "" }), `${name}: both blank`);
    assert.equal(out({ kind: "error", status: 502, message: "m" }), "m", `${name}: message alone`);
  }
});

test("Never a blank line: for every blank form of detail and message, at every status, the text has words and no placeholder", () => {
  const forms = [undefined, null, ...BLANKS];
  for (const [name, call] of WORDERS) {
    for (const detail of forms) for (const message of forms) for (const status of [undefined, 0, 404, 502]) {
      const o = { kind: "error", ...(status === undefined ? {} : { status }), ...(detail === undefined ? {} : { detail }), ...(message === undefined ? {} : { message }) };
      const text = call(o);
      assert.equal(typeof text, "string");
      assert.notEqual(text.trim(), "", `${name} ${JSON.stringify(o)}`);
      assert.doesNotMatch(text, /undefined|null|\[object|:\s*$/, `${name} ${JSON.stringify(o)}: ${text}`);
    }
  }
});

test("A blank detail reaches the banners and row notes with the same words", () => {
  const o = reply(502, "");
  assert.equal(v.workflowSaveEffects(v.workflowSaveResult(o, "PUT"), { name: "wf", authText: AUTH }).banner, "The save failed (HTTP 502)");
  const conflict = v.workflowSaveResult({ kind: "error", status: 409, detail: "" }, "PUT");
  assert.equal(conflict.type, "conflict");
  assert.notEqual(conflict.message.trim(), "");
  assert.equal(v.workflowSaveResult({ kind: "error", status: 409, detail: "" }, "POST").type, "exists");
});

test("A failure line does not touch the other outcome kinds: auth, no answer and approval keep their own words", () => {
  assert.equal(v.runWriteOutcome("cancel", reply(401, {})).type, "auth");
  assert.equal(v.runWriteOutcome("cancel", UNREACHABLE).type, "failed");
  assert.equal(v.submitOutcomeView(approvalOutcome()).type, "approval");
});

// ---- Edit: which tier a document is --------------------------------------------------------------------------

const DOC = (over = {}) => ({ name: "wf", source: "text\n", content_hash: "abc123", ...over });

test("Edit is for every workflow: the document's path extension says the tier, then steps or findings when there is no path", () => {
  const cases = [
    // [document, tier or null]
    [DOC({ path: "/srv/specs/wf.py" }), "python"],
    [DOC({ path: "/srv/specs/wf.py", steps: [{ id: "a" }] }), "python"],
    [DOC({ path: "/srv/specs/wf.yaml.py" }), "python"],
    [DOC({ path: "/srv/specs/wf.PY" }), "python"],
    [DOC({ path: "/srv/specs/a.yaml/wf.py" }), "python"],
    [DOC({ path: "/srv/specs/wf.yaml" }), "yaml"],
    [DOC({ path: "/srv/specs/wf.yml" }), "yaml"],
    [DOC({ path: "/srv/specs/wf.YAML" }), "yaml"],
    [DOC({ path: "/srv/specs/wf.Yml" }), "yaml"],
    [DOC({ path: "/srv/specs/wf.yaml", findings: [] }), "yaml"],
    [DOC({ path: "/srv/specs/wf.yaml", steps: "x" }), "yaml"],
    [DOC({ path: "/srv/specs/wf.yaml.bak" }), null],
    [DOC({ path: "/srv/specs/wf.pyc" }), null],
    [DOC({ path: "/srv/specs/wf" }), null],
    [DOC({ path: "" }), null],
    [DOC({ path: "", steps: [{ id: "a" }], findings: [] }), null],
    [DOC({ steps: [{ id: "a" }] }), "yaml"],
    [DOC({ steps: [] }), "yaml"],
    [DOC({ path: null, steps: [{ id: "a" }] }), "yaml"],
    [DOC({ path: 5, steps: [{ id: "a" }] }), "yaml"],
    [DOC({ findings: [] }), "python"],
    [DOC({ path: undefined, findings: [{ line: 1 }] }), "python"],
    [DOC({ path: 5, findings: [] }), "python"],
    [DOC({ steps: [], findings: [] }), "yaml"],
    [DOC({ path: 5 }), null],
    [DOC(), null],
    [DOC({ steps: "x" }), null],
    [DOC({ steps: {} }), null],
    [DOC({ findings: "x" }), null],
    [DOC({ findings: {} }), null],
    [DOC({ path: undefined, steps: null, findings: null }), null],
  ];
  for (const [doc, tier] of cases) {
    const r = v.editFromDocument(frozen(doc), "wf");
    assert.equal(r.ok, tier !== null, JSON.stringify(doc));
    if (tier !== null) assert.deepEqual(r, { ok: true, tier, source: "text\n", hash: "abc123" }, JSON.stringify(doc));
    else assert.match(r.reason, /cannot be edited here/);
  }
});

test("Edit is for every workflow: the documents as the server sends them, Python (ScriptSpec) and YAML, are both taken", () => {
  assert.equal(v.editFromDocument(frozen(PY_DOC), "wf").tier, "python");
  assert.equal(v.editFromDocument(frozen(YAML_DOC), "wf").tier, "yaml");
  assert.equal(v.editFromDocument({ ...PY_DOC, path: "/srv/specs/wf.yml", steps: [] }, "wf").tier, "yaml", "the path decides");
});

test("Edit is for every workflow: an edited Python workflow follows the YAML rules, with its own tier in every key and size", () => {
  const doc = v.editFromDocument(frozen(PY_DOC), "wf");
  const form = { name: "wf", tier: doc.tier, source: doc.source };
  const ok = { status: "pass", errors: [], findings: [] };
  const check = v.editorSizeCheck(form, { stdin: false, expectedHash: doc.hash });
  assert.equal(check.ok, true);
  const g = v.workflowEditorGate({
    mode: "edit", gate: { disabled: false }, check, validating: false, sending: false, loading: false, name: "wf", tier: "python", source: doc.source,
    validatedKey: v.workflowValidationKey("python", doc.source), result: ok, expectedHash: doc.hash,
  });
  assert.deepEqual(g, { nameProblem: null, validate: true, save: true });
  assert.equal(v.workflowEditorGate({ mode: "edit", gate: {}, check, name: "wf", tier: "python", source: doc.source, validatedKey: v.workflowValidationKey("yaml", doc.source), result: ok, expectedHash: doc.hash }).save, false, "a validate of the text as YAML is not one as Python");
  assert.equal(v.workflowEditorGate({ mode: "edit", gate: {}, check, name: "wf", tier: "python", source: doc.source, validatedKey: v.workflowValidationKey("python", doc.source), result: ok, expectedHash: "" }).save, false, "an edit needs the hash");
  // the size of the PUT body for a Python edit: exactly 122880 bytes passes, one more does not
  const base = Buffer.byteLength(JSON.stringify({ source: "", expected_hash: doc.hash, tier: "python" }));
  const edge = "a".repeat(122_880 - base);
  assert.equal(v.editorSizeCheck({ name: "wf", tier: "python", source: edge }, { expectedHash: doc.hash }).ok, true);
  assert.equal(v.editorSizeCheck({ name: "wf", tier: "python", source: `${edge}a` }, { expectedHash: doc.hash }).ok, false);
  // stdin mode: the same Python caps as a new Python workflow
  assert.equal(v.editorSizeCheck({ name: "wf", tier: "python", source: "x".repeat(131_073) }, { stdin: true, expectedHash: doc.hash }).ok, false);
  assert.equal(v.editorSizeCheck({ name: "wf", tier: "python", source: "x".repeat(131_072) }, { stdin: true, expectedHash: doc.hash }).ok, true);
});

test("editFromDocument: an editable path still needs the text and a hash", () => {
  for (const bad of [{ source: undefined }, { source: 5 }, { content_hash: "" }, { content_hash: undefined }, { content_hash: 7 }]) {
    assert.equal(v.editFromDocument(DOC({ path: "/srv/specs/wf.yaml", ...bad }), "wf").ok, false, JSON.stringify(bad));
  }
});

test("A blank detail on another 409 of a PUT still says why: the banner is never blank", () => {
  for (const detail of ["", " ", "   ", "\n"]) {
    const r = v.workflowSaveResult({ kind: "error", status: 409, detail }, "PUT");
    assert.equal(r.type, "conflict", JSON.stringify(detail));
    assert.notEqual(r.message.trim(), "", `message for ${JSON.stringify(detail)}`);
    assert.notEqual(v.workflowSaveEffects(r, { name: "wf", authText: AUTH }).banner.trim(), "", `banner for ${JSON.stringify(detail)}`);
  }
});

// ======== the runs read: what a row says, and the details' Runs line ===========================================
// Spec cao-window-workflows "Workflows section": the state is that of the newest run among the latest 20 runs
// (GET /workflows/runs?limit=20): "never run" when the list holds fewer than 20 runs and none is its own, "no recent
// run" when it holds 20 and none is its own, "unknown" while the runs read has not answered or failed.

// n runs of other workflows, newest first.
const others = (n, over = {}) => Array.from({ length: n }, (_, i) => run("completed", { run_id: `o${i}`, workflow_name: `other-${i}`, ...over }));

test("WORKFLOW_RUNS_LIMIT is 20, and the runs read asks for exactly that many", () => {
  assert.equal(v.WORKFLOW_RUNS_LIMIT, 20);
  const call = v.detailCall("runs", "wf");
  assert.deepEqual(call.query, { limit: 20 });
  const built = curlRequest({ machine: "cao", port: 9889, stdin: false }, call);
  assert.equal(built.exec.args.at(-1), "http://127.0.0.1:9889/workflows/runs?limit=20");
});

test("workflowStateFor: while the runs read has not answered (or failed) the state is 'unknown', never 'never run'", () => {
  for (const bad of [null, undefined, "x", 5, {}, { length: 0 }]) {
    assert.equal(v.workflowStateFor(bad, "wf"), "unknown", String(bad));
    assert.notEqual(v.workflowStateFor(bad, "wf"), "never run");
  }
});

test("Never run: with fewer than 20 runs and none its own, the state is 'never run' (0, 1, 19 runs)", () => {
  for (const n of [0, 1, 10, 19]) assert.equal(v.workflowStateFor(frozen(others(n)), "wf"), "never run", `${n} runs`);
});

test("No recent run: with 20 runs (or more) and none its own, the state is 'no recent run'", () => {
  for (const n of [20, 21, 50]) assert.equal(v.workflowStateFor(frozen(others(n)), "wf"), "no recent run", `${n} runs`);
});

test("workflowStateFor: a run of its own gives its state in the spec's words, whatever the count", () => {
  for (const [state, words] of [["running", "running"], ["completed", "succeeded"], ["failed", "failed"], ["cancelled", "cancelled"]]) {
    for (const n of [0, 19, 20]) assert.equal(v.workflowStateFor([...others(n), run(state)], "wf"), words, `${state} among ${n + 1}`);
  }
});

test("workflowStateFor: the newest of its own runs decides, others' runs never count", () => {
  const runs = [run("failed", { run_id: "n", started_at: "2026-11-19T10:05:00Z" }), run("running", { run_id: "o", started_at: "2026-11-19T10:00:00Z" }), ...others(5, { state: "running" })];
  assert.equal(v.workflowStateFor(runs, "wf"), "failed");
  assert.equal(v.workflowStateFor(others(3, { state: "running" }), "wf"), "never run");
});

test("workflowRows: the same rule for each row: a full 20 runs makes 'no recent run' for a workflow without one, and a row with its own keeps its state", () => {
  const runs = [run("running", { workflow_name: "busy", run_id: "b" }), ...others(19)];
  assert.equal(runs.length, 20);
  assert.deepEqual(v.workflowRows([row("busy"), row("idle")], runs), [{ name: "busy", state: "running" }, { name: "idle", state: "no recent run" }]);
  assert.deepEqual(v.workflowRows([row("busy"), row("idle")], runs.slice(0, 19)), [{ name: "busy", state: "running" }, { name: "idle", state: "never run" }]);
});

test("Hostile names: constructor, __proto__ and toString get their own state and never an object property", () => {
  for (const name of ["constructor", "__proto__", "toString", "hasOwnProperty", "valueOf"]) {
    const own = run("failed", { workflow_name: name, run_id: "x" });
    assert.equal(v.workflowStateFor([own], name), "failed", name);
    assert.equal(v.workflowStateFor(others(3), name), "never run", name);
    assert.equal(v.workflowStateFor(others(20), name), "no recent run", name);
    assert.deepEqual(v.workflowRows([row(name)], [own]), [{ name, state: "failed" }], name);
    assert.deepEqual(v.workflowRows([row(name)], others(3)), [{ name, state: "never run" }], name);
    assert.equal(v.workflowStateFor([run("running", { workflow_name: "other" })], name), "never run");
  }
});

test("runsLineFor: the Runs line of a workflow with no run follows the rule of its row", () => {
  for (const n of [0, 1, 19]) assert.equal(v.runsLineFor(frozen(others(n)), "wf"), "No runs yet", `${n} runs`);
  for (const n of [20, 21]) assert.equal(v.runsLineFor(frozen(others(n)), "wf"), "No run among the latest 20", `${n} runs`);
});

test("runsLineFor: a workflow with a run of its own has no such line; runs not read yet say so, in their own words", () => {
  for (const n of [0, 19, 20]) assert.equal(v.runsLineFor([...others(n), run("running")], "wf"), null, `${n + 1} runs`);
  for (const bad of [null, undefined, "x", {}]) {
    const line = v.runsLineFor(bad, "wf");
    assert.equal(line, "The runs have not been read yet.");
    assert.notEqual(line, "No runs yet");
  }
});

test("The row and the Runs line agree on every size of list, with and without a run of its own", () => {
  const WORD = { "never run": "No runs yet", "no recent run": "No run among the latest 20" };
  for (const n of [0, 5, 19, 20, 21]) for (const own of [false, true]) {
    const runs = own ? [...others(n), run("completed")] : others(n);
    const state = v.workflowStateFor(runs, "wf");
    const line = v.runsLineFor(runs, "wf");
    if (own) { assert.equal(line, null); assert.equal(state, "succeeded"); } else assert.equal(line, WORD[state], `${n} runs, ${state}`);
  }
});

test("workflowDetailsView: the Runs line is part of the details, with the runs listed when there are any", () => {
  const d = (runs) => v.workflowDetailsView(frozen({ name: "wf", row: row("wf"), runs, flows: [], now: NOW }));
  assert.equal(d(others(19)).runsLine, "No runs yet");
  assert.equal(d(others(20)).runsLine, "No run among the latest 20");
  assert.equal(d(null).runsLine, "The runs have not been read yet.");
  assert.equal(d([run("running")]).runsLine, null);
  assert.equal(d([run("running")]).runs.length, 1);
});

// ---- notes on run rows ----------------------------------------------------------------------------------------

const note = (kind, state, text = "t") => ({ kind, text, state });

test("pruneRunNotes: a failed note stays while its run is in the state it was made for, and goes when the state differs", () => {
  const notes = new Map([["a", note("failed", "running")], ["b", note("failed", "cancelled")], ["c", note("failed", "running")]]);
  const runs = [run("running", { run_id: "a" }), run("running", { run_id: "b" }), run("cancelled", { run_id: "c" })];
  const kept = v.pruneRunNotes(notes, frozen(runs));
  assert.deepEqual([...kept.keys()], ["a"]);
  assert.equal(kept.get("a"), notes.get("a"));
});

test("pruneRunNotes: a note that says a write was requested is answered by the refresh and goes, whatever the run's state", () => {
  const notes = new Map([["a", note("done", "running")]]);
  assert.equal(v.pruneRunNotes(notes, [run("running", { run_id: "a" })]).size, 0);
  assert.equal(v.pruneRunNotes(notes, [run("cancelled", { run_id: "a" })]).size, 0);
});

test("pruneRunNotes: a note for a run that is not in the list any more goes", () => {
  const notes = new Map([["gone", note("failed", "running")], ["a", note("failed", "running")]]);
  assert.deepEqual([...v.pruneRunNotes(notes, [run("running", { run_id: "a" }), null, 5]).keys()], ["a"]);
  assert.equal(v.pruneRunNotes(notes, []).size, 0);
});

test("pruneRunNotes: it returns a new Map in the same order and the Map it was given is not changed", () => {
  const notes = new Map([["z", note("failed", "running")], ["a", note("failed", "running")], ["m", note("failed", "failed")]]);
  const before = [...notes];
  const out = v.pruneRunNotes(notes, [run("running", { run_id: "a" }), run("running", { run_id: "z" }), run("running", { run_id: "m" })]);
  assert.notEqual(out, notes);
  assert.deepEqual([...out.keys()], ["z", "a"]);
  assert.deepEqual([...notes], before, "the input Map is as it was");
  assert.equal(notes.size, 3);
  out.set("new", note("failed", "x"));
  assert.equal(notes.has("new"), false);
});

test("pruneRunNotes: no Map, no notes; a run write followed by a refresh with unchanged state keeps the failure visible", () => {
  for (const bad of [null, undefined, {}, [["a", note("failed", "running")]], "a", 5]) {
    const out = v.pruneRunNotes(bad, [run("running", { run_id: "a" })]);
    assert.ok(out instanceof Map);
    assert.equal(out.size, 0);
  }
  for (const bad of [null, undefined, "x", {}]) assert.ok(v.pruneRunNotes(new Map([["a", note("failed", "running")]]), bad) instanceof Map, "runs that are not a list do not throw");
  const resume = new Map([["r1", note("failed", "cancelled", "Resume failed (HTTP 502)")]]);
  assert.equal(v.pruneRunNotes(resume, [run("cancelled", { run_id: "r1" })]).get("r1").text, "Resume failed (HTTP 502)");
});

test("Hostile names: run ids like __proto__ and constructor are Map keys like any other, and no prototype changes", () => {
  const ids = ["__proto__", "constructor", "toString", "hasOwnProperty"];
  const notes = new Map(ids.map((id) => [id, note("failed", "running")]));
  const runs = ids.map((id) => run("running", { run_id: id }));
  const out = v.pruneRunNotes(notes, runs);
  assert.deepEqual([...out.keys()], ids);
  assert.equal(out.get("__proto__").kind, "failed");
  assert.equal(Object.getPrototypeOf(out), Map.prototype);
  assert.equal(({}).kind, undefined);
  const moved = v.pruneRunNotes(notes, ids.map((id) => run("cancelled", { run_id: id })));
  assert.equal(moved.size, 0);
});

test("Hostile names: runWriteCheck finds a run whose id is __proto__, constructor or toString, and only that run", () => {
  for (const id of ["__proto__", "constructor", "toString"]) {
    const snap = { run_id: id, state: "running" };
    assert.deepEqual(v.runWriteCheck("cancel", snap, [run("running", { run_id: id })]), { send: true, reason: null }, id);
    assert.equal(v.runWriteCheck("cancel", snap, [run("running", { run_id: "other" })]).send, false, id);
    assert.equal(v.runWriteCheck("cancel", snap, [run("completed", { run_id: id })]).send, false, id);
    assert.equal(v.runWriteCheck("resume", { run_id: id, state: "cancelled" }, [run("cancelled", { run_id: id })]).send, true, id);
  }
  for (const action of ["__proto__", "constructor", "toString"]) assert.equal(v.runWriteCheck(action, { run_id: "r1" }, [run("running")]).send, false, action);
});

// ---- combinedStamp ---------------------------------------------------------------------------------------------

test("combinedStamp: the same stamps in the same places give the same text; a change in any one, or their order, gives another", () => {
  const c = v.combinedStamp;
  assert.equal(typeof c(1, 2), "string");
  assert.equal(c(1, 2), c(1, 2));
  assert.notEqual(c(1, 2), c(1, 3));
  assert.notEqual(c(1, 2), c(2, 2));
  assert.notEqual(c(1, 2), c(2, 1));
  assert.notEqual(c(1), c("1"));
  assert.notEqual(c(1, 2), c(12));
  assert.notEqual(c(undefined, 5), c(5, undefined));
});

test("combinedStamp: a stamp that was never set is the same as null, so a missing one is not a change", () => {
  assert.equal(v.combinedStamp(undefined, 7), v.combinedStamp(null, 7));
  assert.equal(v.combinedStamp(), v.combinedStamp());
});

test("combinedStamp: a write of either stamp (a workflow write, or a flow Schedule... creates) makes the details poll due", () => {
  const seen = v.combinedStamp(100, 200);
  const dueFor = (stamp) => v.workflowsPollDue({ shown: true, inFlight: false, lastAt: NOW - 1, runs: [], stamp, seenStamp: seen }, NOW);
  assert.equal(dueFor(v.combinedStamp(100, 200)), false);
  assert.equal(dueFor(v.combinedStamp(101, 200)), true);
  assert.equal(dueFor(v.combinedStamp(100, 201)), true);
  assert.equal(dueFor(v.combinedStamp(100, undefined)), true);
});

// ======== the Run form's model ===============================================================================

const UNREAD = "The inputs could not be read from CAO; enter them by hand.";
const UNDESCRIBED_LABEL = "inputs not described by the spec";
const SPEC_TYPED = { name: "wf", inputs: { count: { type: "int", required: true }, verbose: { type: "bool" }, title: { type: "string", default: "x" }, dir: { type: "path" } }, steps: [] };

test("runFormModel: a spec that was read gives its inputs as typed fields, in the spec's order", () => {
  const m = v.runFormModel(frozen(SPEC_TYPED), "ok");
  assert.equal(m.free, false);
  assert.equal(m.label, null);
  assert.deepEqual(m.fields.map((f) => [f.name, f.type, f.required]), [["count", "int", true], ["verbose", "bool", false], ["title", "string", false], ["dir", "path", false]]);
  assert.equal(m.fields[2].default, "x");
});

test("Inputs not readable: a spec that was read but does not describe its inputs gives free rows labelled as not described by the spec", () => {
  for (const spec of [{}, { name: "wf" }, { inputs: null }, { inputs: [] }, { inputs: { a: { type: "float" } } }, { inputs: { a: "int" } }, { inputs: { a: null } }, null, "x", []]) {
    const m = v.runFormModel(spec, "ok");
    assert.deepEqual(m, { fields: [], free: true, label: UNDESCRIBED_LABEL }, JSON.stringify(spec));
  }
});

test("runFormModel: a spec that could not be read gives free rows with their own label, not the 'not described' one", () => {
  for (const spec of [null, undefined, SPEC_TYPED, {}]) {
    const m = v.runFormModel(spec, "failed");
    assert.deepEqual(m, { fields: [], free: true, label: UNREAD }, JSON.stringify(spec));
    assert.notEqual(m.label, UNDESCRIBED_LABEL);
  }
});

test("runFormModel: a read that is still loading is not reported as a failed one", () => {
  for (const spec of [null, undefined]) {
    const m = v.runFormModel(spec, "loading");
    assert.notEqual(m.label, UNREAD);
    assert.equal(m.free, true);
  }
  assert.equal(v.runFormModel(SPEC_TYPED, "loading").free, false);
  assert.equal(v.runFormModel(null, undefined).label, UNDESCRIBED_LABEL);
});

test("runFormModel: its answer feeds runInputs, for typed and for free forms", () => {
  const typed = v.runFormModel(SPEC_TYPED, "ok");
  assert.deepEqual(v.runInputs(typed, { count: "3", verbose: "false" }, []).inputs, { count: 3, verbose: false });
  const free = v.runFormModel(null, "failed");
  assert.deepEqual(v.runInputs(free, {}, [{ key: "a", value: "1" }]).inputs, { a: "1" });
});

// ======== a YAML spec's own name: line =======================================================================

test("yamlSpecName: the value of the first top-level name: line, without quotes or a trailing comment", () => {
  const n = v.yamlSpecName;
  assert.equal(n("name: wf\nsteps: []\n"), "wf");
  assert.equal(n("name:wf\n"), "wf");
  assert.equal(n("name :   wf  \n"), "wf");
  assert.equal(n('name: "wf"\n'), "wf");
  assert.equal(n("name: 'wf'\n"), "wf");
  assert.equal(n("name: wf # the name\n"), "wf");
  assert.equal(n('name: "wf" # the name\n'), "wf");
  assert.equal(n('name: "a#b"\n'), "a#b");
  assert.equal(n("name: a#b\n"), "a#b");
  assert.equal(n("# a spec\n\nname: wf\ndescription: d\n"), "wf");
  assert.equal(n("name: wf\r\nsteps: []\r\n"), "wf");
  assert.equal(n("name:\nsteps: []\n"), "");
});

test("yamlSpecName: the first top-level line wins; an indented, listed, differently spelled or longer key is not the name line", () => {
  const n = v.yamlSpecName;
  assert.equal(n("name: first\nname: second\n"), "first");
  assert.equal(n("steps:\n  - id: a\n    name: step\n"), null);
  assert.equal(n("steps:\n  - name: step\n"), null);
  assert.equal(n("  name: indented\n"), null);
  assert.equal(n("Name: wf\n"), null);
  assert.equal(n("nameless: wf\n"), null);
  assert.equal(n("name_x: wf\n"), null);
  assert.equal(n("description: |\n  name: inside\nname: real\n"), "real");
  assert.equal(n("steps:\n    name: step\nname: wf\n"), "wf");
});

test("yamlSpecName: nothing to read gives null, and a big or odd source is read at once", () => {
  for (const bad of ["", "steps: []\n", null, undefined, 5, {}, []]) assert.equal(v.yamlSpecName(bad), null, String(bad));
  const t0 = Date.now();
  assert.equal(v.yamlSpecName(`name${" ".repeat(200_000)}x\nname: wf\n`), "wf");
  assert.equal(v.yamlSpecName(`${"x: y\n".repeat(100_000)}name: late\n`), "late");
  assert.ok(Date.now() - t0 < 2000, "no catastrophic backtracking");
});

test("specNameMismatch: YAML whose name: line differs from the workflow's name is a mismatch that names both", () => {
  const m = v.specNameMismatch("yaml", "name: other\nsteps: []\n", "wf");
  assert.match(m, /name: line says "other"/);
  assert.match(m, /named "wf"/);
  assert.match(m, /make them the same/);
  assert.notEqual(v.specNameMismatch("yaml", "name: WF\n", "wf"), null, "case counts");
  assert.notEqual(v.specNameMismatch("yaml", "name: wf\n", ""), null);
  assert.notEqual(v.specNameMismatch("yaml", "name: wf \n", "wf2"), null);
});

test("specNameMismatch: the same name, a spec with no name line, or any Python source is no mismatch", () => {
  assert.equal(v.specNameMismatch("yaml", "name: wf\n", "wf"), null);
  assert.equal(v.specNameMismatch("yaml", 'name: "wf" # c\n', "wf"), null);
  assert.equal(v.specNameMismatch("yaml", "steps: []\n", "wf"), null);
  assert.equal(v.specNameMismatch("yaml", "", "wf"), null);
  assert.equal(v.specNameMismatch("python", "name: other\n", "wf"), null);
  assert.equal(v.specNameMismatch("python", 'NAME = "other"\n', "wf"), null);
  for (const tier of ["ruby", "", undefined, null]) assert.equal(v.specNameMismatch(tier, "name: other\n", "wf"), null, String(tier));
});

// ---- Save is blocked by a name that differs; Validate is not ----------------------------------------------------

const GATE_OK = { nameProblem: null, validate: true, save: true };

test("editorGateWithName: a YAML name: line that differs blocks Save and leaves Validate on", () => {
  const g = v.editorGateWithName(frozen(GATE_OK), { tier: "yaml", source: "name: other\n", name: "wf" });
  assert.equal(g.save, false);
  assert.equal(g.validate, true);
  assert.equal(g.nameProblem, null);
  assert.match(g.specNameProblem, /"other"/);
});

test("editorGateWithName: the same name leaves the gate as it was, with no problem to show", () => {
  assert.deepEqual(v.editorGateWithName(frozen(GATE_OK), { tier: "yaml", source: "name: wf\n", name: "wf" }), { ...GATE_OK, specNameProblem: null });
  assert.deepEqual(v.editorGateWithName(frozen(GATE_OK), { tier: "yaml", source: "steps: []\n", name: "wf" }), { ...GATE_OK, specNameProblem: null });
});

test("editorGateWithName: Python is not affected, and a gate that already says no keeps saying it", () => {
  assert.deepEqual(v.editorGateWithName(GATE_OK, { tier: "python", source: "name: other\n", name: "wf" }), { ...GATE_OK, specNameProblem: null });
  const off = { nameProblem: "bad", validate: true, save: false };
  assert.equal(v.editorGateWithName(off, { tier: "yaml", source: "name: wf\n", name: "wf" }).save, false);
  assert.equal(v.editorGateWithName({ nameProblem: null, validate: false, save: false }, { tier: "yaml", source: "name: wf\n", name: "wf" }).validate, false);
  assert.equal(v.editorGateWithName({ ...GATE_OK, save: "yes" }, { tier: "yaml", source: "name: wf\n", name: "wf" }).save, false, "only a Save that is exactly true stays on");
  assert.deepEqual(v.editorGateWithName(GATE_OK).specNameProblem, null);
  assert.equal(v.editorGateWithName(GATE_OK, {}).save, true);
});

test("editorGateWithName: on the real gate, a clean validate of a spec named otherwise still cannot be saved until the names agree", () => {
  const src = "name: other\nsteps: []\n";
  const real = (name) => v.workflowEditorGate({
    mode: "new", gate: { disabled: false }, check: { ok: true }, name, tier: "yaml", source: src, validatedKey: v.workflowValidationKey("yaml", src), result: PASS,
  });
  assert.equal(v.editorGateWithName(real("wf"), { tier: "yaml", source: src, name: "wf" }).save, false);
  assert.equal(v.editorGateWithName(real("wf"), { tier: "yaml", source: src, name: "wf" }).validate, true);
  assert.equal(v.editorGateWithName(real("other"), { tier: "yaml", source: src, name: "other" }).save, true);
});

// ---- renameYamlSpec, templateFor ------------------------------------------------------------------------------------

test("renameYamlSpec: only the first top-level name: line is set to the name; every other line is as it was", () => {
  const src = "# a spec\nname: old # note\ndescription: d\nsteps:\n  - id: a\n    name: step\n";
  assert.equal(v.renameYamlSpec(src, "new-name"), "# a spec\nname: new-name\ndescription: d\nsteps:\n  - id: a\n    name: step\n");
  assert.equal(v.renameYamlSpec("name: a\nname: b\n", "c"), "name: c\nname: b\n");
  assert.equal(v.renameYamlSpec("name: wf\r\nsteps: []\r\n", "x"), "name: x\r\nsteps: []\r\n");
});

test("renameYamlSpec: with no name line, or no text, the source is returned as it came", () => {
  for (const src of ["steps: []\n", "", "  name: indented\n", "Name: x\n"]) assert.equal(v.renameYamlSpec(src, "x"), src);
  assert.equal(v.renameYamlSpec(undefined, "x"), undefined);
});

test("renameYamlSpec: the new name is read back from the text, for any name the editor takes", () => {
  for (const n of ["wf", "a-b_c", "A1", "x".repeat(64), "__proto__", "validate"]) {
    const out = v.renameYamlSpec("name: old\nsteps: []\n", n);
    assert.equal(v.yamlSpecName(out), n);
    assert.equal(v.specNameMismatch("yaml", out, n), null);
  }
});

test("templateFor: a YAML template's name: line follows the form's name, the typed one or the template's", () => {
  const tpl = { tier: "yaml", name: "hello", source: "name: hello\ndescription: d\nsteps: []\n" };
  assert.deepEqual(v.templateFor(frozen({ name: "", tier: "yaml", source: "" }), tpl), { name: "hello", tier: "yaml", source: tpl.source });
  assert.deepEqual(v.templateFor({ name: "mine", tier: "yaml", source: "" }, tpl), { name: "mine", tier: "yaml", source: "name: mine\ndescription: d\nsteps: []\n" });
  assert.equal(v.templateFor({ name: "mine", tier: "python", source: "x" }, tpl).tier, "yaml");
});

test("templateFor: a typed name the editor would refuse leaves the template's text alone", () => {
  const tpl = { tier: "yaml", name: "hello", source: "name: hello\nsteps: []\n" };
  for (const bad of ["../x", "a b", "runs", "a".repeat(65), "x.yaml"]) {
    const out = v.templateFor({ name: bad, tier: "yaml", source: "" }, tpl);
    assert.equal(out.source, tpl.source, bad);
    assert.equal(out.name, bad);
  }
});

test("templateFor: a Python template's text is never rewritten, even if it has a line that looks like a name", () => {
  const tpl = { tier: "python", name: "tp", source: "name: not-a-spec\nprint(1)\n" };
  assert.deepEqual(v.templateFor({ name: "mine", tier: "yaml", source: "" }, tpl), { name: "mine", tier: "python", source: tpl.source });
});

test("templateFor: nothing it is given is changed, and the shipped templates never end in a name mismatch", () => {
  const form = frozen({ name: "mine", tier: "yaml", source: "typed" });
  assert.doesNotThrow(() => v.templateFor(form, frozen(TEMPLATES[0])));
  for (const t of TEMPLATES) for (const name of ["", "mine", "other-name", "a".repeat(64)]) {
    const out = v.templateFor({ name, tier: "yaml", source: "" }, t);
    assert.equal(v.specNameMismatch(out.tier, out.source, out.name), null, `${t.id} as ${name || "(empty)"}`);
    assert.equal(v.workflowNameProblem(out.name), null);
  }
});

// ======== what a details poll reads, and what each answer does ===============================================

test("detailReads: the list and the runs always, then the flows and the spec only when the plan asked, in that order", () => {
  assert.deepEqual(v.detailReads({ poll: true, flows: false, spec: false }), ["list", "runs"]);
  assert.deepEqual(v.detailReads({ poll: true, flows: true, spec: false }), ["list", "runs", "flows"]);
  assert.deepEqual(v.detailReads({ poll: true, flows: false, spec: true }), ["list", "runs", "spec"]);
  assert.deepEqual(v.detailReads({ poll: true, flows: true, spec: true }), ["list", "runs", "flows", "spec"]);
  for (const none of [undefined, null, {}]) assert.deepEqual(v.detailReads(none), ["list", "runs"]);
});

test("detailReads: it follows the plan: a first poll reads all four, one that only moved a stamp reads what the plan says", () => {
  const first = v.workflowDetailPlan({ shown: true, inFlight: false, lastAt: undefined, flowsAt: undefined, specOk: undefined }, NOW);
  assert.deepEqual(v.detailReads(first), ["list", "runs", "flows", "spec"]);
  const slow = v.workflowDetailPlan({ shown: true, inFlight: false, lastAt: NOW - 60_000, runs: [], flowsAt: NOW - 1, specOk: true }, NOW);
  assert.deepEqual(v.detailReads(slow), ["list", "runs"]);
});

test("detailCall: each read is one GET with its path and a time limit", () => {
  assert.deepEqual(v.detailCall("list", "wf"), { path: "/workflows", maxTime: 8 });
  assert.deepEqual(v.detailCall("runs", "wf"), { path: "/workflows/runs", query: { limit: 20 }, maxTime: 8 });
  assert.deepEqual(v.detailCall("flows", "wf"), { path: "/flows", maxTime: 8 });
  assert.deepEqual(v.detailCall("spec", "wf"), { path: "/workflows/wf", maxTime: 8 });
});

test("detailCall: the spec read needs a name with a route; the other reads do not look at the name", () => {
  for (const bad of [...HOSTILE_NAMES, "runs", "a".repeat(65), ...NOT_STRINGS]) {
    assert.equal(v.detailCall("spec", bad), null, typeof bad === "string" ? JSON.stringify(bad.slice(0, 12)) : typeof bad);
    for (const read of ["list", "runs", "flows"]) assert.notEqual(v.detailCall(read, bad), null);
  }
  for (const name of ["__proto__", "constructor", "toString", "validate", "Runs"]) assert.equal(v.detailCall("spec", name).path, `/workflows/${name}`);
});

test("detailCall: no other read exists, so no other route can be asked for", () => {
  for (const read of ["approve", "plans/approve", "", "LIST", "__proto__", "constructor", "toString", null, undefined, 5, {}]) assert.equal(v.detailCall(read, "wf"), null, String(read));
});

test("detailCall: every call is one curl request the data path accepts, to the CAO machine's own address", () => {
  const cfg = { machine: "cao", port: 9889, stdin: false };
  const urls = { list: "http://127.0.0.1:9889/workflows", runs: "http://127.0.0.1:9889/workflows/runs?limit=20", flows: "http://127.0.0.1:9889/flows", spec: "http://127.0.0.1:9889/workflows/wf" };
  for (const [read, url] of Object.entries(urls)) {
    const built = curlRequest(cfg, v.detailCall(read, "wf"));
    assert.equal(built.ok, true, read);
    assert.equal(built.exec.args.at(-1), url);
    assert.ok(!built.exec.args.includes("-X"), "a GET");
  }
});

test("detailCall: each call returns its own object", () => {
  const a = v.detailCall("runs", "wf");
  a.query.limit = 500;
  a.path = "/x";
  assert.deepEqual(v.detailCall("runs", "wf").query, { limit: 20 });
  assert.equal(v.detailCall("runs", "wf").path, "/workflows/runs");
});

test("detailView: the list and the runs have a view that folds their answers; the flows and the spec do not", () => {
  assert.equal(v.detailView("list"), "workflows");
  assert.equal(v.detailView("runs"), "workflowRuns");
  for (const other of ["flows", "spec", "", "__proto__", "constructor", null, undefined]) assert.equal(v.detailView(other), null, String(other));
});

// ---- foldDetailRead ----------------------------------------------------------------------------------------------

const PREV = (over = {}) => ({ name: "wf", row: undefined, runs: null, flows: [], spec: null, specOk: false, specState: "loading", error: null, notes: new Map(), ...over });

test("foldDetailRead: a list that answered gives the workflow's own row and clears the error, and does not stop the poll", () => {
  const list = [row("other"), row("wf", { description: "mine" })];
  const r = v.foldDetailRead(PREV(), "list", reply(200, list));
  assert.deepEqual(Object.keys(r.patch).sort(), ["error", "row"]);
  assert.equal(r.patch.row.description, "mine");
  assert.equal(r.patch.error, null);
  assert.equal(r.stop, false);
});

test("foldDetailRead: a list that no longer holds the workflow gives no row, which is not an error", () => {
  const r = v.foldDetailRead(PREV(), "list", reply(200, [row("other"), null, 5]));
  assert.deepEqual(r, { patch: { row: null, error: null }, stop: false });
  assert.deepEqual(v.foldDetailRead(PREV(), "list", reply(200, [])), { patch: { row: null, error: null }, stop: false });
});

test("Hostile names: a workflow called __proto__, constructor or toString is found in the list by its name, not by lookup", () => {
  for (const name of ["__proto__", "constructor", "toString"]) {
    const r = v.foldDetailRead(PREV({ name }), "list", reply(200, [row("x"), row(name, { description: name })]));
    assert.equal(r.patch.row.description, name);
    assert.equal(v.foldDetailRead(PREV({ name }), "list", reply(200, [row("x")])).patch.row, null);
  }
});

test("foldDetailRead: a list that failed changes only its notice, so the last good row stays; only no answer at all stops the poll", () => {
  const cases = [
    [UNREACHABLE, "CAO did not answer", true], [reply(401, {}), AUTH, false], [reply(403, { detail: "no" }), AUTH, false],
    [reply(500, { detail: "boom" }), "boom", false], [reply(502, ""), "the workflow list could not be read (HTTP 502)", false],
    [reply(200, { not: "a list" }), "the workflow list could not be read", false], [reply(200, "text"), "the workflow list could not be read", false],
    [null, "the workflow list could not be read", false], [undefined, "the workflow list could not be read", false],
  ];
  for (const [o, error, stop] of cases) {
    const r = v.foldDetailRead(PREV({ row: row("wf") }), "list", o);
    assert.deepEqual(r, { patch: { error }, stop }, JSON.stringify(o)?.slice(0, 60));
  }
});

test("foldDetailRead: runs that answered are kept, and the notes they answer are pruned into a new Map", () => {
  const notes = new Map([["a", note("failed", "running")], ["b", note("done", "running")], ["c", note("failed", "running")]]);
  const runs = [run("running", { run_id: "a" }), run("running", { run_id: "b" }), run("completed", { run_id: "c" })];
  const prev = PREV({ notes });
  const r = v.foldDetailRead(prev, "runs", reply(200, runs));
  assert.deepEqual(r.patch.runs.map((x) => x.run_id), ["a", "b", "c"]);
  assert.deepEqual([...r.patch.notes.keys()], ["a"]);
  assert.notEqual(r.patch.notes, notes);
  assert.equal(notes.size, 3, "the notes of the details are as they were");
  assert.equal(r.stop, false);
  assert.deepEqual(Object.keys(r.patch).sort(), ["notes", "runs"]);
});

test("foldDetailRead: runs that did not answer, or answered with something that is not a list, change nothing", () => {
  for (const o of [UNREACHABLE, reply(500, { detail: "x" }), reply(401, {}), reply(200, {}), reply(200, "x"), null, undefined]) {
    assert.deepEqual(v.foldDetailRead(PREV({ runs: [run("running")] }), "runs", o), { patch: {}, stop: false });
  }
  assert.deepEqual(v.foldDetailRead(PREV(), "runs", reply(200, [])).patch.runs, []);
});

test("foldDetailRead: flows that answered are kept; any other answer changes nothing", () => {
  assert.deepEqual(v.foldDetailRead(PREV(), "flows", reply(200, FLOWS)), { patch: { flows: FLOWS }, stop: false });
  for (const o of [UNREACHABLE, reply(401, {}), reply(200, {}), null]) assert.deepEqual(v.foldDetailRead(PREV(), "flows", o), { patch: {}, stop: false });
});

test("foldDetailRead: a spec that was read is kept, with its state 'ok' and the spec marked read", () => {
  const spec = { name: "wf", inputs: {}, steps: [] };
  assert.deepEqual(v.foldDetailRead(PREV(), "spec", reply(200, spec)), { patch: { spec, specOk: true, specState: "ok" }, stop: false });
});

test("foldDetailRead: a spec read that failed is 'failed' the first time, and keeps 'ok' once one read has succeeded", () => {
  for (const o of [UNREACHABLE, reply(404, { detail: "unknown workflow" }), reply(401, {}), reply(200, [run("running")]), reply(200, "text"), reply(200, null), null, undefined]) {
    assert.deepEqual(v.foldDetailRead(PREV({ specOk: false, specState: "loading" }), "spec", o), { patch: { specState: "failed" }, stop: false });
    assert.deepEqual(v.foldDetailRead(PREV({ specOk: true, specState: "ok", spec: { name: "wf" } }), "spec", o), { patch: { specState: "ok" }, stop: false });
  }
});

test("foldDetailRead: a read nobody asked for changes nothing, and nothing it is given is changed", () => {
  for (const read of ["approve", "", "__proto__", null, undefined]) assert.deepEqual(v.foldDetailRead(PREV(), read, reply(200, [])), { patch: {}, stop: false });
  const prev = frozen(PREV({ notes: undefined }));
  for (const read of ["list", "runs", "flows", "spec"]) assert.doesNotThrow(() => v.foldDetailRead(prev, read, frozen(reply(200, [row("wf")]))));
  assert.equal(v.foldDetailRead(prev, "runs", reply(200, [])).patch.notes.size, 0);
});

// ======== what a submit answer does ===========================================================================

const SUB = { name: "wf", inputs: { a: "1" }, workingDirectory: "/p" };
const fx = (outcome, held = {}) => v.runSubmitEffects(frozen(outcome), frozen(SUB), held);

test("A started run: it is announced under the workflow and moves the stamp, the form that asked closes, no banner", () => {
  const e = fx(reply(202, { run_id: "r9" }), { formHeld: true, recHeld: false, prior: null });
  assert.deepEqual(e.view, { type: "started", runId: "r9" });
  assert.deepEqual(e.started, { name: "wf", runId: "r9" });
  assert.equal(e.stamp, true);
  assert.equal(e.dropForm, true);
  assert.deepEqual(e.record, { op: "delete" });
  assert.equal(e.formBanner, null);
  assert.equal(e.recBanner, null);
});

test("A started run replaces the local row: a kept approval record is deleted, and a late answer is still announced", () => {
  const prior = v.approvalRecord(null, approvalOutcome("wf:abc"), SUB);
  const e = fx(reply(202, {}), { formHeld: false, recHeld: true, prior });
  assert.deepEqual(e.record, { op: "delete" });
  assert.deepEqual(e.started, { name: "wf", runId: null });
  assert.equal(e.stamp, true);
  assert.equal(e.dropForm, false, "the form that asked is gone: the form open now is not closed");
  assert.equal(e.recBanner, null);
});

test("First run needs approval: a 403 approval_required sets the kept record with the submission, closes the form, and shows no failure", () => {
  const e = fx(approvalOutcome("wf:abc", "approve it"), { formHeld: true, recHeld: false, prior: null });
  assert.deepEqual(e.view, { type: "approval" });
  assert.equal(e.record.op, "set");
  assert.equal(e.record.value.state.planId, "wf:abc");
  assert.deepEqual(e.record.value.submission, SUB);
  assert.equal(e.record.value.sending, false);
  assert.equal(e.dropForm, true);
  assert.equal(e.started, null);
  assert.equal(e.stamp, false);
  assert.equal(e.formBanner, null);
  assert.equal(e.recBanner, null);
});

test("Submit again after approval: a second refusal replaces the record, and does not clear a record's own failure line as a failure", () => {
  const prior = v.approvalRecord(null, approvalOutcome("wf:one"), SUB);
  const e = fx(approvalOutcome("wf:two"), { formHeld: false, recHeld: true, prior });
  assert.equal(e.record.op, "set");
  assert.equal(e.record.value.state.planId, "wf:two");
  assert.notEqual(e.record.value, prior);
  assert.equal(e.dropForm, false);
  assert.equal(e.recBanner, null);
});

test("A refused submission: the server's words reach the form that asked and the record that asked, only while each is still held", () => {
  const o = reply(422, { detail: "name_or_path: not found" });
  const both = fx(o, { formHeld: true, recHeld: true, prior: null });
  assert.deepEqual(both.view, { type: "failed", text: "name_or_path: not found" });
  assert.equal(both.formBanner, "name_or_path: not found");
  assert.equal(both.recBanner, "name_or_path: not found");
  assert.equal(both.dropForm, false);
  assert.equal(both.started, null);
  assert.equal(both.stamp, false);
  const formOnly = fx(o, { formHeld: true, recHeld: false });
  assert.equal(formOnly.formBanner, "name_or_path: not found");
  assert.equal(formOnly.recBanner, null);
  const recOnly = fx(o, { formHeld: false, recHeld: true });
  assert.equal(recOnly.formBanner, null);
  assert.equal(recOnly.recBanner, "name_or_path: not found");
});

test("A refused submission keeps the plan-approval record as it was", () => {
  const prior = v.approvalRecord(null, approvalOutcome("wf:one"), SUB);
  for (const o of [reply(500, { detail: "x" }), UNREACHABLE, reply(401, {}), reply(422, { detail: "bad" })]) {
    const e = fx(o, { formHeld: true, recHeld: true, prior });
    assert.deepEqual(e.record, { op: "keep" }, JSON.stringify(o).slice(0, 40));
  }
});

test("A late answer for a form that was discarded never resets a form that did not ask, whatever the answer", () => {
  for (const o of [reply(202, { run_id: "r" }), approvalOutcome(), reply(500, { detail: "x" }), UNREACHABLE, reply(401, {}), null]) {
    const e = fx(o, { formHeld: false, recHeld: false, prior: null });
    assert.equal(e.dropForm, false, "no form is closed");
    assert.equal(e.formBanner, null, "no form gets a banner");
    assert.equal(e.recBanner, null);
  }
});

test("Auth: a refusal for auth is its own view with the auth words on the form and the record, and is never the approval view", () => {
  for (const o of [reply(401, {}), reply(403, { detail: "forbidden" }), reply(403, { detail: { kind: "other" } })]) {
    const e = fx(o, { formHeld: true, recHeld: true });
    assert.deepEqual(e.view, { type: "auth", text: AUTH });
    assert.equal(e.formBanner, AUTH);
    assert.equal(e.recBanner, AUTH);
    assert.equal(e.dropForm, false);
    assert.equal(e.started, null);
  }
  assert.equal(fx(approvalOutcome(), { formHeld: true }).view.type, "approval");
});

test("No answer: the run may have started, so no run is announced and the stamp does not move; the form keeps its text with that line", () => {
  const e = fx(UNREACHABLE, { formHeld: true, recHeld: false });
  assert.equal(e.view.type, "unreachable");
  assert.match(e.formBanner, /may have started/);
  assert.equal(e.started, null);
  assert.equal(e.stamp, false);
  assert.equal(e.dropForm, false);
});

test("A refusal with a blank detail still has words, with the status", () => {
  const e = fx(reply(502, ""), { formHeld: true, recHeld: true });
  assert.equal(e.formBanner, "The run was not submitted (HTTP 502)");
  assert.equal(e.recBanner, "The run was not submitted (HTTP 502)");
});

test("runSubmitEffects: with no held form or record given, nothing is held", () => {
  const e = v.runSubmitEffects(reply(500, { detail: "x" }), SUB);
  assert.equal(e.formBanner, null);
  assert.equal(e.recBanner, null);
  assert.equal(e.dropForm, false);
  assert.equal(v.runSubmitEffects(reply(202, {}), SUB, undefined).dropForm, false);
});

test("runSubmitEffects: for every answer and every holding, a form is closed or given a banner only if it is held, and the parts agree", () => {
  const prior = v.approvalRecord(null, approvalOutcome("wf:p"), SUB);
  const answers = [reply(202, { run_id: "r" }), reply(202, {}), approvalOutcome(), approvalOutcome(null), reply(401, {}), reply(403, { detail: "x" }), reply(404, { detail: "x" }), reply(500, ""), UNREACHABLE, null, undefined, {}];
  for (const o of answers) for (const formHeld of [true, false]) for (const recHeld of [true, false]) for (const p of [null, prior]) {
    const e = fx(o, { formHeld, recHeld, prior: p });
    const label = `${JSON.stringify(o)?.slice(0, 30)} form ${formHeld} rec ${recHeld} prior ${p !== null}`;
    if (e.dropForm) assert.equal(formHeld, true, label);
    if (e.formBanner !== null) assert.equal(formHeld, true, label);
    if (e.recBanner !== null) assert.equal(recHeld, true, label);
    assert.equal(e.stamp, e.started !== null, label);
    assert.equal(e.started !== null, e.view.type === "started", label);
    assert.equal(e.dropForm, formHeld && (e.view.type === "started" || e.view.type === "approval"), label);
    assert.equal(e.record.op === "set", e.view.type === "approval", label);
    if (e.view.type === "started") assert.equal(e.record.op, "delete", label);
    if (e.view.type === "approval") assert.equal(e.formBanner, null, label);
    if (formHeld && e.formBanner !== null) assert.equal(e.formBanner, e.view.text, label);
  }
});

test("Hostile names: a workflow called __proto__ or constructor is announced under that name, nothing else is touched", () => {
  for (const name of ["__proto__", "constructor", "toString"]) {
    const e = v.runSubmitEffects(reply(202, { run_id: "r" }), { name, inputs: {}, workingDirectory: "" }, { formHeld: true });
    assert.deepEqual(e.started, { name, runId: "r" });
    assert.equal(Object.getPrototypeOf(e.started), Object.prototype);
    const a = v.runSubmitEffects(approvalOutcome(), { name, inputs: {}, workingDirectory: "" }, { formHeld: true });
    assert.equal(a.record.value.submission.name, name);
  }
});

// ======== the CLI hint of a source that is too large ======================================================
// A source over the argument-mode limit is refused before any request, and the refusal prints a command that runs:
// `cao workflow create <name> --from-file <path>` for a new Python workflow, `cao workflow update <name> --from-file
// <path> --expected-hash <hash>` for a Python edit (the CLI requires the hash), and for YAML only the words that
// create and update take a Python file. `<path>` is a placeholder the user replaces.

const HASH = "9f86d081884c7d659a2feaa0c55ad0159f86d081884c7d659a2feaa0c55ad015";
const BIGTEXT = "y".repeat(130_000);
const hintOf = (tier, name, expectedHash) => {
  const r = v.editorSizeCheck({ name, tier, source: BIGTEXT }, { stdin: false, expectedHash });
  assert.equal(r.ok, false);
  const m = /Paste a smaller spec or use the CLI \(([\s\S]*)\)\.$/.exec(r.message);
  assert.notEqual(m, null, r.message);
  return m[1];
};
// The command as a shell reads it once the placeholder is replaced by a path.
const argvOf = (cmd) => shellArgv(cmd.replace("<path>", "/tmp/x.py"), "cao");

test("The CLI hint: a new Python workflow prints cao workflow create with its name", () => {
  assert.equal(hintOf("python", "wf", undefined), "cao workflow create wf --from-file <path>");
  assert.equal(hintOf("python", "wf", ""), "cao workflow create wf --from-file <path>", "no hash read yet: a new workflow's command");
  assert.equal(hintOf("python", "wf", null), "cao workflow create wf --from-file <path>");
});

test("The CLI hint: a Python edit prints cao workflow update with its name and the hash the editor holds", () => {
  assert.equal(hintOf("python", "wf", HASH), `cao workflow update wf --from-file <path> --expected-hash ${HASH}`);
  assert.equal(hintOf("python", "wf", "abc"), "cao workflow update wf --from-file <path> --expected-hash abc");
});

test("The CLI hint: YAML, new or edit, has no command, only that create and update take a Python file", () => {
  for (const hash of [undefined, "", HASH]) {
    const h = hintOf("yaml", "wf", hash);
    assert.equal(h, "cao workflow create and update take a Python file only");
    assert.doesNotMatch(h, /--from-file|--expected-hash|<path>/);
    assert.ok(!h.includes(HASH));
  }
});

test("The CLI hint: every combination of tier, new or edit and hash, with the name and hash it was given", () => {
  const rows = [
    ["python", "new", undefined, "cao workflow create wf --from-file <path>"],
    ["python", "edit", HASH, `cao workflow update wf --from-file <path> --expected-hash ${HASH}`],
    ["yaml", "new", undefined, "cao workflow create and update take a Python file only"],
    ["yaml", "edit", HASH, "cao workflow create and update take a Python file only"],
  ];
  for (const [tier, , hash, want] of rows) assert.equal(hintOf(tier, "wf", hash), want, `${tier} ${hash ? "edit" : "new"}`);
});

test("The CLI hint: the command is the same size rule's: stdin mode sends the source, so there is no hint, and a source that fits has none", () => {
  const stdin = v.editorSizeCheck({ name: "wf", tier: "python", source: "x".repeat(131_073) }, { stdin: true });
  assert.equal(stdin.ok, false);
  assert.doesNotMatch(stdin.message, /--from-file|CLI/);
  assert.equal(v.editorSizeCheck({ name: "wf", tier: "python", source: "x" }, {}).message, null);
});

test("The CLI hint: a name the CLI could not be given as one word prints a placeholder, never the name", () => {
  for (const name of [...HOSTILE_NAMES, ...NOT_STRINGS.filter((n) => typeof n !== "symbol" && typeof n !== "bigint")]) {
    for (const hash of [undefined, HASH]) {
      const h = hintOf("python", name, hash);
      assert.ok(h.includes("<name>"), JSON.stringify(name));
      if (typeof name === "string" && name !== "") assert.ok(!h.includes(name), JSON.stringify(name));
      assert.ok(!h.includes("\n"), "one line");
    }
  }
});

test("The CLI hint: a name the editor takes (including odd ones) prints as it is, and the printed command parses to its exact words", () => {
  for (const name of ["wf", "my-flow_2", "A1", "__proto__", "constructor", "-x", "a".repeat(64), "runs"]) {
    assert.deepEqual(argvOf(hintOf("python", name, undefined)), ["workflow", "create", name, "--from-file", "/tmp/x.py"], name);
    assert.deepEqual(argvOf(hintOf("python", name, HASH)), ["workflow", "update", name, "--from-file", "/tmp/x.py", "--expected-hash", HASH], name);
  }
});

test("The CLI hint: a name that is not safe never makes the printed command more than its own words", () => {
  const cwd = globDir();
  for (const name of [...HARD_WORDS, "a b", "x\ny", "it's", "$(id)", "a;b", "\u{1F600}"]) {
    assert.deepEqual(shellArgv(hintOf("python", name, undefined).replace("<path>", "/tmp/x.py").replace("<name>", "NAME"), "cao", { cwd }), ["workflow", "create", "NAME", "--from-file", "/tmp/x.py"], JSON.stringify(name));
    assert.deepEqual(shellArgv(hintOf("python", name, HASH).replace("<path>", "/tmp/x.py").replace("<name>", "NAME"), "cao", { cwd }), ["workflow", "update", "NAME", "--from-file", "/tmp/x.py", "--expected-hash", HASH], JSON.stringify(name));
  }
});

test("The CLI hint: the request's own cli string follows the same rule, and is left out for YAML", () => {
  assert.equal(saveRequest({ mode: "new", name: "wf", tier: "python", source: "s" }).cli, "cao workflow create wf --from-file <path>");
  assert.equal(saveRequest({ mode: "edit", name: "wf", tier: "python", source: "s", expectedHash: HASH }).cli, `cao workflow update wf --from-file <path> --expected-hash ${HASH}`);
  assert.equal(Object.hasOwn(saveRequest({ mode: "new", name: "wf", tier: "yaml", source: "s" }), "cli"), false);
  assert.equal(Object.hasOwn(saveRequest({ mode: "edit", name: "wf", tier: "yaml", source: "s", expectedHash: HASH }), "cli"), false);
  for (const name of ["__proto__", "constructor", "-x", "a".repeat(64)]) {
    assert.deepEqual(argvOf(saveRequest({ mode: "new", name, tier: "python", source: "s" }).cli), ["workflow", "create", name, "--from-file", "/tmp/x.py"], name);
    assert.deepEqual(argvOf(saveRequest({ mode: "edit", name, tier: "python", source: "s", expectedHash: HASH }).cli), ["workflow", "update", name, "--from-file", "/tmp/x.py", "--expected-hash", HASH], name);
  }
});

test("The CLI hint: a request refused for size names that command, or 'the cao CLI' when there is none (YAML)", async () => {
  const cfg = { machine: "cao", port: 9889, stdin: false };
  const send = (req) => callCao(() => { throw new Error("must not run"); }, cfg, { ...req, body: { ...req.body, source: BIGTEXT } });
  const py = await send(saveRequest({ mode: "edit", name: "wf", tier: "python", source: "s", expectedHash: HASH }));
  assert.equal(py.kind, "too_large");
  assert.ok(py.message.includes(`\`cao workflow update wf --from-file <path> --expected-hash ${HASH}\``), py.message);
  const created = await send(saveRequest({ mode: "new", name: "wf", tier: "python", source: "s" }));
  assert.ok(created.message.includes("`cao workflow create wf --from-file <path>`"), created.message);
  for (const req of [saveRequest({ mode: "new", name: "wf", tier: "yaml", source: "s" }), saveRequest({ mode: "edit", name: "wf", tier: "yaml", source: "s", expectedHash: HASH })]) {
    const o = await send(req);
    assert.ok(o.message.endsWith("use `the cao CLI` instead"), o.message);
    assert.equal(o.cli, null);
  }
});

// ======== a template that was renamed is still the template ===============================================

const YAML_T = { id: "y", tier: "yaml", name: "hello", source: "name: hello\ndescription: d\nsteps:\n  - id: a\n    name: step\n" };
const PY_T = { id: "p", tier: "python", name: "tp", source: "name: not-a-spec\nprint(1)\n" };
const BOTH = [PY_T, YAML_T];
const fm = (name, source, tier = "yaml") => ({ name, tier, source });

test("isTemplateText: the template's raw text is the template, whatever name is typed", () => {
  for (const name of ["", "hello", "mine", "a b", "runs", "a".repeat(65)]) assert.equal(v.isTemplateText(fm(name, YAML_T.source), YAML_T), true, JSON.stringify(name));
  for (const name of ["", "tp", "mine"]) assert.equal(v.isTemplateText(fm(name, PY_T.source, "python"), PY_T), true, name);
});

test("isTemplateText: a YAML template renamed to the typed name is still the template", () => {
  for (const name of ["mine", "a-b_c", "x".repeat(64), "validate"]) {
    const renamed = v.templateFor(fm(name, ""), YAML_T);
    assert.equal(renamed.source.startsWith(`name: ${name}\n`), true);
    assert.equal(v.isTemplateText(fm(name, renamed.source), YAML_T), true, name);
  }
});

test("isTemplateText: a name typed over text that was otherwise left alone is still the template", () => {
  const picked = v.templateFor(v.emptyWorkflowForm(), YAML_T);
  assert.equal(picked.name, "hello");
  assert.equal(v.isTemplateText(picked, YAML_T), true);
  assert.equal(v.isTemplateText({ ...picked, name: "mine" }, YAML_T), true, "the text still says hello");
  assert.equal(v.isTemplateText({ ...picked, name: "" }, YAML_T), true);
});

test("isTemplateText: a user edit is not the template, with or without a name typed", () => {
  const edits = [`${YAML_T.source} `, YAML_T.source.replace("description: d", "description: mine"), YAML_T.source.replace("steps:", "steps: "), `# c\n${YAML_T.source}`, "", YAML_T.source.trim(), YAML_T.source.replace("name: hello", "name: HELLO")];
  for (const source of edits) for (const name of ["", "hello", "mine"]) assert.equal(v.isTemplateText(fm(name, source), YAML_T), false, `${JSON.stringify(source.slice(0, 30))} as ${name}`);
});

test("isTemplateText: text renamed for another name than the form holds is the user's own", () => {
  const forMine = v.templateFor(fm("mine", ""), YAML_T).source;
  assert.equal(v.isTemplateText(fm("other", forMine), YAML_T), false);
  assert.equal(v.isTemplateText(fm("", forMine), YAML_T), false);
});

test("isTemplateText: a name the editor refuses does not rename the template, so a renamed text with that name is not it", () => {
  for (const bad of ["a b", "runs", "a".repeat(65), "x.yaml", "../x"]) {
    const renamed = YAML_T.source.replace("name: hello", `name: ${bad}`);
    assert.equal(v.isTemplateText(fm(bad, renamed), YAML_T), false, bad);
    assert.equal(v.isTemplateText(fm(bad, YAML_T.source), YAML_T), true, bad);
  }
});

test("isTemplateText: each template is itself and not the other, and a Python template is never counted renamed", () => {
  assert.equal(v.isTemplateText(fm("", YAML_T.source), PY_T), false);
  assert.equal(v.isTemplateText(fm("", PY_T.source, "python"), YAML_T), false);
  assert.equal(v.isTemplateText(fm("mine", PY_T.source.replace("not-a-spec", "mine"), "python"), PY_T), false);
  assert.equal(v.isTemplateText(fm("mine", PY_T.source, "python"), PY_T), true);
});

test("isTemplateText: a form that is not there, or has no text, is no template", () => {
  for (const bad of [null, undefined, {}, { source: 5 }, { source: null }, "x"]) assert.equal(v.isTemplateText(bad, YAML_T), false, JSON.stringify(bad));
  assert.equal(v.isTemplateText(frozen(fm("mine", YAML_T.source)), frozen(YAML_T)), true, "nothing is changed");
});

test("isTemplateText: the shipped templates, picked under any name the editor takes, are themselves and only themselves", () => {
  for (const t of TEMPLATES) for (const name of ["", "mine", "other-name", "a".repeat(64)]) {
    const picked = v.templateFor(fm(name, ""), t);
    for (const other of TEMPLATES) assert.equal(v.isTemplateText(picked, other), other.id === t.id, `${t.id} picked as ${name || "(empty)"}, asked ${other.id}`);
  }
});

test("templateNeedsConfirm: only a user's own text asks; a template, a renamed template and no text do not", () => {
  const f = (name, source) => fm(name, source);
  assert.equal(v.templateNeedsConfirm(f("", ""), BOTH), false);
  assert.equal(v.templateNeedsConfirm(f("", " \n"), BOTH), false);
  assert.equal(v.templateNeedsConfirm(f("", YAML_T.source), BOTH), false);
  assert.equal(v.templateNeedsConfirm(f("", PY_T.source), BOTH), false);
  assert.equal(v.templateNeedsConfirm(f("mine", v.templateFor(f("mine", ""), YAML_T).source), BOTH), false, "only the name typed does not ask");
  assert.equal(v.templateNeedsConfirm(f("mine", YAML_T.source), BOTH), false, "a name typed over the template does not ask");
  assert.equal(v.templateNeedsConfirm(f("", `${YAML_T.source}# mine\n`), BOTH), true);
  assert.equal(v.templateNeedsConfirm(f("mine", v.templateFor(f("other", ""), YAML_T).source), BOTH), true);
  assert.equal(v.templateNeedsConfirm(f("", "typed"), BOTH), true);
  assert.equal(v.templateNeedsConfirm(f("", "typed"), []), true);
  for (const bad of [null, undefined, "x", {}]) assert.equal(v.templateNeedsConfirm(f("", "typed"), bad), true);
  for (const bad of [null, undefined, {}, { source: 5 }]) assert.equal(v.templateNeedsConfirm(bad, BOTH), false);
});

const specNameMismatchOf = (f) => v.specNameMismatch(f.tier, f.source, f.name);

test("Template picking: picking, typing a name, and picking again never asks until the text is edited", () => {
  let form = v.templateFor(v.emptyWorkflowForm(), YAML_T);
  assert.equal(v.templateNeedsConfirm(form, BOTH), false);
  form = { ...form, name: "mine" };
  assert.equal(v.templateNeedsConfirm(form, BOTH), false, "after typing the name");
  form = v.templateFor(form, YAML_T);
  assert.equal(form.name, "mine");
  assert.equal(specNameMismatchOf(form), null, "the text now carries the typed name");
  assert.equal(v.templateNeedsConfirm(form, BOTH), false, "after picking again");
  form = v.templateFor(form, PY_T);
  assert.equal(form.tier, "python");
  assert.equal(v.templateNeedsConfirm(form, BOTH), false, "after switching to the other template");
  form = { ...form, source: `${form.source}# edited\n` };
  assert.equal(v.templateNeedsConfirm(form, BOTH), true, "after an edit");
});

test("The template chips: at most one is lit, the one whose text is on screen, renamed or not, and none after an edit", () => {
  const lit = (form) => BOTH.filter((t) => v.isTemplateText(form, t)).map((t) => t.id);
  assert.deepEqual(lit(fm("", "")), []);
  assert.deepEqual(lit(v.templateFor(v.emptyWorkflowForm(), YAML_T)), ["y"]);
  assert.deepEqual(lit(v.templateFor(fm("mine", ""), YAML_T)), ["y"]);
  assert.deepEqual(lit({ ...v.templateFor(v.emptyWorkflowForm(), YAML_T), name: "mine" }), ["y"]);
  assert.deepEqual(lit(v.templateFor(v.emptyWorkflowForm(), PY_T)), ["p"]);
  assert.deepEqual(lit({ ...v.templateFor(fm("mine", ""), YAML_T), source: "edited" }), []);
  assert.deepEqual(lit(fm("", "name: hello")), []);
});

// ======== the details show a stale-runs note too ==========================================================

test("A failed runs read keeps the last runs, and the details note says how old they are", () => {
  const T0 = NOW - 5 * MIN;
  const runsView = applyOutcome(applyOutcome(null, reply(200, [run("running")]), T0), UNREACHABLE, NOW);
  const listView = applyOutcome(null, reply(200, [row("wf")]), NOW);
  assert.deepEqual(runsView.data.map((r) => r.state), ["running"], "the last good runs stay");
  assert.deepEqual(v.workflowsNotes(listView, runsView, NOW), ["Runs: CAO unreachable, retrying", "Runs: Showing the last list, 5 minutes old"]);
  const folded = v.foldDetailRead(PREV({ runs: [run("running")] }), "runs", UNREACHABLE);
  assert.deepEqual(folded, { patch: {}, stop: false }, "the details keep their runs as they were");
});

test("The details' notes are the section's: the list's own lines first, then what only the runs read adds, marked Runs:", () => {
  const T0 = NOW - 2 * MIN;
  const listView = applyOutcome(applyOutcome(null, reply(200, [row("wf")]), T0), UNREACHABLE, NOW);
  const runsView = applyOutcome(applyOutcome(null, reply(200, [run("running")]), T0 - 3 * MIN), UNREACHABLE, NOW);
  const notes = v.workflowsNotes(listView, runsView, NOW);
  assert.deepEqual(notes, ["CAO unreachable, retrying", "Showing the last list, 2 minutes old", "Runs: Showing the last list, 5 minutes old"]);
});

test("The details' notes: a runs read that failed with an error or auth is told too, and a healthy pair says nothing", () => {
  const ok = (t) => applyOutcome(null, reply(200, []), t);
  assert.deepEqual(v.workflowsNotes(ok(NOW), ok(NOW), NOW), []);
  const errView = applyOutcome(ok(NOW - 10 * MIN), reply(500, { detail: "db is down" }), NOW);
  assert.deepEqual(v.workflowsNotes(ok(NOW), errView, NOW), ["Runs: CAO answered with an error: db is down", "Runs: Showing the last list, 10 minutes old"]);
  assert.deepEqual(v.workflowsNotes(ok(NOW), applyOutcome(null, reply(401, {}), NOW), NOW), [`Runs: ${AUTH}`]);
});

test("The details and the section read the same notes for the same two views", () => {
  const runsView = applyOutcome(applyOutcome(null, reply(200, [run("running")]), NOW - 7 * MIN), UNREACHABLE, NOW);
  const listView = applyOutcome(null, reply(200, [row("wf")]), NOW);
  const rows = v.workflowRows(listView.data, runsView.data);
  assert.deepEqual(rows, [{ name: "wf", state: "running" }], "the section still shows the last good state");
  const d = v.workflowDetailsView(frozen({ name: "wf", row: listView.data[0], runs: runsView.data, flows: [], now: NOW }));
  assert.equal(d.runs.length, 1, "the details still list the last good run");
  assert.ok(v.workflowsNotes(listView, runsView, NOW).some((l) => /7 minutes old/.test(l)));
});

test("The CLI hint: a hash that is not plain text cannot break out of the printed command, in the hint or in the request's cli string", () => {
  const cwd = globDir();
  for (const hash of ["a b", "x;touch x", "$(id)", "`id`", "x\ny", "it's", "a|b", "a&b", "a>b", "*", "~", "#c", "\u{1F600}", "--x", "-"]) {
    const want = ["workflow", "update", "wf", "--from-file", "/tmp/x.py", "--expected-hash", hash];
    assert.deepEqual(shellArgv(hintOf("python", "wf", hash).replace("<path>", "/tmp/x.py"), "cao", { cwd }), want, `hint, ${JSON.stringify(hash)}`);
    const cli = saveRequest({ mode: "edit", name: "wf", tier: "python", source: "s", expectedHash: hash }).cli;
    assert.deepEqual(shellArgv(cli.replace("<path>", "/tmp/x.py"), "cao", { cwd }), want, `cli, ${JSON.stringify(hash)}`);
  }
});

await finish();
