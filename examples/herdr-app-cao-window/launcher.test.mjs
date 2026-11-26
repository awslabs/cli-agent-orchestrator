// The launcher template and the shell function install.sh uses to fill it (cao-window-install,
// "A launcher starts the window with its home and core session": Launcher content on macOS, Launcher does
// not inherit a caller's core, Unsupported platform).
import { spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { assert, finish, mkTmp, test } from "./test/harness.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const FILL = join(here, "bin", "launcher-fill.sh");
const TEMPLATE = join(here, "bin", "herdr-cao.in");
const tmp = () => mkTmp("cao-window-launcher-");

// Runs fill_launcher from `script` and returns {status, out path, text|null, stderr}.
function fill(os, app, home, core, script = FILL, outName = "herdr-cao") {
  const out = join(tmp(), outName);
  const r = spawnSync("sh", ["-c", '. "$1"; fill_launcher "$2" "$3" "$4" "$5" "$6" "$7"', "sh", script, TEMPLATE, out, os, app, home, core], { encoding: "utf8" });
  return { status: r.status, stderr: r.stderr, out, text: existsSync(out) ? readFileSync(out, "utf8") : null };
}
// A copy of the fill script with Linux support switched on, so the Linux branch is always covered.
function linuxEnabledScript() {
  const p = join(tmp(), "launcher-fill.sh");
  writeFileSync(p, readFileSync(FILL, "utf8").replace(/^LINUX_SUPPORTED=\d/m, "LINUX_SUPPORTED=1"));
  return p;
}
// Runs a filled launcher with a stub `open` / `env`-less PATH and returns the argv the stub received,
// one entry per argument, so quoting is judged by what the program sees and not by how the text looks.
function runLauncher(launcher, stubName) {
  const dir = tmp();
  const log = join(dir, "argv.json");
  const stubs = join(dir, "stubs");
  mkdirSync(stubs);
  writeFileSync(join(stubs, stubName), `#!/bin/sh\nnode -e 'require("fs").writeFileSync(process.argv[1], JSON.stringify(process.argv.slice(2)))' "${log}" "$@"\n`);
  chmodSync(join(stubs, stubName), 0o755);
  const r = spawnSync("sh", [launcher], { env: { ...process.env, PATH: `${stubs}:${process.env.PATH}` }, encoding: "utf8" });
  return { status: r.status, argv: existsSync(log) ? JSON.parse(readFileSync(log, "utf8")) : null, stderr: r.stderr };
}

test("template: a shebang line and one placeholder for the launch line", () => {
  const text = readFileSync(TEMPLATE, "utf8");
  assert.ok(text.startsWith("#!/bin/sh\n"));
  assert.equal(text.match(/@@[A-Z_]+@@/g)?.length, 1, "exactly one placeholder");
});

test("Darwin: open -n -a with the home and the core session", () => {
  const r = fill("Darwin", "/Applications/Herdr.app", "/tmp/h", "caowin");
  assert.equal(r.status, 0);
  assert.ok(r.text.startsWith("#!/bin/sh\n"));
  assert.ok(r.text.includes("open -n -a /Applications/Herdr.app --env HERDR_APP_HOME=/tmp/h --env HC_SESSION=caowin"), r.text);
  assert.ok(!r.text.includes("@@"));
  assert.ok(statSync(r.out).mode & 0o111, "executable");
});

test("Darwin: the launcher passes exactly those arguments to open", () => {
  const r = fill("Darwin", "/Applications/Herdr.app", "/tmp/h", "caowin");
  const run = runLauncher(r.out, "open");
  assert.equal(run.status, 0, run.stderr);
  assert.deepEqual(run.argv, ["-n", "-a", "/Applications/Herdr.app", "--env", "HERDR_APP_HOME=/tmp/h", "--env", "HC_SESSION=caowin"]);
});

test("values with spaces or quotes are shell-quoted and the file parses", () => {
  const r = fill("Darwin", "/Applications/My Herdr.app", "/tmp/it's here", "caowin");
  assert.equal(r.status, 0);
  assert.ok(r.text.includes("'/Applications/My Herdr.app'"));
  assert.equal(spawnSync("sh", ["-n", r.out]).status, 0);
});

test("odd characters reach the app as one argument each and are never run as shell", () => {
  const dir = tmp();
  const marker = join(dir, "pwned");
  const nasty = [
    ["/Applications/My Herdr.app", "/tmp/it's here", "caowin"],
    ["/Applications/Herdr.app", `/tmp/a b;touch ${marker}`, "caowin"],
    ["/Applications/Herdr.app", "/tmp/h", `core$(touch ${marker})`],
    ["/Applications/Herdr.app", "/tmp/h", "core`touch " + marker + "`"],
    ["/Applications/Herdr.app", "/tmp/dollar$HOME and *glob* and \"dq\" and \\back", "c o r e"],
    ["/Applications/Herdr.app", "/tmp/h\nnewline", "caowin"],
  ];
  for (const [app, home, core] of nasty) {
    const r = fill("Darwin", app, home, core);
    assert.equal(r.status, 0, r.stderr);
    assert.equal(spawnSync("sh", ["-n", r.out]).status, 0, `does not parse: ${JSON.stringify(home)}`);
    const run = runLauncher(r.out, "open");
    assert.deepEqual(run.argv, ["-n", "-a", app, "--env", `HERDR_APP_HOME=${home}`, "--env", `HC_SESSION=${core}`]);
  }
  assert.equal(existsSync(marker), false, "a value was executed as shell");
});

test("Linux: the four caller variables are unset before the app starts (when enabled)", () => {
  const r = fill("Linux", "/opt/herdr/herdr-app-gui", "/tmp/h", "caowin", linuxEnabledScript());
  assert.equal(r.status, 0);
  for (const v of ["HC_TERMINAL_ID", "HC_TERMINAL_SOCKET", "HC_SCOPE", "HC_SOCKET"]) assert.ok(r.text.includes(`-u ${v}`), v);
  assert.ok(r.text.includes("HERDR_APP_HOME=/tmp/h HC_SESSION=caowin /opt/herdr/herdr-app-gui"));
  assert.equal(spawnSync("sh", ["-n", r.out]).status, 0);
});

test("Linux (when enabled): a caller's core variables do not reach the app", () => {
  const dir = tmp();
  const out = join(dir, "env.txt");
  // The "app" prints whether the four caller variables and the two window variables are set, then exits.
  const app = join(dir, "app.sh");
  writeFileSync(app, `#!/bin/sh\nprintf '%s|%s|%s|%s|%s|%s\\n' "\${HC_TERMINAL_ID-unset}" "\${HC_TERMINAL_SOCKET-unset}" "\${HC_SCOPE-unset}" "\${HC_SOCKET-unset}" "$HERDR_APP_HOME" "$HC_SESSION" > "${out}"\n`);
  chmodSync(app, 0o755);
  const filled = fill("Linux", app, "/tmp/h", "caowin", linuxEnabledScript());
  assert.equal(filled.status, 0, filled.stderr);
  const env = { ...process.env, HC_TERMINAL_ID: "t9", HC_TERMINAL_SOCKET: "/evil.sock", HC_SCOPE: "s", HC_SOCKET: "/evil2.sock", HC_SESSION: "default", HERDR_APP_HOME: "/main/home" };
  assert.equal(spawnSync("sh", [filled.out], { env }).status, 0);
  for (let i = 0; i < 100 && !existsSync(out); i++) spawnSync("sleep", ["0.05"]);   // the app is started in the background
  assert.equal(readFileSync(out, "utf8").trim(), "unset|unset|unset|unset|/tmp/h|caowin");
});

test("Linux as shipped: exit 4 unless Linux support was enabled", () => {
  const shipped = /^LINUX_SUPPORTED=1/m.test(readFileSync(FILL, "utf8"));
  const r = fill("Linux", "/opt/herdr/herdr-app-gui", "/tmp/h", "caowin");
  if (shipped) assert.equal(r.status, 0);
  else { assert.equal(r.status, 4); assert.equal(r.text, null); }
});

test("an unsupported platform exits 4, writes nothing and points to the README topologies", () => {
  for (const os of ["FreeBSD", "", "Windows_NT", "darwin"]) {
    const r = fill(os, "x", "/tmp/h", "caowin");
    assert.equal(r.status, 4, JSON.stringify(os));
    assert.equal(r.text, null);
    assert.match(r.stderr, /README/);
    assert.match(r.stderr, /topolog/i);
  }
});

test("a failed fill leaves an existing launcher untouched", () => {
  const dir = tmp();
  const out = join(dir, "herdr-cao");
  writeFileSync(out, "#!/bin/sh\necho old\n");
  const r = spawnSync("sh", ["-c", '. "$1"; fill_launcher "$2" "$3" FreeBSD x /tmp/h caowin', "sh", FILL, TEMPLATE, out], { encoding: "utf8" });
  assert.equal(r.status, 4);
  assert.equal(readFileSync(out, "utf8"), "#!/bin/sh\necho old\n");
});

test("an existing launcher is replaced whole, stays executable, and leaves no temp file", () => {
  const dir = tmp();
  const out = join(dir, "herdr-cao");
  writeFileSync(out, "old\n");
  chmodSync(out, 0o600);
  const r = spawnSync("sh", ["-c", '. "$1"; fill_launcher "$2" "$3" Darwin /Applications/Herdr.app /tmp/h caowin', "sh", FILL, TEMPLATE, out], { encoding: "utf8" });
  assert.equal(r.status, 0, r.stderr);
  assert.ok(readFileSync(out, "utf8").includes("open -n -a"));
  assert.ok(statSync(out).mode & 0o111);
  assert.deepEqual(spawnSync("ls", [dir], { encoding: "utf8" }).stdout.trim().split("\n"), ["herdr-cao"]);
});

test("filling the same inputs twice gives the same bytes", () => {
  const a = fill("Darwin", "/Applications/Herdr.app", "/tmp/h", "caowin");
  const b = fill("Darwin", "/Applications/Herdr.app", "/tmp/h", "caowin");
  assert.ok(a.text !== null && a.text.length > 0, "nothing was written");
  assert.equal(a.text, b.text);
});

// ---- the app copy: fill_launcher takes the copy's path as a seventh argument ----

function fillWithCopy(os, app, home, core, copy, script = FILL) {
  const out = join(tmp(), "herdr-cao");
  const r = spawnSync("sh", ["-c", '. "$1"; fill_launcher "$2" "$3" "$4" "$5" "$6" "$7" "$8"', "sh", script, TEMPLATE, out, os, app, home, core, copy], { encoding: "utf8" });
  return { status: r.status, stderr: r.stderr, out, text: existsSync(out) ? readFileSync(out, "utf8") : null };
}

test("Darwin with a copy: the launcher opens the copy while it is a folder, and Herdr.app when it is not", () => {
  const copy = join(tmp(), "Applications", "CAO.app");
  const r = fillWithCopy("Darwin", "/Applications/Herdr.app", "/tmp/h", "caowin", copy);
  assert.equal(r.status, 0, r.stderr);
  assert.ok(!r.text.includes("@@"));
  assert.equal(r.text.match(/\bexec open /g)?.length, 2, "one open line for the copy and one for the app");
  assert.equal(runLauncher(r.out, "open").argv[2], "/Applications/Herdr.app", "no copy: Herdr.app");
  mkdirSync(copy, { recursive: true });
  assert.deepEqual(runLauncher(r.out, "open").argv, ["-n", "-a", copy, "--env", "HERDR_APP_HOME=/tmp/h", "--env", "HC_SESSION=caowin"]);
});

test("Darwin with a copy: a path with spaces and quotes reaches open as one argument", () => {
  const copy = join(tmp(), "my apps", "it's $HOME", "CAO.app");
  mkdirSync(copy, { recursive: true });
  const r = fillWithCopy("Darwin", "/Applications/Other Herdr.app", "/tmp/h", "caowin", copy);
  assert.equal(r.status, 0, r.stderr);
  assert.equal(runLauncher(r.out, "open").argv[2], copy);
});

test("an empty copy argument gives the launcher of before: one open line, Herdr.app only", () => {
  const withEmpty = fillWithCopy("Darwin", "/Applications/Herdr.app", "/tmp/h", "caowin", "");
  assert.equal(withEmpty.text, fill("Darwin", "/Applications/Herdr.app", "/tmp/h", "caowin").text);
});

test("Linux ignores a copy: the app is started as a program", () => {
  const r = fillWithCopy("Linux", "/opt/herdr/herdr-app", "/tmp/h", "caowin", "/tmp/CAO.app", linuxEnabledScript());
  assert.equal(r.status, 0, r.stderr);
  assert.ok(!r.text.includes("CAO.app"));
});

await finish();
