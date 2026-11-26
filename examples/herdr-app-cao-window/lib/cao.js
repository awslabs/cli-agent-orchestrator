// The CAO data path. Every call the window makes to cao-server is built here and nowhere else:
// exec("curl", [...], {on: <CAO machine>, timeout}) against the loopback address of that machine
// (127.0.0.1:<port>). No other host, no port forward. Pure: curlRequest returns
// the exec call to make, outcomeFrom reads the result, and callCao glues them to an injected exec.
// Top-level names are unique across lib/*.js (launch.js owns buildRequest and shq, which flows.js and
// workflows.js import), because bundle mode puts every shared module and the mod's own code in one scope.
import { splitStatus } from "./logic.js";

// Linux limits one argument to PAGE_SIZE * 32 bytes including its NUL (131072 with 4 KiB pages); a
// longer one makes execve of curl fail with E2BIG. 120 KiB of encoded JSON leaves a margin.
export const ARG_BODY_MAX = 122880;
// Herdr's exec feeds no stdin (its options are cwd, on and timeout), so bodies travel as a
// --data-binary argument, the size cap above applies and env_vars is never sent. If a later Herdr
// feeds stdin, set STDIN_SUPPORTED and check STDIN_OPTION names its option.
export const STDIN_SUPPORTED = false;
export const STDIN_OPTION = "stdin";
export const AUTH_MESSAGE = "CAO requires auth - not supported by this window yet";

// UTF-8 byte length of a string, counted by hand: the mod runtime is not promised to have TextEncoder.
// A surrogate pair is 4 bytes; a lone surrogate counts 3, as the replacement character it encodes to.
export function utf8Length(text) {
  let n = 0;
  for (let i = 0; i < text.length; i += 1) {
    const c = text.charCodeAt(i);
    if (c < 0x80) n += 1;
    else if (c < 0x800) n += 2;
    else if (c >= 0xd800 && c <= 0xdbff && i + 1 < text.length && (text.charCodeAt(i + 1) & 0xfc00) === 0xdc00) { n += 4; i += 1; }
    else n += 3;
  }
  return n;
}
export const bodyBytes = (body) => utf8Length(JSON.stringify(body) ?? "");

const DETAIL_MAX = 500;
const clipText = (text) => String(text ?? "").slice(0, DETAIL_MAX);

// One path segment (a flow, session, workflow or run name); anything else throws.
export function seg(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value)) {
    throw new Error(`unsafe path segment: ${JSON.stringify(value)?.slice(0, 40)}`);
  }
  return value;
}

// A TCP port from the layout props: an integer from 1 to 65535, or its decimal digits. null otherwise.
function portOf(port) {
  if (typeof port === "number") return Number.isInteger(port) && port >= 1 && port <= 65535 ? port : null;
  if (typeof port === "string" && /^[0-9]{1,5}$/.test(port)) return portOf(Number(port));
  return null;
}

const refused = (kind, message, extra = {}) => ({ ok: false, outcome: { kind, message, ...extra } });

// cfg: {machine, port, stdin?}. spec: {method?, path, query?, body?, maxTime?, cli?}. Returns
// {ok: true, exec: {cmd, args, opts}} or {ok: false, outcome} for a request that must not be sent.
export function curlRequest(cfg, spec) {
  const stdin = cfg?.stdin ?? STDIN_SUPPORTED;
  const { method = "GET", path, query, body, maxTime = 10, cli } = spec ?? {};
  const machine = cfg?.machine;
  if (typeof machine !== "string" || machine.trim() === "") return refused("refused", "no CAO machine is configured");
  const port = portOf(cfg?.port);
  if (port === null) return refused("refused", "the CAO port is not a number from 1 to 65535");
  if (typeof path !== "string" || !path.startsWith("/") || /\s/.test(path)) return refused("refused", `bad request path: ${String(path).slice(0, 40)}`);
  if (typeof method !== "string" || !/^[A-Z]{3,7}$/.test(method)) return refused("refused", "bad request method");
  if (typeof maxTime !== "number" || !Number.isFinite(maxTime) || maxTime <= 0) return refused("refused", "bad request time limit");
  if (query !== undefined && query !== null && typeof query !== "object") return refused("refused", "bad request query");
  if (body !== null && typeof body === "object" && Object.hasOwn(body, "env_vars") && !stdin) {
    return refused("env_refused", "env values are not sent from this window; use the cao CLI on the CAO machine");
  }
  let qs = "";
  try {
    // encodeURIComponent, not URLSearchParams (not promised either); it also encodes [ ] { } and spaces.
    const pairs = Object.entries(query ?? {}).filter(([, v]) => v !== undefined && v !== null && v !== "")
      .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
    if (pairs.length) qs = `?${pairs.join("&")}`;
  } catch {
    return refused("refused", "bad request query");
  }
  // -q first (it only takes effect as the first argument): curl must not read ~/.curlrc on the CAO machine, where a line such
  // as `include` or `-L` would change the text the window parses. --globoff: curl must not read [1-2] or {a,b} in a path as a
  // range or a list. --noproxy "*": the URL is always loopback, so no http_proxy of the exec environment may carry a request
  // (and its first message) to a proxy.
  const args = ["-q", "-sS", "-w", "\n%{http_code}", "--max-time", String(maxTime), "--globoff", "--noproxy", "*"];
  const opts = { on: machine, timeout: Math.ceil(maxTime + 2) * 1000 };
  if (method !== "GET") args.push("-X", method);
  if (body !== undefined) {
    // A body that does not encode to a JSON string (a function, a symbol, a bigint, a cycle, a toJSON that
    // returns nothing) must not become a missing argument: exec drops it and curl would take the URL as data.
    let text;
    try { text = JSON.stringify(body); } catch { text = undefined; }
    if (typeof text !== "string") return refused("refused", "the request body cannot be encoded as JSON");
    args.push("-H", "Content-Type: application/json");
    if (stdin) {
      args.push("--data-binary", "@-");
      opts[STDIN_OPTION] = text;
    } else {
      const bytes = utf8Length(text);
      if (bytes > ARG_BODY_MAX) {
        return refused("too_large", `too large to send from this window (${bytes} bytes encoded, limit ${ARG_BODY_MAX}); use \`${cli ?? "the cao CLI"}\` instead`, { bytes, max: ARG_BODY_MAX, cli: cli ?? null });
      }
      args.push("--data-binary", text);
    }
  }
  args.push(`http://127.0.0.1:${port}${path}${qs}`);
  return { ok: true, exec: { cmd: "curl", args, opts } };
}

function detailText(json, body) {
  const d = json?.detail;
  if (typeof d === "string") return clipText(d);
  if (Array.isArray(d)) return clipText(d.map((e) => `${Array.isArray(e?.loc) ? e.loc.join(".") : "?"}: ${e?.msg ?? ""}`).join("; "));
  if (d && typeof d === "object") return clipText(JSON.stringify(d));
  return clipText(body);
}

// exec result {stdout, stderr, code} of a curl run with -w "\n%{http_code}" -> one outcome. Anything that
// is not a curl result with a status line (no stdout, a number, a missing code) reads as unreachable.
export function outcomeFrom(result) {
  const stdout = typeof result?.stdout === "string" ? result.stdout : "";
  const stderr = typeof result?.stderr === "string" ? result.stderr : "";
  const { body, code } = splitStatus(stdout);
  if (code === 0) return { kind: "unreachable", stderr: clipText(stderr) };
  let json;
  try { json = body ? JSON.parse(body) : undefined; } catch { json = undefined; }
  if (code >= 200 && code < 300) return { kind: "ok", status: code, body, json };
  if (code === 403 && json?.detail?.kind === "approval_required") {
    const { plan_id: planId, message } = json.detail;
    return { kind: "approval_required", status: code, planId: typeof planId === "string" ? planId : null, message: typeof message === "string" ? message : "" };
  }
  if (code === 401 || code === 403) return { kind: "auth", status: code, message: AUTH_MESSAGE };
  return { kind: "error", status: code, detail: detailText(json, body), json };
}

// Builds the request, refuses it before exec when it must not be sent, and turns a failing exec into
// an unreachable outcome. execFn is the mod's exec; spec is not changed.
export async function callCao(execFn, cfg, spec) {
  const built = curlRequest(cfg, spec);
  if (!built.ok) return built.outcome;
  try {
    return outcomeFrom(await execFn(built.exec.cmd, built.exec.args, built.exec.opts));
  } catch (err) {
    return { kind: "unreachable", stderr: clipText(err?.message ?? err) };
  }
}
