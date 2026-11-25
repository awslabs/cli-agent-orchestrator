// The Workflows section's rules (cao-window-workflows): row state from the newest run, polling, the
// details of a workflow and of a run, the plan-approval-needed state, typed inputs, the size checks and
// the save request. Pure. The window never builds the approve route and never sends a file path: a
// workflow is named by a bare name and created from source text. Top-level names are unique across
// lib/*.js (bundle mode, D8).
import { ARG_BODY_MAX, bodyBytes, seg, utf8Length } from "./cao.js";
import { HONESTY } from "./honesty.js";
import { shq } from "./launch.js";

export const WORKFLOW_POLL_MS = 60_000;
export const WORKFLOW_FAST_POLL_MS = 5_000;
export const WORKFLOW_SCHEDULER_NOTE = "Workflows have no scheduler of their own: Schedule... creates a schedule that runs this one.";

const STATE_WORDS = Object.freeze({ running: "running", completed: "succeeded", failed: "failed", cancelled: "cancelled" });
const startedAt = (run) => { const t = Date.parse(run?.started_at); return Number.isFinite(t) ? t : -Infinity; };

// The row's state words, from the newest of this workflow's runs. The list is not reordered.
export function workflowRowState(runs) {
  if (!Array.isArray(runs) || runs.length === 0) return "never run";
  const newest = runs.reduce((a, b) => (startedAt(b) > startedAt(a) ? b : a));
  const state = newest?.state;
  return typeof state === "string" && Object.hasOwn(STATE_WORDS, state) ? STATE_WORDS[state] : String(state ?? "unknown");
}
export const pollInterval = (runs) => (Array.isArray(runs) && runs.some((r) => r?.state === "running") ? WORKFLOW_FAST_POLL_MS : WORKFLOW_POLL_MS);

// The details of a workflow as its GET /workflows row carries them: mode (not the tier) and step count,
// never source_path, which is the server's own file layout.
export function workflowDetails(row) {
  return {
    name: typeof row?.name === "string" ? row.name : "",
    description: typeof row?.description === "string" ? row.description : "",
    mode: typeof row?.mode === "string" ? row.mode : "",
    stepCount: Number.isInteger(row?.step_count) ? row.step_count : null,
    schedulerNote: WORKFLOW_SCHEDULER_NOTE,
  };
}
// Cancel for a running run, Resume for a cancelled one, nothing else.
export const runActions = (run) => ({ cancel: run?.state === "running", resume: run?.state === "cancelled" });

// The "plan approval needed" state, kept until the next submit: an approval refusal sets it (replacing an
// older one), a successful submit clears it, any other outcome leaves it. The command is for a terminal:
// the plan id is quoted for the shell and follows `--` when it could be read as an option, and with no
// usable plan id there is no command to offer.
export function approvalState(prior, outcome) {
  if (outcome?.kind === "approval_required") {
    const planId = typeof outcome.planId === "string" && outcome.planId !== "" ? outcome.planId : null;
    return {
      needed: true,
      planId,
      command: planId === null ? null : `cao workflow approve ${planId.startsWith("-") ? "-- " : ""}${shq(planId)}`,
      localRun: { status: "not submitted", note: "approval needed" },
      headline: "Plan approval needed. Approve it in a terminal",
      submitAgainLabel: "I approved it: submit again",
      serverMessage: typeof outcome.message === "string" ? outcome.message : "",
    };
  }
  return outcome?.kind === "ok" ? null : prior;
}

const BARE = /^[A-Za-z0-9_-]+$/;
function bare(name) {
  if (typeof name !== "string" || !BARE.test(name)) throw new Error(`a workflow is named by a bare name, not a path: ${String(name).slice(0, 30)}`);
  return name;
}

// POST /workflows/runs:submit. name_or_path is the bare workflow name; working_directory is the optional
// project root of a script run. The same arguments build the same request, so "submit again" repeats it.
export function submitRequest(name, inputs, workingDirectory) {
  const body = { name_or_path: bare(name), inputs: inputs ?? {} };
  if (typeof workingDirectory === "string" && workingDirectory !== "") body.working_directory = workingDirectory;
  return { method: "POST", path: "/workflows/runs:submit", body, cli: "cao workflow run" };
}

const INPUT_TYPES = Object.freeze(["string", "int", "bool", "path"]);
// The run form from GET /workflows/{name}: typed fields in the spec's order, or free key and value rows
// with the "inputs not described" label when the spec does not describe them in a form we know.
export function inputsForm(spec) {
  const ins = spec?.inputs;
  const described = ins && typeof ins === "object" && !Array.isArray(ins)
    && Object.values(ins).every((d) => d && typeof d === "object" && INPUT_TYPES.includes(d.type));
  if (!described) return { fields: [], free: true, label: HONESTY.inputsUndescribed };
  return {
    fields: Object.entries(ins).map(([name, d]) => ({ name, type: d.type, required: d.required === true, default: d.default ?? null })),
    free: false,
    label: null,
  };
}

// Writes a value under a name even for "__proto__", which a plain assignment would turn into a prototype.
const putOwn = (obj, key, value) => Object.defineProperty(obj, key, { value, enumerable: true, writable: true, configurable: true });

// Typed values from the form's text. Only declared fields are read, and only their own entries: an
// inherited property such as "constructor" is not an answer. An empty optional field is left out.
export function coerceInputs(fields, values) {
  const inputs = {};
  const errors = {};
  for (const f of fields) {
    const given = values != null && Object.hasOwn(values, f.name) ? values[f.name] : undefined;
    const raw = given === undefined || given === null ? "" : String(given);
    if (raw === "") {
      if (f.required) putOwn(errors, f.name, "is required");
      continue;
    }
    if (f.type === "int") {
      if (/^-?\d+$/.test(raw) && Number.isSafeInteger(Number(raw))) putOwn(inputs, f.name, Number(raw));
      else putOwn(errors, f.name, "must be a whole number");
    } else if (f.type === "bool") {
      if (raw === "true" || raw === "false") putOwn(inputs, f.name, raw === "true");
      else putOwn(errors, f.name, "must be true or false");
    } else {
      putOwn(inputs, f.name, raw);
    }
  }
  return { inputs, errors };
}

// Flows whose prompt runs this workflow through `cao workflow run <name>` (what Schedule... writes).
export function scheduledBy(flows, name) {
  if (!Array.isArray(flows) || typeof name !== "string" || name === "") return [];
  const re = new RegExp(`cao workflow run ${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?![A-Za-z0-9_-])`);
  return flows.filter((f) => re.test(typeof f?.prompt_template === "string" ? f.prompt_template : "")).map((f) => ({ name: f.name, schedule: f.schedule, enabled: f.enabled }));
}

const KIB = 1024;
const kib = (n) => (n / KIB).toFixed(1);
// A refused source's sizeText: the exact size, then the exact limit, in the unit of the cap exceeded.
const exactSize = (size, limit, unit) => `${size} of ${limit} ${unit}`;
// The size rule, before any request. Every answer is {ok, bytes, limit, unit, sizeText, message}: bytes
// is the size in the unit of limit ("characters" at the Python character cap, "bytes" for every other
// limit), and a refusal's message prints that exact size and that exact limit, never a rounded KiB figure,
// and names the cap in its usual words. sizeText is for the editor line: an accepted source shows a rounded
// "N KiB of M KiB" (or "N of 131072 characters"), a refused one the exact "size of limit unit".
//   Argument mode counts the encoded request body (POST for a new workflow, PUT with expected_hash for an
//   edit) against ARG_BODY_MAX. Stdin mode applies the server's caps: 262144 bytes of UTF-8 for a spec
//   (WORKFLOW_MAX_SPEC_BYTES), and for Python also 131072 characters (SCRIPT_LINT_MAX_SOURCE_CHARS).
export function sourceCheck(tier, text, name, stdinMode, { expectedHash } = {}) {
  if (tier !== "python" && tier !== "yaml") return { ok: false, bytes: 0, limit: 0, unit: "bytes", sizeText: "", message: "choose python or yaml" };
  const source = typeof text === "string" ? text : "";
  if (!stdinMode) {
    const body = expectedHash ? { source, expected_hash: expectedHash, tier } : { name, source, tier };
    const bytes = bodyBytes(body);
    const ok = bytes <= ARG_BODY_MAX;
    // The CLI that does what this save does: create for a new workflow, update (which requires the hash read when
    // editing began) for an edit. Both take a Python file only; a YAML spec has no CLI path.
    const shown = typeof name === "string" && /^[A-Za-z0-9_-]+$/.test(name) ? name : "<name>";
    const hint = tier !== "python"
      ? "cao workflow create and update take a Python file only"
      : expectedHash ? `cao workflow update ${shown} --from-file <path> --expected-hash ${shq(expectedHash)}` : `cao workflow create ${shown} --from-file <path>`;
    return {
      ok, bytes, limit: ARG_BODY_MAX, unit: "bytes",
      sizeText: ok ? `${kib(bytes)} KiB of ${ARG_BODY_MAX / KIB} KiB` : exactSize(bytes, ARG_BODY_MAX, "bytes"),
      message: ok ? null : `This source is ${bytes} bytes once encoded; this window sends at most ${ARG_BODY_MAX} bytes (${ARG_BODY_MAX / KIB} KiB). Paste a smaller spec or use the CLI (${hint}).`,
    };
  }
  const byteLimit = 256 * KIB;
  const utf8Bytes = utf8Length(source);
  const bytesOk = utf8Bytes <= byteLimit;
  if (tier === "yaml") {
    return {
      ok: bytesOk, bytes: utf8Bytes, limit: byteLimit, unit: "bytes",
      sizeText: bytesOk ? `${kib(utf8Bytes)} KiB of ${byteLimit / KIB} KiB` : exactSize(utf8Bytes, byteLimit, "bytes"),
      message: bytesOk ? null : `A YAML spec can be at most ${byteLimit} bytes (256 KiB); this one is ${utf8Bytes} bytes.`,
    };
  }
  // Python has both caps. A refusal reports the cap that is exceeded (the character cap first when both
  // are); an accepted source reports the cap it is closer to, by share of each cap (the character cap on a tie).
  const chars = [...source].length;
  const charLimit = 128 * KIB;
  const charsOk = chars <= charLimit;
  const byChars = !charsOk || (bytesOk && chars * byteLimit >= utf8Bytes * charLimit);
  let message = null;
  if (!charsOk) message = `A Python script can be at most ${charLimit} characters (128 Ki); this one has ${chars} characters.`;
  else if (!bytesOk) message = `A Python script can be at most ${byteLimit} bytes (256 KiB) of UTF-8; this one is ${utf8Bytes} bytes.`;
  const ok = charsOk && bytesOk;
  let sizeText;
  if (!ok) sizeText = byChars ? exactSize(chars, charLimit, "characters") : exactSize(utf8Bytes, byteLimit, "bytes");
  else sizeText = byChars ? `${chars} of ${charLimit} characters (128 Ki)` : `${kib(utf8Bytes)} KiB of ${byteLimit / KIB} KiB`;
  return {
    ok, bytes: byChars ? chars : utf8Bytes, limit: byChars ? charLimit : byteLimit, unit: byChars ? "characters" : "bytes",
    sizeText,
    message,
  };
}

// Save needs a clean validate of the text as it is now, and the answer must say it passed: an answer
// that is empty, unknown or not an object enables nothing. Warnings (reserved notes, warning findings)
// do not block.
export function saveEnabled(validatedText, currentText, result) {
  if (typeof validatedText !== "string" || validatedText !== currentText) return false;
  if (!result || typeof result !== "object" || Array.isArray(result)) return false;
  if (result.status !== "pass" && result.status !== "pass_reserved") return false;
  if (result.errors != null && (!Array.isArray(result.errors) || result.errors.length > 0)) return false;
  return !(Array.isArray(result.findings) ? result.findings : []).some((f) => f?.severity === "error");
}

// POST /workflows for a new workflow, PUT /workflows/{name} with the expected_hash read when editing
// began for an existing one. The body is the source text, the tier and the name or hash: no path.
export function saveRequest({ mode, name, tier, source, expectedHash } = {}) {
  if (mode !== "new" && mode !== "edit") throw new Error("a save is new or edit");
  bare(name);
  if (tier !== "python" && tier !== "yaml") throw new Error("the tier is python or yaml");
  if (typeof source !== "string") throw new Error("the source is text");
  // `cli` is the command that does the same from a Python file; a YAML spec has none (the refusal then says "the cao CLI").
  if (mode === "new") return { method: "POST", path: "/workflows", body: { name, source, tier }, ...(tier === "python" ? { cli: `cao workflow create ${name} --from-file <path>` } : {}) };
  if (typeof expectedHash !== "string" || expectedHash === "") throw new Error("an edit needs the expected_hash read when editing began");
  return { method: "PUT", path: `/workflows/${seg(name)}`, body: { source, expected_hash: expectedHash, tier }, ...(tier === "python" ? { cli: `cao workflow update ${name} --from-file <path> --expected-hash ${shq(expectedHash)}` } : {}) };
}

const STALE_DETAIL = /changed on disk since it was read/;
// What a 409 on save means. On PUT only the server's stale-hash refusal is "changed elsewhere", with the
// text kept and a reload offered; any other 409 (such as the workflow being in the other tier) is shown
// with the server's own reason and no reload. On POST a 409 says the name exists. Anything else is null.
export function staleHashOutcome(outcome, method) {
  if (outcome?.kind !== "error" || outcome.status !== 409) return null;
  const detail = String(outcome.detail ?? "");
  if (method === "PUT") {
    if (STALE_DETAIL.test(detail)) {
      return { type: "stale", message: "This workflow changed elsewhere. Your text is kept; reload to see the new version.", keepText: true, offerReload: true, detail };
    }
    return { type: "conflict", message: detail.trim() !== "" ? detail : "The server refused the update.", keepText: true, offerReload: false, detail };
  }
  if (method === "POST") return { type: "exists", message: "A workflow with that name exists.", detail };
  return null;
}
