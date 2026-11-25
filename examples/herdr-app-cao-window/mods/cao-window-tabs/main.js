// The top bar: one 34 px block above the pane, always drawn, and the window's drag strip. Left: the
// agent tabs of the focused session (its talkable agents plus pinned workers, tabViews), one tab for one shown agent
// and none when no agent is shown, each named as CAO names the agent's tab (agentName), the conductor like the rest and with no tag of
// its role; a tab runs agent.focus. The strip scrolls sideways when the tabs are more than fit, as Herdr's own tabs do, and keeps the
// focused tab in view (the mechanism is copied from mods/tabs/main.js of Herdr, lines cited at each part below). Right, outside
// the scrolling strip so they never move: a thin divider, then the Flows and Workflows buttons, bare words with no count (barButtonLabel;
// the lists' own headers carry the counts).
// A press opens, switches or closes the right sidebar through the pure reducer rsideStep, and the tick keeps the
// sidebar's record and the block's real state in step (rsideLayoutStep). This is the one mod that sends
// layout.show and layout.hide, and the bar is never resized.
//
// This mod also polls the two lists, whether or not the sidebar is open, so what the lists show is never blind: GET /flows
// every 30 s, GET /workflows with its runs every 60 s (5 s while a run is running), and again right after a
// write moved a stamp. It publishes what the right sidebar draws (listView) and the trimmed flow list.
// Every CAO call goes through callCao; the pollers only read.
import { View, div, svg } from "gpui-kit";
import { h_flex, ScrollHandle } from "gpui-base";
import { exec, spaces, terminals, focused, layout, run, theme, props, shared, setShared, checking } from "herdr";
import { callCao } from "./cao.js";
import { createStore } from "./store.js";
import { programMark, crownIcon, focusTerminal } from "./ui.js";
import { tabViews, CROWN_MARK } from "./agents.js";
import { buildModel, shownBlock, applyOutcome } from "./logic.js";
import { flowRows, publishedFlows, flowsPollDue } from "./flows.js";
import { workflowsPollDue, WORKFLOW_RUNS_LIMIT } from "./workflows-view.js";
import { barButtonLabel, rsideStep, rsideLayoutStep, initialLayoutState, listView, initialSwipe, wheelTabStep } from "./shell.js";

const RSIDE_BLOCK = "cao-window-panel";
// The icons of the two buttons: drawings of this mod's own folder (24 grid, outline in the text colour, as Herdr's file.svg and
// changes.svg), found by `mods/<mod>/<file>` as the side mod finds its pin. Flows: a stopwatch (a prompt on a schedule). Workflows: a fork.
const BAR_ICONS = { flows: "mods/cao-window-tabs/flows.svg", workflows: "mods/cao-window-tabs/workflows.svg" };
const KEYS = {
  rside: "caowin.rside", flows: "caowin.flows", flowsView: "caowin.flowsView", workflowsView: "caowin.workflowsView",
  flowsStamp: "caowin.flowsStamp", workflowsStamp: "caowin.workflowsStamp",
};
const TICK_MS = 500;
const listOf = (v) => (Array.isArray(v) ? v : []);

export default class CaoTabs extends View {
  init(_initial, cx) {
    this.store = createStore({ getShared: shared, setShared, local: localStorage });
    this.scroll = ScrollHandle.new();               // the strip's scroll position (Herdr's tabs mod, mods/tabs/main.js:33)
    this.swipe = initialSwipe();                    // the wheel gesture in progress (wheelTabStep; Herdr :37)
    this.layoutState = initialLayoutState();        // the retry state of rsideLayoutStep
    this.flowsView = {};                            // GET /flows, folded by applyOutcome
    this.flowsAt = null;                            // when the last GET /flows started (a failure waits the full period too)
    this.flowsBusy = false;
    this.flowsSeen = this.store.get(KEYS.flowsStamp);
    this.wfView = {};                               // GET /workflows
    this.runsView = {};                             // GET /workflows/runs?limit=20
    this.wfAt = null;
    this.wfBusy = false;
    this.wfSeen = this.store.get(KEYS.workflowsStamp);
    if (checking()) return;                         // a check renders once: no timers, no exec
    cx.timer.every(TICK_MS, (cx) => this.tick(cx));
  }

  // The focused session's shown agents, the address of the focused terminal and the index of the focused tab (-1 when none of the tabs is).
  shown() {
    const saved = shared("caowin.cache");
    const m = buildModel(terminals() ?? [], spaces(), props(), {
      cache: saved && typeof saved === "object" && !Array.isArray(saved) ? saved : {},
      drafts: listOf(shared("caowin.drafts")), pins: listOf(shared("caowin.pins")), local: shared("caowin.seen") ?? {},
    });
    const f = focused()?.terminal;
    const t = typeof f === "string" ? m.terms.find((x) => x.address === f || String(x.address).endsWith(`/${f}`)) : undefined;
    const s = t && m.sessions.find((x) => x.name === t.session);
    const tabs = s ? tabViews(s, m.pins, { replied: m.replied }) : [];
    return { m, f: t?.address, tabs, active: tabs.findIndex((x) => x.term.address === t?.address) };
  }

  cfg() {
    const m = buildModel(terminals() ?? [], spaces(), props(), {});
    return { machine: m.machine, port: props()?.caoPort };
  }

  tick(cx) {
    this.syncLayout();
    this.pollFlows(cx);
    this.pollWorkflows(cx);
  }

  // ---------- the right sidebar's record against the block ----------

  // One step of rsideLayoutStep: the first call of a run hides the block and closes the record; later calls send
  // the missing show or hide, once more after a few ticks, and then take the block's real state as the truth.
  syncLayout() {
    const tree = layout();
    const shown = tree !== null && typeof tree === "object" ? shownBlock(tree, RSIDE_BLOCK) !== null : undefined;
    const step = rsideLayoutStep(this.store.get(KEYS.rside), shown, this.layoutState);
    this.layoutState = step.state;
    if (JSON.stringify(step.rside) !== JSON.stringify(this.store.get(KEYS.rside))) this.store.set(KEYS.rside, step.rside);
    this.applyLayout(step.send);
  }

  // A turn of the wheel over the bar: one tab back or forward by Herdr's rule (wheelTabStep), the neighbour focused as a click on it does.
  // The strip scrolls the new focused tab into view by itself (render). No neighbour, nothing.
  wheel(e, ctx) {
    const r = wheelTabStep(this.swipe, e, Date.now());
    this.swipe = r.swipe;
    const next = r.step === 0 || ctx.active < 0 ? undefined : ctx.tabs[ctx.active + r.step];
    if (next !== undefined) focusTerminal(next.term.address);
  }

  // The only place that shows or hides the block, and only with what a step returned.
  applyLayout(send) {
    if (send === "show") run("layout.show", { id: RSIDE_BLOCK });
    else if (send === "hide") run("layout.hide", { id: RSIDE_BLOCK });
  }

  // A press on a button. The first sync comes first, so a press before the first tick is not undone by the
  // hide of a run's first call.
  press(list) {
    this.syncLayout();
    this.store.set(KEYS.rside, rsideStep(this.store.get(KEYS.rside), { type: "button", list }));
    this.syncLayout();
  }

  // ---------- the two list pollers (always: the counts do not wait for the sidebar) ----------

  publish(key, value) {
    if (JSON.stringify(value) !== JSON.stringify(this.store.get(key))) this.store.set(key, value);
  }

  pollFlows(cx) {
    const stamp = this.store.get(KEYS.flowsStamp);
    if (!flowsPollDue({ shown: true, inFlight: this.flowsBusy, lastAt: this.flowsAt, stamp, seenStamp: this.flowsSeen }, Date.now())) return;
    this.flowsSeen = stamp;
    this.flowsAt = Date.now();
    this.flowsBusy = true;
    cx.spawn(async (cx2) => {
      try {
        const outcome = await callCao(exec, this.cfg(), { path: "/flows", maxTime: 8 });
        this.flowsView = applyOutcome(this.flowsView, outcome, Date.now());
        if (outcome.kind === "ok") this.publish(KEYS.flows, publishedFlows(flowRows(outcome.json)));
        this.publish(KEYS.flowsView, listView("flows", this.flowsView));
      } finally {
        this.flowsBusy = false;
        cx2.notify();
      }
    });
  }

  pollWorkflows(cx) {
    const stamp = this.store.get(KEYS.workflowsStamp);
    if (!workflowsPollDue({ shown: true, inFlight: this.wfBusy, lastAt: this.wfAt, runs: this.runsView.data, stamp, seenStamp: this.wfSeen }, Date.now())) return;
    this.wfSeen = stamp;
    this.wfAt = Date.now();
    this.wfBusy = true;
    cx.spawn(async (cx2) => {
      try {
        const cfg = this.cfg();
        const listed = await callCao(exec, cfg, { path: "/workflows", maxTime: 8 });
        this.wfView = applyOutcome(this.wfView, listed, Date.now());
        if (listed.kind !== "unreachable") {         // the runs read would only fail the same way
          const runs = await callCao(exec, cfg, { path: "/workflows/runs", query: { limit: WORKFLOW_RUNS_LIMIT }, maxTime: 8 });
          this.runsView = applyOutcome(this.runsView, runs, Date.now());
        }
        this.publish(KEYS.workflowsView, listView("workflows", this.wfView, this.runsView));
      } finally {
        this.wfBusy = false;
        cx2.notify();
      }
    });
  }

  // ---------- drawing ----------

  // One agent tab: the logo of its program (as Herdr's own tabs draw it, 14 px muted), its name and, for the session's main agent, a crown, from tabViews (the name an Agents row shows, the conductor's like a worker's, no tag of
  // the role). No state mark: the Agents row and the Sessions line draw the state, and the tab's tooltip, its row's, says it in words.
  // The press is stopped, so it does not start a window drag and a double-click does not maximize.
  // A tab at or before the focused one scrolls it back into view when its own size changes (mods/tabs/main.js:110: a rename or a mark on
  // an earlier tab can push the focused one out).
  tabView(t, v, ctx, i) {
    return h_flex().id(`cao-tab-${v.id}`).py(4).px(10).gap(6).items_center().flex_none().rounded(6).cursor_pointer()
      .when(i <= ctx.active, (e) => e.on_size_change((_size, cx) => { this.scroll.scroll_to_item(ctx.active); cx.notify(); }))
      .when(v.term.address === ctx.f, (e) => e.bg(t.selected)).hover((s) => s.bg(t.hover))
      .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
      .on_click(() => focusTerminal(v.term.address)).tooltip(v.tip)
      .child(programMark(v.icon, t, 14, t.muted))
      .child(div().text_size(12).child(v.name))
      .when(v.main, (e) => e.child(crownIcon(CROWN_MARK, t)));
  }

  // The Flows or Workflows button, drawn as Herdr's own Files and Changes buttons (`panelButton`, mods/tabs/main.js:133-152 of the
  // shipped tabs mod): a 26 px row, px 8, gap 5, rounded 6, text 12, a 14 px icon before the label; the chrome and the muted text at
  // rest, the selected fill and the foreground text while its list is the open one, the hover fill. The press is stopped as on a tab.
  barButton(t, id, icon, label, active, onPress) {
    return h_flex().id(id).h(26).px(8).gap(5).flex_none().items_center().rounded(6)   // fixed: Herdr's panelButton height; one short word and an icon
      .bg(active ? t.selected : t.chrome).text_size(12).text_color(active ? t.foreground : t.muted)
      .cursor_pointer().hover((s) => s.bg(t.hover))
      .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
      .on_click((_e, cx) => { onPress(); cx.notify(); })
      .child(svg(icon).size(14).flex_none())
      .child(label);
  }

  render() {
    const t = theme();
    const ctx = this.shown();
    const rside = this.store.get(KEYS.rside);
    const open = rside?.open === true ? rside.list : null;
    const selected = ctx.tabs[ctx.active]?.id;
    if (selected !== this.selected || ctx.active !== this.selectedIndex) {      // the focused tab moved: bring it into view (mods/tabs/main.js:69-73)
      this.selected = selected;
      this.selectedIndex = ctx.active;
      if (ctx.active >= 0) this.scroll.scroll_to_item(ctx.active);
    }
    // The strip is Herdr's `tab-scroll` (mods/tabs/main.js:156-158): it takes the room the fixed items leave (flex_1, min_w 0), scrolls
    // sideways with a scrollbar tracked by `this.scroll`, and scrolls the focused tab into view when its own size changes. The wheel handler
    // sits on the bar (:155). The divider and the two buttons are the strip's siblings, so they stay at the right and never scroll.
    return h_flex().id("cao-tabs").size_full().px(8).gap(4).items_center().bg(t.chrome)
      .on_scroll_wheel((e) => this.wheel(e, ctx))
      .child(h_flex().id("cao-tab-scroll").flex_1().min_w(0).h_full().items_center().gap(4).overflow_x_scrollbar().track_scroll(this.scroll)
        .on_size_change((_size, cx) => { if (ctx.active >= 0) { this.scroll.scroll_to_item(ctx.active); cx.notify(); } })
        .children(ctx.tabs.map((x, i) => this.tabView(t, x, ctx, i))))
      .child(div().id("cao-tab-divider").w(1).h(16).flex_none().mx(4).bg(t.border))   // fixed: a 1 px rule between the strip and the buttons, no text
      .child(this.barButton(t, "cao-bar-flows", BAR_ICONS.flows, barButtonLabel("flows"), open === "flows", () => this.press("flows")))
      .child(this.barButton(t, "cao-bar-workflows", BAR_ICONS.workflows, barButtonLabel("workflows"), open === "workflows", () => this.press("workflows")));
  }
}
