// The panel mod's right-sidebar host. main.js picks it when props().host is not "dialog": the block
// `cao-window-panel`, hidden at first, shown and hidden only by the top bar (this file sends no layout command
// at all). What it shows is the pure state `caowin.rside`, changed only through rsideStep: the list of flows or
// workflows, or the details of one item under a back arrow.
//
// The top bar polls both lists whether or not this block is shown and publishes what the rows draw
// (`caowin.flowsView`, `caowin.workflowsView`), so this host starts no poll of the lists. It fetches the details
// of the one item it shows (GET /flows/{name} and the next runs, or the workflow's row, runs, flows and spec), only
// while the block is shown, and again right after a write moves a stamp. Every CAO call goes through callCao.
//
// A form is never drawn here: "+", Edit, Run and Schedule... call openDialog (lib/ui.js). The small confirms
// (Enable, Disable, Run now, Cancel run, Resume) are cards inside the sidebar, scoped to the item they were opened
// for (confirmScope), and a confirmed write belongs to its item: its answer is kept in `sb.ops` (opOf) and shows
// when that item's details are shown, whatever is shown by then.
//
// The functions take the panel instance (the exported View of main.js). The host imports are aliased with the
// sb prefix because bundle mode puts the shared modules and every file of the mod in one scope.
import { div as sbDiv } from "gpui-kit";
import { v_flex as sbCol, h_flex as sbRow } from "gpui-base";
import { exec as sbExec, layout as sbLayout, theme as sbTheme, checking as sbChecking } from "herdr";
import { callCao, AUTH_MESSAGE } from "./cao.js";
import { shapeMark, openDialog, dialogBusyNotice } from "./ui.js";
import { shownBlock, applyOutcome, repaintDue, ageWords, dialogKey, statusShape, displayState, sectionSummary } from "./logic.js";
import {
  flowRoute, flowsPollDue, fieldsOfFlow, validateBody, validateResult, flowDetailsView, flowDialogSpec, confirmSpec, writeOutcome, flowRowState, flowSessionWorking, flowsSummary,
} from "./flows.js";
import {
  workflowDetailsView, workflowDetailPlan, detailReads, detailCall, detailView, foldDetailRead, combinedStamp, workflowsNotes, editFromDocument, runDialogSpec, runWriteCheck, runWriteOutcome, runRoute,
} from "./workflows-view.js";
import { rsideStep, confirmScope, scopeOf, opOf, approvalRow } from "./shell.js";

const SB_BLOCK = "cao-window-panel";
const SB_KEYS = {
  rside: "caowin.rside", selection: "caowin.selection", flowsView: "caowin.flowsView", workflowsView: "caowin.workflowsView",
  approvals: "caowin.approvals", flowsStamp: "caowin.flowsStamp", workflowsStamp: "caowin.workflowsStamp",
};
const SB_TICK_MS = 1000;
const SB_REPAINT_MS = 30_000;       // the ages in the notes and the details; a repaint redraws the whole window, so rarely
// minimal: the list payloads do not carry the read's error, so a list is called stale by its age: three poll
// periods (30 s for flows, 60 s for workflows) without a good read. Upgrade path: carry the error in listView.
const SB_STALE_MS = { flows: 90_000, workflows: 180_000 };
const SB_OPS_MAX = 50;
// What the person reads is "schedule"; CAO's own word for it, in its routes (/flows), its sessions (cao-flow-<name>) and its docs, is "flow".
const SB_SCHEDULE_HELP = "A schedule is a CAO flow: a prompt, or a workflow, that CAO's scheduler runs on a cron schedule.";
const sbList = (v) => (Array.isArray(v) ? v : []);

// The sidebar's state on the panel: the details read for each item shown so far (a Map by name, so an answer for an
// item no longer shown lands on its own object), the confirmed writes' answers, the confirm that is open, and the
// item whose prompt is open.
export function initSidebarHost(panel, cx) {
  panel.sb = { fds: new Map(), wds: new Map(), ops: [], confirm: null, prompt: null, paintedAt: Date.now() };
  if (sbChecking()) return;                       // a check renders once: no timers, no exec
  cx.timer.every(SB_TICK_MS, (cx2) => tickSidebar(panel, cx2));
}

// What the right sidebar shows now, read as a state of its shape whatever was stored.
const sbRside = (panel) => rsideStep(panel.store.get(SB_KEYS.rside), { type: "read" });
const sbSend = (panel, event) => panel.store.set(SB_KEYS.rside, rsideStep(panel.store.get(SB_KEYS.rside), event));

// A confirm that is open for an item that is not the one shown is dropped and sends nothing.
function sbDropStaleConfirm(panel) {
  const sb = panel.sb;
  if (sb.confirm !== null && sb.confirm.scope !== confirmScope(sbRside(panel))) { sb.confirm = null; return true; }
  return false;
}

// The answer of a confirmed write for one item: the latest entry replaces the item's earlier one.
function sbSetOp(sb, entry) {
  sb.ops = [...sb.ops.filter((o) => o.item !== entry.item), entry].slice(-SB_OPS_MAX);
}

// ---------- reading the details of the item shown ----------

function sbFreshFd(name, stamp) {
  return { name, inFlight: false, at: null, seenStamp: stamp, flow: null, checks: null, status: null, error: null };
}
function sbFreshWd(name, stamp) {
  return { name, inFlight: false, at: null, flowsAt: null, specOk: false, specState: "loading", seenStamp: stamp, row: undefined, runs: null, flows: [], spec: null, error: null };
}
const sbWorkflowStamp = (panel) => combinedStamp(panel.store.get(SB_KEYS.workflowsStamp), panel.store.get(SB_KEYS.flowsStamp));

// One tick: drop a stale confirm, forget a closed prompt, and, only while the block is shown, read the details of
// the item that is shown (reads only: a tick never writes).
function tickSidebar(panel, cx) {
  const sb = panel.sb;
  const rside = sbRside(panel);
  if (sbDropStaleConfirm(panel)) cx.notify();
  if (sb.prompt !== null && sb.prompt.name !== rside.item) sb.prompt = null;
  if (!rside.open || rside.item === null || shownBlock(sbLayout(), SB_BLOCK) === null) return;
  const now = Date.now();
  if (rside.list === "flows") {
    let fd = sb.fds.get(rside.item);
    if (!fd) { fd = sbFreshFd(rside.item, panel.store.get(SB_KEYS.flowsStamp)); sb.fds.set(rside.item, fd); }
    const stamp = panel.store.get(SB_KEYS.flowsStamp);
    if (flowsPollDue({ shown: true, inFlight: fd.inFlight, lastAt: fd.at, stamp, seenStamp: fd.seenStamp }, now)) sbFetchFlow(panel, cx, panel.model(), fd);
  } else {
    let wd = sb.wds.get(rside.item);
    if (!wd) { wd = sbFreshWd(rside.item, sbWorkflowStamp(panel)); sb.wds.set(rside.item, wd); }
    const plan = workflowDetailPlan({
      shown: true, inFlight: wd.inFlight, lastAt: wd.at, runs: wd.runs, flowsAt: wd.flowsAt, specOk: wd.specOk, stamp: sbWorkflowStamp(panel), seenStamp: wd.seenStamp,
    }, now);
    if (plan.poll) sbFetchWorkflow(panel, cx, panel.model(), wd, plan);
  }
  if (repaintDue(sb.paintedAt, now, SB_REPAINT_MS)) { sb.paintedAt = now; cx.notify(); }
}

// GET /flows/{name}, then POST /flows/validate with that flow's fields for its next three runs (the validator writes
// nothing). `fd` is the object of the flow asked for: an answer for a flow no longer shown lands there.
function sbFetchFlow(panel, cx, m, fd) {
  fd.inFlight = true;
  fd.at = Date.now();
  fd.seenStamp = panel.store.get(SB_KEYS.flowsStamp);
  cx.spawn(async (cx2) => {
    try {
      const route = flowRoute(fd.name);
      const got = route === null ? { kind: "refused", message: "that schedule name cannot be looked up from this window" } : await callCao(sbExec, panel.cfg(m), { path: route, maxTime: 8 });
      panel.views.flow = applyOutcome(panel.views.flow, got, Date.now());
      if (got.kind !== "ok") {
        fd.error = got.kind === "unreachable" ? "CAO did not answer" : String(got.detail ?? got.message ?? "the schedule could not be read");
        return;
      }
      fd.flow = got.json;
      fd.error = null;
      const check = await callCao(sbExec, panel.cfg(m), { method: "POST", path: "/flows/validate", body: validateBody(fieldsOfFlow(got.json)), maxTime: 8 });
      Object.assign(fd, validateResult(check, { editing: fd.name, name: fd.name }));
    } finally {
      fd.inFlight = false;
      cx2.notify();
    }
  });
}

// GET /workflows (the row), GET /workflows/runs?limit=20, then, when the plan says so, GET /flows (for "Scheduled
// by") and GET /workflows/{name} (whether Edit is possible). `wd` is the object of the workflow asked for. A failed
// read keeps what the last good one gave.
function sbFetchWorkflow(panel, cx, m, wd, plan) {
  wd.inFlight = true;
  wd.at = Date.now();
  wd.seenStamp = sbWorkflowStamp(panel);
  if (plan.flows) wd.flowsAt = Date.now();
  cx.spawn(async (cx2) => {
    try {
      const cfg = panel.cfg(m);
      for (const read of detailReads(plan)) {
        const call = detailCall(read, wd.name);
        const outcome = call === null ? { kind: "refused", message: "that workflow name cannot be looked up from this window" } : await callCao(sbExec, cfg, call);
        const view = detailView(read);
        if (view) panel.views[view] = applyOutcome(panel.views[view], outcome, Date.now());
        const { patch, stop } = foldDetailRead(wd, read, outcome);
        Object.assign(wd, patch);
        if (stop) break;
      }
    } finally {
      wd.inFlight = false;
      cx2.notify();
    }
  });
}

// ---------- confirmed writes ----------

// Opens the confirm card for `scope` (the item shown); Confirm runs `go` with the cx of its own click.
function sbAsk(panel, cx, scope, spec, go) {
  const sb = panel.sb;
  if (scope === null || scope !== confirmScope(sbRside(panel)) || opOf(sb.ops, scope)?.state === "running" || panel.gate(panel.model()).disabled) return;
  sb.confirm = { scope, spec, focus: spec.defaultFocus === "confirm" ? "confirm" : "cancel", go };
  panel.keys.focus();
  cx.notify();
}

function sbConfirmNow(panel, cx) {
  const sb = panel.sb;
  const confirm = sb.confirm;
  sb.confirm = null;
  if (confirm !== null && confirm.scope === confirmScope(sbRside(panel))) confirm.go(cx);   // a card whose item was left sends nothing
  cx.notify();
}

// Enable, Disable and Run now each ask first. POST /flows/{name}/enable|disable|run, Run now with its 180 s limit.
// The switch is drawn from the server's answer, so a failed write leaves it where it was; the top bar fetches the
// list again at once (the stamp). The answer is kept for the flow that asked.
function sbSendFlowWrite(panel, cx, action, name) {
  const sb = panel.sb;
  const scope = scopeOf("flows", name);
  const route = flowRoute(name, action);
  if (scope === null || route === null || opOf(sb.ops, scope)?.state === "running" || panel.gate(panel.model()).disabled) return;
  const words = { enable: "enabling...", disable: "disabling...", run: "running..." };
  sbSetOp(sb, { item: scope, state: "running", message: words[action], action });
  cx.notify();
  cx.spawn(async (cx2) => {
    const outcome = await callCao(sbExec, panel.cfg(panel.model()), { method: "POST", path: route, maxTime: confirmSpec(action).timeoutSeconds });
    panel.views.flowWrite = applyOutcome(panel.views.flowWrite, outcome, Date.now());
    const r = writeOutcome(action, outcome);
    sbSetOp(sb, { item: scope, state: r.type === "done" ? "done" : "failed", message: r.text, action });
    panel.store.set(SB_KEYS.flowsStamp, Date.now());
    cx2.notify();
  });
}

// Cancel and Resume of a run: POST /workflows/runs/{id}/cancel|resume, sent only if the run is still in the state
// the action needs. The row is drawn from the server's list, so a failed write leaves the run as it was.
function sbSendRunWrite(panel, cx, action, snap, name) {
  const sb = panel.sb;
  const scope = scopeOf("workflows", name);
  const route = runRoute(action, snap.run_id);
  if (scope === null || route === null || opOf(sb.ops, scope)?.state === "running" || panel.gate(panel.model()).disabled) return;
  const check = runWriteCheck(action, snap, sb.wds.get(name)?.runs);
  const entry = { item: scope, run: snap.run_id, runState: snap.state, action };
  if (!check.send) { sbSetOp(sb, { ...entry, state: "failed", message: check.reason }); cx.notify(); return; }
  sbSetOp(sb, { ...entry, state: "running", message: action === "cancel" ? "Cancelling..." : "Resuming..." });
  cx.notify();
  cx.spawn(async (cx2) => {
    const outcome = await callCao(sbExec, panel.cfg(panel.model()), { method: "POST", path: route, maxTime: 30 });
    panel.views.workflowWrite = applyOutcome(panel.views.workflowWrite, outcome, Date.now());
    const r = runWriteOutcome(action, outcome);
    sbSetOp(sb, { ...entry, state: r.type === "done" ? "done" : "failed", message: r.text });
    panel.store.set(SB_KEYS.workflowsStamp, Date.now());
    cx2.notify();
  });
}

// Keys at the panel while a confirm is open: Esc cancels, Enter acts on what has the focus (Cancel cancels), Tab
// and the arrows move the focus between the two buttons.
function sbOnKey(panel, e, cx) {
  const sb = panel.sb;
  if (sb.confirm === null) return;
  const step = dialogKey({ focus: sb.confirm.focus }, sb.confirm.spec, String(e.key ?? "").toLowerCase());
  sb.confirm.focus = step.focus;
  if (step.action) cx.stop_propagation();
  if (step.action === "cancel") sb.confirm = null;
  else if (step.action === "confirm") { sbConfirmNow(panel, cx); return; }
  cx.notify();
}

// ---------- drawing ----------

const sbNote = (t, text) => sbDiv().text_size(12).text_color(t.dim).child(text);
const sbHint = (t, text) => sbDiv().text_size(11).text_color(t.dim).child(text);
const sbProblem = (t, text) => sbDiv().text_size(11.5).text_color(t.blocked).child(text);
const sbTitle = (t, text) => sbDiv().text_size(12).font_medium().text_color(t.foreground).child(text);
// A value with its label beside it: the text wraps, and the row wraps around it, so no label is cut.
const sbTagged = (t, text, label) => sbRow().flex_wrap().gap(6).items_center()
  .child(sbDiv().min_w(0).text_size(12).child(text))
  .when(Boolean(label), (e) => e.child(sbDiv().text_size(10.5).text_color(t.dim).child(label)));
const sbKv = (t, k, val) => sbRow().gap(8).child(sbDiv().w(70).flex_none().text_size(12).text_color(t.dim).child(k)).child(sbDiv().flex_1().min_w(0).text_size(12).child(val));

// A button of this host. kind: plain | primary | danger; a disabled one has no handler.
function sbButton(t, id, text, { kind = "plain", disabled = false, on } = {}) {
  return sbRow().id(id).px(12).py(5).flex_none().items_center().justify_center().rounded(6).text_size(12)
    .when(kind === "primary", (e) => e.bg(t.solid).text_color(t.solid_foreground))
    .when(kind === "danger", (e) => e.border_1().border_color(t.blocked).text_color(t.blocked))
    .when(kind === "plain", (e) => e.border_1().border_color(t.border).text_color(t.foreground))
    .when(disabled, (e) => e.opacity(0.5))
    .when(!disabled && Boolean(on), (e) => e.cursor_pointer().hover((s) => s.bg(t.hover)).on_click((_ev, cx) => { cx.stop_propagation(); on(cx); }))
    .child(text);
}

// The back arrow of a details view: back to the list, which sends nothing.
function sbBackBar(panel, t, list) {
  return sbRow().id("cao-rside-back").gap(6).items_center().py(2).cursor_pointer().hover((s) => s.bg(t.hover)).rounded(6)
    .on_click((_e, cx) => { sbSend(panel, { type: "back" }); cx.notify(); })
    .child(sbDiv().px(4).text_size(14).child("‹"))
    .child(sbDiv().text_size(12).text_color(t.muted).child(list === "flows" ? "Schedules" : "Workflows"));
}

// The card of an open confirm: its title, text and two buttons, the key focus ringed.
function sbConfirmCard(panel, t) {
  const sb = panel.sb;
  const spec = sb.confirm.spec;
  const ring = (name) => (sb.confirm.focus === name ? t.accent : t.border);
  return sbCol().id("cao-rside-confirm").gap(8).p(10).rounded(8).border_1().border_color(spec.danger ? t.blocked : t.accent).bg(t.surface)
    .child(sbDiv().font_semibold().text_size(13).text_color(spec.danger ? t.blocked : t.foreground).child(spec.title))
    .when(Boolean(spec.body), (e) => e.child(sbDiv().text_size(12).text_color(t.muted).child(spec.body)))
    .child(sbRow().gap(8).flex_wrap().justify_end()
      .child(sbRow().id("cao-rside-confirm-cancel").px(12).py(5).items_center().rounded(6).border_1().border_color(ring("cancel")).text_size(12).cursor_pointer()
        .on_click((_e, cx) => { cx.stop_propagation(); sb.confirm = null; cx.notify(); }).child(spec.cancelLabel ?? "Cancel"))
      .child(sbRow().id("cao-rside-confirm-go").px(12).py(5).items_center().rounded(6).border_1().border_color(spec.danger ? t.blocked : ring("confirm")).text_size(12)
        .text_color(spec.danger ? t.blocked : t.foreground)
        .cursor_pointer().on_click((_e, cx) => { cx.stop_propagation(); sbConfirmNow(panel, cx); })
        .child(spec.confirmLabel)));
}

// A list's payload as the bar published it, or an empty one before the first read.
function sbPayload(panel, kind) {
  const p = panel.store.get(kind === "flows" ? SB_KEYS.flowsView : SB_KEYS.workflowsView);
  const ok = p !== null && typeof p === "object" && !Array.isArray(p);
  return { rows: ok ? sbList(p.rows) : [], at: ok && Number.isFinite(p.at) ? p.at : null, auth: ok && p.auth === true, unreachable: ok && p.unreachable === true };
}

// What the window says about a list that failed or went stale (the strings of the failure states in the sessions
// spec): the auth message, "CAO unreachable", and how old the rows on screen are once no good read has come for a while.
function sbListNotes(payload, kind, now) {
  const lines = [];
  if (payload.auth) lines.push(AUTH_MESSAGE);
  if (payload.unreachable) lines.push("CAO unreachable, retrying");
  if (payload.at !== null && (lines.length > 0 || now - payload.at >= SB_STALE_MS[kind])) {
    const age = ageWords(now - payload.at);
    lines.push(`Showing the last list, ${age === "just now" ? age : `${age} old`}`);
  }
  return lines;
}

// A row of a list: the item's name and its state in words; "running" while a confirmed write of that item is out.
function sbRowView(panel, t, kind, name, stateText, mark) {
  const color = stateText === "running" ? t.working : stateText === "failed" ? t.ansi1 : ["off", "never run", "no recent run", "unknown"].includes(stateText) ? t.dim : t.muted;
  return sbRow().id(`cao-rside-${kind}-${name}`).flex_wrap().py(5).px(8).gap(8).items_center().rounded(6)
    .cursor_pointer().hover((st) => st.bg(t.hover))
    .on_click((_e, cx) => { sbSend(panel, { type: "row", item: name }); cx.notify(); })
    .when(mark, (e) => e.child(shapeMark(statusShape("working"), t)))
    .child(sbDiv().min_w(0).child(name))
    .child(sbDiv().flex_1())
    .child(sbDiv().flex_none().text_size(11).text_color(color).child(stateText));
}

// The list view: the list's name with its summary and "+", the notes, then one row per flow or workflow.
function sbListView(panel, t, m, rside, gate) {
  const kind = rside.list;
  const sb = panel.sb;
  const payload = sbPayload(panel, kind);
  const now = Date.now();
  const rows = payload.rows;
  const off = payload.auth || gate.disabled;
  const summary = kind === "flows" ? flowsSummary(rows) : sectionSummary("workflows", rows);
  const running = (name) => opOf(sb.ops, scopeOf(kind, name))?.state === "running";
  const stateOf = (r) => (kind === "flows"
    ? flowRowState(r, { runNowInFlight: running(r.name), sessionWorking: flowSessionWorking(m.sessions, r.name) }).text
    : (running(r.name) ? "running" : r.state));
  const head = sbRow().id(`cao-rside-head-${kind}`).gap(6).flex_wrap().items_center()
    .child(sbDiv().id(`cao-rside-title-${kind}`).font_semibold().text_size(13).when(kind === "flows", (e) => e.tooltip(SB_SCHEDULE_HELP)).child(kind === "flows" ? "Schedules" : "Workflows"))
    .child(sbDiv().text_size(11).text_color(t.dim).child(payload.at === null ? "" : summary))
    .child(sbDiv().flex_1())
    .child(sbRow().id(`cao-rside-plus-${kind}`).px(8).py(2).flex_none().items_center().justify_center().rounded(6).border_1().border_color(t.border)
      .text_color(off ? t.dim : t.foreground).when(off, (e) => e.opacity(0.5))
      .when(!off, (e) => e.cursor_pointer().hover((s) => s.bg(t.hover)).on_click((_e, cx) => {
        cx.stop_propagation();
        openDialog({ kind: kind === "flows" ? "new-flow" : "new-workflow" });
        cx.notify();
      }))
      .child("+"));
  return sbCol().id(`cao-rside-list-${kind}`).gap(6)
    .child(head)
    .children(sbListNotes(payload, kind, now).map((line) => sbProblem(t, line)))
    .child(sbCol().gap(2)
      .children(rows.map((r) => sbRowView(panel, t, kind, r.name, stateOf(r), stateOf(r) === "running")))
      .when(rows.length === 0 && payload.at !== null, (e) => e.child(sbDiv().px(8).py(12).text_size(12).text_color(t.dim).child("none yet")))
      .when(rows.length === 0 && payload.at === null && !payload.auth && !payload.unreachable, (e) => e.child(sbDiv().px(8).py(12).text_size(12).text_color(t.dim)
        .child(kind === "flows" ? "Loading schedules" : "Loading workflows"))));
}

// The session link of a flow's Runs list: selects that session in the Sessions block and leaves the sidebar as it
// is.
function sbSelectSession(panel, name) {
  panel.store.set(SB_KEYS.selection, { kind: "session", name });
}

// Flow details: schedule in words and cron, the next three runs (the server's scheduler, from POST /flows/validate),
// last run, target, the prompt behind "Show prompt", and the live cao-flow-<name> session.
function sbFlowDetails(panel, t, m, rside, gate) {
  const sb = panel.sb;
  const name = rside.item;
  const fd = sb.fds.get(name);
  const back = sbBackBar(panel, t, "flows");
  if (!fd?.flow) return sbCol().gap(10).child(back).child(sbNote(t, fd?.error ?? "Loading the schedule..."));
  const open = sb.prompt?.name === name && sb.prompt.open === true;
  const v = flowDetailsView({ flow: fd.flow, nextRuns: fd.checks?.nextRuns, validateStatus: fd.status, now: Date.now(), promptOpen: open });
  const live = m.sessions.find((s) => s.name === v.session);
  const op = opOf(sb.ops, scopeOf("flows", name));
  const busy = op?.state === "running";
  const off = gate.disabled || busy;
  const ask = (action) => (cx) => sbAsk(panel, cx, scopeOf("flows", name), flowDialogSpec(action, name), (cx2) => sbSendFlowWrite(panel, cx2, action, name));
  return sbCol().id("cao-flow").gap(10)
    .child(back)
    .child(sbRow().gap(8).flex_wrap().items_center()
      .child(sbDiv().font_semibold().text_size(14).child(v.name))
      .when(Boolean(v.target.script), (e) => e.child(sbDiv().flex_none().text_size(10.5).text_color(t.dim).child("script"))))
    .child(sbRow().gap(8).flex_wrap().items_center()
      .child(sbRow().id("cao-flow-switch").w(34).h(18).flex_none().px(2).items_center().rounded(9).border_1().border_color(v.enabled ? t.accent : t.border)   // fixed: a switch with no text
        .when(v.enabled, (e) => e.bg(t.accent).justify_end()).when(off, (e) => e.opacity(0.5))
        .when(!off, (e) => e.cursor_pointer().on_click((_e, cx) => ask(v.enabled ? "disable" : "enable")(cx)))
        .child(sbDiv().size(12).rounded(6).bg(v.enabled ? t.background : t.dim)))   // fixed: the switch's knob
      .child(sbDiv().text_size(12).text_color(v.enabled ? t.accent : t.dim).child(v.enabledText))
      .child(sbDiv().flex_1())
      .child(sbButton(t, "cao-flow-run-now", "Run now", { kind: "primary", disabled: off, on: ask("run") }))
      .child(sbButton(t, "cao-flow-edit", "Edit", { disabled: off || v.editDisabled, on: (cx) => { openDialog({ kind: "edit-flow", item: name }); cx.notify(); } })))
    .when(Boolean(v.editLabel), (e) => e.child(sbProblem(t, `Edit: ${v.editLabel}`)))
    .when(gate.disabled, (e) => e.child(sbProblem(t, gate.text)))
    .when(op !== null, (e) => e.child(sbDiv().id("cao-flow-op").text_size(12).text_color(op.state === "running" ? t.working : op.state === "done" ? t.muted : t.blocked).child(op.message)))
    .child(sbTitle(t, "Schedule"))
    .child(sbCol().gap(2)
      .child(sbDiv().text_size(12).child(v.schedule.words ? `${v.schedule.words}, ${v.schedule.cron}` : v.schedule.cron))
      .child(sbHint(t, v.schedule.note)))
    .child(sbTitle(t, v.next.heading))
    .child(sbCol().id("cao-flow-next").gap(2)
      .children(v.next.lines.map((l) => sbDiv().text_size(12).child(l)))
      .when(Boolean(v.next.none), (e) => e.child(sbDiv().text_size(12).child(v.next.none)))
      .when(Boolean(v.next.stored), (e) => e.child(sbDiv().text_size(11.5).text_color(v.next.stored.stale ? t.blocked : t.muted)
        .child(`Stored next_run: ${v.next.stored.text}${v.next.stored.stale ? " (stale)" : ""}`)))
      .when(v.next.ifEnabled.length > 0, (e) => e.child(sbCol().gap(2).child(sbHint(t, "If you enable it now:"))
        .children(v.next.ifEnabled.map((l) => sbDiv().text_size(12).child(l)))))
      .when(Boolean(v.next.note), (e) => e.child(sbTagged(t, v.next.note, v.next.label))))
    .child(sbTitle(t, "Last run"))
    .child(sbCol().gap(2).child(sbDiv().text_size(12).child(v.lastRun.text)).child(sbHint(t, v.lastRun.note)))
    .child(sbTitle(t, "Target"))
    .child(sbCol().gap(4)
      .child(sbKv(t, "Profile", v.target.profile)).child(sbKv(t, "Provider", v.target.provider))
      .when(Boolean(v.target.script), (e) => e.child(sbKv(t, "Script", v.target.script)))
      .child(sbKv(t, "Session", v.session))
      .child(sbCol().gap(4).child(sbDiv().text_size(12).text_color(t.dim).child("Prompt"))
        .when(v.prompt.control !== null, (x) => x.child(sbRow().child(sbRow().id("cao-flow-prompt-toggle").px(8).py(3).rounded(6).border_1().border_color(t.border)
          .cursor_pointer().hover((s) => s.bg(t.hover)).text_size(12)
          .on_click((_e, cx) => { sb.prompt = { name, open: !open }; cx.notify(); }).child(v.prompt.control))))
        .when(v.prompt.text !== null, (x) => x.child(sbDiv().id("cao-flow-prompt").p(8).rounded(6).bg(t.surface).text_size(12).child(v.prompt.text)))
        .when(v.prompt.note !== null, (x) => x.child(sbHint(t, v.prompt.note)))))
    .child(sbTitle(t, "Runs"))
    .child(sbCol().gap(4)
      .when(Boolean(live), (e) => e.child(sbRow().id("cao-flow-run").flex_wrap().px(8).py(5).gap(8).items_center().rounded(6).border_1().border_color(t.border).cursor_pointer()
        .hover((st) => st.bg(t.hover)).on_click((_e, cx) => { sbSelectSession(panel, v.session); cx.notify(); })
        .child(shapeMark(statusShape(displayState(live.terms[0], { replied: false, quiet: false })), t)).child(sbDiv().min_w(0).text_size(12).child(v.session))))
      .when(!live, (e) => e.child(sbHint(t, "No session from this schedule is alive.")))
      .child(sbTagged(t, v.runsNote.text, v.runsNote.label)));
}

// One run of a workflow: its state words, how long ago it started, the step it is on, and Cancel or Resume where the
// state allows. The answer of a write on this run, or why it was not sent, shows under it while it still applies:
// a "done" note until the run's state moves, a "failed" one while the run is in the state it was made for.
function sbRunRow(panel, t, name, r, gate, busy) {
  const op = opOf(panel.sb.ops, scopeOf("workflows", name));
  const mine = op !== null && op.run === r.id;
  const here = mine && op.state === "running";
  const note = mine && op.state !== "running" && r.state === op.runState ? op : null;
  const off = gate.disabled || busy;
  const ask = (action) => (cx) => {
    const snap = { run_id: r.id, state: r.state };
    sbAsk(panel, cx, scopeOf("workflows", name), runDialogSpec(action, snap), (cx2) => sbSendRunWrite(panel, cx2, action, snap, name));
  };
  return sbCol().id(`cao-run-${r.id}`).gap(3).p(8).rounded(6).border_1().border_color(t.border)
    .child(sbRow().gap(8).flex_wrap().items_center()
      .child(sbDiv().text_size(12).text_color(r.state === "running" ? t.working : r.state === "failed" ? t.ansi1 : t.foreground).child(r.stateText))
      .child(sbDiv().flex_1().min_w(0).text_size(11.5).text_color(t.muted).child(`${r.startedText}${r.step ? `, step ${r.step}` : ""}`))
      .when(r.cancel, (e) => e.child(sbButton(t, `cao-run-cancel-${r.id}`, here ? "Cancelling..." : "Cancel", { kind: "danger", disabled: off, on: ask("cancel") })))
      .when(r.resume, (e) => e.child(sbButton(t, `cao-run-resume-${r.id}`, here ? "Resuming..." : "Resume", { disabled: off, on: ask("resume") }))))
    .when(note !== null, (e) => e.child(sbDiv().id(`cao-run-note-${r.id}`).text_size(11.5).text_color(note.state === "done" ? t.muted : t.blocked).child(note.message)));
}

// Workflow details: description, mode and step count (from the GET /workflows row), who schedules it, and its runs
// (Cancel and Resume confirmed here), with the "not submitted" row of a run that stopped at plan approval. Run,
// Schedule... and Edit open the dialog.
function sbWorkflowDetails(panel, t, rside, gate) {
  const sb = panel.sb;
  const name = rside.item;
  const wd = sb.wds.get(name);
  const back = sbBackBar(panel, t, "workflows");
  if (!wd || wd.row === undefined) return sbCol().gap(10).child(back).child(sbNote(t, wd?.error ?? "Loading the workflow..."));
  const v = workflowDetailsView({ name, row: wd.row, runs: wd.runs, flows: wd.flows, now: Date.now() });
  if (!v.listed) return sbCol().gap(10).child(back).child(sbNote(t, `Workflow ${name} is not listed; it may have been removed.`));
  const editBlock = wd.specOk ? (editFromDocument(wd.spec, name).reason ?? null) : null;
  const pending = approvalRow(panel.store.get(SB_KEYS.approvals), name);
  const busy = opOf(sb.ops, scopeOf("workflows", name))?.state === "running";
  return sbCol().id("cao-workflow").gap(10)
    .child(back)
    .child(sbRow().gap(8).items_center().child(sbDiv().font_semibold().text_size(14).child(v.name)))
    .child(sbRow().gap(8).flex_wrap().items_center()
      .child(sbButton(t, "cao-wf-run", "Run", { kind: "primary", disabled: gate.disabled, on: (cx) => { openDialog({ kind: "run-workflow", item: name }); cx.notify(); } }))
      .child(sbButton(t, "cao-wf-schedule", "Schedule...", { disabled: gate.disabled, on: (cx) => { openDialog({ kind: "new-flow", workflow: name }); cx.notify(); } }))
      .child(sbButton(t, "cao-wf-edit", "Edit", { disabled: gate.disabled || Boolean(editBlock), on: (cx) => { openDialog({ kind: "edit-workflow", item: name }); cx.notify(); } })))
    .when(Boolean(editBlock), (e) => e.child(sbHint(t, editBlock)))
    .when(gate.disabled, (e) => e.child(sbProblem(t, gate.text)))
    .children(workflowsNotes(panel.views.workflows, panel.views.workflowRuns, Date.now()).map((line) => sbProblem(t, line)))
    .child(sbCol().gap(4)
      .when(Boolean(v.description), (e) => e.child(sbDiv().id("cao-wf-description").text_size(12).child(v.description)))
      .child(sbKv(t, "Mode", v.mode || "unknown")).child(sbKv(t, "Steps", v.stepText)))
    .child(sbTitle(t, "Scheduled by"))
    .child(sbCol().id("cao-wf-scheduled").gap(4)
      .children(v.scheduledBy.map((f) => sbRow().id(`cao-wf-by-${f.name}`).flex_wrap().px(8).py(4).gap(8).items_center().rounded(6).border_1().border_color(t.border).cursor_pointer()
        .hover((st) => st.bg(t.hover)).on_click((_e, cx) => { sbSend(panel, { type: "link", list: "flows", item: f.name }); cx.notify(); })
        .child(sbDiv().text_size(12).child(f.name))
        .child(sbDiv().min_w(0).text_size(11.5).text_color(t.muted).child(f.words === f.cron ? f.cron : `${f.words}, ${f.cron}`))
        .child(sbDiv().flex_none().text_size(11).text_color(f.enabled ? t.accent : t.dim).child(f.enabledText))))
      .when(v.scheduledBy.length === 0, (e) => e.child(sbHint(t, "No schedule runs this workflow."))))
    .child(sbHint(t, v.schedulerNote))
    .child(sbTitle(t, "Runs"))
    .child(sbCol().id("cao-wf-runs").gap(4)
      .when(pending !== null, (e) => e.child(sbCol().id("cao-wf-local-run").gap(2).p(8).rounded(6).border_1().border_color(t.border)
        .child(sbRow().gap(8).flex_wrap().items_center()
          .child(sbDiv().text_size(12).child("not submitted"))
          .child(sbDiv().text_size(11.5).text_color(t.blocked).child("approval needed")))
        .child(sbHint(t, `Plan id: ${pending.planId}. Run opens a new form.`))))
      .children(v.runs.map((r) => sbRunRow(panel, t, name, r, gate, busy)))
      .when(v.runs.length === 0, (e) => e.child(sbHint(t, v.runsLine ?? "No runs yet."))));
}

// The sidebar: a confirm card when one is open for the item shown, else the list, or the details of the item.
export function renderSidebarHost(panel) {
  const t = sbTheme();
  const m = panel.model();
  const sb = panel.sb;
  const rside = sbRside(panel);
  sbDropStaleConfirm(panel);
  const gate = panel.gate(m);
  let body;
  if (sb.confirm !== null) body = sbConfirmCard(panel, t);
  else if (rside.item === null) body = sbListView(panel, t, m, rside, gate);
  else if (rside.list === "flows") body = sbFlowDetails(panel, t, m, rside, gate);
  else body = sbWorkflowDetails(panel, t, rside, gate);
  sb.paintedAt = Date.now();
  return sbCol().id("cao-panel").size_full().min_h(0).bg(t.chrome).text_color(t.foreground).text_size(13)
    .track_focus(panel.keys).on_key_down((e, cx) => sbOnKey(panel, e, cx))
    .when(panel.store.notice() !== null, (e) => e.child(sbDiv().flex_none().px(12).pt(8).text_size(11).text_color(t.blocked).child(panel.store.notice())))
    .when(dialogBusyNotice() !== null, (e) => e.child(sbDiv().flex_none().px(12).pt(8).text_size(11).text_color(t.blocked).child(dialogBusyNotice())))
    .child(sbCol().id("cao-panel-body").flex_1().min_h(0).overflow_y_scrollbar().p(12).gap(10).child(body));
}
