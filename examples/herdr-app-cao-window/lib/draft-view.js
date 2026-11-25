// What the panel decides around the Draft with AI box (cao-window-ai-draft): which requests the reducer's
// effects become and which events their answers become, when an author session still needs attention, which
// box action means what, how a parsed draft lands in the two forms, and what counts as a validation error.
// Pure; lib/draft.js owns the reducer (draftStep) and the rules about the definition. Top-level names are
// unique across lib/*.js (bundle mode, D8).
import { seg } from "./cao.js";
import { applyDraft, draftAllowed, draftBox, isAuthorSession } from "./draft.js";
import { resolvedProvider } from "./launch.js";
import { findingWords, validateReport } from "./workflows-view.js";

// Whether the author session, or a launch that may have created one, is still the window's to clean up: the
// reducer is launching, drafting, reading or cleaning, or a launch answer was lost. While this holds, leaving
// the form asks first, and closing it sends the discard that deletes the session.
export function draftHolds(state) {
  const phase = state?.phase;
  return phase === "launching" || phase === "drafting" || phase === "reading" || phase === "cleaning" || (phase === "failed" && state.lostLaunch === true);
}

// The CAO call an effect of draftStep makes: the launch as draftRequest built it, the one read of the author's
// last answer, and the delete of the session by name. record, forget and fill make no call: null. A terminal id
// that is not a plain path segment throws, and so does a session name that is not one of the window's own author
// sessions (cao-draft-flow-<8 hex> or cao-draft-workflow-<8 hex>): no other session can be deleted from here.
export function draftCall(effect) {
  switch (effect?.type) {
    case "launch": return { ...effect.request, maxTime: 20 };
    case "read": return { path: `/terminals/${seg(effect.id)}/output`, query: { mode: "last" }, maxTime: 10 };
    case "delete":
      if (!isAuthorSession(seg(effect.sessionName))) throw new Error(`not an author session of this window: ${effect.sessionName.slice(0, 40)}`);
      return { method: "DELETE", path: `/sessions/${effect.sessionName}`, maxTime: 15 };
    default: return null;
  }
}

// The event that an effect's callCao outcome becomes, stamped with the attempt it belongs to (`attempt` is the
// reducer's state.attempt when the effect started): launch -> launched, read -> output (the answer's text, or not
// ok), delete -> deleted (ok, and the HTTP status so that a 404 counts as already gone).
export function draftAnswer(effect, outcome, attempt) {
  switch (effect?.type) {
    case "launch": return { type: "launched", outcome, attempt };
    case "read": {
      const text = outcome?.kind === "ok" && typeof outcome.json?.output === "string" ? outcome.json.output : null;
      return { type: "output", ok: text !== null, text: text ?? "", attempt };
    }
    case "delete": return { type: "deleted", ok: outcome?.kind === "ok", status: typeof outcome?.status === "number" ? outcome.status : undefined, attempt };
    default: return null;
  }
}

// The reducer's terminal event for the author terminal, from the model's terminals (Herdr's push data); null
// when the terminal is not listed.
export function draftTerminalEvent(terms, id) {
  const t = (Array.isArray(terms) ? terms : []).find((x) => x?.id === id);
  return t ? { type: "terminal", terminal: id, state: t.state, turn: t.turn } : null;
}

// The box's buttons: draftBox's own, plus "Draft again" once a draft is done with nothing to fix.
export function draftActionList(state) {
  const actions = draftBox(state).actions;
  return state?.phase === "done" && actions.length === 0 ? ["Draft again"] : actions;
}
// What a button means: start (a new author), fix (Fix with AI, only from its button), or cancel. While a launch
// whose answer was lost may have made a session, Retry and Fix with AI first delete that session by name
// (cancel): the reducer would drop its name if they started another author, and no author is left behind.
export function draftActionKind(state, action) {
  if (action === "Cancel") return "cancel";
  if (action === "Fix with AI") return state?.lostLaunch === true ? "cancel" : "fix";
  if (action === "Retry") return state?.lostLaunch === true ? "cancel" : "start";
  return action === "Draft" || action === "Draft again" ? "start" : null;
}

// The description the author works from must not be empty.
export function draftStartCheck(description) {
  return typeof description === "string" && description.trim() !== "" ? { ok: true, text: null } : { ok: false, text: "Describe what you want first." };
}

// draftAllowed (lib/draft.js) once the provider list is known: before it is read, the box asks to wait rather
// than saying the provider is not installed.
export function draftGate(args = {}) {
  if (!Array.isArray(args.providers)) {
    const early = draftAllowed({ ...args, providers: [{ name: args.providerName, installed: true }] });
    return early.ok ? { ok: false, code: "providers_unknown", reason: "The provider list has not loaded yet. Try again in a moment." } : early;
  }
  return draftAllowed(args);
}

// A flow form filled from a parsed draft. The form calls the profile `profile` and the definition calls it
// `agent_profile`; a draft that carries a prompt makes the flow an agent-prompt flow. Marks are keyed by the form's names.
export function flowFormFromDraft(form, parsed) {
  const r = applyDraft({ ...form, agent_profile: form.profile }, parsed);
  const { agent_profile: profile, ...rest } = r.form;
  const marks = { ...r.marks };
  if (Object.hasOwn(marks, "agent_profile")) { marks.profile = marks.agent_profile; delete marks.agent_profile; }
  const prompted = Object.hasOwn(marks, "prompt");
  return { form: { ...rest, profile, ...(prompted ? { kind: "prompt", workflow: "" } : {}) }, marks };
}
// A workflow editor's form (name, tier, source) filled from a parsed draft.
export const workflowFormFromDraft = (form, parsed) => applyDraft(form, parsed);

// The heading flag of a field with its "AI" mark: the CLI flag it already has, then the mark.
export function markedFlag(flag, marks, key) {
  const mark = marks && Object.hasOwn(marks, key) ? marks[key] : null;
  if (!mark) return flag ?? null;
  return flag ? `${flag} · ${mark}` : mark;
}

// The validation errors a filled form shows with "Fix with AI", as text lines for the next author's message.
// A flow: the server check's errors by field (`checks.byField`, flowChecksFromValidate). A workflow: the blocking
// findings of the validate answer, or the size refusal, or why the check could not run.
export function draftValidationErrors(kind, { checks, result, check, note } = {}) {
  if (kind === "flow") {
    return Object.entries(checks?.byField ?? {}).flatMap(([field, list]) => (Array.isArray(list) ? list : []).map((m) => (field === "form" ? String(m) : `${field}: ${m}`)));
  }
  if (check?.ok === false) return [String(check.message ?? "the source is too large")];
  if (result) return validateReport(result).blocking.map(findingWords);
  return note ? [String(note)] : [];
}

// The definition as it stands in the form now, in the draft's own field names, for "Fix with AI" to quote.
export function draftFieldsOf(kind, form) {
  if (kind === "flow") return { name: form?.name ?? "", schedule: form?.schedule ?? "", agent_profile: form?.profile ?? "", provider: form?.provider ?? "", prompt_template: form?.prompt ?? "" };
  return { name: form?.name ?? "", tier: form?.tier ?? "", source: form?.source ?? "" };
}

// Whether a form with a Draft box has something that leaving would lose: a typed description, or an author
// session (or a launch that may have made one) still to clean up.
export const draftFormDirty = (text, state) => (typeof text === "string" && text.trim() !== "") || draftHolds(state);

// What pressing a box button that starts an author checks, in this order, before anything is read or sent: the
// write gate (auth, machine not connected, CAO unreachable), then that draftProfile is set (so the box says how to
// set it even with an empty description), then that there is a description. {ok: true}, or {ok: false, code, reason}.
export function draftPressCheck({ gate, profile, description } = {}) {
  if (gate?.disabled === true) return { ok: false, code: "gate", reason: String(gate.text ?? "") };
  const early = draftAllowed({ profile });
  if (!early.ok && early.code === "no_profile") return early;      // the other refusals need the provider, which is read next
  const d = draftStartCheck(description);
  return d.ok ? { ok: true } : { ok: false, code: "no_description", reason: d.text };
}

// The one MCP server the author must declare: CAO's own (a name that only looks like it is another server).
const OWN_MCP_SERVER = "cao-mcp-server";

// What the same press checks once the author profile has been read (`detail` is the panel's profile read: the
// profile, or {error}): the write gate again, since the read took time; a profile that could not be read (an {error},
// or no profile object at all), which says so instead of falling back to the default provider; a profile whose
// `mcpServers` (the names, from profileFromOutcome) is anything but exactly [cao-mcp-server], or that is a native agent; an object that names no
// provider is a readable profile and resolves to the default as before; then draftGate on the provider the profile resolves to.
// {ok: true, providerName}, or {ok: false, code, reason}.
export function draftBeginCheck({ gate, profile, detail, draftProviders, providers, workdir } = {}) {
  if (gate?.disabled === true) return { ok: false, code: "gate", reason: String(gate.text ?? "") };
  const plain = detail !== null && typeof detail === "object" && !Array.isArray(detail);
  if (!plain || detail.error) {
    const why = plain ? ` (${String(detail.error)})` : "";
    return { ok: false, code: "profile_unreadable", reason: `The author profile ${String(profile)} could not be read${why}, so its provider is not known.` };
  }
  // claude_code adds --mcp-config and --strict-mcp-config only when the profile declares an MCP server, so a profile
  // that declares none starts with the operator's own MCP discovery, and every server a profile declares stays callable
  // whatever allowed_tools says. The author must declare exactly CAO's own server, once. No name or entry is repeated.
  const servers = detail.mcpServers;
  if (!Array.isArray(servers) || servers.length === 0) {
    return { ok: false, code: "no_mcp_server", reason: `The author profile ${String(profile)} must declare exactly one MCP server, ${OWN_MCP_SERVER}: with none declared the agent uses the MCP servers of whoever runs it, so the author's tools cannot be limited to CAO's own. Add ${OWN_MCP_SERVER} to the profile's mcpServers.` };
  }
  if (servers.length !== 1 || servers[0] !== OWN_MCP_SERVER) {
    return { ok: false, code: "other_mcp_server", reason: `The author profile ${String(profile)} declares an MCP server other than ${OWN_MCP_SERVER}, so the author's tools cannot be limited to CAO's own. Use a profile whose only MCP server is ${OWN_MCP_SERVER}.` };
  }
  // A native agent keeps its MCP configuration in the agent's own file, which the profile read does not show.
  if (detail.nativeAgent !== undefined && detail.nativeAgent !== false) {
    return { ok: false, code: "native_agent", reason: `The author profile ${String(profile)} is a native agent: its MCP configuration lives in the agent's own file, which this window cannot see, so the author's tools cannot be limited to CAO's own. Use a profile that is not a native agent.` };
  }
  const providerName = resolvedProvider({}, detail ?? null);
  const allowed = draftGate({ profile, providerName, draftProviders, providers, workdir });
  return allowed.ok ? { ok: true, providerName } : allowed;
}

// The recorded author terminal ids (shared "caowin.drafts") after a look at the terminals Herdr lists, pruned in the
// spirit of pins (lib/logic.js prunePins): nothing is pruned on a snapshot that lists no terminal id (empty, missing,
// or only entries without a text id: a dropped link lists nothing), entries that are not objects are skipped, and an id
// that such a listing no longer holds goes. An id in `heldIds` (the terminal of an author the window is still reading,
// deleting or waiting for) stays: its terminal may not be listed yet. Linear in the ids. {drafts, changed}.
export function pruneDrafts(drafts, terms, heldIds) {
  const own = Array.isArray(drafts) ? drafts : [];
  const live = new Set();
  for (const t of Array.isArray(terms) ? terms : []) {
    if (t !== null && typeof t === "object" && typeof t.id === "string" && t.id !== "") live.add(t.id);
  }
  if (live.size === 0) return { drafts: own, changed: false };
  const held = new Set(Array.isArray(heldIds) ? heldIds : []);
  const next = own.filter((id) => live.has(id) || held.has(id));
  return { drafts: next, changed: next.length !== own.length };
}

// ---------- what the panel does on a tick and when a draft fills a form ----------

// The events one author session is fed on a tick, in order: while it is drafting, the author terminal's state from
// Herdr's push data (when that terminal is listed), then the tick that carries the 180 s timeout.
export function draftTickEvents(state, terms) {
  const ev = state?.phase === "drafting" ? draftTerminalEvent(terms, state.terminalId) : null;
  return ev ? [ev, { type: "tick" }] : [{ type: "tick" }];
}
// Whether the panel stops ticking a job: nothing is left to clean up (draftHolds is false) and no request of it is out.
export const draftJobDone = (state, pending) => !draftHolds(state) && pending === 0;

// What a parsed draft changes in the form that asked, for kind "flow" ({form, touched}: the flow form and its touched
// fields) or "workflow" ({form}: name, tier, source). Returns
//   form, marks   the filled form and the "AI" marks, by the form's field names
//   texts         what to put in the retained text fields (set without their change handlers running)
//   touched       (flow) the fields now shown with their errors; serverErrors, checks, checkedKey: the flow's earlier
//                 answers, cleared; resets: the same for a workflow ({validatedKey, result, note} set to null)
//   loadProfile   (flow) the profile to read for its provider default
//   then          the form's own validation to run next, and only once it has answered the validated event is fed
//                 (draftValidatedEvent), so Fix with AI is never offered for errors the check has not seen.
export function draftFillPlan(kind, current, fields) {
  if (kind === "flow") {
    const r = flowFormFromDraft(current.form, { kind: "flow", fields });
    return {
      kind, form: r.form, marks: r.marks,
      texts: { name: r.form.name, schedule: r.form.schedule, profile: r.form.profile, prompt: r.form.prompt },
      touched: { ...(current.touched ?? {}), ...Object.fromEntries(Object.keys(r.marks).map((k) => [k, true])) },
      serverErrors: {}, checks: null, checkedKey: null, loadProfile: r.form.profile || null, then: "flow-check",
    };
  }
  const r = workflowFormFromDraft(current.form, { kind: "workflow", fields });
  return { kind, form: r.form, marks: r.marks, texts: { name: r.form.name, source: r.form.source }, resets: { validatedKey: null, result: null, note: null }, then: "workflow-check" };
}
// The event that tells the reducer how the form's own validation went, for the attempt the draft belongs to.
export const draftValidatedEvent = (attempt, errors) => ({ type: "validated", errors, attempt });
