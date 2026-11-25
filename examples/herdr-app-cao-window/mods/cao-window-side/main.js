// The left sidebar of the CAO window: one mod, two layout blocks told apart by props().section. "sessions" is
// one line per session (lib/agents.js sessionLine) with the header "+" that opens the dialog;
// "agents" is the selected session's header lines and agent tree (agentsHeader, agentRows). Every row is one line:
// a session's letter badge or an agent's program logo, a name that ends in an ellipsis when it does not fit, and at the
// right end the state mark (and for an agent its age before it and its pin after it, as in Herdr's own lists); the
// state's words and the other facts are the row's tooltip, and an honesty label is never cut (each
// `truncate()` below says what it cuts). The rules live in
// lib/agents.js, lib/shell.js and lib/logic.js; this file watches Herdr's push data, fetches the per-terminal fallback for
// terminals without a cao.profile label, and the health probe, and draws. The Sessions copy runs `tick`; the Agents copy runs `tickAgents` (the recap read
// and a repaint) and none of the Sessions duties, so herdr.seen is written once per focus and the fallback
// fetches do not double.
//
// Every CAO call goes through callCao (lib/cao.js): exec on the CAO machine through herdr-core, never
// fetch. The mods share state through shared() keys under "caowin." (KEYS below).
import { View, div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { exec, hc, spaces, machines, terminals, focused, layout, run, theme, props, shared, setShared, checking, window } from "herdr";
import { callCao, seg, AUTH_MESSAGE } from "./cao.js";
import { createStore } from "./store.js";
import { shapeMark, pinIcon, programMark, crownIcon, sessionBadge, focusTerminal, openDialog, dialogBusyNotice } from "./ui.js";
import { sessionLine, agentRows, pinMark, focusedRow, sessionSelection, agentsHeader, sessionsHeader, foldSummary, badgeColors, badgeFill, goalLines, CROWN_MARK } from "./agents.js";
import { rsideStep } from "./shell.js";
import {
  buildModel, holdModel, sharedCache, cacheKey, shownBlock, fallbackPlan, markPending, fallbackResult, prunePins, initialObs, observe, quietHint,
  filterSessions, navStep, applyOutcome, writesDisabled, readOfThisTurn, repaintDue,
  headerNote, rememberFinishedSeen, seenWrite, seenVerify, SEEN_MODE_DEFAULT,
} from "./logic.js";

const KEYS = {
  selection: "caowin.selection", pins: "caowin.pins", seen: "caowin.seen", drafts: "caowin.drafts",
  filter: "caowin.filter", cache: "caowin.cache", cao: "caowin.cao",
  colors: "caowin.colors",                    // session name -> the colour name of its letter badge (Herdr's spaces rule, see badgeColors)
  rside: "caowin.rside",                      // the right sidebar's state; the Agents block's flow link writes it
  flows: "caowin.flows",                      // the trimmed flow list the top bar publishes; the Agents header reads it
};
// The layout block of each section (make-window.jq) and the size a fold returns to.
const BLOCKS = {
  sessions: { id: "cao-sessions", title: "Sessions", open: { weight: 1 } },
  agents: { id: "cao-agents", title: "Agents", open: { weight: 1 } },
};
const TICK_MS = 1000;
const HEALTH_MS = 60_000;
const REPAINT_MS = 30_000;      // the times in state age; a repaint redraws the whole window, so rarely
const COLLAPSED_PX = 34;        // the header row: what a fold resizes the block to
const COLLAPSED_MAX_PX = 40;    // a block this short or shorter reads as folded
const PIN_SLOT = 18;            // the pin's width: its 14 px drawing and 2 px of padding on each side
const listOf = (v) => (Array.isArray(v) ? v : []);
const focusKey = () => `${focused()?.terminal ?? ""}|${focused()?.agent ?? ""}`;   // the terminal Herdr has focused, as one comparable string
const finishedTurn = (t) => t.state === "done" || t.state === "idle";

// The small text of a row that must be read whole (an honesty label, a note, an age): it does not shrink and
// does not wrap, so the name beside it is what gives way, and none of it is ever cut (cao-window-honesty-labels).
const whole = (text, color) => div().flex_none().whitespace_nowrap().text_size(10.5).text_color(color).child(text);

export default class CaoSide extends View {
  init(_initial, cx) {
    this.section = Object.hasOwn(BLOCKS, props()?.section) ? props().section : "sessions";
    this.store = createStore({ getShared: shared, setShared, local: localStorage });
    const saved = this.store.get(KEYS.cache);
    this.cache = saved && typeof saved === "object" && !Array.isArray(saved) ? saved : {};   // terminal id -> fallbackResult entry
    this.obs = initialObs(Date.now());              // quiet-worker observation
    this.seenState = { mode: SEEN_MODE_DEFAULT, pending: null };
    this.view = {};                                 // every callCao outcome, folded by applyOutcome
    this.lastHealth = 0;
    this.quiet = "";
    this.lastModel = null;                          // the last model that had sessions
    this.reads = {};                                // Agents: terminal id -> {turn, text}, the screen read once per finished turn
    this.paintedAt = Date.now();                    // Agents: when the block last painted (the times in state age)
    this.paintedFocus = "";                         // Agents: the terminal Herdr had focused at the last paint (the highlighted row)
    this.nav = { index: 0, active: false };
    this.items = [];
    this.height = null;
    this.width = null;   // the block's width as last drawn: the recap's clamp reads it (goalLines)
    this.keys = cx.focus_handle();
    if (checking()) return;                         // a check renders once: no timers, no exec
    if (this.section === "sessions") cx.timer.every(TICK_MS, (cx) => this.tick(cx));
    else if (this.section === "agents") cx.timer.every(TICK_MS, (cx) => this.tickAgents(cx));
  }

  model() {
    const m = buildModel(terminals() ?? [], spaces(), props(), {
      cache: this.cache, drafts: listOf(this.store.get(KEYS.drafts)), pins: listOf(this.store.get(KEYS.pins)),
      local: this.store.get(KEYS.seen) ?? {},
    });
    // "loading" counts as connected: the machine is known, its layout is not read yet.
    const connected = machines().some((x) => x.name === m.machine && x.layout !== "unavailable");
    // A lost connection empties terminals(): keep the last non-empty list and draw it dimmed.
    const held = holdModel(this.lastModel, m, connected);
    if (m.sessions.length > 0) this.lastModel = m;
    return { ...held.model, dimmed: held.dimmed };
  }

  // Three things need time: the per-terminal fallback and health fetches, the pin prune, and the seen check.
  tick(cx) {
    const m = this.model();
    const now = Date.now();
    this.obs = observe(this.obs, m.terms, now);
    const quiet = m.sessions.filter((s) => quietHint(s, this.obs, m.props?.quietMinutes ?? 10, now)).map((s) => s.name).join();
    if (quiet !== this.quiet) { this.quiet = quiet; cx.notify(); }
    const pruned = prunePins(m.pins, m.terms);
    if (pruned.changed) this.store.set(KEYS.pins, pruned.pins);
    this.watchSeen(m, now);
    this.syncCache(m);
    if (!shownBlock(layout(), BLOCKS[this.section].id)) return;     // a hidden block fetches nothing
    const ids = fallbackPlan(this.cache, m.terms, now);
    if (ids.length) {
      this.cache = markPending(this.cache, ids, now);
      for (const id of ids) this.fetchTerminal(cx, id, m);
    }
    if (now - this.lastHealth >= HEALTH_MS) {
      this.lastHealth = now;
      this.fetchHealth(cx, m);
    }
  }

  // The Agents copy's own tick: the recap of the selected session's talkable agent, read once per finished turn
  // (and so the context percentage), the profiles and callers the Sessions copy has learned since (adoptCache), and a
  // repaint now and then so the times in state age. No fetch and no write.
  tickAgents(cx) {
    const m = this.model();
    this.adoptCache(m, cx);
    const sel = sessionSelection(this.store.get(KEYS.selection));
    const s = sel ? m.sessions.find((x) => x.name === sel.name) : undefined;
    for (const t of s?.talkable ?? []) {
      if (finishedTurn(t) && t.turn >= 1 && this.reads[t.id]?.turn !== t.turn) this.readScreen(cx, t);
    }
    if (focusKey() !== this.paintedFocus) cx.notify();      // the focus moved (agent.next, space.select, a click elsewhere): the highlighted row follows
    if (repaintDue(this.paintedAt, Date.now(), REPAINT_MS)) { this.paintedAt = Date.now(); cx.notify(); }
  }

  // `hc read <machine>/<terminal>` through the window's own core, where the CAO machine is listed (the same
  // route as the seen label write). The recap and the context percentage are read from that text, and only
  // while the agent is still at the turn the text was read at (readOfThisTurn).
  readScreen(cx, t) {
    this.reads[t.id] = { turn: t.turn };            // no second read for this turn, whatever happens
    cx.spawn(async (cx2) => {
      let text = "";
      try {
        const r = await hc(["read", t.address]);
        if (r?.code === 0) text = r.stdout;
      } catch { /* the block then shows no recap: nothing is inferred */ }
      this.reads[t.id] = { turn: t.turn, text };
      cx2.notify();
    });
  }

  cfg(m) { return { machine: m.machine, port: m.props?.caoPort }; }

  // GET /terminals/{id}, once per terminal without a cao.profile label (an older server, or a restored window).
  fetchTerminal(cx, id, m) {
    cx.spawn(async (cx) => {
      let outcome;
      try { outcome = await callCao(exec, this.cfg(m), { path: `/terminals/${seg(id)}`, maxTime: 4 }); }
      catch { outcome = { kind: "error", status: 0, detail: "unsafe terminal id" }; }
      this.report(outcome);
      this.syncCache(this.model(), { [id]: fallbackResult(this.cache, id, outcome, Date.now())[id] });
      cx.notify();
    });
  }

  // Merges what another copy published under the shared key into this copy's cache, prunes it to the terminals still listed and
  // publishes what the other mods use (sharedCache), only when that differs from what is stored. The stored value is read first and
  // is part of what is published, so this copy never overwrites an entry it does not hold. `fetched` holds the entries a finished
  // fetch added.
  syncCache(m, fetched = {}) {
    const stored = this.store.get(KEYS.cache);
    const next = sharedCache(this.cache, m.terms.map((x) => x.id), fetched, stored);
    this.cache = next.cache;
    if (cacheKey(next.published) === cacheKey(stored)) return;
    this.store.set(KEYS.cache, next.published);
  }

  // The Agents copy never fetches, so its own cache was only what the shared key held when it started: a terminal that came later stayed
  // unresolved and its tree was drawn flat. Every tick it takes the answered entries under the shared key (a read of a local value), and
  // repaints when that taught it something.
  adoptCache(m, cx) {
    const next = sharedCache(this.cache, m.terms.map((x) => x.id), {}, this.store.get(KEYS.cache));
    if (cacheKey(next.cache) === cacheKey(this.cache)) return;
    this.cache = next.cache;
    cx.notify();
  }

  // GET /health at start and every minute: the "CAO unreachable" note works when no fallback fetch runs.
  fetchHealth(cx, m) {
    cx.spawn(async (cx) => {
      this.report(await callCao(exec, this.cfg(m), { path: "/health", maxTime: 4 }));
      cx.notify();
    });
  }

  // Folds the outcome into the view and publishes auth and unreachable once per change, for every mod.
  report(outcome) {
    this.view = applyOutcome(this.view, outcome, Date.now());
    const next = { auth: this.view.auth === true, unreachable: this.view.unreachable === true };
    const was = this.store.get(KEYS.cao);
    if (was?.auth !== next.auth || was?.unreachable !== next.unreachable) this.store.set(KEYS.cao, next);
  }

  // The focused talkable agent counts as seen while this window is active: it is remembered per window
  // and, in write mode, marked with `hc label` (verified by seenVerify; a failed or unseen write switches
  // to the per-window map for the rest of the run).
  watchSeen(m, now) {
    const f = focused()?.terminal;
    if (typeof f !== "string" || window().active !== true) return;
    const t = m.terms.find((x) => x.address === f || String(x.address).endsWith(`/${f}`));
    if (!t?.talkable) return;
    const local = rememberFinishedSeen(m.local, t);
    if (local !== m.local) this.store.set(KEYS.seen, local);
    const p = this.seenState.pending;
    if (p) {
      const live = m.terms.find((x) => x.id === p.id);
      this.seenState = seenVerify(this.seenState, live ? { id: live.id, seen: live.seen } : null, now);
      return;
    }
    const cmd = seenWrite(t, t.address, true, m.machine, this.seenState.mode);
    if (!cmd) return;
    this.seenState = { ...this.seenState, pending: { id: t.id, turn: t.turn, at: now } };
    const mine = this.seenState.pending;
    // A failure counts only for the write that failed: an older call returning after a newer write began must not end that one.
    const failed = () => { if (this.seenState.pending === mine) this.seenState = seenVerify(this.seenState, { failed: true }, Date.now()); };
    hc(cmd).then((r) => { if (r?.code !== 0) failed(); }, failed);
  }

  // The one writer of the selected session.
  selectSession(name) {
    this.store.set(KEYS.selection, { kind: "session", name });
  }

  // A click or Enter on a session line: its first talkable agent gets the pane, and the session is selected.
  openRow(s) {
    const t = s.talkable[0] ?? s.terms[0];
    if (t) focusTerminal(t.address);
    this.selectSession(s.name);
  }

  // The header "+" opens the New session dialog, unless CAO wants auth (no write is tried).
  newSession() {
    if (writesDisabled(this.store.get(KEYS.cao))) return;
    openDialog({ kind: "new-session" });
  }

  clearFilter() {
    this.store.set(KEYS.filter, null);
  }

  // "Started by schedule X": opens the right sidebar at that schedule. The top bar's tick shows the block.
  openFlow(name) {
    const state = this.store.get(KEYS.rside);
    this.store.set(KEYS.rside, rsideStep(state, { type: "link", list: "flows", item: name }));
  }

  // Enter on a row: a session line selects its session and focuses its agent, and an agent row focuses its
  // terminal (the selected session stays).
  openItem(item, m) {
    if (item.section === "sessions") {
      const s = m.sessions.find((x) => x.name === item.id);
      if (s) this.openRow(s);
    } else if (item.section === "agents") {
      const t = m.terms.find((x) => x.id === item.id);
      if (t) focusTerminal(t.address);
    }
  }

  togglePin(id) {
    const pins = listOf(this.store.get(KEYS.pins));
    this.store.set(KEYS.pins, pins.includes(id) ? pins.filter((x) => x !== id) : [...pins, id]);
  }

  // ---------- fold: the block's height lives in the layout, which Herdr saves ----------

  isFolded(section) {
    const px = shownBlock(layout(), BLOCKS[section].id)?.size?.px;
    return typeof px === "number" && px <= COLLAPSED_MAX_PX;
  }

  setFold(section, folded) {
    if (this.isFolded(section) === folded) return;
    const block = BLOCKS[section];
    const key = `caowin.fold.${section}`;
    if (folded) {
      if (!block.open.weight && this.height > COLLAPSED_MAX_PX) this.store.setLocal(key, Math.round(this.height));
      run("layout.resize", { id: block.id, px: COLLAPSED_PX });
      return;
    }
    const saved = Number(this.store.getLocal(key));
    run("layout.resize", { id: block.id, ...(block.open.weight ? block.open : { px: saved > COLLAPSED_MAX_PX ? saved : block.open.px }) });
  }

  onKey(e, cx) {
    const m = e.modifiers ?? {};
    if (m.control || m.alt || m.platform || m.function) return;
    const index = Math.min(this.nav.index, Math.max(this.items.length - 1, 0));
    const step = navStep({ items: this.items, index, folded: { [this.section]: this.isFolded(this.section) } }, String(e.key ?? "").toLowerCase());
    this.nav = { index: step.index, active: true };
    const a = step.action;
    const item = a && this.items[step.index];
    if (a?.type === "fold") this.setFold(a.section, a.folded);
    else if (a?.type === "open" && item) this.openItem(item, this.model());
    else if (a?.type === "pin") this.togglePin(a.id);
    cx.notify();
  }

  // ---------- drawing ----------

  // The title, its tooltip and the folded summary of this block's header, and whether it carries the filter's clear control.
  // Sessions: `Sessions` (or `Sessions M of S . filter: <name>` while a filter is on, shortened by sessionsHeader to what fits
  // the block's width, the whole text in the tooltip) and its count when folded;
  // Agents: `Agents . <session>` and the session's name or "none selected" when folded.
  headParts(m, folded, ag) {
    if (this.section === "sessions") {
      const filter = this.store.get(KEYS.filter) ?? null;
      const head = sessionsHeader(m.sessions, filterSessions(m.sessions, filter, m.replied), filter, this.width);
      return folded ? { title: "Sessions", tip: "Sessions", summary: foldSummary("sessions", m.sessions), clearable: false } : { title: head.text, tip: head.full, summary: "", clearable: head.clearable };
    }
    const title = folded ? "Agents" : ag.head.title;
    return { title, tip: title, summary: folded ? foldSummary("agents", ag.name) : "", clearable: false };
  }

  headerView(t, m, folded, ag) {
    const cao = this.store.get(KEYS.cao);
    const off = writesDisabled(cao, this.view);
    const head = this.headParts(m, folded, ag);
    const plus = this.section === "agents" ? null : h_flex().id(`cao-plus-${this.section}`).w(22).h(22).flex_none().items_center().justify_center().rounded(6)   // fixed: a control with one character
      .bg(t.surface).text_color(off ? t.dim : t.foreground).when(off, (e) => e.opacity(0.5))
      .when(!off, (e) => e.cursor_pointer().hover((s) => s.bg(t.hover))
        .on_click((_e, cx) => { cx.stop_propagation(); this.newSession(); cx.notify(); }))
      .child("+");
    return h_flex().id(`cao-head-${this.section}`).min_h(COLLAPSED_PX - 1).flex_none().px(12).py(4).gap(6).items_center()
      .cursor_pointer().on_click((_e, cx) => { this.setFold(this.section, !folded); cx.notify(); })
      .child(div().w(12).flex_none().text_color(t.dim).child(folded ? "\u25b8" : "\u25be"))   // fixed: the fold arrow
      .child(div().id(`cao-title-${this.section}`).min_w(0).truncate().text_size(11.5).font_medium().text_color(t.dim).tooltip(head.tip).child(head.title))   // truncates: the title with the session's name; the tooltip has it whole
      .child(div().id(`cao-summary-${this.section}`).flex_1().min_w(0).truncate().text_size(11).text_color(t.dim).tooltip(head.summary).child(head.summary))   // truncates: the folded summary, a session's name; the tooltip has it whole
      .when(head.clearable, (e) => e.child(h_flex().id("cao-clear-filter-head").px(7).py(2).flex_none().items_center().rounded(4).bg(t.surface)   // px and py 1 px more than the old outline took
        .text_size(11).text_color(t.foreground).cursor_pointer().hover((st) => st.bg(t.hover))
        .on_click((_e, cx) => { cx.stop_propagation(); this.clearFilter(); cx.notify(); })
        .child("clear")))
      .when(Boolean(plus), (e) => e.child(plus));
  }

  // The lines under the header: what is wrong, in the words of lib/logic.js and lib/cao.js.
  noticeLines(t, m) {
    const cao = this.store.get(KEYS.cao);
    if (this.section === "agents") return [this.store.notice(), dialogBusyNotice()].filter(Boolean).map((line) => div().flex_none().px(12).pb(4).text_size(11).text_color(t.blocked).child(line));
    const note = headerNote({ connected: m.connected, unreachable: cao?.unreachable === true });
    return [cao?.auth === true ? AUTH_MESSAGE : null, note, this.store.notice(), dialogBusyNotice()].filter(Boolean)
      .map((line) => div().flex_none().px(12).pb(4).text_size(11).text_color(t.blocked).child(line));
  }

  renderBody(t, m, ag) {
    if (this.section === "sessions") return this.sessionsBody(t, m);
    return this.agentsBody(t, m, ag);
  }

  // The colour name of every session's badge: Herdr's spaces rule (badgeColors, lib/agents.js) over all the sessions, not only the ones
  // the filter lists, kept under KEYS.colors and written when it changes, as Herdr's spaces mod does in its render.
  badgeColours(m) {
    const next = badgeColors(this.store.get(KEYS.colors), m.sessions.map((s) => s.name));
    if (next.changed) this.store.set(KEYS.colors, next.colors);
    return next.colors;
  }

  // One line per session. A filter that matches nothing leaves a line that clears it.
  sessionsBody(t, m) {
    const now = Date.now();
    const colors = this.badgeColours(m);
    const filter = this.store.get(KEYS.filter) ?? null;
    const sessions = filterSessions(m.sessions, filter, m.replied);
    return v_flex().id("cao-sessions-list").flex_1().min_h(0).overflow_y_scrollbar().p(6).gap(2)
      .when(m.dimmed, (e) => e.opacity(0.5))
      .children(sessions.map((s) => this.sessionLineView(s, m, t, now, colors)))
      .when(sessions.length === 0 && filter === null, (e) => e.child(div().px(16).py(20).text_size(12).text_center().text_color(t.dim).child("No CAO sessions")))
      // A count that falls to zero leaves the status bar, so the filter must be clearable here too.
      .when(sessions.length === 0 && filter !== null, (e) => e.child(h_flex().id("cao-clear-filter").px(16).py(20).justify_center()
        .text_size(12).text_color(t.dim).cursor_pointer().on_click(() => this.clearFilter())
        .child("No session matches this filter. Click to clear it.")));
  }

  // One session as one line, styled like a row of Herdr's spaces list: its letter badge, its name, the "draft" tag, the
  // "stuck?" hint with its label, and at the right end the status shape of the first talkable agent, with an extra mark before it when the
  // session needs you or has a reply the shape does not show (a shape and a colour, no word). The name gives way to the rest;
  // the words of the state are the tooltip.
  sessionLineView(s, m, t, now, colors) {
    const quiet = quietHint(s, this.obs, m.props?.quietMinutes ?? 10, now);
    const line = sessionLine(s, { replied: m.replied, quiet, colors });
    const index = this.items.length;
    this.items.push({ kind: "row", section: "sessions", id: s.name, worker: false });
    const selected = sessionSelection(this.store.get(KEYS.selection))?.name === s.name;
    return h_flex().id(`cao-s-${s.name}`).py(5).px(8).gap(9).items_center().rounded(6)
      .when(selected, (e) => e.bg(t.selected)).when(!selected && this.nav.active && this.nav.index === index, (e) => e.bg(t.hover))
      .cursor_pointer().hover((st) => st.bg(t.hover)).tooltip(line.tip)
      .on_click(() => { this.nav = { index, active: false }; this.openRow(s); })
      .child(sessionBadge(line.badge, badgeFill(line.badge.color, t)))
      .child(div().flex_1().min_w(0).truncate().child(line.name))   // truncates: the session's name; the tooltip has it whole
      .when(line.draft, (e) => e.child(whole("draft", t.muted)))
      .when(line.hint !== null, (e) => e.child(h_flex().id(`cao-stuck-${s.name}`).flex_none().gap(4).items_center().cursor_pointer()
        .on_click((_e, cx) => { cx.stop_propagation(); this.selectSession(s.name); cx.notify(); })
        .child(whole(line.hint.text, t.ansi1)).child(whole(line.hint.label, t.dim))))
      .when(line.extra !== null, (e) => e.child(shapeMark(line.extra, t)))
      .when(line.shape !== null, (e) => e.child(shapeMark(line.shape, t)));
  }

  // What the Agents block shows: the selected session (or the stored selection naming a session the list no longer
  // has) and its header from agentsHeader. The recap and the context percentage
  // come from the screen read of the talkable root, shown only while that root is still at the turn it was read at.
  agentsModel(m) {
    const sel = sessionSelection(this.store.get(KEYS.selection));
    const found = sel ? m.sessions.find((x) => x.name === sel.name) : undefined;
    const root = found?.talkable[0];
    const read = root ? readOfThisTurn(root, this.reads[root.id]) : null;
    const head = agentsHeader(found ?? sel, listOf(this.store.get(KEYS.flows)), m.props, { text: read?.text });
    return { name: sel?.name ?? null, found, head };
  }

  // The recap, the one multi-line text of the left sidebar: its honesty label on a line of its own above, never clipped, then
  // the text wrapped to at most three lines at the size of the agent rows, with an ellipsis after the third (goalLines, a pure clamp: the
  // toolkit has no line clamp). The whole text is the tooltip. Every row of the sidebar stays one line.
  recapView(t, recap) {
    return v_flex().id("cao-tg-recap").px(8).gap(2).tooltip(recap.text)
      .child(whole(recap.label, t.dim))
      .children(goalLines(recap.text, this.width).map((line, i) => div().id(`cao-tx-recap-${i}`).min_w(0).truncate().text_size(12.5).child(line)));   // truncates: a line of the recap that the clamp already made fit; the tooltip has it whole
  }

  // One header line of text and its honesty label (the context percentage). The text gives way (an ellipsis, its words in the tooltip) and the
  // label never does.
  tagged(t, text, label) {
    const note = whole(label, t.dim);
    const key = label.replace(/\W+/g, "-");
    return h_flex().id(`cao-tg-${key}`).px(8).gap(6).items_center()
      .child(div().id(`cao-tx-${key}`).flex_1().min_w(0).truncate().text_size(12).tooltip(text).child(text))   // truncates: the context line's text; the tooltip has it whole
      .child(note);
  }

  // The selected session's header lines, then its agent tree in the order of who started whom.
  agentsBody(t, m, ag) {
    const now = Date.now();
    this.paintedAt = now;
    const { head, found } = ag;
    const tree = found ? agentRows(found, m.pins, { now, replied: m.replied, local: m.local }) : { rows: [] };
    this.paintedFocus = focusKey();
    const focus = focusedRow(tree, focused());   // which row holds the terminal Herdr has focused
    const flowText = head.flow === null ? "" : head.flow.schedule ? `${head.flow.text} \u00b7 ${head.flow.schedule}` : head.flow.text;
    return v_flex().id("cao-agents-list").flex_1().min_h(0).overflow_y_scrollbar().p(6).gap(2)
      .when(m.dimmed, (e) => e.opacity(0.5))
      .when(head.note !== null, (e) => e.child(div().id("cao-d-note").px(8).py(4).truncate().text_size(12).text_color(t.dim).tooltip(head.note).child(head.note)))   // truncates: the note about the selected session; the tooltip has it whole
      // The flow link's label on a line of its own above it, as the recap's: beside the link it took the room and left "Start...".
      .when(head.flow !== null, (e) => e.child(v_flex().id("cao-tg-flow").px(8).gap(2)
        .child(whole(head.flow.label, t.dim))
        .child(h_flex().id("cao-d-flow").items_center().cursor_pointer().tooltip(flowText)
          .on_click((_e, cx) => { this.openFlow(head.flow.flow); cx.notify(); })
          .child(div().flex_1().min_w(0).truncate().text_size(12).text_color(t.accent).child(flowText)))))   // truncates: "Started by schedule X" and its times; the tooltip has them whole
      .when(head.recap !== null, (e) => e.child(this.recapView(t, head.recap)))
      .when(head.context !== null, (e) => e.child(this.tagged(t, `context ${head.context.percent}%`, head.context.label)))
      .children(tree.rows.map((r) => this.agentRowView(t, r, found, focus)));
  }

  // One agent as one line, as a row of Herdr's Agents list: the logo of its program, its name, its best-guess label and
  // parent note, its age, its status shape and, in the 18 px slot at the right end, the pin of a worker, the crown of the session's
  // main agent or nothing (another talkable agent). The indentation shows who started whom. The tooltip
  // says the state in words and the other facts. A click focuses the terminal and leaves the selected session as it is.
  agentRowView(t, r, session, focus) {
    const index = this.items.length;
    this.items.push({ kind: "row", section: "agents", id: r.id, worker: r.worker, session: session.name });
    const isFocus = focus === r.id;
    return h_flex().id(`cao-r-${r.id}`).py(4).pr(8).pl(8 + r.depth * 14).gap(6).items_center().text_size(12.5).rounded(6)
      .when(isFocus, (e) => e.bg(t.selected)).when(!isFocus && this.nav.active && this.nav.index === index, (e) => e.bg(t.hover))
      .cursor_pointer().hover((st) => st.bg(t.hover)).tooltip(r.tip)
      .on_click(() => { this.nav = { index, active: false }; focusTerminal(r.term.address); })
      .child(programMark(r.icon, t, 16, t.foreground))
      .child(div().flex_1().min_w(0).truncate().child(r.name))   // truncates: the agent's name; the tooltip has it whole
      .when(r.label !== null, (e) => e.child(whole(r.label, t.dim)))
      .when(r.parentNote !== null, (e) => e.child(whole(r.parentNote, t.dim)))
      .when(r.age !== null, (e) => e.child(whole(r.age, t.dim)))
      .child(shapeMark(r.shape, t))
      .when(r.slot === "blank", (e) => e.child(div().w(PIN_SLOT).flex_none()))
      .when(r.slot === "crown", (e) => e.child(h_flex().w(PIN_SLOT).flex_none().items_center().justify_center().child(crownIcon(CROWN_MARK, t))))
      .when(r.slot === "pin", (e) => e.child(h_flex().id(`cao-pin-${r.id}`).p(2).flex_none().items_center().rounded(4)
        .cursor_pointer().hover((st) => st.bg(t.hover)).tooltip(r.pinTip)
        .on_click((_e, cx) => { cx.stop_propagation(); this.togglePin(r.id); cx.notify(); })
        .child(pinIcon(pinMark(r.pinned), t))));
  }

  render(cx) {
    const t = theme();
    const m = this.model();
    const folded = this.isFolded(this.section);
    this.items = [{ kind: "header", section: this.section }];
    const ag = this.section === "agents" ? this.agentsModel(m) : null;
    const root = v_flex().id(`cao-side-${this.section}`).size_full().min_h(0).overflow_y_scrollbar().bg(t.chrome)
      .border_t(1).border_color(t.border).text_color(t.foreground).text_size(13)
      .track_focus(this.keys).on_key_down((e, cx) => this.onKey(e, cx)).on_click(() => this.keys.focus())
      .on_size_change((size, cx) => {
        const width = size?.width ?? null;
        this.height = size?.height ?? null;
        if (width !== this.width) { this.width = width; cx?.notify(); }   // the recap's lines follow a drag of the sidebar's edge
      })
      .child(this.headerView(t, m, folded, ag));
    if (folded) return root;
    const body = this.renderBody(t, m, ag);
    return root.children(this.noticeLines(t, m)).when(Boolean(body), (e) => e.child(body));
  }
}
