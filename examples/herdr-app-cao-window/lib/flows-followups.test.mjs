// lib/flows.js: the follow-ups to lib/flows.test.mjs: Edit and the name the route calls
// taken, Run now when the answer never comes, and an answer the window cannot read as a run.
import { assert, finish, test } from "../test/harness.mjs";
import * as flows from "./flows.js";

const TAKEN = { field: "name", message: "a flow with this name already exists" };

test("Edit: the route calls the flow's own name taken, and Save is still allowed when that is the only error", () => {
  const answer = { valid: false, errors: [TAKEN], next_runs: ["2026-11-19T09:00:00-08:00"] };
  const edit = flows.flowChecksFromValidate(answer, { editing: "daily", name: "daily" });
  assert.deepEqual([edit.byField, edit.canCreate, edit.nextRuns.length], [{}, true, 1]);
  assert.equal(flows.flowChecksFromValidate(answer, { editing: "daily" }).canCreate, true, "an edit form cannot rename, so no name means the same name");
});

test("Create still refuses a taken name, and Edit does not hide any other error", () => {
  const answer = { valid: false, errors: [TAKEN], next_runs: [] };
  assert.deepEqual(flows.flowChecksFromValidate(answer).byField, { name: ["a flow with this name already exists"] });
  assert.equal(flows.flowChecksFromValidate(answer).canCreate, false);
  assert.equal(flows.flowChecksFromValidate(answer, { editing: "daily", name: "other" }).canCreate, false, "a different name is a real clash");
  const also = flows.flowChecksFromValidate({ valid: false, errors: [TAKEN, { field: "schedule", message: "bad cron" }], next_runs: [] }, { editing: "daily", name: "daily" });
  assert.deepEqual([also.byField, also.canCreate], [{ schedule: ["bad cron"] }, false]);
  assert.equal(flows.flowChecksFromValidate({ valid: false, errors: [{ field: "schedule", message: "x" }], next_runs: [] }, { editing: "daily" }).canCreate, false);
});

test("Run now with no answer says the flow may still be running; Enable and Disable do not", () => {
  const run = flows.writeOutcome("run", { kind: "unreachable" });
  assert.match(run.text, /may still be running/);
  assert.match(run.text, /cao-flow/);
  assert.equal(run.keepSwitch, true);
  assert.doesNotMatch(flows.writeOutcome("enable", { kind: "unreachable" }).text, /still be running/);
});

test("an answer that is not a plain executed true or false is unclear, and a flow action that does not exist has no outcome", () => {
  const o = flows.writeOutcome("run", { kind: "ok", status: 200, json: { executed: "true" } });
  assert.equal(o.type, "unclear");
  assert.match(o.text, /not whether the schedule ran/);
  assert.throws(() => flows.writeOutcome("delete", { kind: "ok", status: 200, json: {} }));
});

test("a field name the server sends cannot write through the prototype", () => {
  const r = flows.flowChecksFromValidate({ valid: false, errors: [{ field: "__proto__", message: "x" }], next_runs: [] });
  assert.deepEqual(r.byField, { form: ["x"] });
  assert.equal(Object.getPrototypeOf(r.byField), Object.prototype);
  assert.deepEqual(Object.keys(flows.fieldErrorsFrom422({ detail: [{ loc: ["body", "__proto__"], msg: "m" }] }).byField), ["form"]);
});

await finish();
