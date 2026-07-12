import { test } from "node:test";
import assert from "node:assert/strict";
import { createPoller } from "../poller.js";

// Drain the promise chain inside one tick (fetch → resolveTerminal → fetch →
// onFrame). setImmediate is a macrotask and is NOT mocked below, so awaiting it
// lets all pending microtasks settle between timer ticks.
const flush = async () => {
  for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
};

// Minimal fetch stub: records every path, answers by route.
function stubFetch(routeBody) {
  const calls = [];
  global.fetch = async (path) => {
    calls.push(path);
    return { ok: true, status: 200, statusText: "OK", json: async () => routeBody(path) };
  };
  return calls;
}

const okRoutes = (path) =>
  path.includes("/sessions/") ? { terminals: [{ id: "t1" }] } : { screen: "FRAME", fallback: false };

function working() {
  return { focused: true, status: "working" }; // intervalFor → 800ms, always polls
}

test("resolves the terminal id once, then serves cached id on later ticks", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const calls = stubFetch(okRoutes);
  const frames = [];
  const p = createPoller({
    machine: "node-a", session: "s-cache",
    getState: working, onFrame: (f, meta) => frames.push({ f, meta }),
  });

  p.start();                         // first tick
  await flush();
  t.mock.timers.tick(800);           // fire the reschedule → second tick
  await flush();
  p.stop();

  const sessionCalls = calls.filter((c) => c.includes("/sessions/"));
  const screenCalls = calls.filter((c) => c.includes("/screen"));
  assert.equal(sessionCalls.length, 1, "terminal id resolved once and cached");
  assert.equal(screenCalls.length, 2, "screen polled on each tick");
  assert.equal(frames[0].f, "FRAME");
  assert.equal(frames[0].meta.terminalId, "t1");
});

test("forget() clears the cache so the next tick re-resolves the terminal id", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const calls = stubFetch(okRoutes);
  const p = createPoller({ machine: "node-a", session: "s-forget", getState: working, onFrame() {} });

  p.start();
  await flush();
  p.forget();                        // drop the cached id
  t.mock.timers.tick(800);
  await flush();
  p.stop();

  assert.equal(calls.filter((c) => c.includes("/sessions/")).length, 2, "re-resolved after forget()");
});

test("offline state skips fetching and reschedules a slow recheck", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const calls = stubFetch(okRoutes);
  const p = createPoller({
    machine: "node-a", session: "s-offline",
    getState: () => ({ focused: false, status: "offline" }), // intervalFor → 0
    onFrame() {},
  });

  p.start();
  await flush();
  assert.equal(calls.length, 0, "offline tile issues no requests");
  p.stop();
});

test("stop() halts the reschedule loop", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const calls = stubFetch(okRoutes);
  const p = createPoller({ machine: "node-a", session: "s-stop", getState: working, onFrame() {} });

  p.start();
  await flush();
  const after1 = calls.length;
  p.stop();
  t.mock.timers.tick(5000);          // no further ticks should run
  await flush();
  assert.equal(calls.length, after1, "no requests after stop()");
});

test("onError fires when a poll throws", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  global.fetch = async () => ({ ok: false, status: 502, statusText: "Bad Gateway", json: async () => ({}) });
  const errors = [];
  const p = createPoller({
    machine: "node-a", session: "s-err", getState: working,
    onFrame() {}, onError: (e) => errors.push(e),
  });

  p.start();
  await flush();
  p.stop();
  assert.equal(errors.length >= 1, true, "onError invoked on a failed fetch");
});
