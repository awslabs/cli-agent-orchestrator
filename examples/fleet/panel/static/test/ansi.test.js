import { test } from "node:test";
import assert from "node:assert/strict";
import { parseAnsiLine } from "../ansi.js";

test("plain text → one default segment", () => {
  assert.deepEqual(parseAnsiLine("hello"), [{ text: "hello", fg: null, bold: false }]);
});

test("green then reset", () => {
  const segs = parseAnsiLine("\x1b[32mok\x1b[0mbye");
  assert.deepEqual(segs, [
    { text: "ok", fg: 2, bold: false },
    { text: "bye", fg: null, bold: false },
  ]);
});

test("bold + colour", () => {
  const segs = parseAnsiLine("\x1b[1;31mERR\x1b[0m");
  assert.deepEqual(segs, [{ text: "ERR", fg: 1, bold: true }]);
});

test("strips unknown CSI (cursor moves) without emitting text", () => {
  const segs = parseAnsiLine("a\x1b[2Kb");
  assert.deepEqual(segs, [{ text: "ab", fg: null, bold: false }]);
});
