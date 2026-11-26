// make-window.jq derives the CAO window's layout from a base `main` layout: the derived content, a shipped `main` that
// changed shape, and an extra layout prop. Skips (exit 0) when jq is absent.
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { assert, finish, hasTool, skip, test } from "./test/harness.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const SCRIPT = join(here, "make-window.jq");
const BASE = JSON.parse(readFileSync(join(here, "layouts", "main.json"), "utf8"));
const PROPS = JSON.parse(readFileSync(join(here, "layouts", "props.default.json"), "utf8"));
if (!hasTool("jq")) skip("make-window.test.mjs: jq is not installed");

// `props` and `toast` are passed the way install.sh passes them: --argjson. toast undefined = not passed.
function run(layout, props = PROPS, toast) {
  const extra = toast === undefined ? [] : ["--argjson", "toast", JSON.stringify(toast)];
  const r = spawnSync("jq", ["--argjson", "props", JSON.stringify(props), ...extra, "-f", SCRIPT],
    { input: typeof layout === "string" ? layout : JSON.stringify(layout), encoding: "utf8" });
  return { status: r.status, stdout: r.stdout, stderr: r.stderr, json: r.status === 0 ? JSON.parse(r.stdout) : null };
}
const ids = (node) => [node?.id, ...(node?.children ?? []).flatMap(ids)].filter(Boolean);
function find(node, id) {
  if (node?.id === id) return node;
  for (const c of node?.children ?? []) { const hit = find(c, id); if (hit) return hit; }
  return null;
}
const without = (layout, id) => JSON.parse(JSON.stringify(layout), function (k, v) {
  return Array.isArray(v) && k === "children" ? v.filter((c) => c?.id !== id) : v;
});
const CAO_BLOCKS = ["brand", "cao-sessions", "cao-agents", "cao-window-panel", "cao-window-tabs", "status"];
// What the earlier three-block version derived from layouts/main.json (the committed layout before the rework).
const EARLIER = JSON.parse(readFileSync(join(here, "test", "fixtures", "cao-window.three-block.json"), "utf8"));
const EARLIER_SIDE = ["cao-sessions", "cao-flows", "cao-workflows"];   // (earlier layout)
// Where a block stands: its parent's id and its place among the parent's children.
function placeOf(node, id, parent = null) {
  const kids = node?.children ?? [];
  const at = kids.findIndex((c) => c?.id === id);
  if (at >= 0) return { parent: node.id, at, of: kids.length };
  for (const c of kids) { const hit = placeOf(c, id, node); if (hit) return hit; }
  return null;
}
const count = (layout, id) => ids(layout).filter((i) => i === id).length;

test("Derived layout content: the two sidebar blocks, brand, panel, tabs and status carry identical props", () => {
  const { json } = run(BASE);
  for (const s of ["sessions", "agents"]) {
    const b = find(json, `cao-${s}`);
    assert.equal(b.content.mod, "cao-window-side");
    assert.equal(b.content.props.section, s);
    assert.deepEqual(b.size, { weight: 1 }, `${s} has weight 1`);
  }
  for (const [id, mod] of [["brand", "cao-window-brand"], ["cao-window-panel", "cao-window-panel"], ["cao-window-tabs", "cao-window-tabs"], ["status", "cao-window-status"]]) {
    assert.equal(find(json, id).content.mod, mod);
  }
  for (const id of CAO_BLOCKS) {
    const { section, ...rest } = find(json, id).content.props;
    assert.deepEqual(rest, PROPS, id);
  }
});

test("Derived layout content: the top bar and the right sidebar carry their fields, and status keeps the base block's size", () => {
  const { json } = run(BASE);
  const bar = find(json, "cao-window-tabs");
  assert.equal(find(json, "work").children[0].id, "cao-window-tabs", "the top bar is not the first block of the work column");
  assert.deepEqual([bar.size, bar.min, bar.window_drag], [{ px: 34 }, 34, true]);
  const side = find(find(json, "work-body"), "cao-window-panel");
  assert.ok(side, "the right sidebar is not in work-body");
  assert.deepEqual([side.hidden, side.size, side.min, side.max], [true, { px: 368 }, 280, 900]);
  assert.deepEqual(find(json, "status").size, find(BASE, "status").size);
  assert.equal(find(json, "work").children.some((c) => c.id === "work-body"), true);
  assert.equal(find(json, "main").content.tab, "active", "the active-tab block must survive");
});

// The user can drag the left sidebar's edge. What lets the edge
// drag is the ROOT row's `resizable` (default true: its children's boundaries drag) and the sidebar's own `min` and `max`
// (the range of the drag); the sidebar column's `resizable: false` locks only the boundaries between its own children
// (brand, lists, footer), which are fixed blocks, so it stays false (Herdr's layout notes say `resizable` applies to that row or column only;
// the shipped workspace-chrome mod says "dragging its edge narrows it").
test("Resizable sidebar: the left sidebar keeps 268 px and may be dragged between 200 and 640 px", () => {
  const { json } = run(BASE);
  const side = find(json, "sidebar");
  assert.deepEqual(side.size, { px: 268 }, "the default width is unchanged");
  assert.deepEqual([side.min, side.max], [200, 640]);
  assert.equal(side.type, "column");
  assert.equal(side.resizable, false, "its own children are fixed-size blocks, so their boundaries stay locked");
  assert.notEqual(find(json, "root").resizable, false, "the root row's boundary is the one the user drags, so it is not locked");
});

test("Resizable sidebar: a base whose root row is locked, or whose sidebar has another range, is derived to the same range", () => {
  const odd = JSON.parse(JSON.stringify(BASE));
  find(odd, "root").resizable = false;
  Object.assign(find(odd, "sidebar"), { min: 44, max: 480 });
  const { json } = run(odd);
  assert.equal(find(json, "root").resizable, true);
  assert.deepEqual([find(json, "sidebar").min, find(json, "sidebar").max], [200, 640]);
  const again = run(json).json;
  assert.deepEqual(again, json, "a second run changes nothing");
  assert.deepEqual(find(again, "sidebar").size, { px: 268 });
});

test("Resizable sidebar: the script sets the range and leaves the base's own width alone", () => {
  const dragged = JSON.parse(JSON.stringify(BASE));
  find(dragged, "sidebar").size = { px: 410 };
  assert.deepEqual(find(run(dragged).json, "sidebar").size, { px: 410 });
});

// The top-left of the window shows Herdr's wordmark "Herdr"; it must say "CAO".
// Herdr draws that row with its shipped workspace-chrome mod in the `brand` block, so the derivation points the `brand`
// block at the window's own mod, and only that block: the footer of the sidebar is the same mod with the prop `footer`.
const blocksOf = (node) => [...(node?.content ? [node] : []), ...(node?.children ?? []).flatMap(blocksOf)];

test("The top-left says CAO: the brand block is drawn by cao-window-brand, with its place, size and drag strip as Herdr's, and the footer stays Herdr's", () => {
  const was = find(BASE, "brand");
  assert.equal(was.content.mod, "workspace-chrome", "fixture: Herdr's own brand block");
  const { json } = run(BASE);
  const now = find(json, "brand");
  assert.equal(now.content.mod, "cao-window-brand");
  assert.deepEqual([now.type, now.size, now.window_drag], [was.type, was.size, was.window_drag]);
  assert.deepEqual(placeOf(json, "brand"), placeOf(BASE, "brand"));
  assert.equal(count(json, "brand"), 1);
  assert.deepEqual(find(json, "sidebar-footer"), find(BASE, "sidebar-footer"), "the footer keeps its mod and its props");
  assert.deepEqual(blocksOf(json).filter((b) => b.content.mod === "workspace-chrome").map((b) => b.id), ["sidebar-footer"], "the footer is the one block left on Herdr's wordmark mod");
});

test("The top-left says CAO: a second run, an earlier derivation and other props all end with the same brand block", () => {
  const once = run(BASE).json;
  assert.deepEqual(run(once).json, once);
  assert.equal(find(EARLIER, "brand").content.mod, "workspace-chrome", "fixture: the earlier derivation kept Herdr's brand");
  const { json } = run(EARLIER);
  assert.equal(find(json, "brand").content.mod, "cao-window-brand");
  assert.deepEqual(find(json, "sidebar-footer"), find(EARLIER, "sidebar-footer"));
  const props = { ...PROPS, caoPort: 9777, machine: "box" };
  assert.deepEqual(find(run(once, props).json, "brand").content.props, props, "the brand block is stamped like every CAO block");
});

test("The top-left says CAO: a brand block of another mod, or with no content, and a base with no brand block are left as they are", () => {
  const mine = structuredClone(BASE);
  find(mine, "brand").content = { mod: "mine", props: { x: 1 } };
  assert.deepEqual(find(run(mine).json, "brand").content, { mod: "mine", props: { x: 1 } }, "the user's own mod is not replaced");
  const none = run(without(BASE, "brand"));
  assert.equal(none.status, 0, none.stderr);
  assert.equal(count(none.json, "brand"), 0, "a brand block is not made up");
  assert.equal(blocksOf(none.json).filter((b) => b.content.mod === "cao-window-brand").length, 0);
});

test("Derived layout content: the root toast key is set only when the toast switch is on", () => {
  assert.equal(run(BASE, PROPS, true).json.toast, "cao-window-toast");
  assert.equal(run(BASE, PROPS, false).json.toast, undefined);
  assert.equal(run(BASE).json.toast, undefined);                       // the default: no toast mod
  assert.equal(run(run(BASE, PROPS, true).json, PROPS, true).json.toast, "cao-window-toast");
});

test("Derived layout content: no spaces, agents or tabs block and no block of the earlier version; every other base block is kept", () => {
  const { json } = run(BASE);
  const out = ids(json);
  for (const gone of ["spaces", "agents", "tabs", "cao-flows", "cao-workflows"]) assert.ok(!out.includes(gone), gone);   // (earlier layout)
  for (const id of ids(BASE).filter((i) => !["spaces", "agents", "tabs"].includes(i))) assert.ok(out.includes(id), `lost ${id}`);
});

test("a block the user added to the base layout is kept, and the order of the surviving blocks is unchanged", () => {
  const custom = structuredClone(BASE);
  find(custom, "sidebar-lists").children.push({ id: "my-extra", type: "block", content: { mod: "mine" } });
  const { json } = run(custom);
  assert.deepEqual(find(json, "my-extra"), { id: "my-extra", type: "block", content: { mod: "mine" } });
  const kept = ids(BASE).filter((i) => !["spaces", "agents", "tabs"].includes(i));
  assert.deepEqual(ids(json).filter((i) => kept.includes(i)), kept);
});

test("Left sidebar holds sessions only: cao-sessions then cao-agents, and no block of the Flows or Workflows lists", () => {
  const col = find(run(BASE).json, "sidebar-lists");
  assert.deepEqual(col.children.map((c) => c.id), ["cao-sessions", "cao-agents"]);
  for (const c of col.children) assert.ok(["sessions", "agents"].includes(c.content.props.section), c.id);
  for (const gone of EARLIER_SIDE.slice(1)) assert.ok(!find(run(BASE).json, gone), gone);   // (earlier layout)
});

test("Hidden at first start: the right sidebar is hidden in the layout and is never given a size of 0", () => {
  const side = find(run(BASE).json, "cao-window-panel");
  assert.equal(side.hidden, true);
  assert.ok(side.min >= 280 && side.size.px > 0, "a hidden block is shown and hidden by id, not resized to 0");
  const shown = structuredClone(BASE);                  // a base whose right block stands there, shown
  find(shown, "work-body").children.push({ id: "cao-window-panel", type: "block", size: { px: 500 }, content: { mod: "cao-window-panel", props: null } });
  assert.equal(find(run(shown).json, "cao-window-panel").hidden, true);
  assert.equal(find(run(BASE).json, "panel").hidden, true, "Herdr's own hidden panel column is untouched");
});

test("Derived layout content: running it on its own output changes nothing", () => {
  const once = run(BASE).json;
  assert.deepEqual(run(once).json, once);
  const twiceToast = run(BASE, PROPS, true).json;
  assert.deepEqual(run(twiceToast, PROPS, true).json, twiceToast);
});

test("Layout of an earlier derivation: the three-block output re-derives to what the base gives, with no id twice", () => {
  assert.deepEqual(EARLIER_SIDE.filter((i) => find(EARLIER, i)), EARLIER_SIDE, "the fixture is not the earlier three-block output");
  const { json, status } = run(EARLIER);
  assert.equal(status, 0);
  for (const gone of EARLIER_SIDE.slice(1)) assert.ok(!find(json, gone), gone);   // (earlier layout)
  assert.deepEqual(find(json, "sidebar-lists").children.map((c) => c.id), ["cao-sessions", "cao-agents"]);
  const all = ids(json);
  assert.deepEqual(all.filter((id, i) => all.indexOf(id) !== i), [], "an id appears twice");
  assert.deepEqual(json, run(BASE).json, "re-deriving the earlier output is not the layout derived from the base");
  assert.deepEqual(run(json).json, json, "a second run changes the result");
});

test("Layout of an earlier derivation: the top bar and the right sidebar get the new fields where they stand", () => {
  const before = { bar: placeOf(EARLIER, "cao-window-tabs"), side: placeOf(EARLIER, "cao-window-panel") };
  assert.equal(find(EARLIER, "cao-window-tabs").min, 0, "the fixture's bar has the old min");
  assert.equal(find(EARLIER, "cao-window-panel").hidden, undefined, "the fixture's sidebar is not hidden");
  const { json } = run(EARLIER);
  assert.deepEqual({ bar: placeOf(json, "cao-window-tabs"), side: placeOf(json, "cao-window-panel") }, before);
  assert.deepEqual([find(json, "cao-window-tabs").min, find(json, "cao-window-tabs").size], [34, { px: 34 }]);
  const side = find(json, "cao-window-panel");
  assert.deepEqual([side.hidden, side.min, side.max, side.size], [true, 280, 900, { px: 368 }]);
});

test("Layout of an earlier derivation: a block of the side mod is removed wherever it stands, other blocks stay", () => {
  const odd = structuredClone(EARLIER);
  find(odd, "work-body").children.push({ id: "stray-side", type: "block", content: { mod: "cao-window-side", props: { section: "flows" } } });
  find(odd, "sidebar-lists").children.push({ id: "mine", type: "block", content: { mod: "mine" } });
  const { json } = run(odd);
  assert.ok(!find(json, "stray-side"));
  assert.deepEqual(find(json, "mine"), { id: "mine", type: "block", content: { mod: "mine" } });
  assert.deepEqual(find(json, "sidebar-lists").children.map((c) => c.id).slice(0, 3), ["cao-sessions", "cao-agents", "mine"]);
});

test("Layout of an earlier derivation: other props than the defaults are re-stamped on every block", () => {
  const props = { ...PROPS, caoPort: 9777, machine: "box" };
  const { json } = run(EARLIER, props);
  for (const id of CAO_BLOCKS) {
    const { section, ...rest } = find(json, id).content.props;
    assert.deepEqual(rest, props, id);
  }
});

test("Status block kept: re-deriving a previous output leaves exactly one status block, in the same place, with the same size", () => {
  for (const [name, previous] of [["earlier", EARLIER], ["current", run(BASE).json]]) {
    const resized = structuredClone(previous);
    find(resized, "status").size = { px: 36 };                         // a size the user chose in the previous output
    const before = placeOf(resized, "status");
    const { json } = run(resized);
    assert.equal(count(json, "status"), 1, `${name}: status blocks`);
    assert.deepEqual(placeOf(json, "status"), before, `${name}: place`);
    assert.deepEqual(find(json, "status").size, { px: 36 }, `${name}: size`);
    assert.equal(find(json, "status").content.mod, "cao-window-status");
  }
  const { json } = run(EARLIER);
  assert.deepEqual([placeOf(json, "status"), find(json, "status").size], [placeOf(EARLIER, "status"), find(EARLIER, "status").size]);
  assert.equal(count(json, "status"), 1);
});

test("the script does not edit the layout it is given as text: the same input gives the same output", () => {
  assert.equal(run(BASE).stdout, run(BASE).stdout);
});

test("a second run with other props re-stamps every CAO block", () => {
  const once = run(BASE).json;
  const next = run(once, { ...PROPS, caoPort: 9999, machine: "other" }).json;
  for (const id of ["cao-sessions", "cao-window-panel", "status"]) {
    assert.equal(find(next, id).content.props.caoPort, 9999);
    assert.equal(find(next, id).content.props.machine, "other");
  }
  assert.equal(find(next, "cao-agents").content.props.section, "agents");
  assert.deepEqual(ids(next), ids(once), "re-stamping must not add or drop a block");
});

test("extra props reach every CAO block, whatever their JSON type", () => {
  const props = { ...PROPS, talkableProfiles: ["coordinator"], quietMinutes: 3, draftProfile: "author", contextPercentRegex: "ctx (\\d+)%", flag: false, nothing: null };
  const { json } = run(BASE, props);
  for (const id of CAO_BLOCKS) {
    const { section, ...rest } = find(json, id).content.props;
    assert.deepEqual(rest, props, id);
  }
});

for (const anchor of ["sidebar-lists", "work", "work-body", "status"]) {
  test(`a layout without "${anchor}" exits 5, names it and prints nothing`, () => {
    const r = run(without(BASE, anchor));
    assert.equal(r.status, 5);
    assert.equal(r.stdout, "");
    assert.ok(r.stderr.includes(anchor), r.stderr);
  });
}

test("a base that is not a layout at all exits 5 and prints nothing", () => {
  for (const text of ["{}", "[]", "null", "42", '"main"', '{"id":"root","children":[]}']) {
    const r = run(text);
    assert.equal(r.status, 5, `${text}: ${r.stderr}`);
    assert.equal(r.stdout, "", text);
  }
});

test("an anchor that is only a leaf block still counts, and the check is by id, not by position", () => {
  const moved = structuredClone(BASE);
  const col = find(moved, "sidebar-lists");
  col.children.reverse();                                           // spaces/agents order changed in some Herdr release
  const { json } = run(moved);
  assert.deepEqual(find(json, "sidebar-lists").children.slice(0, 2).map((c) => c.id), ["cao-sessions", "cao-agents"]);
});

test("props that are not a JSON object are refused and nothing is printed", () => {
  for (const bad of [[], ["a"], "x", 5, null]) {
    const r = run(BASE, bad);
    assert.notEqual(r.status, 0, `props ${JSON.stringify(bad)} produced a layout`);
    assert.equal(r.stdout, "", JSON.stringify(bad));
  }
});

test("a layout whose blocks have no content object does not crash the script", () => {
  const odd = structuredClone(BASE);
  find(odd, "brand").content = null;
  find(odd, "sidebar-footer").content = "text";
  const r = run(odd);
  assert.equal(r.status, 0, r.stderr);
  assert.equal(find(r.json, "brand").content, null);
});

test("layouts/cao-window.json is what the script makes from layouts/main.json", () => {
  const committed = JSON.parse(readFileSync(join(here, "layouts", "cao-window.json"), "utf8"));
  assert.deepEqual(committed, run(BASE).json);
});

await finish();
