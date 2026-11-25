// lib/workflows.js: what lib/workflows.test.mjs leaves open (task 11.1): the details state that
// workflows have no scheduler, and the source size line names the CLI that fits the tier.
import { assert, finish, test } from "../test/harness.mjs";
import * as wf from "./workflows.js";

test("the details say workflows have no scheduler of their own", () => {
  assert.match(wf.workflowDetails({ name: "x" }).schedulerNote, /no scheduler of their own/);
});

test("a source too large to send names the CLI for Python and says a YAML spec has none", () => {
  const big = "y".repeat(125 * 1024);
  assert.match(wf.sourceCheck("python", big, "wf", false).message, /cao workflow create wf --from-file <path>/);
  assert.match(wf.sourceCheck("yaml", big, "wf", false).message, /take a Python file only/);
});

test("a tier that is neither python nor yaml is not a size answer", () => {
  const r = wf.sourceCheck("ruby", "x", "wf", false);
  assert.equal(r.ok, false);
  assert.match(r.message, /python or yaml/);
});

await finish();
