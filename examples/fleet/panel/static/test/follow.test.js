import { test } from "node:test";
import assert from "node:assert/strict";
import { computeFollow } from "../follow.js";

test("always → true regardless of scroll position", () => {
  assert.equal(computeFollow("always", { scrollHeight: 1000, scrollTop: 0, clientHeight: 200 }), true);
  assert.equal(computeFollow("always", { scrollHeight: 1000, scrollTop: 800, clientHeight: 200 }), true);
});

test("smart → true at/near the bottom", () => {
  // exactly at bottom: 1000 - 800 - 200 = 0 (< 40)
  assert.equal(computeFollow("smart", { scrollHeight: 1000, scrollTop: 800, clientHeight: 200 }), true);
  // 20px from bottom
  assert.equal(computeFollow("smart", { scrollHeight: 1000, scrollTop: 780, clientHeight: 200 }), true);
});

test("smart → false when the user has scrolled up", () => {
  assert.equal(computeFollow("smart", { scrollHeight: 1000, scrollTop: 300, clientHeight: 200 }), false);
});

test("unknown/undefined mode → false (no forced scroll)", () => {
  assert.equal(computeFollow("none", { scrollHeight: 1000, scrollTop: 0, clientHeight: 200 }), false);
  assert.equal(computeFollow(undefined, { scrollHeight: 1000, scrollTop: 0, clientHeight: 200 }), false);
});
