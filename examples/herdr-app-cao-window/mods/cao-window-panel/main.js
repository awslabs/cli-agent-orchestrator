// The panel mod: the exported View, which picks one of two hosts from props().host and hands its render and
// close to it. The block `cao-window-panel` of the layout is the right sidebar (sidebar.js:
// the flows and workflows lists, the details of one item, the inline confirms); an overlay opened with
// host "dialog" is the dialog (dialog.js: the frame, its keys and its lease, every form, and the bar's help).
// Nothing here reads caowin.selection to decide what to draw. The hosts take this instance and use only its
// store, its views, its keys, and the three methods below (model, cfg, gate).
import { View } from "gpui-kit";
import { machines, spaces, terminals, props, shared, setShared } from "herdr";
import { createStore } from "./store.js";
import { buildModel } from "./logic.js";
import { panelGate } from "./flows.js";
import { initSidebarHost, renderSidebarHost } from "./sidebar.js";
import { initDialogHost, renderDialogHost, closeDialogHost } from "./dialog.js";

const listOf = (v) => (Array.isArray(v) ? v : []);

// The two hosts of the panel: the right sidebar and the dialog, each a set of plain functions that take the panel.
const SIDEBAR_HOST = { init: initSidebarHost, render: renderSidebarHost };
const DIALOG_HOST = { init: initDialogHost, render: renderDialogHost, close: closeDialogHost };

export default class CaoPanel extends View {
  init(_initial, cx) {
    this.store = createStore({ getShared: shared, setShared, local: localStorage });
    this.views = {};                 // callCao outcomes folded by applyOutcome: the details reads, profiles, providers
    this.keys = cx.focus_handle();
    this.host = props()?.host === "dialog" ? DIALOG_HOST : SIDEBAR_HOST;
    this.host.init(this, cx);
  }

  render() { return this.host.render(this); }

  // The overlay goes (or the window quits): the host gets one last call, as AGENTS.md describes for a View's close.
  async close(cx) { await this.host.close?.(this, cx); }

  model() {
    const saved = this.store.get("caowin.cache");
    return buildModel(terminals() ?? [], spaces(), props(), {
      cache: saved && typeof saved === "object" && !Array.isArray(saved) ? saved : {},
      drafts: listOf(this.store.get("caowin.drafts")), pins: listOf(this.store.get("caowin.pins")),
      local: this.store.get("caowin.seen") ?? {},
    });
  }

  cfg(m) { return { machine: m.machine, port: m.props?.caoPort }; }

  // Whether a write control works (panelGate): auth comes from the views of reads (and the one the Sessions
  // block publishes), so a write's own 401 shows its message without switching its control off for good; not
  // while CAO is unreachable, nor while the machine is not connected.
  gate(m, { retry = false } = {}) {
    const connected = machines().some((x) => x.name === m.machine && x.layout !== "unavailable");
    return panelGate(this.views, this.store.get("caowin.cao"), { retry, connected });
  }
}
