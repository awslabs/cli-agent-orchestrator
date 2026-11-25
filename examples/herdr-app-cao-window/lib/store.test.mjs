// lib/store.js: shared() and localStorage behind one object that survives storage errors (design D17;
// cao-window-sessions, "Failure states keep the last good view": Storage throws).
import { assert, finish, test } from "../test/harness.mjs";
import { STORAGE_NOTICE, createStore } from "./store.js";

const backing = () => {
  const shared = new Map();
  const localMap = new Map();
  const b = {
    shared, localMap, failGet: false, failSet: false,
    getShared: (k) => { if (b.failGet) throw new Error("disk"); return shared.get(k); },
    setShared: (k, v) => { if (b.failSet) throw new Error("quota"); shared.set(k, v); },
    local: {
      getItem: (k) => (localMap.has(k) ? localMap.get(k) : null),
      setItem: (k, v) => { localMap.set(k, String(v)); },
    },
  };
  return b;
};

test("values round-trip through shared() and localStorage", () => {
  const b = backing();
  const store = createStore(b);
  store.set("caowin.pins", ["a"]); store.setLocal("caowin.fold.flows", "140");
  assert.deepEqual(store.get("caowin.pins"), ["a"]);
  assert.equal(store.getLocal("caowin.fold.flows"), "140");
  assert.deepEqual(b.shared.get("caowin.pins"), ["a"]);
  assert.equal(b.localMap.get("caowin.fold.flows"), "140");
  assert.equal(store.degraded(), false);
  assert.equal(store.notice(), null);
});

test("a key that was never set reads as undefined, whether storage returns undefined or null", () => {
  const b = backing();
  const store = createStore(b);
  assert.equal(store.get("nope"), undefined);
  assert.equal(store.getLocal("nope"), undefined);
  const n = createStore({ ...b, getShared: () => null });
  assert.equal(n.get("nope"), undefined);
  assert.equal(n.degraded(), false);
});

test("falsy values are values: false, 0, the empty string and an empty list round-trip", () => {
  const b = backing();
  const store = createStore(b);
  for (const v of [false, 0, "", []]) { store.set("k", v); assert.deepEqual(store.get("k"), v, JSON.stringify(v)); }
  store.setLocal("fold", 0);
  assert.equal(store.getLocal("fold"), "0");
});

test("local values come back as strings, as localStorage keeps them", () => {
  const store = createStore(backing());
  store.setLocal("fold", 34);
  assert.equal(store.getLocal("fold"), "34");
});

test("another block's write is visible while storage works", () => {
  const b = backing();
  const store = createStore(b);
  store.set("k", 1);
  b.shared.set("k", 2);
  assert.equal(store.get("k"), 2);
});

test("two stores over one backing see each other's writes (two blocks of one window)", () => {
  const b = backing();
  const a = createStore(b), c = createStore(b);
  a.set("caowin.selection", "t1");
  assert.equal(c.get("caowin.selection"), "t1");
});

test("a throwing write keeps the value for this run and raises the notice", () => {
  const b = backing();
  b.failSet = true;
  const store = createStore(b);
  store.set("caowin.pins", ["t1"]);                       // must not throw
  assert.deepEqual(store.get("caowin.pins"), ["t1"]);
  assert.equal(store.degraded(), true);
  assert.equal(store.notice(), STORAGE_NOTICE);
  assert.equal(STORAGE_NOTICE, "settings are not being saved");
});

test("Storage throws: a pin written after the failure replaces the stale stored value", () => {
  const b = backing();
  b.shared.set("caowin.pins", ["old"]);
  const store = createStore(b);
  b.failSet = true;
  store.set("caowin.pins", ["new"]);
  assert.deepEqual(store.get("caowin.pins"), ["new"], "the failed write was lost and the stale value came back");
  store.set("caowin.pins", ["newer"]);
  assert.deepEqual(store.get("caowin.pins"), ["newer"]);
});

test("a write that failed does not lose the writes that worked before it", () => {
  const b = backing();
  const store = createStore(b);
  store.set("a", 1);
  b.failSet = true;
  store.set("b", 2);
  assert.equal(store.get("a"), 1);
  assert.equal(store.get("b"), 2);
});

test("a throwing read degrades and falls back to memory", () => {
  const b = backing();
  const store = createStore(b);
  store.set("k", "v");
  b.failGet = true;
  assert.equal(store.get("k"), "v");
  assert.equal(store.get("never-set"), undefined);
  assert.equal(store.degraded(), true);
  assert.equal(store.notice(), STORAGE_NOTICE);
});

test("the notice stays up for the rest of the run, even when storage works again", () => {
  const b = backing();
  b.failSet = true;
  const store = createStore(b);
  store.set("k", 1);
  b.failSet = false;
  store.set("k", 2);
  assert.equal(store.notice(), STORAGE_NOTICE);
  assert.equal(store.get("k"), 2);
});

test("a throwing localStorage does the same", () => {
  const b = backing();
  b.local.setItem = () => { throw new Error("denied"); };
  b.local.getItem = () => { throw new Error("denied"); };
  const store = createStore(b);
  store.setLocal("fold", "34");
  assert.equal(store.getLocal("fold"), "34");
  assert.equal(store.notice(), STORAGE_NOTICE);
});

test("a storage that is missing altogether does not stop the window", () => {
  const store = createStore({ getShared: () => { throw new TypeError("shared is not a function"); }, setShared: () => { throw new TypeError("x"); }, local: undefined });
  store.set("k", 1);
  store.setLocal("f", "2");
  assert.equal(store.get("k"), 1);
  assert.equal(store.getLocal("f"), "2");
  assert.equal(store.notice(), STORAGE_NOTICE);
});

test("throwing storage never throws out of get, set, getLocal or setLocal", () => {
  const b = backing();
  b.failGet = true; b.failSet = true;
  b.local.getItem = () => { throw new Error("x"); };
  b.local.setItem = () => { throw new Error("x"); };
  const store = createStore(b);
  assert.doesNotThrow(() => { store.set("a", 1); store.get("a"); store.setLocal("b", 2); store.getLocal("b"); });
});

test("a key another block's store cleared reads as empty here, not as this store's old value", () => {
  const b = backing();
  const a = createStore(b), c = createStore(b);
  a.set("caowin.selection", "t1");
  assert.equal(c.get("caowin.selection"), "t1");
  b.shared.delete("caowin.selection");                       // storage no longer holds the key
  const gone = a.get("caowin.selection");
  assert.ok(gone === null || gone === undefined, `still read ${JSON.stringify(gone)} from this store's memory`);
  a.set("caowin.filter", "asking");
  c.set("caowin.filter", null);                              // the other block clears it by writing null
  assert.equal(a.get("caowin.filter") ?? null, null, "this store returned its own old value after another block cleared the key");
});

test("a pin list another block emptied stays empty here, and a later write of ours wins again", () => {
  const b = backing();
  const a = createStore(b), c = createStore(b);
  a.set("caowin.pins", ["t1", "t2"]);
  c.set("caowin.pins", []);
  assert.deepEqual(a.get("caowin.pins"), []);
  a.set("caowin.pins", ["t3"]);
  assert.deepEqual(c.get("caowin.pins"), ["t3"]);
});

test("a local value another block removed is not served from this store's memory either", () => {
  const b = backing();
  const a = createStore(b);
  a.setLocal("caowin.fold.flows", "140");
  b.localMap.delete("caowin.fold.flows");
  const gone = a.getLocal("caowin.fold.flows");
  assert.ok(gone === null || gone === undefined, `still read ${JSON.stringify(gone)} from this store's memory`);
});

test("when storage itself fails, this store's memory is still what it reads", () => {
  const b = backing();
  const a = createStore(b);
  a.set("k", "mine");
  b.failGet = true;
  assert.equal(a.get("k"), "mine");
});

await finish();
