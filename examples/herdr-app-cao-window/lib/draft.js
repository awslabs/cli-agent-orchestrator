// Draft with AI (proposed): an author agent session writes a definition; the window parses it into the
// form, validates it, and always deletes the session. Pure: draftStep is a reducer whose effects the mod
// runs. Nothing here saves, enables or runs anything. Top-level names are unique across lib/*.js, because bundle
// mode puts every shared module in one scope.
//
// The mod's side of the contract:
//   - effects run in order; `launch` carries the attempt number it belongs to;
//   - an answer event (launched, output, deleted, validated) carries `attempt` set to state.attempt as it
//     was when the effect started, so a late answer for an earlier attempt is ignored and cannot fill
//     the form of the next one or end its session. An event without `attempt` is taken as current;
//   - `read` is answered by `output`, `delete` by `deleted`, `fill` by running the form's own
//     validation and then `validated`; `record` and `forget` write shared("caowin.drafts");
//   - `deleted` carries `ok` and the HTTP `status` of the DELETE (outcome.status, absent when no answer came).
//     A 404 means the session is already gone and counts as done. Without `status` a 404 reads as a failure:
//     it is retried once and then reported as "delete the session by hand".
import { HONESTY } from "./honesty.js";
import { enforcementFor } from "./launch.js";

export const DRAFT_TIMEOUT_MS = 180_000;
const DRAFT_FORMATS = Object.freeze({
  flow: 'a JSON object with the string fields name (letters, digits, - and _, at most 64), schedule (a 5-field cron expression; write weekdays with names, for example "0 9 * * mon-fri", never "1-5"), agent_profile, provider and prompt_template.',
  workflow: 'a JSON object with the string fields name (a bare name), tier ("python" or "yaml") and source (the complete text of the workflow, as one JSON string).',
});
const DRAFT_FIELDS = Object.freeze({
  flow: Object.freeze(["name", "schedule", "agent_profile", "provider", "prompt_template"]),
  workflow: Object.freeze(["name", "tier", "source"]),
});
// The Create form calls the flow's prompt `prompt`; the definition calls it `prompt_template`.
const DRAFT_FORM_KEY = Object.freeze({ prompt_template: "prompt" });
// The name of one of this window's own author sessions, as draftRequest builds it (`sessionName`): the server's
// cao- prefix, draft, a kind and the 8 hex characters. It is the only kind of session the window ever deletes,
// so draftCall and the author records of lib/shell.js both ask it.
const DRAFT_NAME_RE = new RegExp(`^cao-draft-(${Object.keys(DRAFT_FORMATS).join("|")})-[0-9a-f]{8}$`);
export const isAuthorSession = (name) => typeof name === "string" && DRAFT_NAME_RE.test(name);
const formKeyOf = (field) => (Object.hasOwn(DRAFT_FORM_KEY, field) ? DRAFT_FORM_KEY[field] : field);

// Who may draft, checked in this order: a configured author profile; a provider that enforces the tool
// restriction natively and at launch (not an install-time one, where `allowed_tools=fs_read` is only an
// override the installed agent ignores); a provider listed in draftProviders (default claude_code); an
// installed provider; and a directory the window has launched from.
export function draftAllowed({ profile, providerName, draftProviders, providers, workdir } = {}) {
  if (!hasName(profile)) return { ok: false, code: "no_profile", reason: "Set draftProfile in the layout props to the profile the author should run as (install.sh --prop 'draftProfile=\"<profile>\"')." };
  const e = enforcementFor(providerName, ["fs_read"]);
  if (e.installTime) {
    return { ok: false, code: "install_time", reason: `${providerName} is an install-time provider: its tool policy is the installed agent's, so a launch override cannot restrict the author. Pick another provider in draftProviders.` };
  }
  if (e.level !== "native") return { ok: false, code: "not_native", reason: "The author must run on a provider that enforces tool restrictions." };
  const allowed = Array.isArray(draftProviders) ? draftProviders : ["claude_code"];
  if (!allowed.includes(providerName)) return { ok: false, code: "provider_not_allowed", reason: `${providerName} is not in draftProviders.` };
  if (!(Array.isArray(providers) ? providers : []).some((p) => p?.name === providerName && p.installed === true)) {
    return { ok: false, code: "not_installed", reason: `${providerName} is not installed on the CAO machine.` };
  }
  if (!hasName(workdir)) return { ok: false, code: "no_workdir", reason: "Set a working directory in New session first." };
  return { ok: true };
}
const hasName = (v) => typeof v === "string" && v.trim() !== "";

// The description first, then the format, then the instruction: text inside the description cannot be
// the last word.
const messageFor = (kind, description) =>
  `Draft a CAO ${kind} for me. What I want:\n${description}\n\nTarget format: ${DRAFT_FORMATS[kind]}\n\n`
  + "Write no files and start no sessions. Reply with only the definition, in one fenced block (```json ... ```).";

// The author launch: fs_read only, a draft-<kind>-<8 hex> name (the server stores cao-<that>), the last
// directory, a key, and the message in the body only. The kind, the hex and the key are checked here,
// because they decide a session name and a retry.
export function draftRequest({ kind, description, profile, provider, workdir, idempotencyKey, hex } = {}) {
  if (typeof kind !== "string" || !Object.hasOwn(DRAFT_FORMATS, kind)) throw new Error(`unknown draft kind ${String(kind).slice(0, 20)}`);
  if (typeof hex !== "string" || !/^[0-9a-f]{8}$/.test(hex)) throw new Error("a draft needs an 8-character lowercase hex suffix");
  if (typeof idempotencyKey !== "string" || idempotencyKey === "") throw new Error("a draft needs an idempotency key");
  return {
    method: "POST",
    path: "/sessions",
    query: { agent_profile: profile, provider, allowed_tools: "fs_read", session_name: `draft-${kind}-${hex}`, working_directory: workdir, idempotency_key: idempotencyKey },
    body: { initial_message: messageFor(kind, String(description ?? "")) },
    sessionName: `cao-draft-${kind}-${hex}`,
    cli: "cao launch",
  };
}
// "Fix with AI": a new author session whose message carries the wish, the previous definition and its
// errors. The caller passes a fresh hex and key for the new session.
export function fixRequest(params, previous, errors) {
  const list = Array.isArray(errors) ? errors : [];
  const description = `${params.description}\n\nYour previous definition:\n${JSON.stringify(previous?.fields ?? {}, null, 2)}\n\nIt failed validation:\n${list.join("\n")}\nFix those problems and reply with the whole corrected definition.`;
  return draftRequest({ ...params, description });
}

// The last fenced block of the author's answer: {ok, lang, body}, or {ok: false, reason: "none" | "malformed"}
// (a block that is opened and never closed). A fence is a line of three backticks with an optional language
// tag; only a bare line closes a block. Read line by line, so long input takes linear time.
export function parseLastFence(text) {
  if (typeof text !== "string") return { ok: false, reason: "none" };
  let open = null;
  let last = null;
  let any = false;
  for (const line of text.replace(/\r\n?/g, "\n").split("\n")) {
    const t = line.trim();
    const tag = t.startsWith("```") ? t.slice(3).trim() : null;
    if (tag === null || !/^[A-Za-z0-9_+-]*$/.test(tag) || (open && tag !== "")) {
      if (open) open.lines.push(line);
      continue;
    }
    any = true;
    if (open) {
      last = { lang: open.lang, body: open.lines.join("\n") };
      open = null;
    } else {
      open = { lang: tag, lines: [] };
    }
  }
  if (open) return { ok: false, reason: "malformed" };
  return last ? { ok: true, ...last } : { ok: false, reason: any ? "malformed" : "none" };
}

// The fields of the block that the kind names, as strings, and nothing else: a draft never carries an
// enabled flag, a script, an engine or a path. A workflow needs a source and the tier python or yaml.
export function parseDraft(kind, fence) {
  if (typeof kind !== "string" || !Object.hasOwn(DRAFT_FIELDS, kind)) return { ok: false, reason: "unknown kind" };
  let obj;
  try { obj = JSON.parse(fence?.body); } catch { return { ok: false, reason: "not JSON" }; }
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) return { ok: false, reason: "not an object" };
  const fields = {};
  for (const k of DRAFT_FIELDS[kind]) if (Object.hasOwn(obj, k) && typeof obj[k] === "string") fields[k] = obj[k];
  if (Object.keys(fields).length === 0) return { ok: false, reason: "no recognised field" };
  if (kind === "workflow" && (typeof fields.source !== "string" || !["python", "yaml"].includes(fields.tier))) {
    return { ok: false, reason: "a workflow needs source and tier python or yaml" };
  }
  return { ok: true, kind, fields };
}

// Fills the form from a parsed draft and marks each filled field "AI". A field the user typed is
// overwritten (and marked). The form it is given is not changed, and only the kind's own fields are read.
export function applyDraft(form, parsed) {
  const fields = parsed?.fields && typeof parsed.fields === "object" ? parsed.fields : {};
  const known = Object.hasOwn(DRAFT_FIELDS, parsed?.kind) ? DRAFT_FIELDS[parsed.kind] : [];
  const out = { ...form };
  const marks = {};
  for (const k of known) {
    if (!Object.hasOwn(fields, k) || typeof fields[k] !== "string") continue;
    out[formKeyOf(k)] = fields[k];
    marks[formKeyOf(k)] = HONESTY.aiField;
  }
  return { form: out, marks };
}
// Editing a field drops its mark. A name that is not a mark (an inherited property) changes nothing.
export function clearMark(marks, field) {
  if (!Object.hasOwn(marks, field)) return marks;
  const rest = { ...marks };
  delete rest[field];
  return rest;
}

// The box's view model. Every phase carries "proposed". Fix with AI is offered only with validation errors.
export function draftBox(state) {
  const base = { label: HONESTY.proposed, title: "Draft with AI", phase: state?.phase };
  const errs = Array.isArray(state?.errors) ? state.errors : [];
  switch (state?.phase) {
    case "idle":
      return { ...base, text: "Describe what you want; an agent drafts it. Nothing is saved until you press Create.", actions: ["Draft"] };
    case "launching":
    case "drafting":
    case "reading":
      if (state.cancelled) return { ...base, text: "cancelling the draft…", actions: [] };
      return { ...base, text: `drafting… ${state.sessionName ?? ""}`.trim(), actions: ["Cancel"] };
    case "cleaning":
      return { ...base, text: "cleaning up the author session…", actions: [] };
    case "done":
      return { ...base, text: state.notice ?? (errs.length ? "The form is filled, but validation found problems." : "The form is filled. Check it before you create."), actions: errs.length ? ["Fix with AI"] : [] };
    default:
      return { ...base, text: state?.notice ?? "The draft failed.", actions: errs.length ? ["Retry", "Fix with AI"] : ["Retry"] };
  }
}

// The recorded author terminal ids (shared("caowin.drafts")). Neither function changes the list it is given.
const asList = (drafts) => (Array.isArray(drafts) ? drafts : []);
const isId = (id) => typeof id === "string" && id !== "";
export const recordDraft = (drafts, id) => (!isId(id) || asList(drafts).includes(id) ? asList(drafts) : [...asList(drafts), id]);
export const forgetDraft = (drafts, id) => (asList(drafts).includes(id) ? asList(drafts).filter((d) => d !== id) : asList(drafts));

export const initialDraft = (kind) => ({
  phase: "idle", kind, sessionName: null, terminalId: null, startedAt: null, notice: null,
  cancelled: false, lostLaunch: false, after: null, deleteAttempts: 0, errors: [], attempt: 0,
});

// Every path that ends the session passes through here, so no session is left without a delete or a report.
function startCleanup(state, after) {
  return [{ ...state, phase: "cleaning", after, deleteAttempts: 0, lostLaunch: false }, [{ type: "delete", sessionName: state.sessionName }]];
}
const BUSY = new Set(["launching", "drafting", "reading", "cleaning"]);
const isStale = (state, event) => event.attempt !== undefined && event.attempt !== state.attempt;

// The author session's whole life. Events: start, fix, launched, terminal, output, tick, cancel, discard,
// deleted, validated. Effects: launch, record, read, fill, delete, forget. One author session at a time:
// start and fix while one is out, running or being deleted do nothing, so its name and id are never lost.
export function draftStep(state, event, now) {
  switch (event?.type) {
    case "start":
    case "fix": {
      if (BUSY.has(state.phase) || typeof event.request?.sessionName !== "string") return [state, []];
      const attempt = (state.attempt ?? 0) + 1;
      const next = {
        ...initialDraft(state.kind), attempt, phase: "launching", sessionName: event.request.sessionName, startedAt: now,
        errors: event.type === "fix" ? state.errors : [],
      };
      return [next, [{ type: "launch", request: event.request, attempt }]];
    }
    case "launched": {
      if (state.phase !== "launching" || isStale(state, event)) return [state, []];
      const o = event.outcome;
      if (o?.kind === "ok" && o.status >= 200 && o.status < 300) {
        const id = isId(o.json?.id) ? o.json.id : null;
        const base = { ...state, terminalId: id, cancelled: false };
        const record = id ? [{ type: "record", id }] : [];
        if (state.cancelled) {
          const [c, fx] = startCleanup(base, { phase: "idle", notice: null });
          return [c, [...record, ...fx]];
        }
        if (!id) {
          const [c, fx] = startCleanup(base, { phase: "failed", notice: "The author session started but CAO did not say which terminal it is, so it cannot be followed. It is being deleted." });
          return [c, fx];
        }
        return [{ ...base, phase: "drafting" }, record];
      }
      // An answer (an error, a refusal) means no session was made. A lost answer (curl got no response)
      // means one may exist: Cancel or closing the form then deletes it by name, and a 404 means there was none.
      if (o?.kind === "unreachable") {
        const notice = `The author launch failed: CAO did not answer. The session ${state.sessionName} may have started. Closing this form deletes the session, or you can delete it by hand.`;
        if (state.cancelled) return startCleanup({ ...state, cancelled: false, lostLaunch: false, notice }, { phase: "idle", notice: null });
        return [{ ...state, phase: "failed", notice, lostLaunch: true }, []];
      }
      if (state.cancelled) return [{ ...state, phase: "idle", cancelled: false, notice: null }, []];
      return [{ ...state, phase: "failed", notice: `The author launch failed: ${String(o?.detail ?? o?.message ?? "an error")}.` }, []];
    }
    case "terminal":
      if (state.phase === "drafting" && isId(event.terminal) && event.terminal === state.terminalId
        && (event.state === "done" || event.state === "idle") && Number(event.turn) >= 1) {
        return [{ ...state, phase: "reading" }, [{ type: "read", id: state.terminalId }]];
      }
      return [state, []];
    case "output": {
      if (state.phase !== "reading" || isStale(state, event)) return [state, []];
      const fail = (notice) => startCleanup(state, { phase: "failed", notice });
      if (event.ok !== true) return fail("The author's output could not be read.");
      const fence = parseLastFence(event.text);
      const parsed = fence.ok ? parseDraft(state.kind, fence) : fence;
      if (!parsed.ok) return fail("The author's output had no definition.");
      const [c, fx] = startCleanup({ ...state, errors: [] }, { phase: "done", notice: null });
      return [c, [{ type: "fill", kind: state.kind, fields: parsed.fields }, ...fx]];
    }
    case "tick":
      if (state.phase === "drafting" && now - state.startedAt >= DRAFT_TIMEOUT_MS) return startCleanup(state, { phase: "failed", notice: "The draft timed out." });
      return [state, []];
    case "cancel":
    case "discard":
      if (state.phase === "launching") return [state.cancelled ? state : { ...state, cancelled: true }, []];
      if (state.phase === "drafting" || state.phase === "reading") return startCleanup(state, { phase: "idle", notice: null });
      if (state.phase === "failed" && state.lostLaunch) return startCleanup(state, { phase: "idle", notice: null });
      return [state, []];
    case "deleted": {
      if (state.phase !== "cleaning" || isStale(state, event)) return [state, []];
      const after = state.after ?? { phase: "idle", notice: null };
      // A 404 means the session is already gone, which is what the delete is for.
      if (event.ok === true || event.status === 404) {
        return [{ ...state, phase: after.phase, notice: after.notice, after: null, deleteAttempts: 0 }, state.terminalId ? [{ type: "forget", id: state.terminalId }] : []];
      }
      if (state.deleteAttempts < 1) return [{ ...state, deleteAttempts: 1 }, [{ type: "delete", sessionName: state.sessionName }]];
      const notice = `${after.notice ? `${after.notice} ` : ""}Could not delete the author session; delete the session by hand: ${state.sessionName}`;
      return [{ ...state, phase: after.phase === "idle" ? "failed" : after.phase, notice, after: null }, []];
    }
    case "validated":
      if (!["cleaning", "done", "failed"].includes(state.phase) || isStale(state, event)) return [state, []];
      return [{ ...state, errors: Array.isArray(event.errors) ? event.errors.map(String) : [] }, []];
    default:
      return [state, []];
  }
}
