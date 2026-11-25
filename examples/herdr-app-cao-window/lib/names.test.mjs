// Bundle mode puts every shared module in one scope, so a top-level name used by two
// lib/*.js files would clash there while it works under the flat copy. This lists the top-level
// declarations of each module and fails on a name two modules declare.
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { assert, finish, test } from "../test/harness.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const DECL = /^(?:export\s+)?(?:async\s+)?(?:function\s*\*?|const|let|var|class)\s+([A-Za-z_$][\w$]*)/;

function topLevelNames(text) {
  const names = [];
  for (const line of text.split("\n")) {
    const m = DECL.exec(line);
    if (m) names.push(m[1]);
  }
  return names;
}

test("no top-level name is declared by two lib/*.js modules", () => {
  const owners = new Map();
  const files = readdirSync(here).filter((f) => f.endsWith(".js"));
  assert.ok(files.length >= 8, `scanned ${files.length} modules`);
  for (const file of files) {
    for (const name of topLevelNames(readFileSync(join(here, file), "utf8"))) {
      owners.set(name, [...(owners.get(name) ?? []), file]);
    }
  }
  const clashes = [...owners].filter(([, where]) => where.length > 1).map(([name, where]) => `${name}: ${where.join(", ")}`);
  assert.deepEqual(clashes, []);
});

test("the scanner sees function, const, let, class and async declarations, exported or not", () => {
  const found = topLevelNames("export const a = 1;\nconst b = 2;\nexport function c() {}\nasync function d() {}\nexport async function e() {}\nlet f;\nclass G {}\n  const nested = 1;\n");
  assert.deepEqual(found, ["a", "b", "c", "d", "e", "f", "G"]);
});

// ---- import bindings count too (bundle mode: the shared modules and one mod share a single scope) ----
// A binding is a top-level declaration, or the local name of an import from a host module ("gpui-kit",
// "herdr", ...). A relative import ("./logic.js") binds nothing new: bundle mode removes that line and the
// name is the shared module's own declaration. This is why lib/ui.js imports `div as uiDiv`: a second
// `div` in the mod's own imports would be a duplicate binding there.
const BINDING_DECL = /^(?:export\s+(?:default\s+)?)?(?:async\s+)?(?:function\s*\*?\s*|const|let|var|class)\s+([A-Za-z_$][\w$]*)/;
const IMPORT = /^import\s+([^;]*?)\s+from\s+["']([^"']+)["']\s*;?/gm;

function importedNames(text) {
  const names = [];
  for (const m of text.matchAll(IMPORT)) {
    if (m[2].startsWith(".")) continue;
    const clause = m[1].replace(/\/\/[^\n]*/g, "");
    const named = /\{([^}]*)\}/.exec(clause);
    const rest = clause.replace(/\{[^}]*\}/, "");
    for (const part of (named ? named[1] : "").split(",")) {
      const id = part.trim().split(/\s+as\s+/).pop().trim();
      if (id) names.push(id);
    }
    for (const part of rest.split(",")) {
      const id = part.trim().replace(/^\*\s+as\s+/, "").trim();
      if (id) names.push(id);
    }
  }
  return names;
}
function bindingsOf(text) {
  const declared = [];
  for (const line of text.split("\n")) {
    const m = BINDING_DECL.exec(line);
    if (m) declared.push(m[1]);
  }
  return [...declared, ...importedNames(text)];
}
// files: {name: source text} that share one scope -> ["binding: a, b"] for each name bound in two files.
function clashesIn(files) {
  const owners = new Map();
  for (const [file, text] of Object.entries(files)) {
    for (const name of new Set(bindingsOf(text))) owners.set(name, [...(owners.get(name) ?? []), file]);
  }
  return [...owners].filter(([, where]) => where.length > 1).map(([name, where]) => `${name}: ${where.join(", ")}`);
}

const libFiles = () => Object.fromEntries(readdirSync(here).filter((f) => f.endsWith(".js") && !f.endsWith(".test.js")).map((f) => [`lib/${f}`, readFileSync(join(here, f), "utf8")]));
const modsDir = join(here, "..", "mods");
const modFiles = () => Object.fromEntries(readdirSync(modsDir, { withFileTypes: true }).filter((d) => d.isDirectory()).map((d) => [
  d.name,
  Object.fromEntries(readdirSync(join(modsDir, d.name)).filter((f) => f.endsWith(".js")).map((f) => [`mods/${d.name}/${f}`, readFileSync(join(modsDir, d.name, f), "utf8")])),
]));

test("the import scanner reads default, named, aliased, namespace and multi-line imports, and skips relative ones", () => {
  const text = [
    'import { div as uiDiv } from "gpui-kit";',
    'import { View, div } from "gpui-kit";',
    'import def, { a, b as c } from "vendor-a";',
    'import * as ns from "vendor-b";',
    'import {',
    '  m1, // a comment',
    '  m2 as m3,',
    '} from "herdr";',
    'import "side-effect-only";',
    'import { fromLib } from "./logic.js";',
    "",
  ].join("\n");
  assert.deepEqual(importedNames(text), ["uiDiv", "View", "div", "a", "c", "def", "ns", "m1", "m3"]);
});
test("the binding scanner sees declarations, a default-exported class and import bindings", () => {
  const text = 'import { x as y } from "herdr";\nexport const a = 1;\nexport default class Mod extends View {}\nasync function f() {}\n  const nested = 1;\n';
  assert.deepEqual(bindingsOf(text), ["a", "Mod", "f", "y"]);
});
test("a mod that imports a name lib/ui.js aliases is reported, and so is a lib file that declares one", () => {
  const ui = 'import { div as uiDiv } from "gpui-kit";\nexport function shapeMark() {}\n';
  assert.deepEqual(clashesIn({ "lib/ui.js": ui, "mods/m/main.js": 'import { uiDiv } from "gpui-kit";\n' }), ["uiDiv: lib/ui.js, mods/m/main.js"]);
  assert.deepEqual(clashesIn({ "lib/ui.js": ui, "lib/other.js": "const uiDiv = 1;\n" }), ["uiDiv: lib/ui.js, lib/other.js"]);
  assert.deepEqual(clashesIn({ "lib/ui.js": ui, "mods/m/main.js": "function shapeMark() {}\n" }), ["shapeMark: lib/ui.js, mods/m/main.js"]);
});
test("a mod that imports a shared name by a relative path, or a host name the shared modules do not bind, is fine", () => {
  const ui = 'import { div as uiDiv } from "gpui-kit";\nexport function shapeMark() {}\n';
  const mod = 'import { View, div } from "gpui-kit";\nimport { shapeMark } from "./ui.js";\nexport default class M extends View {}\n';
  assert.deepEqual(clashesIn({ "lib/ui.js": ui, "mods/m/main.js": mod }), []);
});
test("no binding, declared or imported, is shared by two lib/*.js modules", () => {
  assert.deepEqual(clashesIn(libFiles()), []);
});
test("in each mod's bundle, no binding of the mod is also a binding of a lib/*.js module", () => {
  const lib = libFiles();
  const mods = modFiles();
  assert.ok(Object.keys(mods).length >= 4, `scanned ${Object.keys(mods).length} mod folders`);
  for (const [mod, files] of Object.entries(mods)) assert.deepEqual(clashesIn({ ...lib, ...files }), [], mod);
});
test("no mod declares or imports a name that lib/ui.js exports or aliases", () => {
  const ui = readFileSync(join(here, "ui.js"), "utf8");
  const reserved = new Set(bindingsOf(ui));
  for (const name of ["uiDiv", "uiRow", "uiAgents", "uiRun", "focusTerminal", "shapeMark"]) assert.ok(reserved.has(name), `ui.js binds ${name}`);
  const used = [];
  for (const files of Object.values(modFiles())) {
    for (const [file, text] of Object.entries(files)) for (const name of bindingsOf(text)) if (reserved.has(name)) used.push(`${name} in ${file}`);
  }
  assert.deepEqual(used, []);
});

await finish();
