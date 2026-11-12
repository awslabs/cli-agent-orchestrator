// Tier 1, node lane, of the CAO Dojo drift check (#1116).
//
// Feeds recorded CAO Dojo traces through upstream's own reference pipeline from
// the pinned @ag-ui/client 1.0.3: enforceEvents() (strips and warns about
// undescribed material) then verifyEvents() (the lifecycle and streaming
// verifier). Zero CAO wire code.
//
//   node verify-trace.mjs <trace.ndjson>...
//
// For each trace: every run-plane run must pass verifyEvents with no error, and
// every enforcement warning must start with one listed in cao-dojo/tools/known_gaps.json
// (shared with the Python lane; CAO_DOJO_KNOWN_GAPS overrides the path, for tests).
// After all traces, every known gap must be observed in at least one of them,
// the same union the Python lane takes (known-gaps.mjs). Each stream card goes through enforceEvents
// alone, since a lone CUSTOM is not a run. Before any trace, the three vendored
// upstream conformance streams must each produce their declared outcome, which
// proves this lane can fail.
//
// Exit 0 = clean, 1 = a finding. Needs `npm ci` in this directory.
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { enforceEvents, verifyEvents } from "@ag-ui/client";
import { from, lastValueFrom, toArray } from "rxjs";

import { GapLedger, allowedWarnings } from "./known-gaps.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, "../../..");
const knownGapsPath =
  process.env.CAO_DOJO_KNOWN_GAPS || join(repo, "examples/ag-ui/cao-dojo/tools/known_gaps.json");
const fixturesDir = join(repo, "test/examples/fixtures/ag-ui-conformance");
const CONTROLS = [
  "first-event-not-run-started-fatal",
  "nested-run-started-fatal",
  "run-finished-interrupt-outcome-accepted",
];

// enforceEvents reports through console.warn, so collect what it says.
let warnings = [];
const realWarn = console.warn;
console.warn = (...args) => {
  const text = args.map(String).join(" ");
  if (text.startsWith("[ag-ui]")) warnings.push(text);
  else realWarn(...args);
};

async function pipe(events, stages) {
  warnings = [];
  try {
    const out = await lastValueFrom(from(events).pipe(...stages, toArray()));
    return { outcome: "completed", out, warnings: [...warnings] };
  } catch (err) {
    return { outcome: "failed", error: err?.message ?? String(err), warnings: [...warnings] };
  }
}

function parseTrace(path) {
  const lines = readFileSync(path, "utf8").split("\n").filter((l) => l.trim());
  const records = lines.slice(1).map((l) => JSON.parse(l));
  const runs = new Map();
  const cards = [];
  for (const r of records) {
    if (r.plane === "run" && r.dir === "out") {
      if (!runs.has(r.run)) runs.set(r.run, []);
      runs.get(r.run).push(r.frame);
    } else if (r.plane === "stream") {
      cards.push(r);
    }
  }
  return { runs, cards };
}

const problems = [];
const fail = (msg) => {
  problems.push(msg);
  console.log(`  FAIL ${msg}`);
};

// 1. The upstream controls: the lane must be able to fail.
console.log("[verify-trace] upstream conformance controls (ag-ui 08eae9dc)");
for (const name of CONTROLS) {
  const fixture = JSON.parse(readFileSync(join(fixturesDir, `${name}.json`), "utf8"));
  const got = await pipe(fixture.stream, [enforceEvents(), verifyEvents()]);
  const want = fixture.expect;
  let ok = got.outcome === want.outcome;
  if (ok && want.errorContains) ok = (got.error ?? "").includes(want.errorContains);
  if (ok && want.noWarnings) ok = got.warnings.length === 0;
  if (ok && want.eventTypes) ok = JSON.stringify(got.out.map((e) => e.type)) === JSON.stringify(want.eventTypes);
  if (ok) console.log(`  ok   ${name}: ${got.outcome}${got.error ? ` (${got.error})` : ""}`);
  else fail(`${name}: expected ${JSON.stringify(want.outcome)}, got ${got.outcome} ${got.error ?? ""}`);
}

// 2. The traces.
const ledger = new GapLedger(allowedWarnings(JSON.parse(readFileSync(knownGapsPath, "utf8"))));
const paths = process.argv.slice(2);
if (paths.length === 0) fail("no trace given: node verify-trace.mjs <trace.ndjson>...");
for (const path of paths) {
  console.log(`[verify-trace] ${path}`);
  let parsed;
  try {
    parsed = parseTrace(path);
  } catch (err) {
    fail(`${path}: ${err.message}`);
    continue;
  }
  if (parsed.runs.size === 0) fail(`${path}: no run-plane runs`);
  for (const [runId, frames] of parsed.runs) {
    const got = await pipe(frames, [enforceEvents(), verifyEvents()]);
    if (got.outcome !== "completed") {
      fail(`${runId}: verifyEvents: ${got.error}`);
      continue;
    }
    const unlisted = ledger.record(got.warnings);
    if (unlisted.length) unlisted.forEach((w) => fail(`${runId}: ${w}`));
    else console.log(`  ok   ${runId}: ${frames.map((f) => f.type).join(" > ")}`);
  }
  for (const card of parsed.cards) {
    const got = await pipe([card.frame], [enforceEvents()]);
    const label = `${card.capture} stream ${card.frame.type} ${card.frame.name}`;
    if (got.outcome !== "completed") fail(`${label}: enforceEvents: ${got.error}`);
    else if (got.warnings.length) got.warnings.forEach((w) => fail(`${label}: ${w}`));
    else console.log(`  ok   ${label}`);
  }
}

// 3. Every known gap is observed in at least one trace. Checked once, across all
// of them, so a stale published trace and a fresh recording that differ on a
// gap still pass while either carries it.
for (const [text, gapId] of ledger.unobserved()) {
  fail(`KNOWN_GAPS ${gapId} not observed in any trace (${text}): remove it from known_gaps.json`);
}

console.warn = realWarn;
if (problems.length) {
  console.log(`[verify-trace] ${problems.length} finding(s)`);
  process.exit(1);
}
console.log("[verify-trace] PASS");
