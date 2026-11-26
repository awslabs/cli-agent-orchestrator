// The status bar: "cao, S sessions, A asking, R replied, W working" (middle dots between the parts) from Herdr's
// push data (no HTTP). Each count is a button with a resting faint fill, a hover and, for the filter that is on, a
// highlight; asking, replied and working filter the Sessions block, and the sessions part clears the filter. A
// "?" button ends the bar and opens the status bar help in the dialog. The Dock badge with the asking count lives
// here, not in the toast mod, so it works with or without a toast mod. "CAO unreachable" comes from the Sessions
// block's health probe (shared "caowin.cao"); the counts keep updating without it.
//
// This mod also deletes the Draft with AI author sessions a closed dialog left behind: the dialog's own copy
// dies with it, and this mod is always drawn. It reads the records in `caowin.authors` and the dialog's lease,
// and asks lib/shell.js which names to delete; it writes no lease and clears nothing at start, because a dead
// lease record is harmless.
import { View, div } from "gpui-kit";
import { h_flex } from "gpui-base";
import { exec, machines, spaces, terminals, theme, props, shared, setShared, setBadge, setWindowTitle, checking } from "herdr";
import { callCao } from "./cao.js";
import { createStore } from "./store.js";
import { openDialog, dialogBusyNotice } from "./ui.js";
import { draftCall } from "./draft-view.js";
import { buildModel, holdModel, statusCounts, seenState, nextFilter } from "./logic.js";
import { statusParts, authorDeletes, afterDelete, deleteStarted, deleteEnded, WINDOW_TITLE } from "./shell.js";

const KEYS = { filter: "caowin.filter", cao: "caowin.cao", authors: "caowin.authors", drafts: "caowin.drafts", dialog: "caowin.dialog" };
const listOf = (v) => (Array.isArray(v) ? v : []);

export default class CaoStatus extends View {
  init(_initial, cx) {
    this.store = createStore({ getShared: shared, setShared, local: localStorage });
    this.lastModel = null;                          // the last model that had sessions
    this.badge = undefined;                         // the last text given to setBadge; it does not outlive the app
    this.machine = undefined;                       // the CAO machine of the last model, for the delete calls
    this.inFlight = [];                             // the names this mod has a DELETE out for (in memory only)
    if (checking()) return;                         // a check renders once: no timers, no badge, no title, no exec
    setWindowTitle(WINDOW_TITLE);                   // once: the title does not outlive the app, so it is set at every start, never in render
    this.syncBadge();
    cx.timer.every(1000, () => { this.syncBadge(); this.cleanAuthors(); });
  }

  // The model and the counts of the CAO terminals; the fallback answers come from the Sessions block's copy.
  counts() {
    const saved = shared("caowin.cache");
    const m = buildModel(terminals() ?? [], spaces(), props(), {
      cache: saved && typeof saved === "object" && !Array.isArray(saved) ? saved : {},
      drafts: listOf(shared("caowin.drafts")), pins: listOf(shared("caowin.pins")), local: shared("caowin.seen") ?? {},
    });
    this.machine = m.machine;
    // A lost connection empties terminals(): keep counting the last non-empty model, and keep the badge.
    const connected = machines().some((x) => x.name === m.machine && x.layout !== "unavailable");
    const { model } = holdModel(this.lastModel, m, connected);
    if (m.sessions.length > 0) this.lastModel = m;
    return statusCounts(model.sessions, model.replied, (t) => seenState(t, model.local) === "unknown");
  }

  syncBadge() {
    const asking = this.counts().asking;
    const text = asking > 0 ? String(asking) : null;
    if (text === this.badge) return;
    this.badge = text;
    setBadge(text);
  }

  cfg() { return { machine: this.machine, port: props()?.caoPort }; }

  // ---------- author sessions a closed dialog left behind ----------

  // Once a second: the recorded author sessions whose dialog is no longer live, not already being deleted.
  cleanAuthors() {
    const authors = listOf(this.store.get(KEYS.authors));
    if (authors.length === 0) return;
    for (const name of authorDeletes(authors, this.store.get(KEYS.dialog), Date.now(), this.inFlight)) this.deleteAuthor(name);
  }

  // DELETE /sessions/<name>. The name is in the in-flight list from before the request is sent until its answer
  // arrives, whatever the answer is; the answer is recorded (a 2xx or a 404 is a success, anything else a try).
  async deleteAuthor(name) {
    this.inFlight = deleteStarted(this.inFlight, name);
    let ok = false;
    try {
      // draftCall refuses a name that is not an author session of this window, so no other session is deleted from here.
      const outcome = await callCao(exec, this.cfg(), draftCall({ type: "delete", sessionName: name }));
      ok = outcome.kind === "ok" || (outcome.kind === "error" && outcome.status === 404);
    } catch {
      ok = false;
    }
    this.inFlight = deleteEnded(this.inFlight, name);
    const next = afterDelete(listOf(this.store.get(KEYS.authors)), listOf(this.store.get(KEYS.drafts)), name, ok, Date.now());
    this.store.set(KEYS.authors, next.authors);
    this.store.set(KEYS.drafts, next.drafts);
  }

  // ---------- drawing ----------

  // One part of the bar. A count is a button: a resting faint fill, a hover, and the highlight of the filter that is on.
  partView(t, part) {
    const cell = h_flex().id(`cao-bar-${part.id}`).h(20).px(6).items_center().rounded(4).text_color(t.muted);   // fixed: one line of a 28 px status bar
    if (!part.button) return cell.when(part.id === "unreachable", (e) => e.text_color(t.blocked)).child(part.text);
    return cell.px(7).bg(part.resting ? t.surface : t.chrome)   // px 7: the 1 px the old outline took
      .cursor_pointer().hover((s) => s.bg(t.hover))
      .when(part.active, (e) => e.bg(t.selected).text_color(t.foreground))
      .on_click(() => this.store.set(KEYS.filter, part.action === "filter" ? nextFilter(this.store.get(KEYS.filter) ?? null, part.filter) : null))
      .child(part.text);
  }

  render() {
    const t = theme();
    const parts = statusParts(this.counts(), this.store.get(KEYS.filter) ?? null, { unreachable: this.store.get(KEYS.cao)?.unreachable === true });
    return h_flex().id("cao-status").size_full().px(8).gap(2).items_center().bg(t.chrome).border_t(1).border_color(t.border)
      .text_size(12).children(parts.map((part) => this.partView(t, part)))
      .when(dialogBusyNotice() !== null, (e) => e.child(h_flex().id("cao-bar-busy").h(20).px(6).items_center().text_color(t.blocked).child(dialogBusyNotice())))   // fixed: one line of a 28 px status bar
      .child(div().flex_1())
      .child(h_flex().id("cao-bar-help").w(20).h(20).flex_none().items_center().justify_center().rounded(4).bg(t.surface)   // fixed: a control with one character
        .text_color(t.muted).cursor_pointer().hover((s) => s.bg(t.hover))
        .on_click((_e, cx) => { openDialog({ kind: "bar-help" }); cx.notify(); })
        .child("?"));
  }
}
