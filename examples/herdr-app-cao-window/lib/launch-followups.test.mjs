// lib/launch.js: the follow-ups to the launch outcomes that lib/launch.test.mjs leaves open:
// which field a 400 belongs to, a new key after a failed keyed create, and what a lost response says.
import { assert, finish, test } from "../test/harness.mjs";
import * as launch from "./launch.js";

const form = (over = {}) => ({ ...launch.emptyForm("/work/repo"), profile: "developer", ...over });
const err = (status, detail, extra = {}) => ({ kind: "error", status, detail, ...extra });

test("a 400 goes to the field its detail names: working directory, profile, message, session name", () => {
  const field = (detail) => launch.launchOutcome(err(400, detail), form()).field;
  assert.equal(field("invalid resume_session_id: expected 8-64 chars of [A-Za-z0-9._-] starting with an alphanumeric"), "form", "the window sends no resume id, so no field is its");
  assert.equal(field("Invalid session_name: 'a b'"), "sessionName");
  assert.equal(field("Working directory does not exist: /nowhere"), "workdir");
  assert.equal(field("initial_message must not be empty"), "message");
  assert.equal(field("Agent profile 'x' not found"), "profile");
});

test("a 400 that names no field is a form-level message and is not put on the session name", () => {
  const o = launch.launchOutcome(err(400, "something the window has not seen"), form());
  assert.deepEqual([o.type, o.field, o.message], ["field", "form", "something the window has not seen"]);
});

test("a failed keyed create (424 on the replay) ends the submission, so the next Launch gets a new key", () => {
  let n = 0;
  const submission = launch.withKey(form(), () => `key-${++n}`);
  const o = launch.launchOutcome(err(424, "provider failed to start", { json: { detail: "provider failed to start", terminal_id: "t9" } }), submission);
  assert.deepEqual([o.type, o.resetKey, o.terminalId], ["failed", true, "t9"]);
  assert.equal(launch.keepSubmission(submission, o), null);
  const next = launch.withKey(launch.keepSubmission(submission, o) ?? form(), () => `key-${++n}`);
  assert.equal(next.idempotencyKey, "key-2");
});

test("only a lost response keeps its submission, with its key, for Retry", () => {
  const submission = launch.withKey(form(), () => "k1");
  const lost = launch.launchOutcome({ kind: "unreachable" }, submission);
  assert.equal(launch.keepSubmission(submission, lost), submission);
  for (const outcome of [{ kind: "ok", status: 201, json: { session_name: "cao-s" } }, err(400, "x"), err(429, "limit"), err(500, "boom"), { kind: "auth", status: 401 }]) {
    assert.equal(launch.keepSubmission(submission, launch.launchOutcome(outcome, submission)), null, JSON.stringify(outcome));
  }
});

test("a lost response says the session may have started and names the session to look for", () => {
  const named = launch.launchOutcome({ kind: "unreachable" }, form({ sessionName: "s1", idempotencyKey: "k1" }));
  assert.match(named.message, /may have started/);
  assert.equal(named.lookupSession, "cao-s1");
  assert.equal(launch.launchOutcome({ kind: "unreachable" }, form({ sessionName: "cao-s2", idempotencyKey: "k1" })).lookupSession, "cao-s2", "the server does not prefix twice");
  assert.equal(launch.launchOutcome({ kind: "unreachable" }, form({ idempotencyKey: "k1" })).lookupSession, null, "an unnamed session has no name to look up");
});

test("YOLO on a provider the profile cannot narrow still reads as every tool, and on kiro_cli does not", () => {
  const dev = { name: "developer" };
  assert.match(launch.yoloDialog(dev, "claude_code").title, /every tool/);
  assert.doesNotMatch(launch.yoloDialog(dev, "kiro_cli").title, /every tool|all tools|unrestricted/i);
});

await finish();
