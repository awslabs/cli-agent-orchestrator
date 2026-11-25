// lib/shell.js: the shell state of the window (specs cao-window-shell, cao-window-workflows and the status
// bar of cao-window-sessions; design D27 to D30; tasks 19.1 to 19.8). A case is named with its scenario's
// name first, then a colon and the detail.
//
// The module is imported as a namespace so a function that is not written yet fails its own case with
// "x is not a function" instead of stopping the whole file at the import line.
import { assert, finish, test } from "../test/harness.mjs";
import { draftRequest } from "./draft.js";
import { draftCall } from "./draft-view.js";
import { HONESTY } from "./honesty.js";
import { applyOutcome, barText, knownGapNote } from "./logic.js";
import { approvalRecord } from "./workflows-view.js";
import * as S from "./shell.js";

const DOT = "\u00b7"; // the middle dot of the product copy, kept as an escape so this file stays ASCII

// Deep freeze, so a function that mutates its argument throws in strict mode (ES modules are strict).
const freeze = (v) => {
  if (v && typeof v === "object" && !Object.isFrozen(v)) { Object.freeze(v); for (const k of Object.keys(v)) freeze(v[k]); }
  return v;
};

// ---- 29.2: the top bar's tab strip scrolls like Herdr's tabs, and a wheel gesture steps one tab ----
// Herdr's tabs mod (mods/tabs/main.js:39-62 of the shipped mod) turns the wheel into `tab.prev` or `tab.next`: one step per notch of a
// mouse (delta_lines), and for a touchpad or smooth wheel (pixel deltas) a short swipe of 20 px is one step, a longer swipe goes on
// at 60 px a step, never faster than one step in 100 ms, and a pause of 250 ms or a turn of direction begins a new gesture.
// wheelTabStep is that rule, pure: -1 is the previous tab (Herdr's tab.prev, a positive delta), 1 the next, 0 none.

const SWIPE0 = () => S.initialSwipe();

test("Wheel steps: a notch of a mouse wheel is one step, a positive delta back and a negative one forward, zero none", () => {
  assert.deepEqual(SWIPE0(), { acc: 0, steps: 0, at: 0, last: 0 });
  assert.equal(S.wheelTabStep(SWIPE0(), { delta_lines: { y: 3 } }, 1000).step, -1);
  assert.equal(S.wheelTabStep(SWIPE0(), { delta_lines: { y: 1 } }, 1000).step, -1);
  assert.equal(S.wheelTabStep(SWIPE0(), { delta_lines: { y: -1 } }, 1000).step, 1);
  assert.equal(S.wheelTabStep(SWIPE0(), { delta_lines: { y: 0 } }, 1000).step, 0);
  assert.equal(S.wheelTabStep(SWIPE0(), { delta_lines: { x: 4 } }, 1000).step, 0, "a sideways notch is no step: Herdr reads y only");
  const swipe = freeze({ acc: 7, steps: 2, at: 900, last: 800 });
  assert.deepEqual(S.wheelTabStep(swipe, { delta_lines: { y: 1 } }, 1000).swipe, swipe, "a notch leaves the swipe as it was");
});

test("Wheel steps: a short touchpad swipe of 20 px is one step, a smaller one waits for the rest", () => {
  const one = S.wheelTabStep(SWIPE0(), { delta: { y: 25 } }, 1000);
  assert.equal(one.step, -1);
  assert.deepEqual(one.swipe, { acc: 5, steps: 1, at: 1000, last: 1000 });
  const a = S.wheelTabStep(SWIPE0(), { delta: { y: 10 } }, 1000);
  assert.equal(a.step, 0);
  assert.deepEqual(a.swipe, { acc: 10, steps: 0, at: 1000, last: 0 });
  const b = S.wheelTabStep(a.swipe, { delta: { y: 10 } }, 1050);
  assert.equal(b.step, -1, "10 and 10 reach 20");
  assert.deepEqual(b.swipe, { acc: 0, steps: 1, at: 1050, last: 1050 });
  assert.equal(S.wheelTabStep(SWIPE0(), { delta: { y: -20 } }, 1000).step, 1, "20 exactly is enough, and a negative delta is forward");
  assert.equal(S.wheelTabStep(SWIPE0(), { delta: { y: 19 } }, 1000).step, 0);
});

test("Wheel steps: a longer swipe goes on at 60 px a step and at most one step in 100 ms", () => {
  const first = S.wheelTabStep(SWIPE0(), { delta: { y: 25 } }, 1000);
  const fast = S.wheelTabStep(first.swipe, { delta: { y: 60 } }, 1050);
  assert.equal(fast.step, 0, "65 px are enough but 50 ms is too soon");
  assert.deepEqual(fast.swipe, { acc: 65, steps: 1, at: 1050, last: 1000 });
  const later = S.wheelTabStep(fast.swipe, { delta: { y: 0 } }, 1100);
  assert.equal(later.step, -1, "100 ms after the last step");
  assert.deepEqual(later.swipe, { acc: 5, steps: 2, at: 1100, last: 1100 });
  const short = S.wheelTabStep(first.swipe, { delta: { y: 30 } }, 1200);
  assert.equal(short.step, 0, "35 px are under the 60 that a step needs once the swipe goes on");
});

test("Wheel steps: a pause of more than 250 ms, a turn of direction and a started phase each begin a new gesture", () => {
  const going = freeze({ acc: 40, steps: 3, at: 1000, last: 900 });
  const paused = S.wheelTabStep(going, { delta: { y: 5 } }, 1251);
  assert.deepEqual(paused.swipe, { acc: 5, steps: 0, at: 1251, last: 900 }, "the count starts again");
  const kept = S.wheelTabStep(going, { delta: { y: 5 } }, 1250);
  assert.deepEqual(kept.swipe, { acc: 45, steps: 3, at: 1250, last: 900 }, "250 ms exactly is the same gesture");
  const turned = S.wheelTabStep(freeze({ acc: 12, steps: 1, at: 1000, last: 900 }), { delta: { y: -8 } }, 1010);
  assert.deepEqual(turned.swipe, { acc: -8, steps: 0, at: 1010, last: 900 });
  const started = S.wheelTabStep(going, { delta: { y: 5 }, touch_phase: "started" }, 1010);
  assert.deepEqual(started.swipe, { acc: 5, steps: 0, at: 1010, last: 900 });
});

test("Wheel steps: a malformed event is no step and no change of the count, and the input state is never changed", () => {
  const swipe = freeze({ acc: 4, steps: 1, at: 1000, last: 900 });
  for (const e of [null, undefined, {}, { delta: null }, { delta: {} }, { delta: { y: "x" } }, { delta: { y: NaN } }, { delta: { y: Infinity } }, 5, "wheel"]) {
    const r = S.wheelTabStep(swipe, e, 1010);
    assert.equal(r.step, 0, JSON.stringify(e));
    assert.equal(r.swipe.acc, 4, `${JSON.stringify(e)} adds nothing`);
  }
  for (const bad of [null, undefined, 3, "x", [], { acc: "a" }]) assert.equal(S.wheelTabStep(bad, { delta: { y: 25 } }, 1000).step, -1, `a bad state ${JSON.stringify(bad)} reads as a fresh one`);
});

// ---- 27.17: the OS title of the window ----

test("The window title is CAO: the constant the status mod hands to setWindowTitle is the three letters", () => {
  assert.equal(S.WINDOW_TITLE, "CAO");
});

// ---- 27.20: the wordmark at the top-left of the sidebar ----

test("The top-left says CAO: the text the brand mod draws beside the logo is the three letters, and the window's title", () => {
  assert.equal(S.BRAND_TEXT, "CAO");
  assert.equal(S.BRAND_TEXT, S.WINDOW_TITLE, "the wordmark and the OS title name the window the same way");
});

// ---- 32.1: the CAO mark at the top-left, in the variant that suits the theme (design D42 A) ----
// `isDarkTheme(theme)` reads the luminance of the row's own fill (`chrome`, else `background`): WCAG relative luminance below 0.179, the
// point where white and black draw at the same contrast, is dark. `brandMark(theme)` is the mod-relative path of the drawing for it.

const MARK_LIGHT = "mods/cao-window-brand/cao-mark.svg";
const MARK_DARK = "mods/cao-window-brand/cao-mark-dark.svg";

test("Brand mark theme rule: exact colours at both ends and either side of the 0.179 luminance line", () => {
  const cases = [
    ["#000000", true], ["#ffffff", false], ["#1e1e2e", true], ["#f4f1e9", false], ["#eff1f5", false], ["#0b1024", true],
    ["#757575", true], ["#767676", false],   // 0.1779 and 0.1812: the line sits between these two greys
  ];
  for (const [chrome, dark] of cases) assert.equal(S.isDarkTheme({ chrome }), dark, chrome);
});

test("Brand mark theme rule: short, upper-case and alpha hex forms read as their colour", () => {
  assert.equal(S.isDarkTheme({ chrome: "#000" }), true);
  assert.equal(S.isDarkTheme({ chrome: "#fff" }), false);
  assert.equal(S.isDarkTheme({ chrome: "#FFFFFF" }), false);
  assert.equal(S.isDarkTheme({ chrome: "#1E1E2E" }), true);
  assert.equal(S.isDarkTheme({ chrome: "#000000ff" }), true);
  assert.equal(S.isDarkTheme({ chrome: "#ffffff00" }), false, "the alpha is not part of the brightness");
});

test("Brand mark theme rule: chrome decides; background stands in only when chrome is missing or not a colour", () => {
  assert.equal(S.isDarkTheme({ chrome: "#ffffff", background: "#000000" }), false, "chrome is the row's fill, so it wins");
  assert.equal(S.isDarkTheme({ chrome: "#000000", background: "#ffffff" }), true);
  assert.equal(S.isDarkTheme({ background: "#000000" }), true);
  assert.equal(S.isDarkTheme({ chrome: "red", background: "#000000" }), true);
  assert.equal(S.isDarkTheme({ chrome: "", background: "#ffffff" }), false);
});

test("Brand mark theme rule: nothing readable is not dark", () => {
  for (const t of [undefined, null, 0, "#000000", [], {}, { chrome: 5 }, { chrome: "#00" }, { chrome: "#gggggg" }, { chrome: "rgb(0,0,0)" }]) assert.equal(S.isDarkTheme(t), false, JSON.stringify(t));
});

test("Brand mark: the dark drawing on a dark theme and the light one otherwise, as paths under the brand mod's folder", () => {
  assert.equal(S.brandMark({ chrome: "#1e1e2e" }), MARK_DARK);
  assert.equal(S.brandMark({ chrome: "#f4f1e9" }), MARK_LIGHT);
  assert.equal(S.brandMark(undefined), MARK_LIGHT);
  assert.equal(S.brandMark({}), MARK_LIGHT);
  assert.equal(S.BRAND_MARK_SIZE, 20, "the size and radius of the logo Herdr drew there");
  assert.equal(S.BRAND_MARK_RADIUS, 5);
});

// ---- 19.1: barButtonLabel (owner review of the live window, change F: no counts on the two buttons) ----

const FLOW = (name, enabled, over = {}) => ({ name, schedule: "0 9 * * mon-fri", enabled, next_run: null, ...over });
const flowList = (flags) => flags.map((on, i) => FLOW(`f${i}`, on));
const read = (rows, at = 1000) => ({ rows, at, auth: false, unreachable: false });

test("Button labels: the buttons read 'Schedules' and 'Workflows', whatever the lists hold", () => {
  assert.equal(S.barButtonLabel("flows"), "Schedules");
  assert.equal(S.barButtonLabel("workflows"), "Workflows");
  const wf = [{ name: "a", state: "running" }, { name: "b", state: "succeeded" }, { name: "c", state: "never run" }, { name: "d", state: "failed" }];
  const cases = [
    ["five flows, all off", freeze(flowList([false, false, false, false, false])), read([])],
    ["three flows, two on", freeze(flowList([true, true, false])), read([])],
    ["no flows", freeze([]), read([])],
    ["workflows, one running", freeze(wf), read([])],
    ["nothing read yet", [], undefined],
    ["a failed first read", [], applyOutcome(null, { kind: "unreachable" }, 5000)],
    ["a failed read after a good one", freeze(flowList([true, true])), freeze(applyOutcome(applyOutcome(null, { kind: "ok", json: flowList([true, true]) }, 10_000), { kind: "unreachable" }, 15_000))],
    ["rows that are null", null, read([])],
  ];
  for (const [what, rows, state] of cases) {
    assert.equal(S.barButtonLabel("flows", rows, state), "Schedules", `flows, ${what}`);
    assert.equal(S.barButtonLabel("workflows", rows, state), "Workflows", `workflows, ${what}`);
  }
});

test("Button labels: no count, no 'all off', no 'on', no 'running' and no dot, in any label the function can return", () => {
  const rows = freeze(flowList([false, false, false, false, false]));
  for (const kind of ["flows", "workflows"]) {
    const label = S.barButtonLabel(kind, rows, read(rows));
    assert.doesNotMatch(label, /\d|all off|\bon\b|running|\u00b7/, label);
    assert.match(label, /^[A-Za-z]+$/, "one bare word");
  }
});

test("Button labels: a kind that is not a list is refused", () => {
  assert.throws(() => S.barButtonLabel("sessions", [], read([])), /no button label/);
  assert.throws(() => S.barButtonLabel(undefined));
});

// ---- 19.2: rsideStep, confirmScope, opOf ----

const CLOSED = { open: false, list: "flows", item: null, form: null };
const step = (state, ...events) => events.reduce((s, e) => S.rsideStep(freeze(s), freeze(e)), state);
const click = (list) => ({ type: "button", list });

test("Open, switch and close: Flows, then Workflows, then Workflows again", () => {
  const a = step(CLOSED, click("flows"));
  assert.deepEqual(a, { open: true, list: "flows", item: null, form: null });
  const b = step(a, click("workflows"));
  assert.deepEqual(b, { open: true, list: "workflows", item: null, form: null });
  const c = step(b, click("workflows"));
  assert.equal(c.open, false);
  assert.equal(c.item, null);
  // the state before anything was stored (nothing, or a stored value of the wrong shape) is a closed sidebar
  for (const raw of [undefined, null, "open", 5, []]) assert.deepEqual(S.rsideStep(raw, click("flows")), a, JSON.stringify(raw));
  // switching or closing with an item shown drops the item
  const shown = { open: true, list: "flows", item: "daily", form: null };
  assert.deepEqual(step(shown, click("workflows")), { open: true, list: "workflows", item: null, form: null });
  assert.equal(step(shown, click("flows")).open, false);
  assert.equal(step(shown, click("flows")).item, null);
  // a button for a list the sidebar does not have changes nothing
  assert.deepEqual(step(shown, click("sessions")), shown);
});

test("Stored sidebar state of the wrong shape is read as a normal record: open only for true, a known list, a name for the item, an object for the form", () => {
  // `caowin.rside` is stored and read by three mods, so every field is checked when it is read
  const wrong = freeze({ open: "yes", list: "constructor", item: ["a"], form: 7 });
  assert.deepEqual(S.rsideStep(wrong, { type: "nonsense" }), CLOSED);
  assert.deepEqual(S.rsideStep(wrong, { type: "row", item: "ok" }), CLOSED, "closed, so a row is ignored");
  assert.deepEqual(S.rsideStep(freeze({ open: true, list: "__proto__", item: "x", form: [] }), { type: "nonsense" }), { open: true, list: "flows", item: "x", form: null });
  assert.deepEqual(S.rsideStep(freeze({ open: true, list: "workflows", item: "", form: { kind: "new-flow" } }), { type: "back" }), { open: true, list: "workflows", item: null, form: { kind: "new-flow" } });
  for (const open of [1, "true", {}, [], null, undefined]) assert.equal(S.rsideStep({ open, list: "flows", item: "a" }, { type: "nonsense" }).open, false, `open ${JSON.stringify(open)}`);
  for (const item of [5, {}, null, ""]) assert.equal(S.rsideStep({ open: true, list: "flows", item }, { type: "nonsense" }).item, null, `item ${JSON.stringify(item)}`);
});

test("A closed record has no confirm scope: a stored record with an item but not open gives none, so a confirm is dropped", () => {
  assert.equal(S.confirmScope(freeze({ open: false, list: "flows", item: "a", form: null })), null);
  assert.equal(S.confirmScope(freeze({ open: "yes", list: "flows", item: "a" })), null, "open is true or it is not");
  assert.equal(S.confirmScope(freeze({ open: true, list: "flows", item: "a" })), S.scopeOf("flows", "a"), "fixture: open gives the scope");
});

test("List, then details, then back: a row opens its details, the back arrow returns to the list", () => {
  const list = step(CLOSED, click("flows"));
  const details = step(list, { type: "row", item: "daily-report" });
  assert.deepEqual(details, { open: true, list: "flows", item: "daily-report", form: null });
  const back = step(details, { type: "back" });
  assert.deepEqual(back, list);
  // the other list works the same, and a name that is also an object key is only a value
  for (const name of ["__proto__", "constructor", "toString"]) {
    const d = step(step(CLOSED, click("workflows")), { type: "row", item: name });
    assert.equal(d.item, name);
    assert.equal(d.list, "workflows");
    assert.equal(({}).item, undefined);
  }
  // a row event with no usable item, or on a closed sidebar, changes nothing
  assert.deepEqual(step(list, { type: "row", item: "" }), list);
  assert.deepEqual(step(list, { type: "row", item: 7 }), list);
  assert.deepEqual(step(CLOSED, { type: "row", item: "daily" }), CLOSED);
});

test("Reopening shows the list: closing while the details are shown drops the item, so the next Flows click shows the list", () => {
  const details = { open: true, list: "flows", item: "daily-report", form: null };
  const closed = step(details, { type: "close" });
  assert.deepEqual(closed, { open: false, list: "flows", item: null, form: null });
  const again = step(closed, click("flows"));
  assert.deepEqual(again, { open: true, list: "flows", item: null, form: null });
});

test("Started by flow opens the sidebar at that flow: a link opens the list at the item, from closed or from the other list", () => {
  const link = { type: "link", list: "flows", item: "daily-report" };
  assert.deepEqual(step(CLOSED, link), { open: true, list: "flows", item: "daily-report", form: null });
  const fromWorkflow = { open: true, list: "workflows", item: "wf", form: null };
  assert.deepEqual(step(fromWorkflow, link), { open: true, list: "flows", item: "daily-report", form: null });
  assert.deepEqual(step(CLOSED, { type: "link", list: "workflows", item: "wf" }), { open: true, list: "workflows", item: "wf", form: null });
  assert.deepEqual(step(CLOSED, { type: "link", list: "flows", item: "" }), CLOSED);
  assert.deepEqual(step(CLOSED, { type: "link", list: "sessions", item: "x" }), CLOSED);
});

test("Selection is independent of the session: a session selection event does not change the state", () => {
  for (const state of [CLOSED, { open: true, list: "workflows", item: "wf", form: null }]) {
    for (const event of [{ type: "selection", name: "cao-b" }, { type: "session", name: "cao-b" }, { type: "nonsense" }, {}, null, undefined]) {
      assert.deepEqual(S.rsideStep(freeze(state), event), state, JSON.stringify(event));
    }
  }
});

test("Flows are global: the state holds no session, so selecting a session before or after a flow changes nothing about the flows list", () => {
  const open = step(CLOSED, click("flows"));
  const afterA = step(open, { type: "selection", name: "cao-a" });
  const afterB = step(afterA, { type: "selection", name: "cao-b" });
  assert.deepEqual(afterB, open);
  const picked = step(afterB, { type: "row", item: "daily-report" });
  assert.deepEqual(Object.keys(picked).sort(), ["form", "item", "list", "open"]);
  assert.deepEqual(step(picked, { type: "selection", name: "cao-c" }), picked);
  // a row click names no session either
  assert.ok(!JSON.stringify(picked).includes("cao-"));
});

test("Saved flow shows in the sidebar: a saved event opens the list at that item, from closed, and drops a held form", () => {
  const saved = { type: "saved", list: "flows", item: "new-flow" };
  assert.deepEqual(step(CLOSED, saved), { open: true, list: "flows", item: "new-flow", form: null });
  assert.deepEqual(step({ open: true, list: "workflows", item: "wf", form: null }, saved), { open: true, list: "flows", item: "new-flow", form: null });
  assert.deepEqual(step({ open: false, list: "flows", item: null, form: { kind: "new-flow" } }, saved), { open: true, list: "flows", item: "new-flow", form: null });
  assert.deepEqual(step(CLOSED, { type: "saved", list: "workflows", item: "wf" }), { open: true, list: "workflows", item: "wf", form: null });
  assert.deepEqual(step(CLOSED, { type: "saved", list: "flows" }), CLOSED);
});

test("Form slot opens and closes: form holds the dialog request, form-close empties the slot, nothing else moves", () => {
  const request = { kind: "new-flow", name: "dlg-0a1b2c3d" };
  const held = step({ open: true, list: "workflows", item: "wf", form: null }, { type: "form", request });
  assert.deepEqual(held, { open: true, list: "workflows", item: "wf", form: request });
  assert.deepEqual(step(held, { type: "form-close" }), { open: true, list: "workflows", item: "wf", form: null });
  assert.equal(step(CLOSED, { type: "form-close" }).form, null);
  assert.equal(step(CLOSED, { type: "form", request: "not an object" }).form, null);
});

test("Confirm dropped when the item changes: a confirm is stale when its scope differs from the current item", () => {
  const at = (list, item) => ({ open: true, list, item, form: null });
  const scope = S.confirmScope(at("flows", "a"));
  assert.equal(typeof scope, "string");
  assert.equal(S.confirmScope(at("flows", "a")), scope, "the same item has the same scope");
  assert.notEqual(S.confirmScope(at("flows", "b")), scope, "another item");
  assert.notEqual(S.confirmScope(at("workflows", "a")), scope, "a workflow of the same name is another item");
  assert.notEqual(S.confirmScope(step(at("flows", "a"), { type: "back" })), scope, "the back arrow");
  assert.notEqual(S.confirmScope(step(at("flows", "a"), { type: "close" })), scope, "closing");
  assert.notEqual(S.confirmScope(step(at("flows", "a"), click("workflows"))), scope, "the other list");
  assert.equal(S.confirmScope(CLOSED), null);
  assert.equal(S.confirmScope(at("flows", null)), null);
  assert.equal(S.confirmScope(undefined), null);
  assert.notEqual(S.confirmScope(at("flows", "a")), S.confirmScope(at("flows", "a:")), "no two items share a scope");
});

test("Answer after leaving the details: the answer recorded for the item that asked is returned for that item only", () => {
  const A = S.confirmScope({ open: true, list: "flows", item: "a", form: null });
  const B = S.confirmScope({ open: true, list: "flows", item: "b", form: null });
  const asked = freeze([{ item: A, state: "running" }]);
  const answered = freeze([{ item: A, state: "running" }, { item: A, state: "done", message: "executed" }]);
  assert.deepEqual(S.opOf(asked, A), { item: A, state: "running" });
  assert.equal(S.opOf(asked, B), null, "nothing is shown on b");
  assert.deepEqual(S.opOf(answered, A), { item: A, state: "done", message: "executed" }, "the latest answer for a");
  assert.equal(S.opOf(answered, B), null);
  const proto = freeze([{ item: S.confirmScope({ open: true, list: "flows", item: "__proto__", form: null }), state: "failed", message: "no" }]);
  assert.equal(S.opOf(proto, S.confirmScope({ open: true, list: "flows", item: "__proto__", form: null })).state, "failed");
  assert.equal(S.opOf(proto, "flows:constructor"), null);
});

test("An answer is found by its exact scope: an item that only shares a prefix, a missing item and malformed entries find nothing wrong", () => {
  const long = S.scopeOf("flows", "daily-report");
  const short = S.scopeOf("flows", "daily");
  const ops = freeze([{ item: long, state: "running" }]);
  assert.deepEqual(S.opOf(ops, long), { item: long, state: "running" });
  assert.equal(S.opOf(ops, short), null, "daily is not daily-report");
  assert.equal(S.opOf(freeze([{ item: short, state: "done" }]), long), null, "nor the other way round");
  // no item asked for finds no entry, even one that has no item either
  for (const none of [undefined, null, "", 5]) assert.equal(S.opOf(freeze([{ state: "done" }, { item: "", state: "done" }]), none), null, String(none));
  // entries that are not records are skipped, not read
  assert.deepEqual(S.opOf(freeze([null, 7, "flows:daily-report", [long], { item: long, state: "done" }]), long), { item: long, state: "done" });
});

test("Scope of a list row: scopeOf gives what confirmScope gives for the same item, and none for an unknown list or no item", () => {
  for (const list of ["flows", "workflows"]) {
    for (const item of ["a", "daily-report", "__proto__", "constructor", "x:y"]) {
      assert.equal(S.scopeOf(list, item), S.confirmScope({ open: true, list, item, form: null }), `${list}:${item}`);
    }
  }
  assert.notEqual(S.scopeOf("flows", "a"), S.scopeOf("workflows", "a"));
  for (const [list, item] of [["sessions", "a"], ["constructor", "a"], [undefined, "a"], ["flows", ""], ["flows", null], ["flows", undefined], ["flows", 5], ["flows", ["a"]]]) {
    assert.equal(S.scopeOf(list, item), null, `${String(list)} ${JSON.stringify(item)}`);
  }
});

test("Row shows running meanwhile: the asking item's row reads running while its request is out, and no other row does", () => {
  const A = S.scopeOf("flows", "a"); // the list row builds the scope of its item, as the details do
  const ops = freeze([{ item: A, state: "running" }]);
  assert.equal(S.opOf(ops, A)?.state, "running");
  for (const other of ["flows:b", "workflows:a", null, undefined]) assert.equal(S.opOf(ops, other), null, String(other));
  for (const bad of [undefined, null, "x", 5, {}]) assert.equal(S.opOf(bad, A), null, JSON.stringify(bad));
});

// ---- 19.3: rsideLayoutStep ----

const OPEN = { open: true, list: "flows", item: null, form: null };
// One call per tick: the block is `shown` (a function of the tick number, from 1) and the window's record is
// what the last call returned. -> every call's result, in order.
function ticks(rside, shownAt, n, state) {
  const out = [];
  let r = rside;
  let st = state;
  for (let i = 1; i <= n; i += 1) {
    const res = S.rsideLayoutStep(freeze(r), shownAt(i), freeze(st));
    out.push(res);
    r = res.rside;
    st = res.state;
  }
  return out;
}
const sends = (results) => results.map((r) => r.send);

test("Hidden at first start: the first call with the block shown returns open false and sends hide", () => {
  assert.equal(S.RSIDE_RESEND_TICKS, 4);
  const first = S.rsideLayoutStep(CLOSED, true, undefined);
  assert.equal(first.rside.open, false);
  assert.equal(first.send, "hide");
  // the block was already hidden: nothing to send, and the record stays closed
  const quiet = S.rsideLayoutStep(CLOSED, false, undefined);
  assert.equal(quiet.send, null);
  assert.equal(quiet.rside.open, false);
  // once it is hidden, nothing more is sent, however many ticks follow
  const after = ticks(first.rside, () => false, 12, first.state);
  assert.deepEqual(sends(after), Array(12).fill(null));
  assert.ok(after.every((r) => r.rside.open === false));
  // a layout that could not be read yet (shown is not a boolean) is not a first call: the next real read is
  const unread = S.rsideLayoutStep(OPEN, undefined, undefined);
  assert.equal(unread.send, null);
  assert.deepEqual(unread.rside, OPEN);
  assert.equal(S.rsideLayoutStep(unread.rside, true, unread.state).send, "hide");
});

test("A layout that could not be read is not a first call: a shown value that is not a boolean changes nothing and the next real read is the first", () => {
  for (const shown of [undefined, null, "yes", "", 1, 0, {}, []]) {
    const unread = S.rsideLayoutStep(OPEN, shown, undefined);
    assert.equal(unread.send, null, JSON.stringify(shown));
    assert.deepEqual(unread.rside, OPEN, JSON.stringify(shown));
    assert.equal(S.rsideLayoutStep(unread.rside, true, unread.state).send, "hide", `${JSON.stringify(shown)}: still the first call`);
  }
});

test("Restart with the sidebar left open: the stored record says open and the block is shown, and the window hides it", () => {
  const stored = { open: true, list: "workflows", item: "wf", form: null };
  const first = S.rsideLayoutStep(freeze(stored), true, undefined);
  assert.equal(first.send, "hide");
  assert.equal(first.rside.open, false, "neither button is active");
  assert.equal(first.rside.item, null);
  const after = ticks(first.rside, (i) => i < 2, 10, first.state);
  assert.deepEqual(sends(after), Array(10).fill(null), "the block went hidden on the next tick, and nothing more is sent");
  assert.ok(after.every((r) => r.rside.open === false));
  // the same restart with the block already hidden: the record is closed without a command
  const hiddenAlready = S.rsideLayoutStep(freeze(stored), false, undefined);
  assert.equal(hiddenAlready.send, null);
  assert.equal(hiddenAlready.rside.open, false);
});

test("Hidden by a Herdr command: show is sent, sent once more after RSIDE_RESEND_TICKS ticks, then the block's state is adopted", () => {
  const N = S.RSIDE_RESEND_TICKS;
  // the window is running, its record says open and the block is shown; then a Herdr command hides the block
  const agreed = ticks(OPEN, () => true, 2, S.rsideLayoutStep(CLOSED, false, undefined).state);
  assert.deepEqual(sends(agreed), [null, null]);
  const run = ticks(OPEN, () => false, 3 * N, agreed[1].state);
  const sent = sends(run);
  assert.deepEqual(sent.filter((x) => x !== null), ["show", "show"], "two sends in all");
  assert.equal(sent[0], "show", "the first at once");
  assert.equal(sent[N], "show", "the second RSIDE_RESEND_TICKS ticks after the first");
  assert.equal(sent.slice(1, N).every((x) => x === null), true);
  const adoptedAt = run.findIndex((r) => r.rside.open === false);
  assert.equal(adoptedAt, 2 * N, "adopted RSIDE_RESEND_TICKS ticks after the second send");
  assert.ok(run.slice(adoptedAt).every((r) => r.send === null && r.rside.open === false), "nothing more is sent");
});

test("Command dropped, sent again once: a dropped show is sent again after RSIDE_RESEND_TICKS ticks and the block then shows", () => {
  const N = S.RSIDE_RESEND_TICKS;
  const running = S.rsideLayoutStep(CLOSED, false, undefined).state;
  const run = ticks(OPEN, (i) => i > N + 1, 4 * N, running);
  const sent = sends(run);
  assert.deepEqual(sent.filter((x) => x !== null), ["show", "show"]);
  assert.equal(sent[0], "show");
  assert.equal(sent[N], "show");
  assert.ok(run.every((r) => r.rside.open === true), "the record stays open: the block did show");
  assert.equal(run.slice(N + 1).every((r) => r.send === null), true);
  // the same record and block agree: no send at all, and a long agreement forgets the earlier sends
  const agree = ticks(OPEN, () => true, 3, run[run.length - 1].state);
  assert.deepEqual(sends(agree), [null, null, null]);
  const later = S.rsideLayoutStep(CLOSED, true, agree[2].state);
  assert.equal(later.send, "hide", "a new wish is sent at once, as the first send");
});

test("Button then show: a click that opens with the block hidden sends show at once, and a click that closes sends hide", () => {
  const running = S.rsideLayoutStep(CLOSED, false, undefined).state;
  const opened = S.rsideStep(CLOSED, { type: "button", list: "workflows" });
  const r1 = S.rsideLayoutStep(freeze(opened), false, running);
  assert.equal(r1.send, "show");
  assert.deepEqual(r1.rside, opened);
  const r2 = S.rsideLayoutStep(r1.rside, true, r1.state);
  assert.equal(r2.send, null);
  const closed = S.rsideStep(r2.rside, { type: "button", list: "workflows" });
  assert.equal(S.rsideLayoutStep(freeze(closed), true, r2.state).send, "hide");
  // the wish changed while a show was still out: the other command is sent at once, not after the wait
  const flipped = S.rsideLayoutStep(freeze(closed), true, r1.state);
  assert.equal(flipped.send, "hide");
  // the link written by the Agents block opens the sidebar in the same way
  const linked = S.rsideStep(CLOSED, { type: "link", list: "flows", item: "daily" });
  assert.equal(S.rsideLayoutStep(freeze(linked), false, running).send, "show");
});

test("Block shown by a Herdr command while the record says closed: hide is sent, sent once more, then the block's state is adopted as open", () => {
  // spec "Start state": when they still differ it takes the block's real state as the truth, in both directions
  const N = S.RSIDE_RESEND_TICKS;
  const expectRun = (run, firstAt, label) => {
    const sent = sends(run);
    assert.deepEqual(sent.filter((x) => x !== null), ["hide", "hide"], `${label}: two sends in all`);
    assert.equal(sent[firstAt], "hide", `${label}: the first send`);
    assert.equal(sent[firstAt + N], "hide", `${label}: the second RSIDE_RESEND_TICKS ticks later`);
    const adoptedAt = run.findIndex((r) => r.rside.open === true);
    assert.equal(adoptedAt, firstAt + 2 * N, `${label}: adopted RSIDE_RESEND_TICKS ticks after the second send`);
    assert.ok(run.slice(adoptedAt).every((r) => r.send === null && r.rside.open === true), `${label}: open and quiet from then on`);
  };
  // the window is running with the record closed, and a Herdr command shows the block
  const running = S.rsideLayoutStep(CLOSED, false, undefined).state;
  expectRun(ticks(CLOSED, () => true, 4 * N + 1, running), 0, "after running");
  // the first start: the hide it sends is the first send, and a block that stays shown is adopted the same way
  const first = S.rsideLayoutStep(CLOSED, true, undefined);
  assert.equal(first.send, "hide");
  expectRun([first, ...ticks(first.rside, () => true, 4 * N, first.state)], 0, "at first start");
});

test("Adopting a hidden block closes the record and drops the item: the adopted record is the closed record, list kept", () => {
  const N = S.RSIDE_RESEND_TICKS;
  const shownItem = { open: true, list: "workflows", item: "wf", form: null };
  const agreed = ticks(shownItem, () => true, 1, S.rsideLayoutStep(CLOSED, false, undefined).state);
  const run = ticks(shownItem, () => false, 3 * N, agreed[0].state);
  const adopted = run.find((r) => r.rside.open === false);
  assert.deepEqual(adopted.rside, { open: false, list: "workflows", item: null, form: null });
});

test("A second disagreement after an agreement is sent afresh: a repeat of the same wish gets its own show and its own resend", () => {
  const running = S.rsideLayoutStep(CLOSED, false, undefined).state;
  // the record says open; the block agrees for 2 ticks, differs for 6, agrees for 2, differs for 6
  const shownAt = (i) => i <= 2 || i === 9 || i === 10;
  const run = ticks(OPEN, shownAt, 16, running);
  const expected = Array(16).fill(null);
  for (const i of [2, 6, 10, 14]) expected[i] = "show";
  assert.deepEqual(sends(run), expected);
  assert.ok(run.every((r) => r.rside.open === true), "neither run lasts long enough to be adopted");
});

// ---- 19.4: dialogStep, dialogSpec ----

const REQ = { kind: "new-flow", name: "dlg-0a1b2c3d" };
const BUSY = "A request is in progress. Wait for the answer.";
const types = (effects) => effects.map((e) => e.type);
// Runs events in order from a state; -> {state, effects: every effect so far, last: the last step's effects}.
function drive(state, ...events) {
  let st = state;
  const all = [];
  let last = [];
  for (const e of events) {
    const r = S.dialogStep(freeze(st), freeze(e));
    st = r.state;
    last = r.effects;
    all.push(...r.effects);
  }
  return { state: st, effects: all, last };
}
const opened = (...more) => drive(S.initialDialog(), { type: "open", request: REQ }, ...more);

test("Escape with edits asks first: Esc with edits gives the discard confirm, and Keep editing returns to the form unchanged", () => {
  const edited = opened({ type: "dirty", dirty: true });
  const esc = drive(edited.state, { type: "close" });
  assert.deepEqual(types(esc.last), ["confirm-discard"]);
  assert.equal(esc.last[0].title, "Discard this form?");
  assert.equal(esc.last[0].defaultAction, "Keep editing");
  assert.equal(esc.state.open, true, "the dialog is still open behind the confirm");
  assert.equal(esc.state.dirty, true);
  // Keep editing (or Esc again) returns to the form with the edits; the dialog does not close
  const keep = drive(esc.state, { type: "close" });
  assert.deepEqual(keep.last, []);
  assert.equal(keep.state.open, true);
  assert.equal(keep.state.dirty, true);
  assert.deepEqual(keep.state, edited.state);
  // Discard in the confirm closes
  assert.deepEqual(types(drive(esc.state, { type: "close", discard: true }).last), ["close"]);
  // a form that was edited and then restored is untouched again
  assert.deepEqual(types(drive(edited.state, { type: "dirty", dirty: false }, { type: "close" }).last), ["close"]);
});

test("Escape with an untouched form: the dialog closes without a confirmation", () => {
  const r = drive(opened().state, { type: "close" });
  assert.deepEqual(types(r.last), ["close"]);
  assert.equal(r.state.open, false);
  assert.ok(!types(r.effects).includes("confirm-discard"));
});

test("Escape in a confirmation: Esc returns to the form body and the dialog stays open, edited or not", () => {
  for (const dirty of [false, true]) {
    const base = opened({ type: "dirty", dirty });
    const r = drive(base.state, { type: "close", confirming: "ask-first" });
    assert.deepEqual(r.last, [], `dirty ${dirty}`);
    assert.equal(r.state.open, true);
    assert.deepEqual(r.state, base.state);
  }
  // inside the discard confirm too: it is the confirm's own default action (Keep editing)
  const asked = opened({ type: "dirty", dirty: true }, { type: "close" });
  assert.equal(asked.state.open, true);
  const back = drive(asked.state, { type: "close" });
  assert.equal(back.state.open, true);
  assert.deepEqual(back.last, []);
  // and Esc on the form after that asks again
  assert.deepEqual(types(drive(back.state, { type: "close" }).last), ["confirm-discard"]);
});

test("A second open does not replace the dialog: the request and the typed text are unchanged", () => {
  const base = opened({ type: "dirty", dirty: true });
  const second = drive(base.state, { type: "open", request: { kind: "new-session", name: "dlg-ffffffff" } });
  assert.deepEqual(second.state, base.state);
  assert.deepEqual(second.last, []);
  assert.deepEqual(second.state.request, REQ);
  assert.equal(second.state.dirty, true);
  // a request that is not an object does not open a dialog
  assert.equal(drive(S.initialDialog(), { type: "open", request: "x" }).state.open, false);
  assert.equal(drive(S.initialDialog(), { type: "open" }).state.open, false);
});

test("Close during a launch: Esc or Close is refused with 'A request is in progress. Wait for the answer.'", () => {
  const launching = opened({ type: "write-start" });
  for (const dirty of [false, true]) {
    const r = drive(launching.state, { type: "dirty", dirty }, { type: "close" });
    assert.deepEqual(r.last, [{ type: "refuse", message: BUSY }], `dirty ${dirty}`);
    assert.equal(r.state.open, true);
    assert.equal(r.state.writing, true);
  }
  // a discard cannot slip past the write either
  assert.deepEqual(types(drive(launching.state, { type: "close", discard: true }).last), ["refuse"]);
  // once the answer arrives, closing works again
  assert.deepEqual(types(drive(launching.state, { type: "write-end" }, { type: "close" }).last), ["close"]);
});

test("Close while the form reads: a validate check or profile read does not block closing an untouched form", () => {
  // a read is not a write: the dialog is never told about it, and an event for it changes nothing
  const reading = opened({ type: "read-start" });
  assert.equal(reading.state.writing, false);
  assert.deepEqual(types(drive(reading.state, { type: "close" }).last), ["close"]);
  assert.deepEqual(types(drive(reading.state, { type: "read-end" }, { type: "close" }).last), ["close"]);
});

test("Close after a lost answer: a timeout event lets Close through, with a note that the answer may have been lost", () => {
  const lost = opened({ type: "write-start" }, { type: "timeout" });
  assert.equal(lost.state.open, true);
  const r = drive(lost.state, { type: "close" });
  assert.equal(r.last.length, 1);
  assert.equal(r.last[0].type, "close");
  assert.match(r.last[0].note, /answer may have been lost/);
  assert.equal(r.state.open, false);
  // before the timeout it is still refused, and a timeout with no write in flight does nothing
  assert.deepEqual(types(drive(opened({ type: "write-start" }).state, { type: "close" }).last), ["refuse"]);
  const none = drive(opened().state, { type: "timeout" });
  assert.deepEqual(none.state, opened().state);
  assert.deepEqual(types(drive(none.state, { type: "close" }).last), ["close"]);
  // a late answer after the timeout ends the write
  const late = drive(lost.state, { type: "write-end" });
  assert.equal(late.state.writing, false);
  assert.equal(late.state.lost, false);
});

test("A new write after a lost answer is refused again: write-start clears the lost note, so Close waits for the new write", () => {
  const second = opened({ type: "write-start" }, { type: "timeout" }, { type: "write-start" });
  assert.equal(second.state.writing, true);
  assert.equal(second.state.lost, false, "the first write's timeout does not carry over");
  const r = drive(second.state, { type: "close" });
  assert.deepEqual(r.last, [{ type: "refuse", message: BUSY }]);
  assert.equal(r.state.open, true);
  // and its own timeout lets it through again
  assert.deepEqual(types(drive(second.state, { type: "timeout" }, { type: "close" }).last), ["close"]);
});

test("Events on a closed dialog change nothing: only an open does anything, and no effect comes out", () => {
  const events = [
    { type: "dirty", dirty: true }, { type: "write-start" }, { type: "write-end" }, { type: "timeout" },
    { type: "saved", list: "flows", item: "x" }, { type: "close" }, { type: "close", discard: true }, { type: "close", confirming: "ask-first" },
  ];
  for (const event of events) {
    const r = S.dialogStep(freeze(S.initialDialog()), freeze(event));
    assert.deepEqual(r, { state: S.initialDialog(), effects: [] }, JSON.stringify(event));
  }
  // the same events after a close, from the state the close left
  const closed = opened({ type: "close" });
  assert.equal(closed.state.open, false);
  for (const event of events) assert.deepEqual(S.dialogStep(freeze(closed.state), freeze(event)), { state: S.initialDialog(), effects: [] }, `after a close: ${JSON.stringify(event)}`);
});

test("Selection changes under an open dialog: a selection or sidebar event leaves the state equal", () => {
  const base = opened({ type: "dirty", dirty: true }, { type: "write-start" });
  for (const event of [{ type: "selection", name: "cao-b" }, { type: "rside", open: true }, { type: "button", list: "flows" }, { type: "row", item: "a" }, { type: "unknown" }, null, undefined]) {
    const r = S.dialogStep(freeze(base.state), event);
    assert.deepEqual(r.state, base.state, JSON.stringify(event));
    assert.deepEqual(r.effects, [], JSON.stringify(event));
  }
  assert.deepEqual(base.state.request, REQ, "the snapshot is the request it was opened with");
});

test("Dialog save returns the sidebar to set: a flow save and a workflow Run answered 202 set the sidebar and close; a launch does not", () => {
  const saved = drive(opened({ type: "write-start" }).state, { type: "saved", list: "flows", item: "new-flow" });
  assert.deepEqual(saved.last, [
    { type: "set-rside", rside: { open: true, list: "flows", item: "new-flow", form: null } },
    { type: "close" },
  ]);
  assert.equal(saved.state.open, false);
  const run = drive(opened({ type: "write-start" }).state, { type: "saved", list: "workflows", item: "wf" });
  assert.deepEqual(run.last[0], { type: "set-rside", rside: { open: true, list: "workflows", item: "wf", form: null } });
  assert.equal(run.last[1].type, "close");
  // the rside it returns is what rsideStep gives for the same event
  assert.deepEqual(saved.last[0].rside, S.rsideStep(undefined, { type: "saved", list: "flows", item: "new-flow" }));
  // a launch shows its result card in the dialog: nothing to set, nothing to close, and its write is over
  const launch = drive(opened({ type: "write-start" }).state, { type: "saved" });
  assert.deepEqual(launch.last, []);
  assert.equal(launch.state.open, true);
  assert.equal(launch.state.writing, false);
});

test("Help is small and forms are large: 0.85 by 0.85 for a form, 360 by 220 px for bar-help, kind from DIALOG_KIND", () => {
  assert.ok(["dialog", "window"].includes(S.DIALOG_KIND));
  for (const kind of ["new-session", "new-flow", "edit-flow", "new-workflow", "edit-workflow", "run-workflow"]) {
    const spec = S.dialogSpec({ kind, name: "dlg-00000001" }, {});
    assert.deepEqual([spec.kind, spec.modal, spec.width, spec.height], [S.DIALOG_KIND, true, 0.85, 0.85], kind);
  }
  const help = S.dialogSpec({ kind: "bar-help", name: "dlg-00000002" }, {});
  assert.deepEqual([help.kind, help.modal, help.width, help.height], [S.DIALOG_KIND, true, 360, 220]);
});

test("Dialog request carries the layout props: every prop but section and host, then host dialog, kind and name; the request wins a clash", () => {
  const layout = freeze({ machine: "cao-box", caoPort: 9889, talkableProfiles: ["conductor"], hiddenRootProfiles: ["x"], draftProfile: "author", draftProviders: ["claude_code"], section: "sessions", host: "sidebar" });
  const request = freeze({ kind: "new-flow", name: "dlg-0a1b2c3d" });
  const spec = S.dialogSpec(request, layout);
  assert.deepEqual(spec.props, {
    machine: "cao-box", caoPort: 9889, talkableProfiles: ["conductor"], hiddenRootProfiles: ["x"], draftProfile: "author", draftProviders: ["claude_code"],
    host: "dialog", kind: "new-flow", name: "dlg-0a1b2c3d",
  });
  assert.ok(!("section" in spec.props));
  const clash = S.dialogSpec(freeze({ kind: "edit-flow", name: "dlg-1", flow: "daily", machine: "other" }), layout);
  assert.equal(clash.props.machine, "other");
  assert.equal(clash.props.flow, "daily");
  assert.equal(clash.props.caoPort, 9889);
  assert.equal(clash.props.host, "dialog");
  assert.deepEqual(S.dialogSpec(request, undefined).props, { host: "dialog", kind: "new-flow", name: "dlg-0a1b2c3d" });
});

test("A request cannot choose the host: a request that names host sidebar still opens the overlay as the dialog", () => {
  // the one key the request does not win: the overlay copy picks its surface from props().host (design D29)
  const spec = S.dialogSpec(freeze({ kind: "new-flow", name: "dlg-1", host: "sidebar" }), freeze({ machine: "m", host: "sidebar", section: "sessions" }));
  assert.equal(spec.props.host, "dialog");
  assert.deepEqual(spec.props, { machine: "m", host: "dialog", kind: "new-flow", name: "dlg-1" });
});

// ---- 19.5: the dialog lease and the author records ----

const SEC = 1000;
const NAME = "cao-draft-flow-0a1b2c3d";
const author = (over = {}) => ({ name: NAME, owner: "dlg-a", at: 100 * SEC, tries: 0, id: "t1", ...over });
const lease = (name, until) => ({ name, until });

test("Closed by Herdr: a record past its deadline does not refuse an open", () => {
  assert.deepEqual(S.leaseFor("dlg-a", 5000), { name: "dlg-a", until: 15_000 });
  const rec = freeze(S.leaseFor("dlg-a", 0));
  assert.equal(S.dialogLive(rec, 5000), true);
  assert.equal(S.dialogLive(rec, 10_000), false, "the deadline itself is past");
  assert.equal(S.dialogLive(rec, 60_000), false);
  assert.equal(S.dialogLive(rec, 60_000, "dlg-a"), false);
  // no record, and a record of the wrong shape, are nobody's lease
  for (const bad of [undefined, null, "dlg-a", 5, [], {}, { name: "dlg-a" }, { name: "dlg-a", until: "99999" }, { name: "dlg-a", until: NaN }]) {
    assert.equal(S.dialogLive(bad, 1000), false, JSON.stringify(bad));
  }
  assert.equal(S.dialogLive(rec, NaN), false);
});

test("Stored lease of the wrong shape is not live: an until that is not a finite number never refuses an open, whatever it coerces to", () => {
  const now = 1000;
  // a string, an array, an object and exponent text would all read as live (now < until <= now + 10 s) if the
  // stored value were coerced to a number; the rest are numbers the window never writes
  const wrong = [
    ["a string", "5000"], ["an array", [5000]], ["an object with valueOf", { valueOf() { return 5000; } }], ["exponent text", "1e4"],
    ["NaN", NaN], ["negative", -5000], ["huge", 1e308], ["Infinity", Infinity], ["null", null], ["true", true],
  ];
  for (const [what, until] of wrong) {
    assert.equal(S.dialogLive({ name: "dlg-a", until }, now), false, what);
    assert.equal(S.dialogLive({ name: "dlg-a", until }, now, "dlg-a"), false, `${what}, asked by name`);
    // such a record is nobody's owner, so it never keeps an author session from being deleted
    assert.deepEqual(S.authorDeletes(freeze([author()]), { name: "dlg-a", until }, now, []), [NAME], `${what}: author`);
  }
  // a clock that is not a number refuses nothing, even against a good record
  const good = { name: "dlg-a", until: 5000 };
  assert.equal(S.dialogLive(good, now), true, "fixture: a good record is live");
  for (const clock of ["1000", [1000], null, undefined, NaN, Infinity]) assert.equal(S.dialogLive(good, clock), false, `clock ${String(clock)}`);
});

test("Record from a later clock: an `until` more than 10 s ahead is not live and is nobody's owner", () => {
  const now = 50 * SEC;
  assert.equal(S.dialogLive(lease("dlg-a", now + 10 * SEC), now), true, "exactly 10 s ahead is live");
  assert.equal(S.dialogLive(lease("dlg-a", now + 10 * SEC + 1), now), false, "1 ms more is not");
  assert.equal(S.dialogLive(lease("dlg-a", now + 3600 * SEC), now), false);
  assert.equal(S.dialogLive(lease("dlg-a", now), now), false, "until == now is past");
  assert.equal(S.dialogLive(lease("dlg-a", now + 1), now), true);
  // nobody's owner: the author of that dialog is deletable
  const entries = freeze([author()]);
  assert.deepEqual(S.authorDeletes(entries, lease("dlg-a", now + 3600 * SEC), now, []), [NAME]);
  assert.deepEqual(S.authorDeletes(entries, lease("dlg-a", now + 5 * SEC), now, []), []);
});

test("Open refused only while live: renewed each second for 60 s it stays live, a stall of 8 s is still live, unrenewed it is dead after 10 s", () => {
  let rec = S.leaseFor("dlg-a", 0);
  for (let t = 1; t <= 60; t += 1) {
    assert.equal(S.dialogLive(rec, t * SEC, "dlg-a"), true, `second ${t}`);
    rec = S.leaseFor("dlg-a", t * SEC);
  }
  const written = S.leaseFor("dlg-a", 60 * SEC);
  assert.equal(S.dialogLive(written, 60 * SEC + 8 * SEC), true, "a stall of 8 s");
  assert.equal(S.dialogLive(written, 60 * SEC + 9_999), true);
  assert.equal(S.dialogLive(written, 60 * SEC + 10 * SEC), false, "dead after 10 s");
  // the name, when given, must be the record's
  assert.equal(S.dialogLive(written, 61 * SEC, "dlg-b"), false);
  assert.equal(S.dialogLive(written, 61 * SEC, "dlg-a"), true);
  assert.equal(S.dialogLive(written, 61 * SEC), true, "no name asked: any live record");
});

test("Dialog closed while an author runs: the owner's record cleared, the name is returned", () => {
  const entries = freeze([author()]);
  assert.deepEqual(S.authorDeletes(entries, null, 200 * SEC, []), [NAME]);
  assert.deepEqual(S.authorDeletes(entries, undefined, 200 * SEC, []), [NAME]);
  assert.deepEqual(S.authorDeletes([], null, 200 * SEC, []), []);
  assert.deepEqual(S.authorDeletes(undefined, null, 200 * SEC, undefined), []);
});

test("Dialog closed without running its close hook: the owner's deadline passed, or another dialog's record, returns the name", () => {
  const entries = freeze([author()]);
  const now = 200 * SEC;
  assert.deepEqual(S.authorDeletes(entries, lease("dlg-a", now - 1), now, []), [NAME], "deadline passed");
  assert.deepEqual(S.authorDeletes(entries, lease("dlg-b", now + 5 * SEC), now, []), [NAME], "another dialog's live record");
  // an entry with no owner has no live owner either
  assert.deepEqual(S.authorDeletes(freeze([author({ owner: undefined })]), lease("dlg-a", now + 5 * SEC), now, []), [NAME]);
});

test("Dialog open during drafting: a live owner leaves its entry alone", () => {
  const entries = freeze([author(), author({ name: "cao-draft-workflow-ffffffff", owner: "dlg-b", id: "t2" })]);
  const now = 200 * SEC;
  assert.deepEqual(S.authorDeletes(entries, lease("dlg-a", now + 5 * SEC), now, []), ["cao-draft-workflow-ffffffff"], "only the other dialog's entry");
  const none = freeze([author({ id: null })]);
  assert.deepEqual(S.authorDeletes(none, lease("dlg-a", now + 65 * SEC), now + 60 * SEC, []), [], "not even long after, while its owner lives");
});

test("Dialog closed while the author launch is out: with no id nothing before at + 25 s, the name after; with an id at once", () => {
  const at = 100 * SEC;
  const waiting = freeze([author({ id: null, at })]);
  assert.deepEqual(S.authorDeletes(waiting, null, at, []), []);
  assert.deepEqual(S.authorDeletes(waiting, null, at + 24_999, []), []);
  assert.deepEqual(S.authorDeletes(waiting, null, at + 25_000, []), [NAME]);
  assert.deepEqual(S.authorDeletes(waiting, null, at + 90 * SEC, []), [NAME]);
  assert.deepEqual(S.authorDeletes(freeze([author({ id: "t1", at })]), null, at + 1, []), [NAME], "with an id: at once");
  assert.deepEqual(S.authorDeletes(freeze([author({ id: "", at })]), null, at + 1, []), [], "an empty id is no id");
});

test("Name survives the terminal: the name is returned although no terminal of that name is listed", () => {
  assert.deepEqual(S.authorDeletes(freeze([author({ id: "gone-terminal" })]), null, 500 * SEC, []), [NAME]);
});

// What is not an author session of this window: any other session of the CAO machine, a path, an empty name,
// a name near the shape, and values that are no text. `caowin.authors` is read from disk, so nothing in it may
// choose which session gets a DELETE (the same rule draftCall applies to the effect that sends it).
const NOT_AUTHOR = [
  "cao-real", "../x", "a/b?x=1", "", " ", "cao-draft-flow-0a1b2c3", "cao-draft-flow-0A1B2C3D", "cao-draft-flow-0a1b2c3d9", "cao-draft-other-0a1b2c3d",
  "draft-flow-0a1b2c3d", "cao-draft-flow-0a1b2c3d\n", " cao-draft-flow-0a1b2c3d", "cao-draft-flow-0a1b2c3d/..", 5, null, undefined, ["cao-draft-flow-0a1b2c3d"], { toString: () => NAME },
];

test("Author names that are not the window's own are never deleted: only cao-draft-<kind>-<8 hex> is returned, as draftCall allows", () => {
  const built = [{ kind: "flow", hex: "0a1b2c3d" }, { kind: "workflow", hex: "ffffffff" }].map(({ kind, hex }) => draftRequest({ kind, description: "d", profile: "p", provider: "claude_code", workdir: "/w", idempotencyKey: "k", hex }).sessionName);
  assert.deepEqual(built, [NAME, "cao-draft-workflow-ffffffff"], "fixture: the names the window builds");
  for (const name of built) {
    assert.deepEqual(S.authorDeletes(freeze([author({ name })]), null, 500 * SEC, []), [name], name);
    assert.doesNotThrow(() => draftCall({ type: "delete", sessionName: name }), name);
  }
  for (const name of NOT_AUTHOR) {
    assert.deepEqual(S.authorDeletes(freeze([author({ name })]), null, 500 * SEC, []), [], JSON.stringify(name));
    assert.throws(() => draftCall({ type: "delete", sessionName: name }), JSON.stringify(name));
  }
  // the others in the list are still returned
  assert.deepEqual(S.authorDeletes(freeze([author({ name: "cao-real" }), author()]), null, 500 * SEC, []), [NAME]);
});

test("Author names that are not the window's own are never recorded: recordAuthor leaves the list as it was", () => {
  const before = freeze([author()]);
  for (const name of NOT_AUTHOR) {
    assert.deepEqual(S.recordAuthor(before, name, "dlg-a", 1), before, JSON.stringify(name));
    assert.deepEqual(S.recordAuthor(before, name, "dlg-a", 1, "t9"), before, `${JSON.stringify(name)} with an id`);
  }
  assert.deepEqual(S.recordAuthor(undefined, "cao-real", "dlg-a", 1), []);
});

test("A stored try count that is not a count reads as none: a negative or odd value cannot grant more than two deletes", () => {
  // how many deletes the cleanup sends for an entry whose stored `tries` is this, every one of them failing
  const attempts = (tries) => {
    let authors = freeze([author({ tries })]);
    let n = 0;
    while (S.authorDeletes(authors, null, 500 * SEC, []).length > 0 && n < 10) {
      authors = freeze(S.afterDelete(authors, [], NAME, false).authors);
      n += 1;
    }
    return n;
  };
  for (const tries of [0, -1, -5, -1e9, -Infinity, NaN, "0", "-5", [0], null, undefined, { valueOf: () => -5 }]) assert.equal(attempts(tries), 2, `tries ${String(tries)}`);
  assert.equal(attempts(1), 1);
  for (const tries of [2, 3, 1e300]) assert.equal(attempts(tries), 0, `tries ${tries}`);
  // a failed answer counts from the clamped value, so a stored -5 is one try, not -4
  assert.equal(S.afterDelete(freeze([author({ tries: -5 })]), [], NAME, false).authors[0].tries, 1);
});

test("Slow delete is not sent twice: a name in the in-flight list is not returned, on any number of calls", () => {
  const entries = freeze([author(), author({ name: "cao-draft-flow-bbbbbbbb", id: "t2" })]);
  const flying = freeze([NAME]);
  for (let i = 0; i < 6; i += 1) assert.deepEqual(S.authorDeletes(entries, null, (200 + i * 5) * SEC, flying), ["cao-draft-flow-bbbbbbbb"], `call ${i}`);
  assert.deepEqual(S.authorDeletes(entries, null, 300 * SEC, freeze([NAME, "cao-draft-flow-bbbbbbbb"])), []);
});

test("Failed delete is sent again: a name leaves the list when its answer arrives, and the next tick may send it again", () => {
  const entries = freeze([author()]);
  const none = freeze([]);
  const out = S.deleteStarted(none, NAME);
  assert.deepEqual(out, [NAME]);
  assert.deepEqual(none, [], "the list given is unchanged");
  assert.deepEqual(S.deleteStarted(out, NAME), [NAME], "twice adds one entry");
  assert.deepEqual(S.deleteStarted(freeze([NAME]), "other"), [NAME, "other"]);
  const back = S.deleteEnded(out, NAME);
  assert.deepEqual(back, []);
  assert.deepEqual(out, [NAME], "the list given is unchanged");
  assert.deepEqual(S.deleteEnded(freeze(["a", NAME, "b"]), NAME), ["a", "b"]);
  assert.deepEqual(S.deleteEnded(none, NAME), []);
  assert.deepEqual(S.deleteStarted(undefined, NAME), [NAME]);
  // while in flight it is not returned; once ended and failed once, it is returned again
  assert.deepEqual(S.authorDeletes(entries, null, 300 * SEC, out), []);
  const failedOnce = S.afterDelete(entries, [], NAME, false).authors;
  assert.deepEqual(S.authorDeletes(freeze(failedOnce), null, 300 * SEC, back), [NAME], "the next tick may send it again");
});

test("Launch with no answer keeps the record: status 0 keeps it and the cleanup deletes it by name; 4xx and 5xx remove it", () => {
  const at = 100 * SEC;
  const entries = freeze([author({ id: null, at }), author({ name: "cao-draft-flow-bbbbbbbb", id: null, at })]);
  for (const status of [0, 200, 201, 202, 302, 399, 600, null, undefined, "500", NaN]) {
    assert.deepEqual(S.afterLaunch(entries, NAME, status), entries, `status ${String(status)} keeps it`);
  }
  for (const status of [400, 404, 409, 422, 500, 503, 599]) {
    assert.deepEqual(S.afterLaunch(entries, NAME, status).map((a) => a.name), ["cao-draft-flow-bbbbbbbb"], `status ${status} removes it`);
  }
  assert.deepEqual(S.afterLaunch(entries, "unknown", 500), entries);
  const kept = S.afterLaunch(entries, NAME, 0);
  assert.deepEqual(S.authorDeletes(freeze(kept), null, at + 25 * SEC, []), [NAME, "cao-draft-flow-bbbbbbbb"], "deleted by name 25 s after it was written");
});

test("Delete fails twice after the dialog closed: two failed answers are two tries, then the name is not returned again", () => {
  let authors = freeze([author()]);
  let drafts = ["t1", "t9"];
  assert.deepEqual(S.authorDeletes(authors, null, 300 * SEC, []), [NAME]);
  for (const tries of [1, 2]) {
    const r = S.afterDelete(authors, drafts, NAME, false);
    authors = freeze(r.authors);
    drafts = r.drafts;
    assert.equal(authors[0].tries, tries);
  }
  assert.deepEqual(S.authorDeletes(authors, null, 300 * SEC, []), []);
  assert.equal(authors.length, 1, "the record stays, so Sessions keeps the session with its tag");
  assert.deepEqual(drafts, ["t1", "t9"], "the id stays too");
  assert.equal(authors[0].name, NAME);
  assert.equal(authors[0].at, 100 * SEC);
});

test("A 404 counts as deleted: ok removes the entry and its id from drafts and nothing else", () => {
  const authors = freeze([author({ id: "t1" }), author({ name: "cao-draft-flow-bbbbbbbb", id: "t2" }), author({ name: "cao-draft-flow-cccccccc", id: null })]);
  const drafts = freeze(["t1", "t2", "t9"]);
  const r = S.afterDelete(authors, drafts, NAME, true);
  assert.deepEqual(r.authors.map((a) => a.name), ["cao-draft-flow-bbbbbbbb", "cao-draft-flow-cccccccc"]);
  assert.deepEqual(r.drafts, ["t2", "t9"]);
  const noId = S.afterDelete(authors, drafts, "cao-draft-flow-cccccccc", true);
  assert.deepEqual(noId.authors.map((a) => a.name), [NAME, "cao-draft-flow-bbbbbbbb"]);
  assert.deepEqual(noId.drafts, ["t1", "t2", "t9"]);
  const missing = S.afterDelete(authors, drafts, "nobody", true);
  assert.deepEqual(missing.authors, authors);
  assert.deepEqual(missing.drafts, drafts);
  assert.deepEqual(S.afterDelete(undefined, undefined, NAME, true), { authors: [], drafts: [] });
  // only ok === true is deleted: a status number or text a caller passes by mistake keeps the entry and counts a try
  for (const notOk of [1, 200, 500, "true", "false", {}, [], null, undefined, 0, false]) {
    const kept = S.afterDelete(authors, drafts, NAME, notOk);
    assert.deepEqual(kept.authors.map((a) => [a.name, a.tries]), [[NAME, 1], ["cao-draft-flow-bbbbbbbb", 0], ["cao-draft-flow-cccccccc", 0]], String(notOk));
    assert.deepEqual(kept.drafts, drafts);
  }
});

test("Author recorded before the launch is answered: an entry with the name and owner and no id exists first, and the id added later keeps `at`", () => {
  const before = freeze([author({ name: "cao-draft-flow-bbbbbbbb", owner: "dlg-b", at: 1, tries: 0, id: "t2" })]);
  const first = S.recordAuthor(before, NAME, "dlg-a", 100 * SEC);
  assert.deepEqual(first, [before[0], { name: NAME, owner: "dlg-a", at: 100 * SEC, tries: 0, id: null }]);
  assert.equal(before.length, 1, "the list given is unchanged");
  const withId = S.recordAuthor(freeze(first), NAME, "dlg-a", 130 * SEC, "t7");
  assert.deepEqual(withId[1], { name: NAME, owner: "dlg-a", at: 100 * SEC, tries: 0, id: "t7" }, "at kept, id set");
  assert.deepEqual(withId[0], before[0]);
  assert.equal(withId.length, 2, "no second entry for the same name");
  const tried = S.recordAuthor(freeze([author({ id: null, tries: 1, at: 5 })]), NAME, "dlg-a", 99 * SEC, "t3");
  assert.deepEqual(tried, [{ name: NAME, owner: "dlg-a", at: 5, tries: 1, id: "t3" }], "tries kept too");
  assert.equal(S.recordAuthor(freeze(withId), NAME, "dlg-a", 140 * SEC)[1].id, "t7", "a call with no id does not wipe the one recorded");
  assert.deepEqual(S.recordAuthor(undefined, NAME, "dlg-a", 1), [{ name: NAME, owner: "dlg-a", at: 1, tries: 0, id: null }]);
  // an entry that went through JSON (shared storage) reads the same
  assert.deepEqual(S.authorDeletes(JSON.parse(JSON.stringify(first)), null, 200 * SEC, []), ["cao-draft-flow-bbbbbbbb", NAME]);
});

// ---- 19.6: listView ----

// What GET /flows, GET /workflows and GET /workflows/runs carry beyond what a list row draws: the prompt, the
// script, a workflow's source and input spec, a run's inputs. None of it may reach shared storage.
const SECRET = "TOPSECRET-prompt-source-or-input";
const FLOW_ROW = (name, over = {}) => ({ name, schedule: "0 9 * * mon-fri", enabled: true, next_run: "2026-11-19T09:00:00", prompt_template: SECRET, script: `${SECRET}.py`, agent_profile: "developer", provider: "claude_code", last_run: null, ...over });
const WF_ROW = (name) => ({ name, description: SECRET, mode: "python", step_count: 3, source: SECRET, source_path: `/srv/${SECRET}`, inputs: [{ name: "token", type: "string", default: SECRET }] });
const RUN_ROW = (id, workflow, state, over = {}) => ({ run_id: id, workflow_name: workflow, state, started_at: "2026-11-23T10:00:00Z", current_step_id: "build", inputs: { token: SECRET }, source: SECRET, working_directory: `/w/${SECRET}`, ...over });
const KEYS_BANNED = ["prompt_template", "script", "source", "source_path", "inputs", "input_spec", "working_directory", "description", "agent_profile", "provider"];
const keysDeep = (v, out = new Set()) => {
  if (v && typeof v === "object") for (const [k, x] of Object.entries(v)) { out.add(k); keysDeep(x, out); }
  return out;
};

test("List payload holds no prompt or source: no prompt, source or input spec in either payload, and only the fields a row draws", () => {
  const flowsView = freeze(applyOutcome(null, { kind: "ok", json: [FLOW_ROW("daily"), FLOW_ROW("weekly", { enabled: false })] }, 1000));
  const listView = freeze(applyOutcome(null, { kind: "ok", json: [WF_ROW("wf-a"), WF_ROW("wf-b")] }, 2000));
  const runsView = freeze(applyOutcome(null, { kind: "ok", json: [RUN_ROW("r1", "wf-a", "running"), RUN_ROW("r2", "wf-b", "completed")] }, 3000));
  const f = S.listView("flows", flowsView);
  const w = S.listView("workflows", listView, runsView);
  for (const payload of [f, w]) {
    const text = JSON.stringify(payload);
    assert.ok(!text.includes(SECRET), "no secret text in the payload");
    const banned = [...keysDeep(payload)].filter((k) => KEYS_BANNED.includes(k));
    assert.deepEqual(banned, []);
  }
  assert.deepEqual(Object.keys(f).sort(), ["at", "auth", "rows", "unreachable"]);
  assert.deepEqual(Object.keys(w).sort(), ["at", "auth", "rows", "runs", "runsAt", "unreachable"]);
  assert.deepEqual(f.rows.map((r) => Object.keys(r).sort()), [["enabled", "name", "next_run", "schedule"], ["enabled", "name", "next_run", "schedule"]]);
  assert.deepEqual(w.rows, [{ name: "wf-a", state: "running" }, { name: "wf-b", state: "succeeded" }]);
  assert.deepEqual(w.runs.map((r) => Object.keys(r).sort()), [["current_step_id", "run_id", "started_at", "state", "workflow_name"], ["current_step_id", "run_id", "started_at", "state", "workflow_name"]]);
  assert.deepEqual([f.at, f.auth, f.unreachable, w.at, w.runsAt, w.auth, w.unreachable], [1000, false, false, 2000, 3000, false, false]);
  // what a run row's fields hold are plain values only
  const odd = S.listView("workflows", listView, freeze(applyOutcome(null, { kind: "ok", json: [RUN_ROW("r1", "wf-a", "running", { current_step_id: { secret: SECRET }, started_at: [SECRET] }), 5, null] }, 3000)));
  assert.ok(!JSON.stringify(odd).includes(SECRET));
  assert.equal(odd.runs.length, 1);
  assert.throws(() => S.listView("sessions", flowsView));
});

test("Flows view keeps the next run: each row carries the stored next_run, or null", () => {
  const view = freeze(applyOutcome(null, { kind: "ok", json: [FLOW_ROW("daily"), FLOW_ROW("off", { enabled: false, next_run: "2026-11-01T09:00:00" }), FLOW_ROW("none", { next_run: undefined })] }, 1000));
  const rows = S.listView("flows", view).rows;
  assert.deepEqual(rows.map((r) => [r.name, r.enabled, r.next_run]), [["daily", true, "2026-11-19T09:00:00"], ["off", false, "2026-11-01T09:00:00"], ["none", true, null]]);
  assert.deepEqual(rows[0], { name: "daily", schedule: "0 9 * * mon-fri", enabled: true, next_run: "2026-11-19T09:00:00" });
});

test("Failed read keeps the last rows: the rows and their age stay, the failure only sets its flag", () => {
  const good = applyOutcome(null, { kind: "ok", json: [FLOW_ROW("daily"), FLOW_ROW("weekly")] }, 1000);
  for (const [outcome, flag] of [[{ kind: "unreachable" }, "unreachable"], [{ kind: "auth" }, "auth"], [{ kind: "error", status: 500 }, null]]) {
    const failed = S.listView("flows", freeze(applyOutcome(good, outcome, 9000)));
    assert.deepEqual(failed.rows.map((r) => r.name), ["daily", "weekly"], outcome.kind);
    assert.equal(failed.at, 1000, "the age is the last good read's");
    if (flag) assert.equal(failed[flag], true, flag);
  }
  // workflows: a failed runs read keeps the last runs, and so the states; a list that failed keeps its rows
  const list = applyOutcome(null, { kind: "ok", json: [WF_ROW("wf-a")] }, 2000);
  const runs = applyOutcome(null, { kind: "ok", json: [RUN_ROW("r1", "wf-a", "running")] }, 3000);
  const failedRuns = S.listView("workflows", freeze(list), freeze(applyOutcome(runs, { kind: "unreachable" }, 8000)));
  assert.deepEqual(failedRuns.rows, [{ name: "wf-a", state: "running" }]);
  assert.equal(failedRuns.runsAt, 3000);
  assert.equal(failedRuns.unreachable, true);
  const failedList = S.listView("workflows", freeze(applyOutcome(list, { kind: "auth" }, 8000)), freeze(runs));
  assert.deepEqual(failedList.rows, [{ name: "wf-a", state: "running" }]);
  assert.equal(failedList.auth, true);
  assert.equal(failedList.at, 2000);
  // never read: no rows, no age, and the states are unknown rather than "never run"
  const never = S.listView("workflows", freeze(applyOutcome(null, { kind: "unreachable" }, 5000)), freeze(applyOutcome(null, { kind: "unreachable" }, 5000)));
  assert.deepEqual([never.rows, never.runs, never.at, never.runsAt, never.unreachable], [[], null, null, null, true]);
  const noRuns = S.listView("workflows", freeze(list), freeze(applyOutcome(null, { kind: "unreachable" }, 5000)));
  assert.deepEqual(noRuns.rows, [{ name: "wf-a", state: "unknown" }]);
  assert.equal(noRuns.runs, null);
  assert.deepEqual(S.listView("flows", undefined), { rows: [], at: null, auth: false, unreachable: false });
});

test("Failure on either read of the workflows list says so: auth or unreachable on the list read, or on the runs read, shows in the view", () => {
  const list = freeze(applyOutcome(null, { kind: "ok", json: [WF_ROW("wf-a")] }, 2000));
  const runs = freeze(applyOutcome(null, { kind: "ok", json: [RUN_ROW("r1", "wf-a", "running")] }, 3000));
  const flags = (v) => [v.auth, v.unreachable];
  assert.deepEqual(flags(S.listView("workflows", list, runs)), [false, false], "fixture: both good");
  assert.deepEqual(flags(S.listView("workflows", freeze(applyOutcome(list, { kind: "auth" }, 8000)), runs)), [true, false], "list read: auth");
  assert.deepEqual(flags(S.listView("workflows", list, freeze(applyOutcome(runs, { kind: "auth" }, 8000)))), [true, false], "runs read: auth");
  assert.deepEqual(flags(S.listView("workflows", freeze(applyOutcome(list, { kind: "unreachable" }, 8000)), runs)), [false, true], "list read: unreachable");
  assert.deepEqual(flags(S.listView("workflows", list, freeze(applyOutcome(runs, { kind: "unreachable" }, 8000)))), [false, true], "runs read: unreachable");
  // the flows list has one read
  assert.deepEqual(flags(S.listView("flows", freeze(applyOutcome(null, { kind: "auth" }, 1)))), [true, false]);
});

// ---- 19.7: statusParts, helpView ----

const COUNTS = (over = {}) => ({ sessions: 13, asking: 1, replied: 1, repliedUnknown: 0, working: 1, ...over });
const SEP = ` ${DOT} `;

test("Buttons have states: every count is a button with an active flag for the filter that is on, the sessions part clears the filter, every part has the resting-fill flag", () => {
  const parts = S.statusParts(freeze(COUNTS()), "replied");
  assert.deepEqual(parts.map((p) => p.id), ["cao", "sessions", "asking", "replied", "working"]);
  const byId = Object.fromEntries(parts.map((p) => [p.id, p]));
  for (const id of ["sessions", "asking", "replied", "working"]) assert.equal(byId[id].button, true, id);
  assert.deepEqual(parts.filter((p) => p.active).map((p) => p.id), ["replied"], "only the filter that is on is active");
  assert.equal(parts.every((p) => p.resting === true), true, "the resting fill is set on every part");
  assert.equal(byId.sessions.action, "clear");
  assert.deepEqual([byId.asking, byId.replied, byId.working].map((p) => [p.action, p.filter]), [["filter", "asking"], ["filter", "replied"], ["filter", "working"]]);
  // the lead word is text, not a count: it has no action
  assert.deepEqual([byId.cao.button, byId.cao.action], [false, null]);
  // no filter, an unknown filter: nothing is active; the sessions part is never active
  for (const f of [null, undefined, "bogus", "sessions"]) assert.deepEqual(S.statusParts(COUNTS(), f).filter((p) => p.active), [], String(f));
  for (const f of ["asking", "working"]) assert.deepEqual(S.statusParts(COUNTS(), f).filter((p) => p.active).map((p) => p.id), [f]);
  // the replied part with unknown seen states is still the replied button, shown even when R is 0
  const unknown = S.statusParts(COUNTS({ replied: 0, repliedUnknown: 2 }), "replied");
  assert.deepEqual(unknown.filter((p) => p.active).map((p) => [p.id, p.text]), [["replied", "0 replied (2 unknown)"]]);
  // a count of zero is hidden: no asking part, and so nothing to filter by
  assert.deepEqual(S.statusParts(COUNTS({ asking: 0, working: 0 }), "asking").map((p) => p.id), ["cao", "sessions", "replied"]);
  // the unreachable notice is text (it carries the resting flag but is drawn plain), not a button
  const down = S.statusParts(COUNTS(), null, { unreachable: true });
  assert.deepEqual(down.map((p) => p.id).slice(-1), ["unreachable"]);
  assert.deepEqual([down[down.length - 1].button, down[down.length - 1].action, down[down.length - 1].resting], [false, null, true]);
});

test("One session is still a button: with one session the sessions part reads '1 session' and still clears the filter", () => {
  // spec cao-window-sessions "Status bar counts and filters": each part is a button and the sessions part clears the filter
  const one = S.statusParts(freeze(COUNTS({ sessions: 1 })), "asking")[1];
  assert.deepEqual(one, { id: "sessions", text: "1 session", button: true, action: "clear", filter: null, active: false, resting: true });
  assert.deepEqual(S.statusParts(COUNTS({ sessions: 0, asking: 0, replied: 0, working: 0 }), "asking")[1], { id: "sessions", text: "0 sessions", button: true, action: "clear", filter: null, active: false, resting: true });
});

test("Counts: the parts' text joined is the text barText gives, for every shape of counts", () => {
  for (const counts of [COUNTS(), COUNTS({ sessions: 1, asking: 0, replied: 0, working: 0 }), COUNTS({ sessions: 0, asking: 0, replied: 0, working: 0 }), COUNTS({ replied: 0, repliedUnknown: 3 }), COUNTS({ replied: 2, repliedUnknown: 1, asking: 4, working: 7 })]) {
    for (const unreachable of [false, true]) {
      const parts = S.statusParts(freeze(counts), null, { unreachable });
      assert.equal(parts.map((p) => p.text).join(SEP), barText(counts, { unreachable }), JSON.stringify([counts, unreachable]));
    }
  }
  assert.equal(S.statusParts(COUNTS(), null).map((p) => p.text).join(SEP), `cao${SEP}13 sessions${SEP}1 asking${SEP}1 replied${SEP}1 working`);
  assert.equal(S.statusParts(COUNTS({ sessions: 1 }), null)[1].text, "1 session");
});

test("Help: both notes and the label: 'done means the turn ended', the known-gap sentence and the label, all from knownGapNote()", () => {
  const note = knownGapNote();
  const help = S.helpView();
  assert.equal(help.doneMeans, note.doneMeans);
  assert.match(help.doneMeans, /means the agent's turn ended\.$/);
  assert.equal(help.text, note.text);
  assert.equal(help.text, "An agent that finished by asking you something in prose is not counted as asking.");
  assert.equal(help.label, note.label);
  assert.equal(help.label, HONESTY.knownGap);
  assert.equal(help.label, "known gap: a done agent asking in prose is not counted");
  assert.deepEqual(Object.keys(help).sort(), ["doneMeans", "label", "text"]);
});

// ---- 19.8: the "not submitted" rows (security rule: what the user typed in a Run form is never stored) ----

const APPROVAL = (over = {}) => ({ kind: "approval_required", status: 403, planId: "plan-7", message: "approval is required for this plan", ...over });

test("Approval row survives closing: three fields: the stored row's keys are exactly workflow, planId and at, and approvalRow finds it", () => {
  const rows = S.recordApproval(freeze([]), "wf-a", freeze(APPROVAL()), 1000);
  assert.deepEqual(rows, [{ workflow: "wf-a", planId: "plan-7", at: 1000 }]);
  assert.deepEqual(Object.keys(rows[0]).sort(), ["at", "planId", "workflow"]);
  assert.deepEqual(S.approvalRow(freeze(rows), "wf-a"), { workflow: "wf-a", planId: "plan-7", at: 1000 });
  assert.equal(S.approvalRow(freeze(rows), "wf-b"), null);
  // the plan id as the server's own field name has it, and planId first when both are there
  assert.equal(S.recordApproval([], "wf", { plan_id: "from-server" }, 1)[0].planId, "from-server");
  assert.equal(S.recordApproval([], "wf", { planId: "camel", plan_id: "snake" }, 1)[0].planId, "camel");
  assert.equal(S.recordApproval([], "wf", { planId: "", plan_id: "snake" }, 1)[0].planId, "snake");
  // an outcome without a plan id writes nothing; neither does a missing workflow name
  const before = freeze([{ workflow: "other", planId: "p", at: 5 }]);
  for (const outcome of [APPROVAL({ planId: null }), APPROVAL({ planId: "" }), APPROVAL({ planId: 7 }), APPROVAL({ planId: { id: "x" } }), { kind: "approval_required" }, {}, null, undefined, "plan"]) {
    assert.deepEqual(S.recordApproval(before, "wf-a", outcome, 1000), before, JSON.stringify(outcome));
  }
  for (const name of ["", undefined, null, 5, {}]) assert.deepEqual(S.recordApproval(before, name, APPROVAL(), 1000), before, JSON.stringify(name));
  // a second 403 for the same workflow replaces its row, as the newest
  const again = S.recordApproval(freeze([{ workflow: "wf-a", planId: "old", at: 1 }, { workflow: "wf-b", planId: "b", at: 2 }]), "wf-a", APPROVAL({ planId: "new" }), 3000);
  assert.deepEqual(again, [{ workflow: "wf-b", planId: "b", at: 2 }, { workflow: "wf-a", planId: "new", at: 3000 }]);
  // a stored row with more than three fields is read as three
  assert.deepEqual(S.approvalRow(freeze([{ workflow: "wf-a", planId: "p", at: 1, extra: "x" }]), "wf-a"), { workflow: "wf-a", planId: "p", at: 1 });
  assert.equal(S.approvalRow(undefined, "wf-a"), null);
});

test("Inputs never reach storage: an outcome that carries the typed values, a directory and the server's message gives rows with none of them", () => {
  const SECRET_VALUE = "TOPSECRET";
  const message = "SERVERMSG the plan for these values needs approval";
  const submission = { name: "wf", inputs: { token: SECRET_VALUE }, workingDirectory: "/x" };
  const outcome = APPROVAL({ planId: "plan-9", message });
  // the outcome as the dialog holds it: the call's answer with its own record (what "submit again" resends) alongside
  const held = freeze({ ...outcome, ...approvalRecord(null, outcome, submission), serverMessage: message, submission });
  assert.equal(held.submission.inputs.token, SECRET_VALUE, "the fixture does carry the secret");
  const rows = S.recordApproval(freeze([]), "wf", held, 1234);
  const text = JSON.stringify(rows);
  for (const leaked of [SECRET_VALUE, "/x", "SERVERMSG", message, "submission", "inputs", "state"]) assert.ok(!text.includes(leaked), `${leaked} is not stored`);
  assert.deepEqual(rows, [{ workflow: "wf", planId: "plan-9", at: 1234 }]);
  // a plan id that is not text brings nothing in, whatever it holds
  assert.deepEqual(S.recordApproval([], "wf", { planId: { token: SECRET_VALUE }, plan_id: ["/x"] }, 1), []);
  // a getter that would read the typed values is never called: the row is built from the two named fields only
  let touched = false;
  const trap = { planId: "p", get inputs() { touched = true; return { token: SECRET_VALUE }; }, get submission() { touched = true; return submission; } };
  assert.deepEqual(S.recordApproval([], "wf", trap, 1), [{ workflow: "wf", planId: "p", at: 1 }]);
  assert.equal(touched, false);
  // reading a stored row back adds nothing either
  assert.ok(!JSON.stringify(S.approvalRow(rows, "wf")).includes(SECRET_VALUE));
});

test("Workflow named like a prototype key: round trip: __proto__, constructor and toString are rows like any other", () => {
  for (const name of ["__proto__", "constructor", "toString", "hasOwnProperty", "valueOf"]) {
    const rows = S.recordApproval(freeze([]), name, APPROVAL({ planId: `plan-${name}` }), 42);
    assert.equal(rows.length, 1, name);
    assert.equal(rows[0].workflow, name);
    assert.equal(Object.getPrototypeOf(rows[0]), Object.prototype, "the row's own prototype is untouched");
    const back = JSON.parse(JSON.stringify(rows));
    assert.deepEqual(S.approvalRow(back, name), { workflow: name, planId: `plan-${name}`, at: 42 });
    assert.equal(({}).planId, undefined, "no object was changed");
    assert.equal(({}).workflow, undefined);
    assert.equal(typeof S.approvalRow(back, name), "object");
    // another name is not found through it
    assert.equal(S.approvalRow(back, "other"), null);
  }
  // not recorded, so not found, even though an object would have the key
  assert.equal(S.approvalRow(freeze([]), "constructor"), null);
  assert.equal(S.approvalRow(freeze([]), "__proto__"), null);
  const several = ["__proto__", "constructor", "toString"].reduce((r, n, i) => S.recordApproval(r, n, APPROVAL({ planId: `p${i}` }), i), freeze([]));
  assert.deepEqual(several.map((r) => r.workflow), ["__proto__", "constructor", "toString"]);
  assert.deepEqual(S.clearApproval(freeze(several), "constructor").map((r) => r.workflow), ["__proto__", "toString"]);
});

test("A stored approval row is read as three bounded fields: an object or empty plan id, a missing or odd time are not a row, a long plan id is cut to 80", () => {
  // `caowin.approvals` comes back from disk, so a row there is read as the three fields it should hold, or not at all
  const good = { workflow: "wf", planId: "p", at: 5 };
  const one = (over) => freeze([{ ...good, ...over }]);
  assert.deepEqual(S.approvalRow(one({}), "wf"), good, "fixture: a good row reads back");
  for (const planId of [{ token: "TOPSECRET" }, ["TOPSECRET"], 7, "", null, undefined]) assert.equal(S.approvalRow(one({ planId }), "wf"), null, `planId ${JSON.stringify(planId)}`);
  for (const at of ["x", "5", NaN, Infinity, null, undefined, {}]) assert.equal(S.approvalRow(one({ at }), "wf"), null, `at ${String(at)}`);
  const noAt = { workflow: "wf", planId: "p" };
  assert.equal(S.approvalRow(freeze([noAt]), "wf"), null, "no at at all");
  assert.equal(S.approvalRow(freeze([{ workflow: "", planId: "p", at: 5 }]), ""), null, "no workflow name");
  const cut = S.approvalRow(one({ planId: "p".repeat(1_000_000) }), "wf");
  assert.equal(cut.planId, "p".repeat(80), "a 1 MB plan id is read back as 80 characters");
  // a row that is not readable does not hide a good row of the same workflow after it
  assert.deepEqual(S.approvalRow(freeze([{ workflow: "wf", planId: { x: 1 }, at: 1 }, good]), "wf"), good);
  for (const junk of [null, 5, "wf", ["wf"]]) assert.deepEqual(S.approvalRow(freeze([junk, good]), "wf"), good, JSON.stringify(junk));
});

test("A write drops the unreadable approval rows it finds: only readable rows are written back, and a clock that is no number writes nothing", () => {
  const unreadable = [{ workflow: "w1", planId: { token: "TOPSECRET" }, at: 1 }, { workflow: "w3", planId: "p" }, { workflow: "w4", planId: "p", at: "x" }, null, 5, "w5", { workflow: "", planId: "p", at: 1 }];
  const rows = freeze([...unreadable, { workflow: "w2", planId: "q".repeat(1_000_000), at: 2 }, { workflow: "keep", planId: "k", at: 3 }]);
  const after = S.recordApproval(rows, "wf", APPROVAL({ planId: "plan-7" }), 100);
  assert.deepEqual(after, [{ workflow: "w2", planId: "q".repeat(80), at: 2 }, { workflow: "keep", planId: "k", at: 3 }, { workflow: "wf", planId: "plan-7", at: 100 }]);
  assert.ok(!JSON.stringify(after).includes("TOPSECRET"));
  // the same workflow's unreadable row is replaced, not kept beside the new one
  assert.deepEqual(S.recordApproval(freeze([{ workflow: "wf", planId: { x: 1 }, at: 1 }]), "wf", APPROVAL(), 100), [{ workflow: "wf", planId: "plan-7", at: 100 }]);
  // a time that is no number: no row is written and the list is returned as it was
  for (const now of [undefined, null, NaN, Infinity, "100", {}]) assert.deepEqual(S.recordApproval(rows, "wf", APPROVAL(), now), rows, `now ${String(now)}`);
});

test("Approval row cleared by a good submit: clearApproval removes only that workflow's row", () => {
  const rows = freeze([{ workflow: "a", planId: "pa", at: 1 }, { workflow: "b", planId: "pb", at: 2 }, { workflow: "c", planId: "pc", at: 3 }]);
  assert.deepEqual(S.clearApproval(rows, "b"), [{ workflow: "a", planId: "pa", at: 1 }, { workflow: "c", planId: "pc", at: 3 }]);
  assert.deepEqual(S.clearApproval(rows, "nobody"), rows);
  assert.equal(S.approvalRow(S.clearApproval(rows, "b"), "b"), null);
  assert.equal(S.approvalRow(S.clearApproval(rows, "b"), "a")?.planId, "pa");
  assert.equal(rows.length, 3, "the list given is unchanged");
  assert.deepEqual(S.clearApproval(undefined, "a"), []);
  assert.deepEqual(S.clearApproval(rows, undefined), rows);
});

test("Approval row bounded: a 10,000-character plan id is cut to 80, and 60 recorded workflows leave the newest 50", () => {
  const long = "p".repeat(10_000);
  const one = S.recordApproval([], "wf", APPROVAL({ planId: long }), 1);
  assert.equal(one[0].planId.length, 80);
  assert.equal(one[0].planId, "p".repeat(80));
  assert.equal(S.recordApproval([], "wf", APPROVAL({ planId: "q".repeat(80) }), 1)[0].planId.length, 80, "80 is kept whole");
  assert.equal(S.recordApproval([], "wf", { plan_id: long }, 1)[0].planId.length, 80, "also through plan_id");
  let rows = freeze([]);
  for (let i = 0; i < 60; i += 1) rows = freeze(S.recordApproval(rows, `w${i}`, APPROVAL({ planId: `p${i}` }), i));
  assert.equal(rows.length, 50);
  assert.deepEqual([rows[0].workflow, rows[49].workflow], ["w10", "w59"], "the newest 50, oldest dropped");
  assert.equal(S.approvalRow(rows, "w9"), null);
  assert.equal(S.approvalRow(rows, "w10")?.planId, "p10");
  // a workflow recorded again is the newest and does not take a second place
  const again = S.recordApproval(rows, "w10", APPROVAL({ planId: "fresh" }), 99);
  assert.equal(again.length, 50);
  assert.deepEqual([again[0].workflow, again[49].workflow, again[49].planId], ["w11", "w10", "fresh"]);
});

await finish();
