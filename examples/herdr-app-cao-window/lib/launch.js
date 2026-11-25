// The New session form's rules (cao-window-session-launcher). Pure: no Herdr imports.
//
// Two tables are copied from the server. Each has a test that reads the Python file at test time and
// fails on any difference, and a header here that names the file:
//   the env rules        <- src/cli_agent_orchestrator/utils/forwarded_env.py
//   the enforcement text <- src/cli_agent_orchestrator/utils/enforcement.py
// The YOLO notes for the install-time providers are the ones cli/commands/launch.py prints.
//
// Top-level names are unique across lib/*.js, because bundle mode (D8) puts every shared module in one
// scope: this file owns shq (the shell quoting that flows.js and workflows.js reuse) and buildRequest.
import { utf8Length } from "./cao.js";
import { HONESTY } from "./honesty.js";

// ---------- env rules: copied from src/cli_agent_orchestrator/utils/forwarded_env.py ----------
export const ENV_RULES = Object.freeze({
  blockedPrefixes: Object.freeze(["CLAUDE", "CODEX_", "__MISE_"]),
  prefixAllowlist: Object.freeze(["CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "CLAUDE_CODE_SKIP_VERTEX_AUTH", "CLAUDE_CODE_SKIP_FOUNDRY_AUTH"]),
  hijackKeys: Object.freeze(["PATH", "HOME", "SHELL", "BASH_ENV", "ENV", "ZDOTDIR", "PROMPT_COMMAND", "PS0", "PS1", "PS2", "PS4",
    "PYTHONSTARTUP", "PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PERL5OPT", "PERL5LIB", "PERLLIB", "NODE_PATH",
    "NODE_OPTIONS", "RUBYOPT", "RUBYLIB", "GCONV_PATH", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"]),
  hijackPrefixes: Object.freeze(["LD_", "DYLD_"]),
  maxEntries: 256,
  maxKeyBytes: 128,
  maxValueBytes: 2048,
  maxTotalBytes: 128 * 1024,
  entryOverhead: 3,
});

const wellFormed = (s) => (typeof s.isWellFormed === "function" ? s.isWellFormed() : !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(s));

// Rows [{key, value}] -> {ok, errors: [{row, field: "key" | "value" | "form", message}]}. Applies the
// server's rules in the server's order; a row with an empty key and an empty value is untouched and
// ignored. A message names the rule and never the key or the value: a secret typed into either must not
// reach the screen or a log.
export function envCheck(rows) {
  const errors = [];
  const filled = (Array.isArray(rows) ? rows : [])
    .map((r, row) => ({ key: String(r?.key ?? ""), value: String(r?.value ?? ""), row }))
    .filter((r) => r.key !== "" || r.value !== "");
  if (filled.length > ENV_RULES.maxEntries) errors.push({ row: null, field: "form", message: `at most ${ENV_RULES.maxEntries} env rows` });
  let total = 0;
  let overBudget = false;
  for (const { key, value, row } of filled) {
    const bad = (field, message) => errors.push({ row, field, message });
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) { bad("key", "key must be letters, digits and _, and not start with a digit"); continue; }
    if (key.length > ENV_RULES.maxKeyBytes) { bad("key", `key must be at most ${ENV_RULES.maxKeyBytes} bytes`); continue; }
    if (!ENV_RULES.prefixAllowlist.includes(key) && ENV_RULES.blockedPrefixes.some((p) => key.startsWith(p))) {
      bad("key", `key uses a prefix reserved for provider env (${ENV_RULES.blockedPrefixes.join(", ")})`);
      continue;
    }
    if (ENV_RULES.hijackKeys.includes(key) || ENV_RULES.hijackPrefixes.some((p) => key.startsWith(p))) {
      bad("key", "key is read by the loader, shell or an interpreter when the pane starts and cannot be forwarded");
      continue;
    }
    if (value.includes("\u0000")) { bad("value", "value contains a NUL character"); continue; }
    if (!wellFormed(value)) { bad("value", "value is not valid UTF-8"); continue; }
    const bytes = utf8Length(value);
    if (bytes >= ENV_RULES.maxValueBytes) { bad("value", `value must be under ${ENV_RULES.maxValueBytes} bytes`); continue; }
    total += key.length + bytes + ENV_RULES.entryOverhead;
    if (total > ENV_RULES.maxTotalBytes && !overBudget) {
      overBudget = true;
      bad("value", "all rows together must be at most 128 KiB");
    }
  }
  return { ok: errors.length === 0, errors };
}

// ---------- enforcement: copied from src/cli_agent_orchestrator/utils/enforcement.py ----------
export const PROVIDER_ENFORCEMENT = Object.freeze({
  claude_code: "native", kiro_cli: "native", copilot_cli: "native", opencode_cli: "native", grok_cli: "native",
  kimi_cli: "prompt", qwen_cli: "prompt", codex: "prompt", antigravity_cli: "prompt", omp: "prompt", mcode: "prompt",
  hermes: "none", cursor_cli: "none", mock_cli: "none",
});
export const INSTALL_TIME_PROVIDERS = Object.freeze(["opencode_cli", "kiro_cli"]);
// The five strings of describe_enforcement, in the order of its return statements.
export const ENFORCEMENT_TEXT = Object.freeze({
  installTime: "native at install time (the installed agent's policy applies; launch overrides do not change it)",
  native: "native (the provider refuses blocked tools)",
  unrestricted: "{level} (not applicable: the policy is unrestricted)",
  prompt: "prompt-only (the agent is told, not prevented; treat as unrestricted)",
  none: "none (the provider applies no restriction; the agent runs unrestricted)",
});
const isRestricted = (tools) => tools != null && !tools.includes("*");   // is_restricted()

// What the Blocked list is worth on this provider, worded as the CLI's `Enforcement:` line. An unknown
// provider is `none`, and so is a name that only exists on Object.prototype.
export function enforcementFor(provider, allowedTools) {
  const level = Object.hasOwn(PROVIDER_ENFORCEMENT, provider) ? PROVIDER_ENFORCEMENT[provider] : "none";
  const installTime = level === "native" && INSTALL_TIME_PROVIDERS.includes(provider);
  const restricted = isRestricted(allowedTools);
  let text;
  if (installTime) text = ENFORCEMENT_TEXT.installTime;
  else if (level === "native") text = ENFORCEMENT_TEXT.native;
  else if (!restricted) text = ENFORCEMENT_TEXT.unrestricted.replace("{level}", level);
  else text = level === "prompt" ? ENFORCEMENT_TEXT.prompt : ENFORCEMENT_TEXT.none;
  return { level, installTime, restricted, text };
}

// The Ask-first dialog. A profile that declares no tools is launched with a role default that is not
// "*", so it counts as restricted here (the CLI never passes None to describe_enforcement).
export function askFirstDialog(profile, provider, override) {
  const declared = profile?.allowedTools ?? null;
  const tools = override ?? declared;
  const e = enforcementFor(provider, tools ?? ["(role default)"]);
  return {
    title: `Launch ${profile?.name ?? "the profile"} on ${provider}?`,
    role: profile?.role ?? null,
    tools: tools ? [...tools] : null,
    toolsLabel: override ? null : HONESTY.declaredTools,
    overrideNote: override ? "--allowed-tools replaces the profile's policy" : null,
    enforcement: e.text,
    enforced: e.level === "native" && !e.installTime,
    notEnforcedLabel: e.level !== "native" && e.restricted ? HONESTY.notEnforced : null,
  };
}

// The CLI's own note for --yolo on an install-time provider, whole (cli/commands/launch.py prints it as
// one yellow block; a test reads it from that file and compares).
const YOLO_NOTES = Object.freeze({
  kiro_cli: "--yolo does not widen kiro_cli's tool set. Availability is the installed agent's `tools` list, set at cao install time. "
    + "To get unrestricted access, set 'allowedTools: [\"*\"]' in the profile and re-run 'cao install'.",
  opencode_cli: "--yolo has no runtime effect on opencode_cli. Permissions are set at cao install time. To get unrestricted access, "
    + "set 'allowedTools: [\"*\"]' in the profile and re-run 'cao install'. See docs/opencode-cli.md for details.",
});
// On an install-time provider the title does not promise every tool: the installed agent's policy is
// what applies, and the dialog says so with the install-time line.
export function yoloDialog(profile, provider) {
  const installTime = INSTALL_TIME_PROVIDERS.includes(provider) ? { line: ENFORCEMENT_TEXT.installTime, note: YOLO_NOTES[provider] } : null;
  const name = profile?.name ?? "the profile";
  return {
    title: installTime ? `Launch ${name} on ${provider} with --yolo?` : `Launch ${name} with every tool allowed?`,
    danger: true,
    confirmPrompt: "Type the session name to confirm",
    installTime,
  };
}
export const yoloConfirmEnabled = (typed, sessionName) => typeof sessionName === "string" && sessionName !== "" && typed === sessionName;
export const policyDefault = () => "ask";

// ---------- profiles, provider, fields ----------
export const profilesForForm = (rows) =>
  (Array.isArray(rows) ? rows : []).filter((r) => r && r.loadable === true && typeof r.name === "string" && r.name !== "" && !r.name.startsWith("."));
export const resolvedProvider = (form, profile) => form?.provider || profile?.provider || "kiro_cli";
// Fields that spike 1.3 found `POST /sessions` refuses (named as the form names them). None.
export const CLI_ONLY_FIELDS = Object.freeze([]);

// ---------- the form ----------
export const workdirPrefill = (last) => (typeof last === "string" ? last : "");
export const emptyForm = (lastWorkdir) => ({
  profile: "", provider: "", sessionName: "", workdir: workdirPrefill(lastWorkdir), message: "", async: false,
  policy: policyDefault(), allowedTools: [], memory: false, env: [], idempotencyKey: null,
});
export function nameCheck(name) {
  if (name === "") return { ok: true, message: null, preview: null };
  const ok = typeof name === "string" && /^[A-Za-z0-9_-]+$/.test(name);
  return { ok, message: ok ? null : "only letters, digits, - and _ are allowed", preview: ok ? serverSessionName(name) : null };
}
// The name the server stores: POST /sessions prepends "cao-" unless the name already starts with it.
export const serverSessionName = (name) => (name.startsWith("cao-") ? name : `cao-${name}`);
// The directory is a path on the CAO machine, so the window asks for an absolute one. The server would not
// refuse a relative path: it resolves "x/y" against its own working directory and "~/x" against its own
// home (resolve_and_validate_path). The window cannot know either, and it never falls back to a directory
// it did not ask for, so "." or "~/x" would start the session somewhere the user did not see.
export function workdirCheck(value) {
  if (typeof value !== "string" || value.trim() === "") return { ok: false, message: "a directory on the CAO machine is needed" };
  const ok = value.startsWith("/");
  return { ok, message: ok ? null : "the directory must be an absolute path on the CAO machine, starting with /" };
}
// Whether the form is a YOLO launch: the YOLO policy, or an --allowed-tools override with `*` as an item, which lifts
// the profile's whole policy whatever policy was chosen. The items are split the way parseToolList splits (commas and
// white space), so "fs_read, *" holds `*`; a glob inside a name (`fs_*`, `mcp__x__*`, `**`, `*fs`) is a name, not `*`.
const isYolo = (form) => form.policy === "yolo" || (Array.isArray(form.allowedTools) && form.allowedTools.some((t) => parseToolList(t).includes("*")));
const filledEnv = (form) => (form.env ?? []).filter((r) => (r?.key ?? "") !== "" || (r?.value ?? "") !== "");

// ctx {stdin, cliOnly}: the problems that stop Launch, each at its field and in form order.
export function launchBlocked(form, ctx = {}) {
  const problems = [];
  if (!form.profile) problems.push({ field: "profile", message: "choose an agent profile" });
  const n = nameCheck(form.sessionName);
  if (!n.ok) problems.push({ field: "sessionName", message: n.message });
  const w = workdirCheck(form.workdir);
  if (!w.ok) problems.push({ field: "workdir", message: w.message });
  if (isYolo(form) && form.sessionName === "") problems.push({ field: "sessionName", message: "a session name is needed to confirm YOLO" });
  const env = envCheck(form.env);
  for (const e of env.errors) problems.push({ field: "env", row: e.row, message: e.message });
  const used = { memory: !!form.memory, async: !!form.async };
  for (const f of ctx.cliOnly ?? CLI_ONLY_FIELDS) if (used[f]) problems.push({ field: f, message: "CLI only" });
  // Filled env rows with stdin off always say so, next to the rows' own errors: the way out is the command.
  const useCli = filledEnv(form).length > 0 && !ctx.stdin;
  if (useCli) problems.push({ field: "env", message: "env values are not sent from this window" });
  return { blocked: problems.length > 0, problems, useCli };
}

// One POST /sessions. ctx {resolved, stdin}. The message goes only in the body; env_vars only on stdin;
// YOLO is allowed_tools=*; an override is sent, and a plain launch sends no allowed_tools (the server
// resolves them). A request without an idempotency key is refused: a retry after a lost response would
// create a second session.
export function buildRequest(form, ctx = {}) {
  if (typeof form.idempotencyKey !== "string" || form.idempotencyKey === "") {
    throw new Error("a launch needs an idempotency key (withKey makes one per submission)");
  }
  const query = { agent_profile: form.profile };
  if (form.provider) query.provider = form.provider;
  if (form.sessionName) query.session_name = form.sessionName;
  query.working_directory = form.workdir;
  if (form.policy === "yolo") query.allowed_tools = "*";
  else if (form.allowedTools.length) query.allowed_tools = form.allowedTools.join(",");
  if (form.memory) query.memory_manager = "true";
  query.idempotency_key = form.idempotencyKey;
  let body;
  if (form.message) body = { initial_message: form.message };
  const env = filledEnv(form);
  if (ctx.stdin && env.length) body = { ...(body ?? {}), env_vars: Object.fromEntries(env.map((r) => [r.key, r.value])) };
  return { method: "POST", path: "/sessions", query, body, cli: "cao launch" };
}
// The key is made once per submission and reused on its retry. The form it is given is not changed.
export const withKey = (form, makeKey) => (form.idempotencyKey ? form : { ...form, idempotencyKey: makeKey() });
// Which submission a retry may reuse: only the one a lost response left open. Any other answer ends it,
// so the next Launch is a new submission with a new key (a replay of a failed create answers 424 again).
export const keepSubmission = (submission, outcome) => (outcome?.type === "retry" ? submission : null);

// Quotes one word for a POSIX shell, only when it needs quoting. Used for every command the window offers
// to copy, so a typed value cannot become part of the command.
// A word that starts with "=" is quoted too: zsh turns an unquoted =ls into the path of ls.
export const shq = (s) => {
  const text = String(s);
  return /^[A-Za-z0-9_./:@%+,-][A-Za-z0-9_./:@%+=,-]*$/.test(text) ? text : `'${text.replace(/'/g, "'\\''")}'`;
};

// The same launch as a cao launch line; env values are masked unless reveal is set (Copy). A message
// that starts with "-" follows `--` so the CLI cannot read it as an option.
export function equivalentCommand(form, ctx = {}) {
  const parts = ["cao", "launch", "--headless", "--agents", shq(form.profile)];
  if (form.sessionName) parts.push("--session-name", shq(form.sessionName));
  if (form.provider) parts.push("--provider", shq(form.provider));
  parts.push("--working-directory", shq(form.workdir));
  for (const t of form.allowedTools) parts.push("--allowed-tools", shq(t));
  if (form.policy === "yolo") parts.push("--yolo");
  if (form.policy === "auto") parts.push("--auto-approve");
  if (form.memory) parts.push("--memory");
  if (form.async) parts.push("--async");
  for (const r of filledEnv(form)) parts.push("--env", `${shq(r.key)}=${ctx.reveal ? shq(r.value) : "…"}`);
  if (form.message) {
    if (form.message.startsWith("-")) parts.push("--");
    parts.push(shq(form.message));
  }
  return parts.join(" ");
}

// True only when a value was typed (the policy choice alone does not count).
export function discardNeeded(form, initial) {
  const typed = (f) => JSON.stringify([f.profile, f.provider, f.sessionName, f.workdir, f.message, f.allowedTools, f.memory, f.async, filledEnv(f)]);
  return typed(form) !== typed(initial);
}

// ---------- what the server answered (POST /sessions) ----------

// The form field a 400 or 422 is about, read from the prefix the server puts on its message and never from
// a word inside it (the message echoes what the user typed, so a profile named "engine" would otherwise
// move the error). FastAPI names the request field first ("query.provider: ...", "body.env_vars: ...", as
// outcomeFrom writes loc); the server's own ValueErrors start with a fixed phrase. No prefix, no field.
const FIELD_OF_REQUEST_NAME = Object.freeze({
  agent_profile: "profile", provider: "provider", session_name: "sessionName", working_directory: "workdir",
  allowed_tools: "allowedTools", memory_manager: "memory", env_vars: "env", initial_message: "message",
});
const FIELD_OF_PHRASE = [
  [/^invalid session_name\b/i, "sessionName"],
  [/^agent profile\b/i, "profile"],
  [/^working directory\b/i, "workdir"],
  [/^initial_message\b/, "message"],
];
function fieldOfDetail(detail) {
  const named = /^(?:query|body|header|path|cookie)\.([A-Za-z_][A-Za-z0-9_]*)/.exec(detail);
  if (named) return Object.hasOwn(FIELD_OF_REQUEST_NAME, named[1]) ? FIELD_OF_REQUEST_NAME[named[1]] : "form";
  return FIELD_OF_PHRASE.find(([re]) => re.test(detail))?.[1] ?? "form";
}

// One callCao outcome of POST /sessions -> what the form does next. Types: created, retry (a lost
// response: the session may have started, and the same key makes Retry safe), field, full, failed (a
// replay of a create that failed: its cause, and the key is spent), auth, refused, error.
export function launchOutcome(outcome, form) {
  const kind = outcome?.kind;
  if (kind === "ok") {
    const t = outcome.json;
    if (t && typeof t === "object" && !Array.isArray(t) && typeof t.session_name === "string" && t.session_name !== "") {
      return { type: "created", session: t.session_name, terminalId: t.id ?? null, tools: Array.isArray(t.allowed_tools) ? t.allowed_tools : null, messageSent: Boolean(form.message) };
    }
    return { type: "error", message: "CAO answered without naming the session it created" };
  }
  if (kind === "unreachable") {
    return {
      type: "retry",
      retryKey: form.idempotencyKey,
      message: "no answer from CAO. The session may have started: Retry sends the same request with the same key, so it cannot start a second one.",
      lookupSession: form.sessionName ? serverSessionName(form.sessionName) : null,
    };
  }
  if (kind === "auth") return { type: "auth", message: outcome.message ?? "" };
  if (kind === "too_large" || kind === "env_refused" || kind === "refused") return { type: "refused", kind, message: outcome.message ?? "the request was not sent" };
  if (kind === "error" && outcome.status === 429) return { type: "full", message: "the server is at its terminal limit" };
  if (kind === "error" && outcome.status === 424) {
    return { type: "failed", message: String(outcome.detail ?? "the earlier launch failed"), terminalId: outcome.json?.terminal_id ?? null, resetKey: true };
  }
  if (kind === "error" && (outcome.status === 400 || outcome.status === 422)) {
    const message = String(outcome.detail ?? "");
    return { type: "field", field: fieldOfDetail(message), message };
  }
  return { type: "error", message: outcome?.detail ?? outcome?.message ?? "launch failed" };
}

// ---------- what the New session form in the panel decides (task 9.2 and 9.3) ----------

// What pressing Launch does: show the problems at their fields (the first one takes focus), open the
// Ask-first or YOLO dialog, or send at once (Auto-approve). ctx as launchBlocked takes it.
export function launchPlan(form, ctx = {}) {
  const blocked = launchBlocked(form, ctx);
  if (blocked.blocked) return { action: "blocked", problems: blocked.problems, first: blocked.problems[0], useCli: blocked.useCli };
  if (isYolo(form)) return { action: "dialog", dialog: "yolo" };
  if (form.policy === "auto") return { action: "send" };
  return { action: "dialog", dialog: "ask" };
}

// The text of an --allowed-tools field as a list: commas and spaces separate, empties and repeats go.
export const parseToolList = (text) => [...new Set(String(text ?? "").split(/[\s,]+/).filter((t) => t !== ""))];

// The route of one profile's details; null for a name the window will not put in a path (empty, ".", or holding "..").
export function profileRoute(name) {
  if (typeof name !== "string" || name === "" || name === "." || name.includes("..")) return null;
  try { return `/agents/profiles/${encodeURIComponent(name)}`; } catch { return null; }   // a lone surrogate cannot be encoded
}

// The names of a profile's MCP servers: the keys of the `mcpServers` object the server sends (it leaves the key out
// when the profile has none). Only the names: an entry can hold a command line, arguments, env values, a URL or
// headers, and none of that is kept. A value that is not an object (null, text, a list, a number) comes back as
// null, which the Draft gate refuses: the check fails closed.
const mcpServerNames = (value) => {
  if (value === undefined) return [];
  return value !== null && typeof value === "object" && !Array.isArray(value) ? Object.keys(value) : null;
};

// Whether the profile sets `native_agent` (the name of a Claude agent file that holds its MCP configuration): a plain
// true or false, never the name. Absent, null, "" and false are false; anything else is true (the check fails closed).
const hasNativeAgent = (value) => !(value === undefined || value === null || value === "" || value === false);

// A GET /agents/profiles/{name} answer as the dialogs read it: name, role, declared allowedTools, provider,
// mcpServers (the server names, a list; null when the answer's value is not an object) and nativeAgent (true or false).
// null for any other outcome.
export function profileFromOutcome(outcome) {
  const p = outcome?.kind === "ok" && outcome.json && typeof outcome.json === "object" && !Array.isArray(outcome.json) ? outcome.json : null;
  if (!p || typeof p.name !== "string" || p.name === "") return null;
  return {
    name: p.name,
    role: typeof p.role === "string" && p.role !== "" ? p.role : null,
    allowedTools: Array.isArray(p.allowedTools) ? p.allowedTools.filter((t) => typeof t === "string") : null,
    provider: typeof p.provider === "string" && p.provider !== "" ? p.provider : null,
    mcpServers: mcpServerNames(p.mcpServers),
    nativeAgent: hasNativeAgent(p.native_agent),
  };
}

// The provider choices: every GET /agents/providers row (uninstalled ones marked), and the default the
// server resolves when none is chosen (the profile's provider, else kiro_cli).
export function providerChoices(rows, profile) {
  const options = (Array.isArray(rows) ? rows : []).filter((r) => r && typeof r.name === "string" && r.name !== "")
    .map((r) => ({ name: r.name, installed: r.installed === true, label: r.installed === true ? r.name : `${r.name} (not installed)` }));
  const defaultName = profile?.provider || "kiro_cli";
  return { options, defaultName, defaultText: `Default: ${defaultName}${profile?.provider ? " (the profile's)" : ""}` };
}

// Profile combobox: the rows whose name contains the typed text (case-insensitive), at most `limit`.
export function profileMatches(rows, filter, limit = 8) {
  const needle = String(filter ?? "").trim().toLowerCase();
  return (Array.isArray(rows) ? rows : []).filter((r) => typeof r?.name === "string" && (needle === "" || r.name.toLowerCase().includes(needle))).slice(0, limit);
}

// Profile combobox keys. state {index}; count matches. Up and Down move (no wrap), Home and End jump,
// Enter chooses the highlighted match, Esc closes the list.
export function comboStep(state, count, key) {
  const k = String(key ?? "").toLowerCase().replace(/^arrow/, "");
  const last = Math.max(count - 1, 0);
  const index = Math.min(Math.max(state?.index ?? 0, 0), last);
  if (k === "down") return { index: Math.min(index + 1, last), action: null };
  if (k === "up") return { index: Math.max(index - 1, 0), action: null };
  if (k === "home") return { index: 0, action: null };
  if (k === "end") return { index: last, action: null };
  if (k === "enter" && count > 0) return { index, action: "choose" };
  if (k === "escape" || k === "esc") return { index, action: "close" };
  return { index, action: null };
}

// The tool list line of the Ask-first dialog: the list (an override, or the profile's declared tools); "none" for
// a declared empty list; for a profile that was read and declares none (the server leaves the key out), that the
// server applies the role's defaults; and only a profile that could not be read (null) is "not known".
function toolsWords(tools, profile) {
  if (Array.isArray(tools)) return tools.length > 0 ? tools.join(", ") : "none";
  if (profile) return "none declared by the profile; the server applies the role's defaults";
  return "not known: the profile could not be read";
}

// The dialog before a launch. "ask" is the Ask-first dialog (declared tools, the provider's enforcement
// level, Cancel first); "yolo" is the typed-name dialog (Launch is enabled only when the typed text is
// the session name, and the typed field has the keys first). `profile` may be null when it could not be read.
export function launchDialog(kind, { profile = null, provider, override = null, typed = "", sessionName = "" } = {}) {
  if (kind === "yolo") {
    const y = yoloDialog(profile, provider);
    return { kind: "yolo", ...y, body: null, confirmLabel: "Launch with YOLO", cancelLabel: "Cancel", defaultFocus: "input", confirmEnabled: yoloConfirmEnabled(typed, sessionName), typedName: sessionName };
  }
  const a = askFirstDialog(profile, provider, override);
  return {
    kind: "ask", ...a, toolsText: toolsWords(a.tools, profile),
    toolsHeading: override ? "Allowed tools (your override)" : "Declared tools",
    confirmLabel: "Launch", cancelLabel: "Cancel", defaultFocus: "cancel", confirmEnabled: true,
  };
}

// The dialog for a form with typed values that is about to be left. Keep editing is the default.
export const discardSpec = () => ({
  title: "Discard this form?", body: "What you typed is lost.", confirmLabel: "Discard", cancelLabel: "Keep editing", defaultFocus: "cancel", confirmEnabled: true,
});

// How the first reply reads from the agent's Herdr record (state and turn), without asking CAO.
function replyWords(term) {
  if (!term) return "Waiting for the agent to start.";
  if (term.state === "blocked") return "The agent is asking you something.";
  if (term.state === "error") return "The agent reported an error.";
  if (term.state === "working") return "The agent is working on it.";
  if ((term.state === "done" || term.state === "idle") && term.turn >= 1) return "The agent has replied.";
  return "Waiting for the agent to start.";
}

// The card after a 201: session name, the allowed_tools the server recorded, whether a first message
// went, and (message sent, --async unset) where the first reply stands, read from the pushed terminal
// `term` of that session. `created` is a launchOutcome of type "created".
export function launchResultCard(created, form, term) {
  const sent = created?.messageSent === true;
  const follow = sent && form?.async !== true;
  const tools = Array.isArray(created?.tools) ? created.tools : null;
  let messageLine = "No first message was sent.";
  if (sent) messageLine = follow ? "The first message was delivered." : "The first message was delivered; the window does not wait for the reply (--async).";
  return {
    title: `Started ${created?.session ?? "the session"}.`,
    session: created?.session ?? null,
    toolsText: tools ? (tools.length ? tools.join(", ") : "none") : "not reported by the server",
    messageLine,
    follow,
    replyLine: follow ? replyWords(term) : null,
  };
}

// ---------- what the panel does with a launch answer ----------

// The server fields a launch error is shown at, inline. Every other field comes as a banner.
export const INLINE_LAUNCH_FIELDS = Object.freeze(["profile", "sessionName", "workdir"]);

// A launchOutcome result -> what the form does: {effect: "created"}, {effect: "retry", text} (the answer was
// lost: Retry keeps the key), {effect: "inline", field, message} (profile, sessionName or workdir), or
// {effect: "banner", text}: a field of any other name reads "<field>: <text>", an auth failure reads `authText`.
export function launchAfter(result, authText) {
  if (result?.type === "created") return { effect: "created" };
  if (result?.type === "retry") return { effect: "retry", text: result.message };
  if (result?.type === "field" && INLINE_LAUNCH_FIELDS.includes(result.field)) return { effect: "inline", field: result.field, message: result.message };
  const text = result?.type === "auth" ? authText : result?.message;
  return { effect: "banner", text: result?.type === "field" ? `${result.field}: ${text}` : text };
}

// ---------- a launch is sent from the form that was confirmed ----------

// Whether a launch may be sent now, from the form snapshot taken when its dialog opened (or at the click,
// for Auto-approve): the plan is run again on that snapshot. `kind` is the dialog that was confirmed ("ask",
// "yolo") or "auto"; it must be the one the snapshot's policy asks for, and a YOLO needs the typed name.
export function launchConfirm(form, kind, ctx = {}, typed = "") {
  const plan = launchPlan(form, ctx);
  if (plan.action === "blocked") return { send: false, reason: plan.first?.message ?? "the form is not valid" };
  if ((plan.action === "send" ? "auto" : plan.dialog) !== kind) return { send: false, reason: "the policy changed since the dialog opened" };
  if (kind === "yolo" && !yoloConfirmEnabled(typed, form.sessionName)) return { send: false, reason: "the typed name is not the session name" };
  return { send: true, reason: null };
}

// ---------- the Ask-first dialog waits for the profile read ----------

// What to do when the profile read for the Ask-first dialog returns. scope: what Launch was pressed on; current:
// what it is now (the dialog passes the same string for both); sameForm: the form is still the snapshot of the press;
// dialogOpen: any truthy value means another dialog (the discard dialog, say) is open now and must not be replaced; error: the
// read's error text, or null. The dialog opens only when all of that still holds; otherwise a note says the
// form changed (so nothing is dropped silently), and a read error is shown as it is.
export function launchReadResult({ scope, current, sameForm, dialogOpen, error } = {}) {
  if (scope !== current || sameForm !== true || dialogOpen) return { action: "changed", text: "The form changed, press Launch again." };
  if (error != null) return { action: "error", text: error };
  return { action: "open" };
}

