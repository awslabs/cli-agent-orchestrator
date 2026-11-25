// install.sh and uninstall.sh run into a temporary HOME with stub hc, herdr-app, uname, open and
// defaults on PATH, plus failing curl, wget and git. No GUI starts, and the real hc is never run: the
// stubs come first on PATH and every call is logged. (cao-window-install: the home and core session,
// idempotence and backups, offline, version and layout drift, fresh-home settings, the launcher,
// uninstall, the two environment switches.)
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmodSync, cpSync, existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, realpathSync, rmSync, statSync, symlinkSync, utimesSync, writeFileSync,
} from "node:fs";
import { dirname, isAbsolute, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { assert, finish, hasTool, mkTmp, skip, test } from "./test/harness.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const INSTALL = join(here, "install.sh");
const UNINSTALL = join(here, "uninstall.sh");
// The drawings of our own that the side mod loads by `mods/cao-window-side/<file>` (the crown and the pin): a mod folder's `*.svg` is
// part of the mod, so the installer copies it and lists it in the manifest.
const MOD_ASSETS = ["crown.svg", "pin-filled.svg", "pin-tilted.svg"];
// The tabs mod's own icons of the Flows and Workflows buttons (task 27.22), loaded by `mods/cao-window-tabs/<file>`.
const TAB_ASSETS = ["flows.svg", "workflows.svg"];
// The brand mod's two drawings of the CAO mark (design D42 A): the site's files, drawn by `image()` in full colour.
const BRAND_ASSETS = ["cao-mark-dark.svg", "cao-mark.svg"];
const DRAWINGS = { side: MOD_ASSETS, tabs: TAB_ASSETS, brand: BRAND_ASSETS };
const drawingsOf = (m) => DRAWINGS[m] ?? [];
if (!hasTool("jq")) skip("install.test.mjs: jq is not installed");

const STUB_HC = `#!/bin/sh
D=$(dirname "$0")
{ printf 'HC_SESSION=%s hc' "\${HC_SESSION:-}"; for a in "$@"; do case $a in *' '*) printf ' "%s"' "$a";; *) printf ' %s' "$a";; esac; done; echo
  [ -n "\${HC_SOCKET:-}\${HC_TERMINAL_ID:-}\${HC_SCOPE:-}\${HC_TERMINAL_SOCKET:-}" ] && echo "LEAK hc saw a caller core variable"; } >> "$CALLS"
case "$1" in
  --version) echo "hc $(cat "$D/hc.version")"; exit 0 ;;
  machine)
    case "$2" in
      ls) cat "$D/machines.json"; exit 0 ;;
      add) [ -f "$D/add.fail" ] && exit 9
           name=cao; while [ $# -gt 0 ]; do [ "$1" = --name ] && name=$2; shift; done
           printf '[{"name":"%s"}]\\n' "$name" > "$D/machines.json"; exit 0 ;;
      rm) [ -f "$D/rm.fail" ] && exit 9
          printf '[]\\n' > "$D/machines.json"; exit 0 ;;
    esac ;;
  server)
    case "$2" in
      stop) [ -f "$D/stop.fail" ] && exit 9 ;;
    esac ;;
esac
exit 0
`;
const STUB_APP = `#!/bin/sh
D=$(dirname "$0")
{ printf 'HERDR_APP_HOME=%s herdr-app' "\${HERDR_APP_HOME:-}"; for a in "$@"; do printf ' %s' "$a"; done; echo; } >> "$CALLS"
[ "$1" = status ] && exit "$(cat "$D/app.live")"
exit 0
`;
const STUB_FILE = (name) => `#!/bin/sh\nprintf '%s\\n' "${name} $*" >> "$CALLS"\nexit 0\n`;
const STUB_ECHO = (name, file) => `#!/bin/sh\nprintf '%s\\n' "${name} $*" >> "$CALLS"\ncat "$(dirname "$0")/${file}"\n`;
const STUB_NET = (name) => `#!/bin/sh\nprintf '%s\\n' "${name} $*" >> "$NETLOG"\nexit 1\n`;
// An `open` that records its arguments one per entry, for the launcher argv checks.
const STUB_OPEN_ARGV = `#!/bin/sh\nnode -e 'require("fs").writeFileSync(process.argv[1], JSON.stringify(process.argv.slice(2)))' "$ARGV_OUT" "$@"\n`;

const REAL_MV = spawnSync("sh", ["-c", "command -v mv"], { encoding: "utf8" }).stdout.trim();
const REAL_DATE = spawnSync("sh", ["-c", "command -v date"], { encoding: "utf8" }).stdout.trim();
// A `date` whose epoch the test sets through the file named by $DATE_FILE; any other call is the real one.
const STUB_DATE = `#!/bin/sh
if [ "$1" = "+%s" ] && [ -n "\${DATE_FILE:-}" ] && [ -f "$DATE_FILE" ]; then cat "$DATE_FILE"; exit 0; fi
exec ${REAL_DATE} "$@"
`;
const REAL_CHMOD = spawnSync("sh", ["-c", "command -v chmod"], { encoding: "utf8" }).stdout.trim();
// A `chmod` that parses its arguments as macOS (BSD) does: the first operand that is not an option is the
// mode and every operand after it is a file name, so `chmod 700 -- dir` looks for a file called "--" and
// fails. GNU chmod accepts that form, which is why the Linux runs passed while every Mac install failed.
// Every call is logged to "$CALLS.chmod".
const STUB_BSD_CHMOD = `#!/bin/sh
printf '%s\\n' "$*" >> "$CALLS.chmod"
mode=
for a in "$@"; do
  if [ -z "$mode" ]; then
    case $a in -*) continue ;; esac
    mode=$a
    continue
  fi
  if [ ! -e "$a" ] && [ ! -L "$a" ]; then echo "chmod: $a: No such file or directory" >&2; exit 1; fi
done
exec ${REAL_CHMOD} "$@"
`;
const REAL_CP = spawnSync("sh", ["-c", "command -v cp"], { encoding: "utf8" }).stdout.trim();
// macOS's `cp -c` clones on APFS. This `cp` logs the clone form (-cR) and the plain recursive form (-R), makes the clone fail
// when stubs/cp.noclone exists (a volume that cannot clone), and otherwise runs the real cp as `cp -R`. Any other call is the real cp.
const STUB_CP = `#!/bin/sh
case \${1:-} in
  -cR) printf 'cp %s\\n' "$*" >> "$CALLS"
       if [ -f "$(dirname "$0")/cp.noclone" ]; then echo "cp: clonefile failed: Operation not supported" >&2; exit 1; fi
       shift; exec ${REAL_CP} -R "$@" ;;
  -R) printf 'cp %s\\n' "$*" >> "$CALLS" ;;
esac
exec ${REAL_CP} "$@"
`;
// PlistBuddy over a text "plist": one Key=value line per entry. One -c command per call, as install.sh makes them.
// Print fails for a missing key and Add for an existing one, as the real tool does.
const STUB_PLISTBUDDY = `#!/bin/sh
printf 'PlistBuddy %s\\n' "$*" >> "$CALLS"
[ "$1" = -c ] && [ $# -eq 3 ] || { echo "stub PlistBuddy: expects -c COMMAND FILE" >&2; exit 64; }
cmd=$2; file=$3
[ -f "$file" ] || { echo "File Doesn't Exist, Will Create: $file" >&2; exit 1; }
op=\${cmd%% *}; rest=\${cmd#* }
key=\${rest%% *}; key=\${key#:}
has() { grep -q "^$key=" "$file"; }
case $op in
  Print) has || { echo "Print: Entry, \":$key\", Does Not Exist" >&2; exit 1; }
         grep "^$key=" "$file" | head -n 1 | cut -d= -f2- ;;
  Set) has || { echo "Set: Entry, \":$key\", Does Not Exist" >&2; exit 1; }
       val=\${rest#* }
       grep -v "^$key=" "$file" > "$file.tmp"; printf '%s=%s\\n' "$key" "$val" >> "$file.tmp"; mv "$file.tmp" "$file" ;;
  Add) has && { echo "Add: \":$key\" Entry Already Exists" >&2; exit 1; }
       val=\${rest#* }; val=\${val#* }
       printf '%s=%s\\n' "$key" "$val" >> "$file" ;;
  Delete) has || { echo "Delete: Entry, \":$key\", Does Not Exist" >&2; exit 1; }
          grep -v "^$key=" "$file" > "$file.tmp"; mv "$file.tmp" "$file" ;;
  *) echo "stub PlistBuddy: no $op" >&2; exit 64 ;;
esac
`;
// codesign and xattr: logged; codesign fails while stubs/codesign.fail exists.
const STUB_CODESIGN = `#!/bin/sh
printf 'codesign %s\\n' "$*" >> "$CALLS"
[ -f "$(dirname "$0")/codesign.fail" ] && { echo "codesign: stub failure" >&2; exit 1; }
exit 0
`;
const JQ_DIR = dirname(spawnSync("sh", ["-c", "command -v jq"], { encoding: "utf8" }).stdout.trim());

// copy: true makes a world that builds the app copy: a fake Herdr.app (its plist is a Key=value text file read by the PlistBuddy
// stub) next to the stubs, and `install` passes --app and --app-copy-dir for it unless the call gives its own. Every other world
// passes --no-app-copy, so the cases that are not about the copy do not depend on it.
function world({ hc = "0.1.21", app = "0.1.16", os = "Darwin", machines = "[]", live = 0, spaced = false, copy = false } = {}) {
  const root = realpathSync(mkTmp("cao-window-install-"));   // canonical, as install.sh sees it
  const home = join(root, spaced ? "my home" : "home");
  const stubs = join(root, "stubs");
  mkdirSync(join(home, ".config"), { recursive: true });
  mkdirSync(stubs);
  mkdirSync(join(root, "tmp"));
  const put = (name, text) => { writeFileSync(join(stubs, name), text); chmodSync(join(stubs, name), 0o755); };
  put("hc", STUB_HC); put("herdr-app", STUB_APP); put("open", STUB_FILE("open"));
  put("chmod", STUB_BSD_CHMOD);   // every install in this suite runs under macOS's argument rules for chmod
  put("uname", STUB_ECHO("uname", "uname.out")); put("defaults", STUB_ECHO("defaults", "app.version"));
  for (const n of ["curl", "wget", "git"]) put(n, STUB_NET(n));
  put("cp", STUB_CP); put("PlistBuddy", STUB_PLISTBUDDY); put("codesign", STUB_CODESIGN); put("xattr", STUB_FILE("xattr"));
  writeFileSync(join(stubs, "hc.version"), `${hc}\n`);
  writeFileSync(join(stubs, "app.version"), `${app}\n`);
  writeFileSync(join(stubs, "uname.out"), `${os}\n`);
  writeFileSync(join(stubs, "machines.json"), `${machines}\n`);
  writeFileSync(join(stubs, "app.live"), `${live}\n`);
  const sourceApp = join(root, "Herdr.app");
  const copyDir = join(root, "Applications");
  if (copy) {
    mkdirSync(join(sourceApp, "Contents", "MacOS"), { recursive: true });
    mkdirSync(join(sourceApp, "Contents", "Resources"), { recursive: true });
    mkdirSync(join(sourceApp, "Contents", "_CodeSignature"));
    writeFileSync(join(sourceApp, "Contents", "Info.plist"), [
      "CFBundleName=Herdr", "CFBundleDisplayName=Herdr", "CFBundleExecutable=Herdr", "CFBundleIconFile=Herdr",
      "CFBundleIdentifier=dev.herdr.app", `CFBundleShortVersionString=${app}`, "",
    ].join("\n"));
    writeFileSync(join(sourceApp, "Contents", "MacOS", "Herdr"), "#!/bin/sh\n");
    writeFileSync(join(sourceApp, "Contents", "Resources", "Herdr.icns"), "herdr icon\n");
    writeFileSync(join(sourceApp, "Contents", "_CodeSignature", "CodeResources"), "original signature\n");
  }
  const calls = join(root, "calls.log");
  const netlog = join(root, "net.log");
  writeFileSync(calls, ""); writeFileSync(netlog, "");
  const env = {
    PATH: `${stubs}:${process.env.PATH}`, HOME: home, XDG_CONFIG_HOME: join(home, ".config"),
    CALLS: calls, NETLOG: netlog, LANG: "C", TMPDIR: join(root, "tmp"),
  };
  const w = {
    root, home, stubs, env, calls, netlog, tmpdir: env.TMPDIR, sourceApp, copyDir, copyApp: join(copyDir, "CAO.app"),
    xdg: env.XDG_CONFIG_HOME, caoHome: join(env.XDG_CONFIG_HOME, "herdr-app-cao"),
    mainHome: join(env.XDG_CONFIG_HOME, "herdr-app"), launcher: join(home, ".local", "bin", "herdr-cao"),
    put,
    run(script, args, extraEnv = {}, cwd = root) {
      // A missing script must fail every case, including the ones that only expect a refusal: sh's own
      // "not found" exit would otherwise look like one.
      assert.ok(existsSync(script), `${script} does not exist yet`);
      const r = spawnSync("sh", [script, ...args], { env: { ...env, ...extraEnv }, encoding: "utf8", cwd });
      return { status: r.status, signal: r.signal, out: r.stdout, err: r.stderr, text: r.stdout + r.stderr };
    },
    install: (args, e, cwd) => w.run(INSTALL, copy ? [...args, ...(args.includes("--app") ? [] : ["--app", sourceApp]), ...(args.includes("--app-copy-dir") || args.includes("--no-app-copy") ? [] : ["--app-copy-dir", copyDir])]
      : args.includes("--no-app-copy") ? args : [...args, "--no-app-copy"], e, cwd),
    uninstall: (args, e, cwd) => w.run(UNINSTALL, args, e, cwd),
    log: () => readFileSync(calls, "utf8").split("\n").filter(Boolean),
    net: () => readFileSync(netlog, "utf8").trim(),
    json: (rel) => JSON.parse(readFileSync(join(w.caoHome, rel), "utf8")),
    layout: () => w.json("layouts/cao-window.json"),
    adds: () => w.log().filter((l) => l.includes(" machine add")),
    tmpLeft: () => readdirSync(env.TMPDIR),
  };
  return w;
}

const walk = (dir) => !existsSync(dir) ? [] : readdirSync(dir, { recursive: true, withFileTypes: true })
  .filter((e) => e.isFile()).map((e) => relative(dir, join(e.parentPath ?? e.path, e.name))).sort();
const files = walk;
const sha = (path) => createHash("sha256").update(readFileSync(path)).digest("hex");
const snapshot = (dir) => Object.fromEntries(files(dir).map((f) => [f, [statSync(join(dir, f)).mtimeMs, sha(join(dir, f))]]));
const MAIN_LAYOUT = join(here, "layouts", "main.json");
const withMainHome = (w, layout = readFileSync(MAIN_LAYOUT, "utf8")) => {
  mkdirSync(join(w.mainHome, "layouts"), { recursive: true });
  writeFileSync(join(w.mainHome, "layouts", "main.json"), layout);
};
// Sets every file under dir (and the launcher) to an old mtime, so "unchanged" can be told from "rewritten".
const age = (w) => {
  const old = new Date("2001-02-03T04:05:06Z");
  for (const f of [...files(w.caoHome).map((f) => join(w.caoHome, f)), ...(existsSync(w.launcher) ? [w.launcher] : [])]) utimesSync(f, old, old);
};
const find = (n, id) => (n.id === id ? n : (n.children ?? []).map((c) => find(c, id)).find(Boolean));
const CAO_BLOCKS = ["cao-sessions", "cao-agents", "cao-window-panel", "cao-window-tabs", "status"];
// What the earlier three-block version of the installer derived from layouts/main.json.
const EARLIER_LAYOUT = join(here, "test", "fixtures", "cao-window.three-block.json");
const EARLIER_SIDE_IDS = ["cao-flows", "cao-workflows"];   // (earlier layout)
const idsOf = (n) => [n?.id, ...(n?.children ?? []).flatMap(idsOf)].filter(Boolean);
// Makes the home look as an install of the earlier version left it: its layout, and a manifest that recorded that layout's hash.
function installEarlierLayout(w, { recorded = true } = {}) {
  const layoutPath = join(w.caoHome, "layouts", "cao-window.json");
  writeFileSync(layoutPath, readFileSync(EARLIER_LAYOUT, "utf8"));
  if (!recorded) return layoutPath;
  const manifestPath = join(w.caoHome, "cao-window-install.json");
  const m = JSON.parse(readFileSync(manifestPath, "utf8"));
  m.files.find((f) => f.path === "layouts/cao-window.json").sha256 = sha(layoutPath);
  writeFileSync(manifestPath, `${JSON.stringify(m, null, 2)}\n`);
  return layoutPath;
}
const assertTwoBlockLayout = (layout) => {
  assert.deepEqual(find(layout, "sidebar-lists").children.map((c) => c.id), ["cao-sessions", "cao-agents"]);
  for (const gone of EARLIER_SIDE_IDS) assert.ok(!find(layout, gone), gone);
  const all = idsOf(layout);
  assert.deepEqual(all.filter((id, i) => all.indexOf(id) !== i), [], "an id appears twice");
  assert.equal(find(layout, "cao-window-panel").hidden, true);
  assert.equal(find(layout, "cao-window-tabs").min, 34);
  // owner review (section 27): the left sidebar's edge drags between 200 and 640 px, from 268 px
  const side = find(layout, "sidebar");
  assert.deepEqual([side.size, side.min, side.max], [{ px: 268 }, 200, 640]);
  assert.equal(side.resizable, false, "the blocks inside the sidebar stay locked");
  assert.notEqual(find(layout, "root").resizable, false, "the root row's boundary, the one the user drags, is not locked");
};
const baks = (dir, name) => files(dir).filter((f) => new RegExp(`(^|/)${name.replace(/\./g, "\\.")}\\.bak-\\d+$`).test(f));
const nowEpoch = () => Math.floor(Date.now() / 1000);
// What a launcher passes to `open`, one entry per argument.
function launcherArgv(w, launcherPath) {
  const dir = mkTmp("cao-window-argv-");
  mkdirSync(join(dir, "bin"));
  writeFileSync(join(dir, "bin", "open"), STUB_OPEN_ARGV);
  chmodSync(join(dir, "bin", "open"), 0o755);
  const out = join(dir, "argv.json");
  const r = spawnSync("sh", [launcherPath], { env: { ...w.env, PATH: `${join(dir, "bin")}:${process.env.PATH}`, ARGV_OUT: out }, encoding: "utf8" });
  return { status: r.status, argv: existsSync(out) ? JSON.parse(readFileSync(out, "utf8")) : null, err: r.stderr };
}
const nothingWritten = (w) => {
  assert.deepEqual(files(w.caoHome), [], "files were written to the window's home");
  assert.ok(!existsSync(w.launcher), "the launcher was written");
  assert.deepEqual(w.adds(), [], "hc machine add was called");
};

// ---------------------------------------------------------------------------------------------
// Default and same-machine install
// ---------------------------------------------------------------------------------------------

test("default install: home, layout, mods, shared.json, manifest, machine entry", () => {
  const w = world();
  const r = w.install(["--cao-ssh-host", "build-box"]);
  assert.equal(r.status, 0, r.text);
  assert.ok(existsSync(join(w.caoHome, "layouts", "cao-window.json")));
  assert.equal(w.json("shared.json").layout, "cao-window");
  assert.equal(w.json("shared.json")["frame.welcomed"], "finished");   // a flat string key, as the verified experiment seeded it
  assert.equal(w.json("shared.json").frame, undefined);                  // not a nested object
  for (const m of ["side", "panel", "tabs", "status"]) {
    const dir = join(w.caoHome, "mods", `cao-window-${m}`);
    for (const f of ["mod.json", "main.js", "logic.js", "cao.js", "store.js", "honesty.js"]) assert.ok(existsSync(join(dir, f)), `${m}/${f}`);
  }
  assert.equal(existsSync(join(w.caoHome, "mods", "cao-window-toast")), false);   // TOAST_MOD=0 by default: no toast folder
  assert.match(r.out, /toast mod off, mod imports: copy/);
  for (const f of MOD_ASSETS) assert.ok(existsSync(join(w.caoHome, "mods", "cao-window-side", f)), `the side mod's drawing ${f}`);
  const manifest = w.json("cao-window-install.json");
  assert.equal(manifest.core, "caowin");
  assert.ok(manifest.files.every((f) => /^[0-9a-f]{64}$/.test(f.sha256)));
  assert.ok(w.log().includes("HC_SESSION=caowin hc machine add build-box --session cao --name cao"), w.log().join("\n"));
  assert.equal(w.layout().toast, undefined);   // TOAST_MOD=0 by default
  assertTwoBlockLayout(w.layout());            // the left sidebar's drag range among them (section 27)
});

test("every shared module is copied flat into every mod folder, byte for byte", () => {
  const w = world();
  assert.equal(w.install(["--local"]).status, 0);
  const libs = readdirSync(join(here, "lib")).filter((f) => f.endsWith(".js"));
  assert.ok(libs.length >= 8, `lib/*.js: ${libs}`);
  for (const m of ["side", "panel", "tabs", "status"]) {
    const dir = join(w.caoHome, "mods", `cao-window-${m}`);
    for (const f of libs) assert.equal(sha(join(dir, f)), sha(join(here, "lib", f)), `${m}/${f}`);
    assert.equal(sha(join(dir, "main.js")), sha(join(here, "mods", `cao-window-${m}`, "main.js")));
    assert.equal(sha(join(dir, "mod.json")), sha(join(here, "mods", `cao-window-${m}`, "mod.json")));
  }
  assert.ok(!files(w.caoHome).some((f) => f.endsWith(".test.mjs")), "a test file was installed");
});

// A mod's drawings (`*.svg` in its folder) are installed with it, in both import modes, byte for byte, and each is a manifest entry
// with its hash; nothing else of the folder is.
for (const mode of ["copy", "bundle"]) {
  test(`${mode} mode: the side and tabs mods' svg drawings are installed byte for byte and listed in the manifest with their hash`, () => {
    const w = world();
    const r = w.install(["--local"], mode === "bundle" ? { MOD_IMPORTS: "bundle" } : {});
    assert.equal(r.status, 0, r.text);
    const manifest = new Map(w.json("cao-window-install.json").files.map((f) => [f.path, f.sha256]));
    const wanted = [];
    for (const [m, list] of Object.entries(DRAWINGS)) for (const f of list) {
      const src = join(here, "mods", `cao-window-${m}`, f);
      assert.ok(existsSync(src), `fixture: ${m}/${f} is in the example`);
      assert.equal(sha(join(w.caoHome, "mods", `cao-window-${m}`, f)), sha(src), `${m}/${f} differs from its source`);
      assert.equal(manifest.get(`mods/cao-window-${m}/${f}`), sha(src), `${m}/${f}: the manifest entry`);
      wanted.push(`mods/cao-window-${m}/${f}`);
    }
    const svgs = [...manifest.keys()].filter((f) => f.endsWith(".svg")).sort();
    assert.deepEqual(svgs, wanted.sort(), "no other svg is installed");
    assert.deepEqual(w.tmpLeft(), []);
  });
}

test("uninstall takes the drawings with the rest of the home: none is left where the window's home was", () => {
  const w = world();
  assert.equal(w.install(["--local"]).status, 0);
  assert.ok(MOD_ASSETS.every((f) => existsSync(join(w.caoHome, "mods", "cao-window-side", f))));
  assert.equal(w.uninstall([]).status, 0);
  assert.equal(existsSync(w.caoHome), false, "the home is gone from its place");
  const kept = join(w.xdg, readdirSync(w.xdg).find((n) => /^herdr-app-cao\.removed-\d+$/.test(n)));
  for (const f of MOD_ASSETS) assert.ok(existsSync(join(kept, "mods", "cao-window-side", f)), `${f} moved with the home`);
});

// The brand mod (27.20, D37 S): the window's own draw of the sidebar's top-left row, which says CAO where Herdr's says Herdr. It is
// a mod folder like the others (mod.json, main.js, and in copy mode the shared modules), with the two drawings of the CAO mark (D42 A);
// the two buttons use the files Herdr ships in every home.
const BRAND_MOD = join("mods", "cao-window-brand");
const blocksUsing = (n, mod) => [n, ...(n.children ?? []).flatMap((c) => blocksUsing(c, mod))].filter((b) => b.content?.mod === mod);
for (const mode of ["copy", "bundle"]) {
  test(`${mode} mode: the brand mod's folder is installed with its hashes in the manifest, and the layout's brand block names it`, () => {
    const w = world();
    const r = w.install(["--local"], mode === "bundle" ? { MOD_IMPORTS: "bundle" } : {});
    assert.equal(r.status, 0, r.text);
    const dir = join(w.caoHome, BRAND_MOD);
    const src = join(here, BRAND_MOD);
    assert.ok(existsSync(join(src, "main.js")) && existsSync(join(src, "mod.json")), "fixture: the brand mod is in the example");
    assert.equal(sha(join(dir, "mod.json")), sha(join(src, "mod.json")));
    const manifest = new Map(w.json("cao-window-install.json").files.map((f) => [f.path, f.sha256]));
    const installed = files(dir);
    for (const f of installed) assert.equal(manifest.get(`${BRAND_MOD}/${f}`), sha(join(dir, f)), `${f}: the manifest entry`);
    assert.deepEqual([...manifest.keys()].filter((f) => f.startsWith(`${BRAND_MOD}/`)).map((f) => f.slice(BRAND_MOD.length + 1)).sort(), installed, "the manifest lists what the folder holds, no more");
    if (mode === "copy") {
      assert.equal(sha(join(dir, "main.js")), sha(join(src, "main.js")));
      for (const f of readdirSync(join(here, "lib")).filter((x) => x.endsWith(".js"))) assert.equal(sha(join(dir, f)), sha(join(here, "lib", f)), `${f} is copied flat, byte for byte`);
    } else {
      assert.deepEqual(installed, ["cao-mark-dark.svg", "cao-mark.svg", "main.js", "mod.json"], "the one bundle, mod.json and the two drawings");
      const text = readFileSync(join(dir, "main.js"), "utf8");
      assert.ok(!/from\s+["']\.\.?\//.test(text), "a relative import is left");
      assert.match(text, /^const BRAND_TEXT = /m, "the shared constant is in the bundle, without its export");
      assert.match(text, /\.child\(BRAND_TEXT\)/);
      const copy = join(w.root, "brand.mjs");
      writeFileSync(copy, text);
      const c = spawnSync(process.execPath, ["--check", copy], { encoding: "utf8" });
      assert.equal(c.status, 0, c.stderr);
    }
    assert.deepEqual(installed.filter((f) => f.endsWith(".svg")), BRAND_ASSETS, "the brand mod ships the two drawings of the CAO mark and no other");
    for (const f of BRAND_ASSETS) assert.equal(sha(join(dir, f)), sha(join(src, f)), `${f} is installed byte for byte`);
    const layout = w.layout();
    assert.deepEqual(blocksUsing(layout, "cao-window-brand").map((b) => b.id), ["brand"]);
    assert.deepEqual(blocksUsing(layout, "workspace-chrome").map((b) => b.id), ["sidebar-footer"], "only the footer stays on Herdr's mod");
    assert.equal(find(layout, "sidebar-footer").content.props.footer, true);
    assert.deepEqual(w.tmpLeft(), []);
  });
}

test("the brand mod: a second run is up to date, and the toast switch adds the toast mod beside it", () => {
  const w = world();
  assert.equal(w.install(["--local"]).status, 0);
  age(w);
  const before = snapshot(w.caoHome);
  assert.match(w.install(["--local"]).out, /up to date/);
  assert.deepEqual(snapshot(w.caoHome), before, "a file was rewritten although the run said up to date");
  const t = world();
  assert.equal(t.install(["--local"], { TOAST_MOD: "1" }).status, 0);
  for (const m of ["brand", "toast"]) assert.ok(existsSync(join(t.caoHome, "mods", `cao-window-${m}`, "main.js")), m);
});

// A mod folder holds mod.json, the mod's .js files and its own .svg drawings, nothing else (bundle mode leaves mod.json, main.js and the drawings). A sync tool
// that works in the folder leaves temp files there (Unison: `.unison.main.js.<hash>.unison.tmp`), and an editor leaves notes.
for (const mode of ["copy", "bundle"]) {
  test(`${mode} mode: a stray temp file or note in a mod folder is neither copied nor listed in the manifest`, () => {
    const w = world();
    const tree = join(w.root, "example");
    cpSync(here, tree, { recursive: true });
    const strays = [
      join("mods", "cao-window-side", ".unison.main.js.1a2b3c.unison.tmp"),
      join("mods", "cao-window-panel", "notes.txt"),
      join("mods", "cao-window-tabs", "old", "main.js.orig"),
      join("mods", "cao-window-status", "main.js.bak"),
      join("mods", "cao-window-side", ".unison.pin-filled.svg.4d5e6f.unison.tmp"),   // a sync tool's temp file of a drawing
      join("mods", "cao-window-side", "pin-filled.svg.tmp"),
      join("mods", "cao-window-side", "notes.txt"),
      join("mods", "cao-window-tabs", "scratch.tmp"),
    ];
    for (const rel of strays) { mkdirSync(dirname(join(tree, rel)), { recursive: true }); writeFileSync(join(tree, rel), "stray\n"); }
    const r = w.run(join(tree, "install.sh"), ["--local", "--no-app-copy"], mode === "bundle" ? { MOD_IMPORTS: "bundle" } : {});
    assert.equal(r.status, 0, r.text);
    const installed = files(w.caoHome);
    for (const rel of strays) {
      const name = rel.split("/").at(-1);
      assert.ok(!installed.some((f) => f.endsWith(name)), `${rel} was copied: ${installed.filter((f) => f.endsWith(name))}`);
    }
    const listed = w.json("cao-window-install.json").files.map((f) => f.path);
    assert.deepEqual(listed.filter((f) => /unison|notes\.txt|\.orig|\.bak|\.tmp$/.test(f)), [], "a stray file is in the manifest");
    // Every file of the mod folders that belongs there is still installed.
    for (const m of ["side", "panel", "tabs", "status"]) {
      const dir = join(w.caoHome, "mods", `cao-window-${m}`);
      assert.ok(existsSync(join(dir, "mod.json")) && existsSync(join(dir, "main.js")), `${m}: mod.json and main.js`);
      const drawings = drawingsOf(m);
      assert.deepEqual(files(dir).filter((f) => !f.endsWith(".js") && f !== "mod.json"), [...drawings].sort(), `${m}: only mod.json, .js files and the mod's own drawings`);
    }
    if (mode === "copy") for (const f of ["dialog.js", "sidebar.js", "templates.js"]) assert.ok(existsSync(join(w.caoHome, "mods", "cao-window-panel", f)), `panel/${f}`);
    // The same install from a tree with no strays writes the same files with the same hashes.
    const clean = world();
    assert.equal(clean.install(["--local"], mode === "bundle" ? { MOD_IMPORTS: "bundle" } : {}).status, 0);
    assert.deepEqual(installed, files(clean.caoHome), "the stray folders and files changed what was installed");
  });
}

test("the manifest records the options and the SHA-256 of every file the installer wrote", () => {
  const w = world();
  const r = w.install(["--cao-ssh-host", "build-box", "--machine", "bx7", "--core-session", "mywin", "--cao-port", "9123", "--launcher-name", "cao-open"]);
  assert.equal(r.status, 0, r.text);
  const text = readFileSync(join(w.caoHome, "cao-window-install.json"), "utf8");
  JSON.parse(text);
  for (const option of ["build-box", "bx7", "mywin", "9123", "cao-open"]) assert.ok(text.includes(option), `the manifest does not record ${option}`);
  for (const f of files(w.caoHome).filter((f) => f !== "cao-window-install.json")) {
    assert.ok(text.includes(sha(join(w.caoHome, f))), `no hash recorded for ${f}`);
  }
  assert.ok(text.includes(sha(join(w.home, ".local", "bin", "cao-open"))), "the launcher is not in the manifest");
});

test("the installer writes only the window's home and the launcher", () => {
  const w = world();
  assert.equal(w.install(["--local"]).status, 0);
  const all = walk(w.home);
  const stray = all.filter((f) => !f.startsWith(join(".config", "herdr-app-cao") + "/") && f !== join(".local", "bin", "herdr-cao"));
  assert.deepEqual(stray, []);
  assert.deepEqual(w.tmpLeft(), [], "the installer left its scratch folder behind");
  assert.ok(!all.some((f) => /\.(new|tmp)\b/.test(f)), "a temp file was left behind");
});

test("--local adds the machine through sh -c", () => {
  const w = world();
  assert.equal(w.install(["--local"]).status, 0);
  assert.ok(w.log().includes('HC_SESSION=caowin hc machine add --via "sh -c" --name cao --session cao'), w.log().join("\n"));
});

test("options reach the layout props and the machine entry: port, machine, cao core session, window core", () => {
  const w = world();
  const r = w.install(["--cao-ssh-host", "build-box", "--cao-port", "9999", "--machine", "box", "--cao-core-session", "corex", "--core-session", "mywin", "--prop", 'talkableProfiles=["coordinator"]']);
  assert.equal(r.status, 0, r.text);
  const props = find(w.layout(), "cao-sessions").content.props;
  assert.equal(props.caoPort, 9999); assert.equal(props.machine, "box");
  assert.deepEqual(props.talkableProfiles, ["coordinator"]);
  assert.ok(w.log().includes("HC_SESSION=mywin hc machine add build-box --session corex --name box"), w.log().join("\n"));
  assert.ok(!w.log().some((l) => l.startsWith("HC_SESSION=caowin ")));
  assert.equal(w.json("cao-window-install.json").core, "mywin");
});

test("missing or bad options exit 2 and write nothing", () => {
  const cases = [
    [[], "neither --local nor --cao-ssh-host"],
    [["--local", "--cao-ssh-host", "build-box"], "both --local and --cao-ssh-host"],
    [["--local", "--cao-port", "x"], "port not a number"], [["--local", "--cao-port", ""], "empty port"],
    [["--local", "--cao-port", "-1"], "negative port"], [["--local", "--cao-port", "98.5"], "fractional port"],
    [["--local", "--telemetry", "maybe"], "bad telemetry"],
    [["--local", "--bogus"], "unknown option"], [["--local", "--home"], "option without a value"],
    [["--cao-ssh-host"], "ssh host without a value"],
  ];
  for (const [args, why] of cases) {
    const w = world();
    const r = w.install(args);
    assert.equal(r.status, 2, `${why}: ${r.text}`);
    nothingWritten(w);
    assert.deepEqual(w.tmpLeft(), [], why);
  }
});

test("--help lists every option and both switches, exits 0 and changes nothing", () => {
  const w = world();
  for (const flag of ["--help", "-h"]) {
    const h = w.install([flag]);
    assert.equal(h.status, 0);
    assert.match(h.out, /TOAST_MOD=1/);
    assert.match(h.out, /MOD_IMPORTS=bundle/);
    for (const opt of ["--home", "--core-session", "--machine", "--cao-ssh-host", "--local", "--cao-core-session", "--cao-port",
      "--bin-dir", "--launcher-name", "--app", "--app-copy-dir", "--no-app-copy", "--main-home", "--telemetry", "--prop", "--force"]) {
      assert.ok(h.out.includes(opt), `--help does not list ${opt}`);
    }
  }
  nothingWritten(w);
  assert.deepEqual(w.log().filter((l) => !l.startsWith("uname")), [], "--help ran a tool");
});

// ---------------------------------------------------------------------------------------------
// --prop KEY=JSON
// ---------------------------------------------------------------------------------------------

test("Extra layout prop: every CAO block of the derived layout carries it", () => {
  const w = world();
  assert.equal(w.install(["--local", "--prop", 'talkableProfiles=["coordinator"]']).status, 0);
  for (const id of CAO_BLOCKS) assert.deepEqual(find(w.layout(), id).content.props.talkableProfiles, ["coordinator"], id);
});

test("--prop takes any JSON value: string, number, boolean, null, array, object; repeated and last one wins", () => {
  const w = world();
  const r = w.install(["--local", "--prop", 's="a b"', "--prop", "n=5", "--prop", "b=false", "--prop", "z=null", "--prop", "a=[1,[2]]",
    "--prop", 'o={"k":{"d":[]}}', "--prop", "dup=1", "--prop", "dup=2", "--prop", 'glob=["*","[a]","$HOME"]']);
  assert.equal(r.status, 0, r.text);
  for (const id of CAO_BLOCKS) {
    const p = find(w.layout(), id).content.props;
    assert.deepEqual([p.s, p.n, p.b, p.z, p.a, p.o, p.dup, p.glob], ["a b", 5, false, null, [1, [2]], { k: { d: [] } }, 2, ["*", "[a]", "$HOME"]], id);
  }
});

test("--prop with a value that is not JSON exits 2 and writes nothing", () => {
  for (const bad of ["bad", "k=", "k=[1", "k={a:1}", "k=undefined", "k='x'", "k=1 2", "=1"]) {
    const w = world();
    const r = w.install(["--local", "--prop", bad]);
    assert.equal(r.status, 2, `${bad}: ${r.text}`);
    nothingWritten(w);
  }
});

test("--prop stamps the same props on every run, so a second run with the same --prop is up to date", () => {
  const w = world();
  w.install(["--local", "--prop", 'talkableProfiles=["a"]']);
  age(w);
  const before = snapshot(w.caoHome);
  const launcherBefore = [statSync(w.launcher).mtimeMs, sha(w.launcher)];
  assert.match(w.install(["--local", "--prop", 'talkableProfiles=["a"]']).out, /up to date/);
  assert.deepEqual(snapshot(w.caoHome), before, "a file was rewritten although the run said up to date");
  assert.deepEqual([statSync(w.launcher).mtimeMs, sha(w.launcher)], launcherBefore);
  assert.ok(!files(w.caoHome).some((f) => f.includes(".bak-")));
  assert.equal(w.install(["--local", "--prop", 'talkableProfiles=["b"]']).status, 0);
  assert.deepEqual(find(w.layout(), "cao-agents").content.props.talkableProfiles, ["b"]);
});

// ---------------------------------------------------------------------------------------------
// Refusals: core session names and the main home
// ---------------------------------------------------------------------------------------------

for (const core of ["cao", "default"]) {
  test(`a core session named ${core} is refused and nothing is written`, () => {
    const w = world();
    const r = w.install(["--local", "--core-session", core]);
    assert.equal(r.status, 2);
    assert.match(r.err, new RegExp(core));
    nothingWritten(w);
  });
}

test("an empty core session name is refused (it would be the default core)", () => {
  const w = world();
  const r = w.install(["--local", "--core-session", ""]);
  assert.notEqual(r.status, 0);
  nothingWritten(w);
});

test("odd characters in session and machine names are refused or passed through as one inert argument", () => {
  const marker = (w) => join(w.root, "PWNED");
  const names = [(w) => `a b`, (w) => `x;touch ${marker(w)}`, (w) => `it's`, (w) => `$(touch ${marker(w)})`, (w) => "../../escape", (w) => "-x", (w) => `a"b\\c`, (w) => "ü-é"];
  for (const mk of names) {
    for (const flag of ["--core-session", "--machine", "--cao-core-session"]) {
      const w = world();
      const name = mk(w);
      const r = w.install(["--local", flag, name]);
      assert.equal(existsSync(marker(w)), false, `${flag} ${JSON.stringify(name)} was run as shell`);
      if (r.status !== 0) { nothingWritten(w); continue; }
      assert.ok(!files(w.root).some((f) => f.includes("escape")), "a path escaped the window's home");
      const lines = w.log().filter((l) => l.includes(" machine add"));
      assert.equal(lines.length, 1);
      const shown = name.includes(" ") ? `"${name}"` : name;       // the stub quotes an argument that holds a space
      const expected = { "--core-session": `HC_SESSION=${name} hc machine add`, "--machine": `--name ${shown}`, "--cao-core-session": `--session ${shown}` }[flag];
      assert.ok(lines[0].includes(expected), `${flag}: the name changed on its way to hc: ${lines[0]}`);
      if (flag === "--core-session") {
        const t = launcherArgv(w, w.launcher);
        assert.equal(t.argv.at(-1), `HC_SESSION=${name}`);
      }
      if (flag === "--machine") assert.equal(find(w.layout(), "cao-sessions").content.props.machine, name);
    }
  }
});

test("an ssh host that looks like an option or carries shell syntax is refused or passed as one argument", () => {
  const w = world();
  const r = w.install(["--cao-ssh-host", "-oProxyCommand=touch /nonexistent"]);
  assert.notEqual(r.status, 0, "an option-shaped host would be read by hc as an option");
  assert.deepEqual(w.adds(), []);
  const marker = join(w.root, "PWNED");
  const w2 = world();
  const r2 = w2.install(["--cao-ssh-host", `host;touch ${marker}`]);
  assert.equal(existsSync(marker), false);
  if (r2.status === 0) assert.ok(w2.adds()[0].includes(`"host;touch ${marker}"`) || w2.adds()[0].includes(`host;touch ${marker}`));
});

test("the main home is protected, and so is $HERDR_APP_HOME", () => {
  const w = world();
  withMainHome(w);
  const before = snapshot(w.mainHome);
  assert.equal(w.install(["--local", "--home", w.mainHome]).status, 2);
  assert.equal(w.install(["--local", "--home", `${w.mainHome}/`]).status, 2);
  const other = join(w.root, "other-home");
  assert.equal(w.install(["--local", "--home", other], { HERDR_APP_HOME: other }).status, 2);
  assert.equal(w.install(["--local", "--home", `${other}/`], { HERDR_APP_HOME: other }).status, 2);
  assert.deepEqual(snapshot(w.mainHome), before);
  assert.ok(!existsSync(other));
  assert.deepEqual(w.adds(), []);
});

test("the main home is protected however its path is written: .., a relative path, a symlink", () => {
  const w = world();
  withMainHome(w);
  const before = snapshot(w.mainHome);
  symlinkSync(w.mainHome, join(w.root, "link-to-main"));
  symlinkSync(w.xdg, join(w.root, "link-to-xdg"));
  mkdirSync(join(w.xdg, "x"));
  const spellings = [
    [`${w.xdg}/x/../herdr-app`, w.root],
    ["herdr-app", w.xdg],
    [`./herdr-app/`, w.xdg],
    [join(w.root, "link-to-main"), w.root],
    [join(w.root, "link-to-xdg", "herdr-app"), w.root],
  ];
  for (const [home, cwd] of spellings) {
    const r = w.install(["--local", "--home", home], {}, cwd);
    assert.equal(r.status, 2, `--home ${home} (cwd ${cwd}) was accepted: ${r.text}`);
  }
  assert.deepEqual(snapshot(w.mainHome), before, "the main home was written through");
  assert.deepEqual(w.adds(), []);
});

test("--main-home moves the protected path, and a $HERDR_APP_HOME elsewhere does not block the install", () => {
  const w = world();
  const customMain = join(w.root, "my-main");
  assert.equal(w.install(["--local", "--main-home", customMain, "--home", customMain]).status, 2);
  const ok = w.install(["--local"], { HERDR_APP_HOME: join(w.root, "somewhere-else") });
  assert.equal(ok.status, 0, ok.text);
});

// ---------------------------------------------------------------------------------------------
// Idempotence, backups, customized layouts
// ---------------------------------------------------------------------------------------------

test("a second run changes nothing and says so", () => {
  const w = world();
  assert.equal(w.install(["--cao-ssh-host", "build-box"]).status, 0);
  age(w);
  const before = snapshot(w.caoHome);
  const launcherBefore = [statSync(w.launcher).mtimeMs, sha(w.launcher)];
  const adds = w.adds().length;
  const r = w.install(["--cao-ssh-host", "build-box"]);
  assert.equal(r.status, 0, r.text);
  assert.match(r.out, /up to date/);
  assert.deepEqual(snapshot(w.caoHome), before, "a file was rewritten");
  assert.deepEqual([statSync(w.launcher).mtimeMs, sha(w.launcher)], launcherBefore, "the launcher was rewritten");
  assert.ok(!files(w.caoHome).some((f) => f.includes(".bak-")));
  assert.deepEqual(files(dirname(w.launcher)), ["herdr-cao"]);
  assert.equal(w.adds().length, adds);
  assert.match(r.out, /toast mod off, mod imports: copy/, "the summary line is also printed on an up-to-date run");
});

test("a second run keeps the keys other tools wrote to shared.json", () => {
  const w = world();
  w.install(["--local"]);
  const shared = w.json("shared.json");
  writeFileSync(join(w.caoHome, "shared.json"), JSON.stringify({ ...shared, "caowin.pins": ["t1"], other: { a: 1 } }));
  assert.equal(w.install(["--local"]).status, 0);
  assert.deepEqual([w.json("shared.json")["caowin.pins"], w.json("shared.json").other, w.json("shared.json").layout], [["t1"], { a: 1 }, "cao-window"]);
});

test("a machine that already exists in the window's core session is not added again", () => {
  for (const machines of ['[{"name":"cao"}]', '{"machines":[{"name":"cao","session":"cao"}]}', '[{"id":1,"name":"cao"},{"name":"other"}]']) {
    const w = world({ machines });
    const r = w.install(["--local"]);
    assert.equal(r.status, 0, r.text);
    assert.deepEqual(w.adds(), [], machines);
  }
  const w = world({ machines: '[{"name":"other"}]' });
  w.install(["--local"]);
  assert.equal(w.adds().length, 1, "a different machine name must not count as present");
});

test("a failed machine add exits non-zero, and a rerun adds it and finishes", () => {
  const w = world();
  writeFileSync(join(w.stubs, "add.fail"), "");
  const first = w.install(["--local"]);
  assert.notEqual(first.status, 0);
  assert.doesNotMatch(first.out, /install: done/);
  rmSync(join(w.stubs, "add.fail"));
  const again = w.install(["--local"]);
  assert.equal(again.status, 0, again.text);
  assert.equal(w.adds().length, 2);
  assert.deepEqual(w.tmpLeft(), []);
});

test("Customized layout: refused with exit 2 and nothing written; --force keeps a backup and replaces it", () => {
  const w = world();
  w.install(["--local"]);
  const path = join(w.caoHome, "layouts", "cao-window.json");
  const original = readFileSync(path, "utf8");
  writeFileSync(path, `${original}\n`);
  age(w);
  const before = snapshot(w.caoHome);
  const r = w.install(["--local"]);
  assert.equal(r.status, 2);
  assert.match(r.err, /cao-window\.json/);
  assert.deepEqual(snapshot(w.caoHome), before);
  assert.deepEqual(w.tmpLeft(), []);
  const before2 = Math.floor(Date.now() / 1000);
  assert.equal(w.install(["--local", "--force"]).status, 0);
  const found = baks(join(w.caoHome, "layouts"), "cao-window.json");
  assert.equal(found.length, 1);
  assert.equal(readFileSync(join(w.caoHome, "layouts", found[0]), "utf8"), `${original}\n`);
  assert.equal(readFileSync(path, "utf8"), original);
  const epoch = Number(/bak-(\d+)$/.exec(found[0])[1]);
  assert.ok(epoch >= before2 - 1 && epoch <= nowEpoch() + 1, `the backup suffix ${epoch} is not the time of the run`);
});

test("a customized layout blocks the whole run: edited mod files are not replaced either", () => {
  const w = world();
  w.install(["--local"]);
  writeFileSync(join(w.caoHome, "layouts", "cao-window.json"), "{}\n");
  const mod = join(w.caoHome, "mods", "cao-window-side", "main.js");
  writeFileSync(mod, "// edited\n");
  age(w);
  const before = snapshot(w.caoHome);
  assert.equal(w.install(["--local"]).status, 2);
  assert.deepEqual(snapshot(w.caoHome), before);
});

test("a layout with no manifest to compare to counts as customized", () => {
  const w = world();
  w.install(["--local"]);
  writeFileSync(join(w.caoHome, "layouts", "cao-window.json"), "{}\n");
  rmSync(join(w.caoHome, "cao-window-install.json"));
  assert.equal(w.install(["--local"]).status, 2);
  assert.equal(readFileSync(join(w.caoHome, "layouts", "cao-window.json"), "utf8"), "{}\n");
});

test("a layout that still matches the manifest is re-derived without --force (a Herdr upgrade)", () => {
  const w = world();
  w.install(["--local"]);
  assert.equal(w.install(["--local", "--cao-port", "9999"]).status, 0);
  assert.ok(readFileSync(join(w.caoHome, "layouts", "cao-window.json"), "utf8").includes("9999"));
  assert.equal(baks(join(w.caoHome, "layouts"), "cao-window.json").length, 1, "the replaced layout keeps a backup");
});

test("Layout of an earlier derivation: a home whose three-block layout matches the manifest is re-derived with a backup, and the next run is up to date", () => {
  const w = world();
  assert.equal(w.install(["--local"]).status, 0);
  const path = installEarlierLayout(w);
  const earlier = readFileSync(path, "utf8");
  const r = w.install(["--local"]);
  assert.equal(r.status, 0, r.text);
  assert.doesNotMatch(r.out, /up to date/);
  assertTwoBlockLayout(w.layout());
  const found = baks(join(w.caoHome, "layouts"), "cao-window.json");
  assert.equal(found.length, 1, "the replaced layout keeps one backup");
  assert.equal(readFileSync(join(w.caoHome, "layouts", found[0]), "utf8"), earlier);
  age(w);
  const before = snapshot(w.caoHome);
  const again = w.install(["--local"]);
  assert.match(again.out, /up to date/);
  assert.deepEqual(snapshot(w.caoHome), before, "a file was rewritten although the run said up to date");
});

test("Layout of an earlier derivation: an edited three-block layout is refused with exit 2, and --force keeps it as a backup", () => {
  const w = world();
  w.install(["--local"]);
  const path = installEarlierLayout(w, { recorded: false });
  const edited = `${readFileSync(path, "utf8")}\n`;
  writeFileSync(path, edited);
  age(w);
  const before = snapshot(w.caoHome);
  const r = w.install(["--local"]);
  assert.equal(r.status, 2, r.text);
  assert.match(r.err, /cao-window\.json/);
  assert.deepEqual(snapshot(w.caoHome), before, "a file changed in a refused run");
  assert.equal(w.install(["--local", "--force"]).status, 0);
  const found = baks(join(w.caoHome, "layouts"), "cao-window.json");
  assert.equal(found.length, 1);
  assert.equal(readFileSync(join(w.caoHome, "layouts", found[0]), "utf8"), edited);
  assertTwoBlockLayout(w.layout());
});

test("Layout of an earlier derivation: a main home that holds the earlier output as its layout is re-derived, not extended", () => {
  const w = world();
  withMainHome(w, readFileSync(EARLIER_LAYOUT, "utf8"));
  assert.equal(w.install(["--local"]).status, 0);
  assertTwoBlockLayout(w.layout());
  assert.equal(idsOf(w.layout()).filter((i) => i === "status").length, 1);
});

test("Uninstall after an install over the earlier layout moves everything the installer wrote, backups included, and leaves nothing behind", () => {
  const w = world();
  w.install(["--local"]);
  installEarlierLayout(w);
  assert.equal(w.install(["--local"]).status, 0);
  const written = files(w.caoHome);
  assert.ok(written.some((f) => f.includes(".bak-")), "the install left no backup to move");
  const r = w.uninstall([]);
  assert.equal(r.status, 0, r.text);
  const left = walk(w.home);
  const removed = readdirSync(join(w.home, ".config")).filter((n) => n.startsWith("herdr-app-cao.removed-"));
  assert.equal(removed.length, 1, left.join("\n"));
  assert.deepEqual(left.filter((f) => !f.startsWith(join(".config", removed[0]) + "/")), [], "a file of the install is outside the .removed folder");
  assert.deepEqual(files(join(w.home, ".config", removed[0])), written, "the moved home differs from what the installer wrote");
  assert.ok(!existsSync(w.launcher), "the launcher is still there");
});

test("the brand mod: an install over the earlier layout and without the brand folder brings the folder and re-points the brand block, with a backup of the old layout", () => {
  const w = world();
  assert.equal(w.install(["--local"]).status, 0);
  rmSync(join(w.caoHome, BRAND_MOD), { recursive: true });            // a home an install of before the brand mod left
  const path = installEarlierLayout(w);
  const earlier = readFileSync(path, "utf8");
  assert.deepEqual(blocksUsing(w.layout(), "cao-window-brand"), [], "fixture: the earlier layout draws Herdr's brand row");
  const r = w.install(["--local"]);
  assert.equal(r.status, 0, r.text);
  assert.doesNotMatch(r.out, /up to date/);
  assert.ok(existsSync(join(w.caoHome, BRAND_MOD, "main.js")) && existsSync(join(w.caoHome, BRAND_MOD, "mod.json")), "the brand mod folder is back");
  assert.deepEqual(blocksUsing(w.layout(), "cao-window-brand").map((b) => b.id), ["brand"]);
  assert.deepEqual(blocksUsing(w.layout(), "workspace-chrome").map((b) => b.id), ["sidebar-footer"]);
  assertTwoBlockLayout(w.layout());
  const found = baks(join(w.caoHome, "layouts"), "cao-window.json");
  assert.equal(found.length, 1, "the replaced layout keeps one backup");
  assert.equal(readFileSync(join(w.caoHome, "layouts", found[0]), "utf8"), earlier);
  age(w);
  const before = snapshot(w.caoHome);
  assert.match(w.install(["--local"]).out, /up to date/);
  assert.deepEqual(snapshot(w.caoHome), before);
});

test("the brand mod: uninstall moves its folder with the rest of the home", () => {
  const w = world();
  assert.equal(w.install(["--local"]).status, 0);
  const written = files(join(w.caoHome, BRAND_MOD));
  assert.ok(written.includes("main.js") && written.includes("mod.json"));
  assert.equal(w.uninstall([]).status, 0);
  assert.equal(existsSync(w.caoHome), false);
  const kept = join(w.xdg, readdirSync(w.xdg).find((n) => /^herdr-app-cao\.removed-\d+$/.test(n)));
  assert.deepEqual(files(join(kept, BRAND_MOD)), written, "the brand mod moved with the home");
});

test("Locally edited mod file: kept as .bak-<epoch> and replaced, and only that file", () => {
  const w = world();
  w.install(["--local"]);
  const path = join(w.caoHome, "mods", "cao-window-side", "logic.js");
  const original = readFileSync(path, "utf8");
  writeFileSync(path, `${original}// edited\n`);
  assert.equal(w.install(["--local"]).status, 0);
  const found = baks(join(w.caoHome, "mods", "cao-window-side"), "logic.js");
  assert.equal(found.length, 1);
  assert.equal(readFileSync(join(w.caoHome, "mods", "cao-window-side", found[0]), "utf8"), `${original}// edited\n`);
  assert.equal(readFileSync(path, "utf8"), original);
  assert.equal(files(w.caoHome).filter((f) => f.includes(".bak-")).length, 1, "an untouched file got a backup");
});

test("every kind of shipped file is backed up when edited: mod.json, main.js, the manifest", () => {
  const w = world();
  w.install(["--local"]);
  const edits = [join("mods", "cao-window-tabs", "mod.json"), join("mods", "cao-window-status", "main.js"), "cao-window-install.json"];
  const originals = Object.fromEntries(edits.map((f) => [f, readFileSync(join(w.caoHome, f), "utf8")]));
  for (const f of edits) writeFileSync(join(w.caoHome, f), `${originals[f]} `);
  assert.equal(w.install(["--local"]).status, 0);
  for (const f of edits) {
    assert.equal(readFileSync(join(w.caoHome, f), "utf8"), originals[f], f);
    assert.equal(baks(dirname(join(w.caoHome, f)), f.split("/").at(-1)).length, 1, f);
  }
});

test("a changed shared.json and a changed settings.json (the --telemetry path) are backed up, with the text they had", () => {
  const w = world();
  mkdirSync(w.mainHome, { recursive: true });
  writeFileSync(join(w.mainHome, "settings.json"), '{"telemetry": true}\n');
  w.install(["--local"]);
  const editedShared = JSON.stringify({ ...w.json("shared.json"), layout: "main", mine: 1 });
  const editedSettings = '{"telemetry": true, "mine": 2}\n';
  writeFileSync(join(w.caoHome, "shared.json"), editedShared);
  writeFileSync(join(w.caoHome, "settings.json"), editedSettings);
  const r = w.install(["--local", "--telemetry", "off"]);
  assert.equal(r.status, 0, r.text);
  const sharedBaks = baks(w.caoHome, "shared.json");
  assert.equal(sharedBaks.length, 1, "no backup of the edited shared.json");
  assert.equal(readFileSync(join(w.caoHome, sharedBaks[0]), "utf8"), editedShared);
  assert.deepEqual([w.json("shared.json").layout, w.json("shared.json").mine], ["cao-window", 1]);
  const settingsBaks = baks(w.caoHome, "settings.json");
  assert.equal(settingsBaks.length, 1, "no backup of the settings.json that --telemetry replaced");
  assert.equal(readFileSync(join(w.caoHome, settingsBaks[0]), "utf8"), editedSettings);
  assert.deepEqual(w.json("settings.json"), { telemetry: false, mine: 2 });
});

test("a deleted shipped file is written again and needs no backup", () => {
  const w = world();
  w.install(["--local"]);
  rmSync(join(w.caoHome, "mods", "cao-window-panel", "cao.js"));
  const r = w.install(["--local"]);
  assert.equal(r.status, 0, r.text);
  assert.ok(existsSync(join(w.caoHome, "mods", "cao-window-panel", "cao.js")));
  assert.ok(!files(w.caoHome).some((f) => f.includes(".bak-")));
  assert.doesNotMatch(r.out, /up to date/);
});

test("a launcher that already exists with other content is kept as .bak-<epoch> and replaced", () => {
  const w = world();
  mkdirSync(dirname(w.launcher), { recursive: true });
  writeFileSync(w.launcher, "#!/bin/sh\necho mine\n");
  assert.equal(w.install(["--local"]).status, 0);
  const found = files(dirname(w.launcher)).filter((f) => /^herdr-cao\.bak-\d+$/.test(f));
  assert.equal(found.length, 1);
  assert.equal(readFileSync(join(dirname(w.launcher), found[0]), "utf8"), "#!/bin/sh\necho mine\n");
  assert.match(readFileSync(w.launcher, "utf8"), /open -n -a/);
});

test("no network client is called", () => {
  const w = world();
  assert.equal(w.install(["--local"]).status, 0);
  assert.equal(w.install(["--local"]).status, 0);
  assert.equal(w.install(["--local", "--force", "--telemetry", "off"]).status, 0);
  w.uninstall([]);
  assert.equal(w.net(), "");
});

test("an installer started inside a Herdr terminal does not leak that terminal's core into hc", () => {
  const w = world();
  assert.equal(w.install(["--local"], { HC_SOCKET: "/evil.sock", HC_TERMINAL_ID: "t9", HC_SCOPE: "x", HC_TERMINAL_SOCKET: "/e2", HC_SESSION: "default" }).status, 0);
  assert.ok(!w.log().some((l) => l.startsWith("LEAK")), w.log().join("\n"));
  assert.ok(!w.log().filter((l) => !l.endsWith(" --version")).some((l) => /^HC_SESSION=(default|cao) /.test(l)), "hc ran against a core other than the window's");
});

test("the installer runs from any working directory, by relative or absolute path", () => {
  const w = world();
  const r = spawnSync("sh", ["install.sh", "--local", "--no-app-copy"], { env: w.env, encoding: "utf8", cwd: here });
  assert.equal(r.status, 0, r.stdout + r.stderr);
  assert.ok(existsSync(join(w.caoHome, "layouts", "cao-window.json")));
  const w2 = world();
  assert.equal(w2.install(["--local"], {}, w2.home).status, 0);
});

// ---------------------------------------------------------------------------------------------
// Version and platform checks
// ---------------------------------------------------------------------------------------------

for (const [found, ok] of [["0.1.19", false], ["0.1.20", false], ["0.1.9", false], ["0.0.99", false], ["0.1.21", true], ["0.1.22", true],
  ["0.1.100", true], ["0.2.0", true], ["0.10.0", true], ["1.0.0", true]]) {
  test(`hc ${found} is ${ok ? "accepted" : "refused with exit 3 naming both versions"}`, () => {
    const w = world({ hc: found });
    const r = w.install(["--local"]);
    if (ok) return assert.equal(r.status, 0, r.text);
    assert.equal(r.status, 3);
    assert.ok(r.err.includes(found) && r.err.includes("0.1.21"), r.err);
    nothingWritten(w);
    assert.deepEqual(w.tmpLeft(), []);
  });
}

test("a hc version line with extra words is still read", () => {
  const w = world({ hc: "0.1.21 (build 4f3a2b1, linux)" });
  assert.equal(w.install(["--local"]).status, 0);
});

test("an unreadable hc version, or no hc at all, stops the install before anything is written", () => {
  const garbage = world({ hc: "garbage" });
  assert.notEqual(garbage.install(["--local"]).status, 0);
  nothingWritten(garbage);
  const none = world();
  rmSync(join(none.stubs, "hc"));
  const r = none.install(["--local"], { PATH: `${none.stubs}:${JQ_DIR}:/usr/bin:/bin` });
  assert.notEqual(r.status, 0);
  assert.match(r.err, /hc/);
  nothingWritten(none);
});

for (const [found, ok] of [["0.1.15", false], ["0.1.9", false], ["0.0.99", false], ["0.1.16", true], ["0.1.17", true], ["0.1.100", true], ["0.2.0", true], ["0.10.0", true]]) {
  test(`Herdr app ${found} is ${ok ? "accepted" : "refused with exit 3 naming both versions"}`, () => {
    const w = world({ app: found });
    const r = w.install(["--local"]);
    if (ok) return assert.equal(r.status, 0, r.text);
    assert.equal(r.status, 3);
    assert.ok(r.err.includes(found) && r.err.includes("0.1.16"), r.err);
    nothingWritten(w);
  });
}

test("an app version that cannot be read stops the install before anything is written", () => {
  const w = world({ app: "" });
  assert.notEqual(w.install(["--local"]).status, 0);
  nothingWritten(w);
});

test("Old herdr-core: a refusal names 0.1.19 and 0.1.21", () => {
  const w = world({ hc: "0.1.19" });
  const r = w.install(["--local"]);
  assert.equal(r.status, 3);
  assert.match(r.err, /0\.1\.19/);
  assert.match(r.err, /0\.1\.21/);
});

test("a platform with no supported app exits 4, points at the README topologies and writes nothing", () => {
  for (const os of ["FreeBSD", "SunOS", "Windows_NT"]) {
    const w = world({ os });
    const r = w.install(["--local"]);
    assert.equal(r.status, 4, os);
    assert.match(r.err, /README/);
    assert.match(r.err, /topolog/i);
    nothingWritten(w);
  }
});

test("Linux exits 4 until spike 1.1 enables it", () => {
  const enabled = /^LINUX_SUPPORTED=1/m.test(readFileSync(join(here, "bin", "launcher-fill.sh"), "utf8"));
  const w = world({ os: "Linux" });
  const r = w.install(["--local", "--app", "/opt/herdr/herdr-app-gui"]);
  if (enabled) return assert.equal(r.status, 0, r.text);
  assert.equal(r.status, 4, r.text);
  nothingWritten(w);
});

// ---------------------------------------------------------------------------------------------
// Base layout: lookup order and drift
// ---------------------------------------------------------------------------------------------

test("a base layout that changed shape exits 5 and writes nothing", () => {
  const w = world();
  const broken = JSON.parse(readFileSync(MAIN_LAYOUT, "utf8"));
  const strip = (n) => ({ ...n, children: (n.children ?? []).filter((c) => c.id !== "sidebar-lists").map(strip) });
  withMainHome(w, JSON.stringify(strip(broken)));
  const r = w.install(["--local"]);
  assert.equal(r.status, 5);
  assert.match(r.err, /sidebar-lists/);
  nothingWritten(w);
  assert.deepEqual(w.tmpLeft(), []);
});

for (const anchor of ["sidebar-lists", "work", "work-body", "status"]) {
  test(`Shipped main changed shape: without "${anchor}" the installer exits 5 naming it`, () => {
    const w = world();
    const strip = (n) => ({ ...n, children: (n.children ?? []).filter((c) => c.id !== anchor).map(strip) });
    withMainHome(w, JSON.stringify(strip(JSON.parse(readFileSync(MAIN_LAYOUT, "utf8")))));
    const r = w.install(["--local"]);
    assert.equal(r.status, 5, r.text);
    assert.ok(r.err.includes(anchor), r.err);
    nothingWritten(w);
  });
}

test("drift after a good install changes no file of the home", () => {
  const w = world();
  w.install(["--local"]);
  age(w);
  const before = snapshot(w.caoHome);
  withMainHome(w, JSON.stringify({ id: "root", type: "row", children: [] }));
  assert.equal(w.install(["--local"]).status, 5);
  assert.deepEqual(snapshot(w.caoHome), before);
});

test("a main layout that is not an object, or not JSON, stops the install and writes nothing", () => {
  for (const text of ["[]", "null", "42", '"x"']) {
    const w = world();
    withMainHome(w, text);
    assert.equal(w.install(["--local"]).status, 5, text);
    nothingWritten(w);
  }
  const w = world();
  withMainHome(w, "{not json");
  assert.notEqual(w.install(["--local"]).status, 0);
  nothingWritten(w);
});

test("the main home's layout is the base when it exists; otherwise the shipped copy, with a warning", () => {
  const w = world();
  const custom = JSON.parse(readFileSync(MAIN_LAYOUT, "utf8"));
  custom.children[1].children.push({ id: "my-extra", type: "block", content: { mod: "mine" } });
  withMainHome(w, JSON.stringify(custom));
  const first = w.install(["--local"]);
  assert.equal(first.status, 0);
  assert.ok(JSON.stringify(w.layout()).includes("my-extra"));
  assert.doesNotMatch(first.err, /may lag/);
  const w2 = world();
  const r = w2.install(["--local"]);
  assert.equal(r.status, 0);
  assert.match(r.err, /may lag/);
});

test("--main-home chooses where the base layout is read from, and the main home is never written", () => {
  const w = world();
  const elsewhere = join(w.root, "elsewhere-home");
  const custom = JSON.parse(readFileSync(MAIN_LAYOUT, "utf8"));
  custom.children[1].children.push({ id: "from-elsewhere", type: "block", content: { mod: "mine" } });
  mkdirSync(join(elsewhere, "layouts"), { recursive: true });
  writeFileSync(join(elsewhere, "layouts", "main.json"), JSON.stringify(custom));
  const before = snapshot(elsewhere);
  assert.equal(w.install(["--local", "--main-home", elsewhere]).status, 0);
  assert.ok(JSON.stringify(w.layout()).includes("from-elsewhere"));
  assert.deepEqual(snapshot(elsewhere), before);
});

// ---------------------------------------------------------------------------------------------
// shared.json and settings.json
// ---------------------------------------------------------------------------------------------

test("shared.json is merged: other keys stay, layout and the flat frame.welcomed key are set", () => {
  const w = world();
  mkdirSync(w.caoHome, { recursive: true });
  writeFileSync(join(w.caoHome, "shared.json"), JSON.stringify({ layout: "main", keep: [1, 2], "frame.welcomed": "pending", nested: { a: 1 } }));
  assert.equal(w.install(["--local"]).status, 0);
  assert.deepEqual(w.json("shared.json"), { layout: "cao-window", keep: [1, 2], "frame.welcomed": "finished", nested: { a: 1 } });
});

test("a shared.json that is not a JSON object stops the install and is left as it was", () => {
  for (const text of ["{broken", "[]"]) {
    const w = world();
    mkdirSync(w.caoHome, { recursive: true });
    writeFileSync(join(w.caoHome, "shared.json"), text);
    const r = w.install(["--local"]);
    assert.notEqual(r.status, 0, text);
    assert.deepEqual(files(w.caoHome), ["shared.json"], text);
    assert.equal(readFileSync(join(w.caoHome, "shared.json"), "utf8"), text);
    assert.deepEqual(w.adds(), []);
    assert.ok(!existsSync(w.launcher));
  }
});

test("settings.json is copied from the main home, or the telemetry default is announced", () => {
  const w = world();
  mkdirSync(w.mainHome, { recursive: true });
  writeFileSync(join(w.mainHome, "settings.json"), '{"telemetry": false, "sounds": {"enabled": true}}\n');
  assert.equal(w.install(["--local"]).status, 0);
  assert.deepEqual(w.json("settings.json"), { telemetry: false, sounds: { enabled: true } });
  const w2 = world();
  const r = w2.install(["--local"]);
  assert.match(r.text, /telemetry/i);
  assert.match(r.text, /on by default/i);
  assert.match(r.text, /--telemetry off/);
  assert.ok(!existsSync(join(w2.caoHome, "settings.json")));
});

test("a settings.json the window's home already has is not replaced by the main home's", () => {
  const w = world();
  mkdirSync(w.mainHome, { recursive: true });
  writeFileSync(join(w.mainHome, "settings.json"), '{"telemetry": true}\n');
  w.install(["--local"]);
  writeFileSync(join(w.caoHome, "settings.json"), '{"telemetry": false, "mine": 1}\n');
  assert.equal(w.install(["--local"]).status, 0);
  assert.deepEqual(w.json("settings.json"), { telemetry: false, mine: 1 });
  assert.equal(baks(w.caoHome, "settings.json").length, 0);
  assert.deepEqual(JSON.parse(readFileSync(join(w.mainHome, "settings.json"), "utf8")), { telemetry: true });
});

test("--telemetry writes the explicit value in either case and keeps the other keys", () => {
  const w = world();
  assert.equal(w.install(["--local", "--telemetry", "off"]).status, 0);
  assert.equal(w.json("settings.json").telemetry, false);
  const w2 = world();
  mkdirSync(w2.mainHome, { recursive: true });
  writeFileSync(join(w2.mainHome, "settings.json"), '{"telemetry": true, "sounds": {"enabled": true}}\n');
  assert.equal(w2.install(["--local", "--telemetry", "off"]).status, 0);
  assert.deepEqual(w2.json("settings.json"), { telemetry: false, sounds: { enabled: true } });
  assert.equal(w2.install(["--local", "--telemetry", "on"]).status, 0);
  assert.deepEqual(w2.json("settings.json"), { telemetry: true, sounds: { enabled: true } });
  assert.deepEqual(JSON.parse(readFileSync(join(w2.mainHome, "settings.json"), "utf8")), { telemetry: true, sounds: { enabled: true } }, "the main home's settings changed");
});

test("a main-home settings.json that is not JSON stops a --telemetry install and writes nothing", () => {
  const w = world();
  mkdirSync(w.mainHome, { recursive: true });
  writeFileSync(join(w.mainHome, "settings.json"), "{oops");
  assert.notEqual(w.install(["--local", "--telemetry", "off"]).status, 0);
  nothingWritten(w);
});

// ---- a rerun with the same options changes nothing, also when a settings.json was copied (defect 2) ----

const mainSettings = (w, obj) => {
  mkdirSync(w.mainHome, { recursive: true });
  writeFileSync(join(w.mainHome, "settings.json"), `${JSON.stringify(obj)}\n`);
};
const mainTheme = (w, name, text) => {
  mkdirSync(join(w.mainHome, "themes"), { recursive: true });
  writeFileSync(join(w.mainHome, "themes", `${name}.json`), text);
};
const manifestEntry = (w, path) => w.json("cao-window-install.json").files.find((f) => f.path === path);
const wroteLines = (text) => text.split("\n").filter((l) => /^install: (wrote|kept) /.test(l));

test("a second install with the same options over a main home that has a settings.json says up to date, writes nothing and keeps no .bak", () => {
  const w = world();
  mainSettings(w, { telemetry: false, sounds: { enabled: true } });
  const first = w.install(["--local"]);
  assert.equal(first.status, 0, first.text);
  assert.match(first.out, /install: done/);
  assert.deepEqual(w.json("settings.json"), { telemetry: false, sounds: { enabled: true } });
  assert.equal(manifestEntry(w, "settings.json")?.sha256, sha(join(w.caoHome, "settings.json")), "the manifest does not list the copied settings.json");
  age(w);
  const before = snapshot(w.caoHome);
  const manifestBytes = readFileSync(join(w.caoHome, "cao-window-install.json"), "utf8");
  const launcherBefore = [statSync(w.launcher).mtimeMs, sha(w.launcher)];
  for (const run of ["second", "third"]) {
    const r = w.install(["--local"]);
    assert.equal(r.status, 0, `${run}: ${r.text}`);
    assert.match(r.out, /install: up to date/, `${run} run: ${r.out}`);
    assert.doesNotMatch(r.out, /install: done/, run);
    assert.deepEqual(wroteLines(r.out), [], `${run} run wrote something`);
    assert.deepEqual(snapshot(w.caoHome), before, `${run} run: a file was rewritten`);
    assert.equal(readFileSync(join(w.caoHome, "cao-window-install.json"), "utf8"), manifestBytes, `${run} run: the manifest changed`);
    assert.deepEqual([statSync(w.launcher).mtimeMs, sha(w.launcher)], launcherBefore, `${run} run: the launcher was rewritten`);
    assert.ok(!files(w.caoHome).some((f) => f.includes(".bak-")), `${run} run kept a backup`);
  }
  assert.equal(w.adds().length, 1);
});

test("a rerun with --telemetry off, with the same options, is up to date too", () => {
  const w = world();
  mainSettings(w, { telemetry: true, sounds: { enabled: true } });
  assert.equal(w.install(["--local", "--telemetry", "off"]).status, 0);
  age(w);
  const before = snapshot(w.caoHome);
  const r = w.install(["--local", "--telemetry", "off"]);
  assert.equal(r.status, 0, r.text);
  assert.match(r.out, /install: up to date/);
  assert.deepEqual(snapshot(w.caoHome), before);
  assert.ok(!files(w.caoHome).some((f) => f.includes(".bak-")));
});

test("a settings.json the window's own home holds is kept as it is and stays listed in the manifest with its current hash", () => {
  const w = world();
  mainSettings(w, { telemetry: true });
  assert.equal(w.install(["--local"]).status, 0);
  writeFileSync(join(w.caoHome, "settings.json"), '{"telemetry": false, "mine": 1}\n');
  const r = w.install(["--local"]);
  assert.equal(r.status, 0, r.text);
  assert.deepEqual(w.json("settings.json"), { telemetry: false, mine: 1 });
  assert.equal(readFileSync(join(w.caoHome, "settings.json"), "utf8"), '{"telemetry": false, "mine": 1}\n');
  assert.equal(baks(w.caoHome, "settings.json").length, 0, "the user's settings.json was backed up although it was not replaced");
  assert.equal(manifestEntry(w, "settings.json")?.sha256, sha(join(w.caoHome, "settings.json")));
  age(w);
  const before = snapshot(w.caoHome);
  const again = w.install(["--local"]);
  assert.match(again.out, /install: up to date/);
  assert.deepEqual(snapshot(w.caoHome), before);
});

test("uninstall after two installs with a copied settings.json is unchanged: it moves the home and touches nothing else", () => {
  const w = world();
  mainSettings(w, { telemetry: false });
  w.install(["--local"]);
  w.install(["--local"]);
  const mainBefore = snapshot(w.mainHome);
  const r = w.uninstall([]);
  assert.equal(r.status, 0, r.text);
  assert.ok(!existsSync(w.caoHome));
  assert.ok(!existsSync(w.launcher));
  assert.deepEqual(snapshot(w.mainHome), mainBefore);
  const renamed = readdirSync(w.xdg).filter((n) => /^herdr-app-cao\.removed-\d+$/.test(n));
  assert.equal(renamed.length, 1);
  assert.ok(existsSync(join(w.xdg, renamed[0], "settings.json")));
});

// ---- the theme the copied settings.json names is copied with it (defect 3) ----

const DRACULA = '{"name": "dracula", "colors": {"background": "#282a36"}}\n';

test("a theme named in the copied settings.json is copied from the main home's themes folder, listed in the manifest, and the main home is untouched", () => {
  const w = world();
  mainSettings(w, { telemetry: false, theme: "dracula" });
  mainTheme(w, "dracula", DRACULA);
  mainTheme(w, "other", "{}\n");
  const mainBefore = snapshot(w.mainHome);
  const r = w.install(["--local"]);
  assert.equal(r.status, 0, r.text);
  assert.equal(readFileSync(join(w.caoHome, "themes", "dracula.json"), "utf8"), DRACULA);
  assert.deepEqual(files(join(w.caoHome, "themes")), ["dracula.json"], "only the named theme is copied");
  assert.equal(manifestEntry(w, "themes/dracula.json")?.sha256, sha(join(w.caoHome, "themes", "dracula.json")));
  assert.match(r.out, /install: wrote themes\/dracula\.json/);
  assert.doesNotMatch(r.text, /install: note:.*theme/i);
  assert.deepEqual(snapshot(w.mainHome), mainBefore, "the main home changed");
});

test("a theme the main home does not have is not copied, and one note line says it was not found", () => {
  const w = world();
  mainSettings(w, { telemetry: false, theme: "dracula" });
  const r = w.install(["--local"]);
  assert.equal(r.status, 0, r.text);
  assert.ok(!existsSync(join(w.caoHome, "themes")), "a themes folder was made for a theme that does not exist");
  const notes = r.text.split("\n").filter((l) => /^install: note:/.test(l) && /theme/i.test(l));
  assert.equal(notes.length, 1, r.text);
  assert.match(notes[0], /dracula/);
  assert.match(notes[0], /not found/i);
  assert.ok(notes[0].includes(join(w.mainHome, "themes", "dracula.json")), "the note does not say where the theme was looked for");
  assert.equal(manifestEntry(w, "themes/dracula.json"), undefined);
});

test("with the theme copied, a second install is up to date and writes nothing; a changed theme file in the main home is replaced with a backup", () => {
  const w = world();
  mainSettings(w, { telemetry: false, theme: "dracula" });
  mainTheme(w, "dracula", DRACULA);
  assert.equal(w.install(["--local"]).status, 0);
  age(w);
  const before = snapshot(w.caoHome);
  const manifestBytes = readFileSync(join(w.caoHome, "cao-window-install.json"), "utf8");
  const r = w.install(["--local"]);
  assert.equal(r.status, 0, r.text);
  assert.match(r.out, /install: up to date/);
  assert.deepEqual(wroteLines(r.out), []);
  assert.deepEqual(snapshot(w.caoHome), before);
  assert.equal(readFileSync(join(w.caoHome, "cao-window-install.json"), "utf8"), manifestBytes);
  assert.ok(!files(w.caoHome).some((f) => f.includes(".bak-")));
  mainTheme(w, "dracula", '{"name": "dracula", "colors": {"background": "#000000"}}\n');
  const changed = w.install(["--local"]);
  assert.equal(changed.status, 0, changed.text);
  assert.match(changed.out, /install: wrote themes\/dracula\.json/);
  assert.equal(baks(w.caoHome, "themes/dracula.json").length, 1, "the old theme was not kept");
  assert.match(readFileSync(join(w.caoHome, "themes", "dracula.json"), "utf8"), /#000000/);
});

test("with the theme not found, a second install is still up to date (the note repeats, nothing is written)", () => {
  const w = world();
  mainSettings(w, { theme: "dracula" });
  w.install(["--local"]);
  age(w);
  const before = snapshot(w.caoHome);
  const r = w.install(["--local"]);
  assert.match(r.out, /install: up to date/);
  assert.match(r.text, /install: note:.*dracula.*not found/i);
  assert.deepEqual(snapshot(w.caoHome), before);
});

test("the theme is read from the settings.json the window's home already has, not from the main home's", () => {
  const w = world();
  mainSettings(w, { theme: "dracula" });
  mainTheme(w, "dracula", DRACULA);
  mainTheme(w, "nord", '{"name": "nord"}\n');
  mkdirSync(w.caoHome, { recursive: true });
  writeFileSync(join(w.caoHome, "settings.json"), '{"theme": "nord"}\n');
  const r = w.install(["--local"]);
  assert.equal(r.status, 0, r.text);
  assert.deepEqual(files(join(w.caoHome, "themes")), ["nord.json"]);
});

test("--telemetry keeps the theme and copies the theme file", () => {
  const w = world();
  mainSettings(w, { telemetry: true, theme: "dracula" });
  mainTheme(w, "dracula", DRACULA);
  assert.equal(w.install(["--local", "--telemetry", "off"]).status, 0);
  assert.deepEqual(w.json("settings.json"), { telemetry: false, theme: "dracula" });
  assert.equal(readFileSync(join(w.caoHome, "themes", "dracula.json"), "utf8"), DRACULA);
});

test("settings without a theme, a theme that is not text, and a settings.json that is not an object copy no theme and print no theme note", () => {
  for (const text of ['{"telemetry": false}', '{"theme": 3}', '{"theme": ""}', '{"theme": null}', "[1, 2]", "{not json"]) {
    const w = world();
    mkdirSync(w.mainHome, { recursive: true });
    writeFileSync(join(w.mainHome, "settings.json"), text);
    mainTheme(w, "dracula", DRACULA);
    const r = w.install(["--local"]);
    assert.equal(r.status, 0, `${text}: ${r.text}`);
    assert.ok(!existsSync(join(w.caoHome, "themes")), text);
    assert.doesNotMatch(r.text, /theme/i, text);
  }
});

test("a theme name that could leave the themes folder is not followed: nothing is copied and the note says so", () => {
  for (const name of ["../layouts/main", "../../herdr-app/themes/dracula", "/etc/passwd", "a/b", ".", "..", "a b", "a\nb"]) {
    const w = world();
    mainSettings(w, { theme: name });
    mainTheme(w, "dracula", DRACULA);
    withMainHome(w);
    mkdirSync(join(w.mainHome, "themes", "a"), { recursive: true });
    writeFileSync(join(w.mainHome, "themes", "a", "b.json"), "{}");
    const r = w.install(["--local"]);
    assert.equal(r.status, 0, `${JSON.stringify(name)}: ${r.text}`);
    assert.ok(!existsSync(join(w.caoHome, "themes")), `${JSON.stringify(name)}: a themes folder was made`);
    assert.deepEqual(files(w.caoHome).filter((f) => f.startsWith("themes")), []);
    const notes = r.text.split("\n").filter((l) => /^install: note:/.test(l) && /theme/i.test(l));
    assert.equal(notes.length, 1, `${JSON.stringify(name)}: ${r.text}`);
  }
});

test("a theme name with a leading dot or two dots inside is followed: .hidden and a..b are copied, listed in the manifest, and a second install is up to date", () => {
  for (const name of [".hidden", "a..b"]) {
    const w = world();
    mainSettings(w, { telemetry: false, theme: name });
    mainTheme(w, name, DRACULA);
    const mainBefore = snapshot(w.mainHome);
    const r = w.install(["--local"]);
    assert.equal(r.status, 0, `${name}: ${r.text}`);
    assert.equal(readFileSync(join(w.caoHome, "themes", `${name}.json`), "utf8"), DRACULA, name);
    assert.deepEqual(files(join(w.caoHome, "themes")), [`${name}.json`], `${name}: only the named theme is copied`);
    assert.equal(manifestEntry(w, `themes/${name}.json`)?.sha256, sha(join(w.caoHome, "themes", `${name}.json`)), name);
    assert.doesNotMatch(r.text, /install: note:.*theme/i, name);
    assert.deepEqual(snapshot(w.mainHome), mainBefore, `${name}: the main home changed`);
    age(w);
    const before = snapshot(w.caoHome);
    assert.match(w.install(["--local"]).out, /install: up to date/, name);
    assert.deepEqual(snapshot(w.caoHome), before, `${name}: a file was rewritten`);
  }
});

test("a folder where the theme file goes stops the install before anything is written", () => {
  const w = world();
  mainSettings(w, { theme: "dracula" });
  mainTheme(w, "dracula", DRACULA);
  mkdirSync(join(w.caoHome, "themes", "dracula.json", "keep"), { recursive: true });
  const r = w.install(["--local"]);
  assert.equal(r.status, 1, r.text);
  assert.match(r.err, /not a file/);
  assert.deepEqual(files(w.caoHome), [], "files were written to the window's home");
  assert.ok(existsSync(join(w.caoHome, "themes", "dracula.json", "keep")));
  assert.ok(!existsSync(w.launcher));
  assert.deepEqual(w.adds(), []);
});

test("a theme file that is a symlink in the main home is copied as a regular file; a dangling one is not found", () => {
  const w = world();
  mainSettings(w, { theme: "dracula" });
  mkdirSync(join(w.mainHome, "themes"), { recursive: true });
  writeFileSync(join(w.root, "real-theme.json"), DRACULA);
  symlinkSync(join(w.root, "real-theme.json"), join(w.mainHome, "themes", "dracula.json"));
  assert.equal(w.install(["--local"]).status, 0);
  assert.equal(readFileSync(join(w.caoHome, "themes", "dracula.json"), "utf8"), DRACULA);
  assert.ok(!lstatSync(join(w.caoHome, "themes", "dracula.json")).isSymbolicLink());
  const w2 = world();
  mainSettings(w2, { theme: "dracula" });
  mkdirSync(join(w2.mainHome, "themes"), { recursive: true });
  symlinkSync(join(w2.root, "missing.json"), join(w2.mainHome, "themes", "dracula.json"));
  const r = w2.install(["--local"]);
  assert.equal(r.status, 0, r.text);
  assert.match(r.text, /install: note:.*not found/i);
  assert.ok(!existsSync(join(w2.caoHome, "themes")));
});

test("uninstall removes a copied theme with the rest of the home", () => {
  const w = world();
  mainSettings(w, { theme: "dracula" });
  mainTheme(w, "dracula", DRACULA);
  w.install(["--local"]);
  const mainBefore = snapshot(w.mainHome);
  const r = w.uninstall([]);
  assert.equal(r.status, 0, r.text);
  assert.ok(!existsSync(w.caoHome));
  const renamed = readdirSync(w.xdg).filter((n) => /^herdr-app-cao\.removed-\d+$/.test(n));
  assert.equal(renamed.length, 1);
  assert.ok(existsSync(join(w.xdg, renamed[0], "themes", "dracula.json")));
  assert.deepEqual(snapshot(w.mainHome), mainBefore);
  assert.ok(existsSync(join(w.mainHome, "themes", "dracula.json")), "the main home's theme was removed");
});

// ---------------------------------------------------------------------------------------------
// The launcher and the two switches
// ---------------------------------------------------------------------------------------------

test("the launcher is written with the Darwin content", () => {
  const w = world();
  assert.equal(w.install(["--local", "--bin-dir", join(w.root, "bin"), "--launcher-name", "cao-open"]).status, 0);
  const text = readFileSync(join(w.root, "bin", "cao-open"), "utf8");
  assert.ok(text.includes(`open -n -a /Applications/Herdr.app --env HERDR_APP_HOME=${w.caoHome} --env HC_SESSION=caowin`), text);
  assert.ok(statSync(join(w.root, "bin", "cao-open")).mode & 0o111);
});

test("Launcher content on macOS: the default path, the default app, and --app and --core-session honoured", () => {
  const w = world();
  assert.equal(w.install(["--local", "--app", "/Applications/Other Herdr.app", "--core-session", "mywin"]).status, 0);
  const t = launcherArgv(w, w.launcher);
  assert.deepEqual(t.argv, ["-n", "-a", "/Applications/Other Herdr.app", "--env", `HERDR_APP_HOME=${w.caoHome}`, "--env", "HC_SESSION=mywin"]);
});

test("the launcher is the installed home's and nothing else: running it passes the home it was installed for", () => {
  const w = world();
  const home = join(w.root, "a home, with 'quotes' and $dollar");
  assert.equal(w.install(["--local", "--home", home, "--bin-dir", join(w.root, "my bin")]).status, 0);
  const t = launcherArgv(w, join(w.root, "my bin", "herdr-cao"));
  assert.equal(t.status, 0, t.err);
  assert.equal(t.argv[4], `HERDR_APP_HOME=${home}`);
});

test("the toast switch: off by default, on with TOAST_MOD=1, in the folder, the layout key and the summary line", () => {
  const w = world();
  const off = w.install(["--local"]);
  assert.equal(off.status, 0, off.text);
  assert.match(off.out, /toast mod off/);
  assert.equal(existsSync(join(w.caoHome, "mods", "cao-window-toast")), false);
  assert.equal(w.layout().toast, undefined);
  const on = w.install(["--local"], { TOAST_MOD: "1" });
  assert.equal(on.status, 0, on.text);
  assert.match(on.out, /toast mod on/);
  assert.ok(existsSync(join(w.caoHome, "mods", "cao-window-toast", "main.js")));
  assert.ok(existsSync(join(w.caoHome, "mods", "cao-window-toast", "mod.json")));
  assert.equal(w.layout().toast, "cao-window-toast");
  assert.equal(baks(w.caoHome, "cao-window.json").length, 1, "the replaced layout keeps a backup");
});

test("only TOAST_MOD=1 turns the toast mod on: 0, empty, true and yes leave it off", () => {
  for (const value of ["0", "", "true", "yes", "2"]) {
    const w = world();
    const r = w.install(["--local"], { TOAST_MOD: value });
    assert.equal(r.status, 0, r.text);
    assert.match(r.out, /toast mod off/, JSON.stringify(value));
    assert.equal(w.layout().toast, undefined);
  }
});

test("bundle mode: one main.js per mod, no relative import left, each bundle parses", () => {
  const w = world();
  const r = w.install(["--cao-ssh-host", "build-box"], { MOD_IMPORTS: "bundle" });
  assert.equal(r.status, 0, r.text);
  assert.match(r.out, /mod imports: bundle/);
  for (const m of ["side", "panel", "tabs", "status"]) {
    const dir = join(w.caoHome, "mods", `cao-window-${m}`);
    assert.deepEqual(files(dir), ["main.js", "mod.json", ...drawingsOf(m)].sort(), m);   // the one bundle, mod.json and the mod's own drawings
    const text = readFileSync(join(dir, "main.js"), "utf8");
    assert.ok(!/from\s+["']\.\.?\//.test(text), `${m}: a relative import is left`);
    assert.ok(!/^export\s+(async\s+)?(function|const|let|class)\s/m.test(text), `${m}: a plain export is left`);
    const copy = join(w.root, `${m}.mjs`);
    writeFileSync(copy, text);                                       // node checks a module by its .mjs extension
    const c = spawnSync(process.execPath, ["--check", copy], { encoding: "utf8" });
    assert.equal(c.status, 0, `${m}: ${c.stderr}`);               // catches a name two shared modules both declare
  }
  const panel = readFileSync(join(w.caoHome, "mods", "cao-window-panel", "main.js"), "utf8");
  if (existsSync(join(here, "mods", "cao-window-panel", "templates.js"))) {
    assert.ok(panel.includes("from cao_workflow import"), "the Python template survived the bundle");   // an `import` line inside a template would be dropped
  }
});

test("bundle mode: the host's own imports survive, and a second bundle run changes nothing", () => {
  const w = world();
  assert.equal(w.install(["--local"], { MOD_IMPORTS: "bundle" }).status, 0);
  for (const m of ["side", "panel", "tabs", "status"]) {
    const original = readFileSync(join(here, "mods", `cao-window-${m}`, "main.js"), "utf8");
    const bundled = readFileSync(join(w.caoHome, "mods", `cao-window-${m}`, "main.js"), "utf8");
    for (const line of original.split("\n").filter((l) => /^import .* from ["'](?!\.)/.test(l))) assert.ok(bundled.includes(line), `${m}: lost ${line}`);
  }
  age(w);
  const before = snapshot(w.caoHome);
  const again = w.install(["--local"], { MOD_IMPORTS: "bundle" });
  assert.match(again.out, /up to date/);
  assert.deepEqual(snapshot(w.caoHome), before);
});

test("only MOD_IMPORTS=bundle changes the import mode; an unknown value is refused or means copy, and the summary says which", () => {
  for (const value of ["", "copy"]) {
    const w = world();
    const r = w.install(["--local"], { MOD_IMPORTS: value });
    assert.equal(r.status, 0, r.text);
    assert.match(r.out, /mod imports: copy/, JSON.stringify(value));
    assert.ok(existsSync(join(w.caoHome, "mods", "cao-window-side", "logic.js")));
  }
  for (const value of ["BUNDLE", "yes", "1"]) {
    const w = world();
    const r = w.install(["--local"], { MOD_IMPORTS: value });
    if (r.status !== 0) { nothingWritten(w); continue; }
    assert.match(r.out, /mod imports: copy/, `${JSON.stringify(value)}: the summary must state the mode in effect`);
    assert.ok(existsSync(join(w.caoHome, "mods", "cao-window-side", "logic.js")));
  }
});

test("the summary line names both switches whatever their value, so a stray variable shows", () => {
  const w = world();
  const r = w.install(["--local"], { TOAST_MOD: "1", MOD_IMPORTS: "bundle" });
  assert.match(r.out, /toast mod on, mod imports: bundle/);
});

// ---------------------------------------------------------------------------------------------
// Adversarial: spaces, read-only homes, files in the way
// ---------------------------------------------------------------------------------------------

test("a HOME and a --home with spaces install, repeat as up to date, and uninstall", () => {
  const w = world({ spaced: true });
  const home = join(w.root, "a space", "cao home");
  const r = w.install(["--local", "--home", home]);
  assert.equal(r.status, 0, r.text);
  assert.ok(existsSync(join(home, "layouts", "cao-window.json")));
  assert.ok(existsSync(w.launcher), "the launcher under a HOME with a space");
  age({ ...w, caoHome: home });
  const beforeSpaced = snapshot(home);
  const launcherSpaced = [statSync(w.launcher).mtimeMs, sha(w.launcher)];
  assert.match(w.install(["--local", "--home", home]).out, /up to date/);
  assert.deepEqual(snapshot(home), beforeSpaced, "a file was rewritten although the run said up to date");
  assert.deepEqual([statSync(w.launcher).mtimeMs, sha(w.launcher)], launcherSpaced);
  assert.ok(!files(home).some((f) => f.includes(".bak-")));
  const t = launcherArgv(w, w.launcher);
  assert.equal(t.argv[4], `HERDR_APP_HOME=${home}`);
  const u = w.uninstall(["--home", home]);
  assert.equal(u.status, 0, u.text);
  assert.ok(!existsSync(home));
  assert.ok(!existsSync(w.launcher));
});

test("a read-only parent for the home stops the install with a message, before the machine is added", () => {
  if (process.getuid && process.getuid() === 0) { console.log("note: running as root, so a read-only folder is not read-only; case skipped"); return; }
  const w = world();
  chmodSync(w.xdg, 0o555);
  try {
    const r = w.install(["--local"]);
    assert.notEqual(r.status, 0);
    assert.ok(r.err.trim().length > 0, "no message");
    assert.deepEqual(w.adds(), []);
    assert.deepEqual(w.tmpLeft(), []);
  } finally { chmodSync(w.xdg, 0o755); }
});

test("an unwritable launcher folder stops the install with a message and no machine entry", () => {
  if (process.getuid && process.getuid() === 0) return;
  const w = world();
  const bin = join(w.root, "ro-bin");
  mkdirSync(bin);
  chmodSync(bin, 0o555);
  try {
    const r = w.install(["--local", "--bin-dir", bin]);
    assert.notEqual(r.status, 0);
    assert.ok(r.err.trim().length > 0);
    assert.deepEqual(w.adds(), []);
    assert.deepEqual(files(w.caoHome), [], "files were written to the window's home before the launcher folder was found unwritable");
    assert.deepEqual(readdirSync(bin), [], "something was written to the launcher folder");
    assert.deepEqual(w.tmpLeft(), []);
  } finally { chmodSync(bin, 0o755); }
});

test("a regular file where the home should be stops the install and leaves that file alone", () => {
  const w = world();
  writeFileSync(w.caoHome, "not a folder\n");
  const r = w.install(["--local"]);
  assert.notEqual(r.status, 0);
  assert.equal(readFileSync(w.caoHome, "utf8"), "not a folder\n");
  assert.deepEqual(w.adds(), []);
  assert.ok(!existsSync(w.launcher));
});

test("a regular file where the launcher folder should be stops the install", () => {
  const w = world();
  const bin = join(w.root, "bin-file");
  writeFileSync(bin, "x\n");
  const r = w.install(["--local", "--bin-dir", bin]);
  assert.notEqual(r.status, 0);
  assert.equal(readFileSync(bin, "utf8"), "x\n");
  assert.deepEqual(w.adds(), []);
  assert.deepEqual(files(w.caoHome), [], "files were written to the window's home before the launcher folder was found to be a file");
  assert.ok(!existsSync(w.launcher));
  assert.deepEqual(w.tmpLeft(), []);
});

test("a folder where a shipped file should be stops the install without deleting the folder", () => {
  const w = world();
  mkdirSync(join(w.caoHome, "layouts", "cao-window.json", "keep"), { recursive: true });
  writeFileSync(join(w.caoHome, "layouts", "cao-window.json", "keep", "f"), "x");
  const r = w.install(["--local"]);
  assert.notEqual(r.status, 0);
  assert.ok(existsSync(join(w.caoHome, "layouts", "cao-window.json", "keep", "f")));
  assert.deepEqual(w.adds(), []);
  assert.deepEqual(files(w.caoHome), [join("layouts", "cao-window.json", "keep", "f")], "the home changed: only the folder's own file may be there");
  assert.ok(!existsSync(w.launcher));
  assert.deepEqual(w.tmpLeft(), []);
});

// ---------------------------------------------------------------------------------------------
// Uninstall (task 5.3)
// ---------------------------------------------------------------------------------------------

test("uninstall: quit, then machine rm, then server stop; launcher removed; home renamed", () => {
  const w = world();
  withMainHome(w);
  w.install(["--local"]);
  const mainBefore = snapshot(w.mainHome);
  const r = w.uninstall([]);
  assert.equal(r.status, 0, r.text);
  const lines = w.log();
  const idx = (needle) => lines.findIndex((l) => l === needle);
  const quit = idx(`HERDR_APP_HOME=${w.caoHome} herdr-app do app.quit`);
  const rm = idx("HC_SESSION=caowin hc machine rm cao");
  const stop = idx("HC_SESSION=caowin hc server stop");
  assert.ok(quit >= 0 && rm > quit && stop > rm, lines.join("\n"));
  assert.ok(!existsSync(w.launcher));
  assert.ok(!existsSync(w.caoHome));
  const renamed = readdirSync(w.xdg).filter((n) => /^herdr-app-cao\.removed-\d+$/.test(n));
  assert.equal(renamed.length, 1);
  assert.ok(!lines.some((l) => /^HC_SESSION=(cao|default) /.test(l)), "touched a forbidden core");
  assert.deepEqual(snapshot(w.mainHome), mainBefore);
});

test("uninstall keeps the window's files under the .removed-<epoch> name and touches nothing else", () => {
  const w = world();
  w.install(["--local"]);
  const kept = files(w.caoHome);
  const other = join(dirname(w.launcher), "unrelated");
  writeFileSync(other, "mine");
  const before = nowEpoch();
  assert.equal(w.uninstall([]).status, 0);
  const name = readdirSync(w.xdg).find((n) => /^herdr-app-cao\.removed-\d+$/.test(n));
  assert.deepEqual(files(join(w.xdg, name)), kept);
  const epoch = Number(name.split("-").at(-1));
  assert.ok(epoch >= before - 1 && epoch <= nowEpoch() + 1);
  assert.equal(readFileSync(other, "utf8"), "mine");
  assert.deepEqual(w.tmpLeft(), []);
});

test("uninstall runs only machine ls, machine rm and server stop, in that order: no other hc call, no other herdr-app call", () => {
  const w = world();
  w.install(["--local"]);
  const mark = w.log().length;
  assert.equal(w.uninstall([]).status, 0);
  const calls = w.log().slice(mark);
  const hcCalls = calls.filter((l) => / hc( |$)/.test(l));
  assert.deepEqual(hcCalls, ["HC_SESSION=caowin hc machine ls --json", "HC_SESSION=caowin hc machine rm cao", "HC_SESSION=caowin hc server stop"]);
  const appCalls = calls.filter((l) => / herdr-app( |$)/.test(l)).map((l) => l.replace(/^HERDR_APP_HOME=\S* /, ""));
  assert.ok(appCalls.every((l) => /^herdr-app (status|do app\.quit)$/.test(l)), appCalls.join("\n"));
});

test("uninstall uses the core session, machine name and launcher the install recorded", () => {
  const w = world();
  const bin = join(w.root, "my-bin");
  const home = join(w.root, "my-home");
  w.install(["--local", "--core-session", "mywin", "--machine", "bx7", "--bin-dir", bin, "--launcher-name", "cao-open", "--home", home]);
  const r = w.uninstall(["--home", home]);
  assert.equal(r.status, 0, r.text);
  assert.ok(w.log().includes("HC_SESSION=mywin hc machine rm bx7"), w.log().join("\n"));
  assert.ok(w.log().includes("HC_SESSION=mywin hc server stop"));
  assert.ok(!existsSync(join(bin, "cao-open")));
  assert.ok(!existsSync(home));
  assert.ok(w.log().includes(`HERDR_APP_HOME=${home} herdr-app do app.quit`));
});

test("uninstall does not quit a window that is not running", () => {
  const w = world({ live: 1 });
  w.install(["--local"]);
  assert.equal(w.uninstall([]).status, 0);
  assert.ok(!w.log().some((l) => l.includes("app.quit")));
  assert.ok(w.log().includes("HC_SESSION=caowin hc machine rm cao"));
});

test("uninstall refuses when nothing was installed, and never takes the main home", () => {
  const w = world();
  assert.equal(w.uninstall([]).status, 2);
  mkdirSync(w.mainHome, { recursive: true });
  writeFileSync(join(w.mainHome, "cao-window-install.json"), JSON.stringify({ core: "caowin", machine: "cao", binDir: w.root, launcher: "x" }));
  assert.equal(w.uninstall(["--home", w.mainHome]).status, 2);
  assert.equal(w.uninstall(["--home", `${w.mainHome}/`]).status, 2);
  assert.ok(existsSync(w.mainHome));
  assert.deepEqual(w.log().filter((l) => / hc /.test(l)), []);
});

test("uninstall run twice: the second finds nothing to remove and changes nothing", () => {
  const w = world();
  w.install(["--local"]);
  assert.equal(w.uninstall([]).status, 0);
  const mark = w.log().length;
  assert.equal(w.uninstall([]).status, 2);
  assert.deepEqual(w.log().slice(mark), []);
});

test("uninstall refuses a manifest that names CAO's own core or the default core, and removes nothing", () => {
  for (const core of ["cao", "default", ""]) {
    const w = world();
    w.install(["--local"]);
    const manifest = w.json("cao-window-install.json");
    writeFileSync(join(w.caoHome, "cao-window-install.json"), JSON.stringify({ ...manifest, core }));
    const mark = w.log().length;
    const r = w.uninstall([]);
    assert.equal(r.status, 2, JSON.stringify(core));
    assert.deepEqual(w.log().slice(mark), [], `uninstall called a tool for core ${JSON.stringify(core)}`);
    assert.ok(existsSync(w.caoHome));
    assert.ok(existsSync(w.launcher));
  }
});

test("uninstall with a manifest that is not JSON stops and changes nothing", () => {
  const w = world();
  w.install(["--local"]);
  writeFileSync(join(w.caoHome, "cao-window-install.json"), "{oops");
  const mark = w.log().length;
  const r = w.uninstall([]);
  assert.notEqual(r.status, 0);
  assert.deepEqual(w.log().slice(mark), []);
  assert.ok(existsSync(w.caoHome));
});

test("uninstall started inside a Herdr terminal does not leak that terminal's core into hc", () => {
  const w = world();
  w.install(["--local"]);
  assert.equal(w.uninstall([], { HC_SOCKET: "/evil.sock", HC_TERMINAL_ID: "t9", HC_SESSION: "default" }).status, 0);
  assert.ok(!w.log().some((l) => l.startsWith("LEAK")));
  assert.ok(!w.log().some((l) => /^HC_SESSION=(cao|default) /.test(l)));
});

test("uninstall --help exits 0 and changes nothing; an unknown option exits 2", () => {
  const w = world();
  w.install(["--local"]);
  const before = snapshot(w.caoHome);
  const mark = w.log().length;
  assert.equal(w.uninstall(["--help"]).status, 0);
  assert.equal(w.uninstall(["--bogus"]).status, 2);
  assert.deepEqual(snapshot(w.caoHome), before);
  assert.deepEqual(w.log().slice(mark), []);
});

test("an install after an uninstall starts clean and leaves the .removed folder alone", () => {
  const w = world();
  w.install(["--local"]);
  w.uninstall([]);
  const removed = readdirSync(w.xdg).find((n) => /\.removed-\d+$/.test(n));
  const kept = snapshot(join(w.xdg, removed));
  const r = w.install(["--local"]);
  assert.equal(r.status, 0, r.text);
  assert.doesNotMatch(r.out, /up to date/);
  assert.deepEqual(snapshot(join(w.xdg, removed)), kept);
  assert.equal(w.adds().length, 2, "the machine entry was added again after the uninstall removed it");
});

// ---------------------------------------------------------------------------------------------
// Home and launcher edge cases, interrupted runs, uninstall failures
// ---------------------------------------------------------------------------------------------

test("a --home written through a folder that does not exist yet is still the main home or $HERDR_APP_HOME", () => {
  const w = world();
  withMainHome(w);
  const before = snapshot(w.mainHome);
  const through = (base, name) => `${base}/zz/../${name}`;          // zz is not there: nothing for cd -P to resolve
  const other = join(w.root, "other-home");
  const spellings = [
    [["--home", through(w.xdg, "herdr-app")], {}, "main home through a missing folder"],
    [["--home", `${w.xdg}/./herdr-app`], {}, "main home through ."],
    [["--home", `${w.xdg}//herdr-app`], {}, "main home with a doubled slash"],
    [["--home", `${w.xdg}/zz/yy/../../herdr-app`], {}, "main home through two missing folders"],
    [["--home", through(w.root, "other-home")], { HERDR_APP_HOME: other }, "$HERDR_APP_HOME through a missing folder"],
    [["--home", other], { HERDR_APP_HOME: through(w.root, "other-home") }, "$HERDR_APP_HOME spelled through a missing folder"],
    [["--home", join(w.xdg, "herdr-app"), "--main-home", through(w.xdg, "herdr-app")], {}, "--main-home spelled through a missing folder"],
  ];
  for (const [args, env, why] of spellings) {
    const r = w.install(["--local", ...args], env);
    assert.equal(r.status, 2, `${why}: the install went ahead\n${r.text}`);
  }
  nothingWritten(w);
  assert.deepEqual(snapshot(w.mainHome), before, "the main home was written through");
  assert.ok(!existsSync(other) && !existsSync(join(w.xdg, "zz")), "the install created a folder it was asked to step through");
});

test("--core-session equal to CAO's own core session is refused and nothing is written", () => {
  const w = world();
  const r = w.install(["--local", "--cao-core-session", "work", "--core-session", "work"]);
  assert.equal(r.status, 2, r.text);
  assert.match(r.err, /work/);
  nothingWritten(w);
  assert.deepEqual(w.log().filter((l) => / hc /.test(l)), [], "hc was called for a refused install");
});

test("uninstall refuses a manifest whose core is CAO's core, and never stops it", () => {
  const w = world();
  w.install(["--local", "--cao-core-session", "work"]);
  const manifest = w.json("cao-window-install.json");
  assert.equal(manifest.caoCore, "work");
  writeFileSync(join(w.caoHome, "cao-window-install.json"), JSON.stringify({ ...manifest, core: manifest.caoCore }));
  const mark = w.log().length;
  const r = w.uninstall([]);
  assert.equal(r.status, 2, r.text);
  assert.deepEqual(w.log().slice(mark), [], "uninstall called a tool against CAO's core");
  assert.ok(!w.log().some((l) => l === "HC_SESSION=work hc server stop"));
  assert.ok(existsSync(w.caoHome) && existsSync(w.launcher), "the install was removed");
});

test("a relative --bin-dir and --app are stored and embedded as absolute paths", () => {
  const w = world();
  const r = w.install(["--local", "--bin-dir", "bin", "--app", "Herdr.app"], {}, w.root);
  assert.equal(r.status, 0, r.text);
  const launcher = join(w.root, "bin", "herdr-cao");
  assert.ok(existsSync(launcher), "the launcher is not in <cwd>/bin");
  const manifest = w.json("cao-window-install.json");
  assert.ok(isAbsolute(manifest.binDir), `the manifest stores binDir ${JSON.stringify(manifest.binDir)}`);
  assert.equal(manifest.binDir, join(w.root, "bin"));
  assert.equal(manifest.app, join(w.root, "Herdr.app"), "the manifest stores a relative app path");
  assert.ok(isAbsolute(manifest.home));
  const t = launcherArgv(w, launcher);
  assert.equal(t.argv[2], join(w.root, "Herdr.app"), "the launcher passes a relative app path to open");
  assert.ok(readFileSync(launcher, "utf8").includes(join(w.root, "Herdr.app")));
});

test("uninstall run from another working directory removes the launcher the install wrote", () => {
  const w = world();
  assert.equal(w.install(["--local", "--bin-dir", "bin", "--app", "Herdr.app"], {}, w.root).status, 0);
  const launcher = join(w.root, "bin", "herdr-cao");
  const r = w.uninstall([], {}, w.home);
  assert.equal(r.status, 0, r.text);
  assert.ok(!existsSync(launcher), "the launcher is still there although uninstall reported success");
  assert.ok(!existsSync(w.caoHome));
});

test("a manifest with a relative bin dir never makes uninstall delete a file of the working directory", () => {
  const w = world();
  w.install(["--local"]);
  const manifest = w.json("cao-window-install.json");
  writeFileSync(join(w.caoHome, "cao-window-install.json"), JSON.stringify({ ...manifest, binDir: "decoy-bin" }));
  const elsewhere = join(w.root, "elsewhere");
  mkdirSync(join(elsewhere, "decoy-bin"), { recursive: true });
  const decoy = join(elsewhere, "decoy-bin", manifest.launcher);
  writeFileSync(decoy, "mine");
  w.uninstall([], {}, elsewhere);
  assert.ok(existsSync(decoy), "uninstall deleted a file it resolved against the working directory");
  assert.equal(readFileSync(decoy, "utf8"), "mine");
});

test("uninstall does not report plain success when the launcher is not where the manifest says", () => {
  const w = world();
  w.install(["--local"]);
  const manifest = w.json("cao-window-install.json");
  const missing = join(w.root, "no-such-bin");
  writeFileSync(join(w.caoHome, "cao-window-install.json"), JSON.stringify({ ...manifest, binDir: missing }));
  const r = w.uninstall([]);
  const refused = r.status !== 0 && existsSync(w.caoHome);
  const warned = r.status === 0 && r.err.includes(missing) && /launcher/i.test(r.err);
  assert.ok(refused || warned, `exit ${r.status} with no word about the launcher that was not found:\n${r.text}`);
  assert.ok(existsSync(w.launcher), "the real launcher was removed by a run that could not find it");
});

test("a run killed between the manifest and the layout does not make the next run think the layout was customized", () => {
  const w = world();
  assert.equal(w.install(["--local"]).status, 0);
  const originalLayout = readFileSync(join(w.caoHome, "layouts", "cao-window.json"), "utf8");
  const trigger = join(w.root, "mv.trigger");
  w.put("mv", `#!/bin/sh
for last; do :; done
case $last in
  *layouts/cao-window.json)
    if [ -f "$MV_TRIGGER" ]; then
      case $(cat "$MV_TRIGGER") in
        kill) kill -9 $PPID; exit 1 ;;
        *) exit 1 ;;
      esac
    fi ;;
esac
exec ${REAL_MV} "$@"
`);
  writeFileSync(trigger, "kill");
  const killed = w.install(["--local", "--cao-port", "9999"], { MV_TRIGGER: trigger });
  assert.equal(killed.signal, "SIGKILL", `the simulated kill did not land (status ${killed.status}): ${killed.text}`);
  assert.ok(!readFileSync(join(w.caoHome, "layouts", "cao-window.json"), "utf8").includes("9999"), "the layout was already replaced");
  rmSync(trigger);
  const again = w.install(["--local", "--cao-port", "9999"]);
  assert.equal(again.status, 0, `the rerun was refused:\n${again.text}`);
  assert.ok(readFileSync(join(w.caoHome, "layouts", "cao-window.json"), "utf8").includes("9999"));
  const layoutBaks = baks(join(w.caoHome, "layouts"), "cao-window.json");
  assert.ok(layoutBaks.some((f) => readFileSync(join(w.caoHome, "layouts", f), "utf8") === originalLayout), "no backup holds the layout that was replaced");
  assert.match(again.out, /install: (done|wrote)/);
});

test("a run that fails writing the layout, after the files before it, can be repeated the same way", () => {
  const w = world();
  assert.equal(w.install(["--local"]).status, 0);
  const trigger = join(w.root, "mv.trigger");
  w.put("mv", `#!/bin/sh
for last; do :; done
case $last in
  *layouts/cao-window.json) [ -f "$MV_TRIGGER" ] && exit 1 ;;
esac
exec ${REAL_MV} "$@"
`);
  writeFileSync(trigger, "fail");
  const failed = w.install(["--local", "--cao-port", "9999"], { MV_TRIGGER: trigger });
  assert.notEqual(failed.status, 0);
  rmSync(trigger);
  const again = w.install(["--local", "--cao-port", "9999"]);
  assert.equal(again.status, 0, `the rerun was refused:\n${again.text}`);
  assert.ok(readFileSync(join(w.caoHome, "layouts", "cao-window.json"), "utf8").includes("9999"));
});

for (const [flag, step, mentions] of [["rm.fail", "machine rm", /machine/i], ["stop.fail", "server stop", /server|core/i]]) {
  test(`uninstall exits non-zero and leaves the home in place when hc ${step} fails, so a rerun can retry`, () => {
    const w = world();
    w.install(["--local"]);
    writeFileSync(join(w.stubs, flag), "");
    const r = w.uninstall([]);
    assert.notEqual(r.status, 0, `uninstall reported success although hc ${step} failed:\n${r.text}`);
    assert.match(r.err, mentions);
    assert.ok(existsSync(join(w.caoHome, "cao-window-install.json")), "the manifest is gone, so a rerun cannot know the core or machine");
    assert.deepEqual(readdirSync(w.xdg).filter((n) => /\.removed-/.test(n)), [], "the home was moved aside");
    rmSync(join(w.stubs, flag));
    const mark = w.log().length;
    const again = w.uninstall([]);
    assert.equal(again.status, 0, again.text);
    assert.ok(w.log().slice(mark).includes("HC_SESSION=caowin hc server stop"), "the rerun did not finish the job");
    assert.ok(!existsSync(w.caoHome) && !existsSync(w.launcher));
  });
}

test("a --home that contains the main home, $HOME, the launcher folder or $HERDR_APP_HOME is refused and nothing is written", () => {
  const H = (w) => join(w.root, "h9");
  const cases = [
    ["$HOME", (w) => [["--home", w.home], {}]],
    ["$HOME with a trailing slash", (w) => [["--home", `${w.home}/`], {}]],
    ["$XDG_CONFIG_HOME, the parent of the main home", (w) => [["--home", w.xdg], {}]],
    ["the parent of $HOME", (w) => [["--home", w.root], {}]],
    ["$HOME spelled through ..", (w) => [["--home", `${w.xdg}/../.config/..`], {}]],
    ["a home that is also the launcher folder", (w) => [["--home", H(w), "--bin-dir", H(w)], {}]],
    ["a home that holds the launcher folder", (w) => [["--home", H(w), "--bin-dir", `${H(w)}/bin`], {}]],
    ["a home that holds a custom main home", (w) => [["--home", H(w), "--main-home", `${H(w)}/main`], {}]],
    ["a home that holds $HERDR_APP_HOME", (w) => [["--home", H(w)], { HERDR_APP_HOME: `${H(w)}/app` }]],
  ];
  for (const [why, build] of cases) {
    const w = world();
    withMainHome(w);
    const mainBefore = snapshot(w.mainHome);
    const [args, env] = build(w);
    const r = w.install(["--local", ...args], env);
    assert.equal(r.status, 2, `${why}: the install went ahead\n${r.text}`);
    assert.deepEqual(walk(w.home).filter((f) => !f.startsWith(join(".config", "herdr-app") + "/")), [], `${why}: files were written under $HOME`);
    assert.deepEqual(w.adds(), [], why);
    assert.deepEqual(snapshot(w.mainHome), mainBefore, why);
    assert.ok(!existsSync(H(w)), `${why}: the home was created`);
  }
});

// ---------------------------------------------------------------------------------------------
// Machine removal state, backups, temp files, empty options, homes inside app homes
// ---------------------------------------------------------------------------------------------

const rmCalls = (w, from = 0) => w.log().slice(from).filter((l) => l.includes(" machine rm "));

test("uninstall does not call machine rm when the machine is already absent from machine ls", () => {
  const w = world();
  w.install(["--local"]);
  writeFileSync(join(w.stubs, "machines.json"), "[]\n");
  const mark = w.log().length;
  const r = w.uninstall([]);
  assert.equal(r.status, 0, r.text);
  assert.deepEqual(rmCalls(w, mark), [], "machine rm ran for a machine that machine ls does not list");
  assert.ok(w.log().slice(mark).includes("HC_SESSION=caowin hc server stop"));
  assert.ok(w.log().slice(mark).includes("HC_SESSION=caowin hc machine ls --json"), "uninstall did not ask machine ls");
  assert.ok(!existsSync(w.caoHome));
});

test("uninstall calls machine rm when machine ls lists the machine, in either shape machine ls may print", () => {
  for (const listing of ['[{"name":"cao"}]', '{"machines":[{"id":1,"name":"cao"},{"name":"other"}]}']) {
    const w = world();
    w.install(["--local"]);
    writeFileSync(join(w.stubs, "machines.json"), `${listing}\n`);
    const mark = w.log().length;
    assert.equal(w.uninstall([]).status, 0);
    assert.deepEqual(rmCalls(w, mark), ["HC_SESSION=caowin hc machine rm cao"], listing);
  }
});

test("uninstall does not remove another machine that machine ls lists", () => {
  const w = world();
  w.install(["--local"]);
  writeFileSync(join(w.stubs, "machines.json"), '[{"name":"other"}]\n');
  const mark = w.log().length;
  assert.equal(w.uninstall([]).status, 0);
  assert.deepEqual(rmCalls(w, mark), []);
});

test("server stop fails, the installer runs again, and the next uninstall removes the machine it added back", () => {
  const w = world();
  w.install(["--local"]);
  writeFileSync(join(w.stubs, "stop.fail"), "");
  const failed = w.uninstall([]);
  assert.notEqual(failed.status, 0, failed.text);
  assert.deepEqual(files(w.caoHome).filter((f) => f.includes("machine-removed")), [], "a marker file was left in the home");
  rmSync(join(w.stubs, "stop.fail"));
  const again = w.install(["--local"]);
  assert.equal(again.status, 0, again.text);
  assert.equal(w.adds().length, 2, "the installer did not add the machine back");
  const mark = w.log().length;
  const done = w.uninstall([]);
  assert.equal(done.status, 0, done.text);
  assert.deepEqual(rmCalls(w, mark), ["HC_SESSION=caowin hc machine rm cao"], "the machine the installer added back was left in the core");
  assert.equal(readFileSync(join(w.stubs, "machines.json"), "utf8").trim(), "[]");
});

test("no file named .machine-removed is ever left in the home, whatever step failed", () => {
  for (const flag of ["rm.fail", "stop.fail", null]) {
    const w = world();
    w.install(["--local"]);
    if (flag) writeFileSync(join(w.stubs, flag), "");
    w.uninstall([]);
    const left = walk(w.xdg).filter((f) => f.includes("machine-removed"));
    assert.deepEqual(left, [], `${flag ?? "a clean run"}: ${left}`);
  }
});

test("a rerun over a changed file makes a new backup even when an older identical one exists", () => {
  const w = world();
  w.put("date", STUB_DATE);
  const dateFile = join(w.root, "epoch");
  const env = { DATE_FILE: dateFile };
  writeFileSync(dateFile, "1700000001\n");
  assert.equal(w.install(["--local"], env).status, 0);
  const target = join(w.caoHome, "mods", "cao-window-side", "logic.js");
  const edited = `${readFileSync(target, "utf8")}// mine\n`;
  writeFileSync(target, edited);
  assert.equal(w.install(["--local"], env).status, 0);
  writeFileSync(target, edited);                                   // the same edit again, a second later
  writeFileSync(dateFile, "1700000002\n");
  assert.equal(w.install(["--local"], env).status, 0);
  const dir = join(w.caoHome, "mods", "cao-window-side");
  assert.deepEqual(baks(dir, "logic.js").sort(), ["logic.js.bak-1700000001", "logic.js.bak-1700000002"], "the second replacement was not backed up");
  for (const b of baks(dir, "logic.js")) assert.equal(readFileSync(join(dir, b), "utf8"), edited);
});

test("a user file named like a temp file next to a destination is never deleted", () => {
  const w = world();
  w.install(["--local"]);
  const dst = join(w.caoHome, "mods", "cao-window-side", "logic.js");
  const planted = [`${dst}.new.keep`, `${dst}.new.123`, `${w.launcher}.new.keep`, `${w.launcher}.new.123`];
  for (const f of planted) writeFileSync(f, "mine");
  writeFileSync(dst, `${readFileSync(dst, "utf8")}// edited\n`);        // so both destinations are written again
  writeFileSync(w.launcher, "#!/bin/sh\necho edited\n");
  const r = w.install(["--local"]);
  assert.equal(r.status, 0, r.text);
  assert.match(readFileSync(dst, "utf8"), /^(?!.*\/\/ edited)/s);
  for (const f of planted) assert.ok(existsSync(f) && readFileSync(f, "utf8") === "mine", `${f} was deleted`);
});

test("a run stopped by SIGTERM while it writes removes its own temp file and nothing else", () => {
  const w = world();
  w.install(["--local"]);
  const dst = join(w.caoHome, "layouts", "cao-window.json");
  const keep = `${dst}.new.keep`;
  writeFileSync(keep, "mine");
  w.put("mv", `#!/bin/sh
for last; do :; done
case $last in
  *layouts/cao-window.json) [ -f "$MV_TRIGGER" ] && { kill -TERM $PPID; exit 0; } ;;
esac
exec ${REAL_MV} "$@"
`);
  const trigger = join(w.root, "mv.trigger");
  writeFileSync(trigger, "term");
  const r = w.install(["--local", "--cao-port", "9999"], { MV_TRIGGER: trigger });
  assert.notEqual(r.status, 0, r.text);
  const temps = files(w.caoHome).filter((f) => /\.new\./.test(f) && f !== relative(w.caoHome, keep));
  assert.deepEqual(temps, [], "the run left its own temp file behind");
  assert.equal(readFileSync(keep, "utf8"), "mine");
  assert.deepEqual(w.tmpLeft(), [], "the run left its scratch folder behind");
});

test("a failed write removes the run's own temp file and keeps the user's", () => {
  const w = world();
  w.install(["--local"]);
  const dst = join(w.caoHome, "layouts", "cao-window.json");
  const keep = `${dst}.new.keep`;
  writeFileSync(keep, "mine");
  w.put("mv", `#!/bin/sh
for last; do :; done
case $last in
  *layouts/cao-window.json) [ -f "$MV_TRIGGER" ] && exit 1 ;;
esac
exec ${REAL_MV} "$@"
`);
  const trigger = join(w.root, "mv.trigger");
  writeFileSync(trigger, "fail");
  assert.notEqual(w.install(["--local", "--cao-port", "9999"], { MV_TRIGGER: trigger }).status, 0);
  assert.ok(existsSync(keep), "the user's file was deleted");
  const temps = files(w.caoHome).filter((f) => /\.new\./.test(f) && f !== relative(w.caoHome, keep));
  assert.deepEqual(temps, []);
  assert.equal(readFileSync(keep, "utf8"), "mine");
});

test("a --home inside the main home or inside $HERDR_APP_HOME is refused and nothing is written", () => {
  const w = world();
  withMainHome(w);
  const mainBefore = snapshot(w.mainHome);
  const appHome = join(w.root, "app-home");
  const cases = [
    [["--home", join(w.mainHome, "sub")], {}, "inside the main home"],
    [["--home", join(w.mainHome, "layouts")], {}, "inside the main home, over its layouts folder"],
    [["--home", join(appHome, "sub")], { HERDR_APP_HOME: appHome }, "inside $HERDR_APP_HOME"],
    [["--home", join(appHome, "a", "b")], { HERDR_APP_HOME: appHome }, "deep inside $HERDR_APP_HOME"],
    [["--home", join(w.root, "elsewhere", "..", "app-home", "sub")], { HERDR_APP_HOME: appHome }, "inside $HERDR_APP_HOME, spelled through .."],
  ];
  for (const [args, env, why] of cases) {
    const r = w.install(["--local", ...args], env);
    assert.equal(r.status, 2, `${why}: the install went ahead\n${r.text}`);
    assert.ok(r.err.trim().length > 0, why);
  }
  assert.deepEqual(w.adds(), []);
  assert.deepEqual(snapshot(w.mainHome), mainBefore, "the main home was written");
  assert.ok(!existsSync(appHome));
  assert.deepEqual(w.tmpLeft(), []);
});

test("uninstall refuses a --home that is, holds or lies inside the main home or $HERDR_APP_HOME, and changes nothing", () => {
  const w = world();
  withMainHome(w);
  const appHome = join(w.root, "outer", "app-home");     // "outer" holds the app home but neither $HOME nor the main home
  const manifest = JSON.stringify({ core: "caowin", caoCore: "cao", machine: "cao", binDir: w.root, launcher: "x", files: [] });
  const homes = [
    [join(w.mainHome, "sub"), {}, "inside the main home", false],
    [w.mainHome, {}, "the main home", false],
    [join(appHome, "sub"), { HERDR_APP_HOME: appHome }, "inside $HERDR_APP_HOME", false],
    [dirname(appHome), { HERDR_APP_HOME: appHome }, "holding $HERDR_APP_HOME", false],
    [appHome, { HERDR_APP_HOME: appHome }, "$HERDR_APP_HOME itself", true],
  ];
  for (const [home, env, why, saysAnotherTerminal] of homes) {
    mkdirSync(home, { recursive: true });
    writeFileSync(join(home, "cao-window-install.json"), manifest);
    writeFileSync(join(home, "keep.txt"), "mine");
    const mark = w.log().length;
    const r = w.uninstall(["--home", home], env);
    assert.equal(r.status, 2, `${why}: uninstall went ahead\n${r.text}`);
    // Only the home the running window uses needs the hint: the shell is inside the window being removed.
    if (saysAnotherTerminal) assert.match(r.err, /another terminal/i, `${why}: the refusal does not say where to run uninstall`);
    else assert.doesNotMatch(r.err, /another terminal/i, `${why}: the refusal gives the run-it-elsewhere advice, which does not apply`);
    assert.deepEqual(w.log().slice(mark), [], `${why}: a tool was called`);
    assert.equal(readFileSync(join(home, "keep.txt"), "utf8"), "mine", why);
    assert.ok(!readdirSync(dirname(home)).some((n) => /\.removed-/.test(n)), `${why}: the folder was moved aside`);
  }
});

test("an empty --bin-dir or --app is refused with a message and nothing is written", () => {
  for (const args of [["--bin-dir", ""], ["--app", ""], ["--bin-dir", "", "--app", ""]]) {
    const w = world();
    const r = w.install(["--local", ...args]);
    assert.equal(r.status, 2, `${args.join(" ")}: ${r.text}`);
    assert.match(r.err, /--bin-dir|--app/);
    nothingWritten(w);
    assert.deepEqual(walk(w.root).filter((f) => f === "herdr-cao" || f.startsWith("herdr-app-cao")), [], "a launcher or home was written into the working directory");
    assert.deepEqual(w.tmpLeft(), []);
  }
});

test("an empty --home or --main-home is refused too, instead of meaning the working directory", () => {
  for (const args of [["--home", ""], ["--main-home", ""]]) {
    const w = world();
    const cwd = join(w.root, "work");                 // a folder that holds neither $HOME nor the main home
    mkdirSync(cwd);
    const r = w.install(["--local", ...args], {}, cwd);
    assert.equal(r.status, 2, `${args.join(" ")} was accepted: ${r.text}`);
    nothingWritten(w);
    assert.deepEqual(walk(cwd), [], "files were written into the working directory");
  }
});

// ---------------------------------------------------------------------------------------------
// Backups never overwrite a backup
// ---------------------------------------------------------------------------------------------

// Every backup of a file: <name>.bak-<epoch> and <name>.bak-<epoch>-<n>.
const anyBaks = (dir, name) => files(dir).filter((f) => new RegExp(`(^|/)${name.replace(/\./g, "\\.")}\\.bak-\\d+(-\\d+)?$`).test(f)).sort();
// A world whose `date +%s` is pinned, so several runs fall in one second.
function pinnedWorld(epoch) {
  const w = world();
  w.put("date", STUB_DATE);
  const dateFile = join(w.root, "epoch");
  writeFileSync(dateFile, `${epoch}\n`);
  return { w, env: { DATE_FILE: dateFile } };
}

test("two runs in the same second leave two distinct backups, and the first is not overwritten", () => {
  const { w, env } = pinnedWorld(1700000007);
  assert.equal(w.install(["--local"], env).status, 0);
  const dir = join(w.caoHome, "mods", "cao-window-side");
  const target = join(dir, "logic.js");
  const shipped = readFileSync(target, "utf8");
  const first = `${shipped}// first edit\n`;
  const second = `${shipped}// second edit\n`;
  writeFileSync(target, first);
  assert.equal(w.install(["--local"], env).status, 0);
  assert.equal(readFileSync(join(dir, "logic.js.bak-1700000007"), "utf8"), first);
  writeFileSync(target, second);
  const r = w.install(["--local"], env);
  assert.equal(r.status, 0, r.text);
  assert.deepEqual(anyBaks(dir, "logic.js"), ["logic.js.bak-1700000007", "logic.js.bak-1700000007-1"], "the second edit was not kept as its own backup");
  assert.equal(readFileSync(join(dir, "logic.js.bak-1700000007"), "utf8"), first, "the first backup was overwritten");
  assert.equal(readFileSync(join(dir, "logic.js.bak-1700000007-1"), "utf8"), second);
  assert.equal(readFileSync(target, "utf8"), shipped);
  assert.ok(r.out.includes("logic.js.bak-1700000007-1"), "the run does not say which file holds the old content");
});

test("a third run in the same second takes the next free number, and a freed number is used again first", () => {
  const { w, env } = pinnedWorld(1700000009);
  assert.equal(w.install(["--local"], env).status, 0);
  const dir = join(w.caoHome, "mods", "cao-window-side");
  const target = join(dir, "logic.js");
  const shipped = readFileSync(target, "utf8");
  const edits = ["one", "two", "three"].map((n) => `${shipped}// ${n}\n`);
  for (const text of edits) {
    writeFileSync(target, text);
    assert.equal(w.install(["--local"], env).status, 0);
  }
  assert.deepEqual(anyBaks(dir, "logic.js"), ["logic.js.bak-1700000009", "logic.js.bak-1700000009-1", "logic.js.bak-1700000009-2"]);
  edits.forEach((text, i) => assert.equal(readFileSync(join(dir, `logic.js.bak-1700000009${i ? `-${i}` : ""}`), "utf8"), text, `backup ${i}`));
  rmSync(join(dir, "logic.js.bak-1700000009-1"));
  const fourth = `${shipped}// four\n`;
  writeFileSync(target, fourth);
  assert.equal(w.install(["--local"], env).status, 0);
  assert.equal(readFileSync(join(dir, "logic.js.bak-1700000009-1"), "utf8"), fourth, "the first free number was not used");
  assert.equal(readFileSync(join(dir, "logic.js.bak-1700000009"), "utf8"), edits[0]);
  assert.equal(readFileSync(join(dir, "logic.js.bak-1700000009-2"), "utf8"), edits[2]);
});

test("the launcher's backups follow the same rule within one second", () => {
  const { w, env } = pinnedWorld(1700000011);
  assert.equal(w.install(["--local"], env).status, 0);
  const dir = dirname(w.launcher);
  const texts = ["#!/bin/sh\necho mine one\n", "#!/bin/sh\necho mine two\n"];
  for (const text of texts) {
    writeFileSync(w.launcher, text);
    assert.equal(w.install(["--local"], env).status, 0);
  }
  assert.deepEqual(anyBaks(dir, "herdr-cao"), ["herdr-cao.bak-1700000011", "herdr-cao.bak-1700000011-1"]);
  assert.equal(readFileSync(join(dir, "herdr-cao.bak-1700000011"), "utf8"), texts[0]);
  assert.equal(readFileSync(join(dir, "herdr-cao.bak-1700000011-1"), "utf8"), texts[1]);
});

test("a backup name that a user file already holds is not overwritten either", () => {
  const { w, env } = pinnedWorld(1700000013);
  assert.equal(w.install(["--local"], env).status, 0);
  const dir = join(w.caoHome, "mods", "cao-window-side");
  const target = join(dir, "logic.js");
  writeFileSync(join(dir, "logic.js.bak-1700000013"), "the user's own backup");
  writeFileSync(target, `${readFileSync(target, "utf8")}// edit\n`);
  assert.equal(w.install(["--local"], env).status, 0);
  assert.equal(readFileSync(join(dir, "logic.js.bak-1700000013"), "utf8"), "the user's own backup");
  assert.deepEqual(anyBaks(dir, "logic.js"), ["logic.js.bak-1700000013", "logic.js.bak-1700000013-1"]);
});

// ---------------------------------------------------------------------------------------------
// The manifest is read again before uninstall removes or stops anything, and the home is private
// ---------------------------------------------------------------------------------------------

const MANIFEST_FILE = "cao-window-install.json";
// Installs, then rewrites the manifest with `patch`, as an edit by hand or another program would.
function tamper(patch) {
  const w = world();
  assert.equal(w.install(["--local"]).status, 0);
  const manifest = w.json(MANIFEST_FILE);
  writeFileSync(join(w.caoHome, MANIFEST_FILE), JSON.stringify({ ...manifest, ...patch }));
  return { w, manifest };
}
const nameRule = (v) => !/^$|^\.{1,2}$|^-|[^A-Za-z0-9._-]/.test(v);   // install.sh valid_name

test("uninstall refuses a launcher name that holds a slash or is . or .. : exit 2, no hc call, nothing deleted", () => {
  const cases = [
    ["a/b", (w) => join(w.launcher, "..", "a", "b")],
    ["../decoy", (w) => join(dirname(w.launcher), "..", "decoy")],
    ["sub/../herdr-cao", () => null],
    ["herdr-cao/", () => null],
    ["..", () => null],
    [".", () => null],
  ];
  for (const [launcher, decoyAt] of cases) {
    const { w } = tamper({ launcher });
    const decoy = decoyAt(w);
    if (decoy) { mkdirSync(dirname(decoy), { recursive: true }); writeFileSync(decoy, "mine"); }
    const mark = w.log().length;
    const r = w.uninstall([]);
    assert.equal(r.status, 2, `${JSON.stringify(launcher)}: ${r.text}`);
    assert.deepEqual(w.log().slice(mark), [], `${JSON.stringify(launcher)}: uninstall called a tool before refusing`);
    assert.ok(existsSync(w.caoHome), `${JSON.stringify(launcher)}: the home was moved`);
    assert.ok(existsSync(w.launcher), `${JSON.stringify(launcher)}: the real launcher was removed`);
    if (decoy) assert.equal(readFileSync(decoy, "utf8"), "mine", `${JSON.stringify(launcher)}: a file outside the launcher folder was removed`);
  }
});

test("uninstall refuses an absolute launcher path in the manifest and leaves that file alone", () => {
  const outside = join(mkTmp("cao-window-outside-"), "herdr-cao");
  writeFileSync(outside, "mine");
  const { w } = tamper({ launcher: outside });
  const mark = w.log().length;
  const r = w.uninstall([]);
  assert.equal(r.status, 2, r.text);
  assert.deepEqual(w.log().slice(mark), []);
  assert.equal(readFileSync(outside, "utf8"), "mine");
  assert.ok(existsSync(w.launcher) && existsSync(w.caoHome));
});

const BAD_NAMES = ["a b", "a/b", ".", "..", "a;b", "$(touch x)", "a`b", "a\tb", "a'b", 'a"b', "a*b", "a\nb", "caf\u00e9", "\u202ex", "a\\b"];

for (const key of ["core", "machine"]) {
  test(`uninstall refuses a ${key} name that install.sh would refuse: exit 2, no hc call, nothing removed`, () => {
    for (const bad of [...BAD_NAMES, ...(key === "core" ? ["-x"] : []), ["caowin"], { a: 1 }]) {
      assert.ok(typeof bad !== "string" || !nameRule(bad), `${JSON.stringify(bad)} is a bad name`);
      const { w } = tamper({ [key]: bad });
      const mark = w.log().length;
      const r = w.uninstall([]);
      assert.equal(r.status, 2, `${key} ${JSON.stringify(bad)}: ${r.text}`);
      assert.deepEqual(w.log().slice(mark), [], `${key} ${JSON.stringify(bad)}: uninstall called a tool before refusing`);
      assert.ok(existsSync(w.caoHome) && existsSync(w.launcher), `${key} ${JSON.stringify(bad)}: something was removed`);
    }
  });
}

test("uninstall's refusal of a tampered manifest says which value it refused and does not run it", () => {
  const { w } = tamper({ machine: "$(touch pwned)" });
  const r = w.uninstall([], {}, w.root);
  assert.equal(r.status, 2);
  assert.match(r.err, /machine/i);
  assert.ok(!existsSync(join(w.root, "pwned")));
});

// ---- the window's home is private ----

const modeOf = (path) => statSync(path).mode & 0o777;
function installUnder(mask, args = ["--local"], world_ = world()) {
  const before = process.umask(mask);   // the child shell inherits it
  try { return { w: world_, r: world_.install(args) }; } finally { process.umask(before); }
}

for (const mask of [0o022, 0o002, 0o000]) {
  test(`the window's home is mode 700 after a fresh install under umask ${mask.toString(8).padStart(3, "0")}, and its files stay readable by the owner`, () => {
    const { w, r } = installUnder(mask);
    assert.equal(r.status, 0, r.text);
    assert.equal(modeOf(w.caoHome).toString(8), "700");
    assert.ok(existsSync(join(w.caoHome, MANIFEST_FILE)));
    for (const f of files(w.caoHome)) assert.ok(modeOf(join(w.caoHome, f)) & 0o400, `${f} is not readable by its owner`);
    for (const e of readdirSync(w.caoHome, { recursive: true, withFileTypes: true }).filter((x) => x.isDirectory())) {
      assert.equal(modeOf(join(e.parentPath ?? e.path, e.name)) & 0o700, 0o700, `${e.name} cannot be entered by its owner`);
    }
  });
}

test("a --home with a space and a home the install did not create alone are mode 700 too", () => {
  const w = world();
  const home = join(w.root, "my home");
  const { r } = installUnder(0o022, ["--local", "--home", home], w);
  assert.equal(r.status, 0, r.text);
  assert.equal(modeOf(home).toString(8), "700");
});

test("an install over a home that an earlier run left at 755 makes it 700 again, whether or not anything else changed", () => {
  const w = world();
  assert.equal(installUnder(0o022, ["--local"], w).r.status, 0);
  chmodSync(w.caoHome, 0o755);
  const again = installUnder(0o022, ["--local"], w).r;
  assert.equal(again.status, 0, again.text);
  assert.equal(modeOf(w.caoHome).toString(8), "700");
});

test("the BSD chmod stub fails on a -- after the mode, so the next case can catch the form", () => {
  const w = world();
  mkdirSync(join(w.root, "d"));
  const run = (args) => spawnSync("chmod", args, { env: w.env, encoding: "utf8" });
  const bad = run(["700", "--", join(w.root, "d")]);
  assert.equal(bad.status, 1);
  assert.match(bad.stderr, /chmod: --: No such file or directory/);
  assert.equal(run(["700", join(w.root, "d")]).status, 0);
  assert.equal(run(["-R", "700", join(w.root, "d")]).status, 0);
  assert.equal(run(["--", "700", join(w.root, "d")]).status, 0);
});

test("the install passes where chmod is macOS's (no -- after the mode): exit 0, home 700, launcher and manifest written", () => {
  const w = world();
  const { r } = installUnder(0o022, ["--local"], w);
  assert.equal(r.status, 0, r.text);
  assert.doesNotMatch(r.text, /cannot make .* private/);
  assert.equal(modeOf(w.caoHome).toString(8), "700");
  assert.ok(existsSync(join(w.caoHome, MANIFEST_FILE)));
  assert.ok(existsSync(w.launcher));
  const calls = readFileSync(`${w.calls}.chmod`, "utf8").split("\n").filter(Boolean);
  assert.ok(calls.includes(`700 ${w.caoHome}`), `the home was never made private through chmod: ${calls.join(" | ")}`);
  assert.ok(calls.every((c) => !/ -- /.test(c) && !c.startsWith("700 --")), `a call puts -- after the mode: ${calls.join(" | ")}`);
});

test("uninstall accepts every name install.sh accepts: capitals, digits first, dots, dashes and underscores in the core, the machine and the launcher", () => {
  const names = [["2Win.x_y-z", "M.1_q-R", "herdr.cao_2-x"], ["a", "b", "c"], ["W_", "m.", "l-"]];
  for (const [core, machine, launcherName] of names) {
    const w = world();
    const bin = join(w.root, "bin2");
    const home = join(w.root, "home2");
    const i = w.install(["--local", "--core-session", core, "--machine", machine, "--bin-dir", bin, "--launcher-name", launcherName, "--home", home]);
    assert.equal(i.status, 0, `${core} ${machine} ${launcherName}: ${i.text}`);
    const mark = w.log().length;
    const r = w.uninstall(["--home", home]);
    assert.equal(r.status, 0, `${core} ${machine} ${launcherName}: ${r.text}`);
    assert.ok(w.log().slice(mark).includes(`HC_SESSION=${core} hc machine rm ${machine}`), w.log().slice(mark).join("\n"));
    assert.ok(w.log().slice(mark).includes(`HC_SESSION=${core} hc server stop`));
    assert.ok(!existsSync(join(bin, launcherName)), "the launcher is gone");
  }
});

// ---- uninstall removes a launcher only when it is the one install.sh wrote ----
// install.sh writes the launcher from bin/herdr-cao.in, whose header carries this line. A file at the launcher path
// that does not carry it is somebody else's: it is left where it is with a note naming it, and the uninstall goes on
// (the machine, the core and the home are the window's own, and the launcher is only a convenience). Chosen where the
// spec says only "remove the launcher" under "removes only what the installer made".
const MARKER = readFileSync(join(here, "bin", "herdr-cao.in"), "utf8").split("\n").find((l) => l.startsWith("# Written by "));

test("the launcher install.sh writes carries the marker line, as a whole line", () => {
  assert.ok(MARKER && MARKER.includes("install.sh"), "the template has a marker line");
  const w = world();
  assert.equal(w.install(["--local"]).status, 0);
  assert.ok(readFileSync(w.launcher, "utf8").split("\n").includes(MARKER));
});

test("uninstall removes the launcher the installer wrote even when the user added lines to it, because it still carries the marker", () => {
  const w = world();
  assert.equal(w.install(["--local"]).status, 0);
  writeFileSync(w.launcher, `${readFileSync(w.launcher, "utf8")}# my own note\n`);
  const r = w.uninstall([]);
  assert.equal(r.status, 0, r.text);
  assert.ok(!existsSync(w.launcher));
});

test("a symlink at the launcher path to a file that is not the launcher never costs that file, and the uninstall completes", () => {
  const target = join(mkTmp("cao-window-target-"), "notes.txt");
  writeFileSync(target, "precious");
  const w = world();
  assert.equal(w.install(["--local"]).status, 0);
  rmSync(w.launcher);
  symlinkSync(target, w.launcher);
  const r = w.uninstall([]);
  assert.equal(r.status, 0, r.text);
  assert.equal(readFileSync(target, "utf8"), "precious");
});

test("uninstall leaves a file at the launcher path that lacks the marker line, says so, and still completes", () => {
  const others = ["", "#!/bin/sh\necho mine\n", "# Written by someone else\n", `#!/bin/sh\n${MARKER.toLowerCase()}\n`, `#!/bin/sh\n# not ${MARKER}\n`];
  for (const text of others) {
    const w = world();
    assert.equal(w.install(["--local"]).status, 0);
    writeFileSync(w.launcher, text);
    const mark = w.log().length;
    const r = w.uninstall([]);
    assert.equal(r.status, 0, `${JSON.stringify(text)}: ${r.text}`);
    assert.ok(existsSync(w.launcher), `${JSON.stringify(text)}: the file was removed`);
    assert.equal(readFileSync(w.launcher, "utf8"), text, `${JSON.stringify(text)}: the file was changed`);
    assert.ok(r.err.includes(w.launcher), `${JSON.stringify(text)}: no note names the file:\n${r.err}`);
    assert.ok(w.log().slice(mark).includes("HC_SESSION=caowin hc server stop"), "the rest of the uninstall ran");
    assert.ok(!existsSync(w.caoHome), "the home was moved");
  }
});

test("a launcher name in the manifest that points at a file which is not the launcher does not delete it", () => {
  const notes = join(mkTmp("cao-window-notes-"), "notes.txt");
  writeFileSync(notes, "precious");
  const { w } = tamper({ binDir: dirname(notes), launcher: "notes.txt" });
  const r = w.uninstall([]);
  assert.ok(existsSync(notes), `the file was deleted:\n${r.text}`);
  assert.equal(r.status, 0, r.text);
  assert.equal(readFileSync(notes, "utf8"), "precious");
  assert.ok(r.err.includes(notes), r.err);
});

test("a folder at the launcher path is left with its contents, and the uninstall completes", () => {
  const w = world();
  assert.equal(w.install(["--local"]).status, 0);
  const manifest = w.json(MANIFEST_FILE);
  const dirPath = join(manifest.binDir, "launcher-dir");
  mkdirSync(dirPath);
  writeFileSync(join(dirPath, "keep"), "mine");
  writeFileSync(join(w.caoHome, MANIFEST_FILE), JSON.stringify({ ...manifest, launcher: "launcher-dir" }));
  const r = w.uninstall([]);
  assert.equal(r.status, 0, r.text);
  assert.equal(readFileSync(join(dirPath, "keep"), "utf8"), "mine");
  assert.ok(!existsSync(w.caoHome));
});

// ---------------------------------------------------------------------------------------------
// The icon of the app copy (assets/CAO.icns, built from assets/cao-icon.svg; design D40 C)
// ---------------------------------------------------------------------------------------------

const ICON = join(here, "assets", "CAO.icns");
const ICON_SVG = join(here, "assets", "cao-icon.svg");
const BRAND_MARK = join(here, "..", "..", "docusaurus", "static", "img", "cao-mark.svg");
const PNG_MAGIC = "89504e470d0a1a0a";
// The chunks of an icns file: [type, data] pairs after the 8-byte header ("icns" and the whole length).
function icnsChunks(buf) {
  assert.equal(buf.subarray(0, 4).toString("latin1"), "icns", "the file is not an icns");
  assert.equal(buf.readUInt32BE(4), buf.length, "the length in the header is not the file's size");
  const out = [];
  for (let at = 8; at < buf.length;) {
    const size = buf.readUInt32BE(at + 4);
    assert.ok(size >= 8 && at + size <= buf.length, `a chunk at ${at} runs past the file`);
    out.push([buf.subarray(at, at + 4).toString("latin1"), buf.subarray(at + 8, at + size)]);
    at += size;
  }
  return out;
}

test("icon: CAO.icns holds every size iconutil writes for 16 to 512 and @2x, each with an alpha channel", () => {
  assert.ok(existsSync(ICON), `${ICON} does not exist`);
  const byType = new Map(icnsChunks(readFileSync(ICON)));
  // 16 and 32 px are the format's own ARGB blocks ("ARGB" then packed rows); the others are PNGs.
  for (const type of ["ic04", "ic05"]) assert.equal(byType.get(type)?.subarray(0, 4).toString("latin1"), "ARGB", type);
  const dims = { ic11: 32, ic12: 64, ic07: 128, ic13: 256, ic08: 256, ic14: 512, ic09: 512, ic10: 1024 };
  for (const [type, side] of Object.entries(dims)) {
    const data = byType.get(type);
    assert.ok(data, `${type} (${side} px) is missing`);
    assert.equal(data.subarray(0, 8).toString("hex"), PNG_MAGIC, `${type} is not a PNG`);
    assert.equal(data[25], 6, `${type}: colour type ${data[25]} is not RGBA (no transparent margin)`);
    assert.deepEqual([data.readUInt32BE(16), data.readUInt32BE(20)], [side, side], type);
  }
  assert.ok(statSync(ICON).size < 400 * 1024, "the binary is a few hundred KB at most");
});

test("icon: the source svg has Herdr's tile (824 px squircle in a 1024 canvas, cream, grey edge) and its charcoal", () => {
  const svg = readFileSync(ICON_SVG, "utf8");
  assert.match(svg, /viewBox="0 0 1024 1024"/);
  assert.match(svg, /<rect x="100" y="100" width="824" height="824" rx="175"[^>]*fill="#F4F1E9"/, "the tile: 100 px margin, 824 px, cream");
  assert.match(svg, /stroke="#C9C6BD"/, "the thin light-grey edge");
  assert.match(svg, /stroke="#303438"/, "the charcoal glyph");
  assert.deepEqual([...new Set((svg.match(/<(\w+)[\s>]/g) ?? []).map((t) => t.slice(1, -1).trim()))].sort(), ["circle", "g", "path", "rect", "svg"], "only plain shapes: no embedded image");
});

test("icon: the glyph is the project's own mark, not Herdr's artwork: every coordinate of it is one of cao-mark.svg's", () => {
  const brand = readFileSync(BRAND_MARK, "utf8").replace(/<!--[\s\S]*?-->/g, "");
  const mark = readFileSync(ICON_SVG, "utf8").replace(/<!--[\s\S]*?-->/g, "").split("<g transform")[1];
  assert.ok(mark, "the glyph group is missing");
  const numbers = (text) => new Set((text.match(/-?\d+(?:\.\d+)?/g) ?? []).map(Number));
  const own = numbers(brand);
  const used = [...(mark.matchAll(/ d="([^"]*)"/g))].flatMap((m) => [...numbers(m[1])]).concat([...mark.matchAll(/c[xy]="([^"]*)"|r="([^"]*)"/g)].map((m) => Number(m[1] ?? m[2])));
  assert.ok(used.length > 20, "the glyph has coordinates to check");
  for (const n of used) assert.ok(own.has(n) || own.has(-n), `${n} is not a coordinate of cao-mark.svg`);
  for (const colour of ["#0F73E0", "#16A0B5", "#243F63"]) assert.ok(brand.includes(colour) && mark.includes(colour), `the node colour ${colour}`);
});

// ---------------------------------------------------------------------------------------------
// The app copy: ~/Applications/CAO.app, a renamed, ad-hoc signed clone of Herdr.app (design D40 B)
// ---------------------------------------------------------------------------------------------

const plistOf = (path) => Object.fromEntries(readFileSync(path, "utf8").split("\n").filter(Boolean).map((l) => [l.slice(0, l.indexOf("=")), l.slice(l.indexOf("=") + 1)]));
const copyPlistAt = (app) => plistOf(join(app, "Contents", "Info.plist"));
const copyPlist = (w) => plistOf(join(w.copyApp, "Contents", "Info.plist"));
// What the installer ran that changes a bundle (the reads of PlistBuddy Print are left out).
const BUILD_CALL = /^(cp -cR|cp -R|PlistBuddy -c (Set|Add|Delete) |xattr|codesign)/;
const buildCalls = (w) => w.log().filter((l) => BUILD_CALL.test(l));
const indexOfCall = (log, prefix, from = 0) => log.findIndex((l, i) => i >= from && l.startsWith(prefix));

test("app copy: a first install clones Herdr.app to CAO.app with the name, id and our icon, strips attributes and signs it ad hoc", () => {
  const w = world({ copy: true });
  const r = w.install(["--local"]);
  assert.equal(r.status, 0, r.text);
  const info = copyPlist(w);
  assert.deepEqual([info.CFBundleName, info.CFBundleDisplayName, info.CFBundleIdentifier, info.CFBundleIconFile, info.CAOWindowSourceVersion],
    ["CAO", "CAO", "dev.herdr.app.cao", "CAO.icns", "0.1.16"]);
  assert.equal(info.CFBundleExecutable, "Herdr", "the program inside is Herdr's, under its own name");
  assert.equal(info.CFBundleShortVersionString, "0.1.16");
  assert.ok(existsSync(join(w.copyApp, "Contents", "MacOS", "Herdr")), "the program was cloned");
  assert.equal(sha(join(w.copyApp, "Contents", "Resources", "CAO.icns")), sha(ICON), "the icon is assets/CAO.icns, byte for byte");
  assert.deepEqual(plistOf(join(w.sourceApp, "Contents", "Info.plist")).CFBundleName, "Herdr", "Herdr.app itself is not edited");
  assert.equal(readFileSync(join(w.sourceApp, "Contents", "_CodeSignature", "CodeResources"), "utf8"), "original signature\n");
  assert.ok(!existsSync(join(w.sourceApp, "Contents", "Resources", "CAO.icns")));
  const log = w.log();
  const at = (prefix, from = 0) => indexOfCall(log, prefix, from);
  const clone = at(`cp -cR ${w.sourceApp} `);
  assert.ok(clone >= 0, log.join("\n"));
  const edit = at("PlistBuddy -c Set :CFBundleName CAO ", clone);
  const strip = at("xattr -cr ", edit);
  const sign = at("codesign --force --deep -s - ", strip);
  assert.ok(clone < edit && edit < strip && strip < sign, `order: ${[clone, edit, strip, sign]}\n${log.join("\n")}`);
  assert.ok(log[sign].endsWith(" -s - " + log[strip].slice("xattr -cr ".length)), "the same bundle is stripped and signed");
  assert.ok(at("codesign -v ", sign) > sign, "the signature is checked after it is made");
  assert.deepEqual(readdirSync(w.copyDir), ["CAO.app"], "no temporary folder is left next to it");
  assert.match(r.out, /install: built .*CAO\.app/);
});

test("app copy: the Herdr bundle is cloned as the plain recursive copy when the clone form fails (a volume that cannot clone)", () => {
  const w = world({ copy: true });
  writeFileSync(join(w.stubs, "cp.noclone"), "");
  const r = w.install(["--local"]);
  assert.equal(r.status, 0, r.text);
  const log = w.log();
  const clone = indexOfCall(log, "cp -cR ");
  const plain = indexOfCall(log, `cp -R ${w.sourceApp} `, clone);
  assert.ok(clone >= 0 && plain > clone, log.join("\n"));
  assert.equal(copyPlist(w).CFBundleName, "CAO");
  assert.ok(existsSync(join(w.copyApp, "Contents", "MacOS", "Herdr")));
});

test("app copy: a rerun with the same Herdr version is up to date and writes nothing", () => {
  const w = world({ copy: true });
  assert.equal(w.install(["--local"]).status, 0);
  const copyBefore = snapshot(w.copyDir), homeBefore = snapshot(w.caoHome), mark = w.log().length;
  const r = w.install(["--local"]);
  assert.equal(r.status, 0, r.text);
  assert.match(r.out, /install: up to date/);
  assert.deepEqual(snapshot(w.copyDir), copyBefore, "the copy was touched");
  assert.deepEqual(snapshot(w.caoHome), homeBefore);
  assert.deepEqual(w.log().slice(mark).filter((l) => BUILD_CALL.test(l)), [], "a tool that changes a bundle ran again");
});

test("app copy: a changed Herdr version rebuilds the copy from the new app and leaves nothing of the old one", () => {
  const w = world({ copy: true });
  assert.equal(w.install(["--local"]).status, 0);
  writeFileSync(join(w.copyApp, "Contents", "stale.txt"), "from the old build\n");
  writeFileSync(join(w.stubs, "app.version"), "0.1.17\n");
  writeFileSync(join(w.sourceApp, "Contents", "Info.plist"), readFileSync(join(w.sourceApp, "Contents", "Info.plist"), "utf8").replace("0.1.16", "0.1.17"));
  const mark = w.log().length;
  const r = w.install(["--local"]);
  assert.equal(r.status, 0, r.text);
  assert.match(r.out, /install: built .*CAO\.app/);
  assert.equal(copyPlist(w).CAOWindowSourceVersion, "0.1.17");
  assert.equal(copyPlist(w).CFBundleShortVersionString, "0.1.17", "the bundle is the new one");
  assert.ok(!existsSync(join(w.copyApp, "Contents", "stale.txt")), "a file of the old build is still there");
  assert.ok(w.log().slice(mark).some((l) => l.startsWith("codesign --force --deep -s - ")), "the new copy was signed");
  assert.deepEqual(readdirSync(w.copyDir), ["CAO.app"]);
  assert.equal(w.json(MANIFEST_FILE).appCopy.sourceVersion, "0.1.17");
});

test("app copy: a missing copy is built again on a rerun", () => {
  const w = world({ copy: true });
  assert.equal(w.install(["--local"]).status, 0);
  rmSync(w.copyApp, { recursive: true });
  const r = w.install(["--local"]);
  assert.equal(r.status, 0, r.text);
  assert.match(r.out, /install: built .*CAO\.app/);
  assert.equal(copyPlist(w).CFBundleName, "CAO");
});

test("app copy: a CAO.app that is not ours at the target refuses with exit 2 and writes nothing", () => {
  for (const kind of ["bundle without our key", "plain file", "folder without a plist", "symlink to a bundle with our key"]) {
    const w = world({ copy: true });
    mkdirSync(w.copyDir);
    if (kind === "plain file") writeFileSync(w.copyApp, "mine\n");
    else if (kind === "folder without a plist") { mkdirSync(w.copyApp); writeFileSync(join(w.copyApp, "keep"), "mine\n"); }
    else if (kind === "bundle without our key") {
      mkdirSync(join(w.copyApp, "Contents"), { recursive: true });
      writeFileSync(join(w.copyApp, "Contents", "Info.plist"), "CFBundleName=Somebody's CAO\n");
      writeFileSync(join(w.copyApp, "keep"), "mine\n");
    } else {
      const real = join(w.root, "elsewhere.app");
      mkdirSync(join(real, "Contents"), { recursive: true });
      writeFileSync(join(real, "Contents", "Info.plist"), "CAOWindowSourceVersion=0.1.16\n");
      symlinkSync(real, w.copyApp);
    }
    const before = snapshot(w.copyDir);
    const r = w.install(["--local"]);
    assert.equal(r.status, 2, `${kind}: ${r.text}`);
    assert.match(r.err, /CAO\.app/, kind);
    assert.match(r.err, /not a copy this installer made/, kind);
    assert.match(r.err, /nothing was written/, kind);
    assert.deepEqual(snapshot(w.copyDir), before, `${kind}: the target was changed`);
    nothingWritten(w);
    assert.deepEqual(buildCalls(w), [], `${kind}: a bundle tool ran`);
    assert.deepEqual(w.tmpLeft(), [], `${kind}: a temp file was left`);
  }
});

test("app copy: --no-app-copy builds nothing, and the launcher keeps opening Herdr.app", () => {
  const w = world({ copy: true });
  const r = w.install(["--local", "--no-app-copy"]);
  assert.equal(r.status, 0, r.text);
  assert.ok(!existsSync(w.copyDir), "a folder for the copy was made");
  assert.deepEqual(buildCalls(w), []);
  assert.equal(w.json(MANIFEST_FILE).appCopy, null);
  const t = launcherArgv(w, w.launcher);
  assert.deepEqual(t.argv.slice(0, 3), ["-n", "-a", w.sourceApp]);
  assert.ok(!readFileSync(w.launcher, "utf8").includes("CAO.app"));
});

test("app copy: the manifest records the copy, the Herdr version it came from and the hash of the icon it holds", () => {
  const w = world({ copy: true });
  assert.equal(w.install(["--local"]).status, 0);
  assert.deepEqual(w.json(MANIFEST_FILE).appCopy, { path: w.copyApp, sourceVersion: "0.1.16", iconSha256: sha(ICON) });
  assert.equal(sha(join(w.copyApp, "Contents", "Resources", "CAO.icns")), w.json(MANIFEST_FILE).appCopy.iconSha256, "the icon in the copy is the one listed");
});

test("app copy: the launcher opens the copy when it exists and Herdr.app when it does not", () => {
  const w = world({ copy: true });
  assert.equal(w.install(["--local", "--core-session", "mywin"]).status, 0);
  const text = readFileSync(w.launcher, "utf8");
  assert.ok(text.includes(w.copyApp) && text.includes(w.sourceApp), text);
  assert.deepEqual(launcherArgv(w, w.launcher).argv, ["-n", "-a", w.copyApp, "--env", `HERDR_APP_HOME=${w.caoHome}`, "--env", "HC_SESSION=mywin"]);
  rmSync(w.copyApp, { recursive: true });
  assert.deepEqual(launcherArgv(w, w.launcher).argv, ["-n", "-a", w.sourceApp, "--env", `HERDR_APP_HOME=${w.caoHome}`, "--env", "HC_SESSION=mywin"]);
});

test("app copy: the default folder is Applications in the user's home, and --app-copy-dir may hold spaces", () => {
  const w = world({ copy: true });
  assert.equal(w.run(INSTALL, ["--local", "--app", w.sourceApp]).status, 0);
  assert.ok(existsSync(join(w.home, "Applications", "CAO.app", "Contents", "Info.plist")));
  const spaced = join(w.root, "my apps", "x y");
  assert.equal(w.install(["--local", "--app-copy-dir", spaced]).status, 0);
  assert.equal(copyPlistAt(join(spaced, "CAO.app")).CFBundleName, "CAO");
  assert.deepEqual(launcherArgv(w, w.launcher).argv.slice(0, 3), ["-n", "-a", join(spaced, "CAO.app")]);
});

test("app copy: a signing failure exits 1 and leaves no copy, no temporary folder and no installed file", () => {
  const w = world({ copy: true });
  writeFileSync(join(w.stubs, "codesign.fail"), "");
  const r = w.install(["--local"]);
  assert.equal(r.status, 1, r.text);
  assert.match(r.err, /codesign|sign/);
  assert.ok(!existsSync(w.copyApp));
  assert.deepEqual(existsSync(w.copyDir) ? readdirSync(w.copyDir) : [], [], "a half-built folder was left");
  assert.deepEqual(files(w.caoHome), [], "files were written although the copy failed");
  assert.ok(!existsSync(w.launcher));
  assert.deepEqual(w.tmpLeft(), []);
});

test("app copy: a rebuild that fails keeps the old copy", () => {
  const w = world({ copy: true });
  assert.equal(w.install(["--local"]).status, 0);
  writeFileSync(join(w.stubs, "app.version"), "0.1.17\n");
  writeFileSync(join(w.stubs, "codesign.fail"), "");
  const r = w.install(["--local"]);
  assert.equal(r.status, 1, r.text);
  assert.equal(copyPlist(w).CAOWindowSourceVersion, "0.1.16", "the old copy was replaced by a failed one");
  assert.deepEqual(readdirSync(w.copyDir), ["CAO.app"]);
});

test("app copy: a Herdr app that is not a bundle, or a folder that would hold the copy inside Herdr.app, refuses with exit 2 and says --no-app-copy", () => {
  const w = world({ copy: true });
  const r = w.install(["--local", "--app", join(w.root, "Missing.app")]);
  assert.equal(r.status, 2, r.text);
  assert.match(r.err, /--no-app-copy/);
  nothingWritten(w);
  const inside = w.install(["--local", "--app-copy-dir", join(w.sourceApp, "Contents")]);
  assert.equal(inside.status, 2, inside.text);
  assert.match(inside.err, /inside the Herdr app/);
  assert.ok(!existsSync(join(w.sourceApp, "Contents", "CAO.app")));
  nothingWritten(w);
});

test("app copy: a missing codesign, xattr or PlistBuddy stops the install before anything is written", () => {
  for (const tool of ["codesign", "xattr", "PlistBuddy"]) {
    const w = world({ copy: true });
    rmSync(join(w.stubs, tool));
    const r = w.install(["--local"], { PATH: `${w.stubs}:${JQ_DIR}:/usr/bin:/bin` });
    const real = spawnSync("sh", ["-c", `PATH=${JSON.stringify(`${w.stubs}:${JQ_DIR}:/usr/bin:/bin`)} command -v ${tool}`], { encoding: "utf8" }).stdout.trim();
    if (real || tool === "PlistBuddy" && existsSync("/usr/libexec/PlistBuddy")) continue;   // the tool exists on this machine: nothing to refuse
    assert.equal(r.status, 3, `${tool}: ${r.text}`);
    assert.match(r.err, new RegExp(tool));
    assert.match(r.err, /--no-app-copy/);
    nothingWritten(w);
  }
});

test("app copy: Linux still exits 4 and makes no copy", () => {
  const w = world({ os: "Linux", copy: true });
  const r = w.install(["--local"]);
  assert.equal(r.status, 4, r.text);
  assert.ok(!existsSync(w.copyDir));
  nothingWritten(w);
});

test("app copy: uninstall removes the copy, with the launcher and the home, and leaves Herdr.app alone", () => {
  const w = world({ copy: true });
  assert.equal(w.install(["--local"]).status, 0);
  const sourceBefore = snapshot(w.sourceApp);
  const r = w.uninstall([]);
  assert.equal(r.status, 0, r.text);
  assert.ok(!existsSync(w.copyApp), "the copy is still there");
  assert.ok(!existsSync(w.launcher));
  assert.ok(!existsSync(w.caoHome));
  assert.deepEqual(snapshot(w.sourceApp), sourceBefore, "Herdr.app was touched");
  assert.ok(existsSync(w.copyDir), "the folder that held the copy is the user's: it stays");
  assert.match(r.out, /removed .*CAO\.app/);
});

test("app copy: uninstall refuses a path that is not a folder named *.app carrying our key, and changes nothing", () => {
  const cases = {
    "a .app folder without our key": (w) => { mkdirSync(join(w.root, "Other.app", "Contents"), { recursive: true }); writeFileSync(join(w.root, "Other.app", "Contents", "Info.plist"), "CFBundleName=Other\n"); return join(w.root, "Other.app"); },
    "Herdr.app itself": (w) => w.sourceApp,
    "a folder with our key that is not named *.app": (w) => { const d = join(w.root, "Folder"); mkdirSync(join(d, "Contents"), { recursive: true }); writeFileSync(join(d, "Contents", "Info.plist"), "CAOWindowSourceVersion=0.1.16\n"); return d; },
    "a symlink named *.app to our copy": (w) => { const l = join(w.root, "Link.app"); symlinkSync(w.copyApp, l); return l; },
    "a relative path": () => "CAO.app",
    "the root": () => "/",
    "the home folder": (w) => w.home,
    "a plain file named *.app": (w) => { const f = join(w.root, "File.app"); writeFileSync(f, "x"); return f; },
  };
  for (const [name, make] of Object.entries(cases)) {
    const w = world({ copy: true });
    assert.equal(w.install(["--local"]).status, 0, name);
    const target = make(w);
    const manifest = w.json(MANIFEST_FILE);
    writeFileSync(join(w.caoHome, MANIFEST_FILE), JSON.stringify({ ...manifest, appCopy: { ...manifest.appCopy, path: target } }));
    const before = { copy: snapshot(w.copyDir), home: snapshot(w.caoHome), root: files(w.root).length };
    const mark = w.log().length;
    const r = w.uninstall([]);
    assert.equal(r.status, 2, `${name}: ${r.text}`);
    assert.match(r.err, /nothing was changed/, name);
    assert.deepEqual(snapshot(w.copyDir), before.copy, `${name}: our copy was touched`);
    assert.deepEqual(snapshot(w.caoHome), before.home, `${name}: the home was touched`);
    assert.ok(existsSync(w.launcher), `${name}: the launcher was removed`);
    assert.equal(files(w.root).length, before.root, `${name}: a file appeared or went`);
    assert.deepEqual(w.log().slice(mark).filter((l) => /server stop|machine rm|app\.quit/.test(l)), [], `${name}: it started to uninstall`);
  }
});

test("app copy: uninstall of a copy that is already gone says so and finishes; a manifest with no copy removes none", () => {
  const w = world({ copy: true });
  assert.equal(w.install(["--local"]).status, 0);
  rmSync(w.copyApp, { recursive: true });
  const r = w.uninstall([]);
  assert.equal(r.status, 0, r.text);
  assert.match(r.err, /CAO\.app.*was not found/);
  const v = world({ copy: true });
  assert.equal(v.install(["--local", "--no-app-copy"]).status, 0);
  mkdirSync(v.copyApp, { recursive: true });
  writeFileSync(join(v.copyApp, "keep"), "mine\n");
  assert.equal(v.uninstall([]).status, 0);
  assert.equal(readFileSync(join(v.copyApp, "keep"), "utf8"), "mine\n", "a folder the manifest does not name was removed");
});

await finish();
