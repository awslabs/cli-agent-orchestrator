// lib/honesty.js: one constants table plus a registry of the function that produces each surface's view
// model (cao-window-honesty-labels; design D23). Task 12.4 adds the assertion that nothing is pending.
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { assert, finish, test } from "../test/harness.mjs";
import { auditHonesty } from "../test/honesty-audit.mjs";
import { HONESTY, HONESTY_PENDING, HONESTY_SOURCES } from "./honesty.js";

const here = dirname(fileURLToPath(import.meta.url));

// The rows of the spec's table, written out so a dropped or renamed row is noticed:
// surface -> [label text, module, function].
const TABLE = {
  bestGuess: ["best guess", "logic", "classify"],
  stuckHint: ["heuristic", "logic", "quietHint"],
  contextPercent: ["heuristic", "logic", "contextPercent"],
  releasedToast: ["held while you were busy", "logic", "toastStep"],
  knownGap: ["known gap: a done agent asking in prose is not counted", "logic", "knownGapNote"],
  recap: ["the agent's own words", "logic", "parseRecap"],
  startedByFlow: ["inferred from the session name", "logic", "flowOfSession"],
  flowRoutesMissing: ["needs a CAO change", "flows", "routesState"],
  flowRuns: ["needs a CAO change", "flows", "flowRunsNote"],
  createChecksSkipped: ["server checks skipped", "flows", "routesState"],
  draftBox: ["proposed", "draft", "draftBox"],
  draftField: ["AI", "draft", "applyDraft"],
  declaredTools: ["declared by the profile; the server resolves the final set", "launch", "askFirstDialog"],
  notEnforced: ["not enforced by this provider", "launch", "askFirstDialog"],
  inputsUndescribed: ["inputs not described by the spec", "workflows", "inputsForm"],
};
const EXPECTED = Object.keys(TABLE);
const load = (m) => import(new URL(m, import.meta.url).href);

test("the table has 15 surfaces and the registry has exactly one entry per key", () => {
  assert.equal(EXPECTED.length, 15);
  assert.deepEqual(Object.keys(HONESTY_SOURCES).sort(), [...EXPECTED].sort());
});

test("every surface shows the label text the spec gives it", () => {
  for (const [surface, [text]] of Object.entries(TABLE)) {
    assert.equal(HONESTY[HONESTY_SOURCES[surface].label], text, surface);
  }
});

test("every surface is produced by the function and module the spec names", () => {
  for (const [surface, [, mod, fn]] of Object.entries(TABLE)) {
    const e = HONESTY_SOURCES[surface];
    assert.equal(e.module, `./${mod}.js`, `${surface} module`);
    assert.equal(e.fn, fn, `${surface} function`);
    assert.ok(existsSync(join(here, `${mod}.js`)), `lib/${mod}.js does not exist`);
  }
});

test("the table holds the 13 distinct label texts of the spec and is frozen", () => {
  const texts = new Set(Object.values(TABLE).map(([text]) => text));
  assert.equal(texts.size, 13);
  assert.deepEqual([...new Set(Object.values(HONESTY))].sort(), [...texts].sort());
  assert.ok(Object.isFrozen(HONESTY));
  assert.throws(() => { HONESTY.heuristic = "changed"; }, TypeError);
});

test("no label is empty or padded", () => {
  for (const [key, text] of Object.entries(HONESTY)) {
    assert.equal(typeof text, "string", key);
    assert.ok(text.trim().length > 0 && text === text.trim(), key);
  }
});

test("the registry gives each entry a label key, a module, a function name and a fixture builder", () => {
  for (const [surface, e] of Object.entries(HONESTY_SOURCES)) {
    assert.ok(Object.hasOwn(HONESTY, e.label), `${surface}: unknown label key ${e.label}`);
    assert.equal(typeof e.args, "function", surface);
  }
});

test("pending surfaces name a real surface and the section that builds it", () => {
  for (const [surface, section] of Object.entries(HONESTY_PENDING)) {
    assert.ok(EXPECTED.includes(surface), `${surface} is pending but is not in the table`);
    assert.ok(Number.isInteger(section) && section >= 7 && section <= 12, `${surface}: section ${section}`);
  }
});

test("the audit passes the shipped tables", async () => {
  const r = await auditHonesty({ expected: EXPECTED, honesty: HONESTY, sources: HONESTY_SOURCES, pending: HONESTY_PENDING, load });
  assert.deepEqual(r.problems, []);
  console.log(r.pending.length ? `pending: ${r.pending.join(", ")}` : "nothing pending");
});

test("nothing is left pending: every surface is built and its label audited", () => {
  assert.deepEqual(HONESTY_PENDING, {});
});

test("the audit fails each way a surface can be wrong", async () => {
  const mod = { good: () => ({ label: "L" }), bad: () => ({ label: "other" }), boom: () => { throw new Error("x"); } };
  const run = (over) => auditHonesty({
    expected: ["s"], honesty: { l: "L" }, sources: { s: { label: "l", module: "m", fn: "good", args: () => [] } },
    pending: {}, load: async () => mod, ...over,
  });
  assert.deepEqual((await run({})).problems, []);
  assert.match((await run({ honesty: { l: "" } })).problems[0], /empty or missing label/);
  assert.match((await run({ honesty: { l: "   " } })).problems[0], /empty or missing label/);
  assert.match((await run({ honesty: {} })).problems[0], /empty or missing label/);
  assert.match((await run({ sources: {} })).problems[0], /no registry entry/);
  assert.match((await run({ sources: { s: { label: "l", module: "m", fn: "nope", args: () => [] } } })).problems[0], /no function nope/);
  assert.match((await run({ sources: { s: { label: "l", module: "m", fn: "bad", args: () => [] } } })).problems[0], /lacks the label/);
  assert.match((await run({ sources: { s: { label: "l", module: "m", fn: "boom", args: () => [] } } })).problems[0], /threw/);
  assert.match((await run({ load: async () => { throw new Error("gone"); } })).problems[0], /cannot load/);
  const pendingRun = await run({ pending: { s: 9 }, sources: { s: { label: "l", module: "m", fn: "nope", args: () => [] } } });
  assert.deepEqual(pendingRun, { problems: [], pending: ["s (section 9)"] });
  assert.match((await run({ pending: { s: 9, ghost: 9 } })).problems[0], /ghost: pending but not in the table/);
});

test("the audit finds the label anywhere in a nested view model, and a pending surface still needs its label", async () => {
  const nested = { load: async () => ({ f: () => ({ rows: [{ note: { text: "x L y" } }] }) }) };
  const base = { expected: ["s"], honesty: { l: "L" }, sources: { s: { label: "l", module: "m", fn: "f", args: () => [] } }, pending: {} };
  assert.deepEqual((await auditHonesty({ ...base, ...nested })).problems, []);
  const pendingNoLabel = await auditHonesty({ ...base, ...nested, honesty: { l: "" }, pending: { s: 7 } });
  assert.match(pendingNoLabel.problems[0], /empty or missing label/);
});

test("a surface added to the table without a registry entry fails even when others are fine", async () => {
  const r = await auditHonesty({
    expected: ["a", "b"], honesty: { l: "L" }, pending: {},
    sources: { a: { label: "l", module: "m", fn: "f", args: () => [] } },
    load: async () => ({ f: () => "L" }),
  });
  assert.deepEqual(r.problems, ["b: no registry entry"]);
});

test("the audit hands each fixture builder the loaded module and awaits an async view model", async () => {
  let seen;
  const r = await auditHonesty({
    expected: ["s"], honesty: { l: "L" }, pending: {},
    sources: { s: { label: "l", module: "m", fn: "f", args: (m) => { seen = m; return [1, 2]; } } },
    load: async () => ({ f: async (a, b) => ({ sum: a + b, label: "L" }) }),
  });
  assert.deepEqual(r.problems, []);
  assert.equal(typeof seen.f, "function");
});

await finish();
