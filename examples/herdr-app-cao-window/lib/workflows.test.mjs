// lib/workflows.js: the Workflows section's rules (spec cao-window-workflows; honesty label
// inputsUndescribed of cao-window-honesty-labels). Task 11.1. The two templates of 11.4 are checked
// against the real validate route by test/examples/test_herdr_app_cao_window_templates.py.
//
// Server caps mirrored here: SCRIPT_LINT_MAX_SOURCE_CHARS = 128 * 1024 characters (constants.py:1216)
// and WORKFLOW_MAX_SPEC_BYTES = 256 * 1024 bytes (constants.py:949). Python counts characters, so an
// astral character is one, not two UTF-16 units.
//
// Every function is called inside a test case through the namespace, so a function that does not exist
// yet fails its own case with a TypeError, and the rest of the file still runs.
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { assert, finish, test } from "../test/harness.mjs";
import { HARD_WORDS, globDir, needsQuotes, quotedCount, shellArgv } from "../test/shell-argv.mjs";
import { ARG_BODY_MAX, bodyBytes, curlRequest, outcomeFrom } from "./cao.js";
import * as flows from "./flows.js";
import { HONESTY_PENDING } from "./honesty.js";
import * as wf from "./workflows.js";

const here = dirname(fileURLToPath(import.meta.url));
const KIB = 1024;
const UNDESCRIBED = "inputs not described by the spec";

const run = (state, over = {}) => ({ run_id: "r1", workflow_name: "wf", state, started_at: "2026-11-19T10:00:00Z", current_step_id: null, ...over });
const approval = (planId = "wf:abc123") => outcomeFrom({
  stdout: `${JSON.stringify({ detail: { kind: "approval_required", plan_id: planId, message: "approve it" } })}\n403`,
});
const pass = { status: "pass", errors: [], findings: [] };
const CFG = { machine: "cao", port: 9889, stdin: false };

// ---- row state and polling ------------------------------------------------------------------------

test("No workflows and never run: a workflow with no run reads 'never run'", () => {
  assert.equal(wf.workflowRowState([]), "never run");
});

test("the row shows the state of its newest run in the spec's words", () => {
  assert.equal(wf.workflowRowState([run("running")]), "running");
  assert.equal(wf.workflowRowState([run("completed")]), "succeeded");
  assert.equal(wf.workflowRowState([run("failed")]), "failed");
  assert.equal(wf.workflowRowState([run("cancelled")]), "cancelled");
});

test("the newest run wins whatever order the server lists them in, and the list is not reordered in place", () => {
  const older = run("failed", { run_id: "a", started_at: "2026-11-19T10:00:00Z" });
  const newer = run("completed", { run_id: "b", started_at: "2026-11-19T11:00:00Z" });
  for (const list of [[older, newer], [newer, older]]) assert.equal(wf.workflowRowState(list), "succeeded");
  const list = [older, newer];
  wf.workflowRowState(list);
  assert.deepEqual(list.map((r) => r.run_id), ["a", "b"]);
});

test("an unknown run state, or missing runs, does not throw and still gives words", () => {
  assert.equal(typeof wf.workflowRowState([run("paused")]), "string");
  assert.equal(wf.workflowRowState(null), "never run");
  assert.equal(wf.workflowRowState(undefined), "never run");
});

test("Active run speeds up polling: 60 s normally, 5 s while any listed run is running", () => {
  assert.deepEqual([wf.WORKFLOW_POLL_MS, wf.WORKFLOW_FAST_POLL_MS], [60_000, 5_000]);
  assert.equal(wf.pollInterval([run("completed")]), 60_000);
  assert.equal(wf.pollInterval([run("running")]), 5_000);
  assert.equal(wf.pollInterval([run("completed"), run("failed", { run_id: "x" }), run("running", { run_id: "y" })]), 5_000);
  assert.equal(wf.pollInterval([]), 60_000, "back to 60 s once none is running");
  assert.equal(wf.pollInterval(null), 60_000);
  assert.equal(wf.pollInterval(undefined), 60_000);
});

// ---- details: mode, not tier; actions per run ----------------------------------------------------

test("the details show description, mode and step count from the list row, never the tier or the server's file path", () => {
  const row = { name: "pr-health", source_path: "/srv/cao/workflows/pr-health.py", mode: "sequential", step_count: 3, description: "Check PR health", indexed_at: "2026-11-19T10:00:00Z", tier: "python" };
  const d = wf.workflowDetails(row);
  assert.deepEqual([d.description, d.mode, d.stepCount], ["Check PR health", "sequential", 3]);
  assert.ok(!("tier" in d), "the details say mode, not tier");
  assert.ok(!JSON.stringify(d).includes("/srv/cao"), "source_path is the server's file layout and is not shown");
  assert.ok(!("source_path" in d));
});

test("details of a sparse row give empty values, not 'undefined'", () => {
  const d = wf.workflowDetails({ name: "x" });
  assert.ok(!JSON.stringify(d).includes("undefined"));
  assert.equal(d.stepCount === null || d.stepCount === 0, true);
});

test("Cancel is offered for a running run and Resume for a cancelled one, and for nothing else", () => {
  assert.deepEqual(wf.runActions(run("running")), { cancel: true, resume: false });
  assert.deepEqual(wf.runActions(run("cancelled")), { cancel: false, resume: true });
  for (const state of ["completed", "failed", "paused", "", undefined]) assert.deepEqual(wf.runActions(run(state)), { cancel: false, resume: false }, String(state));
});

// ---- plan approval -------------------------------------------------------------------------------

test("First run needs approval: the plan id, the approve command, and a local 'not submitted' row marked 'approval needed'", () => {
  const s = wf.approvalState(null, approval("wf:abc123"));
  assert.equal(s.needed, true);
  assert.equal(s.planId, "wf:abc123");
  assert.equal(s.command, "cao workflow approve wf:abc123");
  assert.deepEqual(s.localRun, { status: "not submitted", note: "approval needed" });
  assert.equal(s.headline, "Plan approval needed. Approve it in a terminal");
  assert.equal(s.submitAgainLabel, "I approved it: submit again");
});

test("the approve command is safe to paste into a shell whatever the server sends as a plan id", () => {
  for (const planId of ["wf:abc123", "p'1 x", "a;echo INJECTED", "$(echo INJECTED)", "`echo INJECTED`", "x\ny", "é 日本"]) {
    const s = wf.approvalState(null, approval(planId));
    assert.deepEqual(shellArgv(s.command, "cao"), ["workflow", "approve", planId], JSON.stringify(planId));
  }
});

test("a plan id that looks like an option cannot be read as one by the cao command", () => {
  const s = wf.approvalState(null, approval("--help"));
  if (s.command === null) return;
  const argv = shellArgv(s.command, "cao");
  assert.deepEqual(argv.slice(-2), ["--", "--help"]);
});

test("a refusal with no usable plan id still says approval is needed but offers no command that approves nothing", () => {
  for (const planId of [null, undefined, "", 7, {}]) {
    const s = wf.approvalState(null, { kind: "approval_required", status: 403, planId });
    assert.equal(s.needed, true, JSON.stringify(planId));
    assert.equal(s.command, null, JSON.stringify(planId));
  }
});

test("the 'not submitted' row stays until the next submit: another failure keeps it, a 202 replaces it", () => {
  const first = wf.approvalState(null, approval());
  assert.equal(wf.approvalState(first, { kind: "error", status: 500, detail: "x" }), first);
  assert.equal(wf.approvalState(first, { kind: "unreachable" }), first);
  assert.equal(wf.approvalState(first, { kind: "ok", status: 202, json: { run_id: "r9" } }), null, "Submit again after approval");
  assert.equal(wf.approvalState(null, { kind: "error", status: 500 }), null);
  const second = wf.approvalState(first, approval("wf:other"));
  assert.equal(second.planId, "wf:other", "a new refusal replaces the old plan id");
});

test("a 403 that is not an approval refusal is an auth failure and never reaches the approval panel", () => {
  const o = outcomeFrom({ stdout: `${JSON.stringify({ detail: "forbidden" })}\n403` });
  assert.equal(o.kind, "auth");
  assert.equal(wf.approvalState(null, o), null);
});

test("No approve control: no function in lib or in a mod builds POST /workflows/plans/approve", () => {
  const files = [];
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const p = join(dir, name);
      if (statSync(p).isDirectory()) { if (name !== "node_modules") walk(p); } else if (p.endsWith(".js")) files.push(p);
    }
  };
  walk(here);
  const mods = join(here, "..", "mods");
  if (existsSync(mods)) walk(mods);
  assert.ok(files.length >= 5, `scanned ${files.length} files`);
  for (const f of files) {
    const code = readFileSync(f, "utf8").replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
    assert.ok(!/plans\/approve/.test(code), `${f} builds the approve route`);
    assert.ok(!/["'`]\/?workflows\/plans/.test(code), `${f} builds a workflows/plans route`);
  }
});

// ---- the run request ----------------------------------------------------------------------------

test("Run submits POST /workflows/runs:submit with a bare workflow name, the inputs and an optional working directory", () => {
  const r = wf.submitRequest("wf", { x: "1", n: 3, ok: true }, "/work/repo");
  assert.deepEqual([r.method, r.path], ["POST", "/workflows/runs:submit"]);
  assert.deepEqual(r.body, { name_or_path: "wf", inputs: { x: "1", n: 3, ok: true }, working_directory: "/work/repo" });
  assert.match(r.cli, /^cao workflow/);
  assert.deepEqual(wf.submitRequest("wf", {}).body, { name_or_path: "wf", inputs: {} });
  assert.ok(!("working_directory" in wf.submitRequest("wf", {}, "").body));
});

test("a workflow is never submitted by path: anything but a bare name is refused", () => {
  for (const bad of ["../x", "a/b.yaml", "/etc/passwd", "a b", "wf.py", "wf.yaml", "", " wf", "wf\n", "wf;rm", "é", "a\u0000b", undefined, null, 5]) {
    assert.throws(() => wf.submitRequest(bad, {}), /bare/, JSON.stringify(bad));
  }
});

test("the same submission twice is the same request, so 'submit again' sends the same inputs", () => {
  assert.deepEqual(wf.submitRequest("wf", { a: "1" }, "/w"), wf.submitRequest("wf", { a: "1" }, "/w"));
});

test("inputs that make the body too large are refused before exec, naming the CLI", () => {
  const built = curlRequest(CFG, wf.submitRequest("wf", { big: "x".repeat(ARG_BODY_MAX) }));
  assert.equal(built.ok, false);
  assert.equal(built.outcome.kind, "too_large");
  assert.match(built.outcome.message, /cao workflow/);
});

// ---- typed inputs --------------------------------------------------------------------------------

test("inputsForm types the fields from the spec, keeps their order, and carries no label when the spec describes them", () => {
  const typed = wf.inputsForm({ inputs: { mode: { type: "string", required: true, default: "dry_run" }, n: { type: "int" }, ok: { type: "bool" }, p: { type: "path" } } });
  assert.deepEqual(typed.fields.map((f) => [f.name, f.type, f.required]), [["mode", "string", true], ["n", "int", false], ["ok", "bool", false], ["p", "path", false]]);
  assert.equal(typed.fields[0].default, "dry_run");
  assert.deepEqual([typed.free, typed.label], [false, null]);
});

test("a workflow that declares no inputs has no rows and no fallback", () => {
  assert.deepEqual(wf.inputsForm({ inputs: {} }), { fields: [], free: false, label: null });
});

test("Inputs not readable: an absent, malformed or unknown-typed declaration falls back to free rows labelled as undescribed", () => {
  for (const bad of [{}, null, undefined, { inputs: "x" }, { inputs: [] }, { inputs: null }, { inputs: { a: { type: "weird" } } }, { inputs: { a: null } },
    { inputs: { a: {} } }, { inputs: { ok: { type: "string" }, bad: { type: "float" } } }]) {
    const f = wf.inputsForm(bad);
    assert.deepEqual([f.free, f.label], [true, UNDESCRIBED], JSON.stringify(bad));
  }
});

test("coerceInputs converts typed values and reports bad ones per field", () => {
  const fields = wf.inputsForm({ inputs: { n: { type: "int", required: true }, ok: { type: "bool" }, s: { type: "string" }, p: { type: "path" } } }).fields;
  assert.deepEqual(wf.coerceInputs(fields, { n: "3", ok: "true", s: "x", p: "/a/b" }), { inputs: { n: 3, ok: true, s: "x", p: "/a/b" }, errors: {} });
  assert.deepEqual(Object.keys(wf.coerceInputs(fields, { n: "x" }).errors), ["n"]);
  assert.deepEqual(Object.keys(wf.coerceInputs(fields, {}).errors), ["n"], "a required field that is empty");
  assert.deepEqual(Object.keys(wf.coerceInputs(fields, { n: "1", ok: "maybe" }).errors), ["ok"]);
  assert.deepEqual(wf.coerceInputs(fields, { n: "1" }).inputs, { n: 1 }, "an empty optional field is left out, not sent as an empty string");
  assert.equal(wf.coerceInputs(fields, { n: "-4" }).inputs.n, -4);
});

test("an int must be a whole number the window can carry exactly", () => {
  const fields = wf.inputsForm({ inputs: { n: { type: "int" } } }).fields;
  for (const bad of ["3.0", "1e3", "0x10", " 3", "3 ", "+", "-", "٣", "９", "99999999999999999999", "NaN", "Infinity"]) {
    assert.deepEqual(Object.keys(wf.coerceInputs(fields, { n: bad }).errors), ["n"], JSON.stringify(bad));
  }
  assert.equal(wf.coerceInputs(fields, { n: "0" }).inputs.n, 0);
});

test("a typed field that is required cannot be satisfied by an inherited object property such as constructor", () => {
  const fields = wf.inputsForm({ inputs: { constructor: { type: "string", required: true }, toString: { type: "string" } } }).fields;
  const r = wf.coerceInputs(fields, {});
  assert.deepEqual(Object.keys(r.errors), ["constructor"]);
  assert.deepEqual(Object.keys(r.inputs), []);
});

test("values for fields the spec does not declare are not forwarded", () => {
  const fields = wf.inputsForm({ inputs: { a: { type: "string" } } }).fields;
  assert.deepEqual(wf.coerceInputs(fields, { a: "1", sneaky: "2", __proto__: { z: 1 } }).inputs, { a: "1" });
});

// ---- Scheduled by ---------------------------------------------------------------------------------

test("Schedule link: scheduledBy lists the flows whose prompt runs this workflow, with schedule and enabled state", () => {
  const list = [
    { name: "a", schedule: "0 9 * * mon-fri", enabled: false, prompt_template: "Run it: cao workflow run pr-health --input mode=dry_run" },
    { name: "b", schedule: "0 9 * * *", enabled: true, prompt_template: "cao workflow run pr-health-extra" },
    { name: "c", schedule: "0 8 * * *", enabled: true, prompt_template: "cao workflow run pr-health\nthen stop" },
    { name: "d", schedule: "0 8 * * *", enabled: true, prompt_template: "something else" },
  ];
  const got = wf.scheduledBy(list, "pr-health");
  assert.deepEqual(got.map((f) => f.name), ["a", "c"]);
  assert.deepEqual(got.map((f) => [f.schedule, f.enabled]), [["0 9 * * mon-fri", false], ["0 8 * * *", true]]);
});

test("scheduledBy is exact about the name and safe about odd input", () => {
  const list = [{ name: "x", schedule: "x", enabled: true, prompt_template: "cao workflow run abc" }, { name: "n", schedule: "x", enabled: true, prompt_template: null },
    { name: "m", schedule: "x", enabled: true }, { name: "s", schedule: "x", enabled: true, prompt_template: "cao workflow run a-b" }];
  assert.deepEqual(wf.scheduledBy([{ name: "d", schedule: "x", enabled: true, prompt_template: "cao workflow run aXb" }], "a.b"), [], "a name is not a pattern");
  assert.deepEqual(wf.scheduledBy(list, "a"), [], "a prefix of another name is not a match");
  assert.deepEqual(wf.scheduledBy(list, "a-b").map((f) => f.name), ["s"]);
  assert.deepEqual(wf.scheduledBy(list, "A-B"), [], "names are case-sensitive");
  assert.deepEqual(wf.scheduledBy(list, ""), []);
  for (const bad of [[], null, undefined]) assert.deepEqual(wf.scheduledBy(bad, "x"), []);
});

test("Schedule link: a flow created from Schedule... is listed as scheduling its workflow, whatever the inputs hold", () => {
  const prompt = flows.schedulePrompt("pr-health", { mode: "dry_run", note: "it's $(x) ; y" });
  const got = wf.scheduledBy([{ name: "weekly", schedule: "0 9 * * mon-fri", enabled: false, prompt_template: prompt }], "pr-health");
  assert.deepEqual(got.map((f) => f.name), ["weekly"]);
  assert.deepEqual(wf.scheduledBy([{ name: "weekly", schedule: "x", enabled: true, prompt_template: prompt }], "pr-health-2"), []);
});

// ---- the editor: size ----------------------------------------------------------------------------

const postBody = (source, tier = "yaml", name = "wf") => ({ name, source, tier });
const sourceOfBody = (target, makeBody) => "x".repeat(target - bodyBytes(makeBody("")));

test("Source too large in argument mode: a 125 KiB YAML spec is refused, the 120 KiB limit is named, and the CLI is suggested", () => {
  const r = wf.sourceCheck("yaml", "y".repeat(125 * KIB), "wf", false);
  assert.equal(r.ok, false);
  assert.equal(r.limit, ARG_BODY_MAX);
  assert.match(r.message, /120 KiB/);
  assert.match(r.message, /paste a smaller spec or use the CLI/i);
});

test("a POST body of exactly the argument limit is accepted and one byte more is refused", () => {
  const at = sourceOfBody(ARG_BODY_MAX, (s) => postBody(s));
  assert.equal(bodyBytes(postBody(at)), ARG_BODY_MAX);
  assert.equal(wf.sourceCheck("yaml", at, "wf", false).ok, true);
  assert.equal(wf.sourceCheck("yaml", `${at}x`, "wf", false).ok, false);
  const atPy = sourceOfBody(ARG_BODY_MAX, (s) => postBody(s, "python"));
  assert.equal(wf.sourceCheck("python", atPy, "wf", false).ok, true, "python counts its own body");
  assert.equal(wf.sourceCheck("python", `${atPy}x`, "wf", false).ok, false);
});

test("the PUT body (source, expected_hash, tier) is counted when editing, not the POST body", () => {
  const hash = "h".repeat(64);
  const putBody = (s) => ({ source: s, expected_hash: hash, tier: "yaml" });
  const at = sourceOfBody(ARG_BODY_MAX, putBody);
  assert.equal(bodyBytes(putBody(at)), ARG_BODY_MAX);
  assert.equal(wf.sourceCheck("yaml", at, "wf", false, { expectedHash: hash }).ok, true);
  assert.equal(wf.sourceCheck("yaml", `${at}x`, "wf", false, { expectedHash: hash }).ok, false);
});

test("the encoded size is what counts: quotes, newlines, control characters and multi-byte text all grow the body", () => {
  assert.equal(wf.sourceCheck("yaml", "\n".repeat(61_500), "wf", false).ok, false, "123000 bytes once each newline is \\n");
  assert.equal(wf.sourceCheck("yaml", '"'.repeat(61_500), "wf", false).ok, false);
  assert.equal(wf.sourceCheck("yaml", "\u0001".repeat(21_000), "wf", false).ok, false, "126000 bytes once each is \\u0001");
  assert.equal(wf.sourceCheck("yaml", "é".repeat(61_500), "wf", false).ok, false, "123000 bytes of UTF-8");
  assert.equal(wf.sourceCheck("yaml", "\n".repeat(60_000), "wf", false).ok, true, "120000 bytes is under the limit");
});

test("the editor shows the encoded size against the active limit", () => {
  assert.match(wf.sourceCheck("yaml", "y".repeat(KIB), "wf", false).sizeText, /KiB of 120 KiB/);
  assert.match(wf.sourceCheck("yaml", "y".repeat(KIB), "wf", true).sizeText, /KiB of 256 KiB/);
  const py = wf.sourceCheck("python", "p".repeat(KIB), "wf", true).sizeText;
  assert.ok(/131072|128 Ki/.test(py), py);
});

test("Source too large in stdin mode: 130 Ki characters of Python is refused and the 131072-character (128 Ki) cap is named", () => {
  const r = wf.sourceCheck("python", "p".repeat(133_120), "wf", true);
  assert.equal(r.ok, false);
  assert.match(r.message, /131072 characters \(128 Ki\)/);
});

test("stdin mode: Python is counted in characters, at 131072 exactly", () => {
  assert.equal(wf.sourceCheck("python", "p".repeat(131_072), "wf", true).ok, true);
  assert.equal(wf.sourceCheck("python", "p".repeat(131_073), "wf", true).ok, false);
  assert.equal(wf.sourceCheck("python", "é".repeat(131_072), "wf", true).ok, true, "262144 bytes but 131072 characters");
  // 131072 characters in 162144 UTF-16 units and 224288 bytes: under both caps, and over a count of UTF-16 units.
  const astral = (n) => "x".repeat(100_000) + "🙂".repeat(n);
  assert.equal(wf.sourceCheck("python", astral(31_072), "wf", true).ok, true, "131072 characters, counted as Python counts them");
  assert.equal(wf.sourceCheck("python", astral(31_073), "wf", true).ok, false, "131073 characters");
});

test("stdin mode: YAML is counted in UTF-8 bytes, at 262144 exactly, and the 256 KiB cap is named", () => {
  assert.equal(wf.sourceCheck("yaml", "y".repeat(262_144), "wf", true).ok, true);
  const over = wf.sourceCheck("yaml", "y".repeat(262_145), "wf", true);
  assert.equal(over.ok, false);
  assert.match(over.message, /262144 bytes \(256 KiB\)/);
  assert.equal(wf.sourceCheck("yaml", "é".repeat(131_072), "wf", true).ok, true, "262144 bytes");
  assert.equal(wf.sourceCheck("yaml", "é".repeat(131_073), "wf", true).ok, false, "262146 bytes");
  assert.equal(wf.sourceCheck("yaml", "🙂".repeat(65_536), "wf", true).ok, true, "262144 bytes in 131072 UTF-16 units");
  assert.equal(wf.sourceCheck("yaml", "🙂".repeat(65_537), "wf", true).ok, false);
});

test("the size check sends nothing and returns the same answer however often it is asked", () => {
  const a = wf.sourceCheck("yaml", "y".repeat(125 * KIB), "wf", false);
  assert.deepEqual(wf.sourceCheck("yaml", "y".repeat(125 * KIB), "wf", false), a);
});

// ---- the editor: save gating and requests ---------------------------------------------------------

test("Edit after validate: Save needs a clean validate of the text as it is now", () => {
  assert.equal(wf.saveEnabled("a", "a", pass), true);
  assert.equal(wf.saveEnabled("a", "b", pass), false, "changed after validating");
  assert.equal(wf.saveEnabled("a", "a ", pass), false, "even one trailing space");
  assert.equal(wf.saveEnabled("a", "a", null), false);
  assert.equal(wf.saveEnabled("a", "a", undefined), false);
});

test("blocking results keep Save off: a failed status, errors, or an error finding", () => {
  assert.equal(wf.saveEnabled("a", "a", { status: "fail", errors: ["bad"], findings: [] }), false);
  assert.equal(wf.saveEnabled("a", "a", { status: "pass", errors: ["bad"], findings: [] }), false);
  assert.equal(wf.saveEnabled("a", "a", { status: "pass", errors: [], findings: [{ severity: "error", line: 3, message: "m" }] }), false);
});

test("Warnings only: warning findings and reserved notes do not block Save", () => {
  assert.equal(wf.saveEnabled("a", "a", { status: "pass", errors: [], findings: [{ severity: "warning", line: 3, message: "m" }] }), true);
  assert.equal(wf.saveEnabled("a", "a", { status: "pass_reserved", errors: [], reserved_notes: ["mode parallel is not built yet"] }), true);
});

test("an answer that is not a clean pass never enables Save: empty, unknown status, or no validated text", () => {
  for (const result of [{}, { errors: [] }, { status: "weird", errors: [] }, { status: "pending" }, [], "pass", 1]) {
    assert.equal(wf.saveEnabled("a", "a", result), false, JSON.stringify(result));
  }
  for (const text of [undefined, null, 5]) assert.equal(wf.saveEnabled(text, text, pass), false, `text ${String(text)}`);
});

test("Save for a new workflow is POST /workflows with name, source and tier, and nothing else", () => {
  const r = wf.saveRequest({ mode: "new", name: "wf", tier: "yaml", source: "s", path: "/etc/passwd", expectedHash: "h" });
  assert.deepEqual([r.method, r.path, r.body], ["POST", "/workflows", { name: "wf", source: "s", tier: "yaml" }]);
  assert.equal(Object.hasOwn(r, "cli"), false, "a YAML spec has no CLI command to name");
  assert.equal(wf.saveRequest({ mode: "new", name: "wf", tier: "python", source: "s" }).cli, "cao workflow create wf --from-file <path>");
});

test("Save for an existing workflow is PUT /workflows/{name} with source, tier and the expected_hash read when editing began", () => {
  const r = wf.saveRequest({ mode: "edit", name: "wf", tier: "python", source: "s", expectedHash: "h" });
  assert.deepEqual([r.method, r.path, r.body], ["PUT", "/workflows/wf", { source: "s", expected_hash: "h", tier: "python" }]);
  assert.throws(() => wf.saveRequest({ mode: "edit", name: "wf", tier: "yaml", source: "s" }), /expected_hash/);
  assert.throws(() => wf.saveRequest({ mode: "edit", name: "wf", tier: "yaml", source: "s", expectedHash: "" }), /expected_hash/);
});

test("No file option: the name is a bare stem, the mode is new or edit, and no request carries a path field", () => {
  for (const name of ["a/b", "../x", "a.py", "a.yaml", "", "a b", "a\n", "é", undefined]) {
    assert.throws(() => wf.saveRequest({ mode: "new", name, tier: "yaml", source: "s" }), /bare/, JSON.stringify(name));
    assert.throws(() => wf.saveRequest({ mode: "edit", name, tier: "yaml", source: "s", expectedHash: "h" }), /bare/, JSON.stringify(name));
  }
  assert.throws(() => wf.saveRequest({ mode: "delete", name: "wf", tier: "yaml", source: "s", expectedHash: "h" }));
  assert.throws(() => wf.saveRequest({ name: "wf", tier: "yaml", source: "s" }));
  for (const req of [wf.saveRequest({ mode: "new", name: "wf", tier: "yaml", source: "s" }), wf.saveRequest({ mode: "edit", name: "wf", tier: "yaml", source: "s", expectedHash: "h" }), wf.submitRequest("wf", {})]) {
    assert.ok(!Object.hasOwn(req.body, "path") && !Object.hasOwn(req.body, "source_path") && !Object.hasOwn(req.body, "file"));
  }
});

test("a source that is large still goes through the same size gate when it is saved", () => {
  const r = wf.saveRequest({ mode: "new", name: "wf", tier: "yaml", source: "x".repeat(ARG_BODY_MAX) });
  const built = curlRequest(CFG, r);
  assert.equal(built.ok, false);
  assert.equal(built.outcome.kind, "too_large");
});

// ---- concurrent change ---------------------------------------------------------------------------

const STALE = "workflow 'wf' has changed on disk since it was read (expected source hash aaa, found bbb); re-read the spec and re-apply your changes";
const WRONG_TIER = "workflow 'wf' is a Python workflow; update it with tier 'python'";

test("Concurrent change: a PUT refused for a stale hash says the workflow changed elsewhere, keeps the text and offers to reload", () => {
  const o = wf.staleHashOutcome({ kind: "error", status: 409, detail: STALE }, "PUT");
  assert.deepEqual([o.type, o.keepText, o.offerReload], ["stale", true, true]);
  assert.match(o.message, /changed elsewhere/);
});

test("a PUT refused with 409 for another reason is not reported as a concurrent change", () => {
  const o = wf.staleHashOutcome({ kind: "error", status: 409, detail: WRONG_TIER }, "PUT");
  assert.notEqual(o.type, "stale");
  assert.doesNotMatch(o.message, /changed elsewhere/);
  assert.ok(JSON.stringify(o).includes(WRONG_TIER), "the server's reason is shown");
  assert.equal(o.keepText, true);
  assert.notEqual(o.offerReload, true);
});

test("a POST refused with 409 says a workflow with that name exists", () => {
  const o = wf.staleHashOutcome({ kind: "error", status: 409, detail: "workflow 'wf' already exists" }, "POST");
  assert.equal(o.type, "exists");
  assert.match(o.message, /exists/);
});

test("anything else is not this outcome", () => {
  for (const outcome of [{ kind: "ok", status: 200 }, { kind: "error", status: 404, detail: "unknown" }, { kind: "error", status: 500, detail: "x" }, { kind: "unreachable" }, { kind: "auth", status: 401 }, { kind: "error" }]) {
    for (const method of ["PUT", "POST"]) assert.equal(wf.staleHashOutcome(outcome, method), null, `${JSON.stringify(outcome)} ${method}`);
  }
});

// ---- review round: the byte cap also binds Python; hard words in the approve command ------------------

test("stdin mode: a Python source over 256 KiB of UTF-8 is refused even when its character count is under the limit", () => {
  const cjk = "日".repeat(100_000);
  assert.ok([...cjk].length < 131_072 && new TextEncoder().encode(cjk).length > 262_144);
  const r = wf.sourceCheck("python", cjk, "wf", true);
  assert.equal(r.ok, false);
  assert.match(r.message, /262144 bytes \(256 KiB\)/);
});

test("stdin mode: Python has two caps, 131072 characters and 262144 bytes, and each is exact", () => {
  // 87381 CJK characters are 262143 bytes; one ASCII character more makes exactly 262144.
  const atBytes = "日".repeat(87_381) + "x";
  assert.equal(new TextEncoder().encode(atBytes).length, 262_144);
  assert.equal(wf.sourceCheck("python", atBytes, "wf", true).ok, true, "262144 bytes");
  assert.equal(wf.sourceCheck("python", `${atBytes}x`, "wf", true).ok, false, "262145 bytes");
  assert.equal(wf.sourceCheck("python", "x".repeat(131_072), "wf", true).ok, true, "131072 characters");
  assert.equal(wf.sourceCheck("python", "x".repeat(131_073), "wf", true).ok, false, "131073 characters");
  const both = wf.sourceCheck("python", "日".repeat(131_073), "wf", true);
  assert.equal(both.ok, false);
  assert.match(both.message, /131072 characters \(128 Ki\)|262144 bytes \(256 KiB\)/);
});

test("the Python character-cap message still names 131072 characters when the character count is what is over", () => {
  const r = wf.sourceCheck("python", "x".repeat(133_120), "wf", true);
  assert.match(r.message, /131072 characters \(128 Ki\)/);
});

test("a single word that a shell reads as something else survives the copied approve command", () => {
  const cwd = globDir();
  for (const word of HARD_WORDS) {
    const s = wf.approvalState(null, approval(word));
    if (needsQuotes(word) && !word.includes("'")) assert.equal(quotedCount(s.command, word), 1, `${word} is quoted in: ${s.command}`);
    assert.deepEqual(shellArgv(s.command, "cao", { cwd }), ["workflow", "approve", word], word);
  }
});

test("a plan id that starts with = is quoted in the copied approve command (zsh expands an unquoted =ls)", () => {
  const s = wf.approvalState(null, approval("=ls"));
  assert.equal(s.command, "cao workflow approve '=ls'");
});

// ---- size and limit are measured in the same unit, and the cap that is exceeded is named ----

// The unit a cap is counted in: 131072 is the Python character cap, 262144 the byte cap of the server,
// and ARG_BODY_MAX the encoded request body in argument mode.
function measuredIn(limit, text, body) {
  if (limit === 131_072) return [...text].length;
  if (limit === 262_144) return new TextEncoder().encode(text).length;
  if (limit === ARG_BODY_MAX) return bodyBytes(body);
  throw new Error(`a limit that is none of the caps: ${limit}`);
}

test("every refusal reports its size and its limit in the same unit, and names the cap that is exceeded", () => {
  const cjk = (n) => "日".repeat(n);
  const cases = [
    { label: "python, 131073 CJK characters (over both caps)", tier: "python", text: cjk(131_073), stdin: true },
    { label: "python, 100000 CJK characters (over the byte cap only)", tier: "python", text: cjk(100_000), stdin: true },
    { label: "python, 131073 ASCII characters (over the character cap only)", tier: "python", text: "x".repeat(131_073), stdin: true },
    { label: "python, 200000 emoji", tier: "python", text: "🙂".repeat(200_000), stdin: true },
    { label: "yaml, 262145 bytes", tier: "yaml", text: "y".repeat(262_145), stdin: true },
    { label: "yaml, 100000 CJK characters", tier: "yaml", text: cjk(100_000), stdin: true },
  ];
  for (const c of cases) {
    const r = wf.sourceCheck(c.tier, c.text, "wf", c.stdin);
    assert.equal(r.ok, false, c.label);
    assert.ok([131_072, 262_144].includes(r.limit), `${c.label}: limit ${r.limit} is not a cap`);
    const size = measuredIn(r.limit, c.text);
    assert.equal(r.bytes, size, `${c.label}: size ${r.bytes} is not measured in the unit of its limit ${r.limit}`);
    assert.ok(r.bytes > r.limit, `${c.label}: ${r.bytes} does not exceed ${r.limit}`);
    assert.ok(r.message.includes(String(r.limit)), `${c.label}: the message does not name the cap ${r.limit}: ${r.message}`);
    assert.match(r.message, r.limit === 131_072 ? /characters/ : /bytes/, `${c.label}: ${r.message}`);
  }
});

test("a refusal in argument mode reports the encoded body against the 120 KiB limit, for both tiers and for an edit", () => {
  const big = "y".repeat(125 * KIB);
  for (const [tier, opts, body] of [["yaml", {}, postBody(big, "yaml")], ["python", {}, postBody(big, "python")],
    ["yaml", { expectedHash: "h".repeat(64) }, { source: big, expected_hash: "h".repeat(64), tier: "yaml" }]]) {
    const r = wf.sourceCheck(tier, big, "wf", false, opts);
    assert.equal(r.ok, false);
    assert.equal(r.limit, ARG_BODY_MAX);
    assert.equal(r.bytes, measuredIn(r.limit, big, body));
    assert.ok(r.bytes > r.limit);
    assert.match(r.message, /120 KiB/);
  }
});

test("an accepted source reports a size that is under its limit in the same unit", () => {
  for (const [tier, text, stdin] of [["python", "x".repeat(1000), true], ["python", "日".repeat(50_000), true], ["yaml", "y".repeat(1000), true], ["yaml", "y".repeat(1000), false]]) {
    const r = wf.sourceCheck(tier, text, "wf", stdin);
    assert.equal(r.ok, true);
    const body = postBody(text, tier);
    assert.equal(r.bytes, measuredIn(r.limit, text, body), `${tier} ${text.length}`);
    assert.ok(r.bytes <= r.limit);
  }
});

// ---- the unit of every answer, the cap an accepted Python source is closest to, exact counts ----

// The unit is part of every sourceCheck answer: "characters" when the limit is the Python character cap,
// "bytes" for every other limit (the 256 KiB byte cap, the argument-mode body limit, no limit at all).
const unitOf = (limit) => (limit === 131_072 ? "characters" : "bytes");
const oneOfEachAnswer = () => {
  const edit = { expectedHash: "h".repeat(64) };
  return [
    ["unknown tier", wf.sourceCheck("ruby", "x", "wf", false)],
    ["arg mode, yaml, accepted", wf.sourceCheck("yaml", "y".repeat(1000), "wf", false)],
    ["arg mode, python, accepted", wf.sourceCheck("python", "p".repeat(1000), "wf", false)],
    ["arg mode, edit, accepted", wf.sourceCheck("yaml", "y".repeat(1000), "wf", false, edit)],
    ["arg mode, yaml, refused", wf.sourceCheck("yaml", "y".repeat(125 * KIB), "wf", false)],
    ["arg mode, python, refused", wf.sourceCheck("python", "p".repeat(125 * KIB), "wf", false)],
    ["arg mode, edit, refused", wf.sourceCheck("yaml", "y".repeat(125 * KIB), "wf", false, edit)],
    ["stdin, yaml, accepted", wf.sourceCheck("yaml", "y".repeat(1000), "wf", true)],
    ["stdin, yaml, refused", wf.sourceCheck("yaml", "y".repeat(262_145), "wf", true)],
    ["stdin, python, accepted", wf.sourceCheck("python", "p".repeat(1000), "wf", true)],
    ["stdin, python, over the character cap", wf.sourceCheck("python", "x".repeat(131_073), "wf", true)],
    ["stdin, python, over the byte cap", wf.sourceCheck("python", "日".repeat(100_000), "wf", true)],
    ["stdin, python, over both caps", wf.sourceCheck("python", "日".repeat(131_073), "wf", true)],
  ];
};

test("every answer carries a unit that matches its limit: characters at 131072, bytes otherwise", () => {
  for (const [label, r] of oneOfEachAnswer()) {
    assert.ok(Object.hasOwn(r, "unit"), `${label}: no unit`);
    assert.equal(r.unit, unitOf(r.limit), `${label}: unit ${r.unit} for limit ${r.limit}`);
  }
});

test("each refusal names its own cap, in its own unit: characters for the character cap, bytes for the others", () => {
  const expect = (r, unit, limit, size) => assert.deepEqual([r.ok, r.unit, r.limit, r.bytes], [false, unit, limit, size], JSON.stringify(r));
  expect(wf.sourceCheck("python", "x".repeat(131_073), "wf", true), "characters", 131_072, 131_073);
  expect(wf.sourceCheck("python", "日".repeat(100_000), "wf", true), "bytes", 262_144, 300_000);
  expect(wf.sourceCheck("yaml", "y".repeat(262_145), "wf", true), "bytes", 262_144, 262_145);
  expect(wf.sourceCheck("yaml", "日".repeat(100_000), "wf", true), "bytes", 262_144, 300_000);
  const big = "y".repeat(125 * KIB);
  expect(wf.sourceCheck("yaml", big, "wf", false), "bytes", ARG_BODY_MAX, bodyBytes(postBody(big, "yaml")));
  expect(wf.sourceCheck("python", big, "wf", false), "bytes", ARG_BODY_MAX, bodyBytes(postBody(big, "python")));
  const unknown = wf.sourceCheck("ruby", "x", "wf", false);
  assert.deepEqual([unknown.ok, unknown.unit, unknown.limit], [false, "bytes", 0]);
});

test("a Python source over both caps reports one of them, with its own unit, limit and size", () => {
  const r = wf.sourceCheck("python", "日".repeat(131_073), "wf", true);
  assert.equal(r.ok, false);
  assert.ok(r.limit === 131_072 ? r.bytes === 131_073 && r.unit === "characters" : r.bytes === 393_219 && r.unit === "bytes", JSON.stringify(r));
});

test("an accepted Python source reports the cap it is closest to, compared as a share of each cap and not as raw counts", () => {
  const closest = (text, unit, limit, size) => {
    const r = wf.sourceCheck("python", text, "wf", true);
    assert.deepEqual([r.ok, r.unit, r.limit, r.bytes], [true, unit, limit, size], JSON.stringify({ n: text.length, r }));
  };
  closest("x".repeat(1000), "characters", 131_072, 1000);                // ASCII: both shares are equal, and the character cap wins the tie
  closest("é".repeat(100_000), "characters", 131_072, 100_000);          // two-byte text: the shares tie again (100000/131072 = 200000/262144); raw counts would say bytes
  closest("é".repeat(131_072), "characters", 131_072, 131_072);          // at both caps at once
  closest("é".repeat(99_999) + "日", "bytes", 262_144, 200_001);         // one three-byte character: the byte share is now the larger
  closest("日".repeat(60_000), "bytes", 262_144, 180_000);               // three-byte text: bytes are the closer cap
  closest("🙂".repeat(60_000), "bytes", 262_144, 240_000);               // four-byte text
  closest("", "characters", 131_072, 0);                                  // nothing: a tie at zero
});

test("sizeText for an accepted Python source is in the unit of its cap: characters when the character cap is closer, KiB when the byte cap is", () => {
  const chars = wf.sourceCheck("python", "é".repeat(100_000), "wf", true);
  assert.equal(chars.unit, "characters");
  assert.match(chars.sizeText, /^100000 of 131072 characters/);
  assert.doesNotMatch(chars.sizeText, /KiB/);
  const bytes = wf.sourceCheck("python", "é".repeat(99_999) + "日", "wf", true);
  assert.equal(bytes.unit, "bytes");
  assert.match(bytes.sizeText, /KiB of 256 KiB/);
  assert.doesNotMatch(bytes.sizeText, /characters/);
  assert.match(wf.sourceCheck("python", "日".repeat(60_000), "wf", true).sizeText, /KiB of 256 KiB/);
  assert.match(wf.sourceCheck("python", "x".repeat(1000), "wf", true).sizeText, /^1000 of 131072 characters/);
});

test("the answer switches unit at the tie, one byte either way", () => {
  // 100000 characters of two bytes tie; one more byte in the text makes the byte share the larger.
  const tie = "é".repeat(100_000);
  assert.equal(wf.sourceCheck("python", tie, "wf", true).unit, "characters");
  assert.equal(wf.sourceCheck("python", `${tie.slice(1)}日`, "wf", true).unit, "bytes", "100000 characters, 200001 bytes");
  assert.equal(wf.sourceCheck("python", `${tie.slice(1)}é`, "wf", true).unit, "characters", "the same text with the two-byte character back");
});

// ---- exact counts in every refusal message --------------------------------------------------------

// The numbers a message prints, as integers, in order.
const numbersIn = (message) => [...message.matchAll(/\d+(?:\.\d+)?/g)].map((m) => m[0]);

const EDIT = { expectedHash: "h".repeat(64) };
const justOverBody = (makeBody) => sourceOfBody(ARG_BODY_MAX + 1, makeBody);
const EXACT_COUNT_CASES = [
  ["yaml, 262145 bytes", () => wf.sourceCheck("yaml", "y".repeat(262_145), "wf", true), 262_145, 262_144],
  ["yaml, 262146 bytes", () => wf.sourceCheck("yaml", "y".repeat(262_146), "wf", true), 262_146, 262_144],
  ["arg mode, yaml, 122881 bytes", () => wf.sourceCheck("yaml", justOverBody((x) => postBody(x, "yaml")), "wf", false), 122_881, 122_880],
  ["arg mode, python, 122881 bytes", () => wf.sourceCheck("python", justOverBody((x) => postBody(x, "python")), "wf", false), 122_881, 122_880],
  ["arg mode, edit, 122881 bytes", () => wf.sourceCheck("yaml", justOverBody((x) => ({ source: x, expected_hash: EDIT.expectedHash, tier: "yaml" })), "wf", false, EDIT), 122_881, 122_880],
  ["python, 131073 characters", () => wf.sourceCheck("python", "x".repeat(131_073), "wf", true), 131_073, 131_072],
  ["python, 262145 bytes in 87382 characters", () => wf.sourceCheck("python", "日".repeat(87_381) + "xx", "wf", true), 262_145, 262_144],
  ["python, 300000 bytes in 100000 characters", () => wf.sourceCheck("python", "日".repeat(100_000), "wf", true), 300_000, 262_144],
];
for (const [label, answer, size, limit] of EXACT_COUNT_CASES) {
  test(`a refusal prints the exact size and the exact limit, so the size shown is greater than the limit shown: ${label}`, () => {
    const r = answer();
    assert.equal(r.ok, false);
    assert.deepEqual([r.bytes, r.limit], [size, limit], "the answer's own numbers");
    const shown = numbersIn(r.message);
    assert.ok(shown.includes(String(size)), `the message does not print the exact size ${size}: ${r.message}`);
    assert.ok(shown.includes(String(limit)), `the message does not print the exact limit ${limit}: ${r.message}`);
    assert.ok(size > limit);
    assert.doesNotMatch(r.message, /\b(256|120)\.0 KiB/, `the size is shown only as a rounded KiB figure that equals the limit's: ${r.message}`);
    assert.match(r.message, r.unit === "characters" ? /characters/ : /bytes/);
  });
}

// ---- sizeText of a refused source prints exact counts too -------------------------------

const REFUSED_SIZE_TEXT_CASES = [
  ["yaml, 262145 bytes", () => wf.sourceCheck("yaml", "y".repeat(262_145), "wf", true), 262_145, 262_144, "bytes"],
  ["yaml, 262146 bytes", () => wf.sourceCheck("yaml", "y".repeat(262_146), "wf", true), 262_146, 262_144, "bytes"],
  ["yaml, 100000 CJK characters", () => wf.sourceCheck("yaml", "日".repeat(100_000), "wf", true), 300_000, 262_144, "bytes"],
  ...[1, 10].flatMap((over) => [
    [`arg mode, yaml, ${ARG_BODY_MAX + over} bytes`, () => wf.sourceCheck("yaml", sourceOfBody(ARG_BODY_MAX + over, (x) => postBody(x, "yaml")), "wf", false), ARG_BODY_MAX + over, ARG_BODY_MAX, "bytes"],
    [`arg mode, python, ${ARG_BODY_MAX + over} bytes`, () => wf.sourceCheck("python", sourceOfBody(ARG_BODY_MAX + over, (x) => postBody(x, "python")), "wf", false), ARG_BODY_MAX + over, ARG_BODY_MAX, "bytes"],
    [`arg mode, edit, ${ARG_BODY_MAX + over} bytes`, () => wf.sourceCheck("yaml", sourceOfBody(ARG_BODY_MAX + over, (x) => ({ source: x, expected_hash: EDIT.expectedHash, tier: "yaml" })), "wf", false, EDIT), ARG_BODY_MAX + over, ARG_BODY_MAX, "bytes"],
  ]),
  ["python, 131073 characters", () => wf.sourceCheck("python", "x".repeat(131_073), "wf", true), 131_073, 131_072, "characters"],
  ["python, 131100 characters", () => wf.sourceCheck("python", "x".repeat(131_100), "wf", true), 131_100, 131_072, "characters"],
  ["python, 100000 CJK characters (over the byte cap)", () => wf.sourceCheck("python", "日".repeat(100_000), "wf", true), 300_000, 262_144, "bytes"],
  ["python, 262145 bytes in 87382 characters", () => wf.sourceCheck("python", "日".repeat(87_381) + "xx", "wf", true), 262_145, 262_144, "bytes"],
];
for (const [label, answer, size, limit, unit] of REFUSED_SIZE_TEXT_CASES) {
  test(`the sizeText of a refused source prints the exact size and the exact limit in the unit of the cap exceeded: ${label}`, () => {
    const r = answer();
    assert.equal(r.ok, false);
    assert.deepEqual([r.bytes, r.limit, r.unit], [size, limit, unit], "the answer's own numbers");
    const shown = numbersIn(r.sizeText);
    assert.ok(shown.includes(String(size)), `sizeText does not print the exact size ${size}: ${r.sizeText}`);
    assert.ok(shown.includes(String(limit)), `sizeText does not print the exact limit ${limit}: ${r.sizeText}`);
    assert.equal(shown[0], String(size), `the size comes first: ${r.sizeText}`);
    assert.ok(Number(shown[0]) > Number(shown[1]), `the size shown is not greater than the limit shown: ${r.sizeText}`);
    assert.ok(shown.every((n) => !n.includes(".")), `a rounded figure in: ${r.sizeText}`);
    assert.match(r.sizeText, unit === "characters" ? /characters/ : /bytes/, r.sizeText);
  });
}

test("an accepted source keeps its sizeText as before: rounded KiB for bytes, a plain count for characters", () => {
  assert.match(wf.sourceCheck("yaml", "y".repeat(1024), "wf", true).sizeText, /^1\.\d KiB of 256 KiB$/);
  assert.match(wf.sourceCheck("yaml", "y".repeat(1024), "wf", false).sizeText, /^\d+\.\d KiB of 120 KiB$/);
  assert.match(wf.sourceCheck("python", "é".repeat(100_000), "wf", true).sizeText, /^100000 of 131072 characters/);
});

test("the limit keeps its KiB name next to its exact count: 120 KiB in argument mode, 256 KiB for bytes, 128 Ki for characters", () => {
  const justOver = sourceOfBody(ARG_BODY_MAX + 1, (x) => postBody(x, "yaml"));
  assert.match(wf.sourceCheck("yaml", justOver, "wf", false).message, /120 KiB/);
  assert.match(wf.sourceCheck("yaml", "y".repeat(262_145), "wf", true).message, /262144 bytes \(256 KiB\)/);
  assert.match(wf.sourceCheck("python", "x".repeat(131_073), "wf", true).message, /131072 characters \(128 Ki\)/);
});

// ---- honesty --------------------------------------------------------------------------------------

test("inputsUndescribed is no longer pending once workflows.js exists", () => {
  assert.ok(!Object.hasOwn(HONESTY_PENDING, "inputsUndescribed"), "inputsUndescribed is still listed as pending");
});

await finish();
