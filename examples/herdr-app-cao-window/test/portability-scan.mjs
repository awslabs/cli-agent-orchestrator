// Rules and scanner for portability.test.mjs. Generic rules only: this file holds no list of one
// person's tools, hosts or services, not even as fragments (D18). The operator's private list is read
// from CAO_PORTABILITY_EXTRA_PATTERNS / CAO_PORTABILITY_EXTRA_PATTERNS_FILE at run time. Hits are
// reported as file, line and rule name, never as the matched text.
import fs from "node:fs";
import path from "node:path";

const j = (...p) => p.join("");
const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const lit = (name, text) => ({ name, re: new RegExp(esc(text), "i") });
const NAME = "[A-Za-z0-9._-]+";                      // a real user name; a placeholder like <user> does not match

// Shapes of one machine's setup. Skipped in --root mode (redacted evidence holds placeholders of this shape).
export const GENERIC_RULES = [
  { name: "macOS home path", re: new RegExp(esc(j("/Us", "ers/")) + NAME + "/", "i") },
  { name: "Linux home path", re: new RegExp(esc(j("/ho", "me/")) + NAME, "i") },
  lit("tunnel tool", j("auto", "ssh")),
  lit("tunnel option", j("ssh ", "-L")),
  lit("local tunnel port", j("local", "host:9889")),
];

export const GENERIC_LOGINS = new Set(["runner", "root", "ubuntu", "user", "node"]);
export const NOTICE = "private names are not checked: set CAO_PORTABILITY_EXTRA_PATTERNS or CAO_PORTABILITY_EXTRA_PATTERNS_FILE";

export function loginRule(login) {
  if (typeof login !== "string" || login.length < 4 || GENERIC_LOGINS.has(login.toLowerCase())) return null;
  return { name: "the running user's login", re: new RegExp(`(?<![A-Za-z0-9])${esc(login)}(?![A-Za-z0-9])`, "i") };
}

// The operator's private list: comma-separated in the variable, one per line in the file. Blank
// entries and whole-line `#` comments are dropped; a `#` inside a pattern is kept. Patterns are
// literal text, matched whatever the case.
export function extraRules(env) {
  const names = String(env?.CAO_PORTABILITY_EXTRA_PATTERNS ?? "").split(",");
  const file = env?.CAO_PORTABILITY_EXTRA_PATTERNS_FILE;
  if (file) {
    let text;
    try { text = fs.readFileSync(file, "utf8"); }
    catch { throw new Error("CAO_PORTABILITY_EXTRA_PATTERNS_FILE cannot be read"); }
    names.push(...text.split(/\r?\n/).filter((l) => !l.trim().startsWith("#")));
  }
  return names.map((s) => s.trim()).filter(Boolean).map((p) => lit("pattern from the operator's list", p));
}

export const rootRules = (env, login) => [...extraRules(env), ...[loginRule(login)].filter(Boolean)];

// "" when the operator's list is in use, else the one-line notice.
export const notice = (env) => (extraRules(env).length ? "" : NOTICE);

// `--root` with no folder after it must fail, not scan the current directory. The test's own argument
// handling resolves a missing value to "" (the current directory), so the check lives here and runs when
// this module is imported by the command line.
function rejectBareRootFlag(argv) {
  const at = argv.indexOf("--root");
  if (at < 0) return;
  const value = argv[at + 1];
  if (value === undefined || value === "" || value.startsWith("--")) {
    console.error("portability: --root needs a folder to scan");
    process.exit(2);
  }
}
rejectBareRootFlag(process.argv.slice(2));

const SKIP_DIRS = new Set([".git", "node_modules"]);
const MAX_BYTES = 2 * 1024 * 1024;

function* walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (SKIP_DIRS.has(entry.name)) continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(full);
    else if (entry.isFile()) yield full;
  }
}

// [{file, line, rule}] for every line of every text file under root that matches a rule. A root that
// is not a folder throws: a scan of nothing must not read as "no match".
export function scan(root, rules) {
  if (!fs.statSync(root, { throwIfNoEntry: false })?.isDirectory()) throw new Error(`portability: ${root} is not a folder`);
  const hits = [];
  for (const file of walk(root)) {
    const data = fs.readFileSync(file);
    if (data.length > MAX_BYTES || data.includes(0)) continue;
    data.toString("utf8").split("\n").forEach((text, i) => {
      for (const rule of rules) if (rule.re.test(text)) hits.push({ file: path.relative(root, file), line: i + 1, rule: rule.name });
    });
  }
  return hits;
}

const STUB = j("<!-- README-", "STUB");
const REQUIRED = [
  ["Prerequisites", /^prerequisites$/i],
  ["a topology heading for one machine", /one machine|same machine/i],
  ["a topology heading for a remote CAO host", /remote/i],
  ["a topology heading for Linux", /linux/i],
  ["Install", /^install$/i],
  ["Upgrade", /^upgrade$/i],
  ["Uninstall", /^uninstall$/i],
  ["Troubleshooting", /^troubleshooting$/i],
  ["What this does not do", /what this does not do/i],
];

// While the README is the task-4.1 stub the check is deferred; 15.1 removes the marker.
export function readmeProblems(text) {
  if (text.startsWith(STUB)) return { deferred: true, problems: [] };
  const problems = [];
  const lines = text.split("\n");
  const headings = lines.map((l, i) => ({ i, m: /^#{1,6}\s+(.*?)\s*$/.exec(l) })).filter((h) => h.m);
  for (const [label, re] of REQUIRED) {
    if (!headings.some((h) => re.test(h.m[1]))) problems.push(`README has no heading for ${label}`);
  }
  const pre = headings.find((h) => /^prerequisites$/i.test(h.m[1]));
  for (const word of ["TOAST_MOD", "MOD_IMPORTS"]) {
    if (!text.includes(word)) problems.push(`README does not document ${word}`);
  }
  if (pre) {
    const next = headings.find((h) => h.i > pre.i);
    const section = lines.slice(pre.i, next ? next.i : lines.length).join("\n");
    if (!/\bjq\b/.test(section)) problems.push("README Prerequisites does not name jq");
  }
  return { deferred: false, problems };
}
