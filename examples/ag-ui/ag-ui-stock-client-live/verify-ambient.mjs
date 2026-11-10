// Verifies CAO's ambient stream (GET/POST /agui/v1/stream) with the pinned
// upstream AG-UI verifier: @ag-ui/client's verifyEvents, the same check every
// stock AG-UI client runs on the events it receives. The Python pre-check in
// test/_support/agui_conformance.py is a port; this script has the final word.
//
// Usage:
//   node verify-ambient.mjs [--captures DIR] [--live BASE_URL]
//
//   --captures DIR  verify every *.sse file in DIR (default: $CAO_AGUI_CAPTURE_DIR).
//                   The conformance tests write their streams there when the
//                   variable is set.
//   --live BASE     POST a RunAgentInput to BASE/agui/v1/stream with the stock
//                   HttpAgent (forwardedProps.cao.maxSeconds = 5) and require
//                   a verified run that ends RUN_FINISHED with outcome cancelled.
//
// Before either, a self-test proves the verifier is live: one upstream corpus
// stream that must fail and one that must pass.
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { HttpAgent, verifyEvents } from "@ag-ui/client";
import { from, lastValueFrom, toArray } from "rxjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const CORPUS = resolve(HERE, "../../../test/fixtures/agui_conformance_1_0/streams");

const args = process.argv.slice(2);
const opt = (name) => {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : undefined;
};
const capturesDir = opt("--captures") ?? process.env.CAO_AGUI_CAPTURE_DIR;
const liveBase = opt("--live");

let failures = 0;
const report = (ok, what) => {
  console.log(`${ok ? "PASS" : "FAIL"} ${what}`);
  if (!ok) failures++;
};

async function verify(events) {
  try {
    const out = await lastValueFrom(from(events).pipe(verifyEvents(false), toArray()));
    return { ok: true, count: out.length };
  } catch (e) {
    return { ok: false, error: String(e?.message ?? e) };
  }
}

function sseEvents(text) {
  return text
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => JSON.parse(line.slice(5)));
}

// 1. Self-test: the verifier rejects a known-bad stream and accepts a known-good one.
for (const [name, mustPass] of [
  ["first-event-not-run-started-fatal", false],
  ["run-finished-cancelled-outcome-accepted", true],
]) {
  const fixture = JSON.parse(readFileSync(join(CORPUS, `${name}.json`), "utf8"));
  const result = await verify(fixture.stream);
  const ok = result.ok === mustPass &&
    (mustPass || !fixture.expect?.errorContains || result.error.includes(fixture.expect.errorContains));
  report(ok, `self-test ${name} (${mustPass ? "must pass" : "must fail"})${result.error ? `: ${result.error}` : ""}`);
}

// 2. Every capture the conformance tests wrote.
if (capturesDir) {
  const files = readdirSync(capturesDir).filter((f) => f.endsWith(".sse")).sort();
  report(files.length > 0, `captures found in ${capturesDir}: ${files.length}`);
  for (const file of files) {
    const events = sseEvents(readFileSync(join(capturesDir, file), "utf8"));
    const result = await verify(events);
    report(result.ok, `${file}: ${result.ok ? `${result.count} events` : result.error}`);
  }
}

// 3. A live run through the stock HttpAgent.
if (liveBase) {
  const agent = new HttpAgent({ url: `${liveBase.replace(/\/+$/, "")}/agui/v1/stream` });
  const rand = () => Math.random().toString(16).slice(2, 10);
  const input = {
    threadId: `verify-ambient-${rand()}`,
    runId: `verify-ambient-run-${rand()}`,
    state: {},
    messages: [],
    tools: [],
    context: [],
    forwardedProps: { cao: { maxSeconds: 5 } },
  };
  const events = [];
  let streamError = null;
  await new Promise((done) => {
    const sub = agent.run(input).subscribe({
      next: (event) => events.push(event),
      error: (err) => { streamError = String(err?.message ?? err); done(); },
      complete: () => done(),
    });
    setTimeout(() => { sub.unsubscribe(); streamError = streamError ?? "timed out after 20s"; done(); }, 20_000);
  });
  report(streamError === null, `live HttpAgent POST /agui/v1/stream completed${streamError ? `: ${streamError}` : ""}`);
  const result = await verify(events);
  report(result.ok, `live run verified: ${result.ok ? `${result.count} events` : result.error}`);
  const last = events[events.length - 1] ?? {};
  report(
    events[0]?.type === "RUN_STARTED" && events[0]?.runId === input.runId,
    `live run opened RUN_STARTED echoing runId (got ${events[0]?.type} ${events[0]?.runId})`,
  );
  report(
    last.type === "RUN_FINISHED" && last.outcome?.type === "cancelled",
    `live run ended RUN_FINISHED cancelled (got ${last.type} ${JSON.stringify(last.outcome)})`,
  );
}

console.log(failures ? `FAIL verify-ambient (${failures})` : "PASS verify-ambient");
process.exit(failures ? 1 : 0);
