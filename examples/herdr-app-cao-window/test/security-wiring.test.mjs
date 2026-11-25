// Static check of how the mods call into lib: the window never sends an env value (spike 1.10, design D20), and
// the only thing that could change that is the stdin option. Every call a mod makes to launchPlan, launchConfirm,
// launchBlocked, buildRequest and editorSizeCheck must pass `stdin: STDIN_SUPPORTED` or no stdin key, the cfg()
// that feeds callCao must return no stdin, and the word `stdin` must appear nowhere else in a mod. A mod that
// said `stdin: true` would turn the env rows on in the form while the transport still sent them as arguments.
//
// The scanner is exercised on fixtures first (what it must flag and what it must leave alone), so a scanner that
// finds nothing cannot pass for a clean tree. Plain `node test/security-wiring.test.mjs`.
import { cpSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { assert, finish, mkTmp, test } from "./harness.mjs";
import { leaseFor } from "../lib/shell.js";

const here = dirname(fileURLToPath(import.meta.url));
const MODS = join(here, "..", "mods");

// ctx = the index of the argument that carries the options object.
const GUARDED = Object.freeze({ launchPlan: 1, launchConfirm: 2, launchBlocked: 1, buildRequest: 1, editorSizeCheck: 1 });
const OPENERS = "([{";
const CLOSERS = ")]}";
const REGEX_AFTER = new Set(["", "(", ",", "=", ":", "[", "!", "&", "|", "?", "{", "}", ";", "+", "-", "*", "%", "<", ">", "~", "^"]);

// The source with comments blanked and the inside of strings, templates and regular expressions replaced by `_`
// (the `${ }` code of a template is kept), so that only the structure and the identifiers are left to read.
function codeOf(src) {
  let i = 0;
  const n = src.length;
  function code(untilBrace) {
    let out = "";
    let depth = 0;
    let prev = "";
    while (i < n) {
      const c = src[i];
      const d = src[i + 1];
      if (c === "/" && d === "/") { while (i < n && src[i] !== "\n") { out += " "; i += 1; } continue; }
      if (c === "/" && d === "*") {
        const end = src.indexOf("*/", i + 2);
        const stop = end < 0 ? n : end + 2;
        out += src.slice(i, stop).replace(/[^\n]/g, " ");
        i = stop;
        continue;
      }
      if (c === '"' || c === "'") {
        out += c; i += 1;
        while (i < n && src[i] !== c && src[i] !== "\n") { out += src[i] === "\\" ? "__" : "_"; i += src[i] === "\\" ? 2 : 1; }
        out += c; i += 1; prev = c;
        continue;
      }
      if (c === "`") {
        out += c; i += 1;
        while (i < n && src[i] !== "`") {
          if (src[i] === "\\") { out += "__"; i += 2; continue; }
          if (src[i] === "$" && src[i + 1] === "{") { out += "${"; i += 2; out += code(true); out += "}"; continue; }
          out += src[i] === "\n" ? "\n" : "_"; i += 1;
        }
        out += c; i += 1; prev = c;
        continue;
      }
      if (c === "/" && (REGEX_AFTER.has(prev) || /(?:^|[^\w$])(?:return|typeof|case)$/.test(out.trimEnd().slice(-8)))) {
        out += c; i += 1;
        let inClass = false;
        while (i < n && src[i] !== "\n" && (inClass || src[i] !== "/")) {
          if (src[i] === "\\") { out += "__"; i += 2; continue; }
          if (src[i] === "[") inClass = true; else if (src[i] === "]") inClass = false;
          out += "_"; i += 1;
        }
        out += "/"; i += 1;
        while (i < n && /[a-z]/.test(src[i])) { out += src[i]; i += 1; }
        prev = "/";
        continue;
      }
      if (untilBrace) {
        if (c === "{") depth += 1;
        if (c === "}") { if (depth === 0) { i += 1; return out; } depth -= 1; }
      }
      out += c; i += 1;
      if (!/\s/.test(c)) prev = c;
    }
    return out;
  }
  return code(false);
}

// The text between the bracket at `open` and its partner, split at the commas that are not nested.
function partsAt(code, open) {
  const parts = [];
  let depth = 0;
  let from = open + 1;
  for (let i = open; i < code.length; i += 1) {
    const c = code[i];
    if (OPENERS.includes(c)) depth += 1;
    else if (CLOSERS.includes(c)) {
      depth -= 1;
      if (depth === 0) { parts.push(code.slice(from, i)); return { parts, end: i }; }
    } else if (c === "," && depth === 1) { parts.push(code.slice(from, i)); from = i + 1; }
  }
  return null;   // unbalanced
}

// Every problem in one source: [{what, where}], and how many guarded calls, valid stdin options and cfg bodies it held.
export function wiringOf(src, file = "source") {
  const code = codeOf(src);
  const problems = [];
  const lineOf = (at) => `${file}:${code.slice(0, at).split("\n").length}`;
  const seen = { calls: Object.fromEntries(Object.keys(GUARDED).map((k) => [k, 0])), stdinOptions: 0, cfgBodies: 0 };

  for (const [name, ctxAt] of Object.entries(GUARDED)) {
    for (const m of code.matchAll(new RegExp(`(?<![\\w$.])${name}\\s*\\(`, "g"))) {
      if (/\b(?:function|import|export)\s+$/.test(code.slice(Math.max(0, m.index - 12), m.index))) continue;
      const open = m.index + m[0].length - 1;
      const got = partsAt(code, open);
      if (!got) { problems.push({ what: `${name}( is not closed`, where: lineOf(m.index) }); continue; }
      seen.calls[name] += 1;
      const arg = got.parts[ctxAt]?.trim();
      if (arg === undefined || arg === "") continue;   // no options: stdin is off
      if (!arg.startsWith("{")) { problems.push({ what: `${name} takes its options from \`${arg.slice(0, 30)}\`, which this check cannot read`, where: lineOf(m.index) }); continue; }
      const props = partsAt(arg, 0)?.parts.map((p) => p.trim()).filter(Boolean) ?? [];
      for (const p of props) {
        if (p.startsWith("...") || /^["'`\[]/.test(p)) problems.push({ what: `${name} options hold \`${p.slice(0, 30)}\`, which this check cannot read`, where: lineOf(m.index) });
        else if (/^stdin\b/.test(p)) {
          if (/^stdin\s*:\s*STDIN_SUPPORTED$/.test(p)) seen.stdinOptions += 1;
          else problems.push({ what: `${name} passes \`${p.slice(0, 30)}\`, not stdin: STDIN_SUPPORTED`, where: lineOf(m.index) });
        }
      }
    }
  }

  for (const m of code.matchAll(/(?<![\w$.])cfg\s*\([^)]*\)\s*\{/g)) {
    const open = m.index + m[0].length - 1;
    const body = partsAt(code.slice(open), 0);
    const end = body ? open + body.end : code.length;
    seen.cfgBodies += 1;
    if (/\bstdin\b/.test(code.slice(open, end))) problems.push({ what: "cfg() returns a stdin option", where: lineOf(m.index) });
  }

  const everywhere = [...code.matchAll(/\bstdin\b/g)].length;
  if (everywhere !== seen.stdinOptions) problems.push({ what: `the word stdin appears ${everywhere} times, ${seen.stdinOptions} of them as the allowed option`, where: file });
  if (/\b(?:const|let|var|function)\s+STDIN_SUPPORTED\b/.test(code)) problems.push({ what: "STDIN_SUPPORTED is defined here, not imported from cao.js", where: file });
  return { problems, seen };
}

// ---- the approvals key ------------------------------------------------------------------------------
// `caowin.approvals` is saved to disk with the rest of shared storage and holds {workflow, planId, at} rows only
// (design D28; the Run form's inputs never reach it, F28). The one safe way to write it is to pass the result of
// recordApproval( or clearApproval( as the value, because those two build every row from three values read by
// name. So every call whose first argument is the key (the literal, `KEYS.approvals`, a constant holding either,
// or a property of an object literal that holds the literal) and whose function is not a read must have a
// second argument that is such a call. Reads are get, getLocal, getItem and shared.
const APPROVALS_LITERAL = "caowin.approvals";
const KEY_READS = new Set(["get", "getLocal", "getItem", "shared"]);
const NOT_CALLS = new Set(["if", "for", "while", "switch", "catch", "function", "return", "typeof", "await", "new"]);

// [from, to] of each argument of the call whose "(" is at `open` in `code`, and where it ends; null when unbalanced.
function argRanges(code, open) {
  const ranges = [];
  let depth = 0;
  let from = open + 1;
  for (let i = open; i < code.length; i += 1) {
    const c = code[i];
    if (OPENERS.includes(c)) depth += 1;
    else if (CLOSERS.includes(c)) {
      depth -= 1;
      if (depth === 0) { ranges.push([from, i]); return { ranges, end: i }; }
    } else if (c === "," && depth === 1) { ranges.push([from, i]); from = i + 1; }
  }
  return null;
}

// The source twice, at the same positions: `code` (comments blanked, strings and regular expressions replaced by `_`)
// and `keep` (comments blanked, strings kept). null when the scanner lost its place.
function sourceViews(src) {
  const code = codeOf(src);
  if (code.length !== src.length) return null;
  return { code, keep: [...src].map((c, i) => (code[i] === " " && c !== " " ? " " : c)).join("") };
}

export function approvalsProblems(src, file = "source") {
  const views = sourceViews(src);
  if (views === null) return [{ what: "the scanner lost its place in the source", where: file }];
  const { code, keep } = views;
  const lit = `["']${APPROVALS_LITERAL.replace(".", "\\.")}["']`;
  const names = new Set();
  for (const m of keep.matchAll(new RegExp(`(?:const|let|var)\\s+([A-Za-z_$][\\w$]*)\\s*=\\s*(?:${lit}|KEYS\\s*\\.\\s*approvals\\b)`, "g"))) names.add(m[1]);
  const objects = new Set(["KEYS"]);
  const props = new Set(["approvals"]);
  for (const m of keep.matchAll(new RegExp(`(?:const|let|var)\\s+([A-Za-z_$][\\w$]*)\\s*=\\s*\\{[^}]*?\\b([A-Za-z_$][\\w$]*)\\s*:\\s*${lit}`, "g"))) { objects.add(m[1]); props.add(m[2]); }
  const isKey = (arg) => new RegExp(`^${lit}$`).test(arg) || names.has(arg)
    || [...objects].some((o) => [...props].some((p) => new RegExp(`^${o}\\s*\\.\\s*${p}$`).test(arg)));
  const problems = [];
  for (const m of code.matchAll(/([A-Za-z_$][\w$]*)\s*\(/g)) {
    if (NOT_CALLS.has(m[1])) continue;
    const open = m.index + m[0].length - 1;
    const got = argRanges(code, open);
    if (!got) continue;
    const args = got.ranges.map(([a, b]) => keep.slice(a, b).trim());
    if (args.length === 0 || !isKey(args[0]) || KEY_READS.has(m[1])) continue;
    if (!/^(?:recordApproval|clearApproval)\s*\(/.test(args[1] ?? "")) {
      problems.push({ what: `${m[1]}( writes ${APPROVALS_LITERAL} with \`${(args[1] ?? "nothing").slice(0, 40)}\`, not the result of recordApproval( or clearApproval(`, where: `${file}:${code.slice(0, m.index).split("\n").length}` });
    }
  }
  return problems;
}

// Every .js file of each mod folder, not only main.js: a mod may split its code over files (the panel has a sidebar
// host and a dialog host), and a guarded call in any of them is a call. Sorted, so a failure names the same file each run.
// ---- the dialog host's lease (design D29, F31) ------------------------------------------------------
// `caowin.dialog` is {name, until}; the dialog renews it every second from its own timer and clears it when it
// closes. Rules, read from the source of dialog.js:
//   - leaseFor( is called from the timer (the callback of timer.every, or a function it calls);
//   - every function that writes the key first calls a guard that reads the stored record and says yes only when its
//     name is the dialog's own or the key is empty, so a stalled dialog cannot overwrite or clear the lease of
//     the one that replaced it;
//   - the close function stops the timer (.cancel( ) before it calls a function that writes the key;
//   - the guard compares the stored record's name with the dialog's own name (`panel.dl.name`), not with itself;
//   - the timer's period is at most half the lease, so a renewal can be missed once without the lease lapsing;
//   - the close function sets the `closed` flag before it cancels the timer, and the function the timer calls
//     returns on that flag before it renews, because the typings do not say what timer.every returns and a renewal
//     already queued must not write the lease back after the clear.
const LEASE_LITERAL = "caowin.dialog";
const LEASE_MS = leaseFor("x", 0).until;

// name -> [from, to) of the body of every `function name(` in `code`.
function functionBodies(code) {
  const out = new Map();
  for (const m of code.matchAll(/(?<![\w$.])function\s+([A-Za-z_$][\w$]*)\s*\(/g)) {
    const args = argRanges(code, m.index + m[0].length - 1);
    if (!args) continue;
    const open = code.indexOf("{", args.end);
    if (open < 0) continue;
    let depth = 0;
    for (let i = open; i < code.length; i += 1) {
      if (code[i] === "{") depth += 1;
      else if (code[i] === "}") { depth -= 1; if (depth === 0) { out.set(m[1], [open + 1, i]); break; } }
    }
  }
  return out;
}

export function leaseProblems(src, file = "dialog.js", leaseMs = LEASE_MS) {
  const views = sourceViews(src);
  if (views === null) return [{ what: "the scanner lost its place in the source", where: file }];
  const { code, keep } = views;
  const fns = functionBodies(code);
  const codeOfFn = (n) => (fns.has(n) ? code.slice(...fns.get(n)) : "");
  const keepOfFn = (n) => (fns.has(n) ? keep.slice(...fns.get(n)) : "");
  const names = new Set();
  for (const m of keep.matchAll(/(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*["']caowin\.dialog["']/g)) names.add(m[1]);
  const key = `(?:${[...names, `["']${LEASE_LITERAL.replace(".", "\\.")}["']`].join("|")})`;
  const writes = new RegExp(`\\.set\\(\\s*${key}\\s*,`);
  const reads = new RegExp(`\\.get\\(\\s*${key}\\s*\\)`);
  const problems = [];
  const say = (what) => problems.push({ what, where: file });

  // 1. leaseFor( is reached from the timer
  const timer = /timer\.every\(/.exec(code);
  const timerArgs = timer ? argRanges(code, timer.index + timer[0].length - 1) : null;
  if (!timerArgs) say("no timer.every( renews the lease");
  else {
    const seen = new Set();
    const reach = (text, depth) => {
      if (/(?<![\w$.])leaseFor\s*\(/.test(text)) return true;
      if (depth === 0) return false;
      return [...text.matchAll(/(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(/g)].some((c) => {
        if (seen.has(c[1]) || !fns.has(c[1])) return false;
        seen.add(c[1]);
        return reach(codeOfFn(c[1]), depth - 1);
      });
    };
    if (!reach(code.slice(timerArgs.ranges[0][0], timerArgs.end), 3)) say("leaseFor( is not called from the timer");
    // the period: a number, or a constant holding one
    const periodText = keep.slice(...timerArgs.ranges[0]).trim();
    const period = /^\d[\d_]*$/.test(periodText) ? Number(periodText.replaceAll("_", ""))
      : Number(new RegExp(`(?:const|let|var)\\s+${periodText.replace(/[^\w$]/g, "")}\\s*=\\s*(\\d[\\d_]*)\\s*;`).exec(keep)?.[1]?.replaceAll("_", ""));
    if (!Number.isFinite(period)) say(`the timer's period \`${periodText.slice(0, 30)}\` is not a number this check can read`);
    else if (period * 2 > leaseMs) say(`the timer runs every ${period} ms against a lease of ${leaseMs} ms: it must be at most half of it`);
    // the function the timer calls leaves at once once the dialog is closed, before it calls anything else of the file
    const callee = [...code.slice(timerArgs.ranges[0][1], timerArgs.end).matchAll(/(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(/g)].map((c) => c[1]).find((n) => fns.has(n));
    if (callee === undefined) say("the timer's callback calls no function of the file");
    else {
      const body = keepOfFn(callee);
      const exit = body.search(/\bif \([^)]*\.closed\b[^)]*\) return\b/);
      const firstCall = Math.min(...[...fns.keys()].filter((n) => n !== callee).map((n) => body.search(new RegExp(`(?<![\\w$.])${n}\\s*\\(`))).filter((i) => i >= 0));
      if (exit < 0) say(`${callee}, which the timer calls, does not return when the dialog is closed`);
      else if (exit > firstCall) say(`${callee} calls into the file before it checks that the dialog is closed`);
    }
  }

  // 2. a guard reads the stored record, compares its name with the dialog's own and accepts an empty key
  const guardOk = (g) => {
    const body = keepOfFn(g);
    const record = new RegExp(`(?:const|let)\\s+([A-Za-z_$][\\w$]*)\\s*=\\s*[^;]*\\.get\\(\\s*${key}\\s*\\)`).exec(body)?.[1];
    const own = record !== undefined && new RegExp(`\\b${record}\\.name\\s*===\\s*panel\\.dl\\.name\\b|\\bpanel\\.dl\\.name\\s*===\\s*${record}\\.name\\b`).test(body);
    return reads.test(body) && own && /(?:===?\s*(?:undefined|null)|==\s*null)/.test(body);
  };
  const writers = [...fns.keys()].filter((n) => writes.test(keepOfFn(n)));
  if (writers.length < 2) say(`${writers.length} function(s) write the lease: a renewal and a clear are expected`);
  for (const w of writers) {
    const body = keepOfFn(w);
    const at = body.search(writes);
    const guards = [...body.slice(0, at).matchAll(/(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(/g)].map((c) => c[1]).filter((g) => fns.has(g) && guardOk(g));
    if (guards.length === 0) say(`${w} writes the lease without first reading the stored record and checking its name`);
  }

  // 3. the close function stops the timer, then clears
  const close = codeOfFn("closeDialogHost");
  const stop = close.search(/\.cancel\s*(?:\?\.)?\(/);
  const clears = writers.map((w) => close.search(new RegExp(`(?<![\\w$.])${w}\\s*\\(`))).filter((i) => i >= 0);
  const closedSet = close.search(/\.closed\s*=\s*true\b/);
  if (!fns.has("closeDialogHost")) say("there is no closeDialogHost function");
  else if (closedSet < 0) say("closeDialogHost does not set the closed flag");
  else if (stop >= 0 && closedSet > stop) say("closeDialogHost sets the closed flag after it stops the timer");
  else if (stop < 0) say("closeDialogHost does not stop the timer");
  else if (clears.length === 0) say("closeDialogHost does not clear the lease");
  else if (Math.min(...clears) < stop) say("closeDialogHost clears the lease before it stops the timer");
  return problems;
}

const modFilesIn = (root) => readdirSync(root, { withFileTypes: true }).filter((e) => e.isDirectory())
  .flatMap((e) => readdirSync(join(root, e.name)).filter((f) => f.endsWith(".js")).sort().map((f) => join(root, e.name, f)));
const modFiles = () => modFilesIn(MODS);

// ---- the scanner itself ----------------------------------------------------------------------------

test("the scanner leaves a call with `stdin: STDIN_SUPPORTED` and a call with no options alone, and counts them", () => {
  const src = [
    "const plan = launchPlan(form, { stdin: STDIN_SUPPORTED });",
    "const ok = launchConfirm(form, kind, { stdin: STDIN_SUPPORTED }, typed);",
    "const b = launchBlocked(form, { stdin: STDIN_SUPPORTED });",
    "const r = buildRequest(sub, { resolved: resolvedProvider(sub, this.profileOf(sub.profile)), stdin: STDIN_SUPPORTED });",
    "const e = editorSizeCheck(nw.form, { stdin: STDIN_SUPPORTED, expectedHash: nw.expectedHash });",
    "const bare = launchPlan(form);",
    "cfg(m) { return { machine: m.machine, port: m.props?.caoPort }; }",
  ].join("\n");
  const { problems, seen } = wiringOf(src);
  assert.deepEqual(problems, []);
  assert.deepEqual(seen.calls, { launchPlan: 2, launchConfirm: 1, launchBlocked: 1, buildRequest: 1, editorSizeCheck: 1 });
  assert.equal(seen.stdinOptions, 5);
  assert.equal(seen.cfgBodies, 1);
});

test("the scanner flags every other way to give a guarded call a stdin option", () => {
  const bad = [
    "launchPlan(form, { stdin: true });",
    "launchPlan(form, { stdin: !STDIN_SUPPORTED });",
    "launchPlan(form, { stdin: STDIN_SUPPORTED || true });",
    "launchPlan(form, { stdin });",
    "launchPlan(form, { stdin: STDIN_SUPPORTED_TOO });",
    "launchPlan(form, { 'stdin': true });",
    "launchPlan(form, ctx);",
    "launchPlan(form, { ...ctx });",
    "launchPlan(form, { [key]: true });",
    "launchConfirm(form, kind, { stdin: true }, typed);",
    "launchConfirm(form, kind, ctx, typed);",
    "launchBlocked(form, { stdin: this.stdin });",
    "buildRequest(sub, { resolved, stdin: 1 });",
    "editorSizeCheck(form, { stdin: true, expectedHash });",
    "launchPlan(form, { stdin: STDIN_SUPPORTED }); launchPlan(form, { stdin: true });",
    "launchPlan(\n  form,\n  {\n    stdin:\n      true,\n  },\n);",
  ];
  for (const src of bad) assert.ok(wiringOf(src).problems.length > 0, `not flagged: ${src}`);
});

test("the scanner flags a stdin anywhere else: a cfg() that returns it, an exec option, a property read, a redefined constant", () => {
  const bad = [
    "cfg(m) { return { machine: m.machine, stdin: true }; }",
    "cfg(m) { return { machine: m.machine, stdin: STDIN_SUPPORTED }; }",
    "cfg(m) { const c = { machine: m.machine }; c.stdin = true; return c; }",
    "await exec('curl', args, { on: m, stdin: text });",
    "const on = ctx.stdin;",
    "const STDIN_SUPPORTED = true;",
    "let STDIN_SUPPORTED = false;",
    "function STDIN_SUPPORTED() { return true; }",
  ];
  for (const src of bad) assert.ok(wiringOf(src).problems.length > 0, `not flagged: ${src}`);
});

test("the scanner does not read comments, strings, templates or regular expressions as code", () => {
  const fine = [
    "// launchPlan(form, { stdin: true });",
    "/* launchPlan(form, ctx); stdin */ const a = 1;",
    "const s = 'launchPlan(form, { stdin: true })';",
    'const t = "stdin: true";',
    "const u = `launchPlan(form, ${x}, { stdin: true })`;",
    "const r = /stdin: true|launchPlan\\(/.test(x);",
    "const q = /[\"'`]/.test(x); launchPlan(form, { stdin: STDIN_SUPPORTED });",
    "const k = `a ${ launchPlan(form, { stdin: STDIN_SUPPORTED }) } b`;",
    "function launchPlan(form, ctx = {}) { return ctx; }",
    "import { launchPlan, buildRequest } from './launch.js';",
    "this.launchPlan(form, ctx);",
  ];
  for (const src of fine) assert.deepEqual(wiringOf(src).problems, [], src);
  assert.equal(wiringOf(fine[7]).seen.calls.launchPlan, 1, "a call inside a template's ${ } is code");
  assert.equal(wiringOf(fine[8]).seen.calls.launchPlan, 0, "a definition is not a call");
});

test("the scanner reports an unclosed call instead of passing it", () => {
  assert.ok(wiringOf("launchPlan(form, { stdin: true }").problems.length > 0);
});

// ---- the mods ---------------------------------------------------------------------------------------

test("every guarded call in the mods passes `stdin: STDIN_SUPPORTED` or no stdin, no cfg() returns stdin, and stdin is named nowhere else", () => {
  const files = modFiles();
  assert.ok(files.length >= 5, `found ${files.length} mod files`);
  for (const file of files) {
    const { problems } = wiringOf(readFileSync(file, "utf8"), file.slice(file.indexOf("mods")));
    assert.deepEqual(problems, [], `${file}`);
  }
});

test("the scan reads the real calls: each guarded function is called in the mods, a cfg() exists, and the options are all STDIN_SUPPORTED", () => {
  const total = { calls: Object.fromEntries(Object.keys(GUARDED).map((k) => [k, 0])), stdinOptions: 0, cfgBodies: 0 };
  for (const file of modFiles()) {
    const { seen } = wiringOf(readFileSync(file, "utf8"));
    for (const k of Object.keys(GUARDED)) total.calls[k] += seen.calls[k];
    total.stdinOptions += seen.stdinOptions;
    total.cfgBodies += seen.cfgBodies;
  }
  for (const [name, count] of Object.entries(total.calls)) assert.ok(count >= 1, `${name} is not called in any mod: the check would pass without reading it`);
  assert.ok(total.stdinOptions >= 5, `only ${total.stdinOptions} stdin options were read`);
  assert.ok(total.cfgBodies >= 1, "no cfg() was found");
});

test("the mods take STDIN_SUPPORTED from cao.js, whose shipped value is false, so no env value travels", async () => {
  const cao = await import("../lib/cao.js");
  assert.equal(cao.STDIN_SUPPORTED, false);
  // Whichever file of a mod names it takes it from cao.js, and a file of the panel does (it holds the forms).
  const IMPORT = /import \{[^}]*\bSTDIN_SUPPORTED\b[^}]*\} from "\.\/cao\.js"/;
  const naming = modFiles().filter((file) => /\bSTDIN_SUPPORTED\b/.test(codeOf(readFileSync(file, "utf8"))));
  assert.ok(naming.some((file) => file.includes(join("cao-window-panel", ""))), "no file of the panel names STDIN_SUPPORTED");
  for (const file of naming) assert.match(readFileSync(file, "utf8"), IMPORT, file);
});


// ---- a mod split over several files (tasks 21.1, 21.3, 21.4) ----------------------------------------

// Every problem of every .js file of a mods folder, [{file, what, where}], as the scanner reads them.
const scanMods = (root) => modFilesIn(root).flatMap((file) => {
  const rel = file.slice(root.length + 1);
  return wiringOf(readFileSync(file, "utf8"), rel).problems.map((p) => ({ file: rel, ...p }));
});

// A copy of the mods folder in a temporary directory, to plant a file in.
function plantedCopy(rel, text) {
  const root = join(mkTmp("caowin-wiring-"), "mods");
  cpSync(MODS, root, { recursive: true });
  writeFileSync(join(root, rel), text);
  return root;
}

test("a guarded call with stdin in a second file of a mod folder is flagged", () => {
  assert.deepEqual(scanMods(MODS), [], "the real mods are clean first, so the planted file is the only difference");
  const root = plantedCopy(join("cao-window-panel", "extra.js"), "export function send(form) { return launchPlan(form, { stdin: true }); }\n");
  const found = scanMods(root);
  assert.ok(found.length > 0, "the planted call was not seen");
  assert.ok(found.every((p) => p.file === join("cao-window-panel", "extra.js")), JSON.stringify(found));
});


// ---- the approvals key (design D28 and D29, F28) ------------------------------------------------------

test("a write of the approvals key that is not recordApproval or clearApproval is flagged", () => {
  const scan = (root) => modFilesIn(root).flatMap((file) => approvalsProblems(readFileSync(file, "utf8"), file.slice(root.length + 1)));
  assert.deepEqual(scan(MODS), [], "no file of the real mods writes the key but through the two functions");
  const root = plantedCopy(join("cao-window-panel", "leak.js"), "const KEYS = { approvals: \"caowin.approvals\" };\nexport function leak(record) { setShared(KEYS.approvals, record); }\n");
  const found = scan(root);
  assert.equal(found.length, 1, JSON.stringify(found));
  assert.equal(found[0].where.split(":")[0], join("cao-window-panel", "leak.js"));
});

test("the approvals scan leaves reads and the two row builders alone, and flags every other way to write the key", () => {
  const head = 'const KEYS = { approvals: "caowin.approvals", other: "caowin.other" };\nconst APPROVALS = "caowin.approvals";\n';
  const fine = [
    "setShared(KEYS.approvals, recordApproval(rows, name, outcome, now));",
    "this.store.set(KEYS.approvals, clearApproval(rows, name));",
    "setShared(\"caowin.approvals\", recordApproval(\n  rows, name, outcome, Date.now()));",
    "const rows = this.store.get(KEYS.approvals);",
    "const rows = shared(APPROVALS);",
    "const rows = shared('caowin.approvals');",
    "this.store.set(KEYS.other, record);",
    "// setShared(KEYS.approvals, record);",
    "const s = 'setShared(KEYS.approvals, record)';",
    "const t = `setShared(${KEYS.approvals}, record)`;",
    "this.approvals.set(name, record);",
    "Object.keys(this.approvals);",
  ];
  for (const src of fine) assert.deepEqual(approvalsProblems(head + src), [], src);
  const bad = [
    "setShared(KEYS.approvals, record);",
    "this.store.set(KEYS.approvals, rows);",
    "this.store.set(KEYS.approvals, [...rows, { workflow: name, inputs }]);",
    "this.store.set(KEYS.approvals, rows.filter((r) => r.workflow !== name));",
    "setShared(\"caowin.approvals\", record);",
    "setShared('caowin.approvals', record);",
    "setShared(APPROVALS, record);",
    "this.store.set(KEYS.approvals);",
    "localStorage.setItem(APPROVALS, JSON.stringify(rows));",
    "this.store.setLocal(KEYS.approvals, recordApprovalLater(rows));",
    "setShared(KEYS.approvals, next); const next = recordApproval(rows, name, outcome, now);",
    "const key = KEYS.approvals; setShared(key, record);",
    "writeKey(KEYS.approvals, record);",
    "setShared(KEYS.approvals, record, recordApproval(rows, name, outcome, now));",
  ];
  for (const src of bad) assert.ok(approvalsProblems(head + src).length > 0, `not flagged: ${src}`);
  assert.equal(approvalsProblems("setShared(KEYS.approvals, record);").length, 1, "KEYS.approvals is the key even when KEYS is defined in another file");
  assert.equal(approvalsProblems("setShared(\"caowin.approvals\", rows);\nsetShared(\"caowin.approvals\", clearApproval(rows, w));").length, 1, "the problem is the first call, not the second");
});


// ---- the dialog host (task 21.4) ------------------------------------------------------------------------

const LEASE_OK = [
  'const KEY = "caowin.dialog";',
  "function ownsLease(panel) { const record = panel.store.get(KEY); return record === undefined || record === null || record.name === panel.dl.name; }",
  "function renewLease(panel) { if (ownsLease(panel)) panel.store.set(KEY, leaseFor(panel.dl.name, Date.now())); }",
  "function clearLease(panel) { if (ownsLease(panel)) panel.store.set(KEY, null); }",
  "const TICK_MS = 1000;",
  "function tick(panel) { if (panel.dl.closed) return; renewLease(panel); }",
  "export function init(panel, cx) { panel.dl.lease = cx.timer.every(TICK_MS, (cx2) => tick(panel)); }",
  "export async function closeDialogHost(panel, cx) { panel.dl.closed = true; panel.dl.lease.cancel(); clearLease(panel); }",
  "",
].join("\n");

test("dialog host renews and clears its lease", () => {
  const src = readFileSync(join(MODS, "cao-window-panel", "dialog.js"), "utf8");
  assert.deepEqual(leaseProblems(src, "dialog.js"), []);
  assert.match(src, /leaseFor\(/, "the host really calls leaseFor");
  assert.equal(LEASE_MS, 10_000, "the lease the scan measures the timer against is the one shell.js ships");
  assert.match(src, /timer\.every\(DL_TICK_MS,/, "the period the scan reads is the one the host uses");
});

test("the lease scan passes a host that follows the rules and flags each rule broken", () => {
  assert.deepEqual(leaseProblems(LEASE_OK), []);
  const broken = {
    "renewal without a guard": LEASE_OK.replace("if (ownsLease(panel)) panel.store.set(KEY, leaseFor", "panel.store.set(KEY, leaseFor"),
    "clear without a guard": LEASE_OK.replace("if (ownsLease(panel)) panel.store.set(KEY, null)", "panel.store.set(KEY, null)"),
    "a guard that never reads the record": LEASE_OK.replace("panel.store.get(KEY)", "undefined"),
    "a guard that ignores the name": LEASE_OK.replace(" || record.name === panel.dl.name", ""),
    "a guard that refuses an empty key": LEASE_OK.replace("record === undefined || record === null || ", ""),
    "a renewal that no timer calls": LEASE_OK.replace("cx.timer.every(TICK_MS, (cx2) => tick(panel))", "cx.timer.every(TICK_MS, (cx2) => noop(panel))"),
    "a lease written once at open and never renewed": LEASE_OK.replace("if (panel.dl.closed) return; renewLease(panel); }", "if (panel.dl.closed) return; }"),
    "a guard that compares the record with itself": LEASE_OK.replace("record.name === panel.dl.name", "record.name === record.name"),
    "a guard that compares the dialog with itself": LEASE_OK.replace("record.name === panel.dl.name", "panel.dl.name === panel.dl.name"),
    "a timer of 60 s against a 10 s lease": LEASE_OK.replace("const TICK_MS = 1000;", "const TICK_MS = 60000;"),
    "a timer of 6 s, more than half the lease": LEASE_OK.replace("const TICK_MS = 1000;", "const TICK_MS = 6_000;"),
    "a period that is not a number": LEASE_OK.replace("const TICK_MS = 1000;", "const TICK_MS = computePeriod();"),
    "a close that never sets the closed flag": LEASE_OK.replace("panel.dl.closed = true; ", ""),
    "a close that sets the closed flag after the timer stops": LEASE_OK.replace("panel.dl.closed = true; panel.dl.lease.cancel();", "panel.dl.lease.cancel(); panel.dl.closed = true;"),
    "a tick that does not look at the closed flag": LEASE_OK.replace("if (panel.dl.closed) return; ", ""),
    "a tick that renews before it looks": LEASE_OK.replace("if (panel.dl.closed) return; renewLease(panel);", "renewLease(panel); if (panel.dl.closed) return;"),
    "a close that never stops the timer": LEASE_OK.replace("panel.dl.lease.cancel(); ", ""),
    "a close that clears first": LEASE_OK.replace("panel.dl.lease.cancel(); clearLease(panel);", "clearLease(panel); panel.dl.lease.cancel();"),
    "a close that never clears": LEASE_OK.replace("clearLease(panel); }", "}"),
    "no close function": LEASE_OK.replace("closeDialogHost", "somethingElse"),
    "the key written under its literal, unguarded": `${LEASE_OK}function leak(p) { p.store.set("caowin.dialog", null); }\n`,
  };
  for (const [what, src] of Object.entries(broken)) {
    assert.notEqual(src, LEASE_OK, `the variant "${what}" changed nothing, so it proves nothing`);
    assert.ok(leaseProblems(src).length > 0, `not flagged: ${what}`);
  }
  assert.deepEqual(leaseProblems(`// ${LEASE_OK.replaceAll("\n", "\n// ")}`).map((p) => p.what).length > 0, true, "a source with only comments holds no renewal");
});

// ---- the forms in the dialog host (tasks 21.5, 21.6) ----------------------------------------------------------

test("the guarded calls of the New session form are read in dialog.js, and no env value can reach a request from it", () => {
  const text = readFileSync(join(MODS, "cao-window-panel", "dialog.js"), "utf8");
  const { problems, seen } = wiringOf(text, "dialog.js");
  assert.deepEqual(problems, []);
  for (const name of ["launchPlan", "launchConfirm", "launchBlocked", "buildRequest"]) assert.ok(seen.calls[name] >= 1, `${name} is not called in dialog.js: the scan would pass without reading the form`);
  assert.ok(seen.stdinOptions >= 4, `only ${seen.stdinOptions} stdin options were read in dialog.js`);
  assert.match(text, /import \{[^}]*\bSTDIN_SUPPORTED\b[^}]*\} from "\.\/cao\.js"/, "STDIN_SUPPORTED comes from cao.js");
  const code = codeOf(text);
  assert.equal([...code.matchAll(/env_vars/g)].length, 0, "the form never names the body key of env values");
  const calls = [...code.matchAll(/(?<![\w$.])callCao\s*\(([^,]*),([^,]*),/g)].map((m) => `${m[1].trim()}, ${m[2].trim()}`);
  assert.ok(calls.length >= 6, `${calls.length} callCao calls read`);
  for (const c of calls) assert.match(c, /^dlExec, panel\.cfg\(/, "every request goes through callCao with the mod's exec and the panel's cfg (no stdin)");
});


// ---- what the dialog host writes to storage (tasks 21.7, 21.8; design D28, D29 "Values stay in memory", F28) ----
// Shared storage and local storage are saved to disk. The dialog holds a workflow Run form's input values, its
// working directory and the server's words in its own memory, and writes only names, ids, counts and times: every
// write of the host is listed here with the one shape of value it may carry, so a value built from the form (or
// from an outcome, or from the in-memory approval record) is a problem, whatever key it goes to. A write that
// matches no line of the table is a problem too, so a new key or a new writer has to be added here on purpose.
// A write of storage: `<...>store.set(`, any `setLocal(`, `setShared(`, `setItem(` or a file write. A `.set(` on a Map
// or a text state is not storage and is not read. Three more sinks are read the same way (F2 of the review of 20 and 21):
//   - a notification (`notify` of the host, under whatever name a file imports it), which leaves the window as a toast
//     and a system message, so its one allowed shape is listed too;
//   - `setShared` of the host, under whatever name a file imports it;
//   - the store itself used in any way but `.get(`, `.getLocal(`, `.notice(`, `.set(` and `.setLocal(`: an alias
//     (`const st = panel.store`), a destructuring, a pass to another function, `store["set"]` or a method read without
//     its call would hide a write from the table, which keys a write by the text of its receiver.
const STORAGE_WRITERS = /(?:\bstore\s*\.\s*set|[\w$.]*\.?setLocal|[\w$.]*\bsetShared|[\w$.]*\.setItem|[\w$.]*\bwriteFile\w*|[\w$.]*\bappendFile\w*)\s*\(/g;
const WRITE_TABLE = [
  ["DL_LEASE_KEY", /^(?:leaseFor\(panel\.dl\.name, Date\.now\(\)\)|null)$/],
  ["DL_RSIDE_KEY", /^fx\.rside$/],
  ["DL_SELECTION_KEY", /^\{ kind: "session", name: result\.session \}$/],
  ["DL_FLOWS_STAMP_KEY", /^Date\.now\(\)$/],
  ["DL_WORKFLOWS_STAMP_KEY", /^Date\.now\(\)$/],
  ["DL_APPROVALS_KEY", { call: ["recordApproval", "clearApproval"] }],
  ["DL_AUTHORS_KEY", { call: ["recordAuthor", "afterLaunch"], or: /^next\.authors$/ }],
  ["DL_DRAFTS_KEY", { call: ["recordDraft"], or: /^next\.drafts$/ }],
  ['"caowin.lastWorkdir"', /^submission\.workdir$/],
];
// What each file of the panel mod may write and say. A file that is not listed may write nothing and notify nothing, so
// a new file has to be added here on purpose. `notify` is the one shape a notification may take (null: none may be sent).
const NOTIFY_BODY = /^\{ title: "CAO", body: fx\.(?:note|warning), level: "warning" \}$/;
const DIALOG_RULES = { table: WRITE_TABLE, notify: NOTIFY_BODY, expectWrites: true };
const SIDEBAR_RULES = {
  table: [
    ["SB_KEYS.rside", { call: ["rsideStep"] }],
    ["SB_KEYS.selection", /^\{ kind: "session", name \}$/],
    ["SB_KEYS.flowsStamp", /^Date\.now\(\)$/],
    ["SB_KEYS.workflowsStamp", /^Date\.now\(\)$/],
  ],
  notify: null,
  expectWrites: true,
};
const NO_WRITES = { table: [], notify: null, expectWrites: false };
const PANEL_RULES = { "dialog.js": DIALOG_RULES, "sidebar.js": SIDEBAR_RULES, "main.js": NO_WRITES, "templates.js": NO_WRITES };

// Whether `value` is one call of one of `names` and nothing more: the name, then a bracket that closes at the very end,
// so a result with something added to it (a concat, a spread, a second term) is not one.
function isOneCall(value, names) {
  const m = new RegExp(`^(?:${names.join("|")})\\(`).exec(value ?? "");
  if (!m) return false;
  const got = partsAt(value, m[0].length - 1);
  return got !== null && got.end === value.length - 1;
}
const shapeOk = (rule, value) => (rule instanceof RegExp ? rule.test(value ?? "") : isOneCall(value, rule.call) || (rule.or?.test(value ?? "") ?? false));

// The names the host module `herdr` is imported under in `keep` for `name` (`import { notify as dlNotify } from "herdr"`).
function importedAs(keep, name) {
  const out = new Set();
  for (const m of keep.matchAll(/import\s*\{([^}]*)\}\s*from\s*["']herdr["']/g)) {
    for (const part of m[1].split(",")) {
      const [from, to] = part.trim().split(/\s+as\s+/);
      if (from === name) out.add(to ?? from);
    }
  }
  return out;
}

const STORE_USES = /(?<![\w$])store\b(?!\s*\.\s*(?:get|getLocal|notice|set|setLocal)\s*\()/g;

export function storageWriteProblems(src, file = "source", rules = DIALOG_RULES) {
  const views = sourceViews(src);
  if (views === null) return [{ what: "the scanner lost its place in the source", where: file }];
  const { code, keep } = views;
  const problems = [];
  const lineAt = (at) => `${file}:${code.slice(0, at).split("\n").length}`;
  let seen = 0;

  // the store used in a way that hides a write from the table
  for (const m of code.matchAll(STORE_USES)) {
    const own = code.slice(Math.max(0, m.index - 5), m.index) === "this." && /^\s*=\s*createStore\s*\(/.test(code.slice(m.index + 5));
    if (!own) problems.push({ what: "the store is used other than through .get(, .getLocal(, .notice(, .set( and .setLocal(: a write through it would not be read", where: lineAt(m.index) });
  }
  for (const m of keep.matchAll(/\[\s*["']store["']\s*\]/g)) problems.push({ what: "the store is reached by a string key, which this check cannot read", where: lineAt(m.index) });

  // writes of storage, and the host's setShared under the name this file imports it as
  const aliases = [...importedAs(keep, "setShared")].filter((n) => n !== "setShared").map((n) => `(?<![\\w$.])${n}\\s*\\(`);
  const writers = new RegExp([STORAGE_WRITERS.source, ...aliases].join("|"), "g");
  for (const m of code.matchAll(writers)) {
    const open = m.index + m[0].length - 1;
    const got = argRanges(code, open);
    const where = lineAt(m.index);
    const name = m[0].replace(/\s*\($/, "");
    if (!got) { problems.push({ what: `${name}( is not closed`, where }); continue; }
    const args = got.ranges.map(([a, b]) => keep.slice(a, b).trim());
    seen += 1;
    if (/setItem|writeFile|appendFile/.test(name)) { problems.push({ what: `${name}( writes to a file or to local storage directly`, where }); continue; }
    const row = rules.table.find(([key]) => args[0] === key);
    if (!row) { problems.push({ what: `${name}( writes the key \`${String(args[0]).slice(0, 40)}\`, which ${file} may not write`, where }); continue; }
    if (!shapeOk(row[1], args[1])) problems.push({ what: `${name}(${args[0]}) is given \`${String(args[1] ?? "nothing").slice(0, 60)}\`, not a value of the allowed shape`, where });
  }

  // notifications: the host's notify under any name, or called on the module
  const notifiers = [...importedAs(keep, "notify")].map((n) => `(?<![\\w$.])${n}\\s*\\(`);
  const notifyCalls = new RegExp([...notifiers, "\\bherdr\\s*\\.\\s*notify\\s*\\("].join("|"), "g");
  for (const m of code.matchAll(notifyCalls)) {
    const got = argRanges(code, m.index + m[0].length - 1);
    const where = lineAt(m.index);
    if (!got) { problems.push({ what: "a notification call is not closed", where }); continue; }
    const args = got.ranges.map(([a, b]) => keep.slice(a, b).trim());
    seen += 1;
    if (rules.notify === null) problems.push({ what: `${file} may not send a notification`, where });
    else if (args.length !== 1 || !rules.notify.test(args[0])) problems.push({ what: `a notification is given \`${args.join(", ").slice(0, 70)}\`, not one of the two constant bodies`, where });
  }
  if (problems.length === 0 && seen === 0 && rules.expectWrites) return [{ what: "no write of storage was found: the scan read nothing", where: file }];
  return problems;
}

// Every .js file of the panel mod under `root` (a mods folder), each read with the rules listed for its name.
const panelStorageProblems = (root) => readdirSync(join(root, "cao-window-panel")).filter((f) => f.endsWith(".js")).sort()
  .flatMap((f) => storageWriteProblems(readFileSync(join(root, "cao-window-panel", f), "utf8"), f, PANEL_RULES[f] ?? NO_WRITES));

const dialogSource = () => readFileSync(join(MODS, "cao-window-panel", "dialog.js"), "utf8");

test("Inputs never reach storage: every write of storage by the dialog host is one the table allows", () => {
  assert.deepEqual(storageWriteProblems(dialogSource(), "dialog.js"), []);
});

test("Inputs never reach storage: the dialog writes the approvals key, only through recordApproval and clearApproval", () => {
  const code = codeOf(dialogSource());
  assert.ok([...code.matchAll(/(?<![\w$.])recordApproval\s*\(/g)].length >= 1, "a 403 approval_required records a row");
  assert.ok([...code.matchAll(/(?<![\w$.])clearApproval\s*\(/g)].length >= 1, "a good submit clears it");
  assert.deepEqual(approvalsProblems(dialogSource(), "dialog.js"), []);
});

test("Inputs never reach storage: the Run form's directory is never written to local storage, and the one local write is the New session's", () => {
  const code = codeOf(dialogSource());
  assert.equal([...code.matchAll(/\.setLocal\s*\(/g)].length, 1, "one local write: the directory a launch started in");
  assert.equal([...code.matchAll(/localStorage|setItem\s*\(|writeFile|appendFile/g)].length, 0);
});

test("Inputs never reach storage: the dialog reaches CAO only through callCao, and runs no other command", () => {
  const code = codeOf(dialogSource());
  const exec = [...code.matchAll(/(?<![\w$.])dlExec\b/g)].length;
  const calls = [...code.matchAll(/(?<![\w$.])callCao\s*\(/g)].length;
  assert.equal(exec, calls + 1, "dlExec is imported once and passed to callCao each time");
  assert.equal([...code.matchAll(/(?<![\w$.])dlHc\b|\bhc\s*\(|\bdlRun\b/g)].length, 0, "no host command");
});

test("Launch with no answer keeps the record: the author records are written only by the pure functions, and the launch is recorded before it is sent", () => {
  const src = dialogSource();
  const code = codeOf(src);
  for (const fn of ["recordAuthor", "afterLaunch", "afterDelete", "recordDraft"]) assert.ok([...code.matchAll(new RegExp(`(?<![\\w$.])${fn}\\s*\\(`, "g"))].length >= 1, `${fn} is called`);
  assert.deepEqual(storageWriteProblems(src, "dialog.js"), []);
  // The record exists before the author's POST can reach the server: recordAuthor( is textually and causally first in the launch.
  const launch = /function dlDraftLaunch\s*\([^)]*\)\s*\{/.exec(code);
  assert.ok(launch !== null, "dlDraftLaunch exists");
  const body = code.slice(launch.index);
  assert.ok(body.search(/recordAuthor\s*\(/) >= 0 && body.search(/recordAuthor\s*\(/) < body.search(/callCao\s*\(/), "recordAuthor( precedes the request");
});

test("the storage-write scan flags a value that carries an input, a directory, a message or a record, and a key outside the table", () => {
  const head = 'const DL_APPROVALS_KEY = "caowin.approvals";\nconst DL_AUTHORS_KEY = "caowin.authors";\nfunction f(panel, rf, outcome, record, authors) {\n';
  const fine = [
    "panel.store.set(DL_APPROVALS_KEY, recordApproval(rows, name, outcome, Date.now()));",
    "panel.store.set(DL_APPROVALS_KEY, clearApproval(rows, name));",
    "panel.store.set(DL_AUTHORS_KEY, recordAuthor(authors, name, panel.dl.name, Date.now()));",
    "panel.store.set(DL_AUTHORS_KEY, next.authors);",
    "panel.store.set(DL_LEASE_KEY, null); panel.dl.profileCache.set(a, b); ns.env.set(1, 2);",
  ];
  for (const src of fine) assert.deepEqual(storageWriteProblems(`${head}${src}\n}\n`), [], src);
  const bad = [
    "panel.store.set(DL_APPROVALS_KEY, rf.record);",
    "panel.store.set(DL_APPROVALS_KEY, [...rows, { workflow: name, inputs: rf.inputs }]);",
    "panel.store.set(DL_APPROVALS_KEY, recordApproval(rows, name, outcome, Date.now()).concat(record));",
    "panel.store.setLocal(\"caowin.lastRun\", rf.workdir);",
    "panel.store.setLocal(\"caowin.lastWorkdir\", rf.workdir);",
    "panel.store.set(\"caowin.runInputs\", rf.inputs);",
    "panel.store.set(DL_AUTHORS_KEY, [...authors, { name, inputs: rf.inputs }]);",
    "panel.store.set(DL_AUTHORS_KEY, outcome.message);",
    "setShared(\"caowin.approvals\", { workflow: name, planId: outcome.planId, message: outcome.message });",
    "localStorage.setItem(\"x\", JSON.stringify(rf));",
    "writeFileSync(\"/tmp/x\", rf.workdir);",
  ];
  for (const src of bad) assert.ok(storageWriteProblems(`${head}${src}\n}\n`).length > 0, `not flagged: ${src}`);
  assert.ok(storageWriteProblems("const x = 1;").length > 0, "a source with no write at all is not a pass");
});

// ---- every file of the panel mod, and the sinks beside store.set (F2 of the review of sections 20 and 21) ----

test("Inputs never reach storage: every file of the panel mod is read, not only dialog.js, and each writes and says only what its rules allow", () => {
  assert.deepEqual(panelStorageProblems(MODS), []);
  const files = readdirSync(join(MODS, "cao-window-panel")).filter((f) => f.endsWith(".js"));
  for (const f of files) assert.ok(Object.hasOwn(PANEL_RULES, f), `${f} has no entry in PANEL_RULES, so it would be read with no writes allowed`);
  for (const f of ["dialog.js", "sidebar.js"]) {
    const src = readFileSync(join(MODS, "cao-window-panel", f), "utf8");
    assert.ok(storageWriteProblems(src, f, NO_WRITES).length > 0, `${f} holds writes: read with no rules, the scan must object`);
  }
});

test("the storage-write scan flags a notification that carries a form, an aliased store and a destructured one, in any file of the panel", () => {
  const head = 'import { notify as dlNotify, setShared as dlSet } from "herdr";\nconst DL_AUTHORS_KEY = "caowin.authors";\nfunction f(panel, nw, nf, rf, fx, record) {\n';
  const fine = [
    'dlNotify({ title: "CAO", body: fx.note, level: "warning" });',
    'dlNotify({ title: "CAO", body: fx.warning, level: "warning" });',
    "cx.notify(); cx2.notify(); panel.store.notice();",
    "const x = panel.store.get(DL_AUTHORS_KEY); panel.store.set(DL_AUTHORS_KEY, next.authors);",
    "this.store = createStore({ getShared: shared, setShared, local: localStorage });",
    '// const st = panel.store; dlNotify({ title: "CAO", body: nw.form.source, level: "info" });',
    'const s = "dlNotify({ body: rf.inputs }); const st = panel.store";',
  ];
  for (const src of fine) assert.deepEqual(storageWriteProblems(`${head}${src}\n}\n`).filter((p) => !/no write of storage/.test(p.what)), [], src);
  const bad = [
    'dlNotify({ title: "CAO", body: nw.form.source, level: "info" });',
    'dlNotify({ title: "CAO", body: rf.record.message, level: "warning" });',
    'dlNotify({ title: "CAO", body: fx.note, level: "warning" }, rf);',
    "dlNotify(nf.banner);",
    'herdr.notify({ title: "CAO", body: nf.form.prompt, level: "info" });',
    'const st = panel.store; st.set("caowin.lastRun", rf.record);',
    "const { set } = panel.store; set(DL_AUTHORS_KEY, rf.record);",
    "const put = panel.store.set; put(DL_AUTHORS_KEY, rf.record);",
    'panel.store["set"](DL_AUTHORS_KEY, rf.record);',
    "keep(panel.store, rf.record);",
    "function g(store) { store.set(DL_AUTHORS_KEY, rf.record); }",
    "dlSet(DL_AUTHORS_KEY, rf.record);",
    'dlSet("caowin.lastRun", rf.inputs);',
  ];
  for (const src of bad) assert.ok(storageWriteProblems(`${head}${src}\n}\n`).length > 0, `not flagged: ${src}`);
  const noRules = (src) => storageWriteProblems(src, "extra.js", NO_WRITES);
  assert.ok(noRules('import { notify as ping } from "herdr";\nping({ title: "CAO", body: fx.note, level: "warning" });').length > 0, "a file with no notification rule may not send one, even the allowed text");
  assert.ok(noRules('panel.store.set("caowin.selection", null);').length > 0, "a file with no rules may write nothing");
  assert.deepEqual(noRules("const answer = panel.store.get(\"caowin.cao\"); cx.notify();"), [], "reads and a repaint are fine in any file");
});

test("a planted write in a second file of the panel mod, through an alias or a notification, is flagged in that file", () => {
  assert.deepEqual(panelStorageProblems(MODS), [], "the real panel files are clean first, so the planted file is the only difference");
  const plants = {
    "leak.js": 'import { setShared as lkSet } from "herdr";\nexport function leak(panel, rf) { const st = panel.store; st.set("caowin.lastRun", rf.record); lkSet("caowin.x", rf.inputs); }\n',
    "toast.js": 'import { notify as lkNotify } from "herdr";\nexport function say(nw) { lkNotify({ title: "CAO", body: nw.form.source, level: "info" }); }\n',
  };
  for (const [name, text] of Object.entries(plants)) {
    const root = plantedCopy(join("cao-window-panel", name), text);
    const found = panelStorageProblems(root);
    assert.ok(found.length >= 1, `${name}: the planted write was not seen`);
    assert.ok(found.every((p) => p.where.startsWith(`${name}:`)), `${name}: ${JSON.stringify(found)}`);
  }
  const sidebarPlant = join(MODS, "cao-window-panel", "sidebar.js");
  const root = plantedCopy(join("cao-window-panel", "sidebar.js"), readFileSync(sidebarPlant, "utf8").replace("panel.store.set(SB_KEYS.selection, { kind: \"session\", name });", "panel.store.set(SB_KEYS.selection, panel.sb.confirm);"));
  const found = panelStorageProblems(root);
  assert.equal(found.length, 1, JSON.stringify(found));
  assert.ok(found[0].where.startsWith("sidebar.js:") && /not a value of the allowed shape/.test(found[0].what), "a changed value in sidebar.js is flagged there");
});

await finish();
