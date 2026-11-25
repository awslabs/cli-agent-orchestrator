// Source scans of the three mods and the helper module of the window's left sidebar, top bar and status bar
// Mod drawing and Herdr events cannot run under node, so the rules the pure functions of
// lib/shell.js and lib/agents.js cannot reach are pinned here by reading the source: a call order, a timer
// that must not exist, a press that must be stopped, one writer for a shared key.
//
// The scanner reads the source with comments blanked (and, for brace matching, strings too), so a rule named
// in a comment is not a call. Each scan is exercised on a fixture first, so a scanner that finds nothing
// cannot pass for a clean tree. Plain `node test/shell-wiring.test.mjs`.
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { assert, finish, test } from "./harness.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const ROOT = join(here, "..");

// The source with comments replaced by spaces (strings kept) and, in `blank`, the inside of strings and
// templates replaced by `_` too. Both have the length of the source, so an index in one is the same in the other.
export function readSource(src) {
  let keep = "";
  let blank = "";
  let i = 0;
  const n = src.length;
  while (i < n) {
    const c = src[i];
    const d = src[i + 1];
    if (c === "/" && d === "/") {
      while (i < n && src[i] !== "\n") { keep += " "; blank += " "; i += 1; }
      continue;
    }
    if (c === "/" && d === "*") {
      const end = src.indexOf("*/", i + 2);
      const stop = end < 0 ? n : end + 2;
      for (const ch of src.slice(i, stop)) { keep += ch === "\n" ? "\n" : " "; blank += ch === "\n" ? "\n" : " "; }
      i = stop;
      continue;
    }
    if (c === '"' || c === "'" || c === "`") {
      keep += c; blank += c; i += 1;
      while (i < n && src[i] !== c && (c === "`" || src[i] !== "\n")) {
        const two = src[i] === "\\" ? 2 : 1;
        keep += src.slice(i, i + two);
        blank += "_".repeat(two);
        i += two;
      }
      keep += c; blank += c; i += 1;
      continue;
    }
    keep += c; blank += c; i += 1;
  }
  return { keep, blank };
}

// The body of `name(...) { ... }` (a method or a function), without its braces, or null.
export function bodyOf(source, name) {
  const re = new RegExp(`(?:^|\\n)[ \\t]*(?:export\\s+)?(?:async\\s+)?(?:function\\s+)?${name}\\s*\\([^)]*\\)\\s*\\{`);
  const m = re.exec(source.blank);
  if (!m) return null;
  const open = m.index + m[0].length - 1;
  let depth = 0;
  for (let i = open; i < source.blank.length; i += 1) {
    if (source.blank[i] === "{") depth += 1;
    else if (source.blank[i] === "}") { depth -= 1; if (depth === 0) return source.keep.slice(open + 1, i); }
  }
  return null;
}

const count = (text, re) => [...text.matchAll(re)].length;
const read = (...parts) => readSource(readFileSync(join(ROOT, ...parts), "utf8"));
const lineCount = (src, re) => src.keep.split("\n").filter((l) => re.test(l)).length;

// ---- the scanner itself ----------------------------------------------------------------------------

test("the scanner does not read comments as code, and finds a method body by its braces", () => {
  const src = readSource("// run(\"layout.show\")\nconst a = \"}\";\nclass X {\n  tick(cx) {\n    if (a) { run(\"layout.hide\"); }\n    /* run(\"layout.show\") */\n  }\n  other() { return 1; }\n}\n");
  assert.ok(!src.keep.includes("layout.show"));
  const body = bodyOf(src, "tick");
  assert.ok(body !== null && body.includes("layout.hide") && !body.includes("other"));
  assert.equal(bodyOf(src, "missing"), null);
});

// ---- the top bar (20.3) ------------------------------------------------------------------------------

const tabs = () => read("mods", "cao-window-tabs", "main.js");

test("Bar always drawn: the top bar makes no layout.resize call and keeps none of the old resize code", () => {
  const src = tabs();
  assert.equal(count(src.keep, /layout\.resize/g), 0);
  assert.equal(count(src.keep, /tabResize|initialTabResize|RETRY_TICKS|MAX_RETRIES/g), 0);
});

test("Buttons do not drag the window: the tab and the button each stop the press", () => {
  const src = tabs();
  assert.ok(lineCount(src, /stop_propagation/) >= 2, "two lines stop the press");
  for (const name of ["tabView", "barButton"]) {
    const body = bodyOf(src, name);
    assert.ok(body !== null, `${name} exists`);
    assert.match(body, /on_mouse_down\("left", \(_e, cx\) => cx\.stop_propagation\(\)\)/, `${name} stops the press`);
  }
});

test("Hidden at first start: only the top bar sends layout.show and layout.hide, and only what rsideLayoutStep returned", () => {
  const src = tabs();
  const apply = bodyOf(src, "applyLayout");
  assert.ok(apply !== null);
  assert.equal(count(src.keep, /layout\.(show|hide)/g), count(apply, /layout\.(show|hide)/g), "no send outside applyLayout");
  assert.equal(count(apply, /layout\.show/g), 1);
  assert.equal(count(apply, /layout\.hide/g), 1);
  const calls = [...src.keep.matchAll(/this\.applyLayout\(([^)]*)\)/g)].map((m) => m[1].trim());
  assert.ok(calls.length >= 1, "a step is applied");
  assert.ok(count(src.keep, /this\.syncLayout\(\)/g) >= 3, "the tick and the button press both run a step (the press runs it before and after)");
  for (const arg of calls) assert.match(arg, /\.send$/, `applyLayout(${arg}) is not a step's send`);
  assert.ok(count(src.keep, /rsideLayoutStep\(/g) >= 1);
  for (const dir of ["mods"]) {
    for (const mod of readdirSync(join(ROOT, dir), { withFileTypes: true }).filter((e) => e.isDirectory() && e.name !== "cao-window-tabs")) {
      for (const f of readdirSync(join(ROOT, dir, mod.name)).filter((x) => x.endsWith(".js"))) {
        const other = read(dir, mod.name, f);
        assert.equal(count(other.keep, /layout\.(show|hide)/g), 0, `${mod.name}/${f} sends a layout show or hide`);
      }
    }
  }
});

test("Fetched while the sidebar is closed: both pollers of the bar ask whether they are due with shown true", () => {
  const src = tabs();
  assert.equal(count(src.keep, /(?<![\w$])flowsPollDue\(\{\s*shown: true\b/g), 1);
  assert.equal(count(src.keep, /workflowsPollDue\(\{\s*shown: true\b/g), 1);
  assert.equal(count(src.keep, /\bkind === "flows"|\bsection\b/g), 0, "no section or hidden-block condition");
});

test("Count updates while closed: the bar publishes the flow list and both list views through listView", () => {
  const src = tabs();
  for (const key of ["flows", "flowsView", "workflowsView"]) assert.ok(src.keep.includes(`caowin.${key}"`), `caowin.${key} is written`);
  assert.ok(count(src.keep, /listView\(/g) >= 2);
  assert.ok(count(src.keep, /barButtonLabel\(/g) >= 2);
  assert.equal(count(src.keep, /prompt_template|\bsource\b|inputs/g), 0, "nothing of a prompt, source or input spec is named");
});

// ---- the status bar (20.4) ---------------------------------------------------------------------------

const status = () => read("mods", "cao-window-status", "main.js");

test("Slow delete is not sent twice: authorDeletes, deleteStarted and deleteEnded are called once each, around the one DELETE", () => {
  const src = status();
  assert.equal(lineCount(src, /authorDeletes\(/), 1);
  assert.equal(lineCount(src, /deleteStarted\(/), 1);
  assert.equal(lineCount(src, /deleteEnded\(/), 1);
  const order = src.keep.split("\n").map((l, i) => ({ l, i })).filter(({ l }) => /deleteStarted\(|callCao\(|deleteEnded\(/.test(l));
  assert.deepEqual(order.map(({ l }) => /deleteStarted\(/.test(l) ? "started" : /callCao\(/.test(l) ? "call" : "ended"), ["started", "call", "ended"]);
});

test("Slow delete is not sent twice: authorDeletes is given the recorded authors, the stored lease, the clock and the in-flight list", () => {
  const src = status();
  const calls = callArgs(readSource(src.keep), "authorDeletes");
  assert.equal(calls.length, 1, "one call");
  assert.deepEqual(calls[0], ["authors", "this.store.get(KEYS.dialog)", "Date.now()", "this.inFlight"], "ignoring the lease deletes the author of an open dialog; ignoring the in-flight list sends a slow delete again every second");
  assert.match(bodyOf(src, "cleanAuthors"), /const authors = listOf\(this\.store\.get\(KEYS\.authors\)\);/, "the records are the stored ones");
  assert.match(src.keep, /const KEYS = \{[^}]*\bdialog: "caowin\.dialog"/, "the lease key is the one the dialog host writes");
});

test("Slow delete is not sent twice: the in-flight list is edited only by deleteStarted and deleteEnded", () => {
  const src = status();
  const assigns = [...src.keep.matchAll(/this\.inFlight\s*=\s*([^;]*);/g)].map((m) => m[1].trim());
  assert.equal(assigns.length, 3, "the start value and the two edits");
  assert.ok(assigns.includes("[]"));
  assert.ok(assigns.includes("deleteStarted(this.inFlight, name)"));
  assert.ok(assigns.includes("deleteEnded(this.inFlight, name)"));
  assert.equal(count(src.keep, /this\.inFlight\.(push|splice|pop|shift|add|delete)/g), 0);
});

test("Closed by Herdr: the status bar writes no lease and clears nothing at start", () => {
  const src = status();
  assert.equal(count(src.keep, /leaseFor\(/g), 0);
  for (const line of src.keep.split("\n").filter((l) => /caowin\.dialog|KEYS\.dialog/.test(l))) {
    assert.doesNotMatch(line, /\.set\(|setShared\(/, `writes the lease: ${line.trim()}`);
  }
  const init = bodyOf(src, "init");
  assert.ok(init !== null && !/\.set\(|setShared\(/.test(init.replace(/this\.store = createStore\([^)]*\)/, "")), "init writes nothing");
});

test("Delete fails after the dialog closed: a delete goes only through draftCall, and its answer through afterDelete", () => {
  const src = status();
  assert.equal(lineCount(src, /draftCall\(/), 1);
  assert.equal(lineCount(src, /afterDelete\(/), 1);
  assert.ok(src.keep.includes("404"), "a 404 counts as deleted");
});

test("Buttons have states and Help: the parts come from statusParts and the question mark opens the help dialog", () => {
  const src = status();
  assert.ok(count(src.keep, /statusParts\(/g) >= 1);
  assert.ok(count(src.keep, /openDialog\(\{ kind: "bar-help" \}\)/g) === 1);
  assert.ok(count(src.keep, /nextFilter\(/g) >= 1);
});

// ---- the window's title (27.17) ----------------------------------------------------------------------

// The files (relative to ROOT) that call setWindowTitle, for the mods and for the shared lib the mods bundle.
function titleCallers() {
  const out = [];
  for (const mod of readdirSync(join(ROOT, "mods"), { withFileTypes: true }).filter((e) => e.isDirectory())) {
    for (const f of readdirSync(join(ROOT, "mods", mod.name)).filter((x) => x.endsWith(".js"))) {
      if (count(read("mods", mod.name, f).keep, /\bsetWindowTitle\b/g) > 0) out.push(`mods/${mod.name}/${f}`);
    }
  }
  for (const f of readdirSync(join(ROOT, "lib")).filter((x) => x.endsWith(".js"))) {
    if (count(read("lib", f).keep, /\bsetWindowTitle\b/g) > 0) out.push(`lib/${f}`);
  }
  return out;
}

test("One agent is one tab: the bar draws every tab tabViews gives, with no count to reach first, and none without a focused session", () => {
  const src = tabs();
  assert.equal(count(src.keep, /ctx\.tabs\.length|tabs\.length\s*[<>]/g), 0, "no minimum number of tabs");
  assert.match(src.keep, /\.children\(ctx\.tabs\.map\(\(x, i\) => this\.tabView\(t, x, ctx, i\)\)\)/, "every tab of tabViews is drawn (with its index, for the scroll into view)");
  assert.match(bodyOf(src, "shown"), /const tabs = s \? tabViews\([^\n]*\) : \[\];/, "no focused session, no tab");
  assert.equal(count(src.keep, /tabsFor\(/g), 0, "the bar asks tabViews, not tabsFor");
});

test("The window title is CAO: the title scan reads a call in init and in render apart, and sees a mention in a comment as none", () => {
  const src = readSource('import { setWindowTitle } from "herdr";\nclass M {\n  init(cx) {\n    if (checking()) return;\n    setWindowTitle(WINDOW_TITLE);\n  }\n  render() {\n    // setWindowTitle("x")\n    return 1;\n  }\n}\n');
  assert.deepEqual(callArgs(readSource(bodyOf(src, "init")), "setWindowTitle"), [["WINDOW_TITLE"]]);
  assert.deepEqual(callArgs(readSource(bodyOf(src, "render")), "setWindowTitle"), []);
  assert.equal(count(src.keep, /\bsetWindowTitle\b/g), 2, "the import and the one call; the comment is blanked");
});

test("The window title is CAO: the status mod imports setWindowTitle from herdr and calls it once, in init and not in render, with WINDOW_TITLE", () => {
  const src = status();
  assert.match(src.keep, /import \{[^}]*\bsetWindowTitle\b[^}]*\} from "herdr";/, "imported from the host module");
  assert.match(src.keep, /import \{[^}]*\bWINDOW_TITLE\b[^}]*\} from "\.\/shell\.js";/, "the text is the named constant of lib/shell.js");
  assert.equal(count(src.keep, /\bsetWindowTitle\s*\(/g), 1, "one call in the whole file");
  const init = bodyOf(src, "init");
  assert.ok(init !== null, "init exists");
  assert.deepEqual(callArgs(readSource(init), "setWindowTitle"), [["WINDOW_TITLE"]], "the one call is in init, with the constant and nothing else");
  assert.equal(count(bodyOf(src, "render"), /\bsetWindowTitle\b/g), 0, "not on every render");
  assert.ok(init.search(/\bsetWindowTitle\(/) > init.search(/if \(checking\(\)\) return;/), "after the check guard: a check renders once and has no side effects, as the badge");
});

test("The window title is CAO: no other mod and no shared lib file calls setWindowTitle", () => {
  assert.deepEqual(titleCallers(), ["mods/cao-window-status/main.js"]);
});

// ---- the Agents header (27.19) -------------------------------------------------------------------------

test("No folder line in the Agents block: the side mod reads no directory and draws no line for one", () => {
  const src = read("mods", "cao-window-side", "main.js");
  assert.equal(count(src.keep, /\bcwd\b|cao-d-cwd/g), 0, "no read of the terminal's directory and no line id for it");
  assert.equal(count(src.keep, /working directory/g), 0);
  const call = callArgs(readSource(src.keep), "agentsHeader");
  assert.equal(call.length, 1, "one header call");
  assert.deepEqual(call[0].slice(-1), ["{ text: read?.text }"], "the reads are the recap's screen text alone");
});

// ---- the sidebar's wordmark (27.20) --------------------------------------------------------------------
// Herdr draws the top-left row of the left sidebar (its `brand` block) with its shipped `workspace-chrome` mod
// (mods/workspace-chrome/main.js of Herdr 0.1.17, read-only on the Mac): the logo and the text "Herdr", a search button
// and a fold button. The window draws the same row with `cao-window-brand`, the text from BRAND_TEXT. These scans pin that
// the row is the same one in every way but the text: the commands the two buttons run, the padding and the rail.

const brand = () => read("mods", "cao-window-brand", "main.js");
const flat = (text) => text.replace(/\s+/g, " ");

test("The top-left says CAO: the brand mod draws BRAND_TEXT of lib/shell.js beside Herdr's logo, and names no text of its own", () => {
  const src = brand();
  assert.match(src.keep, /import \{[^}]*\bBRAND_TEXT\b[^}]*\} from "\.\/shell\.js";/, "the text is the named constant of lib/shell.js");
  assert.equal(count(src.keep, /\.child\(BRAND_TEXT\)/g), 1, "the constant is drawn once, as a child");
  const SAYS_HERDR = /["'`][^"'`\n]*Herdr[^"'`\n]*["'`]/g;
  assert.equal(count(readSource('x.child("Herdr");\n// child("Herdr")\nimage("frame/welcome/herdr.png");\n').keep, SAYS_HERDR), 1, "the scan finds a literal Herdr, not a comment or the logo's file name");
  assert.equal(count(src.keep, SAYS_HERDR), 0, "no string of the mod says Herdr (the logo's file is herdr.png, lower case)");
  assert.equal(count(src.keep, /\bimage\(brandMark\(t\)\)\.size\(BRAND_MARK_SIZE\)\.rounded\(BRAND_MARK_RADIUS\)/g), 1, "the CAO mark stands beside the text, drawn in full colour at the size and radius Herdr's logo had");
  assert.equal(count(src.keep, /herdr\.png|frame\/welcome/g), 0, "Herdr's logo is no longer drawn (changed on purpose: it was asserted at 20 px)");
  assert.match(src.keep, /import \{[^}]*\bbrandMark\b[^}]*\} from "\.\/shell\.js";/, "the mark and its theme rule are pure code of lib/shell.js");
  assert.ok(src.keep.indexOf("image(") < src.keep.indexOf(".child(BRAND_TEXT)"), "the logo comes before the text");
  assert.match(src.keep, /div\(\)\.text_color\(t\.foreground\)\.child\(BRAND_TEXT\)/, "the text is drawn in the foreground colour at the weight of the user's font_weight setting, like the rows (no explicit weight)");
});

test("The CAO mark's two drawings ship in the brand mod's folder, whole, and are the project's own files", () => {
  const folder = join(ROOT, "mods", "cao-window-brand");
  const site = join(ROOT, "..", "..", "docusaurus", "static", "img");
  for (const f of ["cao-mark.svg", "cao-mark-dark.svg"]) {
    assert.ok(existsSync(join(folder, f)), `${f} is in the brand mod's folder`);
    if (existsSync(join(site, f))) assert.equal(readFileSync(join(folder, f), "utf8"), readFileSync(join(site, f), "utf8"), `${f} is the site's file, byte for byte`);
    assert.match(readFileSync(join(folder, f), "utf8"), /<svg[^>]*viewBox="0 0 120 120"/, `${f} is an svg`);
  }
  assert.equal(count(brand().keep, /\bsvg\("mods\/cao-window-brand/g), 0, "a multi-colour drawing is not drawn with svg(): that takes one tint colour; image() keeps the colours");
});

test("Same search button: it opens the picker with the command Herdr's own button runs, and its drawing is Herdr's", () => {
  const src = brand();
  assert.equal(count(src.keep, /run\("mod\.open", \{ name: "picker" \}\)/g), 1);
  assert.equal(count(src.keep, /\bmod\.open\b/g), 1, "no other mod is opened from the row");
  assert.equal(count(src.keep, /svg\("mods\/workspace-chrome\/search\.svg"\)\.size\(15\)/g), 1);
});

test("Same fold button: it asks Herdr for the rail (76 px on macOS, 44 elsewhere) or the saved width (268 px), and remembers the width it leaves", () => {
  const src = brand();
  const body = bodyOf(src, "brandToggle");
  assert.ok(body !== null, "the fold has one function");
  const text = flat(body);
  assert.match(text, /run\("layout\.resize", \{ id: "sidebar", px: [^}]*\}\)/);
  assert.match(text, /\? \(?shared\("sidebar\.width"\) \?\? 268\)? : window\(\)\?\.platform === "macos" \? 76 : 44/, "back to the saved width or 268; to the rail 76 on macOS, else 44");
  assert.match(text, /setShared\("sidebar\.width", width\)/, "the width it leaves is saved before the rail");
  assert.match(text, /setShared\("sidebar\.collapsed", /, "the flag older mods still read is kept in step");
  assert.equal(count(src.keep, /layout\.resize/g), 1, "one resize, in the fold");
  assert.equal(count(src.keep, /svg\("mods\/workspace-chrome\/sidebar\.svg"\)\.size\(15\)/g), 1);
  assert.match(flat(src.keep), /\.child\(this\.button\("sidebar-toggle", svg\([^)]*\)\.size\(15\), brandToggle\)\)/, "the fold button runs the fold");
});

test("Same rail: the row is narrow when the sidebar is under 160 px, and the wordmark and the search button go with it", () => {
  const src = brand();
  const all = flat(src.keep);
  assert.match(all, /const brandNarrow = \(\) => \(brandFind\(layout\(\), "sidebar"\)\?\.size\?\.px \?\? Infinity\) < 160;/);
  const render = flat(bodyOf(src, "render"));
  assert.match(render, /const collapsed = brandNarrow\(\);/);
  assert.match(render, /\.pl\(collapsed \? 6 : 14\)\.pr\(10\)\.gap\(4\)\.items_center\(\)/, "the padding of Herdr's row");
  assert.match(render, /\.when\(window\(\)\?\.platform === "macos" && !window\(\)\?\.fullscreen && !collapsed, el => el\.pl\(76\)\)/, "room for the traffic lights, on macOS, not fullscreen, not on the rail");
  assert.equal(count(render, /\.when\(!collapsed, /g), 2, "the wordmark and the search button show only when it is not the rail");
  assert.match(render, /\.when\(!\(collapsed && window\(\)\?\.platform === "macos"\), el => el\.child\(this\.button\("sidebar-toggle"/, "on the macOS rail the traffic lights fill the row and the fold button is not drawn");
});

test("Buttons do not drag the window: the brand row's buttons stop the press, as Herdr's do", () => {
  const body = bodyOf(brand(), "button");
  assert.ok(body !== null, "one function draws both buttons");
  assert.match(body, /on_mouse_down\("left", \(_e, cx\) => cx\.stop_propagation\(\)\)/);
  assert.match(body, /\.on_click\(action\)/);
});

test("Only the brand mod resizes the sidebar column (the side mod folds its own blocks), and the row has no timer or call out", () => {
  const callers = [];
  for (const mod of readdirSync(join(ROOT, "mods"), { withFileTypes: true }).filter((e) => e.isDirectory())) {
    for (const f of readdirSync(join(ROOT, "mods", mod.name)).filter((x) => x.endsWith(".js"))) {
      if (count(read("mods", mod.name, f).keep, /layout\.resize",\s*\{\s*id:\s*"sidebar"/g) > 0) callers.push(`mods/${mod.name}/${f}`);
    }
  }
  assert.deepEqual(callers, ["mods/cao-window-brand/main.js"]);
  assert.equal(count(brand().keep, /\bexec\(|\bcx\.timer\b|\bsetBadge\b|\bsetWindowTitle\b|\bcallCao\b|\bfetch\(/g), 0, "the row only draws two buttons");
  assert.deepEqual(JSON.parse(readFileSync(join(ROOT, "mods", "cao-window-brand", "mod.json"), "utf8")), { place: "none" }, "placed by the layout, like the other cao-window mods");
});

test("Mirrored, not cloned: the brand mod names the Herdr lines it mirrors and holds under 60 lines of code", () => {
  const raw = readFileSync(join(ROOT, "mods", "cao-window-brand", "main.js"), "utf8");
  assert.ok(count(raw, /workspace-chrome\/main\.js:\d+/g) >= 3, "the comments cite the lines of Herdr's mod that the code mirrors");
  assert.ok(raw.split("\n").filter((l) => l.trim() !== "" && !l.trim().startsWith("//")).length < 60, "a small equivalent, not Herdr's file copied");
  assert.equal(count(brand().keep, /props\(\)|updates\(\)|footer/g), 0, "the footer half of Herdr's mod is not in it");
});

test("The layout points the brand block at the brand mod, and only that block: the derivation script names cao-window-brand and the brand block, and never the footer", () => {
  const jq = readFileSync(join(ROOT, "make-window.jq"), "utf8").split("\n").filter((l) => !l.trimStart().startsWith("#")).join("\n");
  assert.ok(count(jq, /cao-window-brand/g) >= 1, "the derivation names the mod");
  assert.ok(/"brand"/.test(jq), "the block it re-points is the one with the id brand");
  assert.equal(count(jq, /sidebar-footer/g), 0, "the sidebar footer stays Herdr's");
});


// ---- the dialog opener (20.1) ------------------------------------------------------------------------

const ui = () => read("lib", "ui.js");

test("Opened from either New session control: openDialog reads the lease, then writes it, then opens the overlay", () => {
  const src = ui();
  assert.equal(lineCount(src, /dialogSpec\(/), 1);
  const body = bodyOf(src, "openDialog");
  assert.ok(body !== null);
  const at = (re) => body.search(re);
  assert.ok(at(/dialogLive\(/) >= 0 && at(/dialogLive\(/) < at(/leaseFor\(/), "dialogLive before leaseFor");
  assert.ok(at(/leaseFor\(/) < at(/uiOpenOverlay\(/), "leaseFor before openOverlay");
  assert.match(body, /dialogSpec\([^)]*,\s*uiProps\(\)\)/, "the overlay props carry the opener's layout props");
  assert.equal(count(body, /\bawait\b|\.then\(/g), 0, "the check and the write are one tick");
  assert.match(src.keep, /openOverlay as uiOpenOverlay/);
});

test("A refused open leaves a notice: openDialog notes when it refused, and the three hosts of the open buttons draw it", () => {
  const body = bodyOf(ui(), "openDialog");
  assert.ok(body !== null);
  assert.ok(body.search(/dialogLive\(/) < body.search(/uiBusyUntil = /), "the notice is noted on the refusal");
  assert.ok(body.search(/uiBusyUntil = /) < body.search(/return false/), "and before the open returns false");
  assert.equal(count(body, /uiBusyUntil = /g), 1);
  assert.match(ui().keep, /export (function|const) dialogBusyNotice/);
  for (const [what, src] of [["the side mod", read("mods", "cao-window-side", "main.js")], ["the sidebar", read("mods", "cao-window-panel", "sidebar.js")], ["the status mod", read("mods", "cao-window-status", "main.js")]]) {
    assert.ok(count(src.keep, /dialogBusyNotice\(\)/g) >= 1, `${what} draws the notice`);
    assert.match(src.keep, /import \{[^}]*\bdialogBusyNotice\b[^}]*\} from "\.\/ui\.js"/, `${what} imports it from ui.js`);
  }
});

// ---- the left sidebar (20.1, 20.2) -------------------------------------------------------------------

const side = () => read("mods", "cao-window-side", "main.js");

test("Selecting a session: one writer of the selection, and it takes the session's name alone (no unfold key)", () => {
  const src = side();
  const writes = [...src.keep.matchAll(/store\.set\(KEYS\.selection,\s*\{\s*kind: "session"/g)];
  assert.equal(writes.length, 1);
  const body = bodyOf(src, "selectSession");
  assert.ok(body !== null);
  assert.match(body, /store\.set\(KEYS\.selection,\s*\{\s*kind: "session"/);
  assert.match(src.keep, /^\s*selectSession\(name\) \{/m, "the writer takes the name and nothing else");
  assert.equal(count(src.keep, /unfold/gi), 0, "the side mod has no unfold key, writer or reader");
});
test("Quiet worker: the stuck click selects the session and does not focus a terminal", () => {
  const src = side();
  const body = bodyOf(src, "sessionLineView");
  assert.ok(body !== null);
  assert.match(body, /selectSession\(s\.name\)/);
  assert.equal(count(body, /selectSession\([^)]*true\)|focusTerminal/g), 0);
});
test("Only the Sessions copy runs tick: the Agents copy's own tick does none of its duties", () => {
  const src = side();
  assert.match(src.keep, /section === "sessions"\)[^;]*tick\(/);
  const agents = bodyOf(src, "tickAgents");
  assert.ok(agents !== null);
  assert.equal(count(agents, /watchSeen|fetchTerminal|fetchHealth|syncCache|prunePins|fallbackPlan|seenWrite|observe\(|markPending/g), 0);
  assert.ok(lineCount(src, /tickAgents/) >= 2, "defined and started by a timer");
  assert.match(agents, /this\.readScreen\(/, "the recap read moved here from the panel");
  const reader = bodyOf(src, "readScreen");
  assert.ok(reader !== null && /hc\(\["read"/.test(reader), "the read is an hc read of the terminal's screen");
  assert.equal(count(reader, /watchSeen|seenWrite|hc\(cmd\)/g), 0, "and never a label write");
  assert.ok(src.keep.includes("readOfThisTurn("));
});

test("Agent row focuses its terminal: the row's click focuses and never writes the selected session", () => {
  const src = side();
  const body = bodyOf(src, "agentRowView");
  assert.ok(body !== null);
  assert.match(body, /focusTerminal\(/);
  assert.equal(count(body, /KEYS\.selection|selectSession\(/g), 0);
});

test("New session control: the header plus calls the one opener, and no row at the end of the list does", () => {
  const src = side();
  const body = bodyOf(src, "newSession");
  assert.ok(body !== null);
  assert.match(body, /openDialog\(\{ kind: "new-session" \}\)/);
  assert.ok(count(src.keep, /this\.newSession\(/g) >= 1);
  assert.match(bodyOf(src, "headerView"), /cao-plus-[\s\S]*this\.newSession\(\)/, "the header's + opens the dialog");
});

test("No bottom New session row: the Sessions list ends with its last session, and the file has no such row, id or key target", () => {
  const src = side();
  assert.equal(count(src.keep, /newSessionRow|cao-new-session|"\+ New session"|\bplus: true|\bitem\.plus\b/g), 0);
  assert.equal(bodyOf(src, "newSessionRow"), null);
  const list = bodyOf(src, "sessionsBody");
  assert.ok(list !== null);
  assert.equal(count(list, /newSession/g), 0, "the list body opens nothing");
});

test("Started by flow opens the sidebar at that flow: the link writes the sidebar state through rsideStep and never shows the block itself", () => {
  const src = side();
  const body = bodyOf(src, "openFlow");
  assert.ok(body !== null);
  assert.match(body, /rsideStep\([^)]*type: "link"/);
  assert.match(body, /KEYS\.rside/);
  assert.equal(count(src.keep, /layout\.(show|hide)/g), 0);
});

test("Clear control: the Sessions header comes from sessionsHeader and its control clears the filter", () => {
  const src = side();
  assert.ok(count(src.keep, /sessionsHeader\(/g) >= 1);
  const body = bodyOf(src, "clearFilter");
  assert.ok(body !== null && /KEYS\.filter,\s*null/.test(body));
});

test("Filter header fits its row: the Sessions header is built at the block's width and its tooltip has the whole text", () => {
  const src = side();
  const parts = bodyOf(src, "headParts");
  assert.ok(parts !== null);
  assert.match(parts, /sessionsHeader\([^;]*, filter, this\.width\)/, "the header is shortened to the width the block was last drawn at");
  assert.match(parts, /tip: head\.full/, "the tooltip is the whole text");
  assert.match(bodyOf(src, "headerView"), /\.tooltip\(head\.tip\)\.child\(head\.title\)/, "the title draws the short text and tells the whole one");
});

test("Flow link keeps its row: the inferred label is on a line of its own above the link, so the link is not cut to 'Start...'", () => {
  const body = bodyOf(side(), "agentsBody");
  const [label, link] = [body.indexOf("whole(head.flow.label"), body.indexOf('h_flex().id("cao-d-flow")')];
  assert.ok(label >= 0 && link >= 0 && label < link, "the label comes first, then the link");
  const row = body.slice(link, body.indexOf("\n      .when(", link) === -1 ? undefined : body.indexOf("\n      .when(", link));
  assert.equal(count(row, /whole\(/g), 0, "nothing shares the link's row");
  assert.match(body, /v_flex\(\)\.id\("cao-tg-flow"\)/, "the label and the link stack");
});

test("Buttons have states: a small control rests as a faint filled background, with no outline, and keeps its hover and pointer", () => {
  // The status bar parts and the "?", the header "+" and the header "clear" control. The tab, the pills and the text fields
  // keep their outlines. The top-bar buttons are not in this list: they are drawn as Herdr's own Files and Changes buttons,
  // on the bar's chrome (see "The top-bar buttons look like Herdr's").
  const controls = [
    ["status bar partView", bodyOf(status(), "partView")],
    ["status bar render (the ? button)", bodyOf(status(), "render")],
    ["sidebar headerView (the + and clear controls)", bodyOf(side(), "headerView")],
  ];
  const fills = [1, 1, 2];
  controls.forEach(([name, body], i) => {
    assert.ok(body !== null, `${name} exists`);
    assert.equal(count(body, /\.border_1\(\)/g), 0, `${name} draws no resting outline`);
    assert.equal(count(body, /\.bg\((?:part\.resting \? )?t\.surface\b/g), fills[i], `${name} rests on the t.surface fill`);
    assert.ok(count(body, /\.cursor_pointer\(\)/g) >= 1, `${name} keeps the pointer`);
    assert.ok(count(body, /\.hover\(/g) >= 1, `${name} keeps the hover`);
  });
  assert.ok(count(bodyOf(status(), "render"), /\.border_t\(1\)/g) === 1, "the bar's own top divider stays");
});


// ---- the panel mod's two hosts (21.3, 21.4) -----------------------------------------------------------

const sidebar = () => read("mods", "cao-window-panel", "sidebar.js");
const dialog = () => read("mods", "cao-window-panel", "dialog.js");
const panelMain = () => read("mods", "cao-window-panel", "main.js");

test("Hidden at first start: the sidebar host sends no layout command and opens a dialog only through openDialog", () => {
  const src = sidebar();
  assert.equal(count(src.keep, /layout\.(show|hide|toggle|resize)/g), 0);
  assert.equal(count(src.keep, /\brun\(|\bsbRun\b/g), 0, "no host command at all");
  assert.equal(count(src.keep, /openOverlay\(/g), 0, "an overlay opens only in lib/ui.js");
  assert.ok(count(src.keep, /openDialog\(/g) >= 4);
  const kinds = [...src.keep.matchAll(/"((?:new|edit|run)-(?:flow|workflow))"/g)].map((m) => m[1]);
  assert.deepEqual([...new Set(kinds)].sort(), ["edit-flow", "edit-workflow", "new-flow", "new-workflow", "run-workflow"]);
});

test("Fetched while the sidebar is closed: the sidebar host starts no poll of the lists, and reads its item only while its block is shown", () => {
  const src = sidebar();
  assert.equal(count(src.keep, /path:\s*"\/flows"\s*[,}]|path:\s*"\/workflows"\s*[,}]|WORKFLOW_RUNS_LIMIT|pollFlows|pollWorkflows/g), 0);
  assert.equal(count(src.keep, /timer\.every\(/g), 1, "one timer, the details tick");
  const tick = bodyOf(src, "tickSidebar");
  assert.ok(tick !== null);
  const shown = tick.search(/shownBlock\(/);
  assert.ok(shown >= 0, "the tick asks whether the block is shown");
  for (const fetch of ["sbFetchFlow(", "sbFetchWorkflow("]) assert.ok(tick.indexOf(fetch) > shown, `${fetch} only after the block is known to be shown`);
  assert.match(src.keep, /sbChecking\(\)\) return/);
});

test("Confirm dropped when the details change: a confirm is scoped, dropped when stale, and sends only while current", () => {
  const src = sidebar();
  const ask = bodyOf(src, "sbAsk");
  const now = bodyOf(src, "sbConfirmNow");
  const stale = bodyOf(src, "sbDropStaleConfirm");
  for (const body of [ask, now, stale]) assert.ok(body !== null);
  assert.match(ask, /confirmScope\(/);
  assert.match(now, /confirm\.scope === confirmScope\(/);
  assert.match(stale, /scope !== confirmScope\(/);
  assert.ok(count(bodyOf(src, "renderSidebarHost"), /sbDropStaleConfirm\(/g) >= 1, "a draw drops it");
  assert.ok(count(bodyOf(src, "tickSidebar"), /sbDropStaleConfirm\(/g) >= 1, "a tick drops it");
});

test("Answer after leaving the details: a confirmed write is recorded for its item, with the item's scope, after the answer", () => {
  const src = sidebar();
  for (const name of ["sbSendFlowWrite", "sbSendRunWrite"]) {
    const body = bodyOf(src, name);
    assert.ok(body !== null, name);
    assert.match(body, /scopeOf\(/);
    assert.ok(count(body, /sbSetOp\(/g) >= 2, `${name} records running and the answer`);
    assert.equal(count(body, /\bfd\b|\bwd\b|sb\.confirm/g), 0, `${name} writes to no details object`);
  }
  assert.ok(count(src.keep, /opOf\(/g) >= 4, "the rows and the details read it");
  const list = bodyOf(src, "sbListView");
  assert.match(list, /opOf\(sb\.ops, scopeOf\(kind, name\)\)\?\.state === "running"/, "the row reads running while the write is out");
});

test("Back arrow and Scheduled by: both only write the sidebar's state through rsideStep", () => {
  const src = sidebar();
  assert.equal(count(src.keep, /store\.set\(SB_KEYS\.rside/g), 1, "one writer of the state");
  assert.match(src.keep, /const sbSend = \(panel, event\) => panel\.store\.set\(SB_KEYS\.rside, rsideStep\(/);
  assert.match(bodyOf(src, "sbBackBar"), /sbSend\(panel, \{ type: "back" \}\)/);
  assert.match(src.keep, /sbSend\(panel, \{ type: "link", list: "flows"/);
  assert.match(src.keep, /sbSend\(panel, \{ type: "row", item: name \}\)/);
});

test("Prompt collapsed: the details are drawn with promptOpen from the sidebar's own toggle, and the prompt text only from promptView", () => {
  const src = sidebar();
  assert.match(src.keep, /flowDetailsView\(\{[^}]*promptOpen: open/);
  assert.equal(count(src.keep, /prompt_template|target\.prompt/g), 0);
});

test("Flow session link: it selects the session and nothing else", () => {
  const body = bodyOf(sidebar(), "sbSelectSession");
  assert.ok(body !== null);
  assert.match(body, /store\.set\(SB_KEYS\.selection, \{ kind: "session", name \}\)/);
  assert.equal(count(body, /store\.set\(/g), 1, "one write: the selection");
  assert.equal(count(sidebar().keep, /unfold/gi), 0, "no unfold key");
  assert.equal(count(sidebar().keep, /SB_KEYS\.selection/g), 1, "this one write; the sidebar reads no selection");
});
test("Approval row survives closing: the sidebar reads the key through approvalRow and writes it nowhere", () => {
  const src = sidebar();
  assert.match(src.keep, /approvalRow\(panel\.store\.get\(SB_KEYS\.approvals\), name\)/);
  assert.equal(count(src.keep, /store\.set\(SB_KEYS\.approvals/g), 0);
});

test("The panel hands its render to the host it picked", () => {
  const src = panelMain();
  assert.match(bodyOf(src, "render"), /this\.host\.render\(this\)/);
  assert.match(src.keep, /this\.host\.init\(this, cx\)/);
});

test("The sidebar and the shared widgets import host names only under prefixed aliases, as lib/ui.js does", () => {
  for (const [file, prefix] of [[sidebar(), "sb"], [read("lib", "widgets.js"), "wg"]]) {
    const host = [...file.keep.matchAll(/^import \{([^}]*)\} from "(?:gpui-kit|gpui-base|herdr|crypto)";/gm)].flatMap((m) => m[1].split(",").map((x) => x.trim()).filter(Boolean));
    assert.ok(host.length > 0);
    for (const name of host) assert.match(name, new RegExp(`^[\\w$]+ as ${prefix}[A-Z]\\w*$`), `${prefix}: ${name}`);
  }
});

// ==== the dialog host ====
test("Selection changes under an open dialog: the dialog host reads neither the selection nor the sidebar's state", () => {
  const src = dialog();
  // The reads of a stored key are the lease's (ownsLease) and, since the workflow forms and Draft with AI, the three lists
  // the dialog extends through a pure function (rows of "not submitted", author records, author ids): recordApproval,
  // clearApproval, recordAuthor, afterDelete, afterLaunch and recordDraft each take the list as it is stored and give
  // the new one. The forms read what else they need through the panel's own model() and gate(), which the sidebar uses
  // too; none of it is the selection, the sidebar's state or the flow list.
  const allowed = ["DL_LEASE_KEY", "DL_APPROVALS_KEY", "DL_AUTHORS_KEY", "DL_DRAFTS_KEY"];
  const reads = [...new Set([...src.keep.matchAll(/store\.get\(\s*([^),]+)\)/g)].map((m) => m[1].trim()))];
  for (const key of reads) assert.ok(allowed.includes(key), `the dialog host reads ${key}`);
  assert.ok(reads.includes("DL_LEASE_KEY"), "the lease read of ownsLease");
  assert.equal(count(src.keep, /caowin\.flows"|caowin\.rside"[^\n]*get|store\.get\(DL_RSIDE_KEY|store\.get\(DL_SELECTION_KEY/g), 0);
  assert.equal(count(src.keep, /DL_RSIDE_KEY/g), 2, "the key's definition and the one write of the set-rside effect");
  const event = bodyOf(src, "dialogEvent");
  assert.match(event, /fx\.type === "set-rside"\) panel\.store\.set\(DL_RSIDE_KEY, fx\.rside\)/);
  // A launch selects the new session behind the card: a write, never a read (the scan above), and nowhere but there.
  for (const m of src.keep.matchAll(/DL_SELECTION_KEY/g)) assert.ok(src.keep.slice(m.index - 10, m.index) === "store.set(" || src.keep.slice(m.index - 6, m.index) === "const ", "DL_SELECTION_KEY is defined or written, not read");
});


test("Escape with edits asks first: the key goes to dialogStep, and the overlay is closed only by its close effect", () => {
  const src = dialog();
  assert.match(bodyOf(src, "dlOnKey"), /dialogEvent\(panel, cx, \{ type: "close", confirming: panel\.dl\.nested === true \}\)/);
  assert.match(bodyOf(src, "dialogEvent"), /dialogStep\(/);
  assert.equal(count(src.keep, /dlCloseOverlay\(/g), 1);
  assert.match(bodyOf(src, "dialogEvent"), /dlCloseOverlay\(/);
});


// The text of `const <name> = (...) => { ... }` inside `body` (a keep view): an arrow function a closure holds, which
// bodyOf cannot find. Braces are matched on the `blank` twin of the same text, so a brace in a string does not count.
function arrowOf(source, name) {
  const re = new RegExp(`const ${name} = (\\([^)]*\\)) => \\{`);
  const m = re.exec(source.blank);
  if (!m) return null;
  const open = m.index + m[0].length - 1;
  let depth = 0;
  for (let i = open; i < source.blank.length; i += 1) {
    if (source.blank[i] === "{") depth += 1;
    else if (source.blank[i] === "}") { depth -= 1; if (depth === 0) return source.keep.slice(m.index + `const ${name} = `.length, i + 1); }
  }
  return null;
}

// Runs the profile field's key handler, as the dialog host wrote it, with the real comboStep. `open` is the list's flag,
// `count` the number of matching profiles. Returns what the handler did to the event.
async function pressInProfileField(key, { open, count }) {
  const { comboStep } = await import("../lib/launch.js");
  const src = dialog();
  const fn = arrowOf(readSource(bodyOf(src, "dlProfileCombo")), "onKey");
  assert.ok(fn !== null, "the profile field has an onKey handler");
  const dl = { combo: { open, index: 0 } };
  const matches = Array.from({ length: count }, (_, i) => ({ name: `profile-${i}` }));
  const log = { stopped: 0, chose: null, notified: 0 };
  const make = new Function("comboStep", "dl", "matches", "choose", `return ${fn};`);
  make(comboStep, dl, matches, (_cx, name) => { log.chose = name; })({ key }, { stop_propagation: () => { log.stopped += 1; }, notify: () => { log.notified += 1; } });
  return { ...log, open: dl.combo.open, index: dl.combo.index };
}

test("Escape with edits asks first: Esc in the profile field closes an open list and stops there", async () => {
  const r = await pressInProfileField("Escape", { open: true, count: 3 });
  assert.equal(r.stopped, 1, "the press does not go on to the dialog while it closes the list");
  assert.equal(r.open, false);
});

test("Escape with edits asks first: Esc in the profile field with the list closed goes on to the dialog's keys", async () => {
  const closed = await pressInProfileField("Escape", { open: false, count: 3 });
  assert.equal(closed.stopped, 0, "a closed list must not swallow Esc: the dialog has to ask or close");
  assert.equal(closed.open, false);
  const empty = await pressInProfileField("Escape", { open: true, count: 0 });
  assert.equal(empty.stopped, 0, "an open flag with no rows draws no list, so there is nothing to close");
  const esc = await pressInProfileField("Esc", { open: false, count: 0 });
  assert.equal(esc.stopped, 0);
});

test("Escape with edits asks first: the profile field still takes Enter and the arrows for its list", async () => {
  const enter = await pressInProfileField("Enter", { open: true, count: 3 });
  assert.deepEqual([enter.stopped, enter.chose], [1, "profile-0"]);
  const down = await pressInProfileField("ArrowDown", { open: true, count: 3 });
  assert.deepEqual([down.stopped, down.index, down.open], [0, 1, true]);
});

test("Help: the bar-help request is drawn from helpView", () => {
  const body = bodyOf(dialog(), "dlBody");
  assert.ok(body !== null);
  assert.match(body, /kind === "bar-help"/);
  assert.match(body, /helpView\(\)/);
});


test("Help is small and forms are large: no mod file sets the overlay kind or size, they come from dialogSpec and DIALOG_KIND", () => {
  for (const mod of readdirSync(join(ROOT, "mods"), { withFileTypes: true }).filter((e) => e.isDirectory())) {
    for (const f of readdirSync(join(ROOT, "mods", mod.name)).filter((x) => x.endsWith(".js"))) {
      const src = read("mods", mod.name, f);
      assert.equal(count(src.keep, /DIALOG_KIND|modal:|kind: "dialog"|kind: "window"/g), 0, `${mod.name}/${f}`);
    }
  }
  assert.match(bodyOf(read("lib", "shell.js"), "dialogSpec"), /kind: DIALOG_KIND/);
});


test("No tab index is set before the key test: neither the dialog host nor the shared widgets name tab_index", () => {
  for (const src of [dialog(), read("lib", "widgets.js")]) assert.equal(count(src.keep, /tab_index/g), 0);
});


test("The panel picks its host from props().host and hands render and close to it", () => {
  const src = panelMain();
  assert.match(src.keep, /props\(\)\?\.host === "dialog" \? DIALOG_HOST : SIDEBAR_HOST/);
  assert.match(bodyOf(src, "render"), /this\.host\.render\(this\)/);
  assert.match(bodyOf(src, "close"), /this\.host\.close\?\.\(this, cx\)/);
  assert.match(src.keep, /const DIALOG_HOST = \{[^}]*close: closeDialogHost/);
});


test("The two host files import host names only under prefixed aliases, as lib/ui.js does", () => {
  for (const [file, prefix] of [[sidebar(), "sb"], [dialog(), "dl"], [read("lib", "widgets.js"), "wg"]]) {
    const host = [...file.keep.matchAll(/^import \{([^}]*)\} from "(?:gpui-kit|gpui-base|herdr|crypto)";/gm)].flatMap((m) => m[1].split(",").map((x) => x.trim()).filter(Boolean));
    assert.ok(host.length > 0);
    for (const name of host) assert.match(name, new RegExp(`^[\\w$]+ as ${prefix}[A-Z]\\w*$`), `${prefix}: ${name}`);
  }
});

test("Hidden at first start: the dialog host sends no layout command and opens no overlay of its own", () => {
  const src = dialog();
  assert.equal(count(src.keep, /layout\.(show|hide|toggle|resize)/g), 0);
  assert.equal(count(src.keep, /\brun\(|\bdlRun\b|openOverlay\(/g), 0);
});

// ==== the dialog's forms ====
// The New session form and the flow forms moved from the shipped panel into the dialog host. Their rules live in
// lib/launch.js, lib/flows.js and lib/logic.js (node tests); these scans pin that the dialog applies them and
// does not re-implement them, and the wiring no node test reaches: what a launch tells the frame, what replaces
// the form body, which key a form reads its item from.

const LAUNCH_RULES = ["launchPlan", "launchConfirm", "launchBlocked", "buildRequest", "launchReadResult", "withKey", "launchOutcome", "launchAfter", "launchDialog",
  "launchResultCard", "equivalentCommand", "envCheck", "nameCheck", "workdirCheck", "resolvedProvider", "providerChoices", "profileMatches", "comboStep",
  "profileRoute", "profileFromOutcome", "keepSubmission", "discardNeeded", "parseToolList", "emptyForm", "unchosenProfileText", "dialogKey"];
// Positions of each pattern in `text`, which must all be found and come in the order given.
const inOrder = (text, ...res) => {
  const at = res.map((re) => text.search(re));
  return at.every((n) => n >= 0) && at.every((n, i) => i === 0 || n > at[i - 1]);
};

test("Opened from either New session control: the dialog host draws the kinds the openers send, from the lib rules", () => {
  const src = dialog();
  const registry = /const DL_FORMS = \{([^}]*)\}/.exec(src.keep);
  assert.ok(registry !== null, "the forms are registered by request kind");
  for (const kind of ["new-session"]) assert.match(registry[1], new RegExp(`"${kind}":`), kind);
  for (const name of [...LAUNCH_RULES]) assert.ok(count(src.keep, new RegExp(`(?<![\\w$.])${name}\\(`, "g")) >= 1, `${name} is called by the dialog host`);
  assert.equal(count(src.keep, /guardLeave|leaveForm|selectionKey|dialogStale|openKey|heldSel|lastDetail|FORM_KINDS|panelFormKey|panelLeaveGuard/g), 0, "nothing keys a form to the selection");
});

test("The New session form draws no Kiro engine and no resume field, reads neither from the form and sends neither", () => {
  const src = dialog();
  assert.equal(count(src.keep, /cao-en-|cao-f-resume|KIRO_ENGINES|fieldVisibility|resumeId|form\.engine|ins\.resume|"resume"|--engine|--resume-session-id|Kiro engine|Resume a Claude Code/g), 0);
  assert.equal(count(src.keep, /\bvis\b/g), 0, "no field is drawn from a per-provider visibility any more");
  assert.ok(count(src.keep, /cao-f-tools/g) >= 1 && count(src.keep, /cao-f-memory/g) >= 1, "the other Advanced fields are still drawn");
});

test("The shipped panel no longer opens these forms from its own selection kinds", () => {
  const src = panelMain();
  assert.equal(count(src.keep, /this\.(initForms|tickForms|renderLegacy)\(/g), 0);
  assert.match(bodyOf(src, "render"), /this\.host\.render\(this\)/);
});

test("Selection changes under a confirmation: Launch reads and sends from the snapshot of its press, and compares the dialog with itself", () => {
  const src = dialog();
  const launch = bodyOf(src, "dlOnLaunch");
  assert.ok(launch !== null, "dlOnLaunch exists");
  assert.match(launch, /launchReadResult\(\{\s*scope: ([\w.]+),\s*current: \1,/, "scope and current are one value, so no selection can make them differ");
  assert.match(launch, /dialogOpen: dl\.confirm !== null \|\| dl\.state\.discarding/, "another confirmation or the discard question is not replaced");
  assert.match(launch, /plan\.dialog === "yolo"/, "the YOLO and the * override confirmation come from the plan");
  assert.ok(inOrder(launch, /launchBlocked\(|launchPlan\(/, /dlSendLaunch\(|dlOpenLaunchConfirm\(/), "the plan is made before anything is sent or asked");
  const send = bodyOf(src, "dlSendLaunch");
  assert.ok(send !== null, "dlSendLaunch exists");
  assert.ok(inOrder(send, /launchConfirm\(/, /withKey\(/, /buildRequest\(/, /dialogWriteStart\(/, /callCao\(/), "the plan is checked again on the snapshot, then keyed, built, announced and sent");
  assert.equal(count(send, /caowin\.|store\.get\(/g), 0, "the send reads no stored key");
});

test("Close during a launch: the launch is a write the frame is told about, and no read is", () => {
  const src = dialog();
  const send = bodyOf(src, "dlSendLaunch");
  assert.ok(send !== null);
  assert.equal(count(send, /dialogWriteStart\(panel, cx, DL_LAUNCH_SECONDS\)/g), 1);
  assert.match(send, /maxTime: DL_LAUNCH_SECONDS/, "the lost-answer limit and the curl limit are one number");
  const after = bodyOf(src, "dlAfterLaunch");
  assert.ok(after !== null);
  assert.match(after, /dialogEvent\(panel, cx, \{ type: "saved" \}\)/, "a 201 ends the write and keeps the card in the dialog");
  assert.match(after, /dialogWriteEnd\(panel, cx\)/, "any other answer ends the write");
  for (const name of ["dlLoadProfile", "dlLoadFormData"]) {
    const body = bodyOf(src, name);
    assert.ok(body !== null, `${name} exists`);
    assert.equal(count(body, /dialogWriteStart\(/g), 0, `${name} is a read: it does not block closing`);
  }
});

test("Ask-first, YOLO, override and discard confirmations replace the form body: the form behind cannot change", () => {
  const src = dialog();
  const body = bodyOf(src, "dlBody");
  assert.ok(body !== null);
  assert.ok(inOrder(body, /dl\.confirm !== null/, /dl\.form\.body\(/), "a confirmation is drawn instead of the form body");
  const render = bodyOf(src, "renderDialogHost");
  assert.match(render, /discarding \? dlDiscard\(t\)/, "the discard question replaces it too");
  assert.match(render, /const locked = dl\.confirm !== null/);
  assert.match(render, /dl\.form\.buttons\(panel, t, m, locked\)/, "the form's buttons stay at the bottom, dimmed under a confirmation");
  for (const name of ["dlSessionButtons"]) assert.match(bodyOf(src, name), /locked/, `${name} honors it`);
  const open = bodyOf(src, "dlOpenLaunchConfirm");
  assert.ok(open !== null && /dlAskConfirm\(/.test(open) && /launchDialog\(/.test(open));
  assert.equal(count(bodyOf(src, "dlOnLaunch"), /dlOpenLaunchConfirm\(/g), 2, "Ask first and YOLO");
});

test("Escape in a confirmation: the confirmation's own keys, Esc is its Cancel, and nothing closes the overlay", () => {
  const src = dialog();
  assert.match(bodyOf(src, "dlOnKey"), /dl\.confirm !== null[^\n]*dlConfirmKey\(/);
  const keys = bodyOf(src, "dlConfirmKey");
  assert.ok(keys !== null);
  assert.match(keys, /dialogKey\(/);
  assert.match(keys, /step\.action === "cancel"[^\n]*dlCancelConfirm\(/);
  const cancel = bodyOf(src, "dlCancelConfirm");
  assert.ok(cancel !== null);
  assert.match(cancel, /dl\.confirm = null/);
  assert.equal(count(cancel, /dlCloseOverlay\(/g), 0, "the cancel returns to the form body and the dialog stays");
  assert.equal(count(src.keep, /dlCloseOverlay\(/g), 1, "the overlay is still closed only by the reducer's close effect");
});

test("Result card: a 201 selects the session behind the card, remembers the directory and shows the card with its Close button", () => {
  const src = dialog();
  const after = bodyOf(src, "dlAfterLaunch");
  assert.ok(after !== null);
  assert.match(after, /store\.set\(DL_SELECTION_KEY, \{ kind: "session", name: result\.session \}\)/);
  assert.equal(count(src.keep, /unfold/gi), 0, "the dialog has no unfold key to clear");
  assert.match(after, /store\.setLocal\("caowin\.lastWorkdir", submission\.workdir\)/);
  assert.match(bodyOf(src, "dlResultCard"), /launchResultCard\(/);
  assert.match(bodyOf(src, "dlSessionInit"), /emptyForm\(panel\.store\.getLocal\("caowin\.lastWorkdir"\)\)/, "Prefill after a launch");
  const buttons = bodyOf(src, "dlSessionButtons");
  assert.match(buttons, /dl\.created !== null/, "after a launch the bottom row holds the Close button alone");
  assert.equal(count(src.keep, /store\.set\(DL_SELECTION_KEY/g), 1, "one writer of the selection");
});

const FLOW_RULES = ["flowFormGate", "flowSaveAttempt", "flowSaveEffects", "flowSaveOutcome", "flowCreateBody", "flowUpdateBody", "flowFields", "flowFormOf", "emptyFlowForm",
  "validateBody", "validateResult", "flowCheckKey", "flowRoute", "routesState", "shownErrors", "firstErrorField", "formErrorsOf", "numericWeekdayWarning", "dayAndWeekdayWarning", "scheduleWarning",
  "workflowChoices", "flowFormDirty", "flowDialogSpec", "validateNote", "runWords"];


test("Edit flow and New flow are drawn by the dialog host, from the lib rules of the flow forms", () => {
  const src = dialog();
  const registry = /const DL_FORMS = \{([^}]*)\}/.exec(src.keep);
  assert.ok(registry !== null);
  for (const kind of ["new-flow", "edit-flow"]) assert.match(registry[1], new RegExp(`"${kind}":`), kind);
  for (const name of FLOW_RULES) assert.ok(count(src.keep, new RegExp(`(?<![\\w$.])${name}\\(`, "g")) >= 1, `${name} is called by the dialog host`);
});

test("Close while the form reads: the flow forms' reads and checks do not block closing, their saves do", () => {
  const src = dialog();
  const flow = bodyOf(src, "dlSendFlowSave");
  assert.ok(flow !== null);
  assert.ok(inOrder(flow, /dialogWriteStart\(panel, cx, DL_FLOW_SAVE_SECONDS\)/, /callCao\(/));
  assert.match(flow, /maxTime: DL_FLOW_SAVE_SECONDS/);
  for (const name of ["dlRunCheck", "dlFlowLoad"]) {
    const body = bodyOf(src, name);
    assert.ok(body !== null, `${name} exists`);
    assert.equal(count(body, /dialogWriteStart\(/g), 0, `${name} is a read: it does not block closing`);
  }
  assert.match(bodyOf(src, "dlFlowButtons"), /locked/, "under a confirmation the form's buttons are dimmed");
});

test("Create and enable is confirmed inside the dialog, and only the confirmed path sends enabled", () => {
  const src = dialog();
  const attempt = bodyOf(src, "dlAttemptFlowSave");
  assert.ok(attempt !== null);
  assert.match(attempt, /step\.action === "confirm"\) dlAskConfirm\(panel, cx, \(\) => flowDialogSpec\("createAndEnable", fields\.name\), \(cx2\) => dlSendFlowSave\(panel, cx2, true, fields\)\)/);
  assert.match(attempt, /else dlSendFlowSave\(panel, cx, false\)/);
  assert.equal(count(src.keep, /dlSendFlowSave\(panel, \w+, true/g), 1, "enable is true at one call, the confirm's");
  assert.match(bodyOf(src, "dlSendFlowSave"), /flowCreateBody\(fields, \{ enable \}\)/);
});

test("Saved flow shows in the sidebar: a save tells the reducer, which sets the sidebar's state, after the bar is told to read again", () => {
  const src = dialog();
  const after = bodyOf(src, "dlAfterFlowSave");
  assert.ok(after !== null);
  assert.ok(inOrder(after, /store\.set\(DL_FLOWS_STAMP_KEY, Date\.now\(\)\)/, /dialogEvent\(panel, cx, \{ type: "saved", list: "flows", item: /), "the stamp first, then the save event");
  assert.equal(count(after, /DL_RSIDE_KEY|dlCloseOverlay\(/g), 0, "the reducer's effects set the sidebar and close the dialog");
  assert.match(after, /dlNotify\(\{ title: "CAO", body: fx\.warning/, "a server that ignored enabled: false is still said");
  const failed = after.slice(after.indexOf("dialogWriteEnd("));
  assert.match(failed, /dialogWriteEnd\(panel, cx\)/, "a failed save ends the write and keeps the form");
});

test("Selection changes under an open dialog (Edit flow): the flow comes from the request's item and is read by the dialog itself", () => {
  const src = dialog();
  const init = bodyOf(src, "dlFlowInit");
  assert.ok(init !== null);
  assert.match(init, /request\.item/);
  assert.match(init, /emptyFlowForm\(\{ workflow: request\.workflow \}\)/, "Schedule a workflow: New flow preset to the workflow of the request");
  const load = bodyOf(src, "dlFlowLoad");
  assert.ok(load !== null && /flowRoute\(nf\.editName\)/.test(load) && /callCao\(/.test(load));
  const send = bodyOf(src, "dlSendFlowSave");
  assert.match(send, /flowRoute\(nf\.editName\)/, "Save sends PUT /flows/<the flow the dialog was opened for>");
  assert.match(send, /flowUpdateBody\(nf\.flow \?\? \{\}, fields\)/, "only what changed against the flow the dialog read");
  for (const name of ["dlFlowInit", "dlFlowLoad", "dlSendFlowSave"]) assert.equal(count(bodyOf(src, name), /request\.name\b/g), 0, `${name}: request.name is the dialog's own; the item travels as request.item`);
});


// ==== the workflow forms in the dialog host ====
// New workflow, Edit workflow and the Run form moved from the shipped panel into the dialog host. Their rules live in
// lib/workflows.js and lib/workflows-view.js (node tests) and lib/shell.js (the rows of "not submitted"); these scans pin
// that the dialog applies them, that a request is a write the frame is told about, and that nothing a user typed in
// the Run form is kept anywhere but the dialog's memory.

const WORKFLOW_RULES = ["workflowEditorGate", "editorGateWithName", "editorSizeCheck", "validateReport", "findingWords", "validateOutcomeNote", "workflowSaveResult",
  "workflowSaveEffects", "editFromDocument", "templateFor", "templateNeedsConfirm", "isTemplateText", "workflowFileHint", "workflowDialogSpec", "workflowValidationKey",
  "emptyWorkflowForm", "workflowEditorDirty", "readFailureText", "workflowRoute", "runFormModel", "runInputs", "runInputsOk", "runSubmitEffects", "approvalView",
  "saveRequest", "submitRequest", "recordApproval", "clearApproval"];

test("New workflow, Edit workflow and Run are drawn by the dialog host, from the lib rules of the workflow forms", () => {
  const src = dialog();
  const registry = /const DL_FORMS = \{([^}]*)\}/.exec(src.keep);
  assert.ok(registry !== null);
  for (const kind of ["new-workflow", "edit-workflow", "run-workflow"]) assert.match(registry[1], new RegExp(`"${kind}":`), kind);
  for (const name of WORKFLOW_RULES) assert.ok(count(src.keep, new RegExp(`(?<![\\w$.])${name}\\(`, "g")) >= 1, `${name} is called by the dialog host`);
});

test("Run opens in the dialog: a submit is a write the frame is told about, sent from the form as it was when the button was pressed", () => {
  const src = dialog();
  const send = bodyOf(src, "dlSendRun");
  assert.ok(send !== null, "dlSendRun exists");
  assert.ok(inOrder(send, /submitRequest\(/, /dialogWriteStart\(panel, cx, DL_RUN_SECONDS\)/, /callCao\(/), "the request is built, the frame told, then it is sent");
  assert.match(send, /maxTime: DL_RUN_SECONDS/, "the lost-answer limit and the curl limit are one number");
  const submit = bodyOf(src, "dlOnRunSubmit");
  assert.ok(submit !== null);
  assert.ok(inOrder(submit, /runInputs\(/, /runInputsOk\(/, /dlSendRun\(/), "the inputs are read and checked before anything is sent");
  assert.equal(count(send, /caowin\.|store\.get\(/g), 0, "the send reads no stored key");
});

test("Run opens in the dialog: a 202 stamps the bar's list, clears the row and sends the reducer a saved event for the workflow", () => {
  const after = bodyOf(dialog(), "dlAfterRun");
  assert.ok(after !== null, "dlAfterRun exists");
  assert.ok(inOrder(after, /store\.set\(DL_WORKFLOWS_STAMP_KEY, Date\.now\(\)\)/, /dialogEvent\(panel, cx, \{ type: "saved", list: "workflows", item: /), "the stamp first, then the save event");
  assert.match(after, /store\.set\(DL_APPROVALS_KEY, clearApproval\(rows, submission\.name\)\)/, "a good submit clears the row of the workflow");
  assert.equal(count(after, /DL_RSIDE_KEY|dlCloseOverlay\(/g), 0, "the reducer's effects set the sidebar and close the dialog");
  assert.match(after, /dialogWriteEnd\(panel, cx\)/, "any other answer ends the write and keeps the form");
});

test("First run needs approval: the row is written once, through recordApproval, from the outcome, and the inputs stay on the dialog's own record", () => {
  const src = dialog();
  const after = bodyOf(src, "dlAfterRun");
  assert.ok(after !== null);
  assert.equal(count(src.keep, /store\.set\(DL_APPROVALS_KEY, recordApproval\(/g), 1, "one writer of a row");
  assert.match(after, /const rows = panel\.store\.get\(DL_APPROVALS_KEY\);/, "the stored rows are read once");
  assert.match(after, /store\.set\(DL_APPROVALS_KEY, recordApproval\(rows, submission\.name, outcome, Date\.now\(\)\)\)/, "the row is built from the workflow's name, the outcome's plan id and the time");
  assert.match(after, /runSubmitEffects\(/);
  assert.match(after, /rf\.record = /, "the submission for 'submit again' is kept on the form's own record");
  assert.equal(count(src.keep, /DL_APPROVALS_KEY/g), 4, "the key's definition, the two writes and the one read that feeds them");
});

test("Submit again after approval: it sends the submission kept in the dialog, with no further read of the form", () => {
  const src = dialog();
  const again = bodyOf(src, "dlSubmitAgain");
  assert.ok(again !== null, "dlSubmitAgain exists");
  assert.match(again, /dlSendRun\(panel, cx, rf\.record\.submission\)/);
  assert.equal(count(again, /\.value\(\)/g), 0, "the kept submission is sent as it was, not what the fields hold now");
  assert.equal(count(src.keep, /POST \/workflows\/plans\/approve|plans\/approve/g), 0, "the window has no approve control");
  assert.ok(src.keep.includes("approvalView("), "the plan id, the command and Copy come from approvalView");
});

test("Inputs not readable: the Run form's fields come from the spec the dialog reads, as runFormModel gives them", () => {
  const src = dialog();
  const load = bodyOf(src, "dlRunLoad");
  assert.ok(load !== null, "dlRunLoad exists");
  assert.match(load, /workflowRoute\(rf\.name\)/);
  assert.equal(count(load, /dialogWriteStart\(/g), 0, "a read: it does not block closing");
  assert.match(bodyOf(src, "dlRunMakeFields"), /runFormModel\(/);
});

test("Edit after validate: Validate is a read, Save is a write, and Save sends what the editor held when pressed", () => {
  const src = dialog();
  const validate = bodyOf(src, "dlRunWorkflowValidate");
  assert.ok(validate !== null, "dlRunWorkflowValidate exists");
  assert.equal(count(validate, /dialogWriteStart\(/g), 0);
  assert.match(validate, /path: "\/workflows\/validate"/);
  const save = bodyOf(src, "dlSendWorkflowSave");
  assert.ok(save !== null, "dlSendWorkflowSave exists");
  assert.ok(inOrder(save, /saveRequest\(/, /dialogWriteStart\(panel, cx, DL_WORKFLOW_SAVE_SECONDS\)/, /callCao\(/));
  assert.match(save, /maxTime: DL_WORKFLOW_SAVE_SECONDS/);
  const after = bodyOf(src, "dlAfterWorkflowSave");
  assert.ok(after !== null);
  assert.ok(inOrder(after, /store\.set\(DL_WORKFLOWS_STAMP_KEY, Date\.now\(\)\)/, /dialogEvent\(panel, cx, \{ type: "saved", list: "workflows", item: /));
  assert.match(after, /dialogWriteEnd\(panel, cx\)/);
  assert.equal(count(bodyOf(src, "dlWorkflowLoad") ?? "", /dialogWriteStart\(/g), 0, "the read of the workflow to edit does not block closing");
});

test("Spec name differs: every Save and Validate decision reads the gate that compares the spec's name with the form's, and the editor says why", () => {
  const src = dialog();
  const gate = bodyOf(src, "dlWorkflowGateNow");
  assert.ok(gate !== null, "dlWorkflowGateNow exists");
  assert.ok(inOrder(gate, /workflowEditorGate\(/, /return editorGateWithName\(gate, nw\.form\);/), "the gate is the editor's gate with the form's name and source compared, not the editor's gate alone");
  // The three places that act on it: the Save press, the Validate press, and the two buttons.
  assert.match(bodyOf(src, "dlSendWorkflowSave"), /if \(!dlWorkflowGateNow\(panel, panel\.model\(\)\)\.save\) \{ cx\.notify\(\); return; \}/, "a Save press that the names forbid sends nothing");
  assert.match(bodyOf(src, "dlOnWorkflowValidate"), /dlWorkflowGateNow\(panel, panel\.model\(\)\)\.validate\)/, "Validate is asked of the same gate, which leaves it on");
  const buttons = bodyOf(src, "dlWorkflowButtons");
  assert.match(buttons, /const g = dlWorkflowGateNow\(panel, m\);/);
  assert.match(buttons, /"cao-wf-save"[^\n]*disabled: locked \|\| !g\.save/, "the Save button is off while the names differ");
  assert.match(buttons, /"cao-wf-validate"[^\n]*disabled: locked \|\| !g\.validate/, "the Validate button follows validate, not save");
  const body = bodyOf(src, "dlWorkflowBody");
  assert.match(body, /const g = dlWorkflowGateNow\(panel, m\);/);
  assert.match(body, /\.when\(Boolean\(g\.specNameProblem\), \(e\) => e\.child\(problemLine\(t, g\.specNameProblem\)\)\)/, "the line that says the spec's name must equal the workflow's");
});

test("Concurrent change and the template: both confirmations replace the form body inside the dialog", () => {
  const src = dialog();
  assert.equal(count(src.keep, /dlAskConfirm\(panel, cx, \(\) => workflowDialogSpec\("reload"\)/g), 1);
  assert.equal(count(src.keep, /dlAskConfirm\(panel, cx, \(\) => workflowDialogSpec\("template"\)/g), 1);
  assert.match(bodyOf(src, "dlWorkflowButtons") ?? "", /locked/, "under a confirmation the form's buttons are dimmed");
  assert.match(bodyOf(src, "dlRunButtons") ?? "", /locked/);
});

test("Selection changes under an open dialog (workflows): the workflow comes from the request's item, never the dialog's own name", () => {
  const src = dialog();
  for (const name of ["dlWorkflowInit", "dlRunInit", "dlWorkflowLoad", "dlRunLoad", "dlSendWorkflowSave", "dlSendRun"]) {
    const body = bodyOf(src, name);
    assert.ok(body !== null, `${name} exists`);
    assert.equal(count(body, /request\.name\b/g), 0, `${name}: request.name is the dialog's own; the item travels as request.item`);
  }
  assert.match(bodyOf(src, "dlWorkflowInit"), /request\.item/);
  assert.match(bodyOf(src, "dlRunInit"), /request\.item/);
});

test("Inputs never reach storage: nothing the Run form holds is named in a write of storage or in a notification", () => {
  const src = dialog();
  for (const m of src.keep.matchAll(/(?:store\.(?:set|setLocal)|dlNotify)\(([^;]*);/g)) {
    assert.doesNotMatch(m[1], /\brf\b|\bins\b|\.inputs\b|workingDirectory|\.workdir\b.*rf|serverMessage|record\.submission/, `a write names the form: ${m[0].slice(0, 80)}`);
  }
  assert.equal(count(bodyOf(src, "dlRunInit") ?? "", /store\.setLocal\(/g), 0, "the directory a launch used prefills the form; the form never writes it back");
});

// ==== the Draft with AI box in the dialog host ====
// The author session is the one thing a dialog starts that outlives it, so its record is written before the launch,
// completed by the 201, and left for the always-drawn mod when the dialog goes. The reducer (lib/draft.js) decides;
// these scans pin the order and what the dialog does not do.

const DRAFT_RULES = ["draftStep", "draftBox", "draftRequest", "fixRequest", "draftCall", "draftAnswer", "draftActionList", "draftActionKind", "draftPressCheck", "draftBeginCheck",
  "draftTickEvents", "draftJobDone", "draftFillPlan", "draftValidatedEvent", "draftValidationErrors", "draftFieldsOf", "draftFormDirty", "markedFlag", "clearMark",
  "recordAuthor", "afterDelete", "afterLaunch", "recordDraft", "initialDraft"];

test("Draft request: the box is drawn from the reducer's state, and every rule of it is called from lib", () => {
  const src = dialog();
  for (const name of DRAFT_RULES) assert.ok(count(src.keep, new RegExp(`(?<![\\w$.])${name}\\(`, "g")) >= 1, `${name} is called by the dialog host`);
  assert.equal(count(src.keep, /markForDeletion|draftMarks/g), 0, "the dialog marks nothing: the always-drawn mod finds an author by its record");
  assert.match(bodyOf(src, "dlDraftBox") ?? "", /draftBox\(/);
});

test("Dialog closed while the author launch is out: the author is recorded before the request is sent, and the request is built after it in the file", () => {
  const src = dialog();
  const launch = bodyOf(src, "dlDraftLaunch");
  assert.ok(launch !== null, "dlDraftLaunch exists");
  assert.ok(inOrder(launch, /recordAuthor\(/, /callCao\(/, /afterLaunch\(/), "recorded, sent, then the answer's status");
  const lines = src.keep.split("\n");
  const first = (re) => lines.findIndex((l) => re.test(l));
  assert.ok(first(/recordAuthor\(/) >= 0 && first(/recordAuthor\(/) < first(/draftRequest\(/), "recordAuthor( comes before draftRequest( in the file");
  assert.equal(lineCount(src, /afterLaunch\(/), 1);
});

test("Launch with no answer keeps the record: only the HTTP status of the answer decides, and a lost answer has none", () => {
  const launch = bodyOf(dialog(), "dlDraftLaunch");
  assert.ok(launch !== null);
  assert.match(launch, /afterLaunch\(\s*[\w.()]+,\s*[\w.]+,\s*outcome\.status\)/, "afterLaunch gets outcome.status and nothing else");
  assert.equal(count(launch, /kind === "unreachable"[^\n]*afterLaunch|afterLaunch\([^)]*(?:0|undefined|null)\)/g), 0, "a lost answer is not turned into a status");
});

test("Dialog closed during drafting: close does nothing to the record, and a delete is only ever built by draftCall", () => {
  const src = dialog();
  const close = bodyOf(src, "closeDialogHost");
  assert.ok(close !== null);
  assert.equal(count(close, /callCao|draft|author|Draft|Author|DL_AUTHORS_KEY|DL_DRAFTS_KEY|DELETE/g), 0, "closeDialogHost marks, deletes and forgets nothing");
  assert.equal(count(src.keep, /"DELETE"|'DELETE'|method: "DELETE"/g), 0, "no delete request is written here: draftCall builds it, and only for an author session of this window");
  assert.ok(count(src.keep, /(?<![\w$.])draftCall\(/g) >= 1);
});

test("Delete fails: a delete answer goes through afterDelete, and a 404 counts as deleted", () => {
  const src = dialog();
  const effect = bodyOf(src, "dlRunDraftEffect");
  assert.ok(effect !== null, "dlRunDraftEffect exists");
  assert.match(effect, /afterDelete\(/);
  assert.match(effect, /store\.set\(DL_AUTHORS_KEY, next\.authors\)/);
  assert.match(effect, /store\.set\(DL_DRAFTS_KEY, next\.drafts\)/);
  assert.ok(src.keep.includes("404") || /status === 404/.test(effect), "a 404 is a success");
});

test("Good draft: the author's output fills the form that asked, marks the fields, and only the form's own validation runs", () => {
  const src = dialog();
  const fill = bodyOf(src, "dlFillDraft");
  assert.ok(fill !== null, "dlFillDraft exists");
  assert.match(fill, /draftFillPlan\(/);
  assert.match(fill, /draftValidatedEvent\(/);
  assert.equal(count(fill, /dialogWriteStart\(|callCao\(|dlSendFlowSave|dlSendWorkflowSave|dlSendRun/g), 0, "saving, enabling and running stay the user's actions");
});

test("Fix: Fix with AI and Retry start an author only from a button, never from a tick", () => {
  const src = dialog();
  const press = bodyOf(src, "dlOnDraftAction");
  assert.ok(press !== null);
  assert.ok(inOrder(press, /draftActionKind\(/, /draftPressCheck\(/, /draftBeginCheck\(/, /draftRequest\(|fixRequest\(/), "kind, then the press check, then the begin check, then the request");
  const tick = bodyOf(src, "dlTickDrafts");
  assert.ok(tick !== null);
  assert.equal(count(tick, /dlOnDraftAction|draftRequest\(|fixRequest\(|type: "start"|type: "fix"/g), 0, "a tick feeds terminal and timeout events only");
});

test("Dialog closed during drafting: leaving with an author out asks first, and a draft box is in New flow and New workflow only", () => {
  const src = dialog();
  assert.match(src.keep, /draftFormDirty\(/);
  assert.equal(count(bodyOf(src, "dlWorkflowBody") ?? "", /!editing, \(e\) => e\.child\(dlDraftBox\(/g) + count(bodyOf(src, "dlFlowBody") ?? "", /!editing, \(e\) => e\.child\(dlDraftBox\(/g), 2);
});


// ==== the left sidebar's rows, the labels and the top bar's buttons ====
// The left sidebar's rows are one line each, a name that does not fit is cut with an ellipsis and its words
// are in the tooltip, the honesty labels and the pills are never cut, the agent is named as CAO names its tab, and the top bar's
// buttons carry no counts. The look cannot run under node; the draw code is pinned by reading it.

const LEFT_ROW_BODIES = ["sessionLineView", "agentRowView", "agentsBody", "headerView", "tagged"];

test("One line per row: the left sidebar's mod wraps nothing, and no row view of it asks for a second line", () => {
  const src = side();
  assert.equal(count(src.keep, /\.flex_wrap\(|\.whitespace_normal\(|\.text_wrap\(/g), 0, "no wrapping style in the file");
  for (const name of LEFT_ROW_BODIES) {
    const body = bodyOf(src, name);
    assert.ok(body !== null, `${name} exists`);
    assert.equal(count(body, /flex_wrap|\.py\(\d+\)\.flex_col|v_flex\(\)\.id\(`cao-r-/g), 0, `${name} wraps or stacks a second line`);
  }
  assert.equal(count(bodyOf(src, "agentRowView"), /r\.muted|\.child\(div\(\)\.text_size\(11\)\.text_color\(t\.muted\)\.child\(r\./g), 0, "no muted line under the agent row");
});

test("Names truncate: the session name, the agent name, the header title, the path, the flow text and the recap text end in an ellipsis", () => {
  const src = side();
  for (const [name, min] of [["sessionLineView", 1], ["agentRowView", 1], ["agentsBody", 2], ["headerView", 2], ["tagged", 1]]) {
    const body = bodyOf(src, name);
    assert.ok(body !== null, `${name} exists`);
    assert.ok(count(body, /(?<![\w$])truncate\(\)/g) >= min, `${name} truncates at least ${min} text(s)`);
  }
  // every truncate is on a line that says what it cuts, and never on a label or a pill
  const raw = readFileSync(join(ROOT, "mods", "cao-window-side", "main.js"), "utf8");   // the comments, where the reasons are
  const truncating = raw.split("\n").filter((l) => /(?<![\w$])truncate\(\)/.test(l.replace(/\/\/.*$/, "")) && !/^\s*\/\//.test(l));
  assert.ok(truncating.length >= 8, `${truncating.length} truncating lines`);
  for (const line of truncating) {
    assert.match(line, /\/\/ truncates: \S.{5,}/, `a truncate with no reason: ${line.trim().slice(0, 70)}`);
    assert.doesNotMatch(line.replace(/\/\/.*$/, ""), /label|pill|hint|\bwhole\(/, `a truncate on a label or a pill: ${line.trim().slice(0, 70)}`);
  }
});

test("Tooltip carries the words: the session line, the agent row, the pin, the header lines and the workers line each have one, from the view model", () => {
  const src = side();
  assert.match(bodyOf(src, "sessionLineView"), /\.tooltip\(line\.tip\)/);
  assert.match(bodyOf(src, "agentRowView"), /\.tooltip\(r\.tip\)/);
  assert.match(bodyOf(src, "agentRowView"), /\.tooltip\(r\.pinTip\)/);
  const body = bodyOf(src, "agentsBody");
  assert.ok(count(body, /\.tooltip\(/g) >= 2, "the note and the flow line (the recap's tooltip is in recapView, the context's in tagged)");
  assert.match(bodyOf(src, "tagged"), /\.tooltip\(text\)/, "the recap and the context text");
  assert.match(bodyOf(src, "headerView"), /\.tooltip\(/, "the header's title");
  // a tooltip is only wired on an element that has an id (stateful), and the state words are not drawn on the row
  assert.equal(count(bodyOf(src, "sessionLineView"), /word\(line\.state|line\.state/g), 0);
  assert.equal(count(bodyOf(src, "agentRowView"), /word\(r\.state|r\.state|r\.profile/g), 0);
});

test("Honesty labels are never cut: they are drawn whole, and only the text beside them gives way", () => {
  const src = side();
  for (const name of ["whole"]) {
    const line = src.keep.split("\n").find((l) => new RegExp(`const ${name} = `).test(l));
    assert.ok(line !== undefined, `${name} is defined`);
    const at = src.keep.indexOf(line);
    const def = src.keep.slice(at, src.keep.indexOf(";\n", at) + 1);
    assert.match(def, /flex_none\(\)/, `${name} does not shrink`);
    assert.match(def, /whitespace_nowrap\(\)/, `${name} does not wrap`);
    assert.doesNotMatch(def, /truncate\(\)|overflow_hidden\(\)/, `${name} is not clipped`);
  }
  assert.match(bodyOf(src, "sessionLineView"), /whole\(line\.hint\.label/, "the stuck hint's label");
  assert.match(bodyOf(src, "agentRowView"), /whole\(r\.label/, "the best guess label");
  assert.match(bodyOf(src, "agentsBody"), /whole\(head\.flow\.label/, "the flow link's label");
  assert.match(bodyOf(src, "tagged"), /whole\(label/, "the recap's and the context's label");
});

// ---- the person reads "schedule", CAO's own word is "flow" ----
// Every string the person reads that named a flow now says schedule: the top-bar button, the right sidebar's list and its states, the dialog
// titles, the form's labels and hints, the confirmations, the Agents header link, the toasts and tooltips. What stays: CAO's own names
// (the /flows routes, the `cao flow` and `cao schedule` commands, the cao-flow-<name> sessions, the file and symbol names, the shared keys,
// the kinds "flow" and "new-flow" the code passes around) and the server's own message text that the window matches.

// The string literals of a source text, comments blanked, as [text, line].
const literals = (text) => {
  const src = readSource(text);
  const out = [];
  let line = 1;
  for (let i = 0; i < src.keep.length; i += 1) {
    const c = src.keep[i];
    if (c === "\n") { line += 1; continue; }
    if (c === '"' || c === "'" || c === "`") {
      let j = i + 1;
      while (j < src.keep.length && src.keep[j] !== c && (c === "`" || src.keep[j] !== "\n")) j += src.keep[j] === "\\" ? 2 : 1;
      out.push([src.keep.slice(i + 1, j), line]);
      i = j;
    }
  }
  return out;
};
// A word "flow" or "flows" in prose: a literal with a space in it, the word on its own (not workflow, not cao-flow, not a path or a command).
const FLOW_WORD = /(?<![\w/-])[Ff]lows?(?![\w/-])/;
const PROSE_EXCEPTIONS = new Set([
  "a flow with this name already exists",   // flow_service's own message, matched verbatim (lib/flows.js FLOW_NAME_TAKEN_MESSAGE)
]);
const flowProse = (text) => literals(text).map(([t, line]) => [t.replace(/\$\{[^}]*\}/g, "$"), line]).filter(([t]) => /\s/.test(t) && FLOW_WORD.test(t) && !PROSE_EXCEPTIONS.has(t) && !/^A schedule is a CAO flow: /.test(t) && !/cao flow\b|\bcao-flow\b|\/flows\b/.test(t.replace(/look for its cao-flow session/, "")));

test("The prose scan finds the word flow in a sentence, and leaves CAO's own names, a workflow and a path alone", () => {
  assert.equal(flowProse('const a = "Run this flow now?"; const b = `Disable flow ${n}?`;').length, 2);
  assert.equal(flowProse('const a = "Flows fire only while cao-server runs";').length, 1);
  assert.deepEqual(flowProse('const a = "cao-flow-daily"; const b = "/flows/validate"; const c = "edit-flow"; const d = "caowin.flowsStamp"; const e = "A workflow runs"; const f = "look for its cao-flow session before"; const g = "run `cao flow list` now";'), []);
  assert.deepEqual(flowProse('// the flow\nconst a = "x"; /* flow here */'), [], "a comment is not a string");
  assert.deepEqual(flowProse('const a = `Started by schedule ${flow}`;'), [], "a variable in a template is not a word");
  assert.deepEqual(flowProse('const a = "A schedule is a CAO flow: a prompt that runs";'), [], "the one sentence that says what a schedule is");
  assert.deepEqual(flowProse('const a = "a flow with this name already exists";'), [], "the server's own text");
});

test("No sentence the person reads says flow: the library, the mods and the shared widgets say schedule", () => {
  const files = [...readdirSync(join(ROOT, "lib")).filter((f) => f.endsWith(".js") && !f.endsWith(".test.mjs")).map((f) => `lib/${f}`),
    ...readdirSync(join(ROOT, "mods"), { withFileTypes: true }).filter((e) => e.isDirectory()).flatMap((e) => readdirSync(join(ROOT, "mods", e.name)).filter((f) => f.endsWith(".js")).map((f) => `mods/${e.name}/${f}`))];
  assert.ok(files.length >= 20, `${files.length} files`);
  const found = files.flatMap((f) => flowProse(readFileSync(join(ROOT, f), "utf8")).map(([t, line]) => `${f}:${line}: ${t.slice(0, 70)}`));
  assert.deepEqual(found, []);
});

test("The top-bar button, the list titles and the dialog titles read Schedules and Schedule; the kinds and ids stay CAO's", () => {
  const sh = readFileSync(join(ROOT, "lib", "shell.js"), "utf8");
  assert.match(sh, /return kind === "flows" \? "Schedules" : "Workflows";/, "the button; the kind is CAO's 'flows'");
  assert.match(bodyOf(sidebar(), "sbBackBar"), /list === "flows" \? "Schedules" : "Workflows"/, "the back bar");
  assert.match(bodyOf(sidebar(), "sbListView"), /\.child\(kind === "flows" \? "Schedules" : "Workflows"\)/, "the list title");
  assert.match(bodyOf(sidebar(), "sbListView"), /"Loading schedules"/);
  assert.match(sidebar().keep, /const SB_SCHEDULE_HELP = "A schedule is a CAO flow:/, "the help says a schedule is a CAO flow");
  assert.match(bodyOf(sidebar(), "sbListView"), /\.when\(kind === "flows", \(e\) => e\.tooltip\(SB_SCHEDULE_HELP\)\)/, "on the list title of schedules only");
  assert.match(dialog().keep, /"new-flow": "New schedule", "edit-flow": "Edit schedule"/, "the dialog titles; the kinds are CAO's");
  assert.match(dialog().keep, /"Create schedule"/);
  assert.match(dialog().keep, /"Create and enable\.\.\."/, "the button words are unchanged");
  assert.match(dialog().keep, /`Schedule\.\.\. creates a schedule that runs workflow \$\{form\.workflow\}\./, "the workflow's Schedule... action is kept and says what it makes");
  assert.match(sidebar().keep, /"cao-wf-schedule", "Schedule\.\.\."/, "the workflow action keeps its name");
  assert.match(sidebar().keep, /openDialog\(\{ kind: "new-flow", workflow: name \}\)/, "and opens CAO's new-flow kind");
});

test("The Agents header link reads Started by schedule X and the times after a dot, with no second word schedule", () => {
  const body = bodyOf(side(), "agentsBody");
  assert.match(body, /const flowText = head\.flow === null \? "" : head\.flow\.schedule \? `\$\{head\.flow\.text\} \\u00b7 \$\{head\.flow\.schedule\}` : head\.flow\.text;/);
  assert.doesNotMatch(body, /schedule: /);
});

// ---- the text weight comes from the user's setting, not from our code ----
// Herdr's rows have no weight call, so the person's `font_weight` setting (for example 500) governs them; its block headings are
// `font_medium()` at 11.5 (mods/agents/main.js:439), its Agents rows are 12.5 (mods/agents/main.js:199) and its Spaces rows 13 (the list's
// root, mods/spaces/main.js:561). Our left sidebar, tabs and brand drew semibold and looked bolder than Herdr's window. What stays bold is
// what Herdr draws bold itself: the "!" square and the badge letter (:215, :516), a dialog's or a panel's heading and a toast's title.
test("No explicit weight in the left sidebar, the tabs and the brand row: font_semibold and font_bold are gone, the user's setting governs", () => {
  for (const [name, src] of [["the side mod", side()], ["the tabs mod", tabs()], ["the brand mod", brand()]])
    assert.equal(count(src.keep, /\.font_(?:semibold|bold|extrabold|black|thin|light|normal)\(/g), 0, `${name}: no weight but the setting's (a heading may say font_medium)`);
});

test("The block titles are Herdr's headings: font_medium at 11.5 in the dim token, and the rows keep the sizes of Herdr's lists", () => {
  const head = bodyOf(side(), "headerView");
  assert.match(head, /\.min_w\(0\)\.truncate\(\)\.text_size\(11\.5\)\.font_medium\(\)\.text_color\(t\.dim\)\.tooltip\(head\.tip\)/, "mods/agents/main.js:439");
  const row = bodyOf(side(), "agentRowView");
  assert.match(row, /h_flex\(\)\.id\(`cao-r-\$\{r\.id\}`\)\.py\(4\)\.pr\(8\)\.pl\(8 \+ r\.depth \* 14\)\.gap\(6\)\.items_center\(\)\.text_size\(12\.5\)\.rounded\(6\)/, "an Agents row is 12.5 px, as Herdr's (mods/agents/main.js:199)");
  assert.equal(count(bodyOf(side(), "sessionLineView"), /text_size\(/g), 0, "a Sessions row has no size of its own: it takes the block's 13, as a space row does (mods/spaces/main.js:561)");
  assert.match(bodyOf(side(), "render"), /\.text_color\(t\.foreground\)\.text_size\(13\)/, "the block's 13");
  const name = bodyOf(side(), "sessionLineView").split("\n").find((l) => l.includes("line.name")) ?? "";
  assert.match(name, /div\(\)\.flex_1\(\)\.min_w\(0\)\.truncate\(\)\.child\(line\.name\)/, "the session's name is plain text");
});

test("The shared drawing and the right sidebar are not bolder than Herdr's: row text has no weight, a section title is font_medium, and only headings Herdr draws bold stay bold", () => {
  const w = read("lib", "widgets.js");
  assert.equal(count(w.keep, /font_semibold|font_bold/g), 0, "a form's field heading is a section title: font_medium");
  assert.match(w.keep, /export const heading = [^\n]*\n\s*\.child\(wgDiv\(\)\.text_size\(12\)\.font_medium\(\)\.child\(text\)\)/);
  const sb = sidebar();
  assert.match(sb.keep, /const sbTitle = \(t, text\) => sbDiv\(\)\.text_size\(12\)\.font_medium\(\)\.text_color\(t\.foreground\)\.child\(text\);/, "a section title of a details view");
  const bold = sb.keep.split("\n").filter((l) => /font_semibold|font_bold/.test(l));
  for (const l of bold) assert.match(l, /\.child\((?:spec\.title|v\.name|kind === "flows" \? "[A-Za-z]+" : "Workflows")\)/, `only a confirmation's title or a panel or detail heading is bold, as Herdr's Files and Changes titles: ${l.trim().slice(0, 80)}`);
  assert.equal(bold.length, 4, "the confirmation title, the list title and the two detail names");
  const rowText = [bodyOf(sb, "sbRowView"), bodyOf(sb, "sbRunRow"), bodyOf(sb, "sbWorkflowDetails")].join("\n");
  assert.equal(count(rowText.split("\n").filter((l) => !/v\.name/.test(l)).join("\n"), /font_semibold|font_bold/g), 0, "a row's name or state word is plain text");
  const dlg = dialog().keep.split("\n").filter((l) => /font_semibold|font_bold/.test(l));
  assert.ok(dlg.length > 0 && dlg.length <= 8, "the dialog's headings keep their weight, as Herdr's dialogs' titles do (confirm, prompt, machine-add: font_semibold)");
});

// ---- colour-only marks, no pill text ----
// The words `Replied` and `Asking`/`needs you` left the rows: needs you is Herdr's compact "!" square (mods/agents/main.js:215), a reply is the
// done dot in a highlight token, and the words are the row's tooltip (lib/agents.js). Nothing the left sidebar or the shared drawing draws
// is a word for them any more.
test("No pill: the left sidebar's mod defines and draws none, and no word for needs you or a reply is in the drawing code", () => {
  for (const [name, src] of [["the side mod", side()], ["lib/ui.js", ui()], ["the tabs mod", tabs()]]) {
    assert.equal(count(src.keep, /\bpill\b/g), 0, `${name}: no pill helper, field or call`);
    assert.equal(count(src.keep, /"Replied"|"Asking"|'Replied'|'Asking'|`Replied|`Asking|needs you|\bReplied\b|\bAsking\b/g), 0, `${name}: no word for needs you or a reply`);
  }
  assert.equal(count(side().keep, /border_1\(\)\.border_color\(color\)/g), 0, "the outlined pill box is gone");
  assert.doesNotMatch(bodyOf(side(), "sessionLineView"), /line\.pill|\.pill\b/);
  assert.doesNotMatch(bodyOf(side(), "agentRowView"), /r\.pill|\.pill\b/);
  assert.equal(count(read("lib", "agents.js").keep, /\bpill\s*[:=]|\.pill\b|"Replied"|Asking/g), 0, "the view model has no pill and no pill text");
});

test("The marks have Herdr's shapes: the needs-you square is the compact form of Herdr's status() and the reply is the done dot in a token of its own", () => {
  const body = bodyOf(ui(), "shapeMark");
  assert.match(body, /bang: \(\) => uiDiv\(\)\.size\(14\)\.flex_none\(\)\.rounded\(4\)\.bg\(c\.blocked \+ "26"\)\.text_size\(10\.5\)\.font_bold\(\)\.text_color\(c\.blocked\)\.flex\(\)\.items_center\(\)\.justify_center\(\)\.child\("!"\)/, "mods/agents/main.js:215");
  assert.match(body, /"dot-hi": \(\) => uiDiv\(\)\.size\(7\)\.mx\(3\)\.flex_none\(\)\.rounded\(4\)\.bg\(c\.ansi2 \?\? c\.done \?\? c\.dim\)/, "the done dot's size and spacing, in ansi2");
  assert.notEqual(body.match(/dot: \(\) => [^\n]*/)?.[0], body.match(/"dot-hi": \(\) => [^\n]*/)?.[0], "told apart from the plain done dot");
});

test("Goal text: the recap's label has its own line above, the text below is the lines of goalLines at the block's width, one truncating line each, the whole text in the tooltip", () => {
  const src = side();
  const agents = bodyOf(src, "agentsBody");
  assert.match(agents, /this\.recapView\(t, head\.recap\)/, "the recap goes through its own view, not through tagged (changed on purpose: it was the label-first one-line tagged row)");
  assert.equal(count(agents, /this\.tagged\([^\n]*head\.recap/g), 0, "tagged no longer draws the recap");
  const body = bodyOf(src, "recapView");
  assert.ok(body !== null, "recapView exists");
  assert.match(body, /goalLines\(recap\.text, this\.width\)/, "the lines come from the pure clamp, at the width the block was last drawn at");
  assert.match(body, /\.tooltip\(recap\.text\)/, "the full text is the tooltip");
  const [label, lines] = [body.indexOf("whole(recap.label"), body.indexOf("goalLines(")];
  assert.ok(label >= 0 && label < lines, "the label comes first, on a line of its own above the text");
  assert.match(body, /v_flex\(\)/, "the label and the lines stack");
  assert.match(body, /text_size\(12\.5\)/, "the lines are at the text size of the agent rows");
  assert.equal(count(body, /(?<![\w$])truncate\(\)/g), 1, "each line is one truncating line (a safety net: the clamp already fits it)");
  assert.equal(count(body, /whitespace_nowrap\(\)|\.h\(\d|max_h\(|overflow_hidden\(\)/g), 0, "no fixed height or clip: the clamp is the clamp");
  assert.equal(count(body, /whole\(recap\.label/g), 1, "the label is `whole`: it never shrinks or wraps");
  assert.match(src.keep, /on_size_change\(\(size, cx\) => \{[\s\S]*?this\.width = width;[^}]*cx\?\.notify\(\)/, "the block's width is kept for the clamp");
});

test("The one multi-line text of the left sidebar: the recap; the context line, the flow line, the note and every row stay one line", () => {
  const src = side();
  assert.equal(count(bodyOf(src, "agentRowView"), /goalLines|recapView/g), 0, "rows stay one line");
  assert.equal(count(bodyOf(src, "sessionLineView"), /goalLines|recapView/g), 0, "session lines stay one line");
  assert.equal(count(src.keep, /goalLines\(/g), 1, "the clamp is used once, by the recap");
  assert.match(bodyOf(src, "agentsBody"), /this\.tagged\(t, `context \$\{head\.context\.percent\}%`, head\.context\.label\)/, "the context line is `context N%` and its label, a short line that is not clipped");
});

// The Sessions list is styled like Herdr's spaces list (mods/spaces/main.js of Herdr 0.1.17): a lettered badge before the name, and the state mark at the right end of the row.

test("A session row reads badge, name, draft tag, hint, extra mark, state mark: the marks at the right end, the extra one before the state, the name giving way", () => {
  const body = bodyOf(side(), "sessionLineView");
  const order = ["sessionBadge(", "line.name", '"draft"', "line.hint.text", "shapeMark(line.extra", "shapeMark(line.shape"].map((x) => body.indexOf(x));
  assert.ok(order.every((i) => i >= 0), `all six parts are drawn: ${order}`);
  assert.deepEqual([...order].sort((a, b) => a - b), order, "in that order");
  assert.match(body, /div\(\)\.flex_1\(\)\.min_w\(0\)\.truncate\(\)[^\n]*line\.name/, "the name takes the room and gives way with an ellipsis");
  assert.match(body, /\.when\(line\.extra !== null, \(e\) => e\.child\(shapeMark\(line\.extra, t\)\)\)\s*\.when\(line\.shape !== null, \(e\) => e\.child\(shapeMark\(line\.shape, t\)\)\)\s*;?\s*$/, "the extra mark (needs you, or a reply the state mark does not show) is drawn by shapeMark like the state mark, which is the last thing on the line");
  assert.match(body, /\.tooltip\(line\.tip\)/, "the tooltip still says the name and the state in words");
  assert.equal(count(body, /\.gap\(9\)/g), 1, "the badge is a gap(9) from the name, as in Herdr's space row (:499)");
});

test("The badge is drawn as Herdr's space avatar is: 18 px, rounded 5, a 10.5 px bold white letter, the fill the model's colour through badgeFill", () => {
  const helper = bodyOf(ui(), "sessionBadge");
  assert.ok(helper !== null, "sessionBadge is in lib/ui.js beside shapeMark");
  // mods/spaces/main.js:502
  assert.match(helper, /uiRow\(\)\.size\(18\)\.flex_none\(\)\.rounded\(5\)\.bg\(fill\)\.items_center\(\)\.justify_center\(\)\.text_size\(10\.5\)\.font_bold\(\)\.text_color\("#ffffff"\)\.child\(badge\.letter\)/);
  const row = bodyOf(side(), "sessionLineView");
  assert.match(row, /sessionBadge\(line\.badge, badgeFill\(line\.badge\.color, t\)\)/, "letter and colour name are the model's, the hex comes from the shared rule");
  assert.match(side().keep, /import \{[^}]*\bbadgeFill\b[^}]*\} from "\.\/agents\.js"/);
  assert.match(side().keep, /import \{[^}]*\bsessionBadge\b[^}]*\} from "\.\/ui\.js"/);
  assert.doesNotMatch(row, /#[0-9a-fA-F]{3,8}\b|rgba?\(/, "the row draws no colour literal of its own");
});

test("Badge colours are Herdr's stored assignment, not a hash: kept under one shared key, written when it changes, and no hash of the name anywhere", () => {
  const src = side();
  assert.match(src.keep, /colors: "caowin\.colors"/, "its own key: the window has no Herdr space ids");
  const body = bodyOf(src, "badgeColours");
  assert.ok(body !== null, "one method decides the colours");
  assert.match(body, /badgeColors\(this\.store\.get\(KEYS\.colors\), m\.sessions\.map\(\(s\) => s\.name\)\)/, "the stored colours and every session's name");
  assert.match(body, /if \(next\.changed\) this\.store\.set\(KEYS\.colors, next\.colors\)/, "persisted when it changes, as Herdr does in render() (:538)");
  assert.match(bodyOf(src, "sessionsBody"), /this\.badgeColours\(m\)/);
  assert.match(bodyOf(src, "sessionLineView"), /sessionLine\(s, \{[^}]*colors/, "the line gets the colours");
  const agents = readFileSync(join(ROOT, "lib", "agents.js"), "utf8");
  assert.equal(count(agents, /charCodeAt|codePointAt|hash|% *BADGE|\.length %/gi), 0, "no hash of the name");
});

test("The agent row reads icon, name, label, note, age, state mark, pin on one line, and the age is right-aligned and never cut", () => {
  const body = bodyOf(side(), "agentRowView");
  const order = ["programMark(r.icon", "r.name", "r.label", "r.parentNote", "r.age", "shapeMark(r.shape", "cao-pin-"].map((x) => body.indexOf(x));
  assert.ok(order.every((i) => i >= 0), `all seven parts are drawn: ${order}`);
  assert.deepEqual([...order].sort((a, b) => a - b), order, "in that order: [program icon] name ... age [state mark] [pin], as Herdr's Agents list ends a row with its mark");
  assert.match(body, /\.flex_1\(\)\.min_w\(0\)/, "the name takes the room, so the rest sits at the right edge");
  assert.match(body, /whole\(r\.age/, "the age is drawn whole");
  assert.match(body, /\.when\(r\.slot === "blank", \(e\) => e\.child\(div\(\)\.w\(PIN_SLOT\)\.flex_none\(\)\)\)/, "a row with neither pin nor crown keeps the slot's room, so every state mark is in one column");
  assert.match(side().keep, /const PIN_SLOT = 18;/, "the pin's width: its 14 px drawing and 2 px of padding on each side");
});

test("The program icon is Herdr's: svg() when tinted and image() when not, at the sizes of its lists (16 in the Agents list, 14 in the tabs), and the same file as it", () => {
  const helper = bodyOf(ui(), "programMark");
  assert.ok(helper !== null, "programMark is in lib/ui.js beside shapeMark");
  // mods/agents/main.js:80: p.tint ? svg(p.icon).size(size).flex_none().text_color(color) : image(p.icon).w(size).h(size).flex_none()
  assert.match(helper, /icon\.tint \? uiSvg\(icon\.path\)\.size\(size\)\.flex_none\(\)\.text_color\(color\) : uiImage\(icon\.path\)\.w\(size\)\.h\(size\)\.flex_none\(\)/);
  assert.match(ui().keep, /import \{[^}]*\bimage as uiImage\b[^}]*\} from "gpui-kit"/);
  assert.match(bodyOf(side(), "agentRowView"), /programMark\(r\.icon, t, 16, t\.foreground\)/, "16 px in the foreground, as mods/agents/main.js:304 draws it");
  assert.match(bodyOf(tabs(), "tabView"), /programMark\(v\.icon, t, 14, t\.muted\)/, "14 px muted, as mods/tabs/main.js:120 draws it");
  assert.doesNotMatch(helper, /#[0-9a-fA-F]{3,8}\b|rgba?\(/, "no colour literal");
  assert.match(side().keep, /import \{[^}]*\bprogramMark\b[^}]*\} from "\.\/ui\.js"/);
  assert.match(tabs().keep, /import \{[^}]*\bprogramMark\b[^}]*\} from "\.\/ui\.js"/);
  const logic = readFileSync(join(ROOT, "lib", "agents.js"), "utf8").split("\n").filter((l) => !/^\s*\/\//.test(l)).join("\n");
  assert.match(logic, /"icons\/agent\.svg"/, "the fallback is a file every Herdr home holds: its generic agent glyph");
  assert.equal(count(logic, /icons\/(claude|kiro|codex|gemini)/g), 0, "the window keeps no table of programs: Herdr's record says which");
  for (const f of ["install.sh"]) assert.doesNotMatch(readFileSync(join(ROOT, f), "utf8"), /icons\//, `${f} copies none of Herdr's icons`);
});

test("The Agents rows read the focused terminal: the row of the focused agent takes the selected fill, and a focus change repaints", () => {
  const src = side();
  assert.match(src.keep, /import \{[^}]*\bfocused\b[^}]*\} from "herdr"/);
  assert.match(src.keep, /import \{[^}]*\bfocusedRow\b[^}]*\} from "\.\/agents\.js"/);
  const body = bodyOf(src, "agentsBody");
  assert.match(body, /focusedRow\(tree, focused\(\)\)/, "the rule is the pure function, given Herdr's focus");
  assert.match(body, /agentRowView\(t, r, found, focus\)/, "each row is told which one is focused");
  assert.equal(count(body, /focus\.fold/g), 0, "no fold line to take the fill");
  const row = bodyOf(src, "agentRowView");
  assert.match(row, /focus === r\.id/);
  assert.match(row, /\.bg\(t\.selected\)/, "the fill of the selected session row");
  assert.equal(count(row, /focused\(\)|agentRows\(/g), 0, "the row view decides nothing about focus: the rule is in lib/agents.js");
  const tick = bodyOf(src, "tickAgents");
  assert.match(tick, /focusKey\(\)/);
  assert.match(src.keep, /const focusKey = \(\) => [^\n]*focused\(\)\?\.terminal[^\n]*focused\(\)\?\.agent/, "the key is Herdr's focused terminal and agent");
  assert.match(tick, /this\.paintedFocus/);
  assert.match(tick, /cx\.notify\(\)/, "a focus change from outside the window repaints within a tick");
});
test("The top bar's tab and the Agents row show one name: both come from lib/agents.js (agentName), and the tab reads no profile, id or role by hand", () => {
  const bar = tabs();
  assert.match(bar.keep, /import \{[^}]*\btabViews\b[^}]*\} from "\.\/agents\.js"/, "the tabs are the view model's");
  assert.equal(count(bar.keep, /\bagentName\b/g), 0, "the bar does not name an agent itself: tabViews does, with agentName");
  const body = bodyOf(bar, "tabView");
  assert.match(body, /\.child\(div\(\)\.text_size\(12\)\.child\(v\.name\)\)/, "the tab draws the model's name");
  assert.match(body, /\.tooltip\(v\.tip\)/, "and the model's tooltip, the state in words");
  assert.equal(count(body, /\.profile\b|\.window\b|\bx\.id\)|\.id\.slice/g), 0, "no profile, tab name or id by hand in the tab");
  assert.equal(count(bodyOf(side(), "agentRowView"), /\.profile/g), 0);
});

test("The bar's order is the block's: the tabs are the view model's list, the bar sorts nothing, and it reads no unfold key", () => {
  const bar = tabs();
  const shown = bodyOf(bar, "shown");
  assert.ok(shown !== null, "the bar's tabs come from one method");
  assert.match(shown, /tabViews\(s, m\.pins, \{ replied: m\.replied \}\)/, "the tabs of the session, the same call the block's rows come from");
  assert.equal(count(bar.keep, /\.sort\(|\.reverse\(|\.toSorted\(/g), 0, "the bar puts its tabs in no order of its own");
  assert.equal(count(bar.keep, /unfold/gi), 0, "the bar reads no unfold key");
});
test("The crown marks the main agent only: in the pin's slot at the right end of its row, after the name on its tab, one size, in the accent, and never in the Sessions list", () => {
  const helper = bodyOf(ui(), "crownIcon");
  assert.ok(helper !== null, "crownIcon is in lib/ui.js beside pinIcon");
  assert.match(helper, /uiSvg\(mark\.icon\)\.size\(14\)\.flex_none\(\)\.text_color\(c\[mark\.tone\]\)/, "svg(), one size, a theme token");
  assert.doesNotMatch(helper, /#[0-9a-fA-F]{3,8}\b|rgba?\(|\.when\(/, "no colour literal, nothing that depends on a state");
  const row = bodyOf(side(), "agentRowView");
  assert.match(row, /\.child\(div\(\)\.flex_1\(\)\.min_w\(0\)\.truncate\(\)\.child\(r\.name\)\)/, "the name alone has the room and gives way; the crown is not in its box");
  assert.match(row, /\.when\(r\.slot === "crown", \(e\) => e\.child\(h_flex\(\)\.w\(PIN_SLOT\)\.flex_none\(\)\.items_center\(\)\.justify_center\(\)\.child\(crownIcon\(CROWN_MARK, t\)\)\)\)/, "the crown sits in a slot of the pin's width, so the column aligns");
  assert.ok(row.indexOf("shapeMark(r.shape") < row.indexOf('r.slot === "crown"') && row.indexOf("shapeMark(r.shape") < row.indexOf('r.slot === "pin"'), "after the state mark, where the pin is");
  assert.equal(count(row, /\.main\b/g), 0, "the row view reads the slot, not the flag");
  assert.equal(count(row, /crownIcon\(/g), 1);
  assert.equal(count(bodyOf(tabs(), "tabView"), /crownIcon\(/g), 1);
  assert.equal(count(bodyOf(side(), "sessionLineView"), /crownIcon|CROWN_MARK|\.main\b/g), 0, "a crown on a letter badge would be noise: the Sessions row has none");
  assert.match(side().keep, /import \{[^}]*\bCROWN_MARK\b[^}]*\} from "\.\/agents\.js"/);
  assert.match(side().keep, /import \{[^}]*\bcrownIcon\b[^}]*\} from "\.\/ui\.js"/);
  assert.match(tabs().keep, /import \{[^}]*\bCROWN_MARK\b[^}]*\} from "\.\/agents\.js"/);
  assert.match(tabs().keep, /import \{[^}]*\bcrownIcon\b[^}]*\} from "\.\/ui\.js"/);
  assert.ok(existsSync(join(ROOT, "mods", "cao-window-side", "crown.svg")), "the drawing ships in the side mod's folder, next to the pin's");
  const agents = readFileSync(join(ROOT, "lib", "agents.js"), "utf8").split("\n").filter((l) => !/^\s*\/\//.test(l)).join("\n");
  assert.match(agents, /mods\/cao-window-side\/crown\.svg/);
});

test("No role tag on a tab: the bar draws no 'worker' or 'talkable' word, and no dim tag beside the name", () => {
  const bar = tabs();
  const body = bodyOf(bar, "tabView");
  assert.ok(body !== null && body.length > 100, "fixture: found the tab drawing");
  assert.equal(count(body, /worker|talkable/gi), 0, "the tab does not know a role");
  assert.equal(count(bar.keep, /\.child\("worker"\)|\.child\("talkable"\)/g), 0);
  assert.match(body, /\.child\(programMark\(v\.icon, t, 14, t\.muted\)\)\s*\.child\(div\(\)\.text_size\(12\)\.child\(v\.name\)\)\s*\.when\(v\.main, \(e\) => e\.child\(crownIcon\(CROWN_MARK, t\)\)\);\s*$/, "a tab is its program icon, its name and, for the main agent only, the crown, and nothing else after the name");
  assert.equal(count(body, /text_color\(t\.dim\)|text_size\(11\)/g), 0, "no small muted tag");
});

test("No state mark on a tab: the tab draws no shapeMark and reads no shape, the bar imports none, and its tooltip says the state", () => {
  const bar = tabs();
  const body = bodyOf(bar, "tabView");
  assert.ok(body !== null && body.length > 100, "fixture: found the tab drawing");
  assert.equal(count(body, /shapeMark|v\.shape|\.shape\b/g), 0, "no state mark on a tab");
  assert.equal(count(bar.keep, /\bshapeMark\b/g), 0, "the tabs mod does not import or call shapeMark");
  assert.match(body, /\.tooltip\(v\.tip\)/, "the tooltip is the model's, the state in words");
  assert.match(bodyOf(side(), "agentRowView"), /shapeMark\(r\.shape, t\)/, "the Agents row keeps its mark");
  assert.match(bodyOf(side(), "sessionLineView"), /shapeMark\(line\.shape, t\)/, "and the Sessions line its");
});

test("The pin is a drawing of our own mod folder: svg() draws the model's file at one size in a theme token, a filled shape when pinned, and no outline-only pinned shape", () => {
  const row = bodyOf(side(), "agentRowView");
  assert.match(row, /pinIcon\(pinMark\(r\.pinned\), t\)/, "the icon and its tone are the model's (pinMark), drawn by the shared helper");
  assert.equal(count(row, /\\u25c|\\u2666|[\u25c6\u25c7\u25c8\u2666]/g), 0, "no diamond");
  assert.equal(count(row, /\.child\(r\.pinned \?/g), 0, "the pin is not a text that changes with the state");
  assert.match(row, /cao-pin-\$\{r\.id\}/, "the click target keeps its id");
  assert.match(row, /\.when\(r\.slot === "pin", /, "drawn for a worker's slot only");
  assert.match(row, /\.tooltip\(r\.pinTip\)/, "and its tooltip");
  assert.match(row, /this\.togglePin\(r\.id\)/, "and its click");
  assert.match(side().keep, /import \{[^}]*\bpinMark\b[^}]*\} from "\.\/agents\.js"/);
  assert.match(side().keep, /import \{[^}]*\bpinIcon\b[^}]*\} from "\.\/ui\.js"/);
  const helper = bodyOf(ui(), "pinIcon");
  assert.ok(helper !== null, "pinIcon is in lib/ui.js beside shapeMark");
  assert.match(helper, /uiSvg\(mark\.icon\)\.size\(14\)\.flex_none\(\)\.text_color\(c\[mark\.tone\]\)/, "svg(), one size, a theme token");
  assert.doesNotMatch(helper, /#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(/, "no colour literal");
  assert.doesNotMatch(helper, /\.border|\.border_color|\.rounded/, "no outline is drawn around the icon: the pinned state is the filled drawing, not a stroke");
  assert.equal(count(helper, /\.when\(/g), 0, "nothing in the icon depends on the state: the size never changes, so the row never shifts");
  assert.equal(count(ui().keep, /uiSvg\("mods\/(?:workspace-chrome|cao-window-side)\/pin/g), 0, "the path is the model's, in one place (pinMark)");
  const agents = readFileSync(join(ROOT, "lib", "agents.js"), "utf8").split("\n").filter((l) => !/^\s*\/\//.test(l)).join("\n");   // the code, not the comments that explain the choice
  assert.match(agents, /mods\/cao-window-side\/pin-filled\.svg/, "the pinned drawing is in the side mod's folder, found by mods/<mod>/<file> as Herdr's tabs mod finds its win-*.svg (mods/tabs/main.js:153)");
  assert.match(agents, /mods\/cao-window-side\/pin-tilted\.svg/);
  assert.doesNotMatch(agents, /workspace-chrome\/pin/, "Herdr's outline pin is no longer drawn: it has no filled shape");
  for (const f of ["pin-filled.svg", "pin-tilted.svg"]) assert.ok(existsSync(join(ROOT, "mods", "cao-window-side", f)), `${f} ships in the side mod's folder`);
});

// ---- the top-bar buttons look like Herdr's Files and Changes buttons ----
// Herdr's tabs mod draws them in `panelButton` (mods/tabs/main.js:133-152 of the shipped mod, read only on the Mac): a 26 px row, px 8,
// gap 5, rounded 6, text 12, a 14 px svg() icon before the label, t.chrome and t.muted at rest, t.selected and t.foreground while its
// panel is shown, t.hover on hover, and the press stopped. Our icons are drawings of the tabs mod's own folder.
test("The top-bar buttons look like Herdr's: barButton uses panelButton's geometry and colours, and a 14 px icon of our own before the label", () => {
  const src = tabs();
  const body = bodyOf(src, "barButton");
  assert.ok(body !== null, "barButton exists");
  for (const v of [/\.h\(26\)/, /\.px\(8\)/, /\.gap\(5\)/, /\.rounded\(6\)/, /\.text_size\(12\)/, /\.items_center\(\)/, /\.flex_none\(\)/])
    assert.match(body, v, `Herdr's geometry: ${v}`);
  assert.match(body, /\.bg\(active \? t\.selected : t\.chrome\)/, "chrome at rest, selected while its list is open");
  assert.match(body, /\.text_color\(active \? t\.foreground : t\.muted\)/, "muted at rest, foreground while open");
  assert.match(body, /\.hover\(\(s\) => s\.bg\(t\.hover\)\)/, "the hover fill");
  assert.match(body, /on_mouse_down\("left", \(_e, cx\) => cx\.stop_propagation\(\)\)/, "the press is stopped");
  assert.match(body, /\.child\(svg\(icon\)\.size\(14\)\.flex_none\(\)\)\s*\.child\(label\)/, "a 14 px svg icon before the label");
  assert.equal(count(body, /t\.surface|border|\.py\(|\.px\(11\)/g), 0, "not the old faint-fill look");
  assert.match(src.keep, /import \{[^}]*\bsvg\b[^}]*\} from "gpui-kit"/, "svg() is gpui-kit's, as Herdr's tabs mod uses it");
  const r = bodyOf(src, "render");
  assert.match(r, /barButton\(t, "cao-bar-flows", BAR_ICONS\.flows, barButtonLabel\("flows"\)/, "Flows has its icon");
  assert.match(r, /barButton\(t, "cao-bar-workflows", BAR_ICONS\.workflows, barButtonLabel\("workflows"\)/, "Workflows has its icon");
  assert.match(src.keep, /const BAR_ICONS = \{ flows: "mods\/cao-window-tabs\/flows\.svg", workflows: "mods\/cao-window-tabs\/workflows\.svg" \}/, "found by mods/<mod>/<file>");
  for (const f of ["flows.svg", "workflows.svg"]) {
    const p = join(ROOT, "mods", "cao-window-tabs", f);
    assert.ok(existsSync(p), `${f} ships in the tabs mod's folder`);
    const text = readFileSync(p, "utf8");
    assert.match(text, /viewBox="0 0 24 24"/, `${f}: a 24 grid`);
    assert.match(text, /fill="none"/, `${f}: outline`);
    assert.match(text, /stroke="currentColor"/, `${f}: drawn in the text colour`);
    assert.doesNotMatch(text, /<(script|image|text|use)\b|href=|#[0-9a-fA-F]{3,8}\b/, `${f}: simple paths only, no colour of its own`);
  }
});

// ---- no worker fold ----
// The Agents block lists every worker, always: no "N workers" line, no "hide workers" line, no rule that folds a session of more than 12 agents,
// no `caowin.unfold` key and no chevron. A pin now decides only which agents get a top-bar tab. The block scrolls when its tree is taller than it.

const NO_FOLD = /unfold|foldMark|chevronIcon|foldWorkers|FOLD_OVER|hide workers|setUnfold|caowin\.unfold|mods\/files\/chevron|cao-w-\$\{|focus\.fold|tree\.fold|view\.fold/i;

test("No worker fold: no drawing file and no shared module names the fold line, its key, its chevron, its rule or its writers", () => {
  const files = [
    ...["cao-window-side", "cao-window-panel", "cao-window-tabs", "cao-window-status", "cao-window-brand", "cao-window-toast"].flatMap((m) => readdirSync(join(ROOT, "mods", m)).filter((f) => f.endsWith(".js")).map((f) => ["mods", m, f])),
    ...readdirSync(join(ROOT, "lib")).filter((f) => f.endsWith(".js") && !f.endsWith(".test.mjs")).map((f) => ["lib", f]),
  ];
  const hits = files.flatMap((parts) => [...read(...parts).keep.matchAll(new RegExp(NO_FOLD, "gi"))].map((m) => `${parts.join("/")}: ${m[0]}`));
  assert.deepEqual(hits, [], "every trace of the worker fold is gone");
});

test("No worker fold: the agentRows call takes no unfold argument and no fold line is read from its result", () => {
  const body = bodyOf(side(), "agentsBody");
  assert.match(body, /agentRows\(found, m\.pins, \{ now, replied: m\.replied, local: m\.local \}\)/);
  assert.match(body, /: \{ rows: \[\] \};/, "the empty view is the rows alone");
  assert.equal(count(bodyOf(side(), "agentsModel"), /unfolded/g), 0);
});

// The block scrolls: its body is `flex_1().min_h(0).overflow_y_scrollbar()` inside the block's own column, the shape gpui-kit.d.ts documents for a scrolling list
// (`overflow_y_scrollbar`: "Scrolls vertically and paints a base-layer scrollbar", gpui-kit.d.ts:1119-1121; Herdr's own mods are read on the Mac only, so
// no line of theirs is cited here). `min_h(0)` is what lets a flex child shrink below its content, so that the scrolling happens.
test("The Agents block scrolls vertically when its tree is taller than the block: the list is flex_1, min_h(0) and overflow_y_scrollbar", () => {
  const body = bodyOf(side(), "agentsBody");
  assert.match(flat(body), /return v_flex\(\)\.id\("cao-agents-list"\)\.flex_1\(\)\.min_h\(0\)\.overflow_y_scrollbar\(\)/, "the list scrolls");
  assert.match(flat(bodyOf(side(), "render")), /v_flex\(\)\.id\(`cao-side-\$\{this\.section\}`\)\.size_full\(\)\.min_h\(0\)\.overflow_y_scrollbar\(\)/, "inside the block's own scrolling column");
  assert.equal(count(body, /\.h\(\d|max_h\(|overflow_hidden\(\)/g), 0, "no fixed height cuts the tree");
});

// ---- the tab strip scrolls like Herdr's, and a divider and the buttons stay fixed at the right ----
// Herdr's tabs mod (mods/tabs/main.js:156-160 of the shipped mod, read only on the Mac): a `tab-scroll` h_flex with `flex_1().min_w(0)`,
// `overflow_x_scrollbar().track_scroll(this.scroll)` and an `on_size_change` that scrolls the active tab into view, an `on_scroll_wheel`
// on the bar (:155, the handler :39-62), `ScrollHandle.new()` in init (:33) and `scroll_to_item(active)` when the active tab changes
// (:69-73) and from each tab at or before it (:110). Ours mirrors them; the divider and the two buttons are siblings of the strip.

// The text of the call that opens at `from` (the index of its "("), by balanced parentheses on the blanked source, from the kept source.
const argsAt = (src, from) => {
  let depth = 0;
  for (let i = from; i < src.blank.length; i += 1) {
    if (src.blank[i] === "(") depth += 1;
    else if (src.blank[i] === ")") { depth -= 1; if (depth === 0) return src.keep.slice(from + 1, i); }
  }
  return null;
};

test("The argument scan finds the balanced text of a call, even with a string holding a parenthesis", () => {
  const src = readSource('a.child(h_flex().id("x)").children(f(1, (2))))\n.child(b)');
  assert.equal(argsAt(src, src.blank.indexOf("(")), 'h_flex().id("x)").children(f(1, (2)))');
  assert.equal(argsAt(readSource("a.child(b"), 7), null);
});

test("The strip scrolls like Herdr's tab-scroll: a ScrollHandle made in init, flex_1 and min_w(0), a horizontal scrollbar tracked by it, the active tab scrolled into view", () => {
  const src = tabs();
  assert.match(src.keep, /import \{[^}]*\bScrollHandle\b[^}]*\} from "gpui-base"/, "ScrollHandle is gpui-base's, as Herdr's tabs mod imports it");
  assert.match(bodyOf(src, "init"), /this\.scroll = ScrollHandle\.new\(\);/, "made once, in init (Herdr :33)");
  const r = bodyOf(src, "render");
  const at = r.indexOf('.id("cao-tab-scroll")');
  assert.ok(at > 0, "the strip is the element cao-tab-scroll");
  const strip = argsAt(readSource(r), readSource(r).blank.lastIndexOf(".child(", at) + ".child".length);
  assert.ok(strip !== null);
  for (const v of [/\.flex_1\(\)\.min_w\(0\)/, /\.h_full\(\)/, /\.overflow_x_scrollbar\(\)\.track_scroll\(this\.scroll\)/, /\.on_size_change\(\(_size, cx\) => \{ if \(ctx\.active >= 0\) \{ this\.scroll\.scroll_to_item\(ctx\.active\); cx\.notify\(\); \} \}\)/, /\.children\(ctx\.tabs\.map\(/])
    assert.match(strip, v, `Herdr's strip: ${v}`);
  assert.equal(count(src.keep, /overflow_hidden\(\)/g), 0, "the old clipped strip is gone: the strip scrolls");
  assert.match(r, /if \(ctx\.active >= 0\) this\.scroll\.scroll_to_item\(ctx\.active\);/, "the active tab is scrolled into view when it changes (Herdr :69-73)");
  assert.match(r, /selected !== this\.selected \|\| /, "only when the selection moved, so a hand scroll is not undone at every repaint");
  const tab = bodyOf(src, "tabView");
  assert.match(tab, /\.when\(i <= ctx\.active, \(e\) => e\.on_size_change\(\(_size, cx\) => \{ this\.scroll\.scroll_to_item\(ctx\.active\); cx\.notify\(\); \}\)\)/, "a tab at or before the active one re-scrolls when it changes size (Herdr :110)");
  assert.match(src.raw ?? readFileSync(join(ROOT, "mods", "cao-window-tabs", "main.js"), "utf8"), /mods\/tabs\/main\.js:\d+-\d+/, "the file cites the lines of Herdr's mod it mirrors");
});

test("The buttons never scroll: the divider and both buttons are siblings of the strip, after it, and the strip holds only tabs", () => {
  const src = tabs();
  const r = bodyOf(src, "render");
  const rs = readSource(r);
  const at = r.indexOf('.id("cao-tab-scroll")');
  const open = rs.blank.lastIndexOf(".child(", at) + ".child".length;
  const strip = argsAt(rs, open);
  assert.ok(strip !== null);
  for (const w of ["barButton", "cao-tab-divider", "cao-bar-flows", "cao-bar-workflows", "divider"]) assert.ok(!strip.includes(w), `${w} is not inside the scrolling strip`);
  const after = r.slice(open + strip.length);
  const order = ["cao-tab-divider", "cao-bar-flows", "cao-bar-workflows"].map((w) => after.search(w.replace(/[-]/g, "\\-")));
  assert.ok(order.every((i) => i >= 0), "all three follow the strip");
  assert.ok(order[0] < order[1] && order[1] < order[2], "divider, then the Flows button, then the Workflows button");
  assert.match(r, /h_flex\(\)\.id\("cao-tabs"\)\.size_full\(\)/, "the bar is the row that holds the strip and the fixed items");
});

test("The wheel over the bar steps the tab: on_scroll_wheel on the bar calls wheelTabStep with the swipe, keeps the new swipe and focuses the neighbour", () => {
  const src = tabs();
  assert.match(src.keep, /import \{[^}]*\bwheelTabStep\b[^}]*\binitialSwipe\b[^}]*\} from "\.\/shell\.js"|import \{[^}]*\binitialSwipe\b[^}]*\bwheelTabStep\b[^}]*\} from "\.\/shell\.js"/, "the rule is lib/shell.js's");
  assert.match(bodyOf(src, "init"), /this\.swipe = initialSwipe\(\);/);
  const r = bodyOf(src, "render");
  assert.match(r, /h_flex\(\)\.id\("cao-tabs"\)[^\n]*\n?[^\n]*\.on_scroll_wheel\(\(e\) => this\.wheel\(e, ctx\)\)/, "on the bar, as Herdr's is (:155)");
  const w = bodyOf(src, "wheel");
  assert.ok(w !== null, "wheel exists");
  assert.match(w, /wheelTabStep\(this\.swipe, e, Date\.now\(\)\)/);
  assert.match(w, /this\.swipe = [a-z]+\.swipe;/, "the state it returns is kept");
  assert.match(w, /focusTerminal\(/, "the neighbour tab is focused as a click does");
  assert.equal(count(src.keep, /run\("tab\./g), 0, "no Herdr tab command: the tabs are the window's own");
});

test("The divider before the buttons is 1 px wide, 16 px high, the border colour, and fixed", () => {
  const src = tabs();
  const r = bodyOf(src, "render");
  const line = r.split("\n").find((l) => l.includes("cao-tab-divider"));
  assert.ok(line, "a divider element");
  for (const v of [/\.w\(1\)/, /\.h\(16\)/, /\.flex_none\(\)/, /\.bg\(t\.border\)/, /\.mx\(4\)/]) assert.match(line, v, `${v}`);
  assert.match(readFileSync(join(ROOT, "mods", "cao-window-tabs", "main.js"), "utf8").split("\n").find((l) => l.includes("cao-tab-divider")) ?? "", /\/\/ fixed: \S.{5,}/, "the 16 px height says why it is fixed");
  assert.ok(!/cao-tab-divider[^\n]*(?:text|child\()/.test(line), "the divider holds no text");
});

test("Buttons read bare words: the bar asks for the label of a kind and passes no rows or state", () => {
  const src = tabs();
  const calls = [...src.keep.matchAll(/barButtonLabel\(([^)]*)\)/g)].map((m) => m[1].trim());
  assert.deepEqual(calls.filter((c) => c !== ""), ['"flows"', '"workflows"']);
  assert.match(bodyOf(src, "render"), /cao-bar-flows[^\n]*barButtonLabel\("flows"\)/);
});

// ==== a label is shown in full ====
// No honesty label and no pill is cut by a fixed row height, a truncate or
// an overflow clip. Every such use in the drawing files either is gone or says on its own line why it holds no label text
// (`// fixed: <why>`: a control with one short fixed word, an icon, a dot, a rule). The right sidebar's rows, the dialog and the
// pills still wrap, so they carry no marker at all. The left sidebar's mod alone may also
// truncate the text of a name, a path or a recap, on a line that says so (`// truncates: <what>`), and never on a line that
// holds a label, a pill or a hint.

const CLIP = /\.h\(\d+\)|(?<![\w$])truncate\(\)|overflow_hidden\(\)/;
const FIXED_MARK = /\/\/ fixed: \S.{5,}/;
const TRUNC = /(?<![\w$])truncate\(\)/;
const TRUNC_MARK = /\/\/ truncates: \S.{5,}/;
const SIDE_FILE = /(?:^|\/)cao-window-side\/main\.js$/;
const NO_MARK_ANCHORS = /cao-head-|cao-s-\$\{|cao-r-\$\{|cao-rside-\$\{|cao-stuck-|cao-wf-\$\{|cao-fl-\$\{|cao-d-flow|const pill =|cao-dialog-cancel|cao-dialog-confirm|cao-pr-\$\{/;
const NO_CLIP_BODIES = ["workflowRowView", "flowRowView", "sbRowView", "sbListView", "sbTagged"];

// The problems of one drawing file's text: a clip with no marker, a marker with no reason (the pattern needs five words
// of reason), and a marker or a clip on a row that must wrap.
export function clipProblems(text, file = "source") {
  const src = readSource(text);
  const blank = src.blank.split("\n");
  const raw = text.split("\n");
  const problems = [];
  blank.forEach((line, i) => {
    const where = `${file}:${i + 1}`;
    const clipped = CLIP.test(line);
    const code = raw[i].replace(/\/\/.*$/, "");
    const cutsText = TRUNC.test(line) && !/\.h\(\d+\)|overflow_hidden\(\)/.test(line) && TRUNC_MARK.test(raw[i]) && SIDE_FILE.test(file) && !/label|pill|hint|\bwhole\(/.test(code);
    if (TRUNC_MARK.test(raw[i]) && !cutsText) problems.push({ what: `a \`// truncates:\` line that is not a truncate of a name or a path in the left sidebar's mod, or that holds a label, a pill or a hint: ${raw[i].trim().slice(0, 70)}`, where });
    if (clipped && !cutsText && !FIXED_MARK.test(raw[i])) problems.push({ what: `a fixed height, truncate or overflow clip with no \`// fixed: <why>\` on its line: ${raw[i].trim().slice(0, 70)}`, where });
    if (NO_MARK_ANCHORS.test(raw[i]) && (FIXED_MARK.test(raw[i]) || clipped)) problems.push({ what: `a row that must wrap carries a clip or a marker: ${raw[i].trim().slice(0, 70)}`, where });
  });
  for (const name of NO_CLIP_BODIES) {
    const body = bodyOf(src, name);
    if (body !== null && CLIP.test(body.replace(/\/\/[^\n]*/g, ""))) problems.push({ what: `${name} holds label text and clips it`, where: file });
  }
  return problems;
}

const drawingFiles = () => ["lib/widgets.js", "lib/ui.js", ...readdirSync(join(ROOT, "mods"), { withFileTypes: true }).filter((e) => e.isDirectory())
  .flatMap((e) => readdirSync(join(ROOT, "mods", e.name)).filter((f) => f.endsWith(".js")).sort().map((f) => `mods/${e.name}/${f}`))];

test("Long label in the narrow sidebar: every fixed height, truncate and overflow clip of the drawing files says why it holds no label text", () => {
  const files = drawingFiles();
  assert.ok(files.length >= 10, `${files.length} drawing files`);
  for (const file of files) assert.deepEqual(clipProblems(readFileSync(join(ROOT, file), "utf8"), file), [], file);
});

test("Long session line: the clip scan flags what it must and leaves a marked control and a wrapping row alone", () => {
  const marked = 'const b = row().w(22).h(22).child("+");   // fixed: a control with one character\n';
  assert.deepEqual(clipProblems(marked), []);
  for (const bad of [
    'const r = row().id("x").h(24).child(name);\n',
    'const r = div().min_w(0).truncate().child(name);\n',
    'const r = col().overflow_hidden().child(label);\n',
    'const r = row().h(22).child(name);   // fixed:\n',
    'const r = row().h(22).child(name);   // fixed: ok\n',
  ]) assert.ok(clipProblems(bad).length > 0, `not flagged: ${bad.trim()}`);
  assert.deepEqual(clipProblems('const r = row().min_h(24).py(2).flex_wrap().child(name);\n// row().h(24) is only a comment\nconst s = "truncate()";\n'), []);
  assert.ok(clipProblems('const r = h_flex().id(`cao-s-${name}`).flex_wrap().h(26).child(x);   // fixed: the head holds a name\n').length > 0, "a session line may not carry the marker");
  assert.ok(clipProblems('const pill = (text) => div().h(16).child(text);   // fixed: a small pill\n').length > 0, "a pill may not carry the marker");
  for (const text of [
    'h_flex().id("cao-dialog-cancel").h(28).px(12).child(spec.cancelLabel);   // fixed: dead code: its own confirm\n',
    'h_flex().id("cao-dialog-confirm").h(28).px(12).child(spec.confirmLabel);   // fixed: dead code: its own confirm\n',
    'h_flex().id(`cao-pr-${r.name}`).h(24).px(8).child(r.name);   // fixed: dead code: its own list\n',
  ]) assert.ok(clipProblems(text).length > 0, `a button or a profile row holds label text and may not be fixed or marked: ${text.trim().slice(0, 50)}`);
  assert.deepEqual(clipProblems('h_flex().id("cao-dialog-cancel").min_h(28).px(12).child(spec.cancelLabel);\n'), [], "a row that wraps is fine");
  assert.ok(clipProblems("class A {\n  agentRowView(t, r) {\n    return div().truncate().child(r.profile);\n  }\n}\n").length > 0, "a body that holds label text may not clip it");
  assert.ok(clipProblems("class A {\n  workflowRowView(t, r) {\n    return div().truncate().child(r.profile);   // truncates: a name for a long row\n  }\n}\n", "mods/cao-window-side/main.js").length > 0, "a right-sidebar row body still may not clip");
});

test("Names truncate, labels do not: the clip scan lets the left sidebar's mod cut a name on a marked line, and nothing else", () => {
  const SIDE = "mods/cao-window-side/main.js";
  const name = "const n = div().flex_1().min_w(0).truncate().child(r.name);   // truncates: a name; the tooltip has it whole\n";
  assert.deepEqual(clipProblems(name, SIDE), []);
  assert.ok(clipProblems(name, "mods/cao-window-panel/sidebar.js").length > 0, "only the left sidebar's mod may truncate");
  assert.ok(clipProblems("const n = div().flex_1().min_w(0).truncate().child(r.name);\n", SIDE).length > 0, "an unmarked truncate is flagged");
  assert.ok(clipProblems("const n = div().truncate().child(r.name);   // truncates: x\n", SIDE).length > 0, "a reason of one word is no reason");
  for (const bad of [
    "const n = div().truncate().child(r.label);   // truncates: a label that is too long to show\n",
    "const n = div().truncate().child(line.hint.label);   // truncates: a hint that is too long to show\n",
    "const n = div().truncate().child(pill(text));   // truncates: a pill that is too long to show\n",
    "const n = div().h(22).truncate().child(r.name);   // truncates: a name in a fixed height row\n",
    "const n = div().overflow_hidden().child(r.name);   // truncates: a name in a clipped box here\n",
  ]) assert.ok(clipProblems(bad, SIDE).length > 0, `not flagged: ${bad.trim().slice(0, 60)}`);
  assert.ok(clipProblems("const r = h_flex().id(`cao-s-${s.name}`).truncate().child(x);   // truncates: a row that holds a pill\n", SIDE).length > 0, "a session line itself may not carry the marker");
});

// ==== the in-flight guards, the sidebar's run-state check and the typed YOLO name ====
// A double click must not send twice, a confirm must not send for a run whose state moved, and the YOLO confirmation
// must be given what the user typed. The order of calls is pinned above; these pin the early returns and the arguments.

// The conditions of the `if (<cond>) return;` lines of `body` that come before the first match of `stop`.
function earlyGuards(body, stop) {
  const at = body.search(stop);
  return [...(at < 0 ? body : body.slice(0, at)).matchAll(/\bif \((.*)\) return;/g)].map((m) => m[1]);
}

// What is wrong with one write function: it is missing, or no early return before it sets its flag or sends tests `guard`.
export function inFlightProblems(body, name, guard, stop) {
  if (body === null) return [`${name} is missing`];
  return earlyGuards(body, stop).some((g) => guard.test(g)) ? [] : [`${name} does not return early on ${guard} before it sends`];
}

// The arguments (their text, trimmed) of each call of `name(` in `source`, brackets matched on the blank twin.
export function callArgs(source, name) {
  const out = [];
  for (const m of source.blank.matchAll(new RegExp(`(?<![\\w$.])${name}\\(`, "g"))) {
    if (/\bfunction\s+$/.test(source.blank.slice(Math.max(0, m.index - 12), m.index))) continue;   // the definition
    const open = m.index + m[0].length - 1;
    const args = [];
    let depth = 0;
    let from = open + 1;
    for (let i = open; i < source.blank.length; i += 1) {
      const c = source.blank[i];
      if ("([{".includes(c)) depth += 1;
      else if (")]}".includes(c)) { depth -= 1; if (depth === 0) { args.push(source.keep.slice(from, i).trim()); break; } }
      else if (c === "," && depth === 1) { args.push(source.keep.slice(from, i).trim()); from = i + 1; }
    }
    out.push(args);
  }
  return out;
}

const SENDS = [
  ["dialog.js", "dlSendLaunch", /\bns\.sending\b/, /Object\.assign\(ns|dialogWriteStart\(|cx\.spawn\(/],
  ["dialog.js", "dlSendFlowSave", /\bnf\.sending\b/, /nf\.sending = true|dialogWriteStart\(|cx\.spawn\(/],
  ["dialog.js", "dlSendRun", /\brf\.sending\b/, /rf\.sending = true|dialogWriteStart\(|cx\.spawn\(/],
  ["sidebar.js", "sbSendFlowWrite", /opOf\(sb\.ops, scope\)\?\.state === "running"/, /sbSetOp\(|cx\.spawn\(/],
  ["sidebar.js", "sbSendRunWrite", /opOf\(sb\.ops, scope\)\?\.state === "running"/, /sbSetOp\(|cx\.spawn\(/],
];

test("Close during a launch: the scan of the in-flight guards flags a send that has none, and leaves the real shapes alone", () => {
  const ok = "function dlSendLaunch(panel, cx) {\n  const ns = panel.dl.ns;\n  if (ns.sending) return;\n  if (panel.gate(panel.model()).disabled) { cx.notify(); return; }\n  Object.assign(ns, { sending: true });\n  cx.spawn(async () => {});\n}\n";
  const guard = /\bns\.sending\b/;
  const stop = SENDS[0][3];
  assert.deepEqual(inFlightProblems(bodyOf(readSource(ok), "dlSendLaunch"), "dlSendLaunch", guard, stop), []);
  for (const [what, text] of Object.entries({
    "no guard": ok.replace("  if (ns.sending) return;\n", ""),
    "a guard after the flag is set": ok.replace("  if (ns.sending) return;\n", "").replace("  cx.spawn", "  if (ns.sending) return;\n  cx.spawn"),
    "a guard that names another flag": ok.replace("if (ns.sending) return;", "if (ns.banner) return;"),
    "a guard that does not return": ok.replace("if (ns.sending) return;", "if (ns.sending) cx.notify();"),
    "a guard in a comment": ok.replace("  if (ns.sending) return;\n", "  // if (ns.sending) return;\n"),
  })) assert.equal(inFlightProblems(bodyOf(readSource(text), "dlSendLaunch"), "dlSendLaunch", guard, stop).length, 1, `not flagged: ${what}`);
  assert.equal(inFlightProblems(null, "gone", guard, stop).length, 1, "a function that is not there is a problem, not a pass");
});

test("Close during a launch: each write returns early while one is already out, before it sets its flag or sends", () => {
  for (const [file, name, guard, stop] of SENDS) {
    const src = file === "dialog.js" ? dialog() : sidebar();
    assert.deepEqual(inFlightProblems(bodyOf(src, name), name, guard, stop), [], `${file} ${name}`);
  }
});

test("Run opens in the dialog: a second press of Submit while the run is out returns before it builds a request", () => {
  // dlSendRun has no idempotency key to fall back on: a second POST would start a second run.
  const body = bodyOf(dialog(), "dlSendRun");
  assert.ok(body !== null);
  assert.ok(body.search(/if \(rf\.sending\) return;/) >= 0 && body.search(/if \(rf\.sending\) return;/) < body.search(/submitRequest\(/), "the guard comes first");
});

test("Close during a launch: the flow save's guard names its own sending flag, so a second click sends nothing", () => {
  const guards = earlyGuards(bodyOf(dialog(), "dlSendFlowSave"), /nf\.sending = true/);
  assert.ok(guards.some((g) => /\bnf\.sending\b/.test(g) && /\bgate\.disabled\b/.test(g)), `the guard reads ${JSON.stringify(guards)}`);
});

test("Answer after leaving the details: a confirm sends only for an item that is not already running, and a run write only while the run's state allows it", () => {
  const src = sidebar();
  const flow = bodyOf(src, "sbSendFlowWrite");
  const run = bodyOf(src, "sbSendRunWrite");
  assert.match(flow, /const scope = scopeOf\("flows", name\);/, "the flow write is kept under the flow's scope");
  assert.match(run, /const scope = scopeOf\("workflows", name\);/, "the run write is kept under the workflow's scope");
  for (const [name, body] of [["sbSendFlowWrite", flow], ["sbSendRunWrite", run]]) {
    const ops = [...body.matchAll(/sbSetOp\(sb, \{ (?:\.\.\.entry|item: scope)/g)];
    assert.ok(ops.length >= 2, `${name} records its answer under the scope`);
  }
  const check = /const check = runWriteCheck\(action, snap, sb\.wds\.get\(name\)\?\.runs\);/.exec(run);
  assert.ok(check !== null, "the run's state is read from the details' own list of runs through runWriteCheck");
  assert.ok(check.index < run.search(/cx\.spawn\(/), "the check comes before the request");
  assert.ok(run.search(/if \(!check\.send\) \{[^\n]*sbSetOp\([^\n]*return;/) > check.index, "a run whose state moved is not sent: the card says why and returns");
  assert.equal(count(run, /\bsend: true\b/g), 0, "no stand-in check that always sends");
});

test("YOLO: the typed name is the fourth argument of launchConfirm in the send, from the field and for YOLO only", () => {
  const calls = callArgs(readSource(bodyOf(dialog(), "dlSendLaunch")), "launchConfirm");
  assert.equal(calls.length, 1, "one confirmation check in the send");
  assert.equal(calls[0].length, 4);
  assert.equal(calls[0][3], 'kind === "yolo" ? dl.ins.typed.value() : ""', "what was typed, not the session name the form holds");
  assert.equal(count(dialog().keep, /dl\.ins\.typed\b/g) >= 2, true, "the field exists and is read");
});

test("the argument scan reads the arguments of a call and not of its definition or of another call", () => {
  const src = readSource('function launchConfirm(a, b) {}\nconst x = launchConfirm(form, kind, { s: f(1, 2) }, kind === "y" ? g(a, b) : "");\nconst y = other(launchConfirm(1, 2));\n');
  assert.deepEqual(callArgs(src, "launchConfirm"), [["form", "kind", "{ s: f(1, 2) }", 'kind === "y" ? g(a, b) : ""'], ["1", "2"]]);
});

// ==== end of the dialog host cases ====

// ---- the shared cache is merged before it is published, and the Agents copy adopts it ----

test("syncCache reads the shared key first, merges it through sharedCache, and publishes only what changed", () => {
  const src = side();
  const body = bodyOf(src, "syncCache");
  assert.ok(body !== null);
  const get = body.indexOf("this.store.get(KEYS.cache)");
  const merge = body.indexOf("sharedCache(");
  const set = body.indexOf("this.store.set(KEYS.cache");
  assert.ok(get >= 0 && merge > get && set > merge, "read the shared value, then merge it, then publish");
  assert.match(body, /sharedCache\(this\.cache, .*, fetched, stored\)/, "the stored value is what the merge is given");
  assert.match(body, /cacheKey\(next\.published\)\s*===\s*cacheKey\(stored\)/, "unchanged: nothing is written");
  assert.equal(count(src.keep, /publishedCache/g), 0, "no copy's memory of what it last published stands in for the shared key's value");
  assert.equal(count(src.keep, /store\.set\(KEYS\.cache/g), 1, "one writer of the key");
});
test("The Agents copy adopts what the Sessions copy published on its own tick, without a fetch or a write, and repaints when it learned something", () => {
  const src = side();
  const tick = bodyOf(src, "tickAgents");
  assert.match(tick, /this\.adoptCache\(/);
  const body = bodyOf(src, "adoptCache");
  assert.ok(body !== null);
  assert.match(body, /this\.store\.get\(KEYS\.cache\)/);
  assert.match(body, /sharedCache\(/);
  assert.equal(count(body, /store\.set|callCao|exec\(|fetchTerminal|markPending/g), 0, "a read only: no write and no request");
  assert.match(body, /cx\.notify\(\)/, "the block repaints when its cache grew");
});
test("The pending mark carries the time, so a lost fetch is asked for again", () => {
  assert.match(bodyOf(side(), "tick"), /markPending\(this\.cache, ids, now\)/);
});

await finish();
