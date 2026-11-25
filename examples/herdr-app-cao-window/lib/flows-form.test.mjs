// lib/flows.js: the New flow and Edit form helpers added with task 10.5 (spec cao-window-flows "New flow
// and edit", flow-authoring-api for the routes the form calls, design D13 and D14). Cases come from the
// spec scenarios first: Create disabled, Validation errors, Duplicate on create, Parse-shape 422, Numeric
// day of week, Schedule a workflow. Inputs are deep-frozen where it matters, so a function that writes to
// its argument throws.
import { assert, finish, test } from "../test/harness.mjs";
import { callCao } from "./cao.js";
import * as flows from "./flows.js";

const AUTH = "CAO requires auth - not supported by this window yet";

const deepFreeze = (o) => {
  if (o && typeof o === "object" && !Object.isFrozen(o)) { Object.freeze(o); for (const v of Object.values(o)) deepFreeze(v); }
  return o;
};

const FIELDS = (over = {}) => ({ name: "daily", schedule: "0 9 * * mon-fri", agent_profile: "developer", provider: "claude_code", prompt: "Report.", ...over });
const FORM = (over = {}) => ({ name: "daily", kind: "prompt", workflow: "", schedule: "0 9 * * mon-fri", profile: "developer", provider: "claude_code", prompt: "Report.", ...over });
const ROW = (over = {}) => ({ name: "daily", schedule: "0 9 * * *", agent_profile: "developer", provider: "kiro_cli", prompt_template: "A", enabled: true, next_run: "2026-11-19T09:00:00", script: "gate.py", ...over });
const ok = (json, status = 200) => ({ kind: "ok", status, json });
const err = (status, json, detail) => ({ kind: "error", status, json, detail });

// ---- fieldsOfFlow and validateBody -------------------------------------------------------------------

test("fieldsOfFlow names a GET /flows/{name} row's values as the form and the validate body do", () => {
  assert.deepEqual(flows.fieldsOfFlow(deepFreeze(ROW())), { name: "daily", schedule: "0 9 * * *", agent_profile: "developer", provider: "kiro_cli", prompt: "A" });
  assert.deepEqual(Object.keys(flows.fieldsOfFlow(ROW())), ["name", "schedule", "agent_profile", "provider", "prompt"], "no script, no enabled, no next run");
});

test("fieldsOfFlow of a row with fields missing, or of nothing, is empty text, never undefined", () => {
  const empty = { name: "", schedule: "", agent_profile: "", provider: "", prompt: "" };
  for (const bad of [undefined, null, {}, { name: null, schedule: null, agent_profile: null, provider: null, prompt_template: null }]) assert.deepEqual(flows.fieldsOfFlow(bad), empty, JSON.stringify(bad));
});

test("validateBody is the body POST /flows/validate takes: five fields, the prompt as prompt_template, no script", () => {
  const body = flows.validateBody(deepFreeze(FIELDS({ script: "/etc/passwd", enabled: true, engine: "kas", file_path: "/x" })));
  assert.deepEqual(body, { name: "daily", schedule: "0 9 * * mon-fri", agent_profile: "developer", provider: "claude_code", prompt_template: "Report." });
  assert.deepEqual(Object.keys(body), ["name", "schedule", "agent_profile", "provider", "prompt_template"]);
});

test("Numeric day of week: validateBody sends the schedule exactly as typed, so the server's reading of it decides", () => {
  for (const schedule of ["0 9 * * 1-5", "  0 9 * * 1-5  ", "0\t9 * * 1-5", "0 9 * * 1-5 ".repeat(50), "0 9 * * 1-5\n", "日本 é"]) {
    assert.equal(flows.validateBody(FIELDS({ schedule })).schedule, schedule, JSON.stringify(schedule));
  }
  for (const k of ["name", "provider", "agent_profile"]) assert.equal(flows.validateBody(FIELDS({ [k]: ` ${k} ` }))[k], ` ${k} `, "nothing is trimmed on the way to the server");
});

test("validateBody invents no default: an empty provider or profile goes to the server, which reports it", () => {
  const body = flows.validateBody({ name: "", schedule: "", agent_profile: "", provider: "", prompt: "" });
  assert.deepEqual(body, { name: "", schedule: "", agent_profile: "", provider: "", prompt_template: "" });
});

test("validate and create send the same value for every field they share, so what was checked is what is created", () => {
  const form = FORM({ profile: "developer", provider: "", prompt: "Report [[repo]]" });
  const fields = flows.flowFields(form, { provider: "claude_code" });
  const v = flows.validateBody(fields);
  const c = flows.flowCreateBody(fields, {});
  for (const k of ["name", "schedule", "agent_profile", "provider", "prompt_template"]) assert.equal(c[k], v[k], k);
  assert.deepEqual(Object.keys(c).sort(), [...Object.keys(v), "enabled"].sort());
});

// ---- flowCheckKey ------------------------------------------------------------------------------------

test("flowCheckKey is the same for the same fields and changes when any checked field changes", () => {
  const key = flows.flowCheckKey(FIELDS());
  assert.equal(flows.flowCheckKey(FIELDS()), key);
  for (const k of ["name", "schedule", "agent_profile", "provider", "prompt"]) assert.notEqual(flows.flowCheckKey(FIELDS({ [k]: `${FIELDS()[k]} ` })), key, `a trailing space in ${k}`);
  for (const k of ["name", "schedule", "agent_profile", "provider", "prompt"]) assert.notEqual(flows.flowCheckKey(FIELDS({ [k]: "" })), key, `${k} emptied`);
});

test("flowCheckKey ignores what the validate body does not carry, and the order the fields were built in", () => {
  const key = flows.flowCheckKey(FIELDS());
  assert.equal(flows.flowCheckKey(FIELDS({ script: "x", enabled: true, extra: 1 })), key);
  const shuffled = { prompt: "Report.", provider: "claude_code", name: "daily", agent_profile: "developer", schedule: "0 9 * * mon-fri" };
  assert.equal(flows.flowCheckKey(shuffled), key);
});

test("two different sets of fields never share a key, even when text is moved across the field boundary", () => {
  const a = flows.flowCheckKey(FIELDS({ name: "a", schedule: "b" }));
  assert.notEqual(flows.flowCheckKey(FIELDS({ name: 'a","schedule":"b' })), a);
  assert.notEqual(flows.flowCheckKey(FIELDS({ name: "a", schedule: "b", prompt: "x" })), flows.flowCheckKey(FIELDS({ name: "a", schedule: "bx", prompt: "" })));
  assert.notEqual(flows.flowCheckKey(FIELDS({ name: "ab", schedule: "" })), flows.flowCheckKey(FIELDS({ name: "a", schedule: "b" })));
  assert.notEqual(flows.flowCheckKey(FIELDS({ name: "a|b", schedule: "c" })), flows.flowCheckKey(FIELDS({ name: "a", schedule: "b|c" })));
});

test("flowCheckKey tells names that differ only in case apart: flow names are case-sensitive", () => {
  assert.notEqual(flows.flowCheckKey(FIELDS({ name: "Daily" })), flows.flowCheckKey(FIELDS({ name: "daily" })));
  assert.notEqual(flows.flowCheckKey(FIELDS({ prompt: "Report." })), flows.flowCheckKey(FIELDS({ prompt: "report." })));
});

// ---- presets -----------------------------------------------------------------------------------------

test("the schedule presets write the day of week as names: none holds a digit there, each is a cron the window can put in words", () => {
  assert.ok(Object.isFrozen(flows.FLOW_PRESETS));
  assert.ok(flows.FLOW_PRESETS.length >= 2);
  for (const p of flows.FLOW_PRESETS) {
    assert.equal(p.cron.trim().split(/\s+/).length, 5, p.cron);
    assert.equal(flows.numericWeekdayWarning(p.cron), null, `${p.cron} would trigger the Monday-as-0 warning`);
    assert.notEqual(flows.cronHuman(p.cron), null, `${p.cron} has no words`);
    assert.ok(typeof p.label === "string" && p.label.trim() !== "", p.cron);
  }
  assert.equal(new Set(flows.FLOW_PRESETS.map((p) => p.label)).size, flows.FLOW_PRESETS.length, "labels are distinct");
  assert.equal(new Set(flows.FLOW_PRESETS.map((p) => p.cron)).size, flows.FLOW_PRESETS.length, "crons are distinct");
});

test("the presets include weekdays written as mon-fri, as the spec's example does", () => {
  assert.ok(flows.FLOW_PRESETS.some((p) => /\* \* mon-fri$/.test(p.cron)), JSON.stringify(flows.FLOW_PRESETS));
});

// ---- the forms ---------------------------------------------------------------------------------------

test("a new flow's form is empty and asks for an agent prompt", () => {
  assert.deepEqual(flows.emptyFlowForm(), { name: "", kind: "prompt", workflow: "", schedule: "", profile: "", provider: "", prompt: "" });
  assert.deepEqual(flows.emptyFlowForm({}), flows.emptyFlowForm());
  assert.deepEqual(flows.emptyFlowForm({ workflow: "" }), flows.emptyFlowForm(), "an empty workflow name preselects nothing");
  assert.deepEqual(flows.emptyFlowForm({ workflow: undefined }), flows.emptyFlowForm());
});

test("each new form is its own object: typing in one never changes the next", () => {
  const a = flows.emptyFlowForm();
  a.name = "typed";
  assert.equal(flows.emptyFlowForm().name, "");
  assert.notEqual(flows.emptyFlowForm(), flows.emptyFlowForm());
});

test("Schedule a workflow: the form opens on 'a workflow' with that workflow chosen and nothing else filled", () => {
  const f = flows.emptyFlowForm({ workflow: "pr-health" });
  assert.deepEqual(f, { name: "", kind: "workflow", workflow: "pr-health", schedule: "", profile: "", provider: "", prompt: "" });
  assert.match(flows.scheduleWarning(), /plan approval/, "and the form shows the approval warning");
});

test("the Edit form holds the flow's values under the form's own field names, and opens on an agent prompt", () => {
  assert.deepEqual(flows.flowFormOf(deepFreeze(ROW())), { name: "daily", kind: "prompt", workflow: "", schedule: "0 9 * * *", profile: "developer", provider: "kiro_cli", prompt: "A" });
  assert.deepEqual(Object.keys(flows.flowFormOf(ROW())), Object.keys(flows.emptyFlowForm()), "the same shape as a new form");
  const empty = flows.flowFormOf(undefined);
  assert.deepEqual(empty, flows.emptyFlowForm());
  assert.deepEqual(flows.flowFormOf({ name: null, schedule: null, agent_profile: null, provider: null, prompt_template: null }), flows.emptyFlowForm());
  assert.equal(flows.flowFormOf(ROW({ prompt_template: "Run [[x]]\ncao workflow run y" })).kind, "prompt", "a prompt that mentions a workflow stays a prompt");
});

// ---- flowFields --------------------------------------------------------------------------------------

test("flowFields maps the form to the values the flow routes take: profile becomes agent_profile, the prompt stays as typed", () => {
  const prompt = "\n  Review [[repo]]\n```x```  é\n\n";
  assert.deepEqual(flows.flowFields(deepFreeze(FORM({ prompt })), { provider: "other" }), { name: "daily", schedule: "0 9 * * mon-fri", agent_profile: "developer", provider: "claude_code", prompt });
});

test("the provider defaults to the profile's, and to kiro_cli when there is no profile or it names none; a typed provider wins", () => {
  assert.equal(flows.flowFields(FORM({ provider: "" }), { provider: "codex" }).provider, "codex");
  assert.equal(flows.flowFields(FORM({ provider: "claude_code" }), { provider: "codex" }).provider, "claude_code");
  for (const profile of [undefined, null, {}, { provider: "" }, { provider: null }]) assert.equal(flows.flowFields(FORM({ provider: "" }), profile).provider, "kiro_cli", JSON.stringify(profile));
  assert.equal(flows.flowFields(FORM({ provider: "" })).provider, "kiro_cli", "no profile argument at all");
});

test("a workflow form's prompt is generated from the chosen workflow and the typed prompt is not used", () => {
  const f = flows.flowFields(FORM({ kind: "workflow", workflow: "pr-health", prompt: "ignored text" }), {});
  assert.equal(f.prompt, flows.schedulePrompt("pr-health"));
  assert.match(f.prompt, /cao workflow run pr-health(?![\w-])/);
  assert.ok(!f.prompt.includes("ignored text"));
});

test("a workflow form with no workflow or an unusable name has an empty prompt and does not throw", () => {
  for (const workflow of ["", "a b", "x; rm -rf /", "$(id)", "../x", undefined, null, 5]) {
    assert.equal(flows.flowFields(FORM({ kind: "workflow", workflow, prompt: "typed" }), {}).prompt, "", JSON.stringify(workflow));
  }
});

test("a form of any other kind is read as an agent prompt", () => {
  for (const kind of ["prompt", undefined, "", "other"]) assert.equal(flows.flowFields(FORM({ kind, workflow: "pr-health", prompt: "typed" }), {}).prompt, "typed", String(kind));
});

test("Schedule a workflow end to end: the preselected workflow reaches the create body as a prompt that runs it", () => {
  const form = { ...flows.emptyFlowForm({ workflow: "pr-health" }), name: "pr-nightly", schedule: "0 2 * * *", profile: "developer" };
  const body = flows.flowCreateBody(flows.flowFields(form, { provider: "claude_code" }), {});
  assert.equal(body.name, "pr-nightly");
  assert.equal(body.provider, "claude_code");
  assert.equal(body.enabled, false);
  assert.match(body.prompt_template, /cao workflow run pr-health/);
});

test("Create disabled end to end: a filled form becomes a POST /flows body with enabled false, and Create and enable is the only way to true", () => {
  const fields = flows.flowFields(FORM(), {});
  assert.deepEqual(flows.flowCreateBody(fields, {}), { name: "daily", schedule: "0 9 * * mon-fri", agent_profile: "developer", provider: "claude_code", prompt_template: "Report.", enabled: false });
  assert.equal(flows.flowCreateBody(fields, { enable: true }).enabled, true);
});

test("a workflow form with no workflow chosen has no prompt, so Create stays off however the checks stand", () => {
  const form = { ...flows.emptyFlowForm({ workflow: "" }), kind: "workflow", name: "n", schedule: "0 9 * * *", profile: "developer" };
  const fields = flows.flowFields(form, { provider: "claude_code" });
  const g = flows.flowFormGate({ fields, checks: { canCreate: true, byField: {}, nextRuns: [] }, checkedKey: flows.flowCheckKey(fields), routes: flows.routesState(200), gate: { disabled: false }, sending: false });
  assert.deepEqual([g.create, g.filled], [false, false]);
});

test("Edit with no change sends nothing, and a changed schedule sends only the schedule", () => {
  const row = ROW();
  const same = flows.flowFields(flows.flowFormOf(row), {});
  assert.deepEqual(flows.flowUpdateBody(row, same), {});
  const edited = flows.flowFields({ ...flows.flowFormOf(row), schedule: "30 8 * * mon-fri" }, {});
  assert.deepEqual(flows.flowUpdateBody(row, edited), { schedule: "30 8 * * mon-fri" });
});

// ---- formErrorsOf, shownErrors, firstErrorField ------------------------------------------------------

test("formErrorsOf puts each server field under the form's field of the same meaning", () => {
  const out = flows.formErrorsOf(deepFreeze({ name: ["n"], schedule: ["s"], provider: ["p"], agent_profile: ["a"], prompt_template: ["t"] }));
  assert.deepEqual(out, { name: ["n"], schedule: ["s"], provider: ["p"], profile: ["a"], prompt: ["t"] });
});

test("a server field the form has no place for is shown as a general error of the form, and none is lost or merged wrongly", () => {
  assert.deepEqual(flows.formErrorsOf({ script: ["no script"], enabled: ["x"], form: ["f"] }), { form: ["no script", "x", "f"] });
  assert.deepEqual(flows.formErrorsOf({ prompt_template: ["t"], prompt: ["p"] }), { prompt: ["t", "p"] }, "two server names for the prompt keep both messages, in order");
  for (const field of ["__proto__", "constructor", "toString", "hasOwnProperty", ""]) {
    const input = JSON.parse(`{${JSON.stringify(field)}: ["m"]}`);
    const out = flows.formErrorsOf(input);
    assert.deepEqual(Object.keys(out), ["form"], JSON.stringify(field));
    assert.deepEqual(out.form, ["m"]);
    assert.equal(Object.getPrototypeOf(out), Object.prototype);
  }
});

test("formErrorsOf of nothing is nothing, and it hands out its own lists", () => {
  for (const bad of [undefined, null, {}]) assert.deepEqual(flows.formErrorsOf(bad), {}, JSON.stringify(bad));
  const input = { name: ["a"] };
  const out = flows.formErrorsOf(input);
  out.name.push("b");
  assert.deepEqual(input, { name: ["a"] });
});

test("shownErrors shows a field's error once the field was touched or a save was tried, and the form's general errors always", () => {
  const errors = deepFreeze({ name: ["n"], schedule: ["s"], form: ["f"] });
  assert.deepEqual(flows.shownErrors(errors, {}, false), { form: ["f"] });
  assert.deepEqual(flows.shownErrors(errors, { schedule: true }, false), { schedule: ["s"], form: ["f"] });
  assert.deepEqual(flows.shownErrors(errors, { schedule: true, name: true }, false), errors);
  assert.deepEqual(flows.shownErrors(errors, {}, true), errors);
  assert.deepEqual(flows.shownErrors(errors), { form: ["f"] }, "no touched map and no attempt");
});

test("only a touched field of true counts as touched, and nothing is invented", () => {
  const errors = { name: ["n"] };
  for (const mark of [1, "yes", {}, null, false, undefined]) assert.deepEqual(flows.shownErrors(errors, { name: mark }, false), {}, JSON.stringify(mark));
  assert.deepEqual(flows.shownErrors({}, { name: true }, true), {});
  assert.deepEqual(flows.shownErrors(undefined, {}, true), {});
  assert.deepEqual(flows.shownErrors(null), {});
  assert.deepEqual(flows.shownErrors({ name: ["n"] }, { schedule: true }, false), {}, "touching one field shows no other");
});

test("Validation errors: focus goes to the first field with an error, in the form's order, whatever order the server listed them", () => {
  assert.equal(flows.firstErrorField({ schedule: ["bad"] }), "schedule");
  assert.equal(flows.firstErrorField({ prompt: ["p"], provider: ["p"], profile: ["p"], schedule: ["p"], name: ["p"], form: ["p"] }), "name");
  assert.equal(flows.firstErrorField({ prompt: ["p"], provider: ["p"], profile: ["p"], schedule: ["p"] }), "schedule");
  assert.equal(flows.firstErrorField({ prompt: ["p"], provider: ["p"], profile: ["p"] }), "profile");
  assert.equal(flows.firstErrorField({ prompt: ["p"], provider: ["p"] }), "provider");
  assert.equal(flows.firstErrorField({ form: ["p"], prompt: ["p"] }), "prompt");
  assert.equal(flows.firstErrorField({ form: ["p"] }), "form");
});

test("a field with an empty list of errors takes no focus, and nothing to focus is null", () => {
  assert.equal(flows.firstErrorField({ name: [], schedule: ["s"] }), "schedule");
  for (const none of [{}, undefined, null, { name: [] }, { script: ["not a form field"] }, { name: undefined }]) assert.equal(flows.firstErrorField(none), null, JSON.stringify(none));
});

// ---- flowFormGate ------------------------------------------------------------------------------------

const GOOD_CHECKS = { canCreate: true, byField: {}, nextRuns: [] };
const OPEN = { disabled: false, reason: null, text: null };
const GATE = (over = {}) => ({ fields: FIELDS(), checks: GOOD_CHECKS, checkedKey: flows.flowCheckKey(FIELDS()), routes: flows.routesState(200), gate: OPEN, sending: false, ...over });

test("Create works when every field is filled and the server's check of exactly these fields says valid", () => {
  assert.deepEqual(flows.flowFormGate(GATE()), { create: true, label: null, filled: true });
});

test("Validation errors: Create is off while the check holds errors, and stays off after an edit until a new check says valid", () => {
  const typed = FIELDS({ schedule: "61 * * * *" });
  const failed = flows.flowChecksFromValidate({ valid: false, errors: [{ field: "schedule", message: "bad cron" }], next_runs: [] });
  assert.equal(flows.flowFormGate(GATE({ fields: typed, checks: failed, checkedKey: flows.flowCheckKey(typed) })).create, false);
  const fixed = FIELDS({ schedule: "0 9 * * *" });
  assert.equal(flows.flowFormGate(GATE({ fields: fixed, checks: failed, checkedKey: flows.flowCheckKey(typed) })).create, false, "the old failure does not allow the new text");
  assert.equal(flows.flowFormGate(GATE({ fields: fixed, checks: GOOD_CHECKS, checkedKey: flows.flowCheckKey(typed) })).create, false, "an old pass does not allow new text");
  assert.equal(flows.flowFormGate(GATE({ fields: fixed, checks: GOOD_CHECKS, checkedKey: flows.flowCheckKey(fixed) })).create, true, "the new check is valid");
});

test("Create is off when no check has come back yet, or the answer was not a valid one", () => {
  for (const checks of [undefined, null, {}, { canCreate: false }, { canCreate: "true" }, { canCreate: 1 }]) assert.equal(flows.flowFormGate(GATE({ checks })).create, false, JSON.stringify(checks));
  for (const checkedKey of [undefined, null, "", "other"]) assert.equal(flows.flowFormGate(GATE({ checkedKey })).create, false, JSON.stringify(checkedKey));
});

test("a field left empty, or of blanks, turns Create off whatever the server said, and 'filled' says so", () => {
  for (const k of ["name", "schedule", "agent_profile", "provider", "prompt"]) {
    for (const blank of ["", "   ", "\n\t", undefined, null]) {
      const fields = FIELDS({ [k]: blank });
      const g = flows.flowFormGate(GATE({ fields, checkedKey: flows.flowCheckKey(fields) }));
      assert.deepEqual([g.create, g.filled], [false, false], `${k}=${JSON.stringify(blank)}`);
    }
  }
});

test("Create is off under a gate (auth, unreachable, not connected) and while a save is out", () => {
  for (const gate of [{ disabled: true, reason: "auth" }, { disabled: true, reason: "unreachable" }, { disabled: true, reason: "not_connected" }]) assert.equal(flows.flowFormGate(GATE({ gate })).create, false, gate.reason);
  assert.equal(flows.flowFormGate(GATE({ sending: true })).create, false, "a second click while saving must not send a second request");
  assert.equal(flows.flowFormGate(GATE({ gate: undefined })).create, true, "no gate given is no gate");
  assert.equal(flows.flowFormGate(GATE({ sending: undefined })).create, true);
});

// An older server has no POST /flows/validate, and its POST /flows arms the flow and overwrites the file of a
// same-named flow, so the window cannot create there (spec cao-window-flows, "Validate route missing"). The form
// says how to create it from a terminal instead.
const CLI_ADD = "cao schedule add";
const CLI_DISABLE = "cao schedule disable";

test("Validate route missing: Create is disabled on filled fields, and the form shows cao schedule add and cao schedule disable as the way to create it", () => {
  for (const status of [404, 405]) {
    const routes = flows.routesState(status);
    const g = flows.flowFormGate(GATE({ routes, checks: null, checkedKey: undefined }));
    assert.deepEqual([g.create, g.filled], [false, true], String(status));
    const shown = JSON.stringify(g);
    assert.ok(shown.includes(CLI_ADD) && shown.includes(CLI_DISABLE), `the form text names both commands: ${shown}`);
  }
});

test("Validate route missing: no earlier check, however good, turns Create back on", () => {
  const routes = flows.routesState(404);
  assert.equal(flows.flowFormGate(GATE({ routes })).create, false, "a check that passed before the route went missing");
  for (const checks of [undefined, null, {}, { canCreate: true }, { canCreate: "true" }]) {
    assert.equal(flows.flowFormGate(GATE({ routes, checks })).create, false, JSON.stringify(checks));
  }
  assert.equal(flows.flowFormGate(GATE({ routes, gate: undefined, sending: undefined })).create, false, "no gate and no save in flight is still not enough");
});

test("Validate route missing: blank fields, a gate and a save in flight keep Create off, and the form still names the commands", () => {
  const routes = flows.routesState(404);
  const blank = FIELDS({ name: "" });
  const g = flows.flowFormGate(GATE({ routes, fields: blank, checks: null }));
  assert.deepEqual([g.create, g.filled], [false, false]);
  assert.ok(JSON.stringify(g).includes(CLI_ADD), "an unfilled form also says how to create the flow");
  assert.equal(flows.flowFormGate(GATE({ routes, gate: { disabled: true } })).create, false, "still off under auth");
  assert.equal(flows.flowFormGate(GATE({ routes, sending: true })).create, false);
});

test("a validate route that answered anything but 404 or 405 is not missing: Create follows the server's check as before", () => {
  for (const status of [200, 201, 204, 400, 401, 403, 408, 422, 429, 500, 502, 503, 0, null, undefined, "404", "405", 404.5, NaN]) {
    const routes = flows.routesState(status);
    assert.equal(routes.missing, false, String(status));
    assert.equal(flows.flowFormGate(GATE({ routes })).create, true, `${String(status)}: a good check opens Create`);
    assert.equal(flows.flowFormGate(GATE({ routes, checks: { canCreate: false, byField: {}, nextRuns: [] } })).create, false, `${String(status)}: a failed check keeps it off`);
    const step = flows.flowSaveAttempt({ gate: OPEN, state: { create: true, filled: true }, enable: false, checked: true, routesMissing: routes.missing });
    assert.equal(step.action, "send", String(status));
  }
});

test("with the route present Create has no label, and a route that answered 500 is not a missing route", () => {
  assert.equal(flows.flowFormGate(GATE()).label, null);
  assert.equal(flows.flowFormGate(GATE({ routes: flows.routesState(500), checks: null })).create, false);
  assert.equal(flows.flowFormGate(GATE({ routes: undefined })).create, true);
});

test("flowFormGate with nothing given is closed, empty and unlabelled, and does not throw", () => {
  assert.deepEqual(flows.flowFormGate(), { create: false, label: null, filled: false });
  assert.deepEqual(flows.flowFormGate({}), { create: false, label: null, filled: false });
});

test("Edit: the flow's own name being 'taken' does not hold Save back, and a real error does", () => {
  const taken = { valid: false, errors: [{ field: "name", message: "a flow with this name already exists" }], next_runs: [] };
  const fields = flows.fieldsOfFlow(ROW());
  const key = flows.flowCheckKey(fields);
  const own = flows.validateResult(ok(taken), { editing: "daily", name: "daily" }).checks;
  assert.equal(flows.flowFormGate(GATE({ fields, checks: own, checkedKey: key })).create, true);
  const bad = flows.validateResult(ok({ ...taken, errors: [...taken.errors, { field: "schedule", message: "bad" }] }), { editing: "daily", name: "daily" }).checks;
  assert.equal(flows.flowFormGate(GATE({ fields, checks: bad, checkedKey: key })).create, false);
});

// ---- flowSaveOutcome ---------------------------------------------------------------------------------

test("Create disabled: a 201 is a save of the named flow, and it is not armed", () => {
  assert.deepEqual(flows.flowSaveOutcome(deepFreeze(ok({ name: "daily", enabled: false }, 201)), { mode: "create" }), { type: "saved", name: "daily", armed: false });
  assert.deepEqual(flows.flowSaveOutcome(ok({ name: "daily", enabled: false }, 201), { mode: "create", enable: false }), { type: "saved", name: "daily", armed: false });
});

test("a server older than 'enabled' ignores enabled false and arms the flow: a 201 saying enabled true is flagged unless Create and enable was chosen", () => {
  assert.equal(flows.flowSaveOutcome(ok({ name: "daily", enabled: true }, 201), { mode: "create" }).armed, true);
  assert.equal(flows.flowSaveOutcome(ok({ name: "daily", enabled: true }, 201), { mode: "create", enable: false }).armed, true);
  assert.equal(flows.flowSaveOutcome(ok({ name: "daily", enabled: true }, 201), { mode: "create", enable: true }).armed, false, "armed on purpose is not a warning");
  assert.equal(flows.flowSaveOutcome(ok({ name: "daily", enabled: true }, 201), { mode: "create", enable: "true" }).armed, true, "only a boolean true chose it");
  for (const enabled of ["true", 1, undefined, null, false]) assert.equal(flows.flowSaveOutcome(ok({ name: "daily", enabled }, 201), { mode: "create" }).armed, false, JSON.stringify(enabled));
  assert.equal(flows.flowSaveOutcome(ok(undefined, 201), { mode: "create" }).armed, false);
});

test("an edit of an enabled flow is never flagged as armed", () => {
  assert.deepEqual(flows.flowSaveOutcome(ok({ name: "daily", enabled: true }), { mode: "edit" }), { type: "saved", name: "daily", armed: false });
});

test("a save answer with no usable name still counts as saved, with the name null", () => {
  for (const json of [undefined, null, {}, { name: 5 }, { name: null }, []]) assert.deepEqual(flows.flowSaveOutcome(ok(json, 201), { mode: "create" }), { type: "saved", name: null, armed: false }, JSON.stringify(json));
});

test("Duplicate on create: a 409 says a schedule with that name exists, at the name field, and carries nothing else", () => {
  assert.deepEqual(flows.flowSaveOutcome(err(409, { detail: "x" }, "x"), { mode: "create" }), { type: "conflict", field: "name", text: "a schedule with that name exists" });
});

test("an edit's 409 is not a name clash: it is the server's own message, a failure", () => {
  const o = flows.flowSaveOutcome(err(409, { detail: "flow file is not under the flows folder" }, "flow file is not under the flows folder"), { mode: "edit" });
  assert.deepEqual(o, { type: "failed", text: "flow file is not under the flows folder" });
});

test("Parse-shape 422: a FastAPI detail list lands at the name field", () => {
  const o = flows.flowSaveOutcome(err(422, { detail: [{ loc: ["body", "name"], msg: "Flow name must not contain '/'", type: "value_error" }] }), { mode: "create" });
  assert.deepEqual(o, { type: "fields", errors: { name: ["Flow name must not contain '/'"] }, general: [] });
});

test("Validation errors: the {errors} shape of a 422 lands at the form's fields, with the server's names translated", () => {
  const json = { detail: { errors: [{ field: "schedule", message: "bad cron" }, { field: "agent_profile", message: "empty" }, { field: "prompt_template", message: "p" }, { field: "provider", message: "q" }] } };
  assert.deepEqual(flows.flowSaveOutcome(err(422, json), { mode: "create" }), { type: "fields", errors: { schedule: ["bad cron"], profile: ["empty"], prompt: ["p"], provider: ["q"] }, general: [] });
  assert.equal(flows.flowSaveOutcome(err(422, json), { mode: "edit" }).type, "fields", "an edit's 422 has the same body");
});

test("a 422 for a field the form has none for, or for no field, still reaches the user", () => {
  const o = flows.flowSaveOutcome(err(422, { detail: { errors: [{ field: "script", message: "no script" }, { message: "general one" }] } }), { mode: "create" });
  assert.deepEqual(o, { type: "fields", errors: { form: ["no script"] }, general: ["general one"] });
  assert.deepEqual(flows.flowSaveOutcome(err(422, { detail: "plain" }), { mode: "create" }), { type: "fields", errors: {}, general: ["plain"] });
  assert.equal(flows.flowSaveOutcome(err(422, undefined), { mode: "create" }).type, "fields");
});

test("no answer from CAO is its own outcome and tells the user the write may have happened, in words that fit create and edit", () => {
  const create = flows.flowSaveOutcome({ kind: "unreachable" }, { mode: "create" });
  const edit = flows.flowSaveOutcome({ kind: "unreachable" }, { mode: "edit" });
  assert.equal(create.type, "unreachable");
  assert.equal(edit.type, "unreachable");
  assert.match(create.text, /may exist/);
  assert.match(create.text, /Schedules list/);
  assert.match(edit.text, /may have been applied/);
  assert.notEqual(create.text, edit.text);
  assert.doesNotMatch(create.text, /\bfailed\b/);
});

test("an auth refusal is its own outcome with the message it came with", () => {
  assert.deepEqual(flows.flowSaveOutcome({ kind: "auth", status: 401, message: AUTH }, { mode: "create" }), { type: "auth", text: AUTH });
  assert.deepEqual(flows.flowSaveOutcome({ kind: "auth", status: 403 }, { mode: "edit" }), { type: "auth", text: "" });
});

test("any other answer is a failure that shows the server's message", () => {
  assert.deepEqual(flows.flowSaveOutcome(err(404, { detail: "Flow 'x' not found" }, "Flow 'x' not found"), { mode: "edit" }), { type: "failed", text: "Flow 'x' not found" });
  assert.deepEqual(flows.flowSaveOutcome(err(500, undefined, "boom"), { mode: "create" }), { type: "failed", text: "boom" });
  assert.deepEqual(flows.flowSaveOutcome(err(500), { mode: "create" }), { type: "failed", text: "failed" });
  assert.equal(flows.flowSaveOutcome({ kind: "error", status: 400, message: "m" }, { mode: "create" }).text, "m");
});

test("flowSaveOutcome of an outcome it was never meant to see does not throw and is a failure", () => {
  for (const o of [undefined, null, {}, { kind: "approval_required", status: 403 }, { kind: "weird" }, "x"]) assert.equal(flows.flowSaveOutcome(o, { mode: "create" }).type, "failed", JSON.stringify(o));
  assert.equal(flows.flowSaveOutcome(err(409), undefined).type, "conflict", "no options at all is a create");
});

test("the server's message is shown unchanged, markup and unicode included", () => {
  const msg = "<b>no</b> \"q\" é 日本 ${x} ‮";
  assert.equal(flows.flowSaveOutcome(err(500, undefined, msg), { mode: "create" }).text, msg);
  assert.deepEqual(flows.flowSaveOutcome(err(422, { detail: [{ loc: ["body", "schedule"], msg }] }), { mode: "create" }).errors.schedule, [msg]);
});

// ---- workflowChoices ---------------------------------------------------------------------------------

test("a workflow is picked from GET /workflows by its bare name, in the server's order", () => {
  assert.deepEqual(flows.workflowChoices(deepFreeze([{ name: "pr-health", tier: "yaml" }, { name: "b_2" }, { name: "A" }])), ["pr-health", "b_2", "A"]);
});

test("a workflow whose name could change the generated command is not offered", () => {
  const hostile = ["", "a b", "a;b", "a$(id)", "a`id`", "a'b", "a\"b", "a/b", "..", "a\n", "é", "日本", "a&b", "a|b", "a>b", "*"];
  assert.deepEqual(flows.workflowChoices(hostile.map((name) => ({ name }))), []);
  assert.deepEqual(flows.workflowChoices([{ name: "a-b_C9" }, ...hostile.map((name) => ({ name }))]), ["a-b_C9"], "only a name made of letters, digits, _ and - gets through");
});

test("every name offered can be scheduled, and every name refused is one the prompt generator would refuse", () => {
  const all = ["pr-health", "a b", "x;y", "ok_1", "$(x)", "", "é"];
  const offered = flows.workflowChoices(all.map((name) => ({ name })));
  for (const n of offered) assert.doesNotThrow(() => flows.schedulePrompt(n), n);
  for (const n of all.filter((x) => !offered.includes(x))) assert.throws(() => flows.schedulePrompt(n), Error, n);
});

test("entries that are not workflows are skipped, and an answer that is not a list offers nothing", () => {
  assert.deepEqual(flows.workflowChoices([null, 5, "pr-health", [], {}, { name: 5 }, { name: null }, { nam: "x" }, { name: "ok" }]), ["ok"]);
  for (const bad of [undefined, null, {}, "pr-health", 5]) assert.deepEqual(flows.workflowChoices(bad), [], JSON.stringify(bad));
});

// ---- flowFormDirty -----------------------------------------------------------------------------------

test("a form is dirty when something typed differs from what it opened with, and clean when nothing does", () => {
  const initial = deepFreeze(flows.emptyFlowForm());
  assert.equal(flows.flowFormDirty({ ...initial }, initial), false);
  for (const k of Object.keys(initial)) assert.equal(flows.flowFormDirty({ ...initial, [k]: k === "kind" ? "workflow" : "x" }, initial), true, k);
});

test("typing and then deleting what was typed is clean again; a single space is dirty", () => {
  const initial = flows.flowFormOf(ROW());
  const typed = { ...initial, name: `${initial.name}x` };
  assert.equal(flows.flowFormDirty(typed, initial), true);
  assert.equal(flows.flowFormDirty({ ...typed, name: initial.name }, initial), false);
  assert.equal(flows.flowFormDirty({ ...initial, prompt: `${initial.prompt} ` }, initial), true);
  assert.equal(flows.flowFormDirty({ ...flows.emptyFlowForm(), prompt: " " }, flows.emptyFlowForm()), true);
});

test("Schedule a workflow: a form opened with a workflow chosen is clean until the user changes something", () => {
  const initial = flows.emptyFlowForm({ workflow: "pr-health" });
  assert.equal(flows.flowFormDirty({ ...initial }, initial), false);
  assert.equal(flows.flowFormDirty({ ...initial, workflow: "other" }, initial), true);
  assert.equal(flows.flowFormDirty(flows.emptyFlowForm(), initial), true, "and an empty form is not the form that was opened");
});

// ===================================================================================================
// Round 3: what a flow save attempt does, what its answer does to the form, why a check gave no answer,
// and the CLI named when a flow is too large to send. Spec: cao-window-flows "New flow and edit"
// (Create disabled, Validation errors, Duplicate on create, Parse-shape 422), cao-window-sessions
// "CAO data goes through exec" (Body too large for argument mode) and design D14.
// ===================================================================================================

const CFG = { machine: "cao", port: 9889, stdin: false };
const fakeExec = (reply = { stdout: "{}\n200", stderr: "", code: 0 }) => {
  const calls = [];
  const fn = async (cmd, args, opts) => { calls.push({ cmd, args, opts }); return reply; };
  fn.calls = calls;
  return fn;
};
const OPEN_GATE = { disabled: false, reason: null, text: null };
const BAD_STATE = { create: false, filled: true };
const GOOD_STATE = { create: true, filled: true };
const SELECTED_BANNER = "Fill in the name, schedule, profile and prompt first.";

// ---- FLOW_CLI --------------------------------------------------------------------------------------

test("a flow too large to send is written with 'cao schedule add'", () => {
  assert.equal(flows.FLOW_CLI, "cao schedule add");
});

test("Body too large for argument mode: a create or an edit over 120 KiB is refused before exec and names the flow CLI", async () => {
  const huge = { ...FIELDS(), prompt: "x".repeat(130000) };
  const exec = fakeExec();
  const created = await callCao(exec, CFG, { method: "POST", path: "/flows", body: flows.flowCreateBody(huge, {}), maxTime: 15, cli: flows.FLOW_CLI });
  const edited = await callCao(exec, CFG, { method: "PUT", path: flows.flowRoute("daily"), body: flows.flowUpdateBody(ROW(), huge), maxTime: 15, cli: flows.FLOW_CLI });
  assert.equal(exec.calls.length, 0, "nothing is sent");
  for (const o of [created, edited]) {
    assert.equal(o.kind, "too_large");
    assert.ok(o.message.includes("cao schedule add"), o.message);
  }
});

test("a flow just under the argument limit is sent, so the refusal is not earlier than the cap", async () => {
  const exec = fakeExec();
  const small = { ...FIELDS(), prompt: "x".repeat(100000) };
  const o = await callCao(exec, CFG, { method: "POST", path: "/flows", body: flows.flowCreateBody(small, {}), maxTime: 15, cli: flows.FLOW_CLI });
  assert.equal(exec.calls.length, 1);
  assert.equal(o.kind, "ok");
});

// ---- validateNote ----------------------------------------------------------------------------------

test("a check that could not be sent is never silent: a refused or too large validate gives a visible line with the reason", () => {
  for (const kind of ["too_large", "refused", "env_refused"]) {
    const note = flows.validateNote({ kind, message: `reason for ${kind}` });
    assert.equal(note, `reason for ${kind}`, kind);
  }
});

test("a refusal with no message still gives a line, and a message that is not text is shown as text", () => {
  for (const kind of ["too_large", "refused", "env_refused"]) {
    for (const message of [undefined, null]) {
      const note = flows.validateNote({ kind, message });
      assert.equal(typeof note, "string", `${kind} ${message}`);
      assert.ok(note.length > 0);
    }
  }
  assert.equal(flows.validateNote({ kind: "refused", message: 42 }), "42");
});

test("when CAO does not answer the check, the note says the checks could not run", () => {
  const note = flows.validateNote({ kind: "unreachable", stderr: "curl: (7)" });
  assert.match(note, /did not answer/);
  assert.match(note, /checks could not run/);
});

test("a server error from validate other than a missing route is shown with the server's text", () => {
  assert.equal(flows.validateNote({ kind: "error", status: 500, detail: "boom" }), "CAO could not check the schedule: boom");
  assert.match(flows.validateNote({ kind: "error", status: 422, detail: "x" }), /^CAO could not check the schedule: x$/);
  assert.equal(flows.validateNote({ kind: "error", status: 500 }), "CAO could not check the schedule: error", "no detail still gives a line");
  const msg = "<b>no</b> é 日本 ${x}";
  assert.ok(flows.validateNote({ kind: "error", status: 502, detail: msg }).endsWith(msg));
});

test("Validate route missing and answers with their own place have no note: 404, 405, ok, auth, approval, nothing", () => {
  for (const o of [{ kind: "error", status: 404, detail: "Not Found" }, { kind: "error", status: 405 }, { kind: "ok", status: 200, json: {} },
    { kind: "auth", status: 401, message: "m" }, { kind: "approval_required", status: 403 }, undefined, null, {}, { kind: "weird" }]) {
    assert.equal(flows.validateNote(o), null, JSON.stringify(o));
  }
});

test("a validate call that was refused before it was sent keeps Create off and says why", async () => {
  const huge = { ...FIELDS(), prompt: "x".repeat(130000) };
  const outcome = await callCao(fakeExec(), CFG, { method: "POST", path: "/flows/validate", body: flows.validateBody(huge), maxTime: 8 });
  assert.equal(outcome.kind, "too_large");
  const { status, checks } = flows.validateResult(outcome);
  assert.deepEqual([status, checks], [null, null]);
  assert.equal(flows.flowFormGate({ fields: huge, checks, checkedKey: flows.flowCheckKey(huge), routes: flows.routesState(status), gate: OPEN_GATE, sending: false }).create, false);
  assert.match(flows.validateNote(outcome), /too large to send from this window/);
});

// ---- flowSaveAttempt -------------------------------------------------------------------------------

test("under a gate nothing happens: no send, no confirmation, no check, whatever the form says", () => {
  for (const state of [GOOD_STATE, BAD_STATE, { create: false, filled: false }, undefined]) {
    for (const enable of [true, false, undefined]) {
      for (const checked of [true, false]) {
        for (const routesMissing of [true, false]) {
          assert.deepEqual(flows.flowSaveAttempt({ gate: { disabled: true, reason: "auth" }, state, enable, checked, routesMissing }), { action: "gated" });
        }
      }
    }
  }
});

test("a clean form sends the create, and Create and enable asks for confirmation first", () => {
  assert.deepEqual(flows.flowSaveAttempt({ gate: OPEN_GATE, state: GOOD_STATE, enable: false, checked: true, routesMissing: false }), { action: "send" });
  assert.deepEqual(flows.flowSaveAttempt({ gate: OPEN_GATE, state: GOOD_STATE, enable: true, checked: true, routesMissing: false }), { action: "confirm" });
  for (const enable of ["true", 1, "yes", {}, undefined, null]) assert.equal(flows.flowSaveAttempt({ gate: OPEN_GATE, state: GOOD_STATE, enable, checked: true }).action, "send", JSON.stringify(enable));
});

test("an older server without the validate route sends nothing and opens no dialog, for Create and for Create and enable", () => {
  for (const enable of [false, true]) {
    const r = flows.flowSaveAttempt({ gate: OPEN_GATE, state: BAD_STATE, enable, checked: false, routesMissing: true });
    assert.equal(r.action, "problems", `enable ${enable}`);
    assert.equal(r.runCheck, false, "an older server is not asked to check again");
  }
  for (const enable of [false, true, undefined]) {
    const r = flows.flowSaveAttempt({ gate: OPEN_GATE, state: GOOD_STATE, enable, checked: false, routesMissing: true });
    assert.ok(r.action !== "send" && r.action !== "confirm", `a form the gate let through still sends nothing when the route is missing (enable ${enable}): ${r.action}`);
  }
});


test("a 404 or 405 from POST /flows/validate leads to no POST /flows, from the answer to the press, for Create and for Create and enable", async () => {
  for (const status of [404, 405]) {
    const exec = fakeExec({ stdout: `{"detail":"Not Found"}\n${status}`, stderr: "", code: 0 });
    const fields = FIELDS();
    const outcome = await callCao(exec, CFG, { method: "POST", path: "/flows/validate", body: flows.validateBody(fields), maxTime: 8 });
    const { status: answered, checks } = flows.validateResult(outcome);
    assert.equal(answered, status);
    const routes = flows.routesState(answered);
    const state = flows.flowFormGate({ fields, checks, checkedKey: flows.flowCheckKey(fields), routes, gate: OPEN_GATE, sending: false });
    for (const enable of [false, true]) {
      const step = flows.flowSaveAttempt({ gate: OPEN_GATE, state, enable, routesMissing: routes.missing, sending: false });
      if (step.action === "send" || step.action === "confirm") await callCao(exec, CFG, { method: "POST", path: "/flows", body: flows.flowCreateBody(fields, { enable }), maxTime: 15 });
    }
    assert.deepEqual(exec.calls.map((c) => c.args.at(-1)), [`http://127.0.0.1:9889/flows/validate`], `status ${status}: only the validate call was made`);
  }
});

test("Validation errors: with a problem nothing is sent and nothing is confirmed, for Create and for Create and enable alike", () => {
  for (const enable of [true, false]) {
    for (const checked of [true, false]) assert.equal(flows.flowSaveAttempt({ gate: OPEN_GATE, state: BAD_STATE, enable, checked, routesMissing: false }).action, "problems", `${enable} ${checked}`);
  }
});

test("a click on a form that has not been checked yet runs a check, so the errors it gives can be shown", () => {
  for (const checked of [false, undefined]) assert.equal(flows.flowSaveAttempt({ gate: OPEN_GATE, state: BAD_STATE, enable: false, checked, routesMissing: false }).runCheck, true, String(checked));
});

test("with a problem on an older server whose form is not filled in, the banner says to fill it in; in other cases no banner", () => {
  const unfilled = { create: false, filled: false };
  assert.equal(flows.flowSaveAttempt({ gate: OPEN_GATE, state: unfilled, enable: false, checked: false, routesMissing: true }).banner, SELECTED_BANNER);
  assert.equal(flows.flowSaveAttempt({ gate: OPEN_GATE, state: BAD_STATE, enable: false, checked: false, routesMissing: true }).banner, null, "filled, so the problem is not a missing field");
  assert.equal(flows.flowSaveAttempt({ gate: OPEN_GATE, state: unfilled, enable: false, checked: false, routesMissing: false }).banner, null, "the server's errors show at the fields");
  assert.equal(flows.flowSaveAttempt({ gate: OPEN_GATE, state: GOOD_STATE, enable: false, checked: true, routesMissing: false }).banner, undefined, "no banner key on a send");
});

test("only a problem carries a banner or a check: a send, a confirmation and a gated click carry neither", () => {
  for (const r of [flows.flowSaveAttempt({ gate: OPEN_GATE, state: GOOD_STATE, enable: false }), flows.flowSaveAttempt({ gate: OPEN_GATE, state: GOOD_STATE, enable: true }), flows.flowSaveAttempt({ gate: { disabled: true }, state: BAD_STATE })]) {
    assert.deepEqual(Object.keys(r), ["action"]);
  }
});

test("with nothing known, nothing is sent: no arguments, no state or a state that is not a clean create is a problem", () => {
  for (const args of [undefined, {}, { gate: OPEN_GATE }, { gate: OPEN_GATE, state: {} }, { gate: OPEN_GATE, state: { create: "true" } }, { gate: OPEN_GATE, state: { create: 1 }, enable: true }]) {
    assert.equal(flows.flowSaveAttempt(args).action, "problems", JSON.stringify(args));
  }
  assert.notEqual(flows.flowSaveAttempt({ gate: { disabled: "yes" }, state: GOOD_STATE }).action, "gated", "only a gate that is exactly disabled blocks");
});

test("Validation errors end to end: the schedule error keeps Create off, focus goes to the schedule, and a new valid check of the new fields lets Create send", () => {
  const typed = FIELDS({ schedule: "61 * * * *" });
  const answer = ok({ valid: false, errors: [{ field: "schedule", message: "bad cron" }], next_runs: [] });
  const { checks } = flows.validateResult(answer);
  const gateOf = (fields, c, key) => flows.flowFormGate({ fields, checks: c, checkedKey: key, routes: flows.routesState(200), gate: OPEN_GATE, sending: false });
  const state = gateOf(typed, checks, flows.flowCheckKey(typed));
  const step = flows.flowSaveAttempt({ gate: OPEN_GATE, state, enable: false, checked: true, routesMissing: false });
  assert.equal(step.action, "problems");
  const shown = flows.shownErrors(flows.formErrorsOf(checks.byField), {}, true);
  assert.deepEqual(shown, { schedule: ["bad cron"] });
  assert.equal(flows.firstErrorField(shown), "schedule");
  // the user fixes the schedule: Create is still off until a check of the new fields comes back valid
  const fixed = FIELDS({ schedule: "0 9 * * *" });
  const stale = gateOf(fixed, checks, flows.flowCheckKey(typed));
  assert.equal(flows.flowSaveAttempt({ gate: OPEN_GATE, state: stale, enable: false, checked: false, routesMissing: false }).action, "problems");
  const fresh = flows.validateResult(ok({ valid: true, errors: [], next_runs: [] })).checks;
  const clean = gateOf(fixed, fresh, flows.flowCheckKey(fixed));
  assert.equal(flows.flowSaveAttempt({ gate: OPEN_GATE, state: clean, enable: false, checked: true, routesMissing: false }).action, "send");
});

test("Create disabled end to end: a clean form sends a body with enabled false; Create and enable asks, and its dialog names the flow", () => {
  const fields = flows.flowFields(FORM(), {});
  const state = flows.flowFormGate({ fields, checks: GOOD_CHECKS, checkedKey: flows.flowCheckKey(fields), routes: flows.routesState(200), gate: OPEN_GATE, sending: false });
  const send = flows.flowSaveAttempt({ gate: OPEN_GATE, state, enable: false, checked: true, routesMissing: false });
  assert.equal(send.action, "send");
  assert.equal(flows.flowCreateBody(fields, { enable: false }).enabled, false);
  const ask = flows.flowSaveAttempt({ gate: OPEN_GATE, state, enable: true, checked: true, routesMissing: false });
  assert.equal(ask.action, "confirm");
  const dialog = flows.flowDialogSpec("createAndEnable", fields.name);
  assert.ok(dialog.title.includes("daily"));
  assert.equal(dialog.defaultFocus, "cancel");
});

// ---- flowSaveEffects -------------------------------------------------------------------------------

test("Create disabled: a saved flow leaves the form and selects the flow the server named", () => {
  const r = flows.flowSaveOutcome(ok({ name: "daily", enabled: false }, 201), { mode: "create" });
  assert.deepEqual(flows.flowSaveEffects(r, { name: "typed", authText: AUTH }), { done: true, selectName: "daily", warning: null });
});

test("a saved flow with no name in the answer selects the name that was typed", () => {
  const r = flows.flowSaveOutcome(ok(undefined, 201), { mode: "create" });
  assert.deepEqual(flows.flowSaveEffects(r, { name: "typed", authText: AUTH }), { done: true, selectName: "typed", warning: null });
  assert.equal(flows.flowSaveEffects({ type: "saved", name: null, armed: false }, { name: "typed" }).selectName, "typed");
  assert.equal(flows.flowSaveEffects({ type: "saved", name: "server", armed: false }, { name: "typed" }).selectName, "server");
});

test("a server that ignored enabled false and armed the flow is said so, and only then", () => {
  const armed = flows.flowSaveEffects(flows.flowSaveOutcome(ok({ name: "daily", enabled: true }, 201), { mode: "create" }), { name: "daily", authText: AUTH });
  assert.equal(armed.done, true);
  assert.match(armed.warning, /ignored enabled: false/);
  assert.match(armed.warning, /created the schedule enabled/);
  assert.match(armed.warning, /disable it/);
  const chosen = flows.flowSaveEffects(flows.flowSaveOutcome(ok({ name: "daily", enabled: true }, 201), { mode: "create", enable: true }), { name: "daily" });
  assert.equal(chosen.warning, null, "Create and enable armed it on purpose");
  assert.equal(flows.flowSaveEffects(flows.flowSaveOutcome(ok({ name: "daily", enabled: true }), { mode: "edit" }), { name: "daily" }).warning, null, "an edit is never an armed create");
});

test("Duplicate on create: the form says a schedule with that name exists at the name, and nothing else changes", () => {
  const r = flows.flowSaveOutcome(err(409, { detail: "exists" }, "exists"), { mode: "create" });
  assert.deepEqual(flows.flowSaveEffects(r, { name: "daily", authText: AUTH }), { done: false, serverErrors: { name: ["a schedule with that name exists"] }, banner: null });
});

test("an edit's 409 is shown as the server's message in the banner, not as an error at the name", () => {
  const r = flows.flowSaveOutcome(err(409, { detail: "file is not in the flows folder" }, "file is not in the flows folder"), { mode: "edit" });
  assert.deepEqual(flows.flowSaveEffects(r, { name: "daily", authText: AUTH }), { done: false, serverErrors: {}, banner: "file is not in the flows folder" });
});

test("Parse-shape 422: the message shows at the name field", () => {
  const r = flows.flowSaveOutcome(err(422, { detail: [{ loc: ["body", "name"], msg: "Flow name must not contain '/'", type: "value_error" }] }), { mode: "create" });
  assert.deepEqual(flows.flowSaveEffects(r, { name: "a/b", authText: AUTH }), { done: false, serverErrors: { name: ["Flow name must not contain '/'"] }, banner: null });
});

test("Validation errors: the {errors} shape of a 422 lands at the form's fields, and messages for no field become the banner", () => {
  const r = flows.flowSaveOutcome(err(422, { detail: { errors: [{ field: "schedule", message: "bad cron" }, { field: "agent_profile", message: "empty" }, { message: "one" }, { message: "two" }] } }), { mode: "create" });
  assert.deepEqual(flows.flowSaveEffects(r, { name: "x" }), { done: false, serverErrors: { schedule: ["bad cron"], profile: ["empty"] }, banner: "one; two" });
  const none = flows.flowSaveOutcome(err(422, { detail: { errors: [{ field: "schedule", message: "bad cron" }] } }), { mode: "edit" });
  assert.equal(flows.flowSaveEffects(none, { name: "x" }).banner, null, "no general message, no banner");
});

test("a save that got no answer, or that failed, shows its text in the banner and keeps the form", () => {
  const lost = flows.flowSaveOutcome({ kind: "unreachable" }, { mode: "create" });
  const e = flows.flowSaveEffects(lost, { name: "x", authText: AUTH });
  assert.deepEqual([e.done, e.serverErrors, e.banner], [false, {}, lost.text]);
  assert.match(e.banner, /may exist/);
  const failed = flows.flowSaveEffects(flows.flowSaveOutcome(err(500, undefined, "boom"), { mode: "create" }), { name: "x" });
  assert.deepEqual([failed.done, failed.serverErrors, failed.banner], [false, {}, "boom"]);
});

test("an auth refusal of the save shows the auth text it was given, not the server's own", () => {
  const r = flows.flowSaveOutcome({ kind: "auth", status: 401, message: "from the server" }, { mode: "create" });
  const e = flows.flowSaveEffects(r, { name: "x", authText: AUTH });
  assert.deepEqual(e, { done: false, serverErrors: {}, banner: AUTH });
});

test("an effect that is not done selects nothing, and flowSaveEffects of nothing does not throw or claim success", () => {
  for (const r of [{ type: "conflict", text: "t" }, { type: "failed", text: "t" }, { type: "unreachable", text: "t" }, { type: "fields", errors: {}, general: [] }, undefined, null, {}]) {
    const e = flows.flowSaveEffects(r, { name: "x", authText: AUTH });
    assert.equal(e.done, false, JSON.stringify(r));
    assert.ok(!("selectName" in e));
  }
  assert.equal(flows.flowSaveEffects({ type: "saved", name: "a", armed: false }).done, true, "no options at all");
});

// Validation errors (spec cao-window-flows): "Create is dimmed and sends nothing until a check of the current
// fields says valid, and pressing it checks again and moves focus to the first error". Pressing a dimmed
// Create whose current fields were already checked must still run a check, since the server's answer may
// have changed (the name may be taken by now); flowSaveAttempt only checks when the fields are unchecked.
test("Validation errors: pressing a dimmed Create checks again, also when these exact fields were already checked", () => {
  const step = flows.flowSaveAttempt({ gate: OPEN_GATE, state: BAD_STATE, enable: false, checked: true, routesMissing: false });
  assert.equal(step.action, "problems");
  assert.equal(step.runCheck, true, "the check must run again; the focus goes to the first error afterwards");
  assert.equal(flows.flowSaveAttempt({ gate: OPEN_GATE, state: BAD_STATE, enable: true, checked: true, routesMissing: false }).runCheck, true, "also for Create and enable");
});

await finish();
