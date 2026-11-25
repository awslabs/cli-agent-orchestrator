// The Schedules section's rules (cao-window-flows; what the person reads is "schedule", CAO's own name is "flow", D39 F): rows, schedule words, the confirmations, how server
// answers land at form fields, what to say when the flow routes are missing, and the bodies the window
// sends. Pure. Top-level names are unique across lib/*.js (bundle mode, D8).
import { AUTH_MESSAGE, seg } from "./cao.js";
import { HONESTY } from "./honesty.js";
import { shq } from "./launch.js";
import { ageWords } from "./logic.js";

export const FLOWS_POLL_MS = 30_000;
// The message POST /flows/validate gives for a name that is already registered (flow_service).
export const FLOW_NAME_TAKEN_MESSAGE = "a flow with this name already exists";

const twoDigits = (n) => String(n).padStart(2, "0");
const hasText = (v) => typeof v === "string" && v.trim() !== "";
// The server's stored times carry no zone; this adds none and reads the wall-clock part only.
const WALL_CLOCK = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/;
function wallClockOf(value) {
  const m = typeof value === "string" ? WALL_CLOCK.exec(value) : null;
  if (!m || !Number.isFinite(Date.parse(`${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:00Z`))) return null;
  return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}`;
}

// "running" while Run now is out or the flow's session works, "off" when disabled, else the next run as
// the server stored it (server local time).
export function flowRowState(flow, { runNowInFlight = false, sessionWorking = false } = {}) {
  if (runNowInFlight === true || sessionWorking === true) return { text: "running" };
  if (!flow?.enabled) return { text: "off" };
  const wall = wallClockOf(flow.next_run);
  return { text: wall ? `next ${wall}` : "scheduled" };
}

export function flowsSummary(flows) {
  const list = Array.isArray(flows) ? flows : [];
  if (list.length === 0) return "none yet";
  const on = list.filter((f) => f?.enabled).length;
  return on === 0 ? `${list.length}, all off` : `${list.length}, ${on} on`;
}

const fieldsOf = (cron) => (typeof cron === "string" ? cron.trim().split(/\s+/) : []);
const inRange = (v, max) => /^\d{1,2}$/.test(v) && Number(v) <= max;

// Words only the forms whose meaning is the same in standard cron and in the server's scheduler, and
// null for anything else, a numeric day of week included.
export function cronHuman(cron) {
  const f = fieldsOf(cron);
  if (f.length !== 5) return null;
  const [m, h, dom, mon, dow] = f;
  const stars = dom === "*" && mon === "*";
  if (f.every((x) => x === "*")) return "every minute";
  const step = /^\*\/(\d{1,2})$/.exec(m);
  if (step && h === "*" && stars && dow === "*" && Number(step[1]) >= 1 && Number(step[1]) <= 59) {
    return Number(step[1]) === 1 ? "every minute" : `every ${Number(step[1])} minutes`;
  }
  if (inRange(m, 59) && h === "*" && stars && dow === "*") return `hourly at :${twoDigits(m)}`;
  if (inRange(m, 59) && inRange(h, 23) && stars && dow === "*") return `daily at ${twoDigits(h)}:${twoDigits(m)}`;
  if (inRange(m, 59) && inRange(h, 23) && stars && dow.toLowerCase() === "mon-fri") return `weekdays at ${twoDigits(h)}:${twoDigits(m)}`;
  return null;
}

// The server's scheduler (APScheduler) counts the day of week from Monday as 0, unlike standard cron;
// test_herdr_app_cao_window_cron_premise.py checks that against the installed APScheduler. A digit in
// that field gets this warning; the next runs the server returns are authoritative.
export function numericWeekdayWarning(cron) {
  const f = fieldsOf(cron);
  if (f.length !== 5 || !/\d/.test(f[4])) return null;
  return "This server counts days of the week from Monday as 0, so 1-5 is Tuesday to Saturday. Use names such as mon-fri; the next runs below show what it will do.";
}

// A disabled flow's stored next_run is stale when it is in the past. The stored time has no zone;
// offsetMinutes is the server's offset east of UTC (UTC-8 is -480), read from a validate answer's next run.
// Without it the time counts as past only when it is past in every zone: even at UTC-12.
export function staleNextRun(flow, now, offsetMinutes) {
  if (flow?.enabled !== false || typeof flow.next_run !== "string" || flow.next_run === "") return false;
  const zoned = /(?:Z|[+-]\d{2}:?\d{2})$/.test(flow.next_run);
  const stored = Date.parse(zoned ? flow.next_run : `${flow.next_run}Z`);
  if (!Number.isFinite(stored)) return false;
  if (zoned) return stored < now;
  return offsetMinutes == null ? stored + 12 * 3_600_000 < now : stored - offsetMinutes * 60_000 < now;
}

// Adds a message under a field; "__proto__" and non-text names land under "form", so a server's
// field name can never write through the prototype.
function addAt(map, field, message) {
  const key = typeof field === "string" && field !== "" && field !== "__proto__" ? field : "form";
  if (!Object.hasOwn(map, key)) map[key] = [];
  map[key].push(String(message ?? ""));
}

// A POST /flows/validate answer -> errors at their fields (in the server's order), the next runs, and
// whether Create is allowed: only on a well-formed answer that says valid with no error. For Edit pass
// {editing: <the flow's name>, name: <the name in the form>}: the route checks the name as for a new
// flow, so when the form still holds the edited flow's own name, the error that says it is taken is the
// flow itself and is dropped, and the answer then counts as valid.
export function flowChecksFromValidate(result, { editing, name } = {}) {
  const byField = {};
  const answer = result && typeof result === "object" && !Array.isArray(result) ? result : null;
  const errors = answer && Array.isArray(answer.errors) ? answer.errors : null;
  const nextRuns = answer && Array.isArray(answer.next_runs) ? answer.next_runs : [];
  if (!answer || !errors || typeof answer.valid !== "boolean") return { byField, nextRuns, canCreate: false };
  const ownName = hasText(editing) && (name === undefined || name === editing);
  let dropped = 0;
  let kept = 0;
  for (const e of errors) {
    if (e && typeof e === "object" && ownName && e.field === "name" && String(e.message).toLowerCase() === FLOW_NAME_TAKEN_MESSAGE) { dropped += 1; continue; }
    kept += 1;
    if (e && typeof e === "object") addAt(byField, e.field, e.message);
  }
  const valid = answer.valid === true ? errors.length === 0 : dropped > 0 && kept === 0;
  return { byField, nextRuns, canCreate: valid };
}

// Both 422 shapes: FastAPI's {detail: [{loc, msg}]} (a failed body parse) and ours
// {detail: {errors: [{field, message}]}} (a failed definition). A message with no field, or one for the
// whole body, is general. Messages are the server's text, unchanged.
export function fieldErrorsFrom422(json) {
  const out = { byField: {}, general: [] };
  const d = json?.detail;
  if (Array.isArray(d)) {
    for (const e of d) {
      if (typeof e === "string") { out.general.push(e); continue; }
      if (!e || typeof e !== "object") continue;
      const loc = Array.isArray(e.loc) ? e.loc.filter((x) => typeof x === "string") : [];
      const field = loc.find((x) => x !== "body" && x !== "query");
      if (field) addAt(out.byField, field, e.msg);
      else out.general.push(String(e.msg ?? ""));
    }
  } else if (d && typeof d === "object" && Array.isArray(d.errors)) {
    for (const e of d.errors) {
      if (!e || typeof e !== "object") continue;
      if (hasText(e.field)) addAt(out.byField, e.field, e.message);
      else out.general.push(String(e.message ?? ""));
    }
  } else if (typeof d === "string") {
    out.general.push(d);
  }
  return out;
}

const UNATTENDED = "This schedule launches an agent with nobody watching, and a schedule's script runs a local file when one is set.";
// The dialog for each write the window offers. There is no delete and no other action: anything else throws.
export function confirmSpec(action) {
  const specs = {
    enable: { title: "Enable this schedule?", body: UNATTENDED, confirmLabel: "Enable", defaultFocus: "cancel", timeoutSeconds: 10 },
    run: { title: "Run this schedule now?", body: UNATTENDED, confirmLabel: "Run now", defaultFocus: "cancel", timeoutSeconds: 180 },
    createAndEnable: { title: "Create this schedule and enable it?", body: UNATTENDED, confirmLabel: "Create and enable", defaultFocus: "cancel", timeoutSeconds: 10 },
    disable: { title: "Disable this schedule?", body: "It will not run on its schedule until you enable it again.", confirmLabel: "Disable", defaultFocus: "confirm", timeoutSeconds: 10 },
  };
  if (typeof action !== "string" || !Object.hasOwn(specs, action)) throw new Error(`no dialog for schedule action ${String(action).slice(0, 30)}`);
  return { ...specs[action], cancelLabel: "Cancel", confirmEnabled: true };
}

// What a missing POST /flows/validate (an older server) changes in the panel: 404 or 405 only.
export function routesState(validateStatus) {
  if (validateStatus === 404 || validateStatus === 405) {
    return { missing: true, label: HONESTY.needsCaoChange, nextRunsNote: `${HONESTY.needsCaoChange}: POST /flows/validate`, editDisabled: true, createLabel: HONESTY.serverChecksSkipped };
  }
  return { missing: false, label: null, nextRunsNote: null, editDisabled: false, createLabel: null };
}
export const flowRunsNote = () => ({ text: "Run history is not recorded; only the live session is listed.", label: HONESTY.needsCaoChange });

// One callCao outcome of enable, disable or run now -> what the details say. A failure keeps the switch
// where it was and shows the server's message. A run answer that is not a plain executed true or false is
// reported as unclear, never as either. No answer to Run now after its 180 s limit does not mean the flow
// did not run, and the text says so.
export function writeOutcome(action, outcome) {
  if (action !== "enable" && action !== "disable" && action !== "run") throw new Error(`no outcome for schedule action ${String(action).slice(0, 30)}`);
  if (outcome?.kind === "auth") return { type: "auth", text: outcome.message ?? "" };
  if (outcome?.kind === "ok") {
    if (action !== "run") return { type: "done", text: action === "enable" ? "Enabled." : "Disabled." };
    const executed = outcome.json?.executed;
    if (executed === true) return { type: "done", text: "The schedule ran." };
    if (executed === false) return { type: "done", text: "The run was skipped (its script said no, or its session was still working)." };
    return { type: "unclear", text: "CAO answered, but not whether the schedule ran. Look at its session before running it again." };
  }
  if (outcome?.kind === "unreachable") {
    const text = action === "run"
      ? "CAO did not answer in time. The schedule may still be running: look for its cao-flow session before running it again."
      : "CAO did not answer.";
    return { type: "failed", text, keepSwitch: true };
  }
  return { type: "failed", text: String(outcome?.detail ?? outcome?.message ?? "failed"), keepSwitch: true };
}

// POST /flows takes exactly these fields. A draft cannot add a script or an engine, and a flow is
// created disabled unless Create and enable was chosen (a boolean true, nothing else).
export function flowCreateBody(form, { enable = false } = {}) {
  return {
    name: form.name, schedule: form.schedule, agent_profile: form.agent_profile, provider: form.provider,
    prompt_template: form.prompt, enabled: enable === true,
  };
}
// PUT /flows/{name} takes only what changed, and never a name, a script or an enabled flag.
export function flowUpdateBody(before, after) {
  const body = {};
  for (const k of ["schedule", "agent_profile", "provider"]) if (after[k] !== before[k]) body[k] = after[k];
  const prompt = after.prompt ?? after.prompt_template;
  if (prompt !== undefined && prompt !== (before.prompt_template ?? "")) body.prompt_template = prompt;
  return body;
}

// New flow from a workflow's Schedule...: the agent runs the workflow through the CLI. The command is on
// its own line, quoted for a shell, so an input value cannot add to it.
export function schedulePrompt(workflow, inputs = {}) {
  if (typeof workflow !== "string" || !/^[A-Za-z0-9_-]+$/.test(workflow)) throw new Error("a workflow is named by a bare name");
  const parts = ["cao", "workflow", "run", workflow];
  for (const [k, v] of Object.entries(inputs ?? {})) {
    if (k === "" || k.includes("=")) throw new Error("an input name cannot be empty or hold =");
    parts.push("--input", shq(`${k}=${v}`));
  }
  return `Run the CAO workflow "${workflow}" with this command:\n${parts.join(" ")}\nReport the run id and the result. If it stops for plan approval, say so and stop.`;
}
export const scheduleWarning = () => "An unattended run stops at plan approval unless the plan is approved for these inputs.";

// ---------- write controls in the panel (task 9.2) ----------

// Whether a write control works now. `views`: the {auth, unreachable} flags it depends on (one or a list);
// retry: it is the Retry of a launch whose answer was lost (the way out of "unreachable", so it passes
// then); connected: the CAO machine is listed. Auth wins, then not connected, then unreachable.
export function writeGate(views, { retry = false, connected = true } = {}) {
  const list = (Array.isArray(views) ? views : [views]).filter(Boolean);
  if (list.some((v) => v.auth === true)) return { disabled: true, reason: "auth", text: AUTH_MESSAGE };
  if (connected === false) return { disabled: true, reason: "not_connected", text: "CAO machine not connected" };
  if (!retry && list.some((v) => v.unreachable === true)) return { disabled: true, reason: "unreachable", text: "CAO unreachable, retrying" };
  return { disabled: false, reason: null, text: null };
}

// ---------- the Flows section (task 10.2) ----------

// /flows/<name>, or /flows/<name>/<enable|disable|run>. null when the name is not a bare name or the
// action is not one of those three.
export function flowRoute(name, action) {
  try { seg(name); } catch { return null; }
  if (action === undefined) return `/flows/${name}`;
  return action === "enable" || action === "disable" || action === "run" ? `/flows/${name}/${action}` : null;
}

// The rows of a GET /flows answer the section draws: well-formed names only, the fields it reads.
export function flowRows(data) {
  return (Array.isArray(data) ? data : []).filter((f) => f && typeof f === "object" && flowRoute(f.name) !== null).map((f) => ({
    name: f.name, schedule: typeof f.schedule === "string" ? f.schedule : "", enabled: f.enabled === true, next_run: typeof f.next_run === "string" ? f.next_run : null,
  }));
}
// What the Flows block shares with the other mods (shared storage is saved to disk, so no prompt text).
export const publishedFlows = (rows) => (Array.isArray(rows) ? rows : []).map((f) => ({ name: f.name, schedule: f.schedule, enabled: f.enabled }));

// Whether the Flows block fetches GET /flows now: it is shown, nothing is in flight, and the last fetch is
// 30 s old (or there was none), or a write moved the refresh stamp since the last fetch.
export function flowsPollDue({ shown, inFlight, lastAt, stamp, seenStamp } = {}, now) {
  if (shown !== true || inFlight === true) return false;
  return !Number.isFinite(lastAt) || now - lastAt >= FLOWS_POLL_MS || stamp !== seenStamp;
}

// Flows whose Run now is in flight, kept as {name: startedAtMs} in shared storage. An entry older than the
// 180 s limit of the request (plus a margin) no longer counts, so a window that died mid-run leaves nothing stuck.
export const RUN_NOW_WINDOW_MS = 190_000;
const liveRuns = (map, now) => Object.entries(map && typeof map === "object" && !Array.isArray(map) ? map : {})
  .filter(([, at]) => Number.isFinite(at) && now - at >= 0 && now - at < RUN_NOW_WINDOW_MS);
export const runningFlows = (map, now) => liveRuns(map, now).map(([name]) => name);
export const markRunning = (map, name, now) => ({ ...Object.fromEntries(liveRuns(map, now)), [name]: now });
export const clearRunning = (map, name, now) => Object.fromEntries(liveRuns(map, now).filter(([n]) => n !== name));

// Whether the first terminal of session cao-flow-<name> works (a flow shows "running" then).
export const flowSessionWorking = (sessions, name) =>
  (Array.isArray(sessions) ? sessions : []).find((s) => s.name === `cao-flow-${name}`)?.terms?.[0]?.state === "working";

// What the panel says about a failed or stale list view {data, at, auth, unreachable, error}: the auth
// message, "CAO unreachable", the server's error, and (with data kept) how old the rows on screen are.
export function viewNotes(view, now) {
  const v = view ?? {};
  const lines = [];
  if (v.auth === true) lines.push(AUTH_MESSAGE);
  if (v.unreachable === true) lines.push("CAO unreachable, retrying");
  if (v.error) lines.push(`CAO answered with an error: ${String(v.error).slice(0, 200)}`);
  if (lines.length > 0 && v.at != null && v.data != null) {
    const age = ageWords(now - v.at);
    lines.push(`Showing the last list, ${age === "just now" ? age : `${age} old`}`);
  }
  return lines;
}

// ---------- flow details (task 10.3) ----------

const DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
// "Mon 2026-11-23 09:00" from a server time (wall clock as the server wrote it); the text itself when it does not read.
export function runWords(iso) {
  const wall = wallClockOf(iso);
  if (!wall) return typeof iso === "string" ? iso : "";
  return `${DAY_NAMES[new Date(`${wall.slice(0, 10)}T00:00:00Z`).getUTCDay()]} ${wall}`;
}
// The offset east of UTC, in minutes, that a server time carries ("...-08:00" is -480); null when it has none.
export function offsetMinutesOf(iso) {
  if (typeof iso !== "string") return null;
  const m = /[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|([+-])(\d{2}):?(\d{2}))$/.exec(iso);
  if (!m) return null;
  if (m[1] === undefined) return 0;
  return Number(m[2]) <= 23 && Number(m[3]) <= 59 ? (m[1] === "-" ? -1 : 1) * (Number(m[2]) * 60 + Number(m[3])) : null;
}

// The answer of POST /flows/validate -> {status, checks}: the HTTP status the routesState test reads
// (null when no answer came) and flowChecksFromValidate of the body (null unless it answered 2xx).
export function validateResult(outcome, { editing, name } = {}) {
  const status = outcome?.kind === "ok" || outcome?.kind === "error" ? outcome.status : null;
  return { status, checks: outcome?.kind === "ok" ? flowChecksFromValidate(outcome.json, { editing, name }) : null };
}

// The prompt template of the flow's details stays collapsed: closed gives the control "Show prompt" and no text;
// open gives the text as the server has it and "Hide prompt"; a flow with no prompt gives the note "not available"
// and no control. -> {control, text, note}
export function promptView(flow, open) {
  const text = flow?.prompt_template;
  if (typeof text !== "string" || text.trim() === "") return { control: null, text: null, note: "not available" };
  return open === true ? { control: "Hide prompt", text, note: null } : { control: "Show prompt", text: null, note: null };
}

// The details panel of one flow, from its GET /flows/{name} row, the next runs of POST /flows/validate
// (ISO strings) and that call's HTTP status. A disabled schedule shows "None. The schedule is disabled", its stored
// next run (marked stale when past) and the runs enabling would give; an older server (404 or 405 on
// validate) shows only the stored next run, with the label. The prompt template is only in `prompt`, the
// collapsed view the right sidebar draws (promptView, open when `promptOpen` is true): no other field carries it.
export function flowDetailsView({ flow, nextRuns, validateStatus, now, promptOpen } = {}) {
  const f = flow ?? {};
  const routes = routesState(validateStatus);
  const runs = (Array.isArray(nextRuns) ? nextRuns : []).map(runWords);
  const stored = wallClockOf(f.next_run);
  const stale = staleNextRun(f, now, offsetMinutesOf(Array.isArray(nextRuns) ? nextRuns[0] : undefined));
  let next;
  if (routes.missing) {
    // No validate route, so no runs to show and none invented: the stored next run, and for a disabled flow
    // that it is disabled and whether the stored time is stale.
    const off = f.enabled !== true;
    next = { heading: "Next run", lines: [], none: off ? "None. The schedule is disabled." : null, stored: stored ? { text: stored, stale: off && stale } : null, ifEnabled: [], note: routes.nextRunsNote, label: routes.label };
  } else if (f.enabled === true) next = { heading: "Next 3 runs", lines: runs, none: runs.length === 0 ? "The server gave no next runs." : null, stored: null, ifEnabled: [], note: null, label: null };
  else next = { heading: "Next 3 runs", lines: [], none: "None. The schedule is disabled.", stored: stored ? { text: stored, stale } : null, ifEnabled: runs, note: null, label: null };
  return {
    name: f.name ?? "",
    enabled: f.enabled === true,
    enabledText: f.enabled === true ? "Enabled" : "Disabled",
    schedule: { words: cronHuman(f.schedule), cron: f.schedule ?? "", note: "In the server's local time. Schedules fire only while cao-server is running." },
    next,
    lastRun: { text: f.last_run == null || f.last_run === "" ? "never" : (wallClockOf(f.last_run) ?? "unknown"), note: "CAO records only this time." },
    target: { profile: f.agent_profile ?? "", provider: f.provider ?? "", script: typeof f.script === "string" && f.script !== "" ? f.script : null },
    prompt: promptView(f, promptOpen === true),
    session: `cao-flow-${f.name ?? ""}`,
    runsNote: flowRunsNote(),
    editDisabled: routes.editDisabled,
    editLabel: routes.editDisabled ? routes.label : null,
  };
}

// The fields of a GET /flows/{name} row as the form and validate bodies name them.
export const fieldsOfFlow = (flow) => ({
  name: flow?.name ?? "", schedule: flow?.schedule ?? "", agent_profile: flow?.agent_profile ?? "", provider: flow?.provider ?? "", prompt: flow?.prompt_template ?? "",
});
// The body of POST /flows/validate for these fields.
export const validateBody = (fields) => ({ name: fields.name, schedule: fields.schedule, agent_profile: fields.agent_profile, provider: fields.provider, prompt_template: fields.prompt });

// ---------- New flow and Edit (task 10.5) ----------

// ---- New flow and Edit ----
// The key that says which validate answer belongs to which fields.
export const flowCheckKey = (fields) => JSON.stringify(validateBody(fields));
export const FLOW_PRESETS = Object.freeze([
  { label: "Every day 09:00", cron: "0 9 * * *" },
  { label: "Weekdays 09:00", cron: "0 9 * * mon-fri" },
  { label: "Hourly", cron: "0 * * * *" },
]);
// A new flow's form; `workflow` preselects "a workflow" (Schedule... on a workflow).
export const emptyFlowForm = ({ workflow } = {}) => ({ name: "", kind: workflow ? "workflow" : "prompt", workflow: workflow ?? "", schedule: "", profile: "", provider: "", prompt: "" });
// The Edit form of a GET /flows/{name} row.
export const flowFormOf = (flow) => ({
  name: flow?.name ?? "", kind: "prompt", workflow: "", schedule: flow?.schedule ?? "", profile: flow?.agent_profile ?? "", provider: flow?.provider ?? "", prompt: flow?.prompt_template ?? "",
});
// The values the flow routes take: the provider falls back to the profile's, else kiro_cli; a workflow's prompt is generated.
export function flowFields(form, profile) {
  let prompt = form.prompt;
  if (form.kind === "workflow") {
    try { prompt = schedulePrompt(form.workflow); } catch { prompt = ""; }
  }
  return { name: form.name, schedule: form.schedule, agent_profile: form.profile, provider: form.provider || profile?.provider || "kiro_cli", prompt };
}

// Server field names -> the form's: agent_profile -> profile, prompt_template -> prompt, the rest as is,
// anything else under "form". A map {field: [messages]} in, the same shape out.
export function formErrorsOf(byField) {
  const out = {};
  const to = { name: "name", schedule: "schedule", provider: "provider", agent_profile: "profile", prompt_template: "prompt", prompt: "prompt" };
  for (const [k, list] of Object.entries(byField ?? {})) {
    const key = Object.hasOwn(to, k) ? to[k] : "form";
    out[key] = [...(out[key] ?? []), ...list];
  }
  return out;
}
// The errors the form shows: a field's once the user touched it (or tried to save), and the general ones always.
export function shownErrors(errors, touched = {}, attempted = false) {
  return Object.fromEntries(Object.entries(errors ?? {}).filter(([k]) => attempted || k === "form" || touched[k] === true));
}
// The first field with an error, in the form's order; the one that takes focus.
export const firstErrorField = (errors) => ["name", "schedule", "profile", "provider", "prompt", "form"].find((k) => (errors?.[k] ?? []).length > 0) ?? null;

// What the form says in place of Create when the server has no POST /flows/validate: its POST /flows arms the flow
// and overwrites the file of a flow with the same name, and the window cannot check either beforehand, so the flow
// is made from a terminal on the CAO machine.
export const CREATE_VIA_CLI = "This server cannot check a schedule first, and its create would arm the schedule and overwrite a schedule with the same name. "
  + "Create it in a terminal on the CAO machine with `cao schedule add <file>`, then keep it from running with `cao schedule disable <name>` until you enable it here.";

// Whether Create (and Create and enable) work: not under a gate, not while saving, and then only when the
// server's check of exactly these fields says valid. With no validate route (an older server) Create stays off
// whatever else is true: `label` is "server checks skipped" and `hint` names the CLI commands.
export function flowFormGate({ fields, checks, checkedKey, routes, gate, sending } = {}) {
  const filled = ["name", "schedule", "agent_profile", "provider", "prompt"].every((k) => hasText(fields?.[k]));
  if (routes?.missing === true) return { create: false, label: routes.createLabel, filled, hint: CREATE_VIA_CLI };
  const create = !gate?.disabled && sending !== true && filled && checks?.canCreate === true && checkedKey === flowCheckKey(fields);
  return { create, label: null, filled };
}

// One callCao outcome of POST /flows (mode "create") or PUT /flows/{name} ("edit") -> what the form does.
// Types: saved, conflict (a name that exists, at the name field), fields (422 at the named fields), auth,
// unreachable (the write may have happened), failed.
export function flowSaveOutcome(outcome, { mode, enable = false } = {}) {
  const kind = outcome?.kind;
  if (kind === "auth") return { type: "auth", text: outcome.message ?? "" };
  if (kind === "ok") {
    // A server older than `enabled` ignores it and arms the flow; the answer says so, and the window says it too.
    const armed = mode !== "edit" && enable !== true && outcome.json?.enabled === true;
    return { type: "saved", name: typeof outcome.json?.name === "string" ? outcome.json.name : null, armed };
  }
  if (kind === "unreachable") {
    return { type: "unreachable", text: mode === "edit"
      ? "CAO did not answer. The edit may have been applied: look at the schedule's details before sending it again."
      : "CAO did not answer. The schedule may exist: look in the Schedules list before creating it again." };
  }
  if (kind === "error" && outcome.status === 409 && mode !== "edit") return { type: "conflict", field: "name", text: "a schedule with that name exists" };
  if (kind === "error" && outcome.status === 422) {
    const e = fieldErrorsFrom422(outcome.json);
    return { type: "fields", errors: formErrorsOf(e.byField), general: e.general };
  }
  return { type: "failed", text: String(outcome?.detail ?? outcome?.message ?? "failed") };
}

// The workflow names GET /workflows lists that the window can name in a prompt (bare names only).
export const workflowChoices = (data) => (Array.isArray(data) ? data : []).map((w) => w?.name).filter((n) => typeof n === "string" && /^[A-Za-z0-9_-]+$/.test(n));

// Whether a flow form holds something typed (the form against what it opened with).
export const flowFormDirty = (form, initial) => JSON.stringify(form) !== JSON.stringify(initial);

// ---------- panel write gate ----------

// The views whose answers can switch write controls off: the GETs and the read-only validator. A write's own
// 401 or 403 is shown as its message and never disables its control (it would stay off for good, since a
// disabled control cannot be tried again).
export const READ_VIEWS = Object.freeze(["profiles", "providers", "workflows", "profile", "flow", "flowCheck"]);

// The gate of a panel write control: `views` is {name: applyOutcome view}, `shared` the {auth, unreachable}
// the Sessions block publishes; auth comes from the read views only. opts as writeGate.
export function panelGate(views, shared, opts = {}) {
  const auth = READ_VIEWS.some((n) => views?.[n]?.auth === true);
  return writeGate([shared, { auth }], opts);
}

// ---------- what a flow save does ----------

// What pressing Create, Create and enable or Save does. gate: writeGate's answer; state: flowFormGate's;
// routesMissing: no validate route; sending: a save is in flight. Under a gate, or while a save is out,
// nothing happens ("gated": no check, no focus move). With a problem, or with no validate route (Create is off
// there whatever the fields hold), nothing is sent ("problems"): the banner
// to show, `runCheck` (ask the server to check again, whenever the validate route exists, so a press always
// shows a fresh answer; an older server is not asked), and the keys go to the first error. Otherwise
// "confirm" (Create and enable) or "send".
export function flowSaveAttempt({ gate, state, enable, routesMissing, sending } = {}) {
  if (gate?.disabled === true || sending === true) return { action: "gated" };
  if (routesMissing === true || state?.create !== true) {
    return { action: "problems", banner: routesMissing === true && state?.filled !== true ? "Fill in the name, schedule, profile and prompt first." : null, runCheck: routesMissing !== true };
  }
  return { action: enable === true ? "confirm" : "send" };
}

// What a flow save answer (flowSaveOutcome) does to the form. Saved: leave the form, select `selectName`, and
// say so when a server that ignores `enabled: false` armed the flow. Otherwise the errors to keep at their
// fields and the banner (a 422's general messages, or the failure text; an auth failure reads `authText`).
export function flowSaveEffects(r, { name, authText } = {}) {
  if (r?.type === "saved") {
    return {
      done: true, selectName: r.name ?? name,
      warning: r.armed ? "This server ignored enabled: false and created the schedule enabled. Use the switch to disable it if you did not mean that." : null,
    };
  }
  let serverErrors = {};
  let banner = null;
  if (r?.type === "conflict") serverErrors = { name: [r.text] };
  else if (r?.type === "fields") { serverErrors = r.errors; if (r.general.length > 0) banner = r.general.join("; "); }
  else banner = r?.type === "auth" ? authText : r?.text;
  return { done: false, serverErrors, banner };
}

// ---------- why a check gave no answer, and the CLI for a flow too large ----------

// The CLI command a flow too large for this window is written with (also the way to change one's file).
export const FLOW_CLI = "cao schedule add";

// The note that explains why a validate call gave no answer: CAO did not answer, the request was refused
// before it was sent (with its message), or the server answered with an error other than a missing route.
export function validateNote(outcome) {
  const kind = outcome?.kind;
  if (kind === "unreachable") return "CAO did not answer, so the checks could not run";
  if (kind === "too_large" || kind === "refused" || kind === "env_refused") return String(outcome.message ?? "the request was not sent");
  if (kind === "error" && outcome.status !== 404 && outcome.status !== 405) return `CAO could not check the schedule: ${String(outcome.detail ?? "error")}`;
  return null;
}

// ---------- a flow dialog names its flow ----------

// The dialog of a flow write, naming the flow in its title: confirmSpec(action) with the title for `name`.
export function flowDialogSpec(action, name) {
  const spec = confirmSpec(action);
  const titles = { enable: `Enable schedule ${name}?`, disable: `Disable schedule ${name}?`, run: `Run schedule ${name} now?`, createAndEnable: `Create schedule ${name} and enable it?` };
  return { ...spec, title: titles[action] };
}
