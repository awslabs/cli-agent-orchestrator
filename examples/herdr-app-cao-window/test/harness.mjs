// Tiny plain-assert harness shared by every *.test.mjs of this example. No dependencies.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
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

// Runs one case. An async case is awaited by finish().
export function test(name, fn) {
  if (FILTER && !name.includes(FILTER)) return;
  selected += 1;
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
