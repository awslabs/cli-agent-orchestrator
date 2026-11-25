// The panel mod's dialog host: one centered dialog hosts every form, and the author
// sessions it started are cleaned up when it closes. main.js picks it when props().host is "dialog": the
// overlay lib/ui.js openDialog opens with the request in its props, the opener's layout props merged in
// (dialogSpec), and the dialog's own name in `request.name` (the item a form works on travels as `request.item`).
//
// This file is the frame and its rules: a title, a scrolling body, a row of buttons at the bottom; the keys,
// decided by the pure reducer dialogStep (Esc asks before discarding edits, Esc in a confirmation returns to the
// form, closing is refused while a write is in flight and allowed after its `timeout`); and the lease
// `caowin.dialog` that says the dialog is open. The forms are drawn in the body by kind (DL_FORMS): the status bar's
// help, the New session form, the flow forms (New flow, Edit flow, Schedule...) and the workflow forms (New workflow,
// Edit workflow, Run) are here, and New flow and New workflow carry the Draft with AI box. A form is an object of five functions that take
// the panel: init, load (its first reads), body, buttons (the bottom row) and dirty, and may have a sixth, tick.
//
// A confirmation of a form (Ask first, YOLO, the `*` override, Create and enable) replaces the form body inside the
// dialog, and the form's buttons stay at the bottom, dimmed, so the form behind it cannot change. Esc there is the
// confirmation's own Cancel and returns to the form; the discard question of the frame is drawn the same way.
//
// Open state is a lease. Herdr closes some overlays without running mod code, so a flag only the dialog
// clears would lock every opener for good. The opener wrote the lease for this dialog's name; the dialog renews it
// every second from its own timer (leaseFor), and close(cx) stops that timer first and clears the key after.
// Renewal and clear act only when the stored record carries this dialog's own name or the key is empty, so a
// dialog that stalled past 10 s cannot overwrite or clear the lease of the one that replaced it.
//
// The request is the dialog's own snapshot: it is read once, here, and nothing this file sends or draws reads the
// selected session or what the right sidebar shows afterwards (the one sidebar write is the `set-rside` effect of
// a save). No field sets a tab index: the Tab order is the drawn order, which the manual key test (CHECKLIST.md, U23) checks.
//
// The functions take the panel instance (the exported View of main.js). The host imports are aliased with the dl
// prefix because bundle mode puts the shared modules and every file of the mod in one scope.
import { div as dlDiv } from "gpui-kit";
import { v_flex as dlCol, h_flex as dlRow, InputState as dlInputState, TextareaState as dlTextareaState } from "gpui-base";
import { closeOverlay as dlCloseOverlay, notify as dlNotify, props as dlProps, theme as dlTheme, checking as dlChecking, exec as dlExec } from "herdr";
import { randomUUID as dlUuid } from "crypto";
import { callCao, STDIN_SUPPORTED, AUTH_MESSAGE } from "./cao.js";
import { applyOutcome, dialogKey, unchosenProfileText } from "./logic.js";
import {
  emptyForm, profilesForForm, resolvedProvider, nameCheck, workdirCheck, envCheck, launchBlocked, launchPlan, equivalentCommand, discardNeeded, parseToolList,
  profileFromOutcome, profileRoute, providerChoices, profileMatches, comboStep, launchDialog, buildRequest, withKey, keepSubmission, launchOutcome, launchResultCard,
  launchAfter, launchConfirm, launchReadResult,
} from "./launch.js";
import {
  flowRoute, validateBody, validateResult, FLOW_PRESETS, emptyFlowForm, flowFormOf, flowFields, flowCheckKey, formErrorsOf, shownErrors, firstErrorField, flowFormGate, flowSaveOutcome,
  flowCreateBody, flowUpdateBody, routesState, flowDialogSpec, FLOW_CLI, validateNote, flowSaveAttempt, flowSaveEffects, numericWeekdayWarning, scheduleWarning, workflowChoices,
  flowFormDirty, runWords,
} from "./flows.js";
import {
  workflowRoute, runFormModel, runInputs, runInputsOk, approvalView, runSubmitEffects, emptyWorkflowForm, workflowEditorDirty, workflowValidationKey, validateReport, findingWords,
  validateOutcomeNote, workflowEditorGate, workflowSaveResult, workflowSaveEffects, editFromDocument, editorGateWithName, templateFor, templateNeedsConfirm, isTemplateText,
  TEMPLATE_NOTE, workflowFileHint, workflowDialogSpec, editorSizeCheck, readFailureText,
} from "./workflows-view.js";
import { submitRequest, saveRequest } from "./workflows.js";
import { TEMPLATES } from "./templates.js";
import { initialDraft, draftStep, draftBox, draftRequest, fixRequest, recordDraft, clearMark } from "./draft.js";
import {
  draftCall, draftAnswer, draftActionList, draftActionKind, draftPressCheck, draftBeginCheck, draftTickEvents, draftJobDone, draftFillPlan, draftValidatedEvent,
  markedFlag, draftValidationErrors, draftFieldsOf, draftFormDirty,
} from "./draft-view.js";
import { dialogStep, initialDialog, leaseFor, helpView, recordApproval, clearApproval, recordAuthor, afterDelete, afterLaunch } from "./shell.js";
import { heading, hintLine, problemLine, fieldBox, inputBox, textareaBox, pushButton, chip, choiceRow } from "./widgets.js";

const DL_LEASE_KEY = "caowin.dialog";
const DL_RSIDE_KEY = "caowin.rside";
const DL_SELECTION_KEY = "caowin.selection";   // written once, by a launch that returned 201; the dialog never reads it
const DL_TICK_MS = 1000;
const DL_WRITE_EXTRA_MS = 5000;     // a write may be lost once its --max-time plus 5 s have passed
const DL_LAUNCH_SECONDS = 20;       // the curl limit of POST /sessions, and the time before a lost answer lets the dialog close
const DL_BLUR_CLOSE_MS = 200;       // a profile list closes this long after its field loses the keys, so a click on a row still lands
const DL_COPIED_MS = 2000;
const DL_FLOWS_STAMP_KEY = "caowin.flowsStamp";   // the top bar reads GET /flows again when a write moves it
const DL_FLOW_SAVE_SECONDS = 15;     // the curl limit of POST /flows and PUT /flows/{name}
const DL_WORKFLOWS_STAMP_KEY = "caowin.workflowsStamp";   // the top bar reads GET /workflows again when a write moves it
const DL_AUTHORS_KEY = "caowin.authors";   // the Draft with AI author sessions: {name, owner, at, tries, id}, written only by the pure record functions
const DL_DRAFTS_KEY = "caowin.drafts";     // the terminal ids of those author sessions: what marks a session "draft" in Sessions
const DL_APPROVALS_KEY = "caowin.approvals";   // the "not submitted" rows: {workflow, planId, at}, written only through recordApproval and clearApproval
const DL_WORKFLOW_SAVE_SECONDS = 20;   // the curl limit of POST /workflows and PUT /workflows/{name}
const DL_RUN_SECONDS = 20;             // the curl limit of POST /workflows/runs:submit
const DL_CHECK_DEBOUNCE_MS = 400;    // a flow form asks the server to check it this long after the last change
const DL_TITLES = Object.freeze({
  "new-session": "New session", "new-flow": "New schedule", "edit-flow": "Edit schedule", "new-workflow": "New workflow",
  "edit-workflow": "Edit workflow", "run-workflow": "Run workflow", "bar-help": "Help",
});

// The forms the dialog draws, by request kind. Each is {init(panel), load(panel, cx), body(panel, t, m), buttons(panel, t, m, locked), dirty(panel)}.
const DL_SESSION_FORM = { init: dlSessionInit, load: dlSessionLoad, body: dlSessionBody, buttons: dlSessionButtons, dirty: dlSessionDirty };
const DL_FLOW_FORM = { init: dlFlowInit, load: dlFlowLoad, body: dlFlowBody, buttons: dlFlowButtons, dirty: dlFlowDirty };
const DL_WORKFLOW_FORM = { init: dlWorkflowInit, load: dlWorkflowLoad, body: dlWorkflowBody, buttons: dlWorkflowButtons, dirty: dlWorkflowDirty };
const DL_RUN_FORM = { init: dlRunInit, load: dlRunLoad, body: dlRunBody, buttons: dlRunButtons, dirty: dlRunDirty, tick: dlRunTick };
const DL_FORMS = { "new-session": DL_SESSION_FORM, "new-flow": DL_FLOW_FORM, "edit-flow": DL_FLOW_FORM, "new-workflow": DL_WORKFLOW_FORM, "edit-workflow": DL_WORKFLOW_FORM, "run-workflow": DL_RUN_FORM };

// The dialog's state on the panel: the request snapshot, its own name and overlay id, the reducer's state, the
// renewal timer, and what a form tells the host (`nested`: a confirmation replaces the form body; `writeUntil`: when
// the write in flight may be taken as lost). The form's own state (`ns`, `ins`, `created`, ...) is made by its init.
export function initDialogHost(panel, cx) {
  const given = dlProps();
  const request = given !== null && typeof given === "object" ? { ...given } : {};
  panel.dl = {
    request,
    name: typeof request.name === "string" ? request.name : "",
    overlay: typeof request.overlay === "string" ? request.overlay : null,
    state: dialogStep(initialDialog(), { type: "open", request }).state,
    lease: null, closed: false, focused: false, notice: null, nested: false, writeUntil: null,
    form: typeof request.kind === "string" && Object.hasOwn(DL_FORMS, request.kind) ? DL_FORMS[request.kind] : null,
    job: null,                        // the Draft with AI state of New flow or New workflow: {kind, state, pending, refusal, owner, text}
    confirm: null,                    // the confirmation that replaces the form body: {build, focus, go, cancel, needsFocus}
    loaded: false,                    // the form's first reads are sent from the first tick
    combo: { open: false, index: 0 }, // the profile list of the form
    profileCache: {},                 // profile name -> GET /agents/profiles/{name} answer
  };
  if (dlChecking()) return;                       // a check renders once: no timers, no overlay, no form
  panel.dl.form?.init(panel);
  panel.dl.lease = cx.timer.every(DL_TICK_MS, (cx2) => dlTick(panel, cx2));
}

// ---------- the lease ----------

// Whether the stored lease record is this dialog's, or there is none: the only cases in which this dialog may
// renew it or clear it. Another dialog's record, live or not, is left alone.
function ownsLease(panel) {
  const record = panel.store.get(DL_LEASE_KEY);
  return record === undefined || record === null || record.name === panel.dl.name;
}

// Called every second by the timer: the record is read first, and written only when it is ours or empty.
function renewLease(panel) {
  if (ownsLease(panel)) panel.store.set(DL_LEASE_KEY, leaseFor(panel.dl.name, Date.now()));
}

function clearLease(panel) {
  if (ownsLease(panel)) panel.store.set(DL_LEASE_KEY, null);
}

// One tick: renew the lease, take the keys on the first tick, and tell the reducer when a write in flight has gone
// unanswered for its time limit (so a lost answer cannot trap the user).
function dlTick(panel, cx) {
  const dl = panel.dl;
  if (dl.closed) return;
  renewLease(panel);
  if (!dl.focused) { dl.focused = true; panel.keys.focus(); }
  if (!dl.loaded) { dl.loaded = true; dl.form?.load(panel, cx); }
  dl.form?.tick?.(panel, cx);
  dlTickDrafts(panel, cx);
  dlFocusConfirm(panel);
  if (dl.state.writing && dl.writeUntil !== null && Date.now() >= dl.writeUntil) {
    dl.writeUntil = null;
    dialogEvent(panel, cx, { type: "timeout" });
  }
}

// The overlay goes, or the window quits (best effort: Herdr calls it once, within 250 ms). The renewal timer stops
// first, so that it cannot write the lease back, and the key is cleared after.
export async function closeDialogHost(panel, _cx) {
  const dl = panel.dl;
  if (!dl) return;
  dl.closed = true;
  dl.lease?.cancel?.();
  clearLease(panel);
}

// ---------- events ----------

// One event for the reducer, and what its effects ask of the host: the discard confirm is drawn from the state, a
// refusal is a line above the buttons, a close closes the overlay (after saying a lost answer may have been lost),
// and a save sets the right sidebar's state.
export function dialogEvent(panel, cx, event) {
  const dl = panel.dl;
  const step = dialogStep(dl.state, event);
  dl.state = step.state;
  dl.notice = null;
  for (const fx of step.effects) {
    if (fx.type === "refuse") dl.notice = fx.message;
    else if (fx.type === "set-rside") panel.store.set(DL_RSIDE_KEY, fx.rside);
    else if (fx.type === "close") {
      if (fx.note) dlNotify({ title: "CAO", body: fx.note, level: "warning" });
      if (dl.overlay !== null) dlCloseOverlay(dl.overlay);
    }
  }
  cx.notify();
}

// A form's write (a launch, a save, a submit) is sent: closing is refused until it is answered, or until its
// --max-time plus 5 s have passed. A read does not call this.
export function dialogWriteStart(panel, cx, maxTimeSeconds) {
  panel.dl.writeUntil = Date.now() + maxTimeSeconds * 1000 + DL_WRITE_EXTRA_MS;
  dialogEvent(panel, cx, { type: "write-start" });
}

export function dialogWriteEnd(panel, cx) {
  panel.dl.writeUntil = null;
  dialogEvent(panel, cx, { type: "write-end" });
}

// Esc. With a confirmation of the form open the keys are the confirmation's own (Esc is its Cancel and returns to
// the form body); otherwise the reducer decides (ask before discarding edits, refuse while writing).
function dlOnKey(panel, e, cx) {
  const key = String(e.key ?? "").toLowerCase();
  if (panel.dl.confirm !== null && !panel.dl.state.discarding) { dlConfirmKey(panel, key, cx); return; }
  if (key !== "escape" && key !== "esc") return;
  cx.stop_propagation();
  dlSyncDirty(panel);
  dialogEvent(panel, cx, { type: "close", confirming: panel.dl.nested === true });
}

// The Close button: the same as Esc, so the reducer decides.
function dlPressClose(panel, cx) {
  if (panel.dl.confirm !== null) { dlCancelConfirm(panel, cx); return; }
  dlSyncDirty(panel);
  dialogEvent(panel, cx, { type: "close" });
}

// Tells the reducer whether the form holds edits now, so that a close asks before it discards them.
function dlSyncDirty(panel) {
  const dl = panel.dl;
  dl.state = dialogStep(dl.state, { type: "dirty", dirty: dl.form?.dirty(panel) === true }).state;
}

// ---------- the confirmation of a form ----------

// Opens a confirmation in place of the form body. `build()` gives its spec on every draw (a launchDialog or flowDialogSpec
// spec); `go(cx)` runs when it is confirmed, `cancel(cx)` when it is not. The first key goes to the spec's `defaultFocus`
// ("cancel", "confirm", or "input" for the typed name).
function dlAskConfirm(panel, cx, build, go, cancel = () => {}) {
  const dl = panel.dl;
  dl.confirm = { build, focus: build().defaultFocus ?? "cancel", go, cancel, needsFocus: true };
  dl.nested = true;
  dlFocusConfirm(panel);
  cx.notify();
}

// The keyboard goes to the confirmation: its typed field, else the dialog (which reads the keys).
function dlFocusConfirm(panel) {
  const dl = panel.dl;
  if (dl.confirm === null || !dl.confirm.needsFocus || dl.closed) return;
  dl.confirm.needsFocus = false;
  (dl.confirm.focus === "input" ? dl.ins.typed : panel.keys).focus();
}

// Confirm: the form is let go of first, then the action runs with the snapshot its dialog was opened for.
function dlAcceptConfirm(panel, cx) {
  const dl = panel.dl;
  const c = dl.confirm;
  dl.confirm = null;
  dl.nested = false;
  if (c !== null) c.go(cx);
  cx.notify();
}

// Cancel, Esc and the Close button of a confirmation: back to the form body, which is as it was.
function dlCancelConfirm(panel, cx) {
  const dl = panel.dl;
  const c = dl.confirm;
  dialogEvent(panel, cx, { type: "close", confirming: c !== null });
  dl.confirm = null;
  dl.nested = false;
  if (c !== null) c.cancel(cx);
}

// Keys inside a confirmation (dialogKey): Esc cancels, Enter acts on what has the focus (Cancel cancels, Confirm acts
// only when the spec says it is enabled), Tab and the arrows move the focus between the two buttons.
function dlConfirmKey(panel, key, cx) {
  const dl = panel.dl;
  const step = dialogKey({ focus: dl.confirm.focus }, dl.confirm.build(), key);
  dl.confirm.focus = step.focus;
  if (step.action) cx.stop_propagation();
  if (step.action === "cancel") dlCancelConfirm(panel, cx);
  else if (step.action === "confirm") { dlAcceptConfirm(panel, cx); return; }
  cx.notify();
}

// The card of an open confirmation: its title, its text and two buttons, the key focus ringed.
function dlConfirmCard(panel, t) {
  const dl = panel.dl;
  const spec = dl.confirm.build();
  const ring = (name) => (dl.confirm.focus === name ? t.accent : t.border);
  return dlCol().id("cao-dialog-confirm-card").gap(8).p(10).rounded(8).border_1().border_color(spec.danger ? t.blocked : t.accent).bg(t.surface)
    .child(dlDiv().font_semibold().text_size(13).text_color(spec.danger ? t.blocked : t.foreground).child(spec.title))
    .when(Boolean(spec.body), (e) => e.child(dlDiv().text_size(12).text_color(t.muted).child(spec.body)))
    .when(Boolean(spec.role), (e) => e.child(dlDiv().text_size(12).child(`Declared role: ${spec.role}`)))
    .when(spec.toolsText !== undefined, (e) => e.child(dlCol().gap(2)
      .child(dlDiv().text_size(12).child(`${spec.toolsHeading}: ${spec.toolsText}`))
      .when(Boolean(spec.toolsLabel), (x) => x.child(dlDiv().text_size(10.5).text_color(t.dim).child(spec.toolsLabel)))))
    .when(Boolean(spec.overrideNote), (e) => e.child(dlDiv().text_size(12).text_color(t.blocked).child(spec.overrideNote)))
    .when(Boolean(spec.enforcement), (e) => e.child(dlDiv().text_size(12).child(`Enforcement: ${spec.enforcement}`)))
    .when(Boolean(spec.notEnforcedLabel), (e) => e.child(dlDiv().text_size(11.5).text_color(t.blocked).child(spec.notEnforcedLabel)))
    .when(Boolean(spec.installTime), (e) => e.child(dlCol().gap(2)
      .child(dlDiv().text_size(12).child(spec.installTime.line))
      .child(dlDiv().text_size(11.5).text_color(t.muted).child(spec.installTime.note))))
    .when(spec.kind === "yolo", (e) => e.child(dlCol().gap(4)
      .child(dlDiv().text_size(12).child(`${spec.confirmPrompt}: ${spec.typedName}`))
      .child(inputBox(t, "cao-yolo-typed", dl.ins.typed))))
    .child(dlRow().gap(8).flex_wrap().justify_end()
      .child(dlRow().id("cao-dialog-cancel").min_h(28).px(12).py(4).items_center().rounded(6).border_1().border_color(ring("cancel")).text_size(12).cursor_pointer()
        .on_click((_e, cx) => { cx.stop_propagation(); dlCancelConfirm(panel, cx); }).child(spec.cancelLabel ?? "Cancel"))
      .child(dlRow().id("cao-dialog-confirm").min_h(28).px(12).py(4).items_center().rounded(6).border_1().border_color(spec.danger ? t.blocked : ring("confirm")).text_size(12)
        .text_color(spec.danger ? t.blocked : t.foreground).when(spec.confirmEnabled !== true, (e) => e.opacity(0.5))
        .when(spec.confirmEnabled === true, (e) => e.cursor_pointer().on_click((_e, cx) => { cx.stop_propagation(); dlAcceptConfirm(panel, cx); }))
        .child(spec.confirmLabel)));
}

// ---------- drawing ----------

// A value with its label beside it: the text wraps, and the row wraps around it, so no label is cut.
const dlTagged = (t, text, label) => dlRow().flex_wrap().gap(6).items_center()
  .child(dlDiv().min_w(0).text_size(12).child(text))
  .when(Boolean(label), (e) => e.child(dlDiv().text_size(10.5).text_color(t.dim).child(label)));

// The body of a request by its kind. The help is the status bar's note: "done" means the turn ended, the known gap
// and its label, as helpView gives them. A form is drawn by its own functions, or by the confirmation that replaces it.
function dlBody(panel, t, m) {
  const dl = panel.dl;
  const kind = dl.request.kind;
  if (kind === "bar-help") {
    const help = helpView();
    return dlCol().id("cao-dialog-help").gap(8)
      .child(dlDiv().text_size(12).child(help.doneMeans))
      .child(dlTagged(t, help.text, help.label));
  }
  if (dl.form !== null) return dl.confirm !== null ? dlConfirmCard(panel, t) : dl.form.body(panel, t, m);
  return dlDiv().id("cao-dialog-unknown").text_size(12).text_color(t.dim).child("This dialog request is not one the window knows.");
}

// The discard confirmation: it replaces the form body, Keep editing is the default (Esc returns to the form).
function dlDiscard(t) {
  return dlCol().id("cao-dialog-discard").gap(8).p(10).rounded(8).border_1().border_color(t.accent).bg(t.surface)
    .child(dlDiv().font_semibold().text_size(13).child("Discard this form?"))
    .child(dlDiv().text_size(12).text_color(t.muted).child("What you typed in this form will be lost."));
}

export function renderDialogHost(panel) {
  const t = dlTheme();
  const dl = panel.dl;
  const request = dl.request;
  const item = typeof request.item === "string" && request.item !== "" ? ` ${request.item}` : "";
  const title = Object.hasOwn(DL_TITLES, request.kind) ? `${DL_TITLES[request.kind]}${request.kind === "bar-help" ? "" : item}` : "Dialog";
  const discarding = dl.state.discarding;
  const m = dl.form === null ? null : panel.model();
  const locked = dl.confirm !== null;       // under a confirmation the form behind it cannot change, so its buttons are dimmed
  return dlCol().id("cao-dialog").size_full().min_h(0).bg(t.chrome).text_color(t.foreground).text_size(13)
    .track_focus(panel.keys).on_key_down((e, cx) => dlOnKey(panel, e, cx))
    .child(dlRow().flex_none().px(12).pt(12).pb(6).items_center().child(dlDiv().font_semibold().text_size(14).child(title)))
    .child(dlCol().id("cao-dialog-body").flex_1().min_h(0).overflow_y_scrollbar().px(12).py(6).gap(10)
      .when(panel.store.notice() !== null, (e) => e.child(dlDiv().text_size(11).text_color(t.blocked).child(panel.store.notice())))
      .child(discarding ? dlDiscard(t) : dlBody(panel, t, m)))
    .when(dl.notice !== null, (e) => e.child(dlDiv().flex_none().px(12).child(problemLine(t, dl.notice))))
    .when(dl.state.lost, (e) => e.child(dlDiv().flex_none().px(12).text_size(11).text_color(t.dim).child("No answer came in time. Close leaves the dialog.")))
    .child(dlRow().id("cao-dialog-buttons").flex_none().gap(8).flex_wrap().items_center().justify_end().p(12)
      .when(discarding, (e) => e
        .child(pushButton(t, "cao-dialog-keep", "Keep editing", { on: (cx) => dialogEvent(panel, cx, { type: "close" }) }))
        .child(pushButton(t, "cao-dialog-discard-go", "Discard", { kind: "danger", on: (cx) => dialogEvent(panel, cx, { type: "close", discard: true }) })))
      .when(!discarding, (e) => e
        .children(dl.form === null ? [] : dl.form.buttons(panel, t, m, locked))
        .child(pushButton(t, "cao-dialog-close", "Close", { on: (cx) => dlPressClose(panel, cx) }))));
}

// ---------- reads the forms share ----------

// The loaded profile named `name`, or null (not asked yet, in flight, or unreadable).
function dlProfileOf(panel, name) {
  const d = panel.dl.profileCache[name];
  return d && !d.pending && !d.error ? d : null;
}

// GET /agents/profiles and /agents/providers (and /workflows for New flow), once when the dialog opens. Reads only: they
// never block closing.
function dlLoadFormData(panel, cx) {
  const wanted = [["profiles", "/agents/profiles"], ["providers", "/agents/providers"]];
  if (panel.dl.request.kind === "new-flow") wanted.push(["workflows", "/workflows"]);
  cx.spawn(async (cx2) => {
    for (const [name, path] of wanted) {
      const outcome = await callCao(dlExec, panel.cfg(panel.model()), { path, maxTime: 8 });
      panel.views[name] = applyOutcome(panel.views[name], outcome, Date.now());
      cx2.notify();
    }
  });
}

// GET /agents/profiles/{name}, once per name while the dialog is open: the provider default and the Ask-first
// confirmation read it. Resolves to the profile, or {error}. An answer is cached, a failure never is: the entry is
// dropped, so the next call asks CAO again. `cx` must be an async context (inside cx.spawn).
async function dlLoadProfile(panel, cx, name) {
  const cache = panel.dl.profileCache;
  if (cache[name]?.pending) return cache[name].pending;
  if (cache[name]) return cache[name];
  const route = profileRoute(name);
  const entry = {};
  cache[name] = entry;
  entry.pending = (async () => {
    const outcome = route === null ? { kind: "refused", message: "that profile name cannot be looked up from this window" } : await callCao(dlExec, panel.cfg(panel.model()), { path: route, maxTime: 6 });
    panel.views.profile = applyOutcome(panel.views.profile, outcome, Date.now());
    const detail = profileFromOutcome(outcome)
      ?? { error: outcome.kind === "unreachable" ? "CAO did not answer" : (String(outcome.detail ?? outcome.message ?? "").trim() || "could not read the profile") };
    if (detail.error) { if (cache[name] === entry) delete cache[name]; } else cache[name] = detail;
    cx.notify();
    return detail;
  })();
  return entry.pending;
}

// A profile list opens while its field has the keys or is typed in, and closes when the field loses them (a moment
// later, so a click on a row still lands). `chosen()` is the profile the form holds now.
function dlComboEvents(panel, input, chosen) {
  const dl = panel.dl;
  input.on("change", (_e, cx) => { if (input.value() !== chosen()) { dl.combo = { open: true, index: 0 }; } cx.notify(); });
  input.on("focus", (_e, cx) => { dl.combo.open = true; cx.notify(); });
  input.on("blur", (_e, cx) => cx.timer.after(DL_BLUR_CLOSE_MS, (cx2) => { dl.combo.open = false; cx2.notify(); }));
}

// The profile combobox of a form: the filter field, the matches under it while the list is open, and the chosen
// name. `choose(cx, name)` stores a choice. Keys: Up, Down, Home, End, Enter, Esc (comboStep).
function dlProfileCombo(panel, t, id, flag, input, chosen, error, choose) {
  const dl = panel.dl;
  const matches = profileMatches(profilesForForm(panel.views.profiles?.data), input.value());
  const down = panel.views.profiles?.data === undefined && panel.views.profiles?.unreachable === true;
  const onKey = (e, cx) => {
    const step = comboStep({ index: dl.combo.index }, matches.length, e.key);
    dl.combo.index = step.index;
    if (step.action === "choose") { cx.stop_propagation(); choose(cx, matches[step.index].name); }
    // Esc closes the list only while it is drawn; with no list the press goes on to the dialog's own keys (dlOnKey).
    else if (step.action === "close" && dl.combo.open && matches.length > 0) { cx.stop_propagation(); dl.combo.open = false; }
    cx.notify();
  };
  return fieldBox(t, "Agent profile", flag,
    dlCol().gap(4)
      .child(dlDiv().on_key_down(onKey).child(inputBox(t, id, input, Boolean(error))))
      .when(dl.combo.open && matches.length > 0, (e) => e.child(dlCol().id(`${id}-list`).rounded(6).border_1().border_color(t.border).bg(t.popover).overflow_hidden()   // fixed: clips the rows to the list's rounded corners; its rows wrap, so no profile name is cut
        .children(matches.map((r, i) => dlRow().id(`cao-pr-${r.name}`).min_h(24).px(8).py(3).items_center().text_size(12).cursor_pointer()
          .when(i === dl.combo.index, (x) => x.bg(t.hover)).hover((st) => st.bg(t.hover))
          .on_click((_ev, cx) => { cx.stop_propagation(); choose(cx, r.name); }).child(r.name)))))
      .child(dlDiv().text_size(11.5).text_color(t.muted).child(chosen ? `Selected: ${chosen}` : "Selected: none")),
    { error: error ?? (down ? "CAO did not answer: no profiles to choose from" : null) });
}

// ---------- the New session form ----------

// The form's plain state (`ns`) and its retained text states (`ins`). InputState.new needs a live host call, so they
// are made once, here. The work directory starts as the one last launched from.
function dlSessionInit(panel) {
  const dl = panel.dl;
  const form = emptyForm(panel.store.getLocal("caowin.lastWorkdir"));
  dl.ns = {
    form, initial: form, attempted: false, advanced: false, env: [], copied: false, banner: null,
    sending: false, reading: false, submission: null, retry: false, serverError: null,
  };
  dl.created = null;               // {result, form}: the card of the launch that returned 201, shown in place of the form
  dl.ins = {};
  const text = (name, state, onChange) => { state.on("change", (_e, cx) => { onChange(state.value()); cx.notify(); }); dl.ins[name] = state; };
  text("profileFilter", dlInputState.new({ placeholder: "Type to filter the profiles" }), () => {});
  dlComboEvents(panel, dl.ins.profileFilter, () => dl.ns.form.profile);
  text("sessionName", dlInputState.new({ placeholder: "optional, becomes cao-<name>" }), (v) => dlSetField(panel, "sessionName", v));
  text("workdir", dlInputState.new({ placeholder: "/path/on/the/CAO/machine", value: form.workdir }), (v) => dlSetField(panel, "workdir", v));
  text("message", dlTextareaState.new({ placeholder: "First message for the agent (optional)", rows: 4 }), (v) => dlSetField(panel, "message", v));
  text("tools", dlInputState.new({ placeholder: "execute_bash, fs_read, @cao-mcp-server" }), (v) => dlSetField(panel, "allowedTools", parseToolList(v)));
  text("typed", dlInputState.new({ placeholder: "session name" }), () => {});
  dl.ins.typed.on("submit", (_e, cx) => { if (dl.confirm?.build().confirmEnabled) dlAcceptConfirm(panel, cx); });
}

function dlSessionLoad(panel, cx) {
  dlLoadFormData(panel, cx);
}

// Whether the form holds something typed: the policy choice alone does not count, text in the profile field that was
// not chosen does. A launched form holds nothing: its card shows and Close closes without asking.
function dlSessionDirty(panel) {
  const dl = panel.dl;
  if (dl.created !== null) return false;
  return discardNeeded(dl.ns.form, dl.ns.initial) || unchosenProfileText(dl.ins.profileFilter.value(), dl.ns.form.profile);
}

function dlSetField(panel, key, value) {
  const ns = panel.dl.ns;
  if (ns.serverError?.field === key) ns.serverError = null;
  ns.form = { ...ns.form, [key]: value };
}

function dlSyncEnv(panel) {
  const ns = panel.dl.ns;
  dlSetField(panel, "env", ns.env.map((r) => ({ key: r.key.value(), value: r.value.value() })));
}

function dlAddEnvRow(panel, cx) {
  const row = { key: dlInputState.new({ placeholder: "NAME" }), value: dlInputState.new({ placeholder: "value" }) };
  for (const st of [row.key, row.value]) st.on("change", (_e, cx2) => { dlSyncEnv(panel); cx2.notify(); });
  panel.dl.ns.env.push(row);
  dlSyncEnv(panel);
  cx.notify();
}

function dlRemoveEnvRow(panel, cx, index) {
  const [row] = panel.dl.ns.env.splice(index, 1);
  row?.key.release();
  row?.value.release();
  dlSyncEnv(panel);
  cx.notify();
}

function dlChooseProfile(panel, cx, name) {
  const dl = panel.dl;
  dlSetField(panel, "profile", name);
  dl.ins.profileFilter.set_value(name);
  dl.combo.open = false;
  cx.spawn((cx2) => dlLoadProfile(panel, cx2, name));
  cx.notify();
}

// Moves the keyboard to the field a problem is about (an env problem opens Advanced and takes the row's key field).
function dlFocusProblem(panel, p) {
  const { ns, ins } = panel.dl;
  if (p?.field === "env") {
    ns.advanced = true;
    (ns.env[p.row ?? ns.env.length - 1]?.key ?? ins.tools).focus();
    return;
  }
  if (p?.field === "allowedTools") ns.advanced = true;
  ({ profile: ins.profileFilter, sessionName: ins.sessionName, workdir: ins.workdir })[p?.field]?.focus();
}

// Launch: problems at their fields, else the confirmation the policy asks for (Auto-approve sends at once). What is
// confirmed and sent is the snapshot of this press, whatever the form becomes; the dialog is its own scope, so
// nothing behind it (the selected session, the right sidebar) can make the read's answer stale.
function dlOnLaunch(panel, cx) {
  const dl = panel.dl;
  const ns = dl.ns;
  ns.attempted = true;
  if (ns.sending || ns.reading) return;
  const form = ns.form;
  const plan = launchPlan(form, { stdin: STDIN_SUPPORTED });
  if (plan.action === "blocked") { dlFocusProblem(panel, plan.first); cx.notify(); return; }
  if (panel.gate(panel.model()).disabled) { cx.notify(); return; }
  if (plan.action === "send") { dlSendLaunch(panel, cx, { form, kind: "auto" }); return; }
  if (plan.dialog === "yolo") { dlOpenLaunchConfirm(panel, cx, "yolo", null, form); return; }
  ns.reading = true;
  ns.banner = null;
  cx.notify();
  cx.spawn(async (cx2) => {
    try {
      const detail = await dlLoadProfile(panel, cx2, form.profile);
      const next = launchReadResult({
        scope: dl.name, current: dl.name, sameForm: ns.form === form, dialogOpen: dl.confirm !== null || dl.state.discarding, error: detail.error ?? null,
      });
      if (next.action === "open") dlOpenLaunchConfirm(panel, cx2, "ask", detail, form);
      else ns.banner = { kind: "error", text: next.text };
    } finally {
      ns.reading = false;
      cx2.notify();
    }
  });
}

// The confirmation shows, and Confirm sends, `form`: the snapshot taken when Launch was pressed.
function dlOpenLaunchConfirm(panel, cx, kind, detail, form) {
  const dl = panel.dl;
  const profile = detail ?? dlProfileOf(panel, form.profile) ?? { name: form.profile };
  dl.ins.typed.set_value("");
  const build = () => launchDialog(kind, {
    profile: profile.error ? (kind === "yolo" ? { name: form.profile } : null) : profile, provider: resolvedProvider(form, profile.error ? null : profile),
    override: form.allowedTools.length > 0 ? form.allowedTools : null, typed: dl.ins.typed.value(), sessionName: form.sessionName,
  });
  dlAskConfirm(panel, cx, build, (cx2) => dlSendLaunch(panel, cx2, { form, kind }));
}

// Sends one POST /sessions. A new Launch makes a new idempotency key; Retry (after a lost answer) sends the same
// submission again with the same key, so the server returns the one terminal it already made. Only a click gets
// here, never a tick. A new launch is sent from `form` (the snapshot) after launchConfirm runs the plan on it
// again; a Retry sends the submission of the lost answer as it was. The launch is a write: the frame is told, so
// closing is refused until the answer comes or the limit has passed.
function dlSendLaunch(panel, cx, { retry = false, form = null, kind = null } = {}) {
  const dl = panel.dl;
  const ns = dl.ns;
  if (ns.sending) return;
  if (panel.gate(panel.model(), { retry }).disabled) { cx.notify(); return; }
  if (retry ? !ns.submission : !form) return;
  if (!retry) {
    const check = launchConfirm(form, kind, { stdin: STDIN_SUPPORTED }, kind === "yolo" ? dl.ins.typed.value() : "");
    if (!check.send) { ns.banner = { kind: "error", text: check.reason }; cx.notify(); return; }
  }
  const submission = retry ? ns.submission : withKey(form, () => dlUuid());
  const request = buildRequest(submission, { resolved: resolvedProvider(submission, dlProfileOf(panel, submission.profile)), stdin: STDIN_SUPPORTED });
  Object.assign(ns, { submission, sending: true, retry: false, banner: null, serverError: null });
  dialogWriteStart(panel, cx, DL_LAUNCH_SECONDS);
  cx.spawn(async (cx2) => {
    const outcome = await callCao(dlExec, panel.cfg(panel.model()), { ...request, maxTime: DL_LAUNCH_SECONDS });
    panel.views.launch = applyOutcome(panel.views.launch, outcome, Date.now());
    dlAfterLaunch(panel, cx2, launchOutcome(outcome, submission), submission);
    cx2.notify();
  });
}

// What the form does with the answer. A created session is selected behind the card and the form's card replaces the
// form; anything else keeps every value, and only a lost answer keeps the key.
function dlAfterLaunch(panel, cx, result, submission) {
  const dl = panel.dl;
  const ns = dl.ns;
  ns.sending = false;
  ns.submission = keepSubmission(submission, result);
  const after = launchAfter(result, AUTH_MESSAGE);
  if (after.effect === "created") {
    panel.store.setLocal("caowin.lastWorkdir", submission.workdir);
    dl.created = { result, form: submission };
    panel.store.set(DL_SELECTION_KEY, { kind: "session", name: result.session });
    dialogEvent(panel, cx, { type: "saved" });
    return;
  }
  dialogWriteEnd(panel, cx);
  if (after.effect === "retry") {
    ns.retry = true;
    ns.banner = { kind: "retry", text: after.text };
  } else if (after.effect === "inline") {
    ns.serverError = { field: after.field, message: after.message };
    dlFocusProblem(panel, { field: after.field });
  } else {
    ns.banner = { kind: "error", text: after.text };
  }
}

// The card of a created session: its name, the tools the server recorded, whether a first message went, and where the
// first reply stands (from Herdr's push data, no polling). Its Close button is the dialog's own.
function dlResultCard(panel, t, m) {
  const c = panel.dl.created;
  const s = m.sessions.find((x) => x.name === c.result.session);
  const card = launchResultCard(c.result, c.form, s?.talkable[0] ?? s?.terms[0]);
  return dlCol().id("cao-result-card").gap(4).p(10).rounded(8).border_1().border_color(t.accent).bg(t.surface)
    .child(dlDiv().font_semibold().text_size(13).child(card.title))
    .child(dlDiv().text_size(12).child(`Allowed tools recorded by the server: ${card.toolsText}`))
    .child(dlDiv().text_size(12).child(card.messageLine))
    .when(Boolean(card.replyLine), (e) => e.child(dlDiv().text_size(12).text_color(t.muted).child(card.replyLine)));
}

function dlCopyCommand(panel, cx, reveal) {
  const ns = panel.dl.ns;
  const form = ns.form;
  cx.write_to_clipboard(equivalentCommand(form, { resolved: resolvedProvider(form, dlProfileOf(panel, form.profile)), reveal }));
  ns.copied = reveal ? "values" : "masked";
  cx.timer.after(DL_COPIED_MS, (cx2) => { ns.copied = false; cx2.notify(); });
  cx.notify();
}

function dlSessionBody(panel, t, m) {
  const dl = panel.dl;
  if (dl.created !== null) return dlResultCard(panel, t, m);
  const ns = dl.ns;
  const ins = dl.ins;
  const form = ns.form;
  const profile = dlProfileOf(panel, form.profile);
  const resolved = resolvedProvider(form, profile);
  const gate = panel.gate(m);
  const check = launchBlocked(form, { stdin: STDIN_SUPPORTED });
  const shown = ns.attempted ? check.problems : [];
  const at = (f) => shown.find((p) => p.field === f)?.message;
  const served = (f) => (ns.serverError?.field === f ? ns.serverError.message : null);
  const nameErr = nameCheck(form.sessionName).message ?? at("sessionName") ?? served("sessionName");
  const workdirErr = (ns.attempted ? workdirCheck(form.workdir).message : null) ?? served("workdir");
  const envErrors = envCheck(form.env).errors;
  const providers = providerChoices(panel.views.providers?.data, profile);
  const filledEnv = form.env.some((r) => r.key !== "" || r.value !== "");
  return dlCol().id("cao-new-session").gap(12)
    .child(dlDiv().flex_none().text_size(10.5).text_color(t.dim).child("Global: not tied to the open session"))
    .when(Boolean(ns.banner), (e) => e.child(dlCol().gap(6).child(problemLine(t, ns.banner.text))
      .when(ns.retry, (x) => x.child(dlRow().child(pushButton(t, "cao-launch-retry", "Retry", { kind: "primary", disabled: ns.sending, on: (cx) => dlSendLaunch(panel, cx, { retry: true }) }))))))
    .when(gate.disabled, (e) => e.child(problemLine(t, gate.text)))
    .child(dlProfileCombo(panel, t, "cao-f-profile", "--agents", ins.profileFilter, form.profile, at("profile") ?? served("profile"), (cx, name) => dlChooseProfile(panel, cx, name)))
    .child(fieldBox(t, "Session name", "--session-name", inputBox(t, "cao-f-name", ins.sessionName, Boolean(nameErr)),
      { hint: form.sessionName && !nameErr ? `Starts as ${nameCheck(form.sessionName).preview}` : "Leave empty for a generated name", error: nameErr }))
    .child(fieldBox(t, "Provider", "--provider",
      dlCol().gap(6)
        .child(dlRow().gap(6).flex_wrap()
          .children(providers.options.map((o) => chip(t, `cao-pv-${o.name}`, o.label, form.provider === o.name, (cx) => { dlSetField(panel, "provider", form.provider === o.name ? "" : o.name); cx.notify(); }))))
        .child(dlDiv().text_size(11.5).text_color(t.muted).child(form.provider ? `Set here: ${form.provider}` : providers.defaultText)),
      { hint: providers.options.length === 0 ? "The provider list could not be read; the default applies" : null }))
    .child(fieldBox(t, "Working directory", "--working-directory", inputBox(t, "cao-f-workdir", ins.workdir, Boolean(workdirErr)),
      { hint: "A directory on the CAO machine", error: workdirErr }))
    .child(fieldBox(t, "Message", "MESSAGE", textareaBox(t, "cao-f-message", ins.message, 4), { hint: "Sent to the agent when the session starts" }))
    .child(choiceRow(t, "cao-f-async", false, form.async, "Do not wait for the reply", "--async: the command line returns once the message is delivered", (cx) => { dlSetField(panel, "async", !form.async); cx.notify(); }))
    .child(fieldBox(t, "Tool policy", null, dlCol().gap(8)
      .child(choiceRow(t, "cao-p-ask", true, form.policy === "ask", "Ask first", "Shows the profile's declared tools and the provider's enforcement before launching", (cx) => { dlSetField(panel, "policy", "ask"); cx.notify(); }))
      .child(choiceRow(t, "cao-p-auto", true, form.policy === "auto", "Auto-approve", "--auto-approve: skips that confirmation; the tool policy is unchanged", (cx) => { dlSetField(panel, "policy", "auto"); cx.notify(); }))
      .child(choiceRow(t, "cao-p-yolo", true, form.policy === "yolo", "YOLO", "--yolo: every tool allowed, no confirmation but your typed session name", (cx) => { dlSetField(panel, "policy", "yolo"); cx.notify(); }, true))))
    .child(dlRow().id("cao-adv-toggle").gap(6).items_center().cursor_pointer().on_click((_e, cx) => { ns.advanced = !ns.advanced; cx.notify(); })
      .child(dlDiv().w(12).text_color(t.dim).child(ns.advanced ? "\u25be" : "\u25b8")).child(dlDiv().text_size(12).font_semibold().child("Advanced")))
    .when(ns.advanced, (e) => e.child(dlCol().gap(12).pl(18)
      .child(fieldBox(t, "Allowed tools", "--allowed-tools", inputBox(t, "cao-f-tools", ins.tools),
        { hint: form.allowedTools.length > 0 ? "Replaces the profile's tool policy" : "Comma separated; empty keeps the profile's policy" }))
      .child(choiceRow(t, "cao-f-memory", false, form.memory, "Memory manager", "--memory: also starts a context-manager terminal", (cx) => { dlSetField(panel, "memory", !form.memory); cx.notify(); }))
      .child(fieldBox(t, "Environment", "--env KEY=VALUE", dlCol().gap(6)
        .children(ns.env.map((r, i) => {
          const rowErrors = envErrors.filter((x) => x.row === i);
          return dlCol().gap(3)
            .child(dlRow().gap(6).items_center()
              .child(dlDiv().w(120).flex_none().child(inputBox(t, `cao-env-k-${i}`, r.key, rowErrors.some((x) => x.field === "key"))))
              .child(dlDiv().flex_1().min_w(0).child(inputBox(t, `cao-env-v-${i}`, r.value, rowErrors.some((x) => x.field === "value"))))
              .child(pushButton(t, `cao-env-x-${i}`, "\u00d7", { on: (cx) => dlRemoveEnvRow(panel, cx, i) })))
            .children(rowErrors.map((x) => problemLine(t, x.message)));
        }))
        .child(dlRow().child(pushButton(t, "cao-env-add", "Add variable", { on: (cx) => dlAddEnvRow(panel, cx) })))
        .when(filledEnv && !STDIN_SUPPORTED, (x) => x.child(dlDiv().text_size(11.5).text_color(t.blocked)
          .child("env values are not sent from this window: run the command below in a terminal on the CAO machine instead."))),
        { hint: "Values never leave this window; the command below carries them" }))))
    .child(dlCol().gap(6).p(8).rounded(6).bg(t.surface)
      .child(heading(t, "Equivalent command", "what the CLI would run"))
      .child(dlDiv().text_size(11.5).text_color(t.muted).child(equivalentCommand(form, { resolved })))
      .child(dlRow().gap(8).items_center()
        .child(pushButton(t, "cao-cmd-copy", "Copy", { on: (cx) => dlCopyCommand(panel, cx, false) }))
        .when(filledEnv, (x) => x.child(pushButton(t, "cao-cmd-copy-values", "Copy with values", { on: (cx) => dlCopyCommand(panel, cx, true) })))
        .when(Boolean(ns.copied), (x) => x.child(dlDiv().text_size(11).text_color(t.dim).child(ns.copied === "values" ? "Copied, values included" : "Copied, values masked")))));
}

// The bottom row of the form: Launch, and the note that env values are not sent. After a launch the row holds the
// dialog's Close button alone; under a confirmation Launch is dimmed.
function dlSessionButtons(panel, t, m, locked) {
  const dl = panel.dl;
  if (dl.created !== null) return [];
  const ns = dl.ns;
  const gate = panel.gate(m);
  const check = launchBlocked(ns.form, { stdin: STDIN_SUPPORTED });
  const label = ns.sending ? "Starting..." : ns.reading ? "Reading the profile..." : "Launch";
  return [
    pushButton(t, "cao-launch", label, { kind: "primary", disabled: locked || gate.disabled || check.useCli || ns.sending || ns.reading, on: (cx) => dlOnLaunch(panel, cx) }),
    ...(check.useCli ? [dlDiv().flex_1().min_w(0).text_size(11).text_color(t.blocked).child("env values are not sent from this window")] : []),
  ];
}

// ---------- New flow and Edit flow ----------

// The form's plain state (`nf`) and its retained text states (`fin`). New flow starts empty (or preset to the workflow of
// a Schedule... request); Edit flow starts unloaded: the dialog reads GET /flows/<the item of its request> itself, so it
// holds the flow it was opened for whatever the sidebar shows afterwards, and its Save sends only what changed
// against that read.
function dlFlowInit(panel) {
  const dl = panel.dl;
  const request = dl.request;
  const editName = request.kind === "edit-flow" && typeof request.item === "string" && request.item !== "" ? request.item : null;
  const form = emptyFlowForm({ workflow: request.workflow });
  dl.nf = {
    editName, form, initial: form, touched: {}, attempted: false, checks: null, checkedKey: null, status: null, checking: false, again: false, timer: null,
    sending: false, banner: null, serverErrors: {}, note: null, priming: false, marks: {}, waiters: [], job: null,
    flow: null, loaded: editName === null, loading: false, loadError: null,   // Edit: the flow as read, and how the read went
  };
  dl.fin = {};
  const text = (name, state, key) => {
    state.on("change", (_e, cx) => { if (!dl.nf.priming && key) dlFlowChanged(panel, cx, key, state.value()); });
    dl.fin[name] = state;
  };
  if (editName === null) text("name", dlInputState.new({ placeholder: "weekday-cr-sweep" }), "name");
  text("schedule", dlInputState.new({ placeholder: "0 9 * * mon-fri" }), "schedule");
  text("profile", dlInputState.new({ placeholder: "Type to filter the profiles" }), null);
  text("prompt", dlTextareaState.new({ placeholder: "What the agent should do on each run", rows: 4 }), "prompt");
  dlComboEvents(panel, dl.fin.profile, () => dl.nf.form.profile);
  if (editName === null) {
    dl.fin.draft = dlTextareaState.new({ placeholder: "Describe the schedule you want", rows: 3 });
    dl.nf.job = dl.job = dlNewJob("flow", dl.nf, dl.fin.draft);
  }
}

// The first reads: the profiles, the providers and (New flow) the workflows, and for Edit the flow itself. Reads only:
// none of them blocks closing. An answer that fails is shown with Try again and never kept.
function dlFlowLoad(panel, cx) {
  const nf = panel.dl.nf;
  dlLoadFormData(panel, cx);
  if (nf.editName === null || nf.loading) return;
  nf.loading = true;
  nf.loadError = null;
  cx.spawn(async (cx2) => {
    try {
      const route = flowRoute(nf.editName);
      const outcome = route === null ? { kind: "refused", message: "that schedule name cannot be looked up from this window" } : await callCao(dlExec, panel.cfg(panel.model()), { path: route, maxTime: 8 });
      panel.views.flow = applyOutcome(panel.views.flow, outcome, Date.now());
      if (outcome.kind === "ok" && outcome.json !== null && typeof outcome.json === "object" && !Array.isArray(outcome.json)) dlPrimeEdit(panel, cx2, outcome.json);
      else nf.loadError = outcome.kind === "unreachable" ? "CAO did not answer" : String(outcome.detail ?? outcome.message ?? "the schedule could not be read");
    } finally {
      nf.loading = false;
      cx2.notify();
    }
  });
}

// Edit: the flow's values go into the form's own text states, without the change handlers running.
function dlPrimeEdit(panel, cx, flow) {
  const { nf, fin } = panel.dl;
  const form = flowFormOf(flow);
  nf.priming = true;
  fin.schedule.set_value(form.schedule);
  fin.prompt.set_value(form.prompt);
  fin.profile.set_value(form.profile);
  nf.priming = false;
  Object.assign(nf, { flow, form, initial: form, loaded: true, loadError: null });
  cx.spawn((cx2) => dlLoadProfile(panel, cx2, form.profile).then(() => cx2.notify()));
}

// Whether the form holds something typed: the form against what it opened with, or text in the profile field that
// was not chosen. An Edit that has not loaded holds nothing.
function dlFlowDirty(panel) {
  const { nf, fin } = panel.dl;
  if (!nf.loaded) return false;
  return flowFormDirty(nf.form, nf.initial) || unchosenProfileText(fin.profile.value(), nf.form.profile) || (nf.job !== null && draftFormDirty(fin.draft.value(), nf.job.state));
}

// A field changed: store it, clear the server's complaint about it, and ask the server to check again shortly.
function dlFlowChanged(panel, cx, key, value) {
  const nf = panel.dl.nf;
  nf.form = { ...nf.form, [key]: value };
  nf.marks = clearMark(nf.marks, key);
  nf.touched = { ...nf.touched, [key]: true };
  if (nf.serverErrors[key]) nf.serverErrors = { ...nf.serverErrors, [key]: [] };
  nf.timer?.cancel();
  nf.timer = cx.timer.after(DL_CHECK_DEBOUNCE_MS, (cx2) => dlRunCheck(panel, cx2));
  cx.notify();
}

// The fields as the routes take them (the provider falls back to the profile's, a workflow's prompt is generated).
function dlFlowFieldsNow(panel) {
  const nf = panel.dl.nf;
  return flowFields(nf.form, dlProfileOf(panel, nf.form.profile));
}

// POST /flows/validate for the form as it is now: a read that writes nothing. One at a time; a change meanwhile asks
// for one more run. `cx` can be any context that spawns.
function dlRunCheck(panel, cx, force = false, onDone = null) {
  const nf = panel.dl.nf;
  if (onDone) nf.waiters.push(onDone);
  if (nf.checking) { nf.again = true; return; }
  const fields = dlFlowFieldsNow(panel);
  if (!force && fields.name === "" && fields.schedule === "") return;
  nf.checking = true;
  const key = flowCheckKey(fields);
  cx.spawn(async (cx2) => {
    try {
      const outcome = await callCao(dlExec, panel.cfg(panel.model()), { method: "POST", path: "/flows/validate", body: validateBody(fields), maxTime: 8 });
      panel.views.flowCheck = applyOutcome(panel.views.flowCheck, outcome, Date.now());
      Object.assign(nf, validateResult(outcome, { editing: nf.editName ?? undefined, name: fields.name }));
      nf.checkedKey = key;
      nf.note = validateNote(outcome);
    } finally {
      nf.checking = false;
      if (nf.again) { nf.again = false; dlRunCheck(panel, cx2); }
      else {
        if (nf.attempted) dlFocusFlowError(panel);
        for (const done of nf.waiters.splice(0)) done(nf);
      }
      cx2.notify();
    }
  });
}

// The errors of the form by field: the server's check of the last fields it saw, then what a save answered.
function dlFlowErrors(nf) {
  const checked = formErrorsOf(nf.checks?.byField);
  const merged = {};
  for (const src of [checked, nf.serverErrors]) for (const [k, list] of Object.entries(src)) if (list.length) merged[k] = [...(merged[k] ?? []), ...list];
  return merged;
}

function dlFocusFlowError(panel) {
  const { nf, fin } = panel.dl;
  const field = firstErrorField(shownErrors(dlFlowErrors(nf), nf.touched, true));
  ({ name: fin.name, schedule: fin.schedule, profile: fin.profile, prompt: fin.prompt })[field]?.focus();
}

function dlChooseFlowProfile(panel, cx, name) {
  const dl = panel.dl;
  dl.fin.profile.set_value(name);
  dlFlowChanged(panel, cx, "profile", name);
  dl.combo.open = false;
  cx.spawn((cx2) => dlLoadProfile(panel, cx2, name));
}

// Create, Create and enable..., or Save. With a problem it shows every error and takes the keys to the first one (and
// sends nothing); otherwise it sends, after the confirmation for Create and enable, which is drawn inside the dialog.
function dlAttemptFlowSave(panel, cx, enable) {
  const nf = panel.dl.nf;
  const fields = dlFlowFieldsNow(panel);
  nf.attempted = true;
  const gate = panel.gate(panel.model());
  const routes = routesState(nf.status);
  const state = flowFormGate({ fields, checks: nf.checks, checkedKey: nf.checkedKey, routes, gate, sending: nf.sending });
  const step = flowSaveAttempt({ gate, state, enable, routesMissing: routes.missing, sending: nf.sending });
  if (step.action === "gated") { cx.notify(); return; }
  if (step.action === "problems") {
    if (step.banner) nf.banner = { text: step.banner };
    if (step.runCheck) { nf.timer?.cancel(); dlRunCheck(panel, cx, true); }
    dlFocusFlowError(panel);
    cx.notify();
    return;
  }
  if (step.action === "confirm") dlAskConfirm(panel, cx, () => flowDialogSpec("createAndEnable", fields.name), (cx2) => dlSendFlowSave(panel, cx2, true, fields));
  else dlSendFlowSave(panel, cx, false);
}

// POST /flows (always with enabled: false unless Create and enable was confirmed) or PUT /flows/{name} with only what
// changed against the flow the dialog read (nothing changed, nothing sent). `snap` is the fields the confirmation was
// opened for (Create and enable): they are sent, after the form's own gate is asked about them again. A save is a
// write: the frame is told, so closing is refused until the answer comes or the limit has passed.
function dlSendFlowSave(panel, cx, enable, snap = null) {
  const nf = panel.dl.nf;
  const gate = panel.gate(panel.model());
  if (nf.sending || gate.disabled) return;
  const fields = snap ?? dlFlowFieldsNow(panel);
  const state = flowFormGate({ fields, checks: nf.checks, checkedKey: nf.checkedKey, routes: routesState(nf.status), gate, sending: nf.sending });
  if (!state.create) { nf.banner = { text: "The form changed after you chose this. Check it and try again." }; cx.notify(); return; }
  const mode = nf.editName ? "edit" : "create";
  let spec;
  if (mode === "create") spec = { method: "POST", path: "/flows", body: flowCreateBody(fields, { enable }), maxTime: DL_FLOW_SAVE_SECONDS, cli: FLOW_CLI };
  else {
    const body = flowUpdateBody(nf.flow ?? {}, fields);
    if (Object.keys(body).length === 0) { dlCloseUntouched(panel, cx); return; }
    spec = { method: "PUT", path: flowRoute(nf.editName), body, maxTime: DL_FLOW_SAVE_SECONDS, cli: FLOW_CLI };
  }
  nf.sending = true;
  nf.banner = null;
  dialogWriteStart(panel, cx, DL_FLOW_SAVE_SECONDS);
  cx.spawn(async (cx2) => {
    const outcome = await callCao(dlExec, panel.cfg(panel.model()), spec);
    panel.views.flowSave = applyOutcome(panel.views.flowSave, outcome, Date.now());
    nf.sending = false;
    dlAfterFlowSave(panel, cx2, flowSaveOutcome(outcome, { mode, enable }), nf, fields.name);
    cx2.notify();
  });
}

// An Edit with nothing changed sends nothing and closes the dialog, as an untouched form closes.
function dlCloseUntouched(panel, cx) {
  const dl = panel.dl;
  dl.state = dialogStep(dl.state, { type: "dirty", dirty: false }).state;
  dialogEvent(panel, cx, { type: "close" });
}

// A saved flow is announced to the top bar (the stamp, so its list is read again at once), keeps its armed warning, and
// is shown in the right sidebar by the reducer's effects: they set the sidebar's state and close the dialog. Any other
// answer ends the write and keeps every value, with the errors at their fields.
function dlAfterFlowSave(panel, cx, r, nf, name) {
  const fx = flowSaveEffects(r, { name, authText: AUTH_MESSAGE });
  if (fx.done) {
    panel.store.set(DL_FLOWS_STAMP_KEY, Date.now());
    if (fx.warning) dlNotify({ title: "CAO", body: fx.warning, level: "warning" });
    dialogEvent(panel, cx, { type: "saved", list: "flows", item: fx.selectName });
    return;
  }
  dialogWriteEnd(panel, cx);
  nf.serverErrors = fx.serverErrors;
  if (fx.banner) nf.banner = { text: fx.banner };
  dlFocusFlowError(panel);
}

function dlFlowBody(panel, t, m) {
  const dl = panel.dl;
  const nf = dl.nf;
  const editing = nf.editName !== null;
  if (!nf.loaded) {
    return dlCol().id("cao-flow-form").gap(12)
      .child(nf.loadError === null ? dlDiv().text_size(12).text_color(t.dim).child("Loading the schedule...") : problemLine(t, nf.loadError))
      .when(nf.loadError !== null, (e) => e.child(dlRow().child(pushButton(t, "cao-nf-reread", "Try again", { on: (cx) => { dlFlowLoad(panel, cx); cx.notify(); } }))));
  }
  const form = nf.form;
  const set = dl.fin;
  const profile = dlProfileOf(panel, form.profile);
  const fields = dlFlowFieldsNow(panel);
  const gate = panel.gate(m);
  const routes = routesState(nf.status);
  const errors = shownErrors(dlFlowErrors(nf), nf.touched, nf.attempted);
  const at = (k) => errors[k]?.[0];
  const ok = flowFormGate({ fields, checks: nf.checks, checkedKey: nf.checkedKey, routes, gate, sending: nf.sending });
  const providers = providerChoices(panel.views.providers?.data, profile);
  const workflows = workflowChoices(panel.views.workflows?.data);
  const weekday = numericWeekdayWarning(form.schedule);
  const runs = (nf.checks?.nextRuns ?? []).map((r) => runWords(r));
  return dlCol().id("cao-flow-form").gap(12)
    .when(!editing, (e) => e.child(dlDiv().flex_none().text_size(10.5).text_color(t.dim).child("Global: not tied to the open session")))
    .when(Boolean(nf.banner), (e) => e.child(problemLine(t, nf.banner.text)))
    .when(gate.disabled, (e) => e.child(problemLine(t, gate.text)))
    .when(!editing, (e) => e.child(dlDraftBox(panel, t, nf, gate)))
    .when(!editing && form.kind === "workflow" && Boolean(form.workflow), (e) => e.child(dlDiv().text_size(12).text_color(t.muted)
      .child(`Schedule... creates a schedule that runs workflow ${form.workflow}. The workflow does not change.`)))
    .when(!editing, (e) => e.child(fieldBox(t, "Name", markedFlag(null, nf.marks, "name"), inputBox(t, "cao-nf-name", set.name, Boolean(at("name"))),
      { hint: "Letters, digits, - and _ (1-64)", error: at("name") })))
    .when(!editing, (e) => e.child(fieldBox(t, "What it runs", null, dlRow().gap(6)
      .child(chip(t, "cao-nf-kind-prompt", "An agent prompt", form.kind === "prompt", (cx) => { dlFlowChanged(panel, cx, "kind", "prompt"); }))
      .child(chip(t, "cao-nf-kind-workflow", "A workflow", form.kind === "workflow", (cx) => { dlFlowChanged(panel, cx, "kind", "workflow"); })))))
    .child(fieldBox(t, "Schedule", markedFlag("5-field cron, server local time", nf.marks, "schedule"), dlCol().gap(6)
      .child(inputBox(t, "cao-nf-schedule", set.schedule, Boolean(at("schedule"))))
      .child(dlRow().gap(6).flex_wrap().children(FLOW_PRESETS.map((p) => chip(t, `cao-nf-preset-${p.cron.replace(/[^a-z0-9]/gi, "_")}`, p.label, form.schedule === p.cron, (cx) => {
        set.schedule.set_value(p.cron); dlFlowChanged(panel, cx, "schedule", p.cron);
      })))),
      { error: at("schedule") }))
    .when(Boolean(weekday), (e) => e.child(dlDiv().id("cao-nf-weekday").text_size(11.5).text_color(t.blocked).child(weekday)))
    .child(dlCol().id("cao-nf-runs").gap(2)
      .child(heading(t, "Next 3 runs", "from the server's scheduler"))
      .children(runs.map((r) => dlDiv().text_size(12).child(r)))
      .when(runs.length === 0 && !routes.missing, (e) => e.child(hintLine(t, form.schedule ? "No runs yet: the server has not read this schedule" : "Type a schedule to see its next runs")))
      .when(routes.missing, (e) => e.child(dlDiv().text_size(11.5).text_color(t.blocked).child(routes.nextRunsNote))))
    .child(dlProfileCombo(panel, t, "cao-nf-profile", markedFlag(null, nf.marks, "profile"), set.profile, form.profile, at("profile"), (cx, name) => dlChooseFlowProfile(panel, cx, name)))
    .child(fieldBox(t, "Provider", markedFlag(null, nf.marks, "provider"), dlCol().gap(6)
      .child(dlRow().gap(6).flex_wrap().children(providers.options.map((o) => chip(t, `cao-nf-pv-${o.name}`, o.label, form.provider === o.name, (cx) => { dlFlowChanged(panel, cx, "provider", form.provider === o.name ? "" : o.name); }))))
      .child(dlDiv().text_size(11.5).text_color(t.muted).child(form.provider ? `Set here: ${form.provider}` : providers.defaultText)),
      { error: at("provider") }))
    .when(form.kind === "prompt" || editing, (e) => e.child(fieldBox(t, "Prompt", markedFlag("[[var]] placeholders allowed", nf.marks, "prompt"), textareaBox(t, "cao-nf-prompt", set.prompt, 4), { error: at("prompt") })))
    .when(form.kind === "workflow" && !editing, (e) => e.child(dlCol().gap(8)
      .child(fieldBox(t, "Workflow", "from GET /workflows", dlRow().gap(6).flex_wrap()
        .children(workflows.map((w) => chip(t, `cao-nf-wf-${w}`, w, form.workflow === w, (cx) => { dlFlowChanged(panel, cx, "workflow", w); })))
        .when(workflows.length === 0, (x) => x.child(hintLine(t, "no workflows yet"))), { error: at("prompt") }))
      .child(dlDiv().text_size(11.5).text_color(t.blocked).child(scheduleWarning()))
      .child(fieldBox(t, "Prompt the schedule sends", "generated", dlDiv().id("cao-nf-generated").p(8).rounded(6).bg(t.surface).text_size(12).child(fields.prompt || "(pick a workflow)")))))
    .when(Boolean(nf.note), (e) => e.child(problemLine(t, nf.note)))
    .when(errors.form?.length > 0, (e) => e.child(dlCol().gap(2).children(errors.form.map((x) => problemLine(t, x)))))
    .when(!editing && Boolean(ok.hint), (e) => e.child(problemLine(t, ok.hint)))
    .when(!editing, (e) => e.child(hintLine(t, "Creates the schedule disabled: enable it on purpose from its details.")));
}

// The bottom row of the form: Create flow (or Save changes), Create and enable... and what keeps Create off. Under a
// confirmation they are dimmed; an Edit that has not been read has none.
function dlFlowButtons(panel, t, m, locked) {
  const nf = panel.dl.nf;
  if (!nf.loaded) return [];
  const editing = nf.editName !== null;
  const gate = panel.gate(m);
  const routes = routesState(nf.status);
  const ok = flowFormGate({ fields: dlFlowFieldsNow(panel), checks: nf.checks, checkedKey: nf.checkedKey, routes, gate, sending: nf.sending });
  return [
    pushButton(t, "cao-nf-create", nf.sending ? "Sending..." : editing ? "Save changes" : "Create schedule",
      { kind: "primary", disabled: locked || gate.disabled || nf.sending || (!editing && routes.missing), dim: !ok.create, on: (cx) => dlAttemptFlowSave(panel, cx, false) }),
    ...(editing ? [] : [pushButton(t, "cao-nf-create-enable", "Create and enable...", { disabled: locked || gate.disabled || nf.sending || routes.missing, dim: !ok.create, on: (cx) => dlAttemptFlowSave(panel, cx, true) })]),
    ...(ok.label ? [dlDiv().text_size(11).text_color(t.blocked).child(ok.label)] : []),
  ];
}

// ---------- New workflow and Edit workflow ----------

// The editor's plain state (`nw`) and its retained text states (`win`). Edit starts unloaded: the dialog reads
// GET /workflows/<the item of its request> itself, so it edits the workflow it was opened for whatever the sidebar
// shows afterwards, and Save sends the hash read then.
function dlWorkflowInit(panel) {
  const dl = panel.dl;
  const request = dl.request;
  const edit = request.kind === "edit-workflow";
  const editName = edit && typeof request.item === "string" && request.item !== "" ? request.item : null;
  const blank = emptyWorkflowForm();
  const form = { ...blank, name: editName ?? "" };
  dl.nw = {
    edit, editName, form, initial: form, check: null, validatedKey: null, result: null, validating: false, sending: false,
    loading: false, loaded: !edit, loadFailed: edit && editName === null, canRetry: false, banner: edit && editName === null ? "No workflow was named for this edit." : null,
    note: null, expectedHash: null, nameError: null, offerReload: false, attempted: false, priming: false, marks: {}, job: null,
  };
  dlWorkflowRecheck(dl.nw);
  dl.win = {};
  const text = (name, state, key) => {
    state.on("change", (_e, cx) => { if (!dl.nw.priming) dlWorkflowChanged(panel, cx, key, state.value()); });
    dl.win[name] = state;
  };
  if (!edit) text("name", dlInputState.new({ placeholder: "hello-review" }), "name");
  text("source", dlTextareaState.new({ placeholder: edit ? "" : "Pick a template, or paste the workflow here", rows: 16 }), "source");
  if (!edit) {
    dl.win.draft = dlTextareaState.new({ placeholder: "Describe the workflow you want", rows: 3 });
    dl.nw.job = dl.job = dlNewJob("workflow", dl.nw, dl.win.draft);
  }
}

// The size line and refusal for the text as it is now; computed when the text, name, tier or hash changes, not on every draw.
function dlWorkflowRecheck(nw) {
  nw.check = editorSizeCheck(nw.form, { stdin: STDIN_SUPPORTED, expectedHash: nw.expectedHash });
}

function dlWorkflowChanged(panel, cx, key, value) {
  const nw = panel.dl.nw;
  nw.form = { ...nw.form, [key]: value };
  nw.marks = clearMark(nw.marks, key);
  if (key === "name") nw.nameError = null;
  dlWorkflowRecheck(nw);
  cx.notify();
}

// Whether Validate and Save work now: the panel's write gate, the size rule, a clean validate of this exact text, and for
// Save the name rules (the spec's own name: line must agree).
function dlWorkflowGateNow(panel, m) {
  const nw = panel.dl.nw;
  const gate = workflowEditorGate({
    mode: nw.edit ? "edit" : "new", gate: panel.gate(m), check: nw.check, validating: nw.validating, sending: nw.sending, loading: nw.loading,
    name: nw.form.name, tier: nw.form.tier, source: nw.form.source, validatedKey: nw.validatedKey, result: nw.result, expectedHash: nw.expectedHash,
  });
  return editorGateWithName(gate, nw.form);
}

// Sets the editor's text states and plain state to `form` without the change handlers running.
function dlFillEditor(panel, cx, form, marks = {}) {
  const { nw, win } = panel.dl;
  nw.priming = true;
  win.name?.set_value(form.name);
  win.source.set_value(form.source);
  nw.priming = false;
  nw.form = form;
  nw.marks = marks;
  nw.nameError = null;
  dlWorkflowRecheck(nw);
  cx.notify();
}

// A template fills the tier, the source and (when empty) the name. It asks first, inside the dialog, when it would replace
// text the user typed.
function dlPickTemplate(panel, cx, tpl) {
  const nw = panel.dl.nw;
  if (nw.edit) return;
  if (templateNeedsConfirm(nw.form, TEMPLATES)) dlAskConfirm(panel, cx, () => workflowDialogSpec("template"), (cx2) => dlFillEditor(panel, cx2, templateFor(nw.form, tpl)));
  else dlFillEditor(panel, cx, templateFor(nw.form, tpl));
}

// Edit reads the workflow's source, tier and content hash when the dialog opens (a read: it does not block closing).
// The tier is the one editFromDocument reads from the answer.
function dlWorkflowLoad(panel, cx) {
  const nw = panel.dl.nw;
  if (!nw.edit) { dlLoadFormData(panel, cx); return; }     // New: the profiles and providers the Draft box checks against
  if (nw.editName === null || nw.loading) return;
  nw.loading = true;
  nw.loadFailed = false;
  nw.banner = null;
  const route = workflowRoute(nw.editName);
  cx.spawn(async (cx2) => {
    try {
      const outcome = route === null ? { kind: "refused", message: "that workflow name cannot be looked up from this window" } : await callCao(dlExec, panel.cfg(panel.model()), { path: route, maxTime: 10 });
      panel.views.workflowSpec = applyOutcome(panel.views.workflowSpec, outcome, Date.now());
      const doc = outcome.kind === "ok" ? editFromDocument(outcome.json, nw.editName) : null;
      if (doc?.ok) {
        const form = { name: nw.editName, tier: doc.tier, source: doc.source };
        nw.expectedHash = doc.hash;
        nw.initial = form;
        nw.validatedKey = null;
        nw.result = null;
        nw.offerReload = false;
        nw.loaded = true;
        dlFillEditor(panel, cx2, form);
      } else {
        nw.loadFailed = true;
        nw.canRetry = doc === null;
        nw.banner = doc ? doc.reason : readFailureText(outcome, "the workflow");
      }
    } finally {
      nw.loading = false;
      cx2.notify();
    }
  });
}

// Validate sends the text as it is now (POST /workflows/validate writes nothing, so it is a read). The answer belongs to
// that exact text and tier; a changed text needs a new check.
function dlOnWorkflowValidate(panel, cx) {
  const nw = panel.dl.nw;
  nw.attempted = true;
  if (!dlWorkflowGateNow(panel, panel.model()).validate) { cx.notify(); return; }
  dlRunWorkflowValidate(panel, cx);
}

function dlRunWorkflowValidate(panel, cx, after = null) {
  const nw = panel.dl.nw;
  const { tier, source } = nw.form;
  const key = workflowValidationKey(tier, source);
  nw.validating = true;
  nw.banner = null;
  nw.note = null;
  cx.notify();
  cx.spawn(async (cx2) => {
    try {
      const outcome = await callCao(dlExec, panel.cfg(panel.model()), { method: "POST", path: "/workflows/validate", body: { source, tier }, maxTime: 15, cli: "cao workflow validate" });
      panel.views.workflowCheck = applyOutcome(panel.views.workflowCheck, outcome, Date.now());
      nw.note = validateOutcomeNote(outcome);
      nw.result = outcome.kind === "ok" ? outcome.json : null;
      nw.validatedKey = outcome.kind === "ok" ? key : null;
    } finally {
      nw.validating = false;
      if (after) after(nw);
      cx2.notify();
    }
  });
}

// Save sends the text that was validated: POST /workflows for a new workflow, PUT /workflows/{name} with the hash read when
// the dialog opened. The button works only after a clean check of this exact text. A save is a write: the frame is told, so
// closing is refused until the answer comes or the limit has passed.
function dlSendWorkflowSave(panel, cx) {
  const nw = panel.dl.nw;
  nw.attempted = true;
  if (!dlWorkflowGateNow(panel, panel.model()).save) { cx.notify(); return; }
  const mode = nw.edit ? "edit" : "new";
  const name = mode === "edit" ? nw.editName : nw.form.name;
  let spec;
  try { spec = saveRequest({ mode, name, tier: nw.form.tier, source: nw.form.source, expectedHash: nw.expectedHash }); } catch (err) { nw.banner = String(err?.message ?? err); cx.notify(); return; }
  nw.sending = true;
  nw.banner = null;
  dialogWriteStart(panel, cx, DL_WORKFLOW_SAVE_SECONDS);
  cx.spawn(async (cx2) => {
    const outcome = await callCao(dlExec, panel.cfg(panel.model()), { ...spec, maxTime: DL_WORKFLOW_SAVE_SECONDS });
    panel.views.workflowSave = applyOutcome(panel.views.workflowSave, outcome, Date.now());
    nw.sending = false;
    dlAfterWorkflowSave(panel, cx2, workflowSaveEffects(workflowSaveResult(outcome, spec.method), { name, authText: AUTH_MESSAGE }), nw);
    cx2.notify();
  });
}

// A saved workflow is announced to the top bar (the stamp, so its list is read again at once) and shown in the right sidebar
// by the reducer's effects, which set the sidebar's state and close the dialog. Any other answer ends the write and keeps
// the text, with its banner, the message at the name, and Reload for a stale hash.
function dlAfterWorkflowSave(panel, cx, fx, nw) {
  if (fx.done) {
    panel.store.set(DL_WORKFLOWS_STAMP_KEY, Date.now());
    dialogEvent(panel, cx, { type: "saved", list: "workflows", item: fx.selectName });
    return;
  }
  dialogWriteEnd(panel, cx);
  nw.banner = fx.banner;
  nw.nameError = fx.nameError;
  nw.offerReload = fx.offerReload;
}

// Replacing the typed text with the server's version asks first, inside the dialog.
function dlAskReload(panel, cx) {
  const nw = panel.dl.nw;
  dlAskConfirm(panel, cx, () => workflowDialogSpec("reload"), (cx2) => { nw.loaded = false; nw.loading = false; dlWorkflowLoad(panel, cx2); });
}

function dlWorkflowDirty(panel) {
  const nw = panel.dl.nw;
  return nw.loaded && (workflowEditorDirty(nw.form, nw.initial) || (nw.job !== null && draftFormDirty(panel.dl.win.draft.value(), nw.job.state)));
}

function dlWorkflowBody(panel, t, m) {
  const dl = panel.dl;
  const nw = dl.nw;
  const editing = nw.edit;
  const form = nw.form;
  const set = dl.win;
  const gate = panel.gate(m);
  const g = dlWorkflowGateNow(panel, m);
  const key = workflowValidationKey(form.tier, form.source);
  const report = nw.result && nw.validatedKey === key ? validateReport(nw.result) : null;
  const nameText = nw.nameError ?? (form.name !== "" || nw.attempted ? g.nameProblem : null);
  if (editing && !nw.loadFailed && !nw.loaded) return dlCol().id("cao-workflow-form").gap(12).child(dlDiv().text_size(12).text_color(t.dim).child("Reading the workflow..."));
  if (editing && nw.loadFailed) {
    return dlCol().id("cao-workflow-form").gap(12).child(problemLine(t, nw.banner ?? "The workflow could not be read."))
      .when(nw.canRetry, (e) => e.child(dlRow().child(pushButton(t, "cao-wf-reread", "Try again", { on: (cx) => { dlWorkflowLoad(panel, cx); cx.notify(); } }))));
  }
  return dlCol().id("cao-workflow-form").gap(12)
    .when(!editing, (e) => e.child(dlDiv().flex_none().text_size(10.5).text_color(t.dim).child("Global: not tied to the open session")))
    .when(Boolean(nw.banner), (e) => e.child(dlCol().gap(6).child(problemLine(t, nw.banner))
      .when(nw.offerReload, (x) => x.child(dlRow().child(pushButton(t, "cao-wf-reload", "Reload", { on: (cx) => dlAskReload(panel, cx) }))))))
    .when(gate.disabled, (e) => e.child(problemLine(t, gate.text)))
    .when(!editing, (e) => e.child(dlDraftBox(panel, t, nw, gate)))
    .when(!editing, (e) => e.child(fieldBox(t, "Template", null, dlRow().gap(6).flex_wrap()
      .children(TEMPLATES.map((tpl) => chip(t, `cao-wf-tpl-${tpl.id}`, tpl.label, isTemplateText(form, tpl), (cx) => dlPickTemplate(panel, cx, tpl)))), { hint: TEMPLATE_NOTE })))
    .when(!editing, (e) => e.child(fieldBox(t, "Name", markedFlag(null, nw.marks, "name"), inputBox(t, "cao-wf-name", set.name, Boolean(nameText)), { hint: "A bare name: letters, digits, - and _", error: nameText })))
    .child(fieldBox(t, "Tier", markedFlag(null, nw.marks, "tier"), editing ? dlDiv().text_size(12).child(form.tier)
      : dlRow().gap(6).children(["python", "yaml"].map((x) => chip(t, `cao-wf-tier-${x}`, x, form.tier === x, (cx) => { dlWorkflowChanged(panel, cx, "tier", x); })))))
    .child(fieldBox(t, "Source", markedFlag(null, nw.marks, "source"), textareaBox(t, "cao-wf-source", set.source, 16), { hint: workflowFileHint() }))
    .child(dlDiv().id("cao-wf-size").text_size(11.5).text_color(nw.check?.ok === false ? t.blocked : t.muted).child(nw.check?.ok === false ? nw.check.message : `Size: ${nw.check?.sizeText ?? ""}`))
    .when(Boolean(g.specNameProblem), (e) => e.child(problemLine(t, g.specNameProblem)))
    .when(Boolean(nw.note), (e) => e.child(problemLine(t, nw.note)))
    .when(Boolean(report), (e) => e.child(dlCol().id("cao-wf-report").gap(3)
      .children(report.blocking.map((f) => problemLine(t, findingWords(f))))
      .children(report.warnings.map((f) => dlDiv().text_size(11.5).text_color(t.muted).child(`\u25b2 ${findingWords(f)}`)))
      .when(report.blocking.length === 0, (x) => x.child(dlDiv().text_size(11.5).text_color(t.accent).child(report.warnings.length > 0 ? "No blocking findings. Warnings do not stop you from saving." : "No findings.")))))
    .when(nw.result && !report, (e) => e.child(hintLine(t, "The text changed since the last check: validate it again.")));
}

// The bottom row of the editor: Validate and Save. Under a confirmation they are dimmed; an Edit that has not been read has none.
function dlWorkflowButtons(panel, t, m, locked) {
  const nw = panel.dl.nw;
  if (!nw.loaded) return [];
  const g = dlWorkflowGateNow(panel, m);
  return [
    pushButton(t, "cao-wf-validate", nw.validating ? "Checking..." : "Validate", { disabled: locked || !g.validate, on: (cx) => dlOnWorkflowValidate(panel, cx) }),
    pushButton(t, "cao-wf-save", nw.sending ? "Saving..." : nw.edit ? "Save changes" : "Save workflow", { kind: "primary", disabled: locked || !g.save, on: (cx) => dlSendWorkflowSave(panel, cx) }),
  ];
}

// ---------- Run ----------

// The Run form's state (`rf`). What the user types here (the inputs, the working directory) and the submission kept for
// "submit again" live in this object, in the dialog's memory, and nowhere else: no shared key, local-storage entry or
// file ever holds one: values stay in memory. The directory a launch used prefills the field; the form never
// writes it back. The typed fields are made on the first tick after the spec is read, from the tick (never from a draw).
function dlRunInit(panel) {
  const dl = panel.dl;
  const request = dl.request;
  const name = typeof request.item === "string" && request.item !== "" ? request.item : null;
  const saved = panel.store.getLocal("caowin.lastWorkdir");
  const prefill = typeof saved === "string" ? saved : "";
  dl.rf = {
    name, specState: name === null ? "failed" : "loading", spec: null, form: null, ins: new Map(), rows: [], prefill,
    workdir: dlInputState.new({ placeholder: "optional: the project root on the CAO machine", value: prefill }),
    sending: false, banner: name === null ? "No workflow was named for this run." : null, errors: {}, rowErrors: {}, attempted: false, record: null, copied: false,
  };
}

// The spec of the workflow, for the typed fields (a read: it does not block closing). A read that fails gives free rows
// labelled as unread.
function dlRunLoad(panel, cx) {
  const rf = panel.dl.rf;
  if (rf.name === null) return;
  const route = workflowRoute(rf.name);
  cx.spawn(async (cx2) => {
    const outcome = route === null ? { kind: "refused", message: "that workflow name cannot be looked up from this window" } : await callCao(dlExec, panel.cfg(panel.model()), { path: route, maxTime: 8 });
    panel.views.workflowSpec = applyOutcome(panel.views.workflowSpec, outcome, Date.now());
    const plain = outcome.kind === "ok" && outcome.json !== null && typeof outcome.json === "object" && !Array.isArray(outcome.json);
    rf.spec = plain ? outcome.json : null;
    rf.specState = plain ? "ok" : "failed";
    cx2.notify();
  });
}

function dlFreeRow() { return { key: dlInputState.new({ placeholder: "name" }), value: dlInputState.new({ placeholder: "value" }) }; }

// The fields as runFormModel gives them: typed by their declared type, or free key and value rows with a label.
function dlRunMakeFields(panel) {
  const rf = panel.dl.rf;
  rf.form = runFormModel(rf.spec, rf.specState);
  for (const f of rf.form.fields) rf.ins.set(f.name, dlInputState.new({ placeholder: f.default != null ? `default ${String(f.default)}` : f.type }));
  if (rf.form.free) rf.rows.push(dlFreeRow());
}

function dlRunTick(panel) {
  const rf = panel.dl.rf;
  if (rf.form === null && rf.specState !== "loading") dlRunMakeFields(panel);
}

// Submit reads the form as it is now, and sends that: typed fields by their declared type, or the free rows. With a problem it
// sends nothing and shows it at its field. Only a click gets here, never a tick.
function dlOnRunSubmit(panel, cx) {
  const rf = panel.dl.rf;
  if (rf.sending || rf.form === null || rf.name === null) return;
  rf.attempted = true;
  if (panel.gate(panel.model()).disabled) { cx.notify(); return; }
  const values = Object.fromEntries([...rf.ins].map(([k, st]) => [k, st.value()]));
  const read = runInputs(rf.form, values, rf.rows.map((r) => ({ key: r.key.value(), value: r.value.value() })));
  rf.errors = read.errors;
  rf.rowErrors = read.rowErrors;
  if (!runInputsOk(read)) { cx.notify(); return; }
  dlSendRun(panel, cx, { name: rf.name, inputs: read.inputs, workingDirectory: rf.workdir.value().trim() });
}

// "I approved it: submit again" sends the submission kept in this dialog as it was, not what the fields hold now.
function dlSubmitAgain(panel, cx) {
  const rf = panel.dl.rf;
  if (rf.record === null || rf.sending) return;
  dlSendRun(panel, cx, rf.record.submission);
}

// POST /workflows/runs:submit for `submission` (name, inputs, working directory). A submit is a write: the frame is told,
// so closing is refused until the answer comes or the limit has passed.
function dlSendRun(panel, cx, submission) {
  const rf = panel.dl.rf;
  if (rf.sending) return;
  if (panel.gate(panel.model()).disabled) { cx.notify(); return; }
  let request;
  try { request = submitRequest(submission.name, submission.inputs, submission.workingDirectory); } catch (err) { rf.banner = String(err?.message ?? err); cx.notify(); return; }
  rf.sending = true;
  rf.banner = null;
  dialogWriteStart(panel, cx, DL_RUN_SECONDS);
  cx.spawn(async (cx2) => {
    const outcome = await callCao(dlExec, panel.cfg(panel.model()), { ...request, maxTime: DL_RUN_SECONDS });
    panel.views.workflowRun = applyOutcome(panel.views.workflowRun, outcome, Date.now());
    rf.sending = false;
    dlAfterRun(panel, cx2, outcome, submission);
    cx2.notify();
  });
}

// What the answer does. A 202 clears the workflow's "not submitted" row, stamps the top bar's list and closes the dialog on
// the new run (the reducer's saved event). A 403 approval_required writes that row, from the plan id, the workflow's name and
// the time and nothing else, and keeps the form's own record (with the submission) in this dialog's memory for "submit again".
// Any other answer ends the write, keeps every value and says why.
function dlAfterRun(panel, cx, outcome, submission) {
  const rf = panel.dl.rf;
  const fx = runSubmitEffects(outcome, submission, { formHeld: true, recHeld: rf.record !== null, prior: rf.record });
  const rows = panel.store.get(DL_APPROVALS_KEY);
  if (fx.view.type === "approval") panel.store.set(DL_APPROVALS_KEY, recordApproval(rows, submission.name, outcome, Date.now()));
  if (fx.record.op === "set") rf.record = fx.record.value;
  if (fx.started) {
    rf.record = null;
    panel.store.set(DL_APPROVALS_KEY, clearApproval(rows, submission.name));
    panel.store.set(DL_WORKFLOWS_STAMP_KEY, Date.now());
    dialogEvent(panel, cx, { type: "saved", list: "workflows", item: submission.name });
    return;
  }
  dialogWriteEnd(panel, cx);
  rf.banner = fx.formBanner;
}

// Whether the form holds something typed: a value, a free row, or a directory other than the prefill. With the approval
// view showing there is nothing to lose: the row of "not submitted" stays and Run opens a new form.
function dlRunDirty(panel) {
  const rf = panel.dl.rf;
  if (rf.record !== null) return false;
  return [...rf.ins.values()].some((st) => st.value() !== "") || rf.rows.some((r) => r.key.value() !== "" || r.value.value() !== "") || rf.workdir.value() !== rf.prefill;
}

// The plan-approval view: approval is a terminal command, never a button here. Copy puts the command on the clipboard.
function dlApprovalCard(panel, t) {
  const rf = panel.dl.rf;
  const a = approvalView(rf.record.state);
  return dlCol().id("cao-approval").gap(6).p(10).rounded(8).border_1().border_color(t.blocked).bg(t.surface)
    .child(dlDiv().font_semibold().text_size(13).text_color(t.blocked).child(a.headline))
    .child(dlDiv().id("cao-approval-plan").text_size(12).child(a.planText))
    .when(Boolean(a.command), (e) => e.child(dlRow().gap(8).items_center().flex_wrap()
      .child(dlDiv().id("cao-approval-command").flex_1().min_w(0).p(6).rounded(6).bg(t.input).text_size(12).child(a.command))
      .child(pushButton(t, "cao-approval-copy", rf.copied ? "Copied" : a.copyLabel, { on: (cx) => {
        cx.write_to_clipboard(a.command);
        rf.copied = true;
        cx.timer.after(DL_COPIED_MS, (cx2) => { rf.copied = false; cx2.notify(); });
        cx.notify();
      } }))))
    .when(Boolean(a.serverMessage), (e) => e.child(hintLine(t, a.serverMessage)))
    .child(dlDiv().text_size(11.5).text_color(t.muted).child(`${a.localRun.status}: ${a.localRun.note}`));
}

function dlRunBody(panel, t, m) {
  const rf = panel.dl.rf;
  const gate = panel.gate(m);
  if (rf.name === null) return problemLine(t, rf.banner);
  if (rf.record !== null) {
    return dlCol().id("cao-run-approval").gap(10)
      .when(Boolean(rf.banner), (e) => e.child(problemLine(t, rf.banner)))
      .when(gate.disabled, (e) => e.child(problemLine(t, gate.text)))
      .child(dlApprovalCard(panel, t));
  }
  if (rf.form === null) return dlDiv().id("cao-run-reading").text_size(12).text_color(t.dim).child("Reading the workflow...");
  const form = rf.form;
  const shown = (k) => (rf.attempted && Object.hasOwn(rf.errors, k) ? rf.errors[k] : null);
  return dlCol().id("cao-run-form").gap(12)
    .child(dlDiv().flex_none().text_size(10.5).text_color(t.dim).child("Global: not tied to the open session"))
    .when(Boolean(rf.banner), (e) => e.child(problemLine(t, rf.banner)))
    .when(gate.disabled, (e) => e.child(problemLine(t, gate.text)))
    .when(form.free, (e) => e.child(dlCol().gap(6)
      .child(dlDiv().id("cao-run-undescribed").text_size(11.5).text_color(t.dim).child(form.label))
      .children(rf.rows.map((r, i) => dlCol().gap(3)
        .child(dlRow().gap(6).items_center()
          .child(dlDiv().w(120).flex_none().child(inputBox(t, `cao-run-k-${i}`, r.key, rf.attempted && Boolean(rf.rowErrors[i]))))
          .child(dlDiv().flex_1().min_w(0).child(inputBox(t, `cao-run-v-${i}`, r.value, rf.attempted && Boolean(rf.rowErrors[i])))))
        .when(rf.attempted && Boolean(rf.rowErrors[i]), (x) => x.child(problemLine(t, rf.rowErrors[i])))))
      .child(dlRow().child(pushButton(t, "cao-run-add-row", "Add input", { on: (cx) => { rf.rows.push(dlFreeRow()); cx.notify(); } })))))
    .children(form.fields.map((f) => fieldBox(t, f.name, f.required ? `${f.type}, required` : f.type, inputBox(t, `cao-run-f-${f.name}`, rf.ins.get(f.name), Boolean(shown(f.name))),
      { hint: f.type === "bool" ? "true or false" : f.type === "int" ? "a whole number" : null, error: shown(f.name) })))
    .child(fieldBox(t, "Working directory", "optional", inputBox(t, "cao-run-workdir", rf.workdir), { hint: "The project root of a script run, on the CAO machine" }));
}

// The bottom row of Run: Submit, or after plan approval "I approved it: submit again". Under a confirmation they are dimmed.
function dlRunButtons(panel, t, m, locked) {
  const rf = panel.dl.rf;
  const gate = panel.gate(m);
  if (rf.name === null) return [];
  if (rf.record !== null) {
    const a = approvalView(rf.record.state);
    return [pushButton(t, "cao-approval-again", rf.sending ? "Submitting..." : a.submitAgainLabel, { kind: "primary", disabled: locked || gate.disabled || rf.sending, on: (cx) => dlSubmitAgain(panel, cx) })];
  }
  return [pushButton(t, "cao-run-submit", rf.sending ? "Submitting..." : "Submit", { kind: "primary", disabled: locked || gate.disabled || rf.sending || rf.form === null, on: (cx) => dlOnRunSubmit(panel, cx) })];
}

// ---------- Draft with AI (proposed) ----------

// The box of New flow and New workflow: always labelled proposed. An author session is started by a button and nothing else.
// The reducer (lib/draft.js draftStep) decides everything; this runs what its effects ask, in order. An effect's answer comes
// back as an event stamped with its attempt, so a late answer of an earlier attempt cannot fill the form of the next one.
// The author is the one thing a dialog starts that outlives it: its record is written before the launch is sent, the 201 adds
// the terminal id, and the dialog's own successful delete removes both. What closing the dialog leaves is not cleaned up
// here: the always-drawn status mod finds the record by its name once this dialog's lease is dead.

// The Draft state of one form: the reducer's state, what is out, why the last press was refused, the form that asked and the
// retained description field.
function dlNewJob(kind, owner, text) {
  return { kind, state: initialDraft(kind), pending: 0, refusal: null, owner, text };
}

function dlFeedDraft(panel, cx, job, event) {
  const [next, effects] = draftStep(job.state, event, Date.now());
  job.state = next;
  for (const fx of effects) dlRunDraftEffect(panel, cx, job, fx);
}

// The author's launch. Its record (the session name and this dialog's name, no terminal id yet) goes into storage before the
// request is sent, so a dialog that Herdr closes while the launch is out still has its author deleted by name. The answer's HTTP
// status decides the record: an error status (4xx or 5xx) means no session exists and the record goes; a launch that got no
// answer has no status, so it stays, because the server may still create the session.
async function dlDraftLaunch(panel, job, call) {
  const name = job.state.sessionName;
  panel.store.set(DL_AUTHORS_KEY, recordAuthor(panel.store.get(DL_AUTHORS_KEY), name, panel.dl.name, Date.now()));
  const outcome = await callCao(dlExec, panel.cfg(panel.model()), call);
  panel.store.set(DL_AUTHORS_KEY, afterLaunch(panel.store.get(DL_AUTHORS_KEY), name, outcome.status));
  return outcome;
}

// One effect of the reducer. record: the 201's terminal id joins the record and the list of author ids. forget: nothing, the
// delete's own answer already removed the record and its id. fill: the draft fills the form. launch, read and delete are one
// request each, built by draftCall (which throws for a name that is not one of this window's own author sessions).
function dlRunDraftEffect(panel, cx, job, fx) {
  if (fx.type === "record") {
    panel.store.set(DL_AUTHORS_KEY, recordAuthor(panel.store.get(DL_AUTHORS_KEY), job.state.sessionName, panel.dl.name, Date.now(), fx.id));
    panel.store.set(DL_DRAFTS_KEY, recordDraft(panel.store.get(DL_DRAFTS_KEY), fx.id));
    return;
  }
  if (fx.type === "forget") return;
  if (fx.type === "fill") { dlFillDraft(panel, cx, job, fx); return; }
  let call;
  try { call = draftCall(fx); } catch (err) {
    job.refusal = String(err?.message ?? err);
    return;
  }
  if (call === null) return;
  const attempt = fx.attempt ?? job.state.attempt;
  job.pending += 1;
  cx.spawn(async (cx2) => {
    try {
      const outcome = fx.type === "launch" ? await dlDraftLaunch(panel, job, call) : await callCao(dlExec, panel.cfg(panel.model()), call);
      panel.views.draft = applyOutcome(panel.views.draft, outcome, Date.now());
      if (fx.type === "delete") {
        const next = afterDelete(panel.store.get(DL_AUTHORS_KEY), panel.store.get(DL_DRAFTS_KEY), fx.sessionName, outcome.kind === "ok" || outcome.status === 404);
        panel.store.set(DL_AUTHORS_KEY, next.authors);
        panel.store.set(DL_DRAFTS_KEY, next.drafts);
      }
      dlFeedDraft(panel, cx2, job, draftAnswer(fx, outcome, attempt));
    } finally {
      job.pending -= 1;
      cx2.notify();
    }
  });
}

// The tick of an author session still out: its terminal's state from Herdr's push data (no polling) and the 180 s timeout,
// until nothing is left to clean up. The cleanup this may start (the timeout's delete) is the cleanup the user's Draft click
// asked for, not a new write.
function dlTickDrafts(panel, cx) {
  const job = panel.dl.job;
  if (job === null || draftJobDone(job.state, job.pending)) return;
  for (const ev of draftTickEvents(job.state, panel.model().terms)) dlFeedDraft(panel, cx, job, ev);
}

// A box button. Draft, Draft again and Retry start an author, Fix with AI starts one with the errors, Cancel deletes it. Only a
// click gets here, never a tick. The checks run in the order the lib gives: the button, the press, the profile as read.
function dlOnDraftAction(panel, cx, job, action) {
  const dl = panel.dl;
  const kind = draftActionKind(job.state, action);
  if (kind === "cancel") { dlFeedDraft(panel, cx, job, { type: "cancel" }); cx.notify(); return; }
  if (kind === null) return;
  const description = job.text.value();
  const profile = dl.request.draftProfile;
  const press = draftPressCheck({ gate: panel.gate(panel.model()), profile, description });
  if (!press.ok) { job.refusal = press.reason; cx.notify(); return; }
  job.refusal = null;
  const previous = { kind: job.kind, fields: draftFieldsOf(job.kind, job.owner.form) };   // the definition as it is when the button is pressed
  cx.spawn(async (cx2) => {
    const detail = await dlLoadProfile(panel, cx2, profile);
    const workdir = panel.store.getLocal("caowin.lastWorkdir");
    const check = draftBeginCheck({ gate: panel.gate(panel.model()), profile, detail, draftProviders: dl.request.draftProviders, providers: panel.views.providers?.data, workdir });
    if (!check.ok) { job.refusal = check.reason; cx2.notify(); return; }
    let request;
    try {
      const params = { kind: job.kind, description, profile, provider: check.providerName, workdir, idempotencyKey: dlUuid(), hex: dlUuid().replace(/-/g, "").slice(0, 8) };
      request = kind === "fix" ? fixRequest(params, previous, job.state.errors) : draftRequest(params);
    } catch (err) { job.refusal = String(err?.message ?? err); cx2.notify(); return; }
    dlFeedDraft(panel, cx2, job, { type: kind, request });
    cx2.notify();
  });
  cx.notify();
}

// The author's definition fills the form that asked, marks each filled field "AI", and the form's own validation runs; its
// errors go back to the reducer, which offers "Fix with AI" for them and sends nothing itself. Nothing is saved here.
function dlFillDraft(panel, cx, job, fx) {
  const owner = job.owner;
  const attempt = job.state.attempt;
  const validated = (errors) => dlFeedDraft(panel, cx, job, draftValidatedEvent(attempt, errors));
  const plan = draftFillPlan(job.kind, { form: owner.form, touched: owner.touched }, fx.fields);
  if (plan.kind === "flow") {
    const fin = panel.dl.fin;
    owner.priming = true;
    for (const key of ["name", "schedule", "profile", "prompt"]) fin[key].set_value(plan.texts[key]);
    owner.priming = false;
    Object.assign(owner, { form: plan.form, marks: plan.marks, touched: plan.touched, serverErrors: plan.serverErrors, checks: plan.checks, checkedKey: plan.checkedKey });
    if (plan.loadProfile) cx.spawn((cx2) => dlLoadProfile(panel, cx2, plan.loadProfile).then(() => cx2.notify()));
    dlRunCheck(panel, cx, true, (n) => validated(draftValidationErrors("flow", { checks: n.checks })));
    return;
  }
  Object.assign(owner, plan.resets);
  dlFillEditor(panel, cx, plan.form, plan.marks);
  const report = (n) => validated(draftValidationErrors("workflow", { result: n.result, check: n.check, note: n.note }));
  if (dlWorkflowGateNow(panel, panel.model()).validate) dlRunWorkflowValidate(panel, cx, report);
  else report(owner);
}

// The Draft with AI box of New flow and New workflow, labelled proposed. Its buttons are the reducer's view of what is possible
// now. The title and its label sit in a row that wraps, so a narrow dialog cuts neither.
function dlDraftBox(panel, t, owner, gate) {
  const job = owner.job;
  const box = draftBox(job.state);
  const slug = (a) => a.toLowerCase().replace(/[^a-z]+/g, "-");
  return dlCol().id(`cao-draft-${job.kind}`).gap(6).p(10).rounded(8).border_1().border_color(t.border).bg(t.surface)
    .child(dlRow().gap(6).items_center().flex_wrap()
      .child(dlDiv().font_semibold().text_size(12).child(box.title))
      .child(dlDiv().id(`cao-draft-label-${job.kind}`).text_size(10.5).text_color(t.dim).child(box.label)))
    .child(dlDiv().id(`cao-draft-text-${job.kind}`).text_size(11.5).text_color(t.muted).child(box.text))
    .when(Boolean(job.refusal), (e) => e.child(problemLine(t, job.refusal)))
    .child(textareaBox(t, `cao-draft-desc-${job.kind}`, job.text, 3))
    .child(dlRow().gap(8).items_center().flex_wrap()
      .children(draftActionList(job.state).map((a) => pushButton(t, `cao-draft-${slug(a)}-${job.kind}`, a, {
        kind: a === "Draft" || a === "Draft again" ? "primary" : "plain", disabled: gate.disabled && a !== "Cancel", on: (cx) => dlOnDraftAction(panel, cx, job, a),
      }))));
}
