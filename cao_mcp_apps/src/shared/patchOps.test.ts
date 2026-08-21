// Unit cells for the RFC 6902 apply/diff client (Unit tier).
//
// The property test in patch.test.ts covers the clientDiff -> applyPatch
// round-trip. These targeted unit tests exercise the remaining op branches that
// clientDiff never emits (move / copy / test, array add/remove, whole-document
// replace) plus the error paths, so the view-layer sync client is fully covered.

import { describe, expect, it } from "vitest";
import {
  applyPatch,
  clientDiff,
  deepEqual,
  escapeToken,
  parsePointer,
  unescapeToken,
} from "./patch";
import type { JsonPatchOp } from "./types";

describe("applyPatch — explicit RFC 6902 ops", () => {
  it("adds into an array at an index and with the '-' append token", () => {
    const doc = { items: [1, 2, 3] };
    const inserted = applyPatch(doc, [
      { op: "add", path: "/items/1", value: 99 } as JsonPatchOp,
    ]);
    expect(inserted).toEqual({ items: [1, 99, 2, 3] });

    const appended = applyPatch(doc, [
      { op: "add", path: "/items/-", value: 4 } as JsonPatchOp,
    ]);
    expect(appended).toEqual({ items: [1, 2, 3, 4] });
  });

  it("removes an array element", () => {
    const doc = { items: ["a", "b", "c"] };
    expect(applyPatch(doc, [{ op: "remove", path: "/items/1" }])).toEqual({
      items: ["a", "c"],
    });
  });

  it("replaces the whole document with an empty-pointer replace", () => {
    expect(
      applyPatch({ a: 1 }, [{ op: "replace", path: "", value: { b: 2 } }]),
    ).toEqual({
      b: 2,
    });
  });

  it("moves a value from one location to another", () => {
    const doc = { from: { v: 7 }, to: {} as Record<string, unknown> };
    const moved = applyPatch(doc, [
      { op: "move", from: "/from/v", path: "/to/v" } as JsonPatchOp,
    ]);
    expect(moved).toEqual({ from: {}, to: { v: 7 } });
  });

  it("copies a value without removing the source", () => {
    const doc = { src: { v: 5 }, dst: {} as Record<string, unknown> };
    const copied = applyPatch(doc, [
      { op: "copy", from: "/src/v", path: "/dst/v" } as JsonPatchOp,
    ]);
    expect(copied).toEqual({ src: { v: 5 }, dst: { v: 5 } });
  });

  it("passes a satisfied test op and throws on a failed one", () => {
    const doc = { a: 1 };
    expect(applyPatch(doc, [{ op: "test", path: "/a", value: 1 }])).toEqual(
      doc,
    );
    expect(() =>
      applyPatch(doc, [{ op: "test", path: "/a", value: 2 }]),
    ).toThrow(/test op failed/);
  });

  it("throws on an unsupported op and on a malformed pointer", () => {
    expect(() =>
      applyPatch({}, [
        { op: "frobnicate" as unknown as JsonPatchOp["op"], path: "/a" },
      ]),
    ).toThrow(/unsupported op/);
    expect(() => parsePointer("no-leading-slash")).toThrow(
      /invalid JSON pointer/,
    );
  });

  it("does not mutate the input document", () => {
    const doc = { a: { b: 1 } };
    const snapshot = JSON.stringify(doc);
    applyPatch(doc, [{ op: "replace", path: "/a/b", value: 2 }]);
    expect(JSON.stringify(doc)).toBe(snapshot);
  });
});

describe("pointer token helpers", () => {
  it("escapes and unescapes round-trip", () => {
    expect(escapeToken("a/b~c")).toBe("a~1b~0c");
    expect(unescapeToken("a~1b~0c")).toBe("a/b~c");
  });
});

describe("clientDiff add/remove key cells", () => {
  it("emits add for a new key and remove for a dropped key", () => {
    const ops = clientDiff({ a: 1, gone: true }, { a: 1, fresh: 2 });
    expect(ops).toContainEqual({ op: "add", path: "/fresh", value: 2 });
    expect(ops).toContainEqual({ op: "remove", path: "/gone" });
  });

  it("returns no ops for equal snapshots and a whole-replace for differing scalar roots", () => {
    expect(clientDiff(5, 5)).toEqual([]);
    expect(clientDiff(5, 6)).toEqual([{ op: "replace", path: "", value: 6 }]);
  });

  it("deepEqual distinguishes arrays, objects, and primitives", () => {
    expect(deepEqual([1, 2], [1, 2])).toBe(true);
    expect(deepEqual([1, 2], [1, 3])).toBe(false);
    expect(deepEqual({ a: 1 }, { a: 1 })).toBe(true);
    expect(deepEqual({ a: 1 }, { b: 1 })).toBe(false);
    expect(deepEqual(1, "1")).toBe(false);
  });
});
