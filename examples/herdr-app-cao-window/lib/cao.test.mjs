// lib/cao.js: the one place that builds a curl argument list (cao-window-sessions, "CAO data goes
// through exec on the CAO machine": Request shape, Body too large for argument mode, Body at the limit,
// Approval refusal is not auth, Unreachable server; "Failure states keep the last good view": Auth required).
import { spawnSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { assert, finish, mkTmp, test } from "../test/harness.mjs";
import {
  ARG_BODY_MAX, AUTH_MESSAGE, STDIN_OPTION, STDIN_SUPPORTED, bodyBytes, callCao, curlRequest, outcomeFrom, seg,
} from "./cao.js";
import * as draft from "./draft.js";
import * as flows from "./flows.js";
import * as launch from "./launch.js";
import * as workflows from "./workflows.js";

const CFG = { machine: "cao", port: 9889, stdin: false };
const STDIN_CFG = { ...CFG, stdin: true };
const enc = new TextEncoder();

// A POST /workflows-shaped body whose JSON encoding is exactly `total` bytes. `unit` pads with a
// multi-byte character where it fits, and with ASCII for the remainder.
const bodyOfBytes = (total, unit = "x") => {
  const overhead = bodyBytes({ source: "" });
  const room = total - overhead;
  const per = enc.encode(unit).length;
  const body = { source: unit.repeat(Math.floor(room / per)) + "x".repeat(room % per) };
  assert.equal(bodyBytes(body), total);
  return body;
};
const fakeExec = (reply) => {
  const calls = [];
  const fn = async (cmd, args, opts) => { calls.push({ cmd, args, opts }); return reply; };
  fn.calls = calls;
  return fn;
};
const curl = (code, body = "{}", stderr = "") => outcomeFrom({ stdout: `${body}\n${code}`, stderr });

test("request shape: curl through exec on the CAO machine against 127.0.0.1", () => {
  const { ok, exec } = curlRequest(CFG, { path: "/flows" });
  assert.equal(ok, true);
  assert.equal(exec.cmd, "curl");
  assert.equal(exec.opts.on, "cao");
  assert.ok(exec.opts.timeout > 10_000);
  assert.deepEqual(exec.args.slice(0, 5), ["-sS", "-w", "\n%{http_code}", "--max-time", "10"]);
  assert.equal(exec.args.at(-1), "http://127.0.0.1:9889/flows");
  assert.ok(!exec.args.includes("-X"));
  assert.ok(!exec.args.includes("--data-binary"));
});

test("props machine and port are used as given, and the exec timeout outlasts --max-time", () => {
  const { exec } = curlRequest({ machine: "box", port: 9100 }, { path: "/health", maxTime: 3 });
  assert.equal(exec.opts.on, "box");
  assert.equal(exec.args.at(-1), "http://127.0.0.1:9100/health");
  assert.equal(exec.args[exec.args.indexOf("--max-time") + 1], "3");
  const slow = curlRequest(CFG, { method: "POST", path: "/flows/x/run", maxTime: 180 });
  assert.equal(slow.exec.args[slow.exec.args.indexOf("--max-time") + 1], "180");
  assert.ok(slow.exec.opts.timeout > 180_000, "exec must not give up before curl does");
});

test("a numeric-string port from the layout props is accepted", () => {
  assert.equal(curlRequest({ machine: "cao", port: "9889" }, { path: "/health" }).exec.args.at(-1), "http://127.0.0.1:9889/health");
});

test("the only host ever called is 127.0.0.1, whatever the props hold", () => {
  for (const port of ["9889@evil.test", "9889/x", "9889#", "", undefined, null, 0, 65536, -1, 1.5, "abc", "98 89", NaN, {}, [9889]]) {
    const r = curlRequest({ machine: "cao", port }, { path: "/health" });
    assert.equal(r.ok, false, `port ${JSON.stringify(port)} was accepted`);
  }
});

test("a missing machine is refused instead of running curl on this computer", () => {
  for (const machine of ["", undefined, null, 5, "  "]) {
    assert.equal(curlRequest({ machine, port: 9889 }, { path: "/health" }).ok, false, JSON.stringify(machine));
  }
});

test("a query is encoded and decodes back to the given values; empty values are dropped", () => {
  const query = { agent_profile: "dev", allowed_tools: "a,b", session_name: "", x: undefined, y: null, working_directory: "/w d", odd: "a&b=c#d%é\n+" };
  const { exec } = curlRequest(CFG, { method: "POST", path: "/sessions", query });
  const url = new URL(exec.args.at(-1));
  assert.equal(url.origin, "http://127.0.0.1:9889");
  assert.equal(url.pathname, "/sessions");
  assert.deepEqual([...url.searchParams.keys()].sort(), ["agent_profile", "allowed_tools", "odd", "working_directory"]);
  assert.equal(url.searchParams.get("allowed_tools"), "a,b");
  assert.equal(url.searchParams.get("working_directory"), "/w d");
  assert.equal(url.searchParams.get("odd"), "a&b=c#d%é\n+");
  assert.deepEqual(exec.args.slice(exec.args.indexOf("-X"), exec.args.indexOf("-X") + 2), ["-X", "POST"]);
  assert.ok(!exec.args.at(-1).includes(" "), "the URL is one argument with no raw space");
});

test("a request without a query has no question mark; an all-empty query neither", () => {
  assert.equal(curlRequest(CFG, { path: "/flows", query: {} }).exec.args.at(-1), "http://127.0.0.1:9889/flows");
  assert.equal(curlRequest(CFG, { path: "/flows", query: { a: "", b: undefined } }).exec.args.at(-1), "http://127.0.0.1:9889/flows");
});

test("argument mode: a body of exactly ARG_BODY_MAX bytes is sent as a --data-binary argument", () => {
  assert.equal(ARG_BODY_MAX, 122880);
  const body = bodyOfBytes(122880);
  const r = curlRequest(CFG, { method: "POST", path: "/workflows", body });
  assert.equal(r.ok, true);
  const i = r.exec.args.indexOf("--data-binary");
  assert.equal(r.exec.args[i + 1], JSON.stringify(body));
  assert.ok(r.exec.args.includes("Content-Type: application/json"));
  assert.ok(!(STDIN_OPTION in r.exec.opts));
});

test("argument mode: the same limit holds for a body made of multi-byte characters", () => {
  assert.equal(curlRequest(CFG, { method: "POST", path: "/workflows", body: bodyOfBytes(122880, "é") }).ok, true);
  assert.equal(curlRequest(CFG, { method: "POST", path: "/workflows", body: bodyOfBytes(122881, "é") }).ok, false);
  assert.equal(curlRequest(CFG, { method: "POST", path: "/workflows", body: bodyOfBytes(122880, "€") }).ok, true);
});

test("argument mode: 122881 bytes is refused before exec, naming the CLI path", async () => {
  const body = bodyOfBytes(122881);
  const exec = fakeExec({ stdout: "", code: 0 });
  const outcome = await callCao(exec, CFG, { method: "POST", path: "/workflows", body, cli: "cao workflow create --from-file" });
  assert.equal(exec.calls.length, 0);
  assert.equal(outcome.kind, "too_large");
  assert.match(outcome.message, /too large to send from this window/);
  assert.ok(outcome.message.includes("cao workflow create --from-file"));
});

test("Body too large for argument mode: 130000 encoded bytes", async () => {
  const exec = fakeExec({ stdout: "", code: 0 });
  const outcome = await callCao(exec, CFG, { method: "POST", path: "/workflows", body: bodyOfBytes(130000), cli: "cao workflow create --from-file" });
  assert.equal(exec.calls.length, 0);
  assert.equal(outcome.kind, "too_large");
  assert.ok(outcome.message.includes("cao workflow create --from-file"));
});

test("a too-large refusal without a named command still says what to do, and each write names its own command", async () => {
  const exec = fakeExec({ stdout: "", code: 0 });
  const plain = await callCao(exec, CFG, { method: "POST", path: "/flows", body: bodyOfBytes(130000) });
  assert.equal(plain.kind, "too_large");
  assert.match(plain.message, /cao/);
  for (const cli of ["cao launch", "cao schedule add"]) {
    const o = await callCao(exec, CFG, { method: "POST", path: "/x", body: bodyOfBytes(130000), cli });
    assert.ok(o.message.includes(cli), cli);
  }
  assert.equal(exec.calls.length, 0);
});

test("argument mode: escaping counts, so a source shorter than the cap can still be refused", () => {
  const quotes = { source: '"'.repeat(61_500) };           // 61,500 characters, 123,000 bytes once each " is escaped
  assert.ok(quotes.source.length < ARG_BODY_MAX);
  assert.equal(curlRequest(CFG, { method: "POST", path: "/workflows", body: quotes }).ok, false);
  const accents = { source: "é".repeat(62_000) };           // 62,000 characters, 124,000 bytes of UTF-8
  assert.equal(curlRequest(CFG, { method: "POST", path: "/workflows", body: accents }).ok, false);
  const newlines = { source: "\n".repeat(61_500) };         // each newline is two bytes as \n
  assert.equal(curlRequest(CFG, { method: "POST", path: "/workflows", body: newlines }).ok, false);
});

test("stdin mode: a larger body is sent on stdin and never as an argument", () => {
  const body = bodyOfBytes(300_000);
  const r = curlRequest(STDIN_CFG, { method: "POST", path: "/workflows", body });
  assert.equal(r.ok, true);
  assert.equal(r.exec.args[r.exec.args.indexOf("--data-binary") + 1], "@-");
  assert.equal(r.exec.opts[STDIN_OPTION], JSON.stringify(body));
  assert.ok(!r.exec.args.some((a) => a.length > 1000));
});

test("stdin mode: even a small body travels on stdin, never in the argument list", () => {
  const r = curlRequest(STDIN_CFG, { method: "POST", path: "/flows", body: { name: "secret-looking-name" } });
  assert.ok(!JSON.stringify(r.exec.args).includes("secret-looking-name"));
  assert.equal(r.exec.opts[STDIN_OPTION], '{"name":"secret-looking-name"}');
});

test("the mode defaults to what spike 1.10 recorded", () => {
  assert.equal(typeof STDIN_SUPPORTED, "boolean");
  const body = bodyOfBytes(130000);
  const r = curlRequest({ machine: "cao", port: 9889 }, { method: "POST", path: "/workflows", body });
  assert.equal(r.ok, STDIN_SUPPORTED, "without cfg.stdin the recorded default applies");
});

test("env values are refused in argument mode and allowed on stdin only", async () => {
  const body = { initial_message: "hi", env_vars: { API_URL: "https://example.test" } };
  const exec = fakeExec({ stdout: "", code: 0 });
  const outcome = await callCao(exec, CFG, { method: "POST", path: "/sessions", body });
  assert.equal(outcome.kind, "env_refused");
  assert.equal(exec.calls.length, 0);
  const r = curlRequest(STDIN_CFG, { method: "POST", path: "/sessions", body });
  assert.equal(r.ok, true);
  assert.ok(!JSON.stringify(r.exec.args).includes("example.test"));
  assert.ok(r.exec.opts[STDIN_OPTION].includes("example.test"));
});

test("an env_vars key is refused in argument mode even when empty, and a body without it is not", () => {
  assert.equal(curlRequest(CFG, { method: "POST", path: "/sessions", body: { env_vars: {} } }).ok, false);
  assert.equal(curlRequest(CFG, { method: "POST", path: "/sessions", body: { initial_message: "mentions env_vars in prose" } }).ok, true);
});

test("a path that is not absolute or has whitespace is refused, and segments are checked", () => {
  assert.equal(curlRequest(CFG, { path: "flows" }).ok, false);
  assert.equal(curlRequest(CFG, { path: "/flows /x" }).ok, false);
  assert.equal(curlRequest(CFG, { path: "/flows\n/x" }).ok, false);
  assert.equal(curlRequest(CFG, { path: undefined }).ok, false);
  const doubleSlash = curlRequest(CFG, { path: "//evil.test/x" });
  if (doubleSlash.ok) assert.equal(new URL(doubleSlash.exec.args.at(-1)).hostname, "127.0.0.1");
  assert.equal(seg("cao-draft-flow-0a1b2c3d"), "cao-draft-flow-0a1b2c3d");
  assert.equal(seg("Flow_1"), "Flow_1");
  for (const bad of ["../x", "a/b", "", "a b", ".", "..", "a?b", "a#b", "%2e%2e", "é", "a\nb", undefined, null, {}]) {
    assert.throws(() => seg(bad), /unsafe/, JSON.stringify(bad));
  }
});

test("outcomes: ok, unreachable, auth, approval_required and error", () => {
  const r = (code, body = "{}") => outcomeFrom({ stdout: `${body}\n${code}`, stderr: "" });
  assert.deepEqual({ kind: r(200, '[{"a":1}]').kind, json: r(200, '[{"a":1}]').json }, { kind: "ok", json: [{ a: 1 }] });
  assert.equal(r(202).kind, "ok");
  assert.equal(outcomeFrom({ stdout: "\n000", stderr: "curl: (7) Failed to connect" }).kind, "unreachable");
  assert.equal(outcomeFrom({ stdout: "", stderr: "" }).kind, "unreachable");
  assert.equal(r(401).kind, "auth");
  assert.equal(r(403, '{"detail":"forbidden"}').kind, "auth");
  const approval = r(403, JSON.stringify({ detail: { kind: "approval_required", plan_id: "p:1", message: "approve it" } }));
  assert.deepEqual([approval.kind, approval.planId], ["approval_required", "p:1"]);
  const notFound = r(404, '{"detail":"Flow \'x\' not found"}');
  assert.deepEqual([notFound.kind, notFound.status, notFound.detail], ["error", 404, "Flow 'x' not found"]);
  const parse = r(422, JSON.stringify({ detail: [{ loc: ["body", "name"], msg: "bad", type: "x" }] }));
  assert.deepEqual([parse.kind, parse.detail], ["error", "body.name: bad"]);
});

test("Unreachable server: status 000 gives an unreachable outcome with no body", () => {
  const o = outcomeFrom({ stdout: "\n000", stderr: "curl: (28) Operation timed out" });
  assert.equal(o.kind, "unreachable");
  assert.equal(o.body, undefined);
  assert.equal(o.json, undefined);
});

test("the 2xx boundary: 200 to 299 are ok, 199 and 300 are not", () => {
  for (const code of [200, 201, 204, 299]) assert.equal(curl(code).kind, "ok", String(code));
  for (const code of [199, 300, 301, 400, 409, 429, 500, 503]) assert.equal(curl(code).kind, "error", String(code));
  assert.equal(curl(204, "").kind, "ok");
  assert.equal(curl(204, "").json, undefined);
});

test("401 is auth whatever the body; 403 is auth unless it is the approval refusal", () => {
  for (const body of ["", "not json", "{}", '{"detail":{"kind":"approval_required","plan_id":"p"}}']) assert.equal(curl(401, body).kind, "auth", body);
  for (const body of ["", "<html>forbidden</html>", '{"detail":{"kind":"something_else"}}', '{"detail":"approval_required"}', '{"detail":{"kind":"approval_required "}}', "[]", "null"]) {
    assert.equal(curl(403, body).kind, "auth", body);
  }
  assert.equal(AUTH_MESSAGE, "CAO requires auth - not supported by this window yet");
});

test("Approval refusal is not auth", () => {
  const o = outcomeFrom({ stdout: `${JSON.stringify({ detail: { kind: "approval_required", plan_id: "wf:abc", message: "m" } })}\n403`, stderr: "" });
  assert.deepEqual([o.kind, o.planId], ["approval_required", "wf:abc"]);
  assert.notEqual(o.kind, "auth");
});

test("an approval refusal without a plan id still is not auth and carries a null plan id", () => {
  const o = curl(403, '{"detail":{"kind":"approval_required"}}');
  assert.equal(o.kind, "approval_required");
  assert.equal(o.planId, null);
});

test("the approval kind is honoured only on a 403", () => {
  for (const code of [200, 400, 401, 404, 500]) assert.notEqual(curl(code, '{"detail":{"kind":"approval_required","plan_id":"p"}}').kind, "approval_required", String(code));
});

test("error outcomes carry the status and the server's detail in each shape", () => {
  assert.deepEqual([curl(409, '{"detail":"exists"}').status, curl(409, '{"detail":"exists"}').detail], [409, "exists"]);
  const two = curl(422, JSON.stringify({ detail: [{ loc: ["body", "a"], msg: "x" }, { loc: ["query", "b"], msg: "y" }] }));
  assert.equal(two.detail, "body.a: x; query.b: y");
  const nested = curl(422, JSON.stringify({ detail: { errors: [{ field: "schedule" }] } }));
  assert.equal(nested.kind, "error");
  assert.ok(nested.detail.includes("schedule"));
  assert.deepEqual(nested.json.detail.errors, [{ field: "schedule" }]);
  assert.equal(curl(500, "Internal Server Error").detail, "Internal Server Error");
});

test("a response that is not JSON never throws: a 200 is ok without json, a 500 is an error", () => {
  const ok = curl(200, "<html>proxy page</html>");
  assert.equal(ok.kind, "ok");
  assert.equal(ok.json, undefined);
  assert.equal(ok.body, "<html>proxy page</html>");
  assert.equal(curl(500, "<html>boom</html>").kind, "error");
  assert.equal(curl(200, "{truncated").kind, "ok");
});

test("a huge error body is cut down before it reaches the UI", () => {
  const o = curl(500, "x".repeat(50_000));
  assert.equal(o.kind, "error");
  assert.ok(o.detail.length <= 500, `${o.detail.length} characters`);
});

test("a result that is not an exec result is read as unreachable, not as a crash", () => {
  for (const result of [undefined, null, {}, { stdout: null }, { stdout: 42 }, { stderr: "x" }, "oops"]) {
    assert.equal(outcomeFrom(result).kind, "unreachable", JSON.stringify(result));
  }
});

test("a response whose last line is not a three-digit code is unreachable", () => {
  for (const stdout of ["body only", '{"a":1}\n20', '{"a":1}\n2000', '{"a":1}\nabc']) {
    assert.equal(outcomeFrom({ stdout, stderr: "" }).kind, "unreachable", stdout);
  }
});

test("callCao runs exec once with the built command and returns the mapped outcome", async () => {
  const exec = fakeExec({ stdout: '{"ok":true}\n201', stderr: "" });
  const spec = { method: "POST", path: "/flows", body: { name: "daily" } };
  const o = await callCao(exec, CFG, spec);
  assert.equal(exec.calls.length, 1);
  assert.equal(exec.calls[0].cmd, "curl");
  assert.equal(exec.calls[0].opts.on, "cao");
  assert.deepEqual([o.kind, o.status, o.json], ["ok", 201, { ok: true }]);
  assert.deepEqual(spec, { method: "POST", path: "/flows", body: { name: "daily" } }, "the spec was changed");
});

test("callCao turns an exec rejection into unreachable", async () => {
  const outcome = await callCao(async () => { throw new Error("no such machine"); }, CFG, { path: "/health" });
  assert.equal(outcome.kind, "unreachable");
});

test("callCao turns a synchronous exec throw and a non-Error rejection into unreachable", async () => {
  assert.equal((await callCao(() => { throw "plain string"; }, CFG, { path: "/health" })).kind, "unreachable");
  assert.equal((await callCao(async () => Promise.reject(undefined), CFG, { path: "/health" })).kind, "unreachable");
});

test("callCao never calls exec for a refused request", async () => {
  const exec = fakeExec({ stdout: "{}\n200", stderr: "" });
  for (const spec of [{ path: "no-slash" }, { method: "POST", path: "/s", body: { env_vars: { A: "b" } } }, { method: "POST", path: "/w", body: bodyOfBytes(130000) }]) {
    assert.notEqual((await callCao(exec, CFG, spec)).kind, "ok");
  }
  assert.equal(exec.calls.length, 0);
});

test("bodyBytes counts UTF-8 of the JSON encoding", () => {
  assert.equal(bodyBytes({ a: "é" }), new TextEncoder().encode('{"a":"é"}').length);
  assert.equal(bodyBytes({ a: "€" }), enc.encode('{"a":"€"}').length);
  assert.equal(bodyBytes({ a: "😀" }), enc.encode('{"a":"😀"}').length);
  assert.equal(bodyBytes({ a: '"' }), 10);
});

test("a path with glob characters never reaches curl as a globbing request", () => {
  for (const path of ["/flows/a[1-2]", "/flows/{a,b}", "/flows/a]", "/x/[", "/x/}", "/x/{", "/flows/a[]b"]) {
    const r = curlRequest(CFG, { path });
    if (!r.ok) continue;                                    // refusing is one safe answer
    assert.ok(r.exec.args.includes("-g") || r.exec.args.includes("--globoff"), `${path}: curl would expand it as a range or list`);
  }
  assert.equal(curlRequest(CFG, { path: "/flows/daily-1" }).ok, true, "an ordinary path must still be sent");
});

test("brackets and braces in a query reach curl encoded, or curl is told not to glob", () => {
  const r = curlRequest(CFG, { path: "/flows", query: { "a[0]": "x[1-2]", "k{": "{a,b}" } });
  assert.equal(r.ok, true);
  const safe = r.exec.args.includes("-g") || r.exec.args.includes("--globoff");
  assert.ok(safe || !/[\[\]{}]/.test(r.exec.args.at(-1)), `${r.exec.args.at(-1)} holds a raw bracket or brace`);
});

const CAO_JS = join(dirname(fileURLToPath(import.meta.url)), "cao.js");

test("cao.js loads and works in a runtime without TextEncoder or URLSearchParams", () => {
  const script = join(mkTmp("cao-window-noglobals-"), "probe.mjs");
  writeFileSync(script, `
    delete globalThis.TextEncoder;
    delete globalThis.URLSearchParams;
    if (typeof TextEncoder !== "undefined" || typeof URLSearchParams !== "undefined") throw new Error("the globals are still there");
    const cao = await import(${JSON.stringify(pathToFileURL(CAO_JS).href)});
    const cfg = { machine: "cao", port: 9889, stdin: false };
    const sized = (total) => {                                  // a POST /workflows-shaped body of exactly total UTF-8 bytes
      const room = total - cao.bodyBytes({ source: "" });
      return { source: "\\u00e9".repeat(Math.floor(room / 2)) + "x".repeat(room % 2) };
    };
    const out = {
      bytes: cao.bodyBytes({ a: "\\u00e9\\u20ac\\ud83d\\ude00" }),
      atCap: cao.curlRequest(cfg, { method: "POST", path: "/w", body: sized(cao.ARG_BODY_MAX) }).ok,
      overCap: cao.curlRequest(cfg, { method: "POST", path: "/w", body: sized(cao.ARG_BODY_MAX + 1) }).ok,
      url: cao.curlRequest(cfg, { path: "/sessions", query: { agent_profile: "dev", working_directory: "/w d", odd: "a&b=c#d%\\u00e9+", empty: "" } }).exec.args.at(-1),
    };
    console.log(JSON.stringify(out));
  `);
  const r = spawnSync(process.execPath, [script], { encoding: "utf8" });
  assert.equal(r.status, 0, `cao.js does not run without those globals:\n${r.stderr.slice(0, 600)}`);
  const out = JSON.parse(r.stdout.trim().split("\n").at(-1));
  assert.equal(out.bytes, Buffer.byteLength(JSON.stringify({ a: "é€😀" })));
  assert.equal(out.atCap, true);
  assert.equal(out.overCap, false);
  const [base, query] = out.url.split("?");
  assert.equal(base, "http://127.0.0.1:9889/sessions");
  const decoded = Object.fromEntries(query.split("&").map((kv) => kv.split("=").map((x) => decodeURIComponent(x.replace(/\+/g, " ")))));
  assert.deepEqual(decoded, { agent_profile: "dev", working_directory: "/w d", odd: "a&b=c#d%é+" });
});

test("a body that does not encode to a JSON string is refused, never sent with a missing argument", async () => {
  const circular = {};
  circular.self = circular;
  const bodies = [["a function", () => 1], ["a symbol", Symbol("x")], ["a bigint", 10n], ["a circular object", circular], ["a toJSON that returns nothing", { toJSON() { return undefined; } }]];
  for (const cfg of [CFG, STDIN_CFG]) {
    for (const [what, body] of bodies) {
      let r;
      assert.doesNotThrow(() => { r = curlRequest(cfg, { method: "POST", path: "/flows", body }); }, `${what} (stdin ${cfg.stdin}) made curlRequest throw`);
      assert.equal(r.ok, false, `${what} (stdin ${cfg.stdin}) was accepted: the app's exec would drop the missing argument and curl would take the URL as data`);
      const exec = fakeExec({ stdout: "{}\n200", stderr: "" });
      const outcome = await callCao(exec, cfg, { method: "POST", path: "/flows", body });
      assert.equal(exec.calls.length, 0, what);
      assert.notEqual(outcome.kind, "ok", what);
    }
  }
});

test("every argument of a request, and its stdin text, is a string", () => {
  const specs = [
    { path: "/health" },
    { path: "/flows", query: { n: 5, b: true, z: 0, s: "x" } },
    { method: "POST", path: "/flows", body: { name: "daily" } },
    { method: "POST", path: "/flows", body: null },
    { method: "POST", path: "/flows", body: "plain text" },
    { method: "POST", path: "/flows", body: [1, 2, 3] },
    { method: "PUT", path: "/flows/daily", body: { enabled: false }, maxTime: 3.5 },
    { method: "DELETE", path: "/sessions/cao-draft-flow-0a1b2c3d", maxTime: 20 },
    { method: "POST", path: "/flows/daily/run", maxTime: 180 },
  ];
  for (const cfg of [CFG, STDIN_CFG]) {
    for (const spec of specs) {
      const r = curlRequest(cfg, spec);
      assert.equal(r.ok, true, JSON.stringify(spec));
      r.exec.args.forEach((a, i) => assert.equal(typeof a, "string", `${JSON.stringify(spec)}: argument ${i} is ${typeof a}`));
      if (STDIN_OPTION in r.exec.opts) assert.equal(typeof r.exec.opts[STDIN_OPTION], "string", JSON.stringify(spec));
      if (spec.body !== undefined && !cfg.stdin) assert.equal(r.exec.args[r.exec.args.indexOf("--data-binary") + 1], JSON.stringify(spec.body));
    }
  }
});

test("a null body is sent as the JSON text null, and an undefined body sends no body at all", () => {
  const nul = curlRequest(CFG, { method: "POST", path: "/flows", body: null });
  assert.equal(nul.exec.args[nul.exec.args.indexOf("--data-binary") + 1], "null");
  const none = curlRequest(CFG, { method: "POST", path: "/flows", body: undefined });
  assert.ok(!none.exec.args.includes("--data-binary"));
});

// ---- what an error answer shows: a 422 that echoes the request body must not put an env value in any text ----

const SECRET = "sk-live-5e7f1";
// Every string of a value, keys included, except what sits under a key named in `skip`.
function strings(value, skip = []) {
  if (typeof value === "string") return [value];
  if (value === null || typeof value !== "object") return [];
  return Object.entries(value).flatMap(([k, v]) => (skip.includes(k) ? [] : [k, ...strings(v, skip)]));
}
const echoBody = (loc) => JSON.stringify({ detail: [{ type: "value_error", loc, msg: "value is not valid", input: { env_vars: { K: SECRET } }, ctx: { error: SECRET } }] });

test("outcomeFrom of a 422 that echoes an env value in its input gives the location and message as text, and never the value", () => {
  for (const loc of [["body", "env_vars"], ["body", "env_vars", "K"], ["query", "agent_profile"], [], "body", null]) {
    const out = outcomeFrom({ stdout: `${echoBody(loc)}\n422`, stderr: "", code: 0 });
    assert.deepEqual([out.kind, out.status], ["error", 422], JSON.stringify(loc));
    assert.ok(out.detail.includes("value is not valid"), out.detail);
    const text = strings(out, ["json"]);
    assert.ok(!text.some((t) => t.includes(SECRET)), `the value is in ${JSON.stringify(text)}`);
  }
});

test("outcomeFrom shows no env value from an error whose detail is text or an empty list, though the body echoes one", () => {
  for (const body of [JSON.stringify({ detail: "bad request", input: { env_vars: { K: SECRET } } }), JSON.stringify({ detail: [], input: SECRET })]) {
    const out = outcomeFrom({ stdout: `${body}\n400`, stderr: "", code: 0 });
    assert.ok(!strings(out, ["json"]).some((t) => t.includes(SECRET)), body);
  }
});

// ---- a body is data, never a file: the --data-binary operand in argument mode ----------------------------------

const AT = ["@/etc/passwd", "@-", "@", "@@x", "@../../key"];
const ENV_VALUE = "tok-ENV-9c1d";
const ENV_ROWS = [{ key: "API_TOKEN", value: ENV_VALUE }];
const ENV_OBJ = { API_TOKEN: ENV_VALUE };
const ENV_EXTRAS = { env: ENV_ROWS, env_vars: ENV_OBJ, envVars: ENV_OBJ };

// Every request the window builds with a body, from text that tries to be a file name; `extra` adds fields no builder takes.
function bodyRequests(at, extra = {}) {
  const fields = { name: "daily", schedule: "0 9 * * mon-fri", agent_profile: at, provider: "claude_code", prompt: at, ...extra };
  const before = { schedule: "0 8 * * *", agent_profile: "x", provider: "kiro_cli", prompt_template: "old" };
  const draftParams = { kind: "flow", description: at, profile: "author", provider: "claude_code", workdir: "/work/repo", idempotencyKey: "k1", hex: "0a1b2c3d", ...extra };
  return {
    "launch": launch.buildRequest({ ...launch.emptyForm("/work/repo"), profile: "developer", message: at, idempotencyKey: "k1", ...extra }, { resolved: "claude_code", stdin: false }),
    "flow create": { method: "POST", path: "/flows", body: flows.flowCreateBody(fields) },
    "flow create and enable": { method: "POST", path: "/flows", body: flows.flowCreateBody(fields, { enable: true }) },
    "flow edit": { method: "PUT", path: "/flows/daily", body: flows.flowUpdateBody(before, fields) },
    "flow validate": { method: "POST", path: "/flows/validate", body: flows.validateBody(fields) },
    "workflow create": workflows.saveRequest({ mode: "new", name: "nightly", tier: "python", source: at, ...extra }),
    "workflow update": workflows.saveRequest({ mode: "edit", name: "nightly", tier: "yaml", source: at, expectedHash: "h1", ...extra }),
    "workflow run": workflows.submitRequest("nightly", { text: at }, "/work/repo"),
    "draft launch": draft.draftRequest(draftParams),
    "draft launch of a workflow": draft.draftRequest({ ...draftParams, kind: "workflow" }),
    "draft fix": draft.fixRequest(draftParams, { fields: { prompt_template: at } }, [at]),
  };
}

test("in argument mode the --data-binary operand of every body the window builds is JSON text, never an @file, whatever the user typed", () => {
  for (const at of AT) {
    for (const [name, spec] of Object.entries(bodyRequests(at))) {
      const r = curlRequest(CFG, { ...spec, maxTime: 10 });
      assert.equal(r.ok, true, `${name} ${at}`);
      const args = r.exec.args;
      assert.equal(args.filter((a) => a === "--data-binary").length, 1, `${name}: one body`);
      const operand = args[args.indexOf("--data-binary") + 1];
      assert.ok(!operand.startsWith("@"), `${name} ${at}: curl would read a file: ${operand.slice(0, 30)}`);
      assert.equal(operand, JSON.stringify(spec.body), `${name} ${at}`);
      assert.ok(args.every((a, i) => i === args.indexOf("--data-binary") + 1 || !a.startsWith("@")), `${name} ${at}: an argument starts with @`);
    }
  }
});

test("a body that is itself text, a list or a number is sent as JSON text too, so a string that starts with @ is quoted", () => {
  for (const body of ["@/etc/passwd", "@-", ["@x"], 0, false, "", " @x"]) {
    const r = curlRequest(CFG, { method: "POST", path: "/flows", body });
    const operand = r.exec.args[r.exec.args.indexOf("--data-binary") + 1];
    assert.equal(operand, JSON.stringify(body));
    assert.ok(!operand.startsWith("@"), JSON.stringify(body));
  }
});

test("a draft, flow or workflow request built from a form that also holds env rows carries no env value, in its body, its query or its curl arguments", () => {
  for (const [name, spec] of Object.entries(bodyRequests("ordinary text", ENV_EXTRAS))) {
    assert.ok(!JSON.stringify(spec).includes(ENV_VALUE), `${name}: the request spec holds the value`);
    const r = curlRequest(CFG, { ...spec, maxTime: 10 });
    assert.equal(r.ok, true, name);
    assert.ok(!r.exec.args.join("\n").includes(ENV_VALUE), `${name}: a curl argument holds the value`);
    assert.ok(!Object.hasOwn(spec.body ?? {}, "env_vars") && !Object.hasOwn(spec.body ?? {}, "env"), `${name}: the body names env`);
  }
});

await finish();
