// lib/flows.js: the Flows section's rules (spec cao-window-flows; honesty labels flowRoutesMissing,
// flowRuns and createChecksSkipped of cao-window-honesty-labels). Tasks 10.1 and the 10.4 test.
//
// The scheduler claim behind numericWeekdayWarning is the server's, not the window's: APScheduler 3.11.3
// counts the day of week from Monday as 0, so `1-5` is Tuesday to Saturday. The Python test
// test/examples/test_herdr_app_cao_window_cron_premise.py checks that claim against the installed
// APScheduler; this file checks only what the window says about it.
//
// Every function is called inside a test case through the namespace, so a function that does not exist
// yet fails its own case with a TypeError, and the rest of the file still runs.
import { assert, finish, test } from "../test/harness.mjs";
import { existsSync, readFileSync } from "node:fs";
import { HARD_WORDS, globDir, needsQuotes, quotedCount, shellArgv } from "../test/shell-argv.mjs";
import { HONESTY_PENDING } from "./honesty.js";
import * as flows from "./flows.js";
import { dialogKey, sessionsFrom } from "./logic.js";

const NEEDS_CHANGE = "needs a CAO change";
const CHECKS_SKIPPED = "server checks skipped";

const FLOW = (over = {}) => ({ name: "daily", enabled: true, next_run: "2026-11-19T09:00:00", schedule: "0 9 * * mon-fri", ...over });

// ---- rows ------------------------------------------------------------------------------------------

test("All disabled: five flows read 'off' and the folded summary reads '5, all off'", () => {
  const rows = Array.from({ length: 5 }, (_, i) => FLOW({ name: `f${i}`, enabled: false }));
  assert.deepEqual(rows.map((f) => flows.flowRowState(f, {}).text), ["off", "off", "off", "off", "off"]);
  assert.equal(flows.flowsSummary(rows), "5, all off");
});

test("No flows: the section reads 'none yet'", () => {
  assert.equal(flows.flowsSummary([]), "none yet");
});

test("flowsSummary of a mixed list says how many there are and does not say 'all off'", () => {
  const s = flows.flowsSummary([FLOW(), FLOW({ name: "b", enabled: false })]);
  assert.match(s, /^2\b/);
  assert.doesNotMatch(s, /all off/);
  assert.match(flows.flowsSummary([FLOW(), FLOW({ name: "b" })]), /^2\b/);
});

test("an enabled flow reads as its next run in words, not as a raw timestamp", () => {
  const t = flows.flowRowState(FLOW(), {}).text;
  assert.match(t, /^next /);
  assert.ok(t.includes("09:00"), t);
  assert.ok(!t.includes("T09:00:00"), `a raw ISO time: ${t}`);
  assert.notEqual(flows.flowRowState(FLOW({ next_run: null }), {}).text, "off", "an enabled flow with no stored next run is not 'off'");
});

test("a disabled flow reads 'off' whatever its stored next run says", () => {
  assert.equal(flows.flowRowState(FLOW({ enabled: false, next_run: "2026-11-19T09:00:00" }), {}).text, "off");
  assert.equal(flows.flowRowState(FLOW({ enabled: false, next_run: null }), {}).text, "off");
});

test("Running from the session: 'running' while the cao-flow-<name> terminal works or Run now is in flight", () => {
  assert.equal(flows.flowRowState(FLOW(), { sessionWorking: true }).text, "running");
  assert.equal(flows.flowRowState(FLOW(), { runNowInFlight: true }).text, "running");
  assert.equal(flows.flowRowState(FLOW({ enabled: false }), { runNowInFlight: true }).text, "running", "a disabled flow can be run now");
  assert.equal(flows.flowRowState(FLOW({ enabled: false }), { sessionWorking: true }).text, "running");
  assert.notEqual(flows.flowRowState(FLOW(), { sessionWorking: false, runNowInFlight: false }).text, "running");
});

test("flowRowState with no options and a malformed next_run does not throw", () => {
  assert.equal(typeof flows.flowRowState(FLOW()).text, "string");
  for (const next_run of ["", "not a date", 0, {}, "2026-13-45T99:99:99"]) assert.equal(typeof flows.flowRowState(FLOW({ next_run }), {}).text, "string", JSON.stringify(next_run));
});

test("the Flows section refreshes every 30 s", () => {
  assert.equal(flows.FLOWS_POLL_MS, 30_000);
});

// ---- schedule in words -----------------------------------------------------------------------------

test("cronHuman words the forms that read the same in standard cron and in the server", () => {
  const w = flows.cronHuman("0 9 * * mon-fri");
  assert.match(w, /weekday/i);
  assert.ok(w.includes("09:00"), w);
  const d = flows.cronHuman("30 8 * * *");
  assert.match(d, /daily|every day/i);
  assert.ok(d.includes("08:30"), d);
  assert.match(flows.cronHuman("* * * * *"), /every minute/i);
  assert.match(flows.cronHuman("*/15 * * * *"), /15/);
  assert.match(flows.cronHuman("5 * * * *"), /:05|05/);
});

test("cronHuman gives no words for numeric day-of-week, for anything it is not sure of, or for garbage", () => {
  for (const cron of ["0 9 * * 1-5", "0 9 * * 0", "0 9 * * 1,3,5", "0 9 * * */2", "not a cron",
    "", "   ", "* * * *", "* * * * * *", "0 25 * * *", "60 9 * * *", "0 24 * * *", "-1 9 * * *", "0 9 * * mon-fri extra"]) {
    assert.equal(flows.cronHuman(cron), null, JSON.stringify(cron));
  }
  for (const bad of [null, undefined, 5, {}, []]) assert.equal(flows.cronHuman(bad), null, String(bad));
});

test("Numeric day of week: the warning says the server counts from Monday as 0, 1-5 is Tuesday to Saturday, and suggests names", () => {
  const w = flows.numericWeekdayWarning("0 9 * * 1-5");
  assert.match(w, /Monday as 0/);
  assert.match(w, /Tuesday to Saturday/);
  assert.match(w, /mon-fri/);
});

test("the warning fires for any digit in the day-of-week field, in any form", () => {
  for (const cron of ["0 9 * * 0", "0 9 * * 1-5", "0 9 * * 1,3", "0 9 * * */2", "0 9 * * mon-5", "0 9 * * 6", "0 9 * * 7", "  0   9 *\t* 1-5  "]) {
    assert.equal(typeof flows.numericWeekdayWarning(cron), "string", JSON.stringify(cron));
  }
});

test("no warning when the day-of-week field has names or a star, whatever the other fields hold; none for text that is not a cron", () => {
  for (const cron of ["0 9 * * mon-fri", "0 9 * * *", "*/5 * * * *", "0 9 1 * *", "30 8 15 6 *", "0 9 * * mon,wed", "nonsense", "", "1 2 3 4", "1 2 3 4 5 6"]) {
    assert.equal(flows.numericWeekdayWarning(cron), null, JSON.stringify(cron));
  }
  for (const bad of [null, undefined, 5]) assert.equal(flows.numericWeekdayWarning(bad), null, String(bad));
});

test("Disabled flow with a stale next run: a stored time in the past is stale; in the future, or for an enabled flow, it is not", () => {
  const now = Date.UTC(2026, 10, 19, 12, 0, 0);
  const disabled = (next_run) => FLOW({ enabled: false, next_run });
  assert.equal(flows.staleNextRun(disabled("2026-11-01T09:00:00"), now, -480), true);
  assert.equal(flows.staleNextRun(disabled("2026-11-25T09:00:00"), now, -480), false);
  assert.equal(flows.staleNextRun(FLOW({ enabled: true, next_run: "2026-11-01T09:00:00" }), now, -480), false);
  assert.equal(flows.staleNextRun(disabled(null), now, -480), false);
  assert.equal(flows.staleNextRun(disabled("not a date"), now, -480), false);
});

test("the stored next run has no time zone: the server's offset decides whether 'now' has passed it", () => {
  // The same stored string, 10:00 on the 19th, is 18:00 UTC at UTC-8 (after now = 12:00 UTC) and 02:00 UTC at UTC+8 (before it).
  const now = Date.UTC(2026, 10, 19, 12, 0, 0);
  const f = FLOW({ enabled: false, next_run: "2026-11-19T10:00:00" });
  assert.equal(flows.staleNextRun(f, now, -480), false);
  assert.equal(flows.staleNextRun(f, now, 480), true);
});

test("with no known offset a stored time is called stale only when it is past in every zone", () => {
  const now = Date.UTC(2026, 10, 19, 12, 0, 0);
  assert.equal(flows.staleNextRun(FLOW({ enabled: false, next_run: "2026-11-05T09:00:00" }), now, null), true);
  assert.equal(flows.staleNextRun(FLOW({ enabled: false, next_run: "2026-11-19T11:00:00" }), now, null), false);
});

// ---- validate answers ------------------------------------------------------------------------------

test("flowChecksFromValidate lists the errors at their fields, in the server's order, and the next runs", () => {
  const r = flows.flowChecksFromValidate({
    valid: false, next_runs: [],
    errors: [{ field: "schedule", message: "bad" }, { field: "name", message: "taken" }, { field: "schedule", message: "worse" }],
  });
  assert.deepEqual(r.byField, { schedule: ["bad", "worse"], name: ["taken"] });
  assert.equal(Object.keys(r.byField)[0], "schedule", "focus goes to the first field the server named");
  assert.equal(r.canCreate, false);
  const ok = flows.flowChecksFromValidate({ valid: true, errors: [], next_runs: ["2026-11-19T09:00:00-08:00", "2026-11-20T09:00:00-08:00", "2026-11-21T09:00:00-08:00"] });
  assert.deepEqual([ok.canCreate, ok.nextRuns.length, ok.byField], [true, 3, {}]);
});

test("Create is not allowed on an answer that is missing, malformed or contradicts itself", () => {
  for (const bad of [undefined, null, {}, [], "ok", { valid: "yes", errors: [] }, { valid: true, errors: [{ field: "name", message: "taken" }] }, { valid: false, errors: [] }]) {
    const r = flows.flowChecksFromValidate(bad);
    assert.equal(r.canCreate, false, JSON.stringify(bad));
    assert.ok(Array.isArray(r.nextRuns), JSON.stringify(bad));
  }
});

test("Parse-shape 422: a FastAPI detail list lands at the field named by loc", () => {
  assert.deepEqual(
    flows.fieldErrorsFrom422({ detail: [{ loc: ["body", "name"], msg: "Flow name must not contain '/'", type: "value_error" }] }),
    { byField: { name: ["Flow name must not contain '/'"] }, general: [] },
  );
});

test("Validation errors: the {errors: [{field, message}]} shape lands at its fields too", () => {
  assert.deepEqual(
    flows.fieldErrorsFrom422({ detail: { errors: [{ field: "schedule", message: "bad cron" }, { field: "schedule", message: "again" }, { field: "provider", message: "empty" }] } }),
    { byField: { schedule: ["bad cron", "again"], provider: ["empty"] }, general: [] },
  );
});

test("a 422 that names no field, or has an unexpected shape, is shown as a general message and never throws", () => {
  assert.deepEqual(flows.fieldErrorsFrom422({ detail: "plain text" }), { byField: {}, general: ["plain text"] });
  assert.deepEqual(flows.fieldErrorsFrom422({ detail: [{ msg: "no location" }] }), { byField: {}, general: ["no location"] });
  assert.ok(JSON.stringify(flows.fieldErrorsFrom422({ detail: [{ loc: ["body"], msg: "whole body" }] })).includes("whole body"), "a message for the whole body is not lost");
  for (const bad of [undefined, null, {}, [], "x", { detail: null }, { detail: 5 }, { detail: [] }, { detail: {} }, { detail: { errors: "x" } }, { detail: [null, 3, "s"] }]) {
    const r = flows.fieldErrorsFrom422(bad);
    assert.ok(r && typeof r.byField === "object" && Array.isArray(r.general), JSON.stringify(bad));
  }
});

test("server messages reach the field verbatim: markup and unicode are not altered", () => {
  const msg = "<b>bad</b> \"cron\" é 日本 ‮ ${x}";
  assert.deepEqual(flows.fieldErrorsFrom422({ detail: [{ loc: ["body", "schedule"], msg }] }).byField.schedule, [msg]);
});

// ---- confirmations and writes ----------------------------------------------------------------------

test("Enable starts on Cancel: Enable, Run now and Create and enable start on Cancel and warn that nobody is watching", () => {
  for (const action of ["enable", "run", "createAndEnable"]) {
    const c = flows.confirmSpec(action);
    assert.equal(c.defaultFocus, "cancel", action);
    assert.match(c.body, /launches an agent with nobody watching/, action);
    assert.match(c.body, /runs a local file/, action);
    assert.ok(c.title && c.confirmLabel && c.cancelLabel, action);
  }
});

test("Disable also asks first, with its own words", () => {
  const c = flows.confirmSpec("disable");
  assert.ok(c.title && c.confirmLabel && c.cancelLabel);
  assert.doesNotMatch(c.body, /nobody watching/);
});

test("Run now uses a curl time limit of 180 s; the other confirmations say nothing longer", () => {
  assert.equal(flows.confirmSpec("run").timeoutSeconds, 180);
  for (const action of ["enable", "disable", "createAndEnable"]) assert.ok(flows.confirmSpec(action).timeoutSeconds < 180, action);
});

test("the window offers no delete: an unknown or destructive action has no dialog", () => {
  assert.equal(flows.confirmSpec("enable").defaultFocus, "cancel", "the function exists and knows its own actions");
  for (const action of ["delete", "remove", "approve", "", undefined, "__proto__", "constructor", "toString"]) assert.throws(() => flows.confirmSpec(action), Error, String(action));
});

test("Run now executes: the details say the flow ran", () => {
  const o = flows.writeOutcome("run", { kind: "ok", status: 200, json: { executed: true } });
  assert.equal(o.type, "done");
  assert.match(o.text, /\bran\b/i);
  assert.doesNotMatch(o.text, /skipped|fail/i);
});

test("Run now skipped: the details say it was skipped, why, and do not claim it failed", () => {
  const o = flows.writeOutcome("run", { kind: "ok", status: 200, json: { executed: false } });
  assert.equal(o.type, "done");
  assert.match(o.text, /skipped/i);
  assert.match(o.text, /script said no|still working/i);
  assert.doesNotMatch(o.text, /fail|error/i);
});

test("an answer that is not a plain executed true or false is not reported as either: it reads differently from both", () => {
  const ran = flows.writeOutcome("run", { kind: "ok", status: 200, json: { executed: true } }).text;
  const skipped = flows.writeOutcome("run", { kind: "ok", status: 200, json: { executed: false } }).text;
  assert.notEqual(ran, skipped);
  for (const json of [undefined, null, {}, { executed: "true" }, { executed: "false" }, { executed: 1 }, { executed: 0 }, { executed: null }, []]) {
    const o = flows.writeOutcome("run", { kind: "ok", status: 200, json });
    assert.notEqual(o.text, ran, `${JSON.stringify(json)} was reported as a run`);
    assert.notEqual(o.text, skipped, `${JSON.stringify(json)} was reported as skipped`);
  }
});

test("Enable fails: a 404 keeps the switch where it was and shows the server's message", () => {
  assert.deepEqual(
    flows.writeOutcome("enable", { kind: "error", status: 404, detail: "Flow 'x' not found" }),
    { type: "failed", text: "Flow 'x' not found", keepSwitch: true },
  );
  const d = flows.writeOutcome("disable", { kind: "error", status: 500, detail: "boom" });
  assert.deepEqual([d.type, d.keepSwitch, d.text], ["failed", true, "boom"]);
});

test("no answer from CAO keeps the switch, and an auth refusal is its own outcome", () => {
  for (const action of ["enable", "disable", "run"]) {
    const o = flows.writeOutcome(action, { kind: "unreachable" });
    assert.deepEqual([o.type, o.keepSwitch], ["failed", true], action);
    assert.equal(flows.writeOutcome(action, { kind: "auth", status: 401 }).type, "auth", action);
  }
});

test("enable and disable succeed with a plain 'done'", () => {
  assert.equal(flows.writeOutcome("enable", { kind: "ok", status: 200, json: { success: true } }).type, "done");
  assert.equal(flows.writeOutcome("disable", { kind: "ok", status: 200, json: { success: true } }).type, "done");
});

// ---- the flow routes missing ----------------------------------------------------------------------

test("Validate route missing: a 404 or 405 labels the next runs and Edit, and Create says the checks were skipped", () => {
  for (const status of [404, 405]) {
    const r = flows.routesState(status);
    assert.equal(r.missing, true, String(status));
    assert.equal(r.label, NEEDS_CHANGE);
    assert.equal(r.nextRunsNote, "needs a CAO change: POST /flows/validate");
    assert.equal(r.editDisabled, true);
    assert.equal(r.createLabel, CHECKS_SKIPPED);
  }
});

test("any other answer from the validate route leaves the panel as it is", () => {
  const present = { missing: false, label: null, nextRunsNote: null, editDisabled: false, createLabel: null };
  for (const status of [200, 201, 400, 401, 403, 409, 422, 500, 502, 503, 0, undefined, null, "404"]) {
    assert.deepEqual(flows.routesState(status), present, String(status));
  }
});

test("the Runs list says run history is not recorded and that this needs a CAO change", () => {
  const n = flows.flowRunsNote();
  assert.equal(n.label, NEEDS_CHANGE);
  assert.match(n.text, /not recorded/);
});

// ---- create and edit bodies ----------------------------------------------------------------------

test("Create disabled: POST /flows carries enabled false unless Create and enable is chosen", () => {
  const form = { name: "daily", schedule: "0 9 * * mon-fri", agent_profile: "developer", provider: "claude_code", prompt: "Report." };
  assert.deepEqual(flows.flowCreateBody(form, {}), {
    name: "daily", schedule: "0 9 * * mon-fri", agent_profile: "developer", provider: "claude_code", prompt_template: "Report.", enabled: false,
  });
  assert.equal(flows.flowCreateBody(form, { enable: true }).enabled, true);
  assert.equal(flows.flowCreateBody(form).enabled, false, "no options: disabled");
  for (const enable of ["true", 1, "yes", {}]) assert.equal(flows.flowCreateBody(form, { enable }).enabled, false, `enable=${JSON.stringify(enable)}`);
});

test("the create body names only the fields POST /flows takes, so a draft cannot slip in script or engine", () => {
  const form = { name: "d", schedule: "0 9 * * *", agent_profile: "p", provider: "kiro_cli", prompt: "x", script: "/etc/passwd", engine: "kas", enabled: true, file_path: "/x" };
  assert.deepEqual(Object.keys(flows.flowCreateBody(form, {})).sort(), ["agent_profile", "enabled", "name", "prompt_template", "provider", "schedule"]);
  assert.equal(flows.flowCreateBody(form, {}).enabled, false);
});

test("a prompt typed on the form reaches the body as prompt_template, with [[var]] placeholders untouched", () => {
  const form = { name: "d", schedule: "0 9 * * *", agent_profile: "p", provider: "kiro_cli", prompt: "Review [[repo]] for [[who]]\n```x```" };
  assert.equal(flows.flowCreateBody(form, {}).prompt_template, form.prompt);
});

test("Edit sends only what changed, and nothing when nothing changed", () => {
  const before = { schedule: "0 9 * * *", agent_profile: "developer", provider: "kiro_cli", prompt_template: "A" };
  assert.deepEqual(flows.flowUpdateBody(before, { ...before, schedule: "30 8 * * *" }), { schedule: "30 8 * * *" });
  assert.deepEqual(flows.flowUpdateBody(before, { ...before, prompt: "B" }), { prompt_template: "B" });
  assert.deepEqual(flows.flowUpdateBody(before, { ...before, agent_profile: "reviewer", provider: "codex" }), { agent_profile: "reviewer", provider: "codex" });
  assert.deepEqual(flows.flowUpdateBody(before, before), {});
});

test("an edit cannot rename a flow or set a script: the update body has no name, script or enabled", () => {
  const before = { name: "a", schedule: "0 9 * * *", agent_profile: "p", provider: "kiro_cli", prompt_template: "A" };
  const body = flows.flowUpdateBody(before, { ...before, name: "b", script: "x.sh", enabled: true, file_path: "/x" });
  assert.deepEqual(body, {});
});

// ---- Schedule a workflow --------------------------------------------------------------------------

test("Schedule a workflow: the generated prompt runs the workflow through the CLI with the inputs, quoted for the shell", () => {
  const p = flows.schedulePrompt("pr-health", { mode: "dry_run" });
  const line = p.split("\n").find((l) => l.includes("cao workflow run"));
  const cmd = line.slice(line.indexOf("cao workflow run"));
  assert.deepEqual(shellArgv(cmd, "cao"), ["workflow", "run", "pr-health", "--input", "mode=dry_run"]);
  const nasty = { note: "it's $(echo INJECTED) ; echo INJECTED", path: "/work/my repo" };
  const line2 = flows.schedulePrompt("wf", nasty).split("\n").find((l) => l.includes("cao workflow run"));
  const argv = shellArgv(line2.slice(line2.indexOf("cao workflow run")), "cao");
  assert.deepEqual(argv, ["workflow", "run", "wf", "--input", `note=${nasty.note}`, "--input", `path=${nasty.path}`]);
  assert.ok(!argv.includes("INJECTED"));
});

test("Schedule a workflow with no inputs is just the run command", () => {
  const p = flows.schedulePrompt("pr-health", {});
  assert.match(p, /cao workflow run pr-health(?![\w-])/);
  assert.doesNotMatch(p, /--input/);
});

test("the form warns that an unattended run stops at plan approval unless the plan is approved for these inputs", () => {
  assert.match(flows.scheduleWarning(), /stops at plan approval unless the plan is approved for these inputs/);
});

// ---- review round: hard words in the prompt, and the taken-name message the server sends ---------------

test("a single word that a shell reads as something else survives the generated command, as an input value and as an input name", () => {
  const cwd = globDir();
  const commandOf = (prompt) => {
    const line = prompt.split("\n").find((l) => l.includes("cao workflow run"));
    return line.slice(line.indexOf("cao workflow run"));
  };
  for (const word of HARD_WORDS) {
    const asValue = commandOf(flows.schedulePrompt("pr-health", { k: word, other: "plain" }));
    if (needsQuotes(`k=${word}`) && !word.includes("'")) assert.equal(quotedCount(asValue, `k=${word}`), 1, `value ${word} is quoted in: ${asValue}`);
    assert.deepEqual(shellArgv(asValue, "cao", { cwd }), ["workflow", "run", "pr-health", "--input", `k=${word}`, "--input", "other=plain"], `value ${word}`);
    if (word.includes("=")) continue; // an input name cannot hold =
    const asName = commandOf(flows.schedulePrompt("pr-health", { [word]: "v" }));
    if (needsQuotes(`${word}=v`) && !word.includes("'")) assert.equal(quotedCount(asName, `${word}=v`), 1, `name ${word} is quoted in: ${asName}`);
    assert.deepEqual(shellArgv(asName, "cao", { cwd }), ["workflow", "run", "pr-health", "--input", `${word}=v`], `name ${word}`);
  }
});

const FLOW_SERVICE_PY = new URL("../../../src/cli_agent_orchestrator/services/flow_service.py", import.meta.url);

// The string a module-level constant is set to: one literal, or literals joined by adjacency inside
// parentheses. null when the constant is not there.
function pyConstant(text, name) {
  const m = new RegExp(`^${name}\\s*=\\s*(\\(\\s*)?((?:f?"(?:[^"\\\\\\n]|\\\\.)*"\\s*)+)`, "m").exec(text.replace(/#.*$/gm, ""));
  if (!m) return null;
  return [...m[2].matchAll(/"((?:[^"\\\n]|\\.)*)"/g)].map((x) => x[1].replace(/\\(.)/g, "$1")).join("");
}

test("the reader of Python constants takes one literal, adjacent literals, and says null for a missing name", () => {
  assert.equal(pyConstant('X = "a b"\n', "X"), "a b");
  assert.equal(pyConstant('X = (\n    "a "\n    "b"  # c\n)\n', "X"), "a b");
  assert.equal(pyConstant('Y = "a"\n', "X"), null);
  assert.equal(pyConstant('NAME_TAKEN_MESSAGE = "it\\"s"\n', "NAME_TAKEN_MESSAGE"), 'it"s');
  assert.equal(pyConstant('OTHER_NAME_TAKEN_MESSAGE = "x"\n', "NAME_TAKEN_MESSAGE"), null, "a longer name is not this name");
});

test("the window's FLOW_NAME_TAKEN_MESSAGE is the server's NAME_TAKEN_MESSAGE in flow_service.py", () => {
  if (!existsSync(FLOW_SERVICE_PY)) return console.log("SKIP - flow_service.py not found; taken-name message parity not checked");
  const server = pyConstant(readFileSync(FLOW_SERVICE_PY, "utf8"), "NAME_TAKEN_MESSAGE");
  if (server === null) return console.log("SKIP - flow_service.py has no NAME_TAKEN_MESSAGE (a branch without the flow authoring API); parity not checked");
  assert.equal(typeof flows.FLOW_NAME_TAKEN_MESSAGE, "string", "lib/flows.js does not export FLOW_NAME_TAKEN_MESSAGE");
  assert.equal(flows.FLOW_NAME_TAKEN_MESSAGE, server, "the window would no longer recognise the server's taken-name error");
});

// ---- honesty ---------------------------------------------------------------------------------------

test("flowRoutesMissing, flowRuns and createChecksSkipped are no longer pending once flows.js exists", () => {
  for (const surface of ["flowRoutesMissing", "flowRuns", "createChecksSkipped"]) assert.ok(!Object.hasOwn(HONESTY_PENDING, surface), `${surface} is still listed as pending`);
});

// ===================================================================================================
// The Flows section, flow details and write gating: the pure helpers added with tasks 10.2 to 10.4.
// Cases are derived from specs cao-window-flows, cao-window-sessions (Failure states) and D12-D14.
// Inputs are deep-frozen where it matters, so a function that writes to its argument throws.
// ===================================================================================================

const deepFreeze = (o) => {
  if (o && typeof o === "object" && !Object.isFrozen(o)) { Object.freeze(o); for (const v of Object.values(o)) deepFreeze(v); }
  return o;
};
const SEC = 1000;
const MIN = 60 * SEC;
const AUTH = "CAO requires auth - not supported by this window yet";
// A server-local "next run" as POST /flows/validate gives it: ISO with the server's offset.
const RUN = (day, offset = "-08:00") => `2026-11-${day}T09:00:00${offset}`;

// ---- writeGate: writes are disabled under auth, when the machine is not connected and when CAO is unreachable ----

test("Auth required: any auth view disables the write with the auth message, one view or a list of views", () => {
  const off = { disabled: true, reason: "auth", text: AUTH };
  assert.deepEqual(flows.writeGate({ auth: true }), off);
  assert.deepEqual(flows.writeGate([{}, { auth: true }]), off, "the Workflows view can carry the auth failure while Flows is fine");
  assert.deepEqual(flows.writeGate([{ auth: true }, { unreachable: true }]), off);
});

test("a write is also disabled while CAO is unreachable, and says so", () => {
  const w = flows.writeGate({ unreachable: true });
  assert.deepEqual([w.disabled, w.reason, w.text], [true, "unreachable", "CAO unreachable, retrying"]);
  assert.equal(flows.writeGate([{}, { unreachable: true }]).disabled, true);
});

test("a write is disabled when the CAO machine is not connected, and says so", () => {
  const w = flows.writeGate({}, { connected: false });
  assert.deepEqual([w.disabled, w.reason], [true, "not_connected"]);
  assert.match(w.text, /not connected/);
});

test("the reason is the most serious one: auth, then not connected, then unreachable", () => {
  assert.equal(flows.writeGate({ auth: true, unreachable: true }, { connected: false }).reason, "auth");
  assert.equal(flows.writeGate({ unreachable: true }, { connected: false }).reason, "not_connected");
  assert.equal(flows.writeGate({ unreachable: true }).reason, "unreachable");
});

test("Retry is the way out of 'unreachable' only: it passes then, never under auth or when not connected", () => {
  assert.equal(flows.writeGate({ unreachable: true }, { retry: true }).disabled, false);
  assert.equal(flows.writeGate({ auth: true }, { retry: true }).disabled, true);
  assert.equal(flows.writeGate({}, { retry: true, connected: false }).disabled, true);
});

test("a healthy, empty or missing view leaves the write enabled, with no reason and no text", () => {
  const on = { disabled: false, reason: null, text: null };
  for (const views of [{}, [], undefined, null, [null, undefined], [{}, {}], { auth: false, unreachable: false }]) assert.deepEqual(flows.writeGate(views), on, JSON.stringify(views));
  assert.deepEqual(flows.writeGate(deepFreeze([{ unreachable: false }]), deepFreeze({})), on);
});

// ---- flowRoute -------------------------------------------------------------------------------------

test("flowRoute builds /flows/<name> and the three action routes, nothing else", () => {
  assert.equal(flows.flowRoute("daily"), "/flows/daily");
  for (const action of ["enable", "disable", "run"]) assert.equal(flows.flowRoute("daily", action), `/flows/daily/${action}`);
  for (const name of ["a-b_C9", "__proto__", "x".repeat(64)]) assert.equal(flows.flowRoute(name, "run"), `/flows/${name}/run`, name);
});

test("flowRoute refuses an action it does not know: there is no delete, no approve, no path trick", () => {
  for (const action of ["delete", "remove", "Enable", " enable", "enable ", "enable/", "run?x=1", "../x", "", null, "constructor", "toString", "__proto__", 5]) {
    assert.equal(flows.flowRoute("daily", action), null, JSON.stringify(action));
  }
});

test("flowRoute refuses a name that is not a bare name, so a server-sent name cannot change the path", () => {
  for (const name of ["", "..", "../evil", "a/b", "a\\b", "a b", "a?x=1", "a#b", "a%2fb", "a\n", "é", "日本", "a.b", "/flows/x", undefined, null, 5, {}, ["a"]]) {
    assert.equal(flows.flowRoute(name), null, JSON.stringify(name));
    assert.equal(flows.flowRoute(name, "run"), null, `${JSON.stringify(name)} with an action`);
  }
});

// ---- flowRows and publishedFlows -------------------------------------------------------------------

test("flowRows keeps the fields the section reads, in the server's order", () => {
  const data = deepFreeze([
    { name: "b", schedule: "0 9 * * *", enabled: true, next_run: "2026-11-19T09:00:00", prompt_template: "secret prompt", script: "gate.py", agent_profile: "dev" },
    { name: "a", schedule: "5 * * * *", enabled: false, next_run: null },
  ]);
  assert.deepEqual(flows.flowRows(data), [
    { name: "b", schedule: "0 9 * * *", enabled: true, next_run: "2026-11-19T09:00:00" },
    { name: "a", schedule: "5 * * * *", enabled: false, next_run: null },
  ]);
});

test("flowRows reads a value only when it has the right type: enabled is true only for true", () => {
  const [r] = flows.flowRows([{ name: "x", schedule: 5, enabled: "true", next_run: 5 }]);
  assert.deepEqual(r, { name: "x", schedule: "", enabled: false, next_run: null });
  for (const enabled of [1, "yes", {}, [], null, undefined]) assert.equal(flows.flowRows([{ name: "x", enabled }])[0].enabled, false, JSON.stringify(enabled));
  assert.equal(flows.flowRows([{ name: "x", enabled: true }])[0].enabled, true);
  assert.equal(flows.flowRows([{ name: "x", next_run: "" }])[0].next_run, "", "an empty string is still a string");
});

test("flowRows drops what is not a flow with a bare name, and an answer that is not a list is no rows", () => {
  const rows = flows.flowRows([null, 5, "x", [], { name: "a/b" }, { name: "" }, { name: "../x" }, { schedule: "x" }, { name: 5 }, { name: "Ok" }, { name: "é" }]);
  assert.deepEqual(rows.map((r) => r.name), ["Ok"], "a name keeps its case");
  for (const bad of [undefined, null, {}, "flows", 5, { name: "x" }]) assert.deepEqual(flows.flowRows(bad), [], JSON.stringify(bad));
});

test("All disabled: five rows from GET /flows read 'off' and fold to '5, all off'", () => {
  const data = deepFreeze(Array.from({ length: 5 }, (_, i) => ({ name: `f${i}`, schedule: "0 9 * * *", enabled: false, next_run: "2026-11-01T09:00:00", prompt_template: "p" })));
  const rows = flows.flowRows(data);
  assert.deepEqual(rows.map((r) => flows.flowRowState(r, {}).text), ["off", "off", "off", "off", "off"]);
  assert.equal(flows.flowsSummary(rows), "5, all off");
});

test("publishedFlows shares exactly name, schedule and enabled, and never the prompt or the next run", () => {
  const rows = deepFreeze(flows.flowRows([{ name: "a", schedule: "0 9 * * *", enabled: true, next_run: "2026-11-19T09:00:00", prompt_template: "secret" }]));
  const out = flows.publishedFlows(rows);
  assert.deepEqual(out, [{ name: "a", schedule: "0 9 * * *", enabled: true }]);
  assert.deepEqual(Object.keys(out[0]), ["name", "schedule", "enabled"]);
  assert.notEqual(out[0], rows[0], "a copy: the shared storage never holds the section's own row");
  assert.ok(!JSON.stringify(flows.publishedFlows([{ name: "a", schedule: "s", enabled: true, prompt_template: "secret", script: "x" }])).includes("secret"));
});

test("publishedFlows of nothing, or of something that is not a list, is an empty list", () => {
  assert.deepEqual(flows.publishedFlows([]), []);
  for (const bad of [undefined, null, {}, "x", 5]) assert.deepEqual(flows.publishedFlows(bad), [], JSON.stringify(bad));
});

// ---- the 30 s poll ---------------------------------------------------------------------------------

test("the Flows block refreshes when it is shown and the last fetch is 30 s old: 29.999 s waits, 30 s fetches", () => {
  const base = { shown: true, inFlight: false, lastAt: 1_000_000, stamp: 3, seenStamp: 3 };
  assert.equal(flows.flowsPollDue(base, 1_000_000 + 29_999), false);
  assert.equal(flows.flowsPollDue(base, 1_000_000 + 30_000), true);
  assert.equal(flows.flowsPollDue(base, 1_000_000 + 30_001), true);
  assert.equal(flows.flowsPollDue(base, 1_000_000), false);
});

test("a flow write moves the stamp and the block fetches at once, however fresh the last fetch is", () => {
  const base = { shown: true, inFlight: false, lastAt: 1_000_000, stamp: 4, seenStamp: 3 };
  assert.equal(flows.flowsPollDue(base, 1_000_001), true);
  assert.equal(flows.flowsPollDue({ ...base, stamp: 3 }, 1_000_001), false, "the same stamp is no news");
  assert.equal(flows.flowsPollDue({ ...base, stamp: undefined, seenStamp: undefined }, 1_000_001), false, "no stamp at all is no news");
});

test("the first fetch is due at once when the block is shown, whatever the old time says", () => {
  for (const lastAt of [undefined, null, NaN, Infinity, "10"]) assert.equal(flows.flowsPollDue({ shown: true, inFlight: false, lastAt, stamp: 1, seenStamp: 1 }, 5_000), true, String(lastAt));
});

test("nothing is fetched while the block is not shown, or while a fetch is already out, even when a fetch is overdue or a write moved the stamp", () => {
  const due = { lastAt: 0, stamp: 9, seenStamp: 1 };
  assert.equal(flows.flowsPollDue({ ...due, shown: false, inFlight: false }, 10 ** 9), false);
  assert.equal(flows.flowsPollDue({ ...due, shown: true, inFlight: true }, 10 ** 9), false, "never two at once");
  assert.equal(flows.flowsPollDue({ ...due, shown: true, inFlight: false }, 10 ** 9), true);
  assert.equal(flows.flowsPollDue({ ...due, shown: undefined }, 10 ** 9), false);
  assert.equal(flows.flowsPollDue(undefined, 10 ** 9), false);
  assert.equal(flows.flowsPollDue({}, 10 ** 9), false);
});

// ---- Run now in flight -----------------------------------------------------------------------------

test("a Run now mark outlives the 180 s curl limit of the request, but not by much", () => {
  assert.ok(flows.RUN_NOW_WINDOW_MS > 180_000, "a run that takes the full 180 s must still show as running");
  assert.ok(flows.RUN_NOW_WINDOW_MS <= 300_000, "a window that died mid-run must not leave a flow 'running' for long");
});

test("runningFlows lists the marks younger than the window: the edge is exact", () => {
  const W = flows.RUN_NOW_WINDOW_MS;
  const now = 10 ** 12;
  const map = deepFreeze({ fresh: now, edgeIn: now - (W - 1), edgeOut: now - W, old: now - W - 1, wayOld: 0 });
  assert.deepEqual(flows.runningFlows(map, now), ["fresh", "edgeIn"]);
});

test("a mark that cannot be read as a time, or that lies in the future, does not count", () => {
  const now = 10 ** 12;
  const map = deepFreeze({ nan: NaN, str: String(now), nul: null, inf: Infinity, obj: {}, future: now + 1, ok: now - 1 });
  assert.deepEqual(flows.runningFlows(map, now), ["ok"]);
});

test("runningFlows of a missing or malformed map is nothing", () => {
  for (const bad of [undefined, null, [], [["a", 1]], [9], "a", 5, true]) assert.deepEqual(flows.runningFlows(bad, 10), [], JSON.stringify(bad));
});

test("markRunning adds the flow at the given time, keeps live marks and drops expired ones, and leaves its argument alone", () => {
  const W = flows.RUN_NOW_WINDOW_MS;
  const now = 10 ** 12;
  const before = deepFreeze({ live: now - 1000, dead: now - W - 1 });
  assert.deepEqual(flows.markRunning(before, "daily", now), { live: now - 1000, daily: now });
  assert.deepEqual(flows.markRunning(undefined, "daily", now), { daily: now });
  assert.deepEqual(flows.markRunning(null, "daily", now), { daily: now });
  assert.deepEqual(flows.markRunning(deepFreeze({ daily: now - 5000 }), "daily", now), { daily: now }, "a second Run now restarts the clock");
});

test("clearRunning removes only the named flow, drops expired marks, and clearing a flow that is not marked changes nothing live", () => {
  const W = flows.RUN_NOW_WINDOW_MS;
  const now = 10 ** 12;
  const before = deepFreeze({ a: now - 10, b: now - 20, dead: now - W });
  assert.deepEqual(flows.clearRunning(before, "a", now), { b: now - 20 });
  assert.deepEqual(flows.clearRunning(before, "zzz", now), { a: now - 10, b: now - 20 });
  assert.deepEqual(flows.clearRunning(undefined, "a", now), {});
  assert.deepEqual(flows.clearRunning([], "a", now), {});
});

test("Run now lifecycle: marked, shown as running, cleared, no longer running; two flows do not disturb each other", () => {
  const now = 10 ** 12;
  let map = flows.markRunning({}, "one", now);
  map = flows.markRunning(map, "two", now + 100);
  assert.deepEqual(flows.runningFlows(map, now + 200), ["one", "two"]);
  map = flows.clearRunning(map, "one", now + 300);
  assert.deepEqual(flows.runningFlows(map, now + 400), ["two"]);
  assert.deepEqual(flows.runningFlows(map, now + 100 + flows.RUN_NOW_WINDOW_MS), [], "a mark left by a dead window expires on its own");
});

test("the marks survive shared storage, which holds JSON, and a flow may be named like an Object method", () => {
  const now = 10 ** 12;
  for (const name of ["__proto__", "constructor", "toString", "hasOwnProperty", "a-b_9"]) {
    const stored = JSON.parse(JSON.stringify(flows.markRunning({}, name, now)));
    assert.deepEqual(flows.runningFlows(stored, now + 1), [name], name);
    const again = flows.markRunning(stored, "other", now + 2);
    assert.deepEqual(flows.runningFlows(again, now + 3).sort(), [name, "other"].sort(), name);
    assert.deepEqual(flows.runningFlows(flows.clearRunning(again, name, now + 4), now + 5), ["other"], name);
    assert.equal(Object.getPrototypeOf(again), Object.prototype, "the prototype is untouched");
  }
});

// ---- running from the session ----------------------------------------------------------------------

const SESSION = (name, ...states) => ({ name, terms: states.map((state) => ({ state })) });

test("Running from the session: the first terminal of cao-flow-<name> working is running", () => {
  assert.equal(flows.flowSessionWorking(deepFreeze([SESSION("cao-flow-daily", "working")]), "daily"), true);
  assert.equal(flows.flowSessionWorking([SESSION("other", "idle"), SESSION("cao-flow-daily", "working", "idle")], "daily"), true);
});

test("only the first terminal counts: a working second terminal does not make the flow running", () => {
  assert.equal(flows.flowSessionWorking([SESSION("cao-flow-daily", "idle", "working")], "daily"), false);
  assert.equal(flows.flowSessionWorking([SESSION("cao-flow-daily", "done", "working", "working")], "daily"), false);
});

test("a first terminal in any other state is not running", () => {
  for (const state of ["idle", "done", "blocked", "error", "starting", "quiet", "", undefined, null, "Working"]) {
    assert.equal(flows.flowSessionWorking([SESSION("cao-flow-daily", state)], "daily"), false, String(state));
  }
});

test("the session must be exactly cao-flow-<name>: neighbours with a shared prefix or suffix do not match", () => {
  const all = [SESSION("cao-flow-daily-2", "working"), SESSION("cao-flow-dail", "working"), SESSION("xcao-flow-daily", "working"), SESSION("cao-flow-DAILY", "working"), SESSION("daily", "working"), SESSION("cao-daily", "working")];
  assert.equal(flows.flowSessionWorking(all, "daily"), false);
  assert.equal(flows.flowSessionWorking(all, "daily-2"), true);
  assert.equal(flows.flowSessionWorking(all, "dail"), true);
});

test("no session, no terminals or no list: not running", () => {
  assert.equal(flows.flowSessionWorking([], "daily"), false);
  assert.equal(flows.flowSessionWorking([{ name: "cao-flow-daily" }], "daily"), false);
  assert.equal(flows.flowSessionWorking([{ name: "cao-flow-daily", terms: [] }], "daily"), false);
  for (const bad of [undefined, null, {}, "cao-flow-daily", 5]) assert.equal(flows.flowSessionWorking(bad, "daily"), false, JSON.stringify(bad));
});

test("the session the window builds from CAO terminals is read the same way", () => {
  const terminals = [
    { session: "cao-flow-nightly", id: "t1", state: "working" },
    { session: "cao-flow-nightly", id: "t2", state: "idle" },
    { session: "cao-flow-idle-one", id: "t3", state: "idle" },
    { session: "cao-flow-idle-one", id: "t4", state: "working" },
  ];
  const sessions = sessionsFrom(terminals);
  assert.equal(flows.flowSessionWorking(sessions, "nightly"), true);
  assert.equal(flows.flowSessionWorking(sessions, "idle-one"), false);
  assert.equal(flows.flowRowState({ enabled: false }, { sessionWorking: flows.flowSessionWorking(sessions, "nightly") }).text, "running", "even a disabled flow is running while its session works");
});

// ---- viewNotes: auth, unreachable, error and the age of what is on screen ------------------------------

test("Auth required: the Flows view says CAO requires auth", () => {
  assert.deepEqual(flows.viewNotes({ auth: true }, 0), [AUTH]);
});

test("a healthy view, or none at all, has no notes, however old its data is", () => {
  assert.deepEqual(flows.viewNotes(undefined, 5), []);
  assert.deepEqual(flows.viewNotes(null, 5), []);
  assert.deepEqual(flows.viewNotes({}, 5), []);
  assert.deepEqual(flows.viewNotes({ data: [{ name: "a" }], at: 0 }, 10 * MIN), [], "the age is told only next to a failure");
});

test("Stale data: rows from 10:00 that fail to refresh at 10:05 stay, with a note that they are 5 minutes old", () => {
  const t0 = Date.UTC(2026, 10, 19, 10, 0, 0);
  const notes = flows.viewNotes(deepFreeze({ data: [{ name: "a" }], at: t0, unreachable: true }), t0 + 5 * MIN);
  assert.deepEqual(notes, ["CAO unreachable, retrying", "Showing the last list, 5 minutes old"]);
  assert.deepEqual(flows.viewNotes({ data: [], at: t0, auth: true }, t0 + 5 * MIN), [AUTH, "Showing the last list, 5 minutes old"]);
});

test("the age is said in words with the right edges: just now under 5 s, then seconds, minutes, hours", () => {
  const age = (ms) => flows.viewNotes({ data: [1], at: 1000, unreachable: true }, 1000 + ms)[1];
  assert.equal(age(0), "Showing the last list, just now");
  assert.equal(age(4_999), "Showing the last list, just now");
  assert.equal(age(5_000), "Showing the last list, 5 seconds old");
  assert.equal(age(59_000), "Showing the last list, 59 seconds old");
  assert.equal(age(60_000), "Showing the last list, 1 minute old");
  assert.equal(age(2 * MIN), "Showing the last list, 2 minutes old");
  assert.equal(age(60 * MIN), "Showing the last list, 1 hour old");
});

test("no age is told when there is no data on screen, or no time to measure from", () => {
  assert.deepEqual(flows.viewNotes({ unreachable: true, at: 0 }, MIN), ["CAO unreachable, retrying"]);
  assert.deepEqual(flows.viewNotes({ unreachable: true, data: null, at: 0 }, MIN), ["CAO unreachable, retrying"]);
  assert.deepEqual(flows.viewNotes({ unreachable: true, data: [] }, MIN), ["CAO unreachable, retrying"]);
  assert.equal(flows.viewNotes({ unreachable: true, data: [], at: 0 }, MIN).length, 2, "an empty last list is still a last list, and 0 is a time");
});

test("an error from CAO is shown with its text, cut at 200 characters, after the auth and unreachable lines", () => {
  assert.deepEqual(flows.viewNotes({ error: "boom" }, 0), ["CAO answered with an error: boom"]);
  const long = flows.viewNotes({ error: "x".repeat(500) }, 0)[0];
  assert.match(long, /^CAO answered with an error: x{200}$/);
  assert.deepEqual(flows.viewNotes({ error: "" }, 0), []);
  assert.deepEqual(flows.viewNotes({ auth: true, unreachable: true, error: "e", data: [], at: 0 }, MIN), [AUTH, "CAO unreachable, retrying", "CAO answered with an error: e", "Showing the last list, 1 minute old"]);
});

test("an auth or unreachable flag counts only when it is true", () => {
  assert.deepEqual(flows.viewNotes({ auth: "yes", unreachable: 1 }, 0), []);
});

// ---- runWords and offsetMinutesOf --------------------------------------------------------------------

test("runWords says the weekday and the server's wall clock: the offset is not applied, so the day cannot move", () => {
  assert.equal(flows.runWords("2026-11-23T09:00:00-08:00"), "Mon 2026-11-23 09:00");
  assert.equal(flows.runWords("2026-11-21T23:30:00-08:00"), "Sat 2026-11-21 23:30", "23:30 at UTC-8 is already Sunday in UTC");
  assert.equal(flows.runWords("2026-11-22T00:15:00+05:30"), "Sun 2026-11-22 00:15", "00:15 at +05:30 is still Saturday in UTC");
  assert.equal(flows.runWords("2026-11-23T09:00:00Z"), "Mon 2026-11-23 09:00");
  assert.equal(flows.runWords("2026-11-23 09:00:59.987"), "Mon 2026-11-23 09:00", "a space for T, no offset, seconds dropped");
});

test("runWords does not depend on the time zone of the host", () => {
  const was = process.env.TZ;
  try {
    for (const tz of ["UTC", "America/Los_Angeles", "Pacific/Auckland", "Asia/Kolkata", "Pacific/Kiritimati"]) {
      process.env.TZ = tz;
      assert.equal(flows.runWords("2026-11-21T09:00:00-08:00"), "Sat 2026-11-21 09:00", tz);
      assert.equal(flows.runWords("2026-11-22T00:15:00+05:30"), "Sun 2026-11-22 00:15", tz);
      assert.equal(flows.runWords("2026-11-22T23:59:00Z"), "Sun 2026-11-22 23:59", tz);
    }
  } finally {
    if (was === undefined) delete process.env.TZ; else process.env.TZ = was;
  }
});

test("runWords names every weekday: a week of server times reads Sun to Sat", () => {
  const days = ["22", "23", "24", "25", "26", "27", "28"].map((d) => flows.runWords(`2026-11-${d}T09:00:00-08:00`).slice(0, 3));
  assert.deepEqual(days, ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]);
});

test("Numeric day of week: the next runs of 0 9 * * 1-5 on a Wednesday read Thu, Fri and Sat", () => {
  const runs = ["19", "20", "21"].map((d) => RUN(d));
  assert.deepEqual(runs.map(flows.runWords), ["Thu 2026-11-19 09:00", "Fri 2026-11-20 09:00", "Sat 2026-11-21 09:00"]);
});

test("runWords handles month and year ends and a leap day", () => {
  assert.equal(flows.runWords("2026-12-31T23:59:00Z"), "Thu 2026-12-31 23:59");
  assert.equal(flows.runWords("2027-01-01T00:00:00Z"), "Fri 2027-01-01 00:00");
  assert.equal(flows.runWords("2028-02-29T12:00:00Z"), "Tue 2028-02-29 12:00");
});

test("runWords gives a time it cannot read back as it came, and nothing for what is not text", () => {
  for (const text of ["not a date", "tomorrow 9am", "2026-13-45T99:99:00Z", "2026-13-01T09:00:00Z", "2026-11-23", "09:00"]) assert.equal(flows.runWords(text), text, text);
  for (const bad of [undefined, null, 5, {}, []]) assert.equal(flows.runWords(bad), "", String(bad));
  assert.equal(flows.runWords(""), "");
});

test("offsetMinutesOf reads the offset east of UTC: -08:00 is -480, +05:30 is 330, Z is 0", () => {
  assert.equal(flows.offsetMinutesOf("2026-11-19T09:00:00-08:00"), -480);
  assert.equal(flows.offsetMinutesOf("2026-11-19T09:00:00+05:30"), 330);
  assert.equal(flows.offsetMinutesOf("2026-11-19T09:00:00Z"), 0);
  assert.equal(flows.offsetMinutesOf("2026-11-19T09:00:00+00:00"), 0);
  assert.equal(flows.offsetMinutesOf("2026-11-19T09:00:00+14:00"), 840);
  assert.equal(flows.offsetMinutesOf("2026-11-19T09:00:00-12:00"), -720);
  assert.equal(flows.offsetMinutesOf("2026-11-19T09:00:00+23:59"), 1439);
});

test("offsetMinutesOf reads the other spellings of a server time: no colon, a fraction, no seconds, a space", () => {
  assert.equal(flows.offsetMinutesOf("2026-11-19T09:00:00-0800"), -480);
  assert.equal(flows.offsetMinutesOf("2026-11-19T09:00:00.123456-08:00"), -480);
  assert.equal(flows.offsetMinutesOf("2026-11-19T09:00-08:00"), -480);
  assert.equal(flows.offsetMinutesOf("2026-11-19 09:00:00+05:30"), 330);
});

test("offsetMinutesOf is null for a time with no offset, an offset that is not one, and anything that is not text", () => {
  for (const text of ["2026-11-19T09:00:00", "2026-11-19", "", "garbage", "2026-11-19T09:00:00+24:00", "2026-11-19T09:00:00+05:60", "2026-11-19T09:00:00-08:00 extra", "2026-11-19T09:00:00-8", "-08:00"]) {
    assert.equal(flows.offsetMinutesOf(text), null, JSON.stringify(text));
  }
  for (const bad of [undefined, null, 5, {}, []]) assert.equal(flows.offsetMinutesOf(bad), null, String(bad));
});

// ---- validateResult ----------------------------------------------------------------------------------

test("validateResult of an answer carries the HTTP status and the checks of the body", () => {
  const body = { valid: false, errors: [{ field: "schedule", message: "bad" }], next_runs: [] };
  const r = flows.validateResult(deepFreeze({ kind: "ok", status: 200, json: body }));
  assert.equal(r.status, 200);
  assert.deepEqual(r.checks, flows.flowChecksFromValidate(body));
  assert.deepEqual([r.checks.byField, r.checks.canCreate], [{ schedule: ["bad"] }, false]);
});

test("Validate route missing: a 404 or 405 reads as the missing-route state, with no checks", () => {
  for (const status of [404, 405]) {
    const r = flows.validateResult({ kind: "error", status, detail: "Not Found", json: { detail: "Not Found" } });
    assert.deepEqual(r, { status, checks: null });
    assert.equal(flows.routesState(r.status).missing, true, String(status));
  }
});

test("a server error from validate keeps its status and has no checks, so Create is not allowed on it", () => {
  assert.deepEqual(flows.validateResult({ kind: "error", status: 500, detail: "boom" }), { status: 500, checks: null });
  assert.equal(flows.routesState(flows.validateResult({ kind: "error", status: 500 }).status).missing, false);
});

test("no answer, an auth refusal or an approval refusal have no status and no checks", () => {
  for (const o of [{ kind: "unreachable" }, { kind: "auth", status: 401, message: AUTH }, { kind: "approval_required", status: 403, planId: "wf:1" }, undefined, null, {}, { kind: "other", status: 200 }]) {
    assert.deepEqual(flows.validateResult(o), { status: null, checks: null }, JSON.stringify(o));
  }
});

test("an answer whose body is not a validate answer gives checks that refuse Create, and does not throw", () => {
  for (const json of [undefined, null, [], "ok", {}, { valid: true }]) {
    const r = flows.validateResult({ kind: "ok", status: 200, json });
    assert.equal(r.status, 200);
    assert.equal(r.checks.canCreate, false, JSON.stringify(json));
  }
});

test("Edit passes its own name to the checks: the 'name taken' error of the flow itself is dropped, any other name's is kept", () => {
  const json = { valid: false, errors: [{ field: "name", message: "a flow with this name already exists" }], next_runs: [RUN("19")] };
  const own = flows.validateResult({ kind: "ok", status: 200, json }, { editing: "daily", name: "daily" });
  assert.deepEqual([own.checks.canCreate, own.checks.byField], [true, {}]);
  const other = flows.validateResult({ kind: "ok", status: 200, json }, { editing: "daily", name: "weekly" });
  assert.deepEqual([other.checks.canCreate, other.checks.byField], [false, { name: ["a flow with this name already exists"] }]);
  const create = flows.validateResult({ kind: "ok", status: 200, json });
  assert.equal(create.checks.canCreate, false, "a new flow with a taken name is refused");
});

// ---- flowDetailsView ---------------------------------------------------------------------------------

const NOW = Date.UTC(2026, 10, 19, 12, 0, 0); // 04:00 at UTC-8
const DAILY = (over = {}) => ({
  name: "daily", enabled: true, schedule: "0 9 * * mon-fri", next_run: "2026-11-19T09:00:00", last_run: "2026-11-18T09:00:03",
  agent_profile: "developer", provider: "claude_code", prompt_template: "Report on [[repo]].", ...over,
});
const THREE = [RUN("19"), RUN("20"), RUN("23")];
const view = (over = {}) => flows.flowDetailsView(deepFreeze({ flow: DAILY(), nextRuns: THREE, validateStatus: 200, now: NOW, ...over }));

test("an enabled flow shows its next three runs in words, from the validate answer", () => {
  const v = view();
  assert.equal(v.next.heading, "Next 3 runs");
  assert.deepEqual(v.next.lines, ["Thu 2026-11-19 09:00", "Fri 2026-11-20 09:00", "Mon 2026-11-23 09:00"]);
  assert.deepEqual([v.next.none, v.next.stored, v.next.ifEnabled, v.next.note, v.next.label], [null, null, [], null, null]);
  assert.deepEqual([v.enabled, v.enabledText], [true, "Enabled"]);
});

test("an enabled flow whose validate answer holds no runs says so, instead of showing nothing", () => {
  for (const nextRuns of [[], undefined, null, "x", {}]) {
    const v = view({ nextRuns });
    assert.deepEqual(v.next.lines, [], JSON.stringify(nextRuns));
    assert.equal(typeof v.next.none, "string");
    assert.ok(v.next.none.length > 0);
  }
});

test("Disabled flow with a stale next run: 'None. The schedule is disabled', the stored time marked stale, and the three runs enabling would give", () => {
  const v = view({ flow: DAILY({ enabled: false, next_run: "2026-11-01T09:00:00" }) });
  assert.match(v.next.none, /^None\. The schedule is disabled/);
  assert.deepEqual(v.next.stored, { text: "2026-11-01 09:00", stale: true });
  assert.deepEqual(v.next.ifEnabled, ["Thu 2026-11-19 09:00", "Fri 2026-11-20 09:00", "Mon 2026-11-23 09:00"]);
  assert.deepEqual(v.next.lines, []);
  assert.deepEqual([v.enabled, v.enabledText], [false, "Disabled"]);
});

test("a disabled flow's stored next run is stale only when it is past, judged in the server's zone taken from the validate answer", () => {
  const stale = (next_run, nextRuns) => view({ flow: DAILY({ enabled: false, next_run }), nextRuns }).next.stored;
  // 2026-11-19T10:00 is 18:00 UTC at UTC-8 (after now = 12:00 UTC) and 02:00 UTC at UTC+8 (before it).
  assert.equal(stale("2026-11-19T10:00:00", [RUN("20", "-08:00")]).stale, false);
  assert.equal(stale("2026-11-19T10:00:00", [RUN("20", "+08:00")]).stale, true);
  assert.equal(stale("2026-11-19T10:00:00", []).stale, false, "with no zone known a time is past only when it is past in every zone");
  assert.equal(stale("2026-11-19T04:00:00", [RUN("20", "-08:00")]).stale, false, "exactly now is not past");
  assert.equal(stale("2026-11-19T03:59:00", [RUN("20", "-08:00")]).stale, true);
});

test("a disabled flow with no stored next run shows no stored time", () => {
  for (const next_run of [null, undefined, "", "garbage"]) assert.equal(view({ flow: DAILY({ enabled: false, next_run }) }).next.stored, null, JSON.stringify(next_run));
});

test("Validate route missing: the next-runs area is labelled and shows only the stored next run, for an enabled and a disabled flow", () => {
  for (const validateStatus of [404, 405]) {
    for (const enabled of [true, false]) {
      const v = view({ flow: DAILY({ enabled }), validateStatus, nextRuns: undefined });
      assert.equal(v.next.note, "needs a CAO change: POST /flows/validate", `${validateStatus} ${enabled}`);
      assert.equal(v.next.label, "needs a CAO change");
      assert.deepEqual(v.next.lines, []);
      assert.deepEqual(v.next.ifEnabled, []);
      assert.equal(v.next.stored.text, "2026-11-19 09:00");
      if (enabled) assert.equal(v.next.none, null, "an enabled flow is not called disabled");
      else assert.match(v.next.none, /^None\. The schedule is disabled/, "a disabled flow still says so");
    }
  }
});

test("Disabled flow with a stale next run, on a server without validate: 'None. The schedule is disabled', the stored time marked stale, the label, and no runs", () => {
  for (const validateStatus of [404, 405]) {
    for (const nextRuns of [undefined, [], THREE]) {
      const v = view({ flow: DAILY({ enabled: false, next_run: "2026-11-01T09:00:00" }), validateStatus, nextRuns });
      const at = `${validateStatus} ${JSON.stringify(nextRuns)}`;
      assert.match(v.next.none, /^None\. The schedule is disabled/, at);
      assert.deepEqual(v.next.stored, { text: "2026-11-01 09:00", stale: true }, at);
      assert.equal(v.next.note, "needs a CAO change: POST /flows/validate", at);
      assert.equal(v.next.label, "needs a CAO change", at);
      assert.deepEqual([v.next.lines, v.next.ifEnabled], [[], []], `${at}: no runs are invented, and none passed in are shown`);
      assert.deepEqual([v.enabled, v.enabledText, v.editDisabled, v.editLabel], [false, "Disabled", true, "needs a CAO change"], at);
    }
  }
});

test("on a server without validate a disabled flow's stored time is stale only when past in every zone: the edge is exact", () => {
  // now is 2026-11-19 12:00 UTC; with no zone known a time counts as past only 12 h after it, as at UTC-12.
  const stored = (next_run) => view({ flow: DAILY({ enabled: false, next_run }), validateStatus: 404, nextRuns: undefined }).next.stored;
  assert.deepEqual(stored("2026-11-01T09:00:00"), { text: "2026-11-01 09:00", stale: true });
  assert.deepEqual(stored("2026-11-18T23:59:00"), { text: "2026-11-18 23:59", stale: true });
  assert.deepEqual(stored("2026-11-19T00:00:00"), { text: "2026-11-19 00:00", stale: false }, "exactly 12 h before now is not yet past in every zone");
  assert.deepEqual(stored("2026-11-25T09:00:00"), { text: "2026-11-25 09:00", stale: false });
});

test("a disabled flow with a future stored next run is not marked stale, and one with none shows none, on a server without validate", () => {
  for (const validateStatus of [404, 405]) {
    const future = view({ flow: DAILY({ enabled: false, next_run: "2026-12-25T09:00:00" }), validateStatus });
    assert.deepEqual(future.next.stored, { text: "2026-12-25 09:00", stale: false });
    assert.match(future.next.none, /^None\. The schedule is disabled/);
    for (const next_run of [null, undefined, "", "garbage"]) {
      const v = view({ flow: DAILY({ enabled: false, next_run }), validateStatus });
      assert.equal(v.next.stored, null, JSON.stringify(next_run));
      assert.match(v.next.none, /^None\. The schedule is disabled/, JSON.stringify(next_run));
      assert.deepEqual([v.next.lines, v.next.ifEnabled], [[], []]);
    }
  }
});

test("on a server without validate an enabled flow shows only its stored time, never marked stale, whatever it says", () => {
  for (const validateStatus of [404, 405]) {
    const v = view({ flow: DAILY({ enabled: true, next_run: "2026-11-01T09:00:00" }), validateStatus, nextRuns: undefined });
    assert.deepEqual(v.next.stored, { text: "2026-11-01 09:00", stale: false });
    assert.equal(v.next.none, null);
    assert.equal(v.next.heading, "Next run");
  }
});

test("on a server without validate a flow whose enabled flag is not true reads as disabled all through", () => {
  for (const enabled of ["true", 1, "yes", null, undefined]) {
    const v = view({ flow: DAILY({ enabled, next_run: "2026-11-01T09:00:00" }), validateStatus: 404 });
    assert.match(v.next.none, /^None\. The schedule is disabled/, JSON.stringify(enabled));
    assert.equal(v.enabled, false, JSON.stringify(enabled));
  }
});

test("a server that has validate keeps its own states: the missing-route label and heading do not leak into a normal answer", () => {
  const off = view({ flow: DAILY({ enabled: false, next_run: "2026-11-01T09:00:00" }), validateStatus: 200 });
  assert.deepEqual([off.next.heading, off.next.note, off.next.label], ["Next 3 runs", null, null]);
  assert.equal(off.next.ifEnabled.length, 3);
});

test("without the validate route no run is shown even if some were passed: the area shows only what the route could not have given", () => {
  const v = view({ validateStatus: 404, nextRuns: THREE });
  assert.deepEqual([v.next.lines, v.next.ifEnabled], [[], []]);
  assert.equal(view({ validateStatus: 404, flow: DAILY({ next_run: null }), nextRuns: [] }).next.stored, null);
});

test("Edit is disabled with the label on an older server, and enabled otherwise", () => {
  for (const validateStatus of [404, 405]) assert.deepEqual([view({ validateStatus }).editDisabled, view({ validateStatus }).editLabel], [true, "needs a CAO change"]);
  for (const validateStatus of [200, 500, undefined, null, 0]) {
    const v = view({ validateStatus });
    assert.deepEqual([v.editDisabled, v.editLabel, v.next.label, v.next.note], [false, null, null, null], String(validateStatus));
  }
});

test("the schedule is shown in words and as the cron, in the server's local time, noting flows fire only while cao-server runs", () => {
  const v = view();
  assert.match(v.schedule.words, /weekdays at 09:00/);
  assert.equal(v.schedule.cron, "0 9 * * mon-fri");
  assert.match(v.schedule.note, /server's local time/);
  assert.match(v.schedule.note, /only while cao-server is running/);
});

test("a numeric day-of-week schedule has no words, only its raw cron", () => {
  const v = view({ flow: DAILY({ schedule: "0 9 * * 1-5" }) });
  assert.deepEqual([v.schedule.words, v.schedule.cron], [null, "0 9 * * 1-5"]);
  assert.equal(view({ flow: DAILY({ schedule: undefined }) }).schedule.cron, "");
});

test("last_run is shown as its time with a note that CAO records only that time, and 'never' when there is none", () => {
  const v = view();
  assert.deepEqual(v.lastRun, { text: "2026-11-18 09:00", note: "CAO records only this time." });
  for (const last_run of [null, undefined]) assert.equal(view({ flow: DAILY({ last_run }) }).lastRun.text, "never", String(last_run));
});

test("last_run: a missing or empty value reads 'never', a value that does not read as a time reads 'unknown', a valid one reads as its time", () => {
  const text = (last_run) => view({ flow: DAILY({ last_run }) }).lastRun.text;
  for (const last_run of [null, undefined, ""]) assert.equal(text(last_run), "never", JSON.stringify(last_run));
  for (const last_run of ["yesterday", "garbage", "2026-13-45T99:99:00Z", "09:00", "2026-11-18", 0, 5, {}, []]) assert.equal(text(last_run), "unknown", JSON.stringify(last_run));
  assert.equal(text("2026-11-18T09:00:03"), "2026-11-18 09:00");
  assert.equal(text("2026-11-18T23:59:59-08:00"), "2026-11-18 23:59");
  assert.equal(text("2026-11-18 07:30:00Z"), "2026-11-18 07:30");
  const v = view({ flow: DAILY({ last_run: "yesterday" }) });
  assert.equal(v.lastRun.note, "CAO records only this time.", "the note stays with an unreadable time too");
});

test("'never' is said only when there is no last_run: an unreadable one is never reported as 'never'", () => {
  assert.notEqual(view({ flow: DAILY({ last_run: "whenever" }) }).lastRun.text, "never");
  assert.notEqual(view({ flow: DAILY({ last_run: "whenever" }) }).lastRun.text, view({ flow: DAILY({ last_run: null }) }).lastRun.text);
});

test("the target shows profile, provider and the script only when one is set; the prompt is not in it", () => {
  assert.deepEqual(view().target, { profile: "developer", provider: "claude_code", script: null });
  assert.equal(view({ flow: DAILY({ script: "gate.py" }) }).target.script, "gate.py");
  for (const script of ["", null, undefined, 5, {}]) assert.equal(view({ flow: DAILY({ script }) }).target.script, null, JSON.stringify(script));
});

test("the prompt is in the details only as promptView says: as the server has it when open, nothing for a flow without one", () => {
  const odd = "Zeile 1\n  \"quoted\" <b>x</b> \u00e9 \u65e5\u672c ${x}";
  assert.equal(view({ flow: DAILY({ prompt_template: odd }), promptOpen: true }).prompt.text, odd);
  assert.equal(view({ flow: DAILY({ prompt_template: "" }), promptOpen: true }).prompt.text, null);
  assert.equal(view({ flow: DAILY({ prompt_template: null }), promptOpen: true }).prompt.note, "not available");
});

test("Prompt collapsed: the details hold no prompt text unless promptOpen is true", () => {
  const secret = "SECRET-PROMPT-LINE-7";
  const flow = DAILY({ prompt_template: `first line\n${secret}` });
  for (const promptOpen of [undefined, false, null, "yes", 1, {}]) {
    assert.ok(!JSON.stringify(view({ flow, promptOpen })).includes(secret), `promptOpen ${JSON.stringify(promptOpen)} leaked the prompt`);
  }
  const open = view({ flow, promptOpen: true });
  assert.ok(JSON.stringify(open).includes(secret), "open shows it");
  assert.equal(JSON.stringify(open).split(secret).length - 1, 1, "once, in the prompt view");
});

test("Runs holds only the live cao-flow-<name> session, with the note that history needs a CAO change", () => {
  const v = view();
  assert.equal(v.session, "cao-flow-daily");
  assert.deepEqual(v.runsNote, flows.flowRunsNote());
  assert.equal(v.runsNote.label, "needs a CAO change");
  assert.equal(view({ flow: DAILY({ name: "a-b_9" }) }).session, "cao-flow-a-b_9");
});

test("the enabled switch reads true only for true", () => {
  for (const enabled of ["true", 1, "yes", null, undefined]) {
    const v = view({ flow: DAILY({ enabled }) });
    assert.equal(v.enabled, false, JSON.stringify(enabled));
    assert.match(v.next.none, /disabled/, `${JSON.stringify(enabled)} is read as a disabled flow all through`);
  }
});

test("on an older server the enabled switch still shows the flow's state", () => {
  for (const enabled of [true, false]) assert.equal(view({ flow: DAILY({ enabled }), validateStatus: 404 }).enabled, enabled);
});

test("flowDetailsView of nothing at all does not throw and has empty text, not 'undefined'", () => {
  for (const args of [undefined, {}, { flow: null }, { flow: {}, nextRuns: [null, 5, {}] }]) {
    const v = flows.flowDetailsView(args);
    assert.equal(v.name, "");
    assert.ok(!JSON.stringify(v).includes("undefined"), JSON.stringify(v));
    assert.equal(v.session, "cao-flow-");
  }
});

test("a validate answer with run times the window cannot read shows them as they came", () => {
  assert.deepEqual(view({ nextRuns: ["soon", 5, null] }).next.lines, ["soon", "", ""]);
});

// ===================================================================================================
// Round 3: the confirm dialogs of the flow writes, and the gate of the panel's write controls.
// Specs: cao-window-flows "Enable, disable and run now are confirmed" (Enable starts on Cancel, Run now),
// cao-window-sessions "Failure states" (auth is judged per surface), design D14.
// ===================================================================================================

const WRITES = ["enable", "disable", "run", "createAndEnable"];

test("every flow dialog is confirmable: confirmEnabled is exactly true for each action", () => {
  for (const action of WRITES) {
    assert.strictEqual(flows.confirmSpec(action).confirmEnabled, true, action);
    assert.strictEqual(flows.flowDialogSpec(action, "daily").confirmEnabled, true, `${action} with a flow name`);
  }
});

test("each confirmSpec call is its own object: changing one dialog does not change the next", () => {
  const a = flows.confirmSpec("enable");
  a.confirmEnabled = false;
  a.defaultFocus = "confirm";
  const b = flows.confirmSpec("enable");
  assert.deepEqual([b.confirmEnabled, b.defaultFocus], [true, "cancel"]);
});

test("Enable starts on Cancel: Enter without moving focus closes the dialog, for Enable, Run now and Create and enable", () => {
  for (const action of ["enable", "run", "createAndEnable"]) {
    for (const spec of [flows.confirmSpec(action), flows.flowDialogSpec(action, "daily")]) {
      assert.equal(spec.defaultFocus, "cancel", action);
      for (const key of ["Enter", "Return", "enter"]) assert.equal(dialogKey({ focus: spec.defaultFocus }, spec, key).action, "cancel", `${action} ${key}: nothing is sent`);
    }
  }
});

test("Create and enable can be confirmed from the keyboard: Tab to Confirm, then Enter confirms", () => {
  const spec = flows.flowDialogSpec("createAndEnable", "daily");
  const moved = dialogKey({ focus: spec.defaultFocus }, spec, "Tab");
  assert.deepEqual(moved, { focus: "confirm", action: null });
  assert.equal(dialogKey({ focus: moved.focus }, spec, "Enter").action, "confirm");
  assert.equal(dialogKey({ focus: spec.defaultFocus }, spec, "ArrowRight").focus, "confirm");
});

test("every flow dialog confirms on Enter when Confirm has the focus, and cancels on Esc from anywhere", () => {
  for (const action of WRITES) {
    const spec = flows.flowDialogSpec(action, "daily");
    assert.equal(dialogKey({ focus: "confirm" }, spec, "Enter").action, "confirm", action);
    for (const focus of ["cancel", "confirm"]) assert.equal(dialogKey({ focus }, spec, "Escape").action, "cancel", `${action} ${focus}`);
  }
});

test("a dialog spec that does not say confirmEnabled: true cannot be confirmed, so the flag is what makes each flow dialog work", () => {
  for (const action of WRITES) {
    const { confirmEnabled, ...without } = flows.confirmSpec(action);
    assert.equal(dialogKey({ focus: "confirm" }, without, "Enter").action, null, action);
  }
});

test("flowDialogSpec names the flow in the title of every flow dialog", () => {
  for (const action of WRITES) {
    assert.ok(flows.flowDialogSpec(action, "daily-report").title.includes("daily-report"), action);
    assert.ok(flows.flowDialogSpec(action, "nightly").title.includes("nightly"), action);
    assert.notEqual(flows.flowDialogSpec(action, "a").title, flows.flowDialogSpec(action, "b").title, action);
  }
});

test("each flow dialog's title says what it does to the flow", () => {
  assert.match(flows.flowDialogSpec("enable", "x").title, /^Enable\b/);
  assert.match(flows.flowDialogSpec("disable", "x").title, /^Disable\b/);
  assert.match(flows.flowDialogSpec("run", "x").title, /^Run\b.*\bnow\b/);
  const both = flows.flowDialogSpec("createAndEnable", "x").title;
  assert.match(both, /\bCreate\b/);
  assert.match(both, /\benable\b/i);
  assert.equal(new Set(WRITES.map((a) => flows.flowDialogSpec(a, "x").title)).size, 4, "no two dialogs share a title");
});

test("the name is put in the title as it is: markup, quotes, unicode and a long name are not changed", () => {
  for (const name of ["<b>x</b>", "a\"b'c", "日本-é", "x".repeat(64), "a b", "__proto__"]) {
    for (const action of WRITES) assert.ok(flows.flowDialogSpec(action, name).title.includes(name), `${action} ${name}`);
  }
});

test("Enable, Run now and Create and enable say that the flow launches an agent with nobody watching, and that a script runs a local file", () => {
  for (const action of ["enable", "run", "createAndEnable"]) {
    const { body } = flows.flowDialogSpec(action, "daily");
    assert.match(body, /launches an agent with nobody watching/, action);
    assert.match(body, /script runs a local file when one is set/, action);
  }
  assert.doesNotMatch(flows.flowDialogSpec("disable", "daily").body, /nobody watching/);
});

test("a flow dialog keeps what confirmSpec says besides the title: labels, focus, the 180 s limit of Run now", () => {
  for (const action of WRITES) {
    const plain = flows.confirmSpec(action);
    const named = flows.flowDialogSpec(action, "daily");
    assert.deepEqual({ ...named, title: null }, { ...plain, title: null }, action);
  }
  assert.equal(flows.flowDialogSpec("run", "daily").timeoutSeconds, 180);
  assert.ok(flows.flowDialogSpec("enable", "daily").timeoutSeconds < 180);
});

test("flowDialogSpec has no dialog for an action the window does not offer, as confirmSpec has none", () => {
  for (const action of ["delete", "remove", "approve", "", undefined, null, "__proto__", "constructor", "toString", "Enable"]) {
    assert.throws(() => flows.flowDialogSpec(action, "daily"), Error, String(action));
  }
});

// ---- READ_VIEWS and panelGate -------------------------------------------------------------------------

const READS = ["profiles", "providers", "workflows", "profile", "flow", "flowCheck"];
const WRITE_VIEWS = ["launch", "flowWrite", "flowSave"];
const AUTH_GATE = { disabled: true, reason: "auth", text: AUTH };

test("only the views of reads and of the validator can switch write controls off: READ_VIEWS names them and holds no write", () => {
  assert.deepEqual([...flows.READ_VIEWS].sort(), [...READS].sort());
  for (const w of WRITE_VIEWS) assert.ok(!flows.READ_VIEWS.includes(w), w);
  assert.ok(Object.isFrozen(flows.READ_VIEWS));
});

test("Auth required: a 401 or 403 on any read view disables the write controls with the auth message", () => {
  for (const name of READS) assert.deepEqual(flows.panelGate({ [name]: { auth: true } }, {}), AUTH_GATE, name);
  assert.deepEqual(flows.panelGate(deepFreeze({ profiles: {}, flow: { auth: true }, launch: {} }), deepFreeze({})), AUTH_GATE, "one auth among healthy views");
});

test("a write's own 401 or 403 never switches its control off: launch, flowWrite and flowSave are not read", () => {
  for (const name of WRITE_VIEWS) {
    const g = flows.panelGate({ [name]: { auth: true } }, {});
    assert.deepEqual(g, { disabled: false, reason: null, text: null }, name);
  }
  assert.equal(flows.panelGate({ launch: { auth: true }, flowWrite: { auth: true }, flowSave: { auth: true } }, {}).disabled, false);
  assert.equal(flows.panelGate({ launch: { auth: true }, flow: {} }, {}).disabled, false);
  assert.equal(flows.panelGate({ launch: { auth: true }, flow: { auth: true } }, {}).reason, "auth", "but a read's auth still counts next to a write's");
});

test("a view the panel does not know, or an auth flag that is not true, does not switch anything off", () => {
  assert.equal(flows.panelGate({ somethingElse: { auth: true }, workflowsRun: { auth: true } }, {}).disabled, false);
  for (const auth of ["yes", 1, "true", {}, null, undefined, false]) assert.equal(flows.panelGate({ flow: { auth } }, {}).disabled, false, JSON.stringify(auth));
});

test("the auth the Sessions block publishes also disables the controls, with the same message", () => {
  assert.deepEqual(flows.panelGate({}, { auth: true }), AUTH_GATE);
  assert.deepEqual(flows.panelGate(undefined, { auth: true }), AUTH_GATE);
});

test("a CAO that is unreachable disables the controls, and Retry is the way out of that and of nothing else", () => {
  const g = flows.panelGate({}, { unreachable: true });
  assert.deepEqual([g.disabled, g.reason, g.text], [true, "unreachable", "CAO unreachable, retrying"]);
  assert.equal(flows.panelGate({}, { unreachable: true }, { retry: true }).disabled, false);
  assert.equal(flows.panelGate({ flow: { auth: true } }, { unreachable: true }, { retry: true }).reason, "auth");
  assert.equal(flows.panelGate({}, { auth: true }, { retry: true }).disabled, true);
  assert.equal(flows.panelGate({}, {}, { retry: true, connected: false }).disabled, true);
});

test("a machine that is not connected disables the controls and says so", () => {
  const g = flows.panelGate({}, {}, { connected: false });
  assert.deepEqual([g.disabled, g.reason], [true, "not_connected"]);
  assert.match(g.text, /not connected/);
});

test("the reason is the most serious one: auth, then not connected, then unreachable", () => {
  assert.equal(flows.panelGate({ flow: { auth: true } }, { unreachable: true }, { connected: false }).reason, "auth");
  assert.equal(flows.panelGate({}, { unreachable: true }, { connected: false }).reason, "not_connected");
  assert.equal(flows.panelGate({}, { unreachable: true }).reason, "unreachable");
});

test("healthy views, nothing shared and no options leave the controls enabled, and the inputs are not changed", () => {
  const on = { disabled: false, reason: null, text: null };
  for (const [views, shared] of [[{}, {}], [undefined, undefined], [null, null], [{ flow: { at: 1, data: [] } }, { auth: false, unreachable: false }], [[], []]]) {
    assert.deepEqual(flows.panelGate(views, shared), on, JSON.stringify([views, shared]));
  }
  assert.deepEqual(flows.panelGate(deepFreeze({ flow: {}, launch: {} }), deepFreeze({}), deepFreeze({})), on);
});

test("Auth required reaches the save: the gate of a refused flow save is the gate of the form", () => {
  const gate = flows.panelGate({ flowCheck: { auth: true } }, {});
  const fields = { name: "daily", schedule: "0 9 * * *", agent_profile: "developer", provider: "kiro_cli", prompt: "x" };
  const state = flows.flowFormGate({ fields, checks: { canCreate: true }, checkedKey: flows.flowCheckKey(fields), routes: flows.routesState(200), gate, sending: false });
  assert.equal(state.create, false, "no Create under auth");
  const open = flows.panelGate({ flowSave: { auth: true } }, {});
  assert.equal(flows.flowFormGate({ fields, checks: { canCreate: true }, checkedKey: flows.flowCheckKey(fields), routes: flows.routesState(200), gate: open, sending: false }).create, true, "a refused save can be tried again");
});

// ---- 19.6: promptView (the prompt stays collapsed behind "Show prompt") ----

test("Prompt collapsed: closed gives the control 'Show prompt' and no text, open gives the text and 'Hide prompt', no prompt gives 'not available'", () => {
  const prompt = Array.from({ length: 40 }, (_, i) => `line ${i + 1} of the prompt`).join("\n");
  const flow = deepFreeze(DAILY({ prompt_template: prompt }));
  const closed = flows.promptView(flow, false);
  assert.equal(closed.control, "Show prompt");
  assert.equal(closed.text, null);
  assert.equal(closed.note, null);
  assert.ok(!JSON.stringify(closed).includes("line 1 of"), "the prompt is nowhere in the collapsed view");
  const open = flows.promptView(flow, true);
  assert.equal(open.control, "Hide prompt");
  assert.equal(open.text, prompt, "the text as the server has it");
  assert.equal(open.note, null);
  for (const odd of [undefined, null, 1, "yes", {}]) assert.deepEqual(flows.promptView(flow, odd), closed, `open = ${JSON.stringify(odd)} is closed`);
  // a flow with no prompt: nothing to show or hide, in either state
  for (const f of [DAILY({ prompt_template: null }), DAILY({ prompt_template: "" }), DAILY({ prompt_template: "  \n " }), DAILY({ prompt_template: 5 }), {}, null, undefined]) {
    for (const o of [false, true]) assert.deepEqual(flows.promptView(f, o), { control: null, text: null, note: "not available" }, `${JSON.stringify(f)} ${o}`);
  }
  // the details view carries it, collapsed unless the caller says open
  const details = flows.flowDetailsView(deepFreeze({ flow: DAILY({ prompt_template: prompt }), nextRuns: [], validateStatus: 200, now: NOW }));
  assert.deepEqual(details.prompt, closed);
  assert.deepEqual(flows.flowDetailsView(deepFreeze({ flow: DAILY({ prompt_template: prompt }), nextRuns: [], validateStatus: 200, now: NOW, promptOpen: true })).prompt, open);
  assert.deepEqual(flows.flowDetailsView({}).prompt, { control: null, text: null, note: "not available" });
});

// ---- 29.6: the person reads "schedule"; CAO's own name for it stays "flow" in its routes, sessions and files ----

test("Schedule words: the confirmations, titles and outcomes say schedule, never flow", () => {
  assert.deepEqual(["enable", "run", "createAndEnable", "disable"].map((a) => flows.confirmSpec(a).title), ["Enable this schedule?", "Run this schedule now?", "Create this schedule and enable it?", "Disable this schedule?"]);
  assert.deepEqual(["enable", "run", "createAndEnable", "disable"].map((a) => flows.flowDialogSpec(a, "nightly").title), ["Enable schedule nightly?", "Run schedule nightly now?", "Create schedule nightly and enable it?", "Disable schedule nightly?"]);
  assert.deepEqual(["enable", "run", "createAndEnable"].map((a) => flows.confirmSpec(a).confirmLabel), ["Enable", "Run now", "Create and enable"], "the button words are unchanged");
  assert.equal(flows.confirmSpec("enable").body, "This schedule launches an agent with nobody watching, and a schedule's script runs a local file when one is set.");
  assert.equal(flows.writeOutcome("run", { kind: "ok", json: { executed: true } }).text, "The schedule ran.");
  assert.equal(flows.writeOutcome("run", { kind: "ok", json: {} }).text, "CAO answered, but not whether the schedule ran. Look at its session before running it again.");
  assert.equal(flows.writeOutcome("run", { kind: "unreachable" }).text, "CAO did not answer in time. The schedule may still be running: look for its cao-flow session before running it again.", "the session is CAO's own name, cao-flow-<name>");
});

test("Schedule words: what stays CAO's own: the routes, the session names, the server's own message and the CLI it names", () => {
  assert.equal(flows.flowRoute("daily"), "/flows/daily");
  assert.equal(flows.FLOW_NAME_TAKEN_MESSAGE, "a flow with this name already exists", "the text the server sends and the window matches");
  assert.match(flows.CREATE_VIA_CLI, /^This server cannot check a schedule first, and its create would arm the schedule and overwrite a schedule with the same name\. Create it in a terminal on the CAO machine with `cao schedule add <file>`/);
  assert.equal(flows.flowSessionWorking([{ name: "cao-flow-daily", terms: [{ state: "working" }] }], "daily"), true, "the session of a schedule is cao-flow-<name>");
  assert.equal(flows.flowSessionWorking([{ name: "cao-schedule-daily", terms: [{ state: "working" }] }], "daily"), false);
});

await finish();
