// What the Workflows section and the workflow views of the panel decide (cao-window-workflows): the rows
// of the section, when its polls run, what its notices say, the details of one workflow with its runs, and
// the confirmed writes on a run (Cancel, Resume). Pure; the mods only draw and send. The rules about
// workflows themselves (row state, approval, inputs, size checks, the save request) are lib/workflows.js,
// and the flow rules are lib/flows.js. Top-level names are unique across lib/*.js (bundle mode, D8).
import { AUTH_MESSAGE, seg } from "./cao.js";
import { FLOWS_POLL_MS, cronHuman, viewNotes } from "./flows.js";
import { ageWords } from "./logic.js";
import {
  approvalState, coerceInputs, inputsForm, pollInterval, runActions, saveEnabled, scheduledBy, sourceCheck, staleHashOutcome, workflowDetails, workflowRowState,
} from "./workflows.js";

// The server's words for a failed answer: its detail, else its message, where a blank text is no words. With
// none, `bare` when the answer carried no text at all; when it carried a blank one (an empty body, "detail": ""),
// `named`, which names the action, with the HTTP status when there is one.
function wvWords(outcome, bare, named) {
  const given = [outcome?.detail, outcome?.message].filter((v) => v !== undefined && v !== null);
  const text = given.find((v) => String(v).trim() !== "");
  if (text !== undefined) return String(text);
  if (given.length === 0) return bare;
  return typeof outcome?.status === "number" ? `${named} (HTTP ${outcome.status})` : named;
}

const wfFinite = (v) => typeof v === "number" && Number.isFinite(v);

// The server's name rule (WORKFLOW_NAME_RE): 1 to 64 letters, digits, - and _. And the one name that would read
// another route: GET and POST /workflows/runs are the run list, declared before GET /workflows/{name}, so a
// workflow named "runs" could not be reached at /workflows/runs. (POST /workflows/validate and
// POST /workflows/plans/approve are not reachable by a name: validate is POST only, and the other has two
// segments; "runs:submit" is not a name.)
const WF_NAME_MAX = 64;
const WF_RESERVED = Object.freeze(["runs"]);
const wfNameOk = (name) => typeof name === "string" && name.length <= WF_NAME_MAX && !WF_RESERVED.includes(name);

// /workflows/<name> for a name the server accepts and no fixed route shadows; null for anything else, so a name
// read from the server can never become another path.
export function workflowRoute(name) {
  try { seg(name); } catch { return null; }
  return wfNameOk(name) ? `/workflows/${name}` : null;
}
// /workflows/runs/<id>/<cancel|resume>; null for another action or an id that is not a plain segment.
export function runRoute(action, runId) {
  if (action !== "cancel" && action !== "resume") return null;
  try { seg(runId); } catch { return null; }
  return `/workflows/runs/${runId}/${action}`;
}

// The runs of one workflow, in the order the server gave them (newest first).
export const runsOfWorkflow = (runs, name) => (Array.isArray(runs) ? runs : []).filter((r) => r && typeof r === "object" && r.workflow_name === name);

// How many runs the window reads (GET /workflows/runs?limit=20). A workflow's state is among these only.
export const WORKFLOW_RUNS_LIMIT = 20;

// The state words of one workflow from the runs read: "unknown" while that read has not answered (or failed, so
// there is nothing to go on), never "never run". With runs: its newest run's state; none of its own among fewer
// than 20 runs is "never run", and none of its own among a full 20 is "no recent run", since an older run may exist.
export function workflowStateFor(runs, name) {
  if (!Array.isArray(runs)) return "unknown";
  const mine = runsOfWorkflow(runs, name);
  if (mine.length > 0) return workflowRowState(mine);
  return runs.length >= WORKFLOW_RUNS_LIMIT ? "no recent run" : "never run";
}

// The rows of the Workflows section: a bare name and its state words (workflowStateFor). A row with a name
// that could not be put in a path is left out; source_path is never read.
export function workflowRows(list, runs) {
  return (Array.isArray(list) ? list : [])
    .filter((w) => w && typeof w === "object" && workflowRoute(w.name) !== null)
    .map((w) => ({ name: w.name, state: workflowStateFor(runs, w.name) }));
}

// The details' Runs line when the workflow has no run to list: it follows the same rule as the row.
export function runsLineFor(runs, name) {
  if (!Array.isArray(runs)) return "The runs have not been read yet.";
  if (runsOfWorkflow(runs, name).length > 0) return null;
  return runs.length >= WORKFLOW_RUNS_LIMIT ? `No run among the latest ${WORKFLOW_RUNS_LIMIT}` : "No runs yet";
}

// Whether the section (or the details of a workflow) fetches now: it is shown, nothing is in flight, and the
// last fetch is one poll interval old (60 s, or 5 s while a listed run is running) or there was none, or a
// write moved the refresh stamp since the last fetch.
export function workflowsPollDue({ shown, inFlight, lastAt, runs, stamp, seenStamp } = {}, now) {
  if (shown !== true || inFlight === true) return false;
  return !wfFinite(lastAt) || stamp !== seenStamp || now - lastAt >= pollInterval(runs);
}

// One stamp from the refresh stamps the details watch (a workflow write's and a flow write's: Schedule... creates a
// flow, which moves the second), so a change in any one of them reads as a change of the whole.
export const combinedStamp = (...stamps) => JSON.stringify(stamps.map((v) => (v === undefined ? null : v)));

// What a details poll fetches: the workflow list and its runs on the poll interval; the flows (for "Scheduled
// by") at most every 30 s, or when a write moved the stamp; the spec (its inputs) until one read succeeded, and
// again after a write moved the stamp. A failed read is never kept, so the next poll asks again.
export function workflowDetailPlan({ shown, inFlight, lastAt, runs, flowsAt, specOk, stamp, seenStamp } = {}, now) {
  const poll = workflowsPollDue({ shown, inFlight, lastAt, runs, stamp, seenStamp }, now);
  const moved = stamp !== seenStamp;
  return {
    poll,
    flows: poll && (!wfFinite(flowsAt) || moved || now - flowsAt >= FLOWS_POLL_MS),
    spec: poll && (specOk !== true || moved),
  };
}

// The notices under the Workflows header: the list's own (auth, unreachable, an error, the age of the rows on
// screen), then what only the runs read adds, marked "Runs:".
export function workflowsNotes(listView, runsView, now) {
  const own = viewNotes(listView, now);
  const extra = viewNotes(runsView, now).filter((line) => !own.includes(line)).map((line) => `Runs: ${line}`);
  return [...own, ...extra];
}

// "started 5 minutes ago" from the server's UTC time; the text as it came when it does not read as a time.
export function startedWords(startedAt, now) {
  const t = typeof startedAt === "string" ? Date.parse(startedAt) : NaN;
  if (!Number.isFinite(t)) return typeof startedAt === "string" && startedAt !== "" ? `started ${startedAt}` : "start time unknown";
  const age = ageWords(now - t);
  return age === "just now" ? "started just now" : `started ${age} ago`;
}

const stepText = (r) => (typeof r?.current_step_id === "string" && r.current_step_id !== "" ? r.current_step_id : null);
const shortRunId = (id) => (typeof id === "string" ? (id.length > 12 ? `${id.slice(0, 8)}...` : id) : "");

// The details of one workflow: its GET /workflows row (description, mode, step count; never source_path), the
// flows whose prompt runs it ("Scheduled by", with the schedule in words and the enabled state), and its runs
// with the actions each allows. `listed` is false when the list no longer holds the workflow.
export function workflowDetailsView({ name, row, runs, flows, now } = {}) {
  const base = workflowDetails(row ?? { name });
  return {
    ...base,
    name: typeof name === "string" ? name : base.name,
    listed: row != null,
    runsLine: runsLineFor(runs, name),
    stepText: base.stepCount === null ? "unknown" : String(base.stepCount),
    scheduledBy: scheduledBy(flows, name).map((f) => ({
      name: f.name, cron: f.schedule, words: cronHuman(f.schedule) ?? f.schedule, enabled: f.enabled === true, enabledText: f.enabled === true ? "enabled" : "disabled",
    })),
    runs: runsOfWorkflow(runs, name).map((r) => {
      const act = runActions(r);
      const routable = runRoute("cancel", r.run_id) !== null;
      return {
        id: String(r.run_id ?? ""), shortId: shortRunId(r.run_id), state: r.state, stateText: workflowRowState([r]), startedText: startedWords(r.started_at, now), step: stepText(r),
        cancel: act.cancel && routable, resume: act.resume && routable,
      };
    }),
  };
}

// The dialog of a write on a run. Cancel and Resume both start on the safe button; Resume says that the
// run's agents work with nobody watching. Anything else throws, so no other write can be dressed as one.
export function runDialogSpec(action, run) {
  const id = shortRunId(run?.run_id ?? run?.id);
  const specs = {
    cancel: { title: `Cancel run ${id}?`, body: "This asks the run to stop. Steps already finished are kept.", confirmLabel: "Cancel run", cancelLabel: "Keep it running", danger: true },
    resume: { title: `Resume run ${id}?`, body: "This starts the run again from its journal. Its agents work with nobody watching, and a step that needs plan approval stops again.", confirmLabel: "Resume run", cancelLabel: "Not now", danger: false },
  };
  if (typeof action !== "string" || !Object.hasOwn(specs, action)) throw new Error(`no dialog for run action ${String(action).slice(0, 30)}`);
  return { ...specs[action], defaultFocus: "cancel", confirmEnabled: true };
}

const REQUIRED_STATE = Object.freeze({ cancel: "running", resume: "cancelled" });
// Whether Confirm may send the run write that was pressed for `snapshot` ({run_id, state} as the row showed
// it): the run must still be in the state the action needs in the latest list. A run that fell out of the
// list, or that moved on, is not written to; the reason is for the row.
export function runWriteCheck(action, snapshot, live) {
  if (runRoute(action, snapshot?.run_id) === null) return { send: false, reason: "This run cannot be addressed from this window." };
  if (!Array.isArray(live)) return { send: true, reason: null };
  const now = live.find((r) => r?.run_id === snapshot.run_id);
  if (!now) return { send: false, reason: "The run is not in the list any more. Wait for the next refresh." };
  if (now.state !== REQUIRED_STATE[action]) return { send: false, reason: `The run is ${workflowRowState([now])} now, so nothing was sent.` };
  return { send: true, reason: null };
}

// The notes on run rows after a refresh brought `runs` (a Map of run id to {kind, text, state}, where `state` is the
// state of the run the note was made for). A "done" note ("requested") has been answered by the refresh and goes. A
// failed note stays while the run is still in the state it was made for, and goes when the refresh shows the run in
// another state, or not at all. A new Map; the one given is not changed.
export function pruneRunNotes(notes, runs) {
  const live = Array.isArray(runs) ? runs : [];
  return new Map([...(notes instanceof Map ? notes : [])].filter(([id, n]) => {
    if (n?.kind === "done") return false;
    const run = live.find((r) => r?.run_id === id);
    return run !== undefined && run.state === n?.state;
  }));
}

// One callCao outcome of a run write -> what its row says. A failure keeps the run as it was.
export function runWriteOutcome(action, outcome) {
  if (action !== "cancel" && action !== "resume") throw new Error(`no outcome for run action ${String(action).slice(0, 30)}`);
  const kind = outcome?.kind;
  if (kind === "auth") return { type: "auth", text: outcome.message ?? AUTH_MESSAGE };
  if (kind === "ok") return { type: "done", text: action === "cancel" ? "Cancel requested. The run shows cancelled after the next refresh." : "Resume requested. The run shows its new state after the next refresh." };
  if (kind === "unreachable") return { type: "failed", text: "CAO did not answer. The run may have changed: wait for the next refresh before trying again." };
  return { type: "failed", text: wvWords(outcome, "failed", `${action === "cancel" ? "Cancel" : "Resume"} failed`) };
}

// Why a read gave no data, for the line under a view: CAO did not answer, or the server's own words.
export function readFailureText(outcome, what) {
  if (outcome?.kind === "unreachable") return "CAO did not answer";
  if (outcome?.kind === "auth") return outcome.message ?? AUTH_MESSAGE;
  return wvWords(outcome, `${what} could not be read`, `${what} could not be read`);
}

// ---------- Run (the inputs form, the answer, the plan-approval view) ----------

// Writes a value under a name even for "__proto__", which a plain assignment would turn into a prototype.
const wvPutOwn = (obj, key, value) => Object.defineProperty(obj, key, { value, enumerable: true, writable: true, configurable: true });

// The Run form for the spec read and how that read went (specState: "ok", "failed" or "loading"). A spec that was
// read gives lib/workflows.js inputsForm: typed fields, or free rows labelled "inputs not described by the spec".
// A read that failed gives free rows labelled as unread instead, since the spec was never seen, not described badly.
export function runFormModel(spec, specState) {
  if (specState === "failed") return { fields: [], free: true, label: "The inputs could not be read from CAO; enter them by hand." };
  return inputsForm(spec);
}

// The inputs of a form that has no typed fields: rows of {key, value}. An empty row is ignored; a value with no
// name, and a name used twice, are problems at their row (`rowErrors`, keyed by the row's index). Values stay text.
export function freeInputs(rows) {
  const inputs = {};
  const rowErrors = {};
  const seen = new Set();
  (Array.isArray(rows) ? rows : []).forEach((row, i) => {
    const key = typeof row?.key === "string" ? row.key.trim() : "";
    const value = typeof row?.value === "string" ? row.value : "";
    if (key === "" && value === "") return;
    if (key === "") { wvPutOwn(rowErrors, String(i), "needs a name"); return; }
    if (seen.has(key)) { wvPutOwn(rowErrors, String(i), `"${key}" is used twice`); return; }
    seen.add(key);
    wvPutOwn(inputs, key, value);
  });
  return { inputs, errors: {}, rowErrors };
}

// The inputs the Run form sends: typed fields (`values` by field name) or free rows, whichever `form`
// (lib/workflows.js inputsForm) says. `errors` is by field name, `rowErrors` by row index; nothing is sent while either is non-empty.
export function runInputs(form, values, rows) {
  if (form?.free === true) return freeInputs(rows);
  const typed = coerceInputs(Array.isArray(form?.fields) ? form.fields : [], values);
  return { inputs: typed.inputs, errors: typed.errors, rowErrors: {} };
}
export const runInputsOk = (r) => Object.keys(r?.errors ?? {}).length === 0 && Object.keys(r?.rowErrors ?? {}).length === 0;

// One callCao outcome of POST /workflows/runs:submit -> what the form and the details do. Types:
// started (a 202; `runId` when the answer names it), approval (a 403 approval_required: the plan-approval view),
// auth, unreachable (the run may exist: look in the list before submitting again) and failed (the server's words).
export function submitOutcomeView(outcome) {
  const kind = outcome?.kind;
  if (kind === "ok") return { type: "started", runId: typeof outcome.json?.run_id === "string" ? outcome.json.run_id : null };
  if (kind === "approval_required") return { type: "approval" };
  if (kind === "auth") return { type: "auth", text: outcome.message ?? AUTH_MESSAGE };
  if (kind === "unreachable") return { type: "unreachable", text: "CAO did not answer. The run may have started: look in the runs list before you submit again." };
  return { type: "failed", text: wvWords(outcome, "failed", "The run was not submitted") };
}

const SERVER_NOTE_MAX = 300;
// What the plan-approval view draws, from approvalState(): the headline, the plan id (or that the server gave none),
// the command to run in a terminal with its Copy action, the server's own words, the local "not submitted" row of
// the runs list, and the submit-again action. There is no approve action: nothing here names the approve route.
export function approvalView(state) {
  if (!state?.needed) return null;
  return {
    headline: state.headline,
    planText: state.planId === null ? "The server gave no plan id." : `Plan id: ${state.planId}`,
    command: state.command,
    copyLabel: "Copy",
    serverMessage: String(state.serverMessage ?? "").slice(0, SERVER_NOTE_MAX),
    localRun: state.localRun,
    submitAgainLabel: state.submitAgainLabel,
  };
}

// What stays of a submission that stopped at plan approval: {state: approvalState(), submission: {name, inputs,
// workingDirectory}, sending}. The same name, inputs and directory go again on "submit again". An approval
// refusal replaces the record, a started run drops it, any other outcome leaves it as it was.
export function approvalRecord(prior, outcome, submission) {
  const state = approvalState(prior?.state ?? null, outcome);
  if (state === null) return null;
  return outcome?.kind === "approval_required" ? { state, submission, sending: false } : prior ?? null;
}

// ---------- New workflow and Edit (the editor) ----------

export const emptyWorkflowForm = () => ({ name: "", tier: "yaml", source: "" });
export const workflowEditorDirty = (form, initial) => JSON.stringify(form) !== JSON.stringify(initial);

// What a validate answer belongs to: the tier and the exact text it was sent with. Save compares the current
// key with this one, so a changed text, or a changed tier, needs a new validate.
export const workflowValidationKey = (tier, text) => `${tier}\u0000${typeof text === "string" ? text : ""}`;

const WF_BARE = /^[A-Za-z0-9_-]+$/;
// The message under the name field, or null: a bare name of at most 64 characters, no path and no extension,
// and not a name a fixed route answers for.
export function workflowNameProblem(name) {
  if (typeof name !== "string" || !WF_BARE.test(name)) return "Use letters, digits, - and _ only: a bare name, no path and no extension.";
  if (name.length > WF_NAME_MAX) return `A workflow name is at most ${WF_NAME_MAX} characters.`;
  if (WF_RESERVED.includes(name)) return `"${name}" is a name the server uses for its own routes. Pick another.`;
  return null;
}

// POST /workflows/validate's answer -> what the editor shows. Blocking: error findings at their lines, and error
// strings an error finding does not already say. Warnings: warning findings and the reserved-construct notes.
// They do not block. An answer that fails with nothing to show still blocks, with a general line.
export function validateReport(json) {
  const answer = json && typeof json === "object" && !Array.isArray(json) ? json : null;
  const findings = Array.isArray(answer?.findings) ? answer.findings.filter((f) => f && typeof f === "object") : [];
  const item = (line, text) => ({ line: Number.isInteger(line) ? line : null, text: String(text ?? "") });
  const blocking = findings.filter((f) => f.severity === "error").map((f) => item(f.line, f.message));
  const warnings = findings.filter((f) => f.severity === "warning").map((f) => item(f.line, f.message));
  const mirrored = blocking.length > 0;
  for (const e of Array.isArray(answer?.errors) ? answer.errors : []) {
    if (typeof e !== "string") continue;
    const at = /^line (\d+):/.exec(e);
    if (mirrored && at) continue;                    // the Python tier repeats each error finding as a string
    blocking.push(item(at ? Number(at[1]) : null, e));
  }
  for (const n of Array.isArray(answer?.reserved_notes) ? answer.reserved_notes : []) if (typeof n === "string") warnings.push(item(null, n));
  if (answer?.status === "fail" && blocking.length === 0) blocking.push(item(null, "The server says this source does not pass validation."));
  return { status: typeof answer?.status === "string" ? answer.status : null, blocking, warnings };
}
export const findingWords = (f) => (f.line === null ? f.text : `line ${f.line}: ${f.text}`);

// Why a validate call gave no answer: CAO did not answer, the request was refused before it was sent, or the
// server answered with an error. null for an answer.
export function validateOutcomeNote(outcome) {
  const kind = outcome?.kind;
  if (kind === "ok") return null;
  if (kind === "unreachable") return "CAO did not answer, so the source could not be checked.";
  if (kind === "auth") return outcome.message ?? AUTH_MESSAGE;
  if (kind === "too_large" || kind === "refused" || kind === "env_refused") return String(outcome.message ?? "the request was not sent");
  return `CAO could not check the workflow: ${wvWords(outcome, "error", "error")}`;
}

// A YAML spec's own `name:` line (the first one at the start of a line, so a step's indented name is not it): its
// value without quotes or a trailing comment; null when the spec has none.
const WV_NAME_LINE = /^name[ \t]*:[ \t]*(.*)$/m;
export function yamlSpecName(source) {
  const m = WV_NAME_LINE.exec(typeof source === "string" ? source : "");
  if (!m) return null;
  const quoted = /^(["'])(.*?)\1/.exec(m[1].trim());
  return quoted ? quoted[2] : m[1].replace(/[ \t]+#.*$/, "").trim();
}
// The server refuses a YAML spec whose `name:` differs from the workflow's name (a 400 after a clean validate), so Save
// checks it first. The message for the line under the editor, or null: no check for Python, or a spec with no name line.
export function specNameMismatch(tier, source, name) {
  if (tier !== "yaml") return null;
  const inSpec = yamlSpecName(source);
  if (inSpec === null || inSpec === name) return null;
  return `The spec's name: line says "${inSpec}", but the workflow is named "${String(name)}". The server refuses a spec whose name differs; make them the same.`;
}
// workflowEditorGate's answer with the name check added: Save also needs the spec's `name:` line to agree with the
// workflow's name (Validate does not: the server's validate does not look at it). {nameProblem, specNameProblem, validate, save}.
export function editorGateWithName(gate, { tier, source, name } = {}) {
  const specNameProblem = specNameMismatch(tier, source, name);
  return { ...gate, specNameProblem, save: gate.save === true && specNameProblem === null };
}
// The source with its `name:` line set to `name`; unchanged when there is no such line.
export const renameYamlSpec = (source, name) => (yamlSpecName(source) === null ? source : source.replace(WV_NAME_LINE, `name: ${name}`));

// Whether Validate and Save work now. mode: "new" or "edit"; `check` is sourceCheck's answer for the text as it
// is now; `gate` is the panel's write gate; `validatedKey` and `result` are what the last clean read gave. Save
// needs the same text and tier as that read (lib/workflows.js saveEnabled), a valid name for a new workflow, and,
// for an edit, the hash read when editing began. Nothing works while a request is out or the text is too large.
export function workflowEditorGate({ mode, gate, check, validating, sending, loading, name, tier, source, validatedKey, result, expectedHash } = {}) {
  const nameProblem = mode === "new" ? workflowNameProblem(name) : null;
  const base = gate?.disabled !== true && validating !== true && sending !== true && loading !== true
    && check?.ok === true && (tier === "python" || tier === "yaml") && typeof source === "string" && source.trim() !== "";
  const hashOk = mode !== "edit" || (typeof expectedHash === "string" && expectedHash !== "");
  const validated = saveEnabled(validatedKey, workflowValidationKey(tier, source), result);
  return { nameProblem, validate: base, save: base && validated && nameProblem === null && hashOk };
}

// One callCao outcome of POST /workflows (method "POST", a new workflow) or PUT /workflows/{name} (an edit) ->
// what the editor does. Types: saved, stale (the hash no longer matches: text kept, reload offered), conflict (another
// 409 on a PUT), exists (a 409 on a POST), auth, unreachable (the save may have happened), failed.
export function workflowSaveResult(outcome, method) {
  const kind = outcome?.kind;
  if (kind === "auth") return { type: "auth", text: outcome.message ?? AUTH_MESSAGE };
  if (kind === "ok") return { type: "saved", name: typeof outcome.json?.name === "string" ? outcome.json.name : null };
  if (kind === "unreachable") return { type: "unreachable", text: "CAO did not answer. The save may have happened: look in the Workflows list before you save again." };
  const conflict = staleHashOutcome(outcome, method);
  if (conflict) return conflict;
  return { type: "failed", text: wvWords(outcome, "failed", "The save failed") };
}
// What a save answer does to the editor: saved leaves it and selects `selectName`; anything else keeps the text,
// with the banner, a message at the name for a name that exists, and Reload for a stale hash.
export function workflowSaveEffects(r, { name, authText } = {}) {
  if (r?.type === "saved") return { done: true, selectName: r.name ?? name };
  if (r?.type === "exists") return { done: false, banner: null, nameError: r.message, offerReload: false };
  if (r?.type === "stale") return { done: false, banner: r.message, nameError: null, offerReload: true };
  if (r?.type === "conflict") return { done: false, banner: r.message, nameError: null, offerReload: false };
  return { done: false, banner: r?.type === "auth" ? authText : String(r?.text ?? "failed"), nameError: null, offerReload: false };
}

// What Edit loads from GET /workflows/{name}: the workflow's source text, its tier and the content hash that is the
// PUT's expected_hash. Both tiers' answers carry `source` and `content_hash` (a Python workflow is a ScriptSpec,
// which has them too) and name no tier, so the tier is read the way the server's own dispatch reads it, by the
// extension of the answer's `path`: .py is python, .yaml and .yml are yaml. Without a path, a `steps` list says
// yaml and a `findings` list says python. An answer with no source, no hash or no tier cannot be edited.
function wvDocumentTier(doc) {
  if (typeof doc.path === "string") {
    if (/\.py$/i.test(doc.path)) return "python";
    return /\.ya?ml$/i.test(doc.path) ? "yaml" : null;
  }
  if (Array.isArray(doc.steps)) return "yaml";
  return Array.isArray(doc.findings) ? "python" : null;
}
export function editFromDocument(doc, name) {
  const tier = doc && typeof doc === "object" ? wvDocumentTier(doc) : null;
  if (tier !== null && typeof doc.source === "string" && typeof doc.content_hash === "string" && doc.content_hash !== "") {
    return { ok: true, tier, source: doc.source, hash: doc.content_hash };
  }
  return { ok: false, reason: `CAO's answer for ${typeof name === "string" ? name : "this workflow"} has no source text, content hash or known tier, so it cannot be edited here.` };
}

// A template applied to the form with the name settled: applyTemplate, then a YAML template's `name:` line is
// rewritten to the form's name, so the spec and the workflow agree from the start.
export function templateFor(form, template) {
  const next = applyTemplate(form, template);
  return next.tier === "yaml" && workflowNameProblem(next.name) === null ? { ...next, source: renameYamlSpec(next.source, next.name) } : next;
}

// The form after a template is picked: its tier and source; the name only when none is typed.
export function applyTemplate(form, template) {
  return { ...form, tier: template.tier, source: template.source, name: form.name === "" ? template.name : form.name };
}
// A template replaces text the user typed only after asking: text that is empty or is some template's own is not theirs.
// Whether the form's source is `template`'s own text: the template as it is, or as templateFor makes it for the name the
// form holds now (a YAML template has its name: line rewritten to a typed name, which is still the template).
export const isTemplateText = (form, template) => typeof form?.source === "string" && (form.source === template.source || form.source === templateFor(form, template).source);
export const templateNeedsConfirm = (form, templates) => typeof form?.source === "string" && form.source.trim() !== "" && !(Array.isArray(templates) ? templates : []).some((t) => isTemplateText(form, t));

export const TEMPLATE_NOTE = "The profile names in a template are placeholders: replace them with profiles installed on the CAO machine before you save.";
export const workflowFileHint = () => "A Python file already on the CAO machine is installed with `cao workflow create <name> --from-file <path>` (Python only). A YAML spec is pasted into the editor.";

// The two dialogs of the editor. reload: Edit's stale hash, replacing the typed text with the server's version.
// template: a template over text the user typed. Both start on the safe button.
export function workflowDialogSpec(kind) {
  const specs = {
    reload: { title: "Replace your text with the server's version?", body: "The workflow changed elsewhere. Your edits in this editor are lost.", confirmLabel: "Replace my text", cancelLabel: "Keep my text", danger: true },
    template: { title: "Replace your text with the template?", body: "What you typed in the editor is lost.", confirmLabel: "Replace", cancelLabel: "Keep my text", danger: true },
  };
  if (typeof kind !== "string" || !Object.hasOwn(specs, kind)) throw new Error(`no dialog for ${String(kind).slice(0, 30)}`);
  return { ...specs[kind], defaultFocus: "cancel", confirmEnabled: true };
}

// The size line and message of the editor for its text as it is now (lib/workflows.js sourceCheck, with the hash of an edit).
export function editorSizeCheck(form, { stdin, expectedHash } = {}) {
  return sourceCheck(form?.tier, form?.source, form?.name, stdin === true, expectedHash ? { expectedHash } : {});
}

// ---------- what the panel does with answers (decisions the mod only follows) ----------

// The reads of one details poll, in order, and the CAO call and the view each makes. The list comes first and the
// runs next; the flows (for "Scheduled by") and the spec (for the Run form and Edit) only when the plan asked.
// `view` names the panel view that folds the outcome (applyOutcome); null: no view keeps it.
export function detailReads(plan) {
  return ["list", "runs", ...(plan?.flows ? ["flows"] : []), ...(plan?.spec ? ["spec"] : [])];
}
// The callCao spec of one read for workflow `name`, or null when its route cannot be built (a refused outcome then).
export function detailCall(read, name) {
  switch (read) {
    case "list": return { path: "/workflows", maxTime: 8 };
    case "runs": return { path: "/workflows/runs", query: { limit: WORKFLOW_RUNS_LIMIT }, maxTime: 8 };
    case "flows": return { path: "/flows", maxTime: 8 };
    case "spec": { const route = workflowRoute(name); return route === null ? null : { path: route, maxTime: 8 }; }
    default: return null;
  }
}
export const detailView = (read) => (read === "list" ? "workflows" : read === "runs" ? "workflowRuns" : null);

// The answer of one read -> {patch, stop}: the fields of the details object to set, and whether the rest of the poll
// is skipped. `prev` is the details object ({name, notes, specOk, ...}). A failed read changes nothing but its notice
// (the list) or the spec state, so what the last good read gave stays. A list that did not answer stops the poll (the
// others would only fail the same way). New runs end the notes they answer (pruneRunNotes).
export function foldDetailRead(prev, read, outcome) {
  const okList = outcome?.kind === "ok" && Array.isArray(outcome.json);
  switch (read) {
    case "list":
      return {
        patch: okList ? { row: outcome.json.find((w) => w?.name === prev.name) ?? null, error: null } : { error: readFailureText(outcome, "the workflow list") },
        stop: outcome?.kind === "unreachable",
      };
    case "runs": return { patch: okList ? { runs: outcome.json, notes: pruneRunNotes(prev.notes, outcome.json) } : {}, stop: false };
    case "flows": return { patch: okList ? { flows: outcome.json } : {}, stop: false };
    case "spec": {
      const spec = outcome?.kind === "ok" && outcome.json && typeof outcome.json === "object" && !Array.isArray(outcome.json) ? outcome.json : null;
      return { patch: spec ? { spec, specOk: true, specState: "ok" } : { specState: prev.specOk ? "ok" : "failed" }, stop: false };
    }
    default: return { patch: {}, stop: false };
  }
}

// What an answer of POST /workflows/runs:submit does, for the submission {name, inputs, workingDirectory}.
// `formHeld`: the Run form that asked is still the one the panel holds; `recHeld`: the kept approval record that
// asked is still the one kept for the name; `prior`: the record kept for the name now. Returns
//   view      submitOutcomeView's answer (started, approval, auth, unreachable, failed)
//   record    {op: "set", value} (a new approval refusal), {op: "delete"} (a started run) or {op: "keep"}
//   started   {name, runId} to announce under the workflow, or null; stamp: move the refresh stamp
//   dropForm  close the Run form that asked (only if still held): a started run or an approval refusal
//   formBanner / recBanner  the failure text for the form or the kept record that asked, only while it is still held
export function runSubmitEffects(outcome, submission, { formHeld = false, recHeld = false, prior = null } = {}) {
  const view = submitOutcomeView(outcome);
  const next = approvalRecord(prior, outcome, submission);
  const record = next === null ? { op: "delete" } : next === prior ? { op: "keep" } : { op: "set", value: next };
  const started = view.type === "started" ? { name: submission.name, runId: view.runId } : null;
  const done = view.type === "started" || view.type === "approval";
  const text = done ? null : view.text;
  return { view, record, started, stamp: started !== null, dropForm: done && formHeld, formBanner: formHeld ? text : null, recBanner: recHeld ? text : null };
}
