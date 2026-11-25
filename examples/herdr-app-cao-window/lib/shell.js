// The shell state of the window as pure functions: the labels of the top bar's two buttons, the right sidebar's state, the dialog's
// reducer, lease and author records, the list payloads, the status bar's parts and the help. The mods only
// draw and send what these return, so `node lib/shell.test.mjs` pins the rules. No function mutates its
// arguments. Top-level names are unique across lib/*.js (bundle mode puts every shared module in one scope). Source stays ASCII: the middle dot
// is written as an escape.

import { forgetDraft, isAuthorSession } from "./draft.js";
import { flowRows } from "./flows.js";
import { barText, knownGapNote } from "./logic.js";
import { workflowRows } from "./workflows-view.js";

const SH_DOT = "\u00b7";
const shList = (v) => (Array.isArray(v) ? v : []);

// ---------- the top bar's two buttons ----------

// The label of the Schedules or Workflows button (CAO calls a schedule a flow; the person reads "Schedules"): the bare word, whatever the list holds or whether it was read. The
// counts moved to the right sidebar's own list headers: a count in the bar read `Schedules 5 . all off`,
// which says little and takes room. The arguments a caller may still pass (rows, state) are ignored. To rename the
// buttons, change the two words here.
export function barButtonLabel(kind) {
  if (kind !== "flows" && kind !== "workflows") throw new Error(`no button label for ${String(kind).slice(0, 30)}`);
  return kind === "flows" ? "Schedules" : "Workflows";
}

// ---------- the top bar's wheel ----------

// A wheel turned over the top bar steps to the neighbouring tab, by the rule of Herdr's own tabs mod (mods/tabs/main.js:39-62 of the
// shipped mod, which sends `tab.prev` or `tab.next`): one step per notch of a mouse wheel (`delta_lines`); for a touchpad or a smooth
// wheel (`delta` in pixels) a short swipe of 20 px is one step, a longer one goes on at 60 px a step and never faster than one step in
// 100 ms, and a pause of more than 250 ms, a turn of direction or a `started` phase begins a new gesture. One difference: the direction of
// a swipe step is the sign of what was gathered, Herdr's is the sign of the event that reached the amount, which are the same except
// for an event of 0. The state is {acc, steps, at, last}; an event with no usable delta changes nothing. -> {swipe, step}, step -1
// (the previous tab), 1 (the next) or 0.
export const initialSwipe = () => ({ acc: 0, steps: 0, at: 0, last: 0 });
const shNum = (v) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
export function wheelTabStep(swipe, e, now) {
  const s = swipe !== null && typeof swipe === "object" && !Array.isArray(swipe)
    ? { acc: shNum(swipe.acc), steps: shNum(swipe.steps), at: shNum(swipe.at), last: shNum(swipe.last) } : initialSwipe();
  if (e !== null && typeof e === "object" && e.delta_lines) {
    const y = shNum(e.delta_lines.y);
    return { swipe: s, step: y === 0 ? 0 : y > 0 ? -1 : 1 };
  }
  const dy = e !== null && typeof e === "object" ? e.delta?.y : undefined;
  if (typeof dy !== "number" || !Number.isFinite(dy)) return { swipe: s, step: 0 };
  const next = e.touch_phase === "started" || now - s.at > 250 || dy * s.acc < 0 ? { ...s, acc: 0, steps: 0 } : { ...s };
  next.at = now;
  next.acc += dy;
  const need = next.steps ? 60 : 20;
  if (Math.abs(next.acc) < need || now - next.last < 100) return { swipe: next, step: 0 };
  const dir = Math.sign(next.acc);
  next.acc -= dir * need;
  next.steps += 1;
  next.last = now;
  return { swipe: next, step: dir > 0 ? -1 : 1 };
}

// ---------- the right sidebar's state ----------

const SH_LISTS = ["flows", "workflows"];
const shIsList = (v) => SH_LISTS.includes(v);
const shIsItem = (v) => typeof v === "string" && v !== "";

// A stored state is {open, list, item, form}: whether the sidebar is shown, which list it shows, the item whose
// details it shows (a name, or null for the list view) and the request a form holds in it (null unless a form
// is open from the sidebar).
// Whatever was stored read as a state of this shape: anything else is a closed sidebar at the flows list.
function shRside(raw) {
  const r = raw !== null && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  return {
    open: r.open === true,
    list: shIsList(r.list) ? r.list : "flows",
    item: shIsItem(r.item) ? r.item : null,
    form: r.form !== null && typeof r.form === "object" && !Array.isArray(r.form) ? r.form : null,
  };
}

// The reducer of the right sidebar. A button opens its list (no item), closes when its list is the open one, or
// switches to the other list; a row shows an item's details; back returns to the list; a link or a save from the
// dialog opens the list at an item; close shuts it and drops the item, so the next button always shows the list;
// form and form-close hold and empty the dialog request. Any other event (a session selection included)
// leaves the state as it was: what the sidebar shows is stored apart from the selected session.
export function rsideStep(state, event) {
  const s = shRside(state);
  switch (event?.type) {
    case "button":
      if (!shIsList(event.list)) return s;
      return s.open && s.list === event.list ? { ...s, open: false, item: null } : { ...s, open: true, list: event.list, item: null };
    case "row": return s.open && shIsItem(event.item) ? { ...s, item: event.item } : s;
    case "back": return { ...s, item: null };
    case "link": return shIsList(event.list) && shIsItem(event.item) ? { ...s, open: true, list: event.list, item: event.item } : s;
    case "saved": return shIsList(event.list) && shIsItem(event.item) ? { ...s, open: true, list: event.list, item: event.item, form: null } : s;
    case "close": return { ...s, open: false, item: null };
    case "form": return event.request !== null && typeof event.request === "object" && !Array.isArray(event.request) ? { ...s, form: event.request } : s;
    case "form-close": return { ...s, form: null };
    default: return s;
  }
}

// The scope of one item of a list: what an inline confirm and the answer of its write are keyed by. A list row
// builds it for its own item (the row of flow `a` reads "running" while the list is shown and no item is), so
// the format lives here and nowhere else. null for a list the sidebar does not have or an item that is no name.
export const scopeOf = (list, item) => (shIsList(list) && shIsItem(item) ? `${list}:${item}` : null);

// The scope an inline confirm is made for: the list and item shown now, or null when no item is. A confirm that
// is still open is stale when its scope differs from this (the item changed, the list switched, the sidebar
// closed), and is dropped without sending anything.
export const confirmScope = (rside) => {
  const s = shRside(rside);
  return s.open ? scopeOf(s.list, s.item) : null;
};

// The answer recorded for one item's confirmed write: the latest entry of `ops` ({item, state, message}, state
// "running", "done" or "failed", item a confirmScope) whose item is this one, else null. The entry belongs to the
// item that asked, so it shows there when its details are next shown and on its list row meanwhile, and on no
// other item. A list, so an item named like an object key is only a value.
export function opOf(ops, item) {
  if (typeof item !== "string" || item === "") return null;
  const hits = shList(ops).filter((o) => o !== null && typeof o === "object" && o.item === item);
  return hits.length > 0 ? hits[hits.length - 1] : null;
}

// ---------- the right sidebar's record against the block's real state ----------

// Ticks (calls of rsideLayoutStep) between a show or hide and its single resend.
export const RSIDE_RESEND_TICKS = 4;

export const initialLayoutState = () => ({ started: false, wanted: null, sends: 0, waited: 0 });

// Herdr keeps a block's shown state across restarts, and a Herdr command can change it, so the window's record
// (`rside.open`) and the block (`shown`, true or false) are two records of one fact. One call per tick:
//   - the first call of a run closes the record and sends hide when the block is shown (hidden after every
//     start);
//   - afterwards, when they differ it sends the missing command at once, sends it once more after
//     RSIDE_RESEND_TICKS ticks while they still differ, and RSIDE_RESEND_TICKS ticks after that takes the block's
//     real state as the truth and sends nothing more. A changed wish (the record flipped) is a new first send.
// `shown` that is not a boolean (the layout could not be read) changes nothing, and is not a first call.
// -> {rside, send: "show" | "hide" | null, state}
export function rsideLayoutStep(rside, shown, state) {
  const r = shRside(rside);
  const st = state !== null && typeof state === "object" ? state : initialLayoutState();
  if (typeof shown !== "boolean") return { rside: r, send: null, state: st };
  if (st.started !== true) {
    const send = shown ? "hide" : null;
    return { rside: rsideStep(r, { type: "close" }), send, state: { started: true, wanted: false, sends: send ? 1 : 0, waited: 0 } };
  }
  const want = r.open;
  if (want === shown) return { rside: r, send: null, state: { started: true, wanted: want, sends: 0, waited: 0 } };
  const command = want ? "show" : "hide";
  if (st.wanted !== want || st.sends === 0) return { rside: r, send: command, state: { started: true, wanted: want, sends: 1, waited: 0 } };
  const waited = st.waited + 1;
  if (waited < RSIDE_RESEND_TICKS) return { rside: r, send: null, state: { ...st, waited } };
  if (st.sends < 2) return { rside: r, send: command, state: { started: true, wanted: want, sends: 2, waited: 0 } };
  const adopted = shown ? { ...r, open: true } : rsideStep(r, { type: "close" });
  return { rside: adopted, send: null, state: { started: true, wanted: shown, sends: 0, waited: 0 } };
}

// ---------- the dialog ----------

// The overlay kind of every form: "dialog" (centered, dims). It can be changed to "window".
export const DIALOG_KIND = "dialog";
const SH_BUSY_TEXT = "A request is in progress. Wait for the answer.";
const SH_LOST_NOTE = "The answer may have been lost.";
const shObject = (v) => v !== null && typeof v === "object" && !Array.isArray(v);

// {open, request, dirty, discarding, writing, lost}: the request the dialog was opened with (its own copy, never
// re-read from the selection), whether the form holds edits, whether the discard confirm is showing, whether a
// write is out, and whether that write timed out.
export const initialDialog = () => ({ open: false, request: null, dirty: false, discarding: false, writing: false, lost: false });

const shDialog = (raw) => (shObject(raw) && raw.open === true
  ? { open: true, request: shObject(raw.request) ? raw.request : null, dirty: raw.dirty === true, discarding: raw.discarding === true, writing: raw.writing === true, lost: raw.lost === true }
  : initialDialog());

// The reducer of the dialog's keys and writes -> {state, effects}. Events:
//   open {request}            a second open while open changes nothing
//   dirty {dirty}             the form holds edits or not
//   close {confirming, discard}  Esc or Close; `confirming` names a confirmation the form body shows (Esc there
//                             returns to the form), `discard` is the Discard button of the discard confirm (it counts only while that confirm shows)
//   write-start, write-end    a launch, save or submit is out, or answered (reads are not told to the dialog)
//   timeout                   the write's --max-time plus 5 s passed without an answer
//   saved {list, item}        a flow or workflow save, or a workflow Run answered 202, names the item to show
//                             in the right sidebar; with no item (a launch) the result card stays in the dialog
// Any other event (a selection, a sidebar event) leaves the state equal. Effects: confirm-discard, refuse {message},
// close {note?}, set-rside {rside}.
export function dialogStep(state, event) {
  const s = shDialog(state);
  const none = { state: s, effects: [] };
  if (event?.type === "open") return s.open || !shObject(event.request) ? none : { state: { ...initialDialog(), open: true, request: event.request }, effects: [] };
  if (!s.open) return none;
  switch (event?.type) {
    case "dirty": return { state: { ...s, dirty: event.dirty === true }, effects: [] };
    case "write-start": return { state: { ...s, writing: true, lost: false }, effects: [] };
    case "write-end": return { state: { ...s, writing: false, lost: false }, effects: [] };
    case "timeout": return s.writing ? { state: { ...s, lost: true }, effects: [] } : none;
    case "saved": {
      const ended = { ...s, writing: false, lost: false };
      if (!shIsList(event.list) || !shIsItem(event.item)) return { state: ended, effects: [] };
      return { state: initialDialog(), effects: [{ type: "set-rside", rside: rsideStep(undefined, { type: "saved", list: event.list, item: event.item }) }, { type: "close" }] };
    }
    case "close":
      if (s.discarding && event.discard === true) return { state: initialDialog(), effects: [{ type: "close" }] };
      if (s.discarding || event.confirming) return { state: { ...s, discarding: false }, effects: [] };
      if (s.writing) return s.lost ? { state: initialDialog(), effects: [{ type: "close", note: SH_LOST_NOTE }] } : { state: s, effects: [{ type: "refuse", message: SH_BUSY_TEXT }] };
      if (!s.dirty) return { state: initialDialog(), effects: [{ type: "close" }] };
      return { state: { ...s, discarding: true }, effects: [{ type: "confirm-discard", title: "Discard this form?", defaultAction: "Keep editing" }] };
    default: return none;
  }
}

// The overlay a request opens: the size (0.85 of the window each way for a form, 360 by 220 px for the status
// bar help), the kind, and the props the overlay copy gets. An overlay copy is given only these, not the layout
// block's props, so they carry the opener's layout props (the CAO machine, its port, the profile lists) except
// `section` and `host`, then the request; the request wins a clash, and host is "dialog".
export function dialogSpec(request, layoutProps) {
  const { section: _section, host: _host, ...layout } = shObject(layoutProps) ? layoutProps : {};
  const req = shObject(request) ? request : {};
  const help = req.kind === "bar-help";
  // host comes last: the request wins every other clash, but not this one, because the overlay copy picks its
  // surface (dialog or sidebar) from props().host, so a request cannot send it to the sidebar.
  return { kind: DIALOG_KIND, modal: true, width: help ? 360 : 0.85, height: help ? 220 : 0.85, props: { ...layout, ...req, host: "dialog" } };
}

// ---------- the dialog lease and the author records ----------

// `caowin.dialog` is {name, until}: the dialog renews it every second with leaseFor, so it is live for 10 s after
// its last renewal. Herdr closes some overlays without running mod code, so the record is read against the
// reader's own clock and nobody clears it.
const LEASE_MS = 10_000;
export const leaseFor = (name, now) => ({ name, until: now + LEASE_MS });

// Whether a record keeps the dialog open: its deadline is still ahead of `now` and no more than LEASE_MS ahead
// (a clock set back leaves one far in the future, which is dead), and, when a name is given, it is that
// dialog's. No record and a record of the wrong shape are not live.
export function dialogLive(record, now, name) {
  if (!shObject(record) || !Number.isFinite(record.until) || !Number.isFinite(now)) return false;
  if (!(now < record.until && record.until <= now + LEASE_MS)) return false;
  return name === undefined || record.name === name;
}

// `caowin.authors` is a list of {name, owner, at, tries, id}: a Draft with AI author session by the name chosen
// for it, the dialog that owns it, when it was recorded, how many deletes failed and its terminal id (null until
// the launch answered 201). `caowin.drafts` stays a list of ids.
const AUTHOR_LAUNCH_MS = 25_000;   // the launch's --max-time plus 5 s
const AUTHOR_TRIES = 2;
const shHasId = (a) => typeof a?.id === "string" && a.id !== "";
// A try count is a whole number from 0 up; whatever else is stored (a negative, a fraction, text) counts as 0, so
// a forged value cannot grant more than AUTHOR_TRIES deletes.
const shTries = (a) => (Number.isInteger(a?.tries) && a.tries >= 0 ? a.tries : 0);

// Records an author before its launch is sent (no id), or sets the id on the entry of that name, keeping its `at`
// and `tries`. A call with no id does not wipe the one recorded. A name that is not one of the window's own
// author sessions (isAuthorSession) is not recorded. A new list.
export function recordAuthor(authors, name, owner, now, id) {
  const list = shList(authors);
  if (!isAuthorSession(name)) return list;
  const newId = typeof id === "string" && id !== "" ? id : null;
  if (list.some((a) => a?.name === name)) return list.map((a) => (a?.name === name ? { ...a, id: newId ?? (shHasId(a) ? a.id : null) } : a));
  return [...list, { name, owner, at: now, tries: 0, id: newId }];
}

// The names to send DELETE /sessions/<name> for now: fewer than AUTHOR_TRIES failed tries, no DELETE out
// (`inFlight`, the names the always-drawn mod keeps in its own memory), an owner that is not live, and, while no
// id is recorded, only once the launch's time limit has passed since `at`, so a launch still out is not deleted
// before it creates its session. The name is the key, and only the window's own author-session names count
// (isAuthorSession): `caowin.authors` is read from disk, so it cannot choose another session to delete. No
// terminal is looked up.
export function authorDeletes(authors, record, now, inFlight) {
  const out = shList(inFlight);
  return shList(authors).filter((a) => isAuthorSession(a?.name)
    && shTries(a) < AUTHOR_TRIES
    && !out.includes(a.name)
    && !(typeof a.owner === "string" && a.owner !== "" && dialogLive(record, now, a.owner))
    && (shHasId(a) || now >= a.at + AUTHOR_LAUNCH_MS)).map((a) => a.name);
}

// After a DELETE answered: ok (a 2xx or a 404) removes the entry and its id from drafts; else the entry counts a
// try and stays. -> {authors, drafts}, new lists.
export function afterDelete(authors, drafts, name, ok) {
  const list = shList(authors);
  const entry = list.find((a) => a?.name === name);
  if (!entry) return { authors: list, drafts: shList(drafts) };
  if (ok === true) return { authors: list.filter((a) => a !== entry), drafts: forgetDraft(drafts, entry.id) };
  return { authors: list.map((a) => (a === entry ? { ...a, tries: shTries(a) + 1 } : a)), drafts: shList(drafts) };
}

// After the author launch answered with `status`: an HTTP error status (400 to 599) means no session exists, so
// the entry goes; anything else keeps it, status 0 (curl 000, a timeout) included, because the server may still
// create the session and the cleanup deletes it by name. A 201 changes nothing here (recordAuthor adds the id).
export function afterLaunch(authors, name, status) {
  const list = shList(authors);
  return Number.isInteger(status) && status >= 400 && status <= 599 ? list.filter((a) => a?.name !== name) : list;
}

// The two edits of the status mod's own list of names it has a DELETE out for: a new list each, never an edit.
export const deleteStarted = (inFlight, name) => (shList(inFlight).includes(name) ? [...shList(inFlight)] : [...shList(inFlight), name]);
export const deleteEnded = (inFlight, name) => shList(inFlight).filter((n) => n !== name);

// ---------- the list payloads the bar publishes ----------

// Shared storage is saved to disk, so a payload holds only what a list row draws, built field by field from an
// allow-list: no prompt, no script, no workflow source and no run input ever gets in, and none is named here
// to be stripped. A run keeps five plain fields.
const SH_RUN_FIELDS = ["run_id", "workflow_name", "state", "started_at", "current_step_id"];
const shPlain = (v) => (typeof v === "string" || (typeof v === "number" && Number.isFinite(v)) ? v : null);
const shRun = (run) => Object.fromEntries(SH_RUN_FIELDS.map((key) => [key, shPlain(run[key])]));
const shAt = (view) => (Number.isFinite(view?.at) ? view.at : null);

// `caowin.flowsView` and `caowin.workflowsView` from the applyOutcome views of the reads (a failed read keeps the
// last good data in the view, so it keeps the rows; `at` is the time of the last good read, null before one):
//   listView("flows", flowsRead)                    -> {rows: [{name, schedule, enabled, next_run}], at, auth, unreachable}
//   listView("workflows", listRead, runsRead)       -> {rows: [{name, state}], runs, at, runsAt, auth, unreachable}
// `runs` is null until the runs were read (the rows' states then read "unknown", never "never run"). A view in the
// auth state or unreachable on either read says so.
export function listView(kind, view, runsView) {
  if (kind === "flows") return { rows: flowRows(view?.data), at: shAt(view), auth: view?.auth === true, unreachable: view?.unreachable === true };
  if (kind !== "workflows") throw new Error(`no list view for ${String(kind).slice(0, 30)}`);
  const runs = Array.isArray(runsView?.data) ? runsView.data : null;
  return {
    rows: workflowRows(view?.data, runs),
    runs: runs === null ? null : runs.filter(shObject).map(shRun),
    at: shAt(view),
    runsAt: shAt(runsView),
    auth: view?.auth === true || runsView?.auth === true,
    unreachable: view?.unreachable === true || runsView?.unreachable === true,
  };
}

// ---------- the status bar's parts and its help ----------

// The OS title of the window, the text Mission Control, Cmd-` and the window switchers show instead of "Herdr". The status mod gives it to setWindowTitle once, when it starts; Herdr does not keep it over a restart.
export const WINDOW_TITLE = "CAO";

// The text of the wordmark at the top-left of the left sidebar, which Herdr draws as "Herdr". The brand mod
// draws it beside Herdr's logo. It is the window's title, so the two cannot name the window differently.
export const BRAND_TEXT = WINDOW_TITLE;

// The mark beside it: the project's own, in two drawings kept in the brand mod's folder (the site's cao-mark.svg for light
// surfaces and cao-mark-dark.svg for dark ones). Herdr's logo was drawn at 20 px with a 5 px radius; the mark takes the same room. The
// drawings have several colours, so the mod draws them with `image()`; `svg()` takes one tint colour and would flatten them.
export const BRAND_MARK_SIZE = 20;
export const BRAND_MARK_RADIUS = 5;

// Whether a theme is dark, from the fill the brand row is drawn on: its `chrome` token, else `background` when chrome is missing or not a
// hex colour (#rgb, #rrggbb, #rrggbbaa; the alpha is ignored). Dark means WCAG relative luminance below 0.179, where white and black
// give the same contrast. Anything unreadable is not dark, so the light drawing is the default.
const SH_HEX = /^#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i;
function shLuminance(color) {
  if (typeof color !== "string" || !SH_HEX.test(color)) return null;
  const h = color.slice(1);
  const full = h.length === 3 ? [...h].map((c) => c + c).join("") : h;
  const [r, g, b] = [0, 2, 4].map((i) => {
    const v = parseInt(full.slice(i, i + 2), 16) / 255;
    return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
export function isDarkTheme(theme) {
  const l = shLuminance(theme?.chrome) ?? shLuminance(theme?.background);
  return l !== null && l < 0.179;
}

// The drawing for a theme, as the path `image()` takes (mods/<mod>/<file>, as the other mods name their own drawings).
export const brandMark = (theme) => `mods/cao-window-brand/${isDarkTheme(theme) ? "cao-mark-dark" : "cao-mark"}.svg`;

// What each part of barText's text is, by its wording: [id, pattern, filter]. The lead word and the unreachable
// notice are text; the four counts are buttons.
const SH_PARTS = [
  ["sessions", /^\d+ sessions?$/, null],
  ["asking", / asking$/, "asking"],
  ["replied", /^\d+ replied/, "replied"],
  ["working", / working$/, "working"],
];

// The bar's parts, in order, as the mod draws them: {id, text, button, action, filter, active, resting}. The text
// is barText's, part by part (`opts.unreachable` as for barText). Every part carries the resting flag (the faint fill the mod draws on a button at rest). The four counts
// are buttons: asking, replied and working toggle their filter (`action` "filter"), sessions clears it (`action`
// "clear"), and the part of the filter that is on is `active`. The lead word and the unreachable notice are not
// buttons (`action` null). An unknown or empty `filter` leaves every part inactive.
export function statusParts(counts, filter, opts) {
  return barText(counts, opts).split(` ${SH_DOT} `).map((text, i) => {
    const hit = SH_PARTS.find(([, re]) => re.test(text));
    const id = hit ? hit[0] : text === "CAO unreachable" ? "unreachable" : i === 0 ? "cao" : text;
    const part = hit ? hit[2] : null;
    return {
      id,
      text,
      button: hit !== undefined,
      action: part !== null ? "filter" : id === "sessions" ? "clear" : null,
      filter: part,
      active: part !== null && part === filter,
      resting: true,
    };
  });
}

// The status bar help: the sentence that "done" means the turn ended, the known-gap sentence and the honesty label,
// all as knownGapNote gives them.
export function helpView() {
  const note = knownGapNote();
  return { doneMeans: note.doneMeans, text: note.text, label: note.label };
}

// ---------- the "not submitted" rows of a workflow Run that needs plan approval ----------

// `caowin.approvals` is a list of {workflow, planId, at}, saved to disk with the rest of shared storage. What the
// user typed in the Run form, the directory and the server's own words stay in the dialog's memory; a row is built
// from exactly three values read by name, never by copying the outcome, and a row read back is rebuilt the same
// way. A list, so a workflow named like an object key is only a value.
const SH_PLAN_ID_MAX = 80;
const SH_APPROVALS_MAX = 50;
// A row as it is read, from storage or from the outcome: a workflow name, a plan id (text, cut to 80) and a time
// (a number), or null when any of the three is not what it should be.
const shRow = (r) => (shObject(r) && typeof r.workflow === "string" && r.workflow !== "" && typeof r.planId === "string" && r.planId !== "" && Number.isFinite(r.at)
  ? { workflow: r.workflow, planId: r.planId.slice(0, SH_PLAN_ID_MAX), at: r.at }
  : null);
const shPlanId = (outcome) => [outcome?.planId, outcome?.plan_id].find((v) => typeof v === "string" && v !== "");

// After a 403 approval_required answer for `workflow`: its row, the plan id cut to 80 characters and `now`,
// replacing the row of the same workflow and kept as the newest of at most 50. An outcome without a plan id, a
// missing workflow name or a `now` that is no number writes nothing. The rows already stored are written back
// as rows (shRow), so one that is not readable is dropped here. A new list.
export function recordApproval(rows, workflow, outcome, now) {
  const list = shList(rows);
  const planId = shPlanId(outcome);
  if (planId === undefined || typeof workflow !== "string" || workflow === "" || !Number.isFinite(now)) return list;
  const row = { workflow, planId: planId.slice(0, SH_PLAN_ID_MAX), at: now };
  return [...list.map(shRow).filter((r) => r !== null && r.workflow !== workflow), row].slice(-SH_APPROVALS_MAX);
}

// After a good submit of `workflow`: its row goes, no other. A new list.
export const clearApproval = (rows, workflow) => shList(rows).filter((r) => r?.workflow !== workflow);

// The first readable row of `workflow`, as three fields, or null.
export function approvalRow(rows, workflow) {
  return shList(rows).map(shRow).find((r) => r !== null && r.workflow === workflow) ?? null;
}
