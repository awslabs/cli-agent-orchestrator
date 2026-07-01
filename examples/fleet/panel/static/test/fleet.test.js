import { test } from "node:test";
import assert from "node:assert/strict";
import { dampFleet, OFFLINE_GRACE } from "../fleet.js";

const up = (name, sessions = []) => ({ name, online: true, sessions });
const down = (name) => ({ name, online: false, error: "ConnectTimeout" });

test("online machines pass straight through, untouched", () => {
  const state = new Map();
  const out = dampFleet([up("node-a", [{ id: "s1", status: "idle" }])], state);
  assert.equal(out[0].online, true);
  assert.equal(out[0].stale ?? false, false);
  assert.deepEqual(out[0].sessions, [{ id: "s1", status: "idle" }]);
});

test("a single dropped probe holds last-known-good — no offline flap", () => {
  const state = new Map();
  dampFleet([up("node-a", [{ id: "s1", status: "idle" }])], state); // seed last-good
  const out = dampFleet([down("node-a")], state); // one transient miss
  assert.equal(out[0].online, true, "held online through a single miss");
  assert.equal(out[0].stale, true, "flagged stale while held");
  assert.deepEqual(out[0].sessions, [{ id: "s1", status: "idle" }], "sessions kept, tiles not torn down");
});

test("sustained unreachability past the grace window surfaces offline", () => {
  const state = new Map();
  dampFleet([up("node-a", [{ id: "s1", status: "idle" }])], state);
  let out;
  for (let i = 0; i < OFFLINE_GRACE; i++) out = dampFleet([down("node-a")], state); // all held
  assert.equal(out[0].online, true, "still held within grace");
  out = dampFleet([down("node-a")], state); // one past grace
  assert.equal(out[0].online, false, "declared offline once grace is exhausted");
});

test("recovery resets the miss counter and stale flag", () => {
  const state = new Map();
  dampFleet([up("node-a", [{ id: "s1", status: "idle" }])], state);
  dampFleet([down("node-a")], state); // miss 1 (held)
  const rec = dampFleet([up("node-a", [{ id: "s1", status: "working" }])], state);
  assert.equal(rec[0].online, true);
  assert.equal(rec[0].stale ?? false, false);
  // counter reset: a fresh single miss is tolerated again
  const out2 = dampFleet([down("node-a")], state);
  assert.equal(out2[0].online, true);
  assert.equal(out2[0].stale, true);
});

test("a node down from its very first probe shows offline immediately", () => {
  const state = new Map();
  const out = dampFleet([down("node-z")], state);
  assert.equal(out[0].online, false, "no last-known-good to hold → offline at once");
});
