import { test } from "node:test";
import assert from "node:assert/strict";
import { tileStatus } from "../wall.js";

test("an offline machine is always offline, whatever the terminal reports", () => {
  assert.equal(tileStatus(false, "processing"), "offline");
  assert.equal(tileStatus(false, null), "offline");
});

test("active terminal states map to working (substring match)", () => {
  for (const s of ["processing", "running", "at work", "WORKING"]) {
    assert.equal(tileStatus(true, s), "working");
  }
});

test("error surfaces as error", () => {
  assert.equal(tileStatus(true, "error: boom"), "error");
});

test("anything else on an online machine is idle", () => {
  assert.equal(tileStatus(true, "waiting"), "idle");
  assert.equal(tileStatus(true, ""), "idle");
  assert.equal(tileStatus(true, null), "idle");
});
