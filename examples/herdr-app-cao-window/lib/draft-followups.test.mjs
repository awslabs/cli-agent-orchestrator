// lib/draft.js: the follow-ups to lib/draft.test.mjs: an answer for an earlier attempt is
// ignored, validation may answer while the session is being deleted, a lost launch response names the
// session that may exist, and a failed Fix keeps the errors so it can be asked again.
import { assert, finish, test } from "../test/harness.mjs";
import * as draft from "./draft.js";

const BASE = { kind: "flow", description: "Report open reviews", profile: "author", provider: "claude_code", workdir: "/work/repo", idempotencyKey: "k1", hex: "0a1b2c3d" };
const request = (over = {}) => draft.draftRequest({ ...BASE, ...over });
const GOOD = `\`\`\`json\n${JSON.stringify({ name: "daily", schedule: "0 9 * * mon-fri", agent_profile: "developer", provider: "claude_code", prompt_template: "Report." })}\n\`\`\``;
const created = (id, name) => ({ kind: "ok", status: 201, json: { id, session_name: name } });
const types = (fx) => fx.map((e) => e.type);

// attempt 1 is cancelled while its output is being read and deleted; attempt 2 is then started and is
// reading too, and attempt 1's late output arrives.
function secondAttemptReading() {
  let s = draft.initialDraft("flow");
  [s] = draft.draftStep(s, { type: "start", request: request() }, 0);
  [s] = draft.draftStep(s, { type: "launched", attempt: 1, outcome: created("t1", "cao-draft-flow-0a1b2c3d") }, 10);
  [s] = draft.draftStep(s, { type: "terminal", terminal: "t1", state: "done", turn: 1 }, 20);
  [s] = draft.draftStep(s, { type: "cancel" }, 30);
  [s] = draft.draftStep(s, { type: "deleted", attempt: 1, ok: true }, 40);
  const second = request({ hex: "11112222", idempotencyKey: "k2" });
  [s] = draft.draftStep(s, { type: "start", request: second }, 50);
  [s] = draft.draftStep(s, { type: "launched", attempt: 2, outcome: created("t2", second.sessionName) }, 60);
  [s] = draft.draftStep(s, { type: "terminal", terminal: "t2", state: "done", turn: 1 }, 70);
  return s;
}

test("each start is its own attempt, and the launch effect says which", () => {
  let [s, fx] = draft.draftStep(draft.initialDraft("flow"), { type: "start", request: request() }, 0);
  assert.deepEqual([s.attempt, fx[0].attempt], [1, 1]);
  [s] = draft.draftStep(s, { type: "launched", outcome: { kind: "error", status: 400, detail: "x" } }, 5);
  [s, fx] = draft.draftStep(s, { type: "start", request: request({ hex: "33334444", idempotencyKey: "k3" }) }, 10);
  assert.deepEqual([s.attempt, fx[0].attempt], [2, 2]);
});

test("a late output of an earlier attempt neither fills the form nor ends the next attempt's session", () => {
  const s = secondAttemptReading();
  assert.deepEqual([s.phase, s.attempt, s.terminalId], ["reading", 2, "t2"]);
  const [stale, fx] = draft.draftStep(s, { type: "output", attempt: 1, ok: true, text: GOOD }, 80);
  assert.deepEqual([stale, fx], [s, []]);
  const [, current] = draft.draftStep(s, { type: "output", attempt: 2, ok: true, text: GOOD }, 90);
  assert.deepEqual(types(current), ["fill", "delete"]);
  const [, untagged] = draft.draftStep(s, { type: "output", ok: true, text: GOOD }, 90);
  assert.deepEqual(types(untagged), ["fill", "delete"], "an event with no attempt is taken as current");
});

test("a late deleted, launched or validated of an earlier attempt changes nothing", () => {
  const s = secondAttemptReading();
  for (const ev of [{ type: "deleted", attempt: 1, ok: true }, { type: "launched", attempt: 1, outcome: created("t9", "cao-draft-flow-ffffffff") }, { type: "validated", attempt: 1, errors: ["old"] }]) {
    assert.deepEqual(draft.draftStep(s, ev, 100), [s, []], ev.type);
  }
  let [cleaning] = draft.draftStep(s, { type: "cancel" }, 100);
  assert.deepEqual(draft.draftStep(cleaning, { type: "deleted", attempt: 1, ok: true }, 110), [cleaning, []], "an earlier attempt's delete answer does not end this cleanup");
  [cleaning] = draft.draftStep(cleaning, { type: "deleted", attempt: 2, ok: true }, 120);
  assert.equal(cleaning.phase, "idle");
});

test("validation may answer while the session is still being deleted, and its errors are kept", () => {
  let s = secondAttemptReading();
  [s] = draft.draftStep(s, { type: "output", attempt: 2, ok: true, text: GOOD }, 80);
  assert.equal(s.phase, "cleaning");
  [s] = draft.draftStep(s, { type: "validated", attempt: 2, errors: ["schedule: bad cron"] }, 85);
  assert.deepEqual(s.errors, ["schedule: bad cron"]);
  let fx;
  [s, fx] = draft.draftStep(s, { type: "deleted", attempt: 2, ok: true }, 90);
  assert.deepEqual([s.phase, s.errors, types(fx)], ["done", ["schedule: bad cron"], ["forget"]]);
  assert.ok(draft.draftBox(s).actions.includes("Fix with AI"));
});

test("a validated answer while an author is out or running is not an answer to anything and is ignored", () => {
  let [s] = draft.draftStep(draft.initialDraft("flow"), { type: "start", request: request() }, 0);
  assert.deepEqual(draft.draftStep(s, { type: "validated", errors: ["x"] }, 5), [s, []]);
});

test("a lost launch response says the session may have started and names it, so it can be deleted by hand", () => {
  let [s] = draft.draftStep(draft.initialDraft("flow"), { type: "start", request: request() }, 0);
  [s] = draft.draftStep(s, { type: "launched", outcome: { kind: "unreachable" } }, 10);
  assert.equal(s.phase, "failed");
  assert.match(s.notice, /may have started/);
  assert.ok(s.notice.includes("cao-draft-flow-0a1b2c3d"));
});

test("a Fix that fails to launch keeps the errors, so Fix with AI can be asked again", () => {
  let s = secondAttemptReading();
  [s] = draft.draftStep(s, { type: "output", attempt: 2, ok: true, text: GOOD }, 80);
  [s] = draft.draftStep(s, { type: "validated", attempt: 2, errors: ["name: bad"] }, 82);
  [s] = draft.draftStep(s, { type: "deleted", attempt: 2, ok: true }, 84);
  const fix = draft.fixRequest({ ...BASE, hex: "55556666", idempotencyKey: "k5" }, { kind: "flow", fields: { name: "daily" } }, s.errors);
  let fx;
  [s, fx] = draft.draftStep(s, { type: "fix", request: fix }, 90);
  assert.equal(fx[0].attempt, 3);
  [s] = draft.draftStep(s, { type: "launched", attempt: 3, outcome: { kind: "error", status: 400, detail: "no" } }, 95);
  assert.deepEqual([s.phase, s.errors], ["failed", ["name: bad"]]);
  assert.deepEqual(draft.draftBox(s).actions, ["Retry", "Fix with AI"]);
});

test("a start begins with no errors, and a fill clears the old ones until the new validation answers", () => {
  let s = secondAttemptReading();
  s = { ...s, errors: ["old"] };
  [s] = draft.draftStep(s, { type: "output", attempt: 2, ok: true, text: GOOD }, 80);
  assert.deepEqual(s.errors, []);
});

test("a cancelled launch still counts as busy, so a second Draft cannot orphan the first author session", () => {
  let [s] = draft.draftStep(draft.initialDraft("flow"), { type: "start", request: request() }, 0);
  [s] = draft.draftStep(s, { type: "cancel" }, 5);
  const [after, fx] = draft.draftStep(s, { type: "start", request: request({ hex: "77778888", idempotencyKey: "k7" }) }, 6);
  assert.deepEqual([after, fx], [s, []]);
  assert.deepEqual(draft.draftBox(s).actions, []);
  const [cleaning, late] = draft.draftStep(s, { type: "launched", attempt: 1, outcome: created("t1", "cao-draft-flow-0a1b2c3d") }, 8);
  assert.deepEqual([cleaning.phase, types(late)], ["cleaning", ["record", "delete"]]);
});

test("a draft's session name, a prototype name or a hex that is not 8 lowercase hex digits never reaches a request", () => {
  for (const kind of ["__proto__", "constructor", "toString"]) assert.throws(() => request({ kind }), /kind/, kind);
  assert.deepEqual(draft.parseDraft("__proto__", { body: "{\"name\":\"a\"}" }), { ok: false, reason: "unknown kind" });
});

test("applyDraft puts a flow's prompt_template in the form's prompt field, and ignores keys the kind does not name", () => {
  const r = draft.applyDraft({ prompt: "typed", name: "" }, { kind: "flow", fields: { prompt_template: "drafted", enabled: "true", script: "/x" } });
  assert.deepEqual(r.form, { prompt: "drafted", name: "" });
  assert.deepEqual(r.marks, { prompt: "AI" });
  assert.deepEqual(draft.applyDraft({ a: 1 }, { kind: "nope", fields: { a: "x" } }), { form: { a: 1 }, marks: {} });
});

test("a 404 on the delete after a normal draft counts as deleted: the id is forgotten and nothing is reported by hand", () => {
  let s = secondAttemptReading();
  [s] = draft.draftStep(s, { type: "output", attempt: 2, ok: true, text: GOOD }, 80);
  let fx;
  [s, fx] = draft.draftStep(s, { type: "deleted", attempt: 2, ok: false, status: 404 }, 90);
  assert.deepEqual([s.phase, types(fx), s.notice], ["done", ["forget"], null]);
});

test("a lost launch answer, then Retry: the new attempt starts and carries no stale lost flag", () => {
  let [s] = draft.draftStep(draft.initialDraft("flow"), { type: "start", request: request() }, 0);
  [s] = draft.draftStep(s, { type: "launched", attempt: 1, outcome: { kind: "unreachable" } }, 10);
  assert.equal(s.lostLaunch, true);
  let fx;
  [s, fx] = draft.draftStep(s, { type: "start", request: request({ hex: "99990000", idempotencyKey: "k9" }) }, 20);
  assert.deepEqual([s.phase, s.lostLaunch, types(fx).filter((t) => t === "launch")], ["launching", false, ["launch"]]);
});

await finish();
