import { test } from "node:test";
import assert from "node:assert/strict";
import { intervalFor } from "../schedule.js";

test("focused console is fastest", () => {
  assert.equal(intervalFor({ focused: true, status: "working" }), 800);
  assert.equal(intervalFor({ focused: true, status: "idle" }), 800);
});

test("visible tile cadence tracks status", () => {
  assert.equal(intervalFor({ focused: false, status: "working" }), 1000);
  assert.equal(intervalFor({ focused: false, status: "idle" }), 3000);
});

test("offline tile stops polling", () => {
  assert.equal(intervalFor({ focused: false, status: "offline" }), 0);
});

test("focused console keeps polling even when offline (self-heal)", () => {
  assert.equal(intervalFor({ focused: true, status: "offline" }), 800);
});
