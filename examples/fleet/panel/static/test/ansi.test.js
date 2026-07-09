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

test("bright foreground (90-97) maps to palette 8-15", () => {
  const segs = parseAnsiLine("\x1b[90mgrey\x1b[97mwhite");
  assert.deepEqual(segs, [
    { text: "grey", fg: 8, bold: false },
    { text: "white", fg: 15, bold: false },
  ]);
});

test("bold-off (22) clears bold without touching colour", () => {
  const segs = parseAnsiLine("\x1b[1;33mhot\x1b[22mcool");
  assert.deepEqual(segs, [
    { text: "hot", fg: 3, bold: true },
    { text: "cool", fg: 3, bold: false },
  ]);
});

test("fg-reset (39) drops colour but keeps bold", () => {
  const segs = parseAnsiLine("\x1b[1;34mblue\x1b[39mplain");
  assert.deepEqual(segs, [
    { text: "blue", fg: 4, bold: true },
    { text: "plain", fg: null, bold: true },
  ]);
});

test("adjacent runs with identical style merge into one segment", () => {
  const segs = parseAnsiLine("\x1b[31ma\x1b[31mb");
  assert.deepEqual(segs, [{ text: "ab", fg: 1, bold: false }]);
});
