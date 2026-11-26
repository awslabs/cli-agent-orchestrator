// Tiny plain-assert harness shared by every *.test.mjs of this example. No dependencies.
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export { assert };

// A temporary folder that is removed when the test process exits, so a run leaves nothing in /tmp.
const tmpDirs = [];
export function mkTmp(prefix) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  tmpDirs.push(dir);
  return dir;
}
process.on("exit", () => { for (const d of tmpDirs) fs.rmSync(d, { recursive: true, force: true }); });

const pending = [];
let failed = 0;

function ok(name) {
  console.log(`ok - ${name}`);
}

function bad(name, err) {
  failed += 1;
  console.log(`FAIL - ${name}`);
  console.log(String(err?.stack ?? err));
}

// CAO_WINDOW_TEST_FILTER=<text> runs only the cases whose name contains the text (for working on one
// case). It says so up front, the last line says the run was filtered, and a filter that matches no case fails.
const FILTER = process.env.CAO_WINDOW_TEST_FILTER || "";
let selected = 0;
if (FILTER) console.log(`filter - only cases whose name contains ${JSON.stringify(FILTER)} run`);

// A suite whose cases are independent and slow (install.test.mjs runs a shell installer in most of them) calls
// parallel(n) before its first case: finish() then runs the file again in up to n child processes, each taking every n-th
// case, and reports their lines together. CAO_WINDOW_TEST_WORKERS=1 runs the file in one process (to read one run's order),
// and a larger value overrides n. A child is told its share by CAO_WINDOW_TEST_SHARD=<index>/<count>.
const SHARD = /^(\d+)\/(\d+)$/.exec(process.env.CAO_WINDOW_TEST_SHARD ?? "");
const shard = SHARD ? { index: Number(SHARD[1]), count: Number(SHARD[2]) } : null;
let workers = 1;
export function parallel(n) {
  const wanted = Number(process.env.CAO_WINDOW_TEST_WORKERS);
  workers = Math.max(1, Math.min(Number.isInteger(wanted) && wanted > 0 ? wanted : n, os.availableParallelism()));
}

// Runs one case. An async case is awaited by finish().
export function test(name, fn) {
  if (FILTER && !name.includes(FILTER)) return;
  const position = selected;
  selected += 1;
  if (shard ? position % shard.count !== shard.index : workers > 1) return;   // another process runs it
  try {
    const result = fn();
    if (result && typeof result.then === "function") pending.push(result.then(() => ok(name), (e) => bad(name, e)));
    else ok(name);
  } catch (err) {
    bad(name, err);
  }
}

// Call last: `await finish();`. Exits 1 when any case failed, or when a filter selected no case (a run
// that checked nothing must not look like a pass).
export async function finish() {
  await Promise.all(pending);
  if (FILTER && selected === 0) {
    console.log(`FAIL - no case matches the filter ${JSON.stringify(FILTER)}`);
    process.exit(1);
  }
  if (!shard && workers > 1) failed += await runShards(Math.min(workers, selected));
  if (failed > 0) {
    console.log(`${failed} case(s) failed`);
    process.exit(1);
  }
  console.log(FILTER ? "all selected cases passed (filtered run)" : "all cases passed");
}

// Exit 0 with a visible SKIP line (the pytest wrapper treats a missing tool as a failure on the CI unit leg).
export function skip(reason) {
  console.log(`SKIP - ${reason}`);
  process.exit(0);
}

export const hasTool = (name) => !spawnSync(name, ["--version"]).error;

// The summary lines a child prints are the parent's to print once.
const SUMMARY = /^(all (selected )?cases passed|\d+ case\(s\) failed|filter - )/;

// Runs this file again as `count` children and prints what they print (their summary lines left out). Returns how many cases
// failed. A child that exits non-zero without a FAIL line (a crash) counts as one failure, with its output shown.
async function runShards(count) {
  const children = Array.from({ length: count }, (_, index) => new Promise((resolve) => {
    const child = spawn(process.execPath, [...process.execArgv, process.argv[1]], {
      env: { ...process.env, CAO_WINDOW_TEST_SHARD: `${index}/${count}` }, stdio: ["ignore", "pipe", "pipe"],
    });
    let out = "";
    child.stdout.on("data", (d) => { out += d; });
    child.stderr.on("data", (d) => { out += d; });
    child.on("close", (code, signal) => resolve({ code, signal, out }));
  }));
  let failures = 0;
  for (const done of children.map((c) => c.then((r) => {   // print each child's lines as it finishes
    const lines = r.out.split("\n").filter((l) => !SUMMARY.test(l));
    process.stdout.write(`${lines.join("\n").replace(/\n+$/, "")}\n`);
    return r;
  }))) {
    const r = await done;
    const own = r.out.split("\n").filter((l) => l.startsWith("FAIL - ")).length;
    failures += own;
    if ((r.code !== 0 || r.signal) && own === 0) { failures += 1; console.log(`FAIL - a test process ended with ${r.signal ?? `exit ${r.code}`} before it reported`); }
  }
  return failures;
}
