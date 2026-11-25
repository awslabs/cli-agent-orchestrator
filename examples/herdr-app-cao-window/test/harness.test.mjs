// The harness every other suite relies on: ok/FAIL lines, exit codes, SKIP and temp-dir cleanup.
// The pytest wrapper (test/examples/test_herdr_app_cao_window_node.py) reads these exit codes.
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { assert, finish, hasTool, mkTmp, test } from "./harness.mjs";

const HARNESS_URL = pathToFileURL(path.join(path.dirname(fileURLToPath(import.meta.url)), "harness.mjs")).href;

// Writes `body` as a module that imports the harness, runs it with node, returns {status, out}.
function runScript(body) {
  const file = path.join(mkTmp("cao-window-harness-"), "case.mjs");
  fs.writeFileSync(file, `import { assert, test, finish, skip, mkTmp, hasTool } from ${JSON.stringify(HARNESS_URL)};\n${body}\n`);
  const env = { ...process.env };
  delete env.CAO_WINDOW_TEST_FILTER;                 // a filtered outer run must not filter these inner ones
  const r = spawnSync(process.execPath, [file], { encoding: "utf8", env });
  return { status: r.status, out: r.stdout + r.stderr };
}

test("passing cases print ok lines and exit 0", () => {
  const r = runScript('test("alpha", () => assert.equal(1, 1)); await finish();');
  assert.equal(r.status, 0, r.out);
  assert.match(r.out, /^ok - alpha$/m);
  assert.match(r.out, /all cases passed/);
});

test("a failing case prints FAIL with the assertion, later cases still run, and the exit code is 1", () => {
  const r = runScript('test("bad", () => assert.equal(1, 2)); test("after", () => {}); await finish();');
  assert.equal(r.status, 1);
  assert.match(r.out, /^FAIL - bad$/m);
  assert.match(r.out, /AssertionError/);
  assert.match(r.out, /^ok - after$/m);
  assert.match(r.out, /1 case\(s\) failed/);
});

test("a rejected async case also fails the run", () => {
  const r = runScript('test("late", async () => { await null; throw new Error("boom"); }); await finish();');
  assert.equal(r.status, 1);
  assert.match(r.out, /^FAIL - late$/m);
});

test("skip prints a SKIP line and exits 0 before any case runs", () => {
  const r = runScript('skip("jq is not installed"); test("never", () => { throw new Error("ran"); }); await finish();');
  assert.equal(r.status, 0);
  assert.match(r.out, /^SKIP - jq is not installed$/m);
  assert.doesNotMatch(r.out, /FAIL/);
});

test("mkTmp folders are removed when the process exits", () => {
  const r = runScript('const d = mkTmp("cao-window-gone-"); console.log("DIR=" + d); test("t", () => {}); await finish();');
  const dir = /^DIR=(.*)$/m.exec(r.out)[1];
  assert.ok(dir.length > 0);
  assert.equal(fs.existsSync(dir), false);
});

test("mkTmp folders are removed even when a case failed", () => {
  const r = runScript('const d = mkTmp("cao-window-gone2-"); console.log("DIR=" + d); test("t", () => { throw new Error("x"); }); await finish();');
  assert.equal(r.status, 1);
  assert.equal(fs.existsSync(/^DIR=(.*)$/m.exec(r.out)[1]), false);
});

test("hasTool is true for node and false for a missing program", () => {
  assert.equal(hasTool("node"), true);
  assert.equal(hasTool("cao-window-no-such-tool"), false);
});

test("CAO_WINDOW_TEST_FILTER runs only the matching cases and says the run was filtered", () => {
  const file = path.join(mkTmp("cao-window-harness-"), "case.mjs");
  fs.writeFileSync(file, `import { test, finish } from ${JSON.stringify(HARNESS_URL)};\ntest("alpha one", () => {});\ntest("beta two", () => { throw new Error("must not run"); });\nawait finish();\n`);
  const r = spawnSync(process.execPath, [file], { env: { ...process.env, CAO_WINDOW_TEST_FILTER: "alpha" }, encoding: "utf8" });
  assert.equal(r.status, 0, r.stdout + r.stderr);
  assert.match(r.stdout, /^filter - only cases whose name contains "alpha" run$/m);
  assert.match(r.stdout, /^ok - alpha one$/m);
  assert.doesNotMatch(r.stdout, /beta two/);
  assert.match(r.stdout, /filtered run/);
});

test("a CAO_WINDOW_TEST_FILTER that matches no case exits non-zero and says so", () => {
  const file = path.join(mkTmp("cao-window-harness-"), "case.mjs");
  fs.writeFileSync(file, `import { test, finish } from ${JSON.stringify(HARNESS_URL)};\ntest("alpha one", () => {});\nawait finish();\n`);
  const r = spawnSync(process.execPath, [file], { env: { ...process.env, CAO_WINDOW_TEST_FILTER: "no such case" }, encoding: "utf8" });
  assert.notEqual(r.status, 0, `a filter that selected nothing passed:\n${r.stdout}${r.stderr}`);
  assert.match(r.stdout + r.stderr, /no case/i);
  assert.doesNotMatch(r.stdout, /passed/);
});

test("a file with no cases at all and a filter set also fails", () => {
  const file = path.join(mkTmp("cao-window-harness-"), "case.mjs");
  fs.writeFileSync(file, `import { finish } from ${JSON.stringify(HARNESS_URL)};\nawait finish();\n`);
  const r = spawnSync(process.execPath, [file], { env: { ...process.env, CAO_WINDOW_TEST_FILTER: "x" }, encoding: "utf8" });
  assert.notEqual(r.status, 0);
});

test("without a filter, a file with no cases still exits 0", () => {
  const r = runScript("await finish();");
  assert.equal(r.status, 0, r.out);
});

await finish();
