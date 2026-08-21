// Property + unit tests for the RFC 6902 sync client (shared/patch.ts).
//
// RFC-6902 round-trip: for all snapshots `prev`, `curr`,
//   applyPatch(prev, clientDiff(prev, curr))  deep-equals  curr,
// including pointer-escaping cases (keys containing '/' and '~').

import fc from "fast-check";
import { describe, expect, it } from "vitest";
import { applyPatch, clientDiff, deepEqual, parsePointer } from "./patch";

// A JSON value arbitrary whose object keys are biased to include the JSON
// Pointer escaping characters so round-trip escaping is exercised.
const keyArb = fc.oneof(
  fc.constantFrom("a", "b", "terminals", "sessions", "counts", "scopes"),
  fc.string({ minLength: 1, maxLength: 4 }),
  fc.constantFrom("a/b", "c~d", "~/~", "with/slash", "tilde~key"),
);

const jsonValueArb = fc.letrec((tie) => ({
  value: fc.oneof(
    { depthSize: "small" },
    fc.boolean(),
    fc.integer({ min: -1000, max: 1000 }),
    fc.string({ maxLength: 6 }),
    fc.constant(null),
    fc.array(tie("value"), { maxLength: 4 }),
    fc.dictionary(keyArb, tie("value"), { maxKeys: 5 }),
  ),
})).value;

const objectArb = fc.dictionary(keyArb, jsonValueArb, { maxKeys: 6 });

describe("patch round-trip", () => {
  it("applyPatch(prev, clientDiff(prev, curr)) deep-equals curr", () => {
    fc.assert(
      fc.property(objectArb, objectArb, (prev, curr) => {
        const ops = clientDiff(prev, curr);
        const result = applyPatch(prev, ops);
        expect(deepEqual(result, curr)).toBe(true);
      }),
      { numRuns: 500 },
    );
  });

  it("does not mutate the previous snapshot", () => {
    fc.assert(
      fc.property(objectArb, objectArb, (prev, curr) => {
        const snapshot = JSON.stringify(prev);
        applyPatch(prev, clientDiff(prev, curr));
        expect(JSON.stringify(prev)).toBe(snapshot);
      }),
      { numRuns: 200 },
    );
  });
});

describe("pointer escaping", () => {
  it("round-trips keys containing '/' and '~'", () => {
    const prev = { "a/b": 1, "c~d": 2, plain: 3 };
    const curr = { "a/b": 9, "c~d": 2, plain: 4, "new/key": 5 };
    expect(deepEqual(applyPatch(prev, clientDiff(prev, curr)), curr)).toBe(
      true,
    );
  });

  it("parsePointer decodes escaped tokens", () => {
    expect(parsePointer("/a~1b/c~0d")).toEqual(["a/b", "c~d"]);
    expect(parsePointer("")).toEqual([]);
  });
});

describe("snapshot-shaped diffs", () => {
  it("whole-key replaces terminals/sessions and per-key replaces counts", () => {
    const prev = {
      sessions: [{ id: "s1" }],
      terminals: [],
      counts: { sessions: 1, terminals: 0 },
      scopes: [],
    };
    const curr = {
      sessions: [{ id: "s1" }],
      terminals: [{ id: "t1" }],
      counts: { sessions: 1, terminals: 1 },
      scopes: [],
    };
    const ops = clientDiff(prev, curr);
    const paths = ops.map((o) => o.path);
    expect(paths).toContain("/terminals"); // whole-key replace
    expect(paths).toContain("/counts/terminals"); // per-key scalar replace
    expect(deepEqual(applyPatch(prev, ops), curr)).toBe(true);
  });
});
