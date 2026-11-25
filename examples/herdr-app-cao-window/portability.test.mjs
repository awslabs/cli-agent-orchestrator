// Portability: nothing shipped may name one person's setup or an internal system, and CAO data goes through
// exec on the CAO machine.
// Run: node examples/herdr-app-cao-window/portability.test.mjs
//   node portability.test.mjs --root <dir>   scans <dir> with the login and the operator's list.
// Private names are the operator's list (CAO_PORTABILITY_EXTRA_PATTERNS / _FILE); none are written here.
// Shapes that the scanner looks for are built from fragments (j) so this file does not trip its own scan.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { assert, finish, mkTmp, test } from "./test/harness.mjs";
import {
  GENERIC_RULES, NOTICE, extraRules, loginRule, notice, readmeProblems, rootRules, scan,
} from "./test/portability-scan.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const argv = process.argv.slice(2);
const LOGIN_NOW = os.userInfo().username;

if (argv.includes("--root")) {
  const root = path.resolve(argv[argv.indexOf("--root") + 1] ?? "");
  const hits = scan(root, rootRules(process.env, LOGIN_NOW));
  for (const h of hits) console.log(`${h.file}:${h.line}: ${h.rule}`);
  const n = notice(process.env);
  if (n) console.log(n);
  console.log(hits.length ? `${hits.length} match(es) in ${root}` : `no match in ${root}`);
  process.exit(hits.length ? 1 : 0);
}

const FULL = [...GENERIC_RULES, ...extraRules(process.env), ...[loginRule(LOGIN_NOW)].filter(Boolean)];
const j = (...p) => p.join("");
const HOME_SHAPE = j("/ho", "me/");
const MAC_SHAPE = j("/Us", "ers/");

function tempCopy() {
  const dir = mkTmp("cao-window-portability-");
  fs.cpSync(HERE, dir, { recursive: true, filter: (src) => !src.includes("node_modules") });
  return dir;
}
const plant = (dir, text, rel = path.join("lib", "planted.js")) => {
  fs.mkdirSync(path.dirname(path.join(dir, rel)), { recursive: true });
  fs.writeFileSync(path.join(dir, rel), `// x\n${text}\n`);
};
const where = (hits) => hits.map((h) => `${h.file}:${h.line}`);

// Runs a copy of this test file the way an operator or CI does, with a chosen environment.
function runCli(dir, { env = {}, args = [], dropList = true } = {}) {
  const base = { ...process.env, CAO_PORTABILITY_CHILD: "1" };
  if (dropList) { delete base.CAO_PORTABILITY_EXTRA_PATTERNS; delete base.CAO_PORTABILITY_EXTRA_PATTERNS_FILE; }
  const r = spawnSync(process.execPath, [path.join(dir, "portability.test.mjs"), ...args], { env: { ...base, ...env }, encoding: "utf8" });
  return { status: r.status, out: r.stdout + r.stderr };
}

console.log(notice(process.env) || "private names: checked with the operator's list");

test("the shipped tree is clean", () => {
  const hits = scan(HERE, FULL);
  assert.deepEqual(hits.map((h) => `${h.file}:${h.line} ${h.rule}`), []);
});

test("a leaked home path fails and names the file and line, for both path shapes", () => {
  for (const text of [j(MAC_SHAPE, "someone/x"), j(HOME_SHAPE, "someone/x")]) {
    const dir = tempCopy();
    plant(dir, text);
    assert.deepEqual(where(scan(dir, FULL)), [`${path.join("lib", "planted.js")}:2`]);
  }
});

test("a placeholder user in a path shape is not a leak", () => {
  const dir = tempCopy();
  plant(dir, j(HOME_SHAPE, "<user>/x and ", MAC_SHAPE, "<user>/y"));
  assert.deepEqual(scan(dir, FULL), []);
});

test("path shapes match whatever the case, and in any file type", () => {
  const dir = tempCopy();
  plant(dir, j(MAC_SHAPE.toUpperCase(), "SOMEONE/x"), "README.md");
  plant(dir, j(HOME_SHAPE.toUpperCase(), "someone/x"), path.join("bin", "launcher.in"));
  plant(dir, j("x ", HOME_SHAPE, "someone/x"), path.join("mods", "cao-window-side", "main.js"));
  plant(dir, j(MAC_SHAPE, "someone/x"), path.join("layouts", "planted.json"));
  assert.deepEqual(where(scan(dir, FULL)).sort(), [
    path.join("README.md") + ":2", path.join("bin", "launcher.in") + ":2",
    path.join("layouts", "planted.json") + ":2", path.join("mods", "cao-window-side", "main.js") + ":2",
  ].sort());
});

test("every matching line of a file is reported, with its own line number", () => {
  const dir = tempCopy();
  fs.writeFileSync(path.join(dir, "lib", "planted.js"), j("ok\n", MAC_SHAPE, "a/x\nfine\n", HOME_SHAPE, "b/y\n"));
  assert.deepEqual(where(scan(dir, FULL)), [`${path.join("lib", "planted.js")}:2`, `${path.join("lib", "planted.js")}:4`]);
});

test("each tunnel shape fails", () => {
  for (const text of [j("auto", "ssh -f"), j("AUTO", "SSH"), j("ssh ", "-L 1:2:3"), j("local", "host:9889"), j("LOCAL", "HOST:9889")]) {
    const dir = tempCopy();
    plant(dir, text);
    assert.equal(scan(dir, FULL).length, 1, `not caught: ${text.length} chars`);
  }
});

test("near misses of the tunnel shapes are not flagged", () => {
  const dir = tempCopy();
  plant(dir, j("local", "host:9890 and 127.0.0.1:9889 and ssh -p 22"));
  assert.deepEqual(scan(dir, FULL), []);
});

test("the running user's login counts when it is long and not generic", () => {
  const LOGIN = j("jdoe", "42");                          // a made-up login, built from parts
  assert.equal(loginRule(LOGIN).re.test(`path ${LOGIN} here`), true);
  assert.equal(loginRule(LOGIN).re.test(`${LOGIN}1`), false);   // inside a longer word
  assert.equal(loginRule(LOGIN).re.test(`x${LOGIN}`), false);
  assert.equal(loginRule(LOGIN).re.test(`/${LOGIN}/`), true);
  assert.equal(loginRule(LOGIN).re.test(LOGIN.toUpperCase()), true);   // case-insensitive
  for (const login of ["root", "runner", "ubuntu", "user", "node", "abc", "", undefined, null, 42]) {
    assert.equal(loginRule(login), null, String(login));
  }
  const dir = tempCopy();
  plant(dir, `owner is ${LOGIN}`);
  assert.equal(scan(dir, [loginRule(LOGIN)]).length, 1);
});

test("the login length boundary is four characters, and generic names are generic in any case", () => {
  assert.equal(loginRule("abc"), null);
  assert.notEqual(loginRule("abcd"), null);
  for (const login of ["ROOT", "Runner", "UBUNTU", "User", "NODE"]) assert.equal(loginRule(login), null, login);
});

test("a login with regex characters is matched literally", () => {
  const rule = loginRule(j("j.do", "e+1"));
  assert.equal(rule.re.test(j("j.do", "e+1")), true);
  assert.equal(rule.re.test(j("jxdo", "ee1")), false);
});

test("a pattern from CAO_PORTABILITY_EXTRA_PATTERNS fails, without printing the text", () => {
  const HOST = j("build-host", "-7");                      // a made-up host name
  const dir = tempCopy();
  plant(dir, `see ${HOST} for details`);
  const rules = extraRules({ CAO_PORTABILITY_EXTRA_PATTERNS: `${j("other", "-host")}, ${HOST} ` });
  const hits = scan(dir, rules);
  assert.equal(hits.length, 1);
  assert.equal(JSON.stringify(hits).includes(HOST), false);
  assert.deepEqual(extraRules({}), []);
});

test("operator patterns match whatever the case and are literal, not regular expressions", () => {
  const dir = mkTmp("cao-window-literal-");                 // a clean folder: this file itself holds some of these strings
  fs.writeFileSync(path.join(dir, "note.txt"), j("See BUILD-host", "-7 and a.b(c)\n"));
  assert.equal(scan(dir, extraRules({ CAO_PORTABILITY_EXTRA_PATTERNS: j("build-HOST", "-7") })).length, 1);
  assert.equal(scan(dir, extraRules({ CAO_PORTABILITY_EXTRA_PATTERNS: "a.b(c)" })).length, 1);
  assert.equal(scan(dir, extraRules({ CAO_PORTABILITY_EXTRA_PATTERNS: "a.b(x)" })).length, 0);
  assert.equal(scan(dir, extraRules({ CAO_PORTABILITY_EXTRA_PATTERNS: ".*" })).length, 0);   // would match every line if read as a regex
});

test("blank, comma-only and whitespace-only patterns match nothing and count as no list", () => {
  for (const raw of ["", " ", ",", " , ,, ", "\t"]) {
    assert.deepEqual(extraRules({ CAO_PORTABILITY_EXTRA_PATTERNS: raw }), [], JSON.stringify(raw));
    assert.equal(notice({ CAO_PORTABILITY_EXTRA_PATTERNS: raw }), NOTICE, JSON.stringify(raw));
  }
});

test("a pattern from CAO_PORTABILITY_EXTRA_PATTERNS_FILE fails; blank lines and comments are ignored", () => {
  const HOST = j("build-host", "-8");
  const list = path.join(mkTmp("cao-window-list-"), "private.txt");
  fs.writeFileSync(list, `# the operator's list\n\n${HOST}\n`);
  const dir = tempCopy();
  plant(dir, `see ${HOST}`);
  const rules = extraRules({ CAO_PORTABILITY_EXTRA_PATTERNS_FILE: list });
  assert.equal(rules.length, 1);
  assert.equal(scan(dir, rules).length, 1);
  assert.throws(() => extraRules({ CAO_PORTABILITY_EXTRA_PATTERNS_FILE: path.join(path.dirname(list), "missing.txt") }), /CAO_PORTABILITY_EXTRA_PATTERNS_FILE/);
});

test("a patterns file tolerates CRLF line endings, indented comments and a pattern with a hash inside", () => {
  const A = j("host", "-a"), B = j("host", "#b");
  const list = path.join(mkTmp("cao-window-list-"), "private.txt");
  fs.writeFileSync(list, `   # indented comment\r\n${A}\r\n\r\n  ${B}  \r\n`);
  const rules = extraRules({ CAO_PORTABILITY_EXTRA_PATTERNS_FILE: list });
  const dir = tempCopy();
  fs.writeFileSync(path.join(dir, "lib", "planted.js"), `${A}\n${B}\nclean\n`);
  assert.deepEqual(scan(dir, rules).map((h) => h.line), [1, 2]);
});

test("the variable and the file are both used when both are set", () => {
  const list = path.join(mkTmp("cao-window-list-"), "private.txt");
  fs.writeFileSync(list, "from-file\n");
  assert.equal(extraRules({ CAO_PORTABILITY_EXTRA_PATTERNS: "from-var", CAO_PORTABILITY_EXTRA_PATTERNS_FILE: list }).length, 2);
});

test("a patterns file that is empty or only comments means no list", () => {
  const list = path.join(mkTmp("cao-window-list-"), "private.txt");
  fs.writeFileSync(list, "# nothing yet\n\n");
  const env = { CAO_PORTABILITY_EXTRA_PATTERNS_FILE: list };
  assert.deepEqual(extraRules(env), []);
  assert.equal(notice(env), NOTICE);
});

test("with no extra patterns the notice says the operator's list is missing", () => {
  assert.equal(notice({}), NOTICE);
  assert.equal(notice({ CAO_PORTABILITY_EXTRA_PATTERNS: "x" }), "");
  assert.match(NOTICE, /private names are not checked/);
  assert.match(NOTICE, /CAO_PORTABILITY_EXTRA_PATTERNS/);
  assert.match(NOTICE, /CAO_PORTABILITY_EXTRA_PATTERNS_FILE/);
});

test("run with no list configured, the CLI exits 0 on the clean tree and prints the notice", () => {
  if (process.env.CAO_PORTABILITY_CHILD) return;           // the child must not recurse
  const r = runCli(HERE);
  assert.equal(r.status, 0, r.out);
  assert.ok(r.out.includes(NOTICE), r.out);
});

test("the CLI fails on each generic shape, names file and line, and never prints the matched text", () => {
  if (process.env.CAO_PORTABILITY_CHILD) return;
  const cases = [
    [j(MAC_SHAPE, "zq9someone/x"), "zq9someone"], [j(HOME_SHAPE, "zq9someone/x"), "zq9someone"],
    [j("auto", "ssh zq9tail"), "zq9tail"], [j("ssh ", "-L 1:2:3 zq9fwd"), "zq9fwd"], [j("local", "host:9889 zq9port"), "zq9port"],
  ];
  for (const [text, secret] of cases) {
    const dir = tempCopy();
    plant(dir, text);
    const r = runCli(dir);
    assert.notEqual(r.status, 0, text);
    assert.ok(r.out.includes(path.join("lib", "planted.js")) && r.out.includes(":2"), `no file:line for ${secret}\n${r.out}`);
    assert.ok(!r.out.includes(secret), `the matched text was printed for ${secret}`);
  }
});

test("the CLI fails on a pattern from the operator's variable and file, and says so when a list is in use", () => {
  if (process.env.CAO_PORTABILITY_CHILD) return;
  const HOST = j("zq9-host", "-7");
  const dir = tempCopy();
  plant(dir, `see ${HOST}`);
  const viaVar = runCli(dir, { env: { CAO_PORTABILITY_EXTRA_PATTERNS: HOST } });
  assert.notEqual(viaVar.status, 0);
  assert.ok(viaVar.out.includes(path.join("lib", "planted.js")));
  assert.ok(!viaVar.out.includes(HOST));
  assert.ok(!viaVar.out.includes(NOTICE));
  const list = path.join(mkTmp("cao-window-list-"), "private.txt");
  fs.writeFileSync(list, `${HOST}\n`);
  const viaFile = runCli(dir, { env: { CAO_PORTABILITY_EXTRA_PATTERNS_FILE: list } });
  assert.notEqual(viaFile.status, 0);
  assert.ok(!viaFile.out.includes(HOST));
  const clean = runCli(HERE, { env: { CAO_PORTABILITY_EXTRA_PATTERNS: HOST } });
  assert.equal(clean.status, 0, clean.out);
  assert.ok(!clean.out.includes(NOTICE));
});

test("the CLI fails when the running user's own login appears, and does not print it", () => {
  if (process.env.CAO_PORTABILITY_CHILD) return;
  const rule = loginRule(LOGIN_NOW);
  if (!rule) { console.log("note: this account's login is generic or short, so the running-login case is not exercised here"); return; }
  const dir = tempCopy();
  plant(dir, `owner ${LOGIN_NOW}`);
  const r = runCli(dir);
  assert.notEqual(r.status, 0);
  assert.ok(r.out.includes(path.join("lib", "planted.js")));
  // The report names the file under the scratch folder, and $TMPDIR may sit under the login's own home. The check is that the
  // matched text is not printed, so the folder's path is taken out of the output first (and no temp path is asserted on).
  assert.ok(!r.out.split(dir).join("").includes(LOGIN_NOW));
});

test("a patterns file that cannot be read makes the CLI fail and names the variable", () => {
  if (process.env.CAO_PORTABILITY_CHILD) return;
  const missing = path.join(mkTmp("cao-window-list-"), "missing.txt");
  const r = runCli(HERE, { env: { CAO_PORTABILITY_EXTRA_PATTERNS_FILE: missing } });
  assert.notEqual(r.status, 0);
  assert.match(r.out, /CAO_PORTABILITY_EXTRA_PATTERNS_FILE/);
});

test("--root mode applies the login and the list but skips path shapes", () => {
  const LOGIN = j("jdoe", "42");
  const dir = mkTmp("cao-window-root-");
  fs.writeFileSync(path.join(dir, "note.md"), j(HOME_SHAPE, "<user>/x\n", MAC_SHAPE, "example/y\n"));
  assert.deepEqual(scan(dir, rootRules({}, LOGIN)), []);
  assert.equal(scan(dir, GENERIC_RULES).length, 1);          // the second line is a shape; the first has a placeholder
  fs.appendFileSync(path.join(dir, "note.md"), `owner ${LOGIN}\n`);
  assert.equal(scan(dir, rootRules({}, LOGIN)).length, 1);
  fs.appendFileSync(path.join(dir, "note.md"), "zq9listed\n");
  assert.equal(scan(dir, rootRules({ CAO_PORTABILITY_EXTRA_PATTERNS: "zq9listed" }, LOGIN)).length, 2);   // the list is applied too
});

test("--root over a folder holding only a home-path shape reports no match and exits 0", () => {
  if (process.env.CAO_PORTABILITY_CHILD) return;
  const dir = mkTmp("cao-window-root-");
  fs.writeFileSync(path.join(dir, "x.md"), j(HOME_SHAPE, "<user>/x\n"));
  const r = runCli(HERE, { args: ["--root", dir] });
  assert.equal(r.status, 0, r.out);
  assert.match(r.out, /^no match/m);
  assert.ok(r.out.includes(NOTICE), r.out);
});

test("--root reports a private-list match by file and line, exits 1, and never prints the text", () => {
  if (process.env.CAO_PORTABILITY_CHILD) return;
  const HOST = j("zq9-host", "-9");
  const dir = mkTmp("cao-window-root-");
  fs.mkdirSync(path.join(dir, "references"));
  fs.writeFileSync(path.join(dir, "references", "evidence.md"), `line one\nmentions ${HOST}\n`);
  const r = runCli(HERE, { args: ["--root", dir], env: { CAO_PORTABILITY_EXTRA_PATTERNS: HOST } });
  assert.equal(r.status, 1, r.out);
  assert.ok(r.out.includes(`${path.join("references", "evidence.md")}:2`), r.out);
  assert.ok(!r.out.includes(HOST));
});

test("--root does not run the README heading check", () => {
  if (process.env.CAO_PORTABILITY_CHILD) return;
  const dir = mkTmp("cao-window-root-");
  fs.writeFileSync(path.join(dir, "README.md"), "# Not a complete README\n");
  assert.equal(runCli(HERE, { args: ["--root", dir] }).status, 0);
});

test("--root with a folder that does not exist, or with no folder, fails instead of reporting no match", () => {
  if (process.env.CAO_PORTABILITY_CHILD) return;
  const gone = path.join(mkTmp("cao-window-root-"), "absent");
  const missingDir = runCli(HERE, { args: ["--root", gone] });
  assert.notEqual(missingDir.status, 0);
  assert.doesNotMatch(missingDir.out, /^no match/m);
  const noValue = runCli(HERE, { args: ["--root"] });
  assert.notEqual(noValue.status, 0, "--root without a folder must not scan the current directory");
});

const GOOD_README = `# Title
## Prerequisites
You need \`jq\`.
## Topology: one machine
## Topology: remote CAO host
## Topology: Herdr app on Linux
## Install
## Upgrade
## Uninstall
## Troubleshooting
## What this does not do
Switches: TOAST_MOD and MOD_IMPORTS.
`;

test("the README heading check passes a complete README", () => {
  assert.deepEqual(readmeProblems(GOOD_README), { deferred: false, problems: [] });
});

test("the README heading check names what is missing", () => {
  const r = readmeProblems(GOOD_README.replace("## Upgrade\n", "").replace("`jq`", "tools"));
  assert.equal(r.deferred, false);
  assert.ok(r.problems.some((p) => /Upgrade/.test(p)));
  assert.ok(r.problems.some((p) => /jq/.test(p)));
});

for (const heading of ["## Prerequisites\n", "## Topology: one machine\n", "## Topology: remote CAO host\n",
  "## Topology: Herdr app on Linux\n", "## Install\n", "## Upgrade\n", "## Uninstall\n", "## Troubleshooting\n",
  "## What this does not do\n"]) {
  test(`the README check fails when the heading ${JSON.stringify(heading.trim())} is missing`, () => {
    const r = readmeProblems(GOOD_README.replace(heading, ""));
    assert.equal(r.deferred, false);
    assert.ok(r.problems.length >= 1, "a missing heading went unnoticed");
  });
}

test("the README check names a missing installer switch", () => {
  const r = readmeProblems(GOOD_README.replace("MOD_IMPORTS", "x"));
  assert.deepEqual(r.problems, ["README does not document MOD_IMPORTS"]);
  assert.deepEqual(readmeProblems(GOOD_README.replace("TOAST_MOD", "x")).problems, ["README does not document TOAST_MOD"]);
});

test("jq counts only when the Prerequisites section itself names it", () => {
  const elsewhere = GOOD_README.replace("You need `jq`.\n", "Nothing yet.\n").replace("## Install\n", "## Install\nRun jq first.\n");
  assert.ok(readmeProblems(elsewhere).problems.some((p) => /jq/.test(p)));
});

test("headings may sit at any level", () => {
  assert.deepEqual(readmeProblems(GOOD_README.replaceAll("## ", "### ")), { deferred: false, problems: [] });
});

test("a README stub defers the heading check", () => {
  const r = readmeProblems(j("<!-- README-", "STUB: replaced by the real README -->\n# x\n"));
  assert.deepEqual(r, { deferred: true, problems: [] });
});

test("the shipped README passes its heading check (or is the stub)", () => {
  const readme = path.join(HERE, "README.md");
  assert.ok(fs.existsSync(readme), "README.md is missing");
  const r = readmeProblems(fs.readFileSync(readme, "utf8"));
  if (r.deferred) console.log("README is a stub; heading check deferred");
  assert.deepEqual(r.problems, []);
});

// The stub marker is gone, so the heading check above is no longer deferred. The marker is
// built from fragments so this file does not hold it.
test("no file under the example holds the README stub marker, and the README heading check runs", () => {
  const marker = j("README-", "STUB");
  const holders = [];
  const walkFiles = (dir) => fs.readdirSync(dir, { withFileTypes: true }).forEach((e) => {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) { if (e.name !== "node_modules" && e.name !== ".git") walkFiles(p); }
    else if (fs.readFileSync(p, "utf8").includes(marker)) holders.push(path.relative(HERE, p));
  });
  walkFiles(HERE);
  assert.deepEqual(holders, []);
  const r = readmeProblems(fs.readFileSync(path.join(HERE, "README.md"), "utf8"));
  assert.equal(r.deferred, false);
  assert.deepEqual(r.problems, []);
});

// ---- the data path: CAO data goes through exec on the CAO machine ----

function sourceFiles(root) {
  const out = [];
  for (const dir of ["lib", "mods"]) {
    const start = path.join(root, dir);
    if (!fs.existsSync(start)) continue;
    const walkDir = (d) => fs.readdirSync(d, { withFileTypes: true }).forEach((e) => {
      const p = path.join(d, e.name);
      if (e.isDirectory()) walkDir(p);
      else if (e.name.endsWith(".js")) out.push(p);
    });
    walkDir(start);
  }
  return out;
}

// [message] for every lib/mods source that calls fetch, names a URL whose parsed host is not the CAO
// machine's loopback, or (outside lib/cao.js) names curl or builds a curl argument list in another way.
const CURL_SHAPES = [/%\{http_code\}/, /["'`]-w["'`]/, /["'`]--max-time["'`]/, /["'`]--data-binary["'`]/];
const isCommentLine = (line) => /^\s*(\/\/|\/\*|\*)/.test(line);
function loopbackProblem(url) {
  const probe = url.replace(/\$\{[^}]*\}/g, "1");           // a template placeholder stands for digits or a path
  let parsed;
  try { parsed = new URL(probe); } catch { return "is not a URL that can be read"; }
  if (parsed.protocol !== "http:") return `uses ${parsed.protocol}`;
  if (parsed.hostname !== "127.0.0.1") return `goes to host ${parsed.hostname}`;
  if (parsed.username || parsed.password) return "carries credentials";
  return null;
}
function dataPathProblems(root) {
  const problems = [];
  for (const file of sourceFiles(root)) {
    const rel = path.relative(root, file);
    const text = fs.readFileSync(file, "utf8");
    if (/\bfetch\s*\(/.test(text)) problems.push(`${rel} calls fetch`);
    for (const m of text.matchAll(/https?:\/\/[^\s"'`)]*/g)) {
      const why = loopbackProblem(m[0]);
      if (why) problems.push(`${rel} names ${m[0].slice(0, 40)}: ${why}`);
    }
    if (rel === path.join("lib", "cao.js")) continue;
    if (/["'`]curl["'`]/.test(text)) problems.push(`${rel} names curl`);
    const code = text.split("\n").filter((l) => !isCommentLine(l)).join("\n");
    if (CURL_SHAPES.some((re) => re.test(code))) problems.push(`${rel} builds a curl argument list`);
  }
  return problems;
}

test("no mod or lib file calls fetch or names a URL other than 127.0.0.1, and only lib/cao.js names curl", () => {
  assert.deepEqual(dataPathProblems(HERE), []);
});

test("the data-path check catches fetch, other URLs and curl outside lib/cao.js, and allows the loopback URL", () => {
  const cases = [
    ['fetch("x")', path.join("lib", "planted.js"), /calls fetch/],
    ["await window.fetch (u)", path.join("mods", "cao-window-side", "main.js"), /calls fetch/],
    ['const u = "https://example.test/x";', path.join("lib", "planted.js"), /names https:\/\/example\.test/],
    [j("const u = `http://local", "host:9889/x`;"), path.join("mods", "cao-window-panel", "main.js"), /names http:\/\/localhost/],
    ["const u = 'http://127.0.0.1.example.test/x';", path.join("lib", "planted.js"), /names http:\/\/127\.0\.0\.1\.example/],
    ['exec("curl", [])', path.join("lib", "planted.js"), /names curl/],
    ["exec(`curl`, [])", path.join("mods", "cao-window-status", "main.js"), /names curl/],
  ];
  for (const [text, rel, expected] of cases) {
    const dir = tempCopy();
    plant(dir, text, rel);
    assert.ok(dataPathProblems(dir).some((p) => expected.test(p)), `not caught: ${text}`);
  }
  const ok = tempCopy();
  plant(ok, 'const u = "http://127.0.0.1:9889/flows"; // curl is only named in prose', path.join("lib", "planted.js"));
  assert.deepEqual(dataPathProblems(ok), []);
  const caoJs = tempCopy();
  plant(caoJs, 'exec("curl", args);', path.join("lib", "cao.js"));
  assert.deepEqual(dataPathProblems(caoJs), []);
});

test("a curl argument list built without naming curl is caught outside lib/cao.js", () => {
  const cases = [
    [j('const a = ["-sS", "-w", "\\n%{http_code}", u];'), path.join("mods", "cao-window-side", "main.js")],
    [j("const a = ['-w', '%{http_code}'];"), path.join("lib", "planted.js")],
    [j('run(["--max-time", "3", u]);'), path.join("mods", "cao-window-status", "main.js")],
    [j('args.push("--data-binary", body);'), path.join("lib", "planted.js")],
    [j("const fmt = `%{http_code}`;"), path.join("lib", "planted.js")],
  ];
  for (const [text, rel] of cases) {
    const dir = tempCopy();
    plant(dir, text, rel);
    assert.ok(dataPathProblems(dir).some((p) => /builds a curl argument list/.test(p)), `not caught: ${text}`);
  }
});

test("the same shapes are fine in lib/cao.js and in a comment elsewhere", () => {
  const caoJs = tempCopy();
  plant(caoJs, 'const args = ["-sS", "-w", "\\n%{http_code}", "--max-time", "3", "--data-binary", b];', path.join("lib", "cao.js"));
  assert.deepEqual(dataPathProblems(caoJs), []);
  const prose = tempCopy();
  plant(prose, '// curl prints the code with -w "\\n%{http_code}" and --max-time bounds it\n/* "-w" and "--data-binary" are cao.js business */\n * "--max-time"', path.join("lib", "planted.js"));
  assert.deepEqual(dataPathProblems(prose), []);
});

test("the URL host is parsed, so a loopback prefix in front of another host does not pass", () => {
  const bad = [
    "http://127.0.0.1:1@other.host/",
    "http://127.0.0.1:9889@evil.test/x",
    "http://127.0.0.1@evil.test/",
    "http://127.0.0.1.example.test/x",
    "http://127.0.0.1:9889.evil.test/x",
    "https://127.0.0.1:9889/x",
    "http://[::1]:9889/x",
    "http://127.0.0.2:9889/x",
    "http://${host}:9889/x",
    "http://user:pw@127.0.0.1:9889/x",
  ];
  for (const url of bad) {
    const dir = tempCopy();
    plant(dir, `const u = \`${url}\`;`, path.join("mods", "cao-window-panel", "main.js"));
    assert.ok(dataPathProblems(dir).some((p) => p.includes("names ")), `not caught: ${url}`);
  }
});

test("loopback URLs, with or without a port or a template placeholder, are allowed", () => {
  const dir = tempCopy();
  plant(dir, ["const a = `http://127.0.0.1:${cfg.port}${path}${qs}`;", 'const b = "http://127.0.0.1:9889/flows";', 'const c = "http://127.0.0.1/";', "// see http://127.0.0.1:9889/health"].join("\n"), path.join("lib", "cao.js"));
  assert.deepEqual(dataPathProblems(dir), []);
});

await finish();
