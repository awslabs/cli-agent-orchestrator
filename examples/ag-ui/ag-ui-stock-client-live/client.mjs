// AG-UI stock client (P0-2 / AC3): uses the pinned upstream @ag-ui/client
// HttpAgent with ZERO CAO-specific wire code to POST a RunAgentInput to
// /agui/v1/run and assert at least one post-connect frame is received.
//
// This is the literal AC3 artifact: a stock AG-UI SDK client speaking the run
// plane's stock wire dialect. No CopilotKit page is needed to prove protocol
// compliance — HttpAgent IS the reference client.
import { HttpAgent } from "@ag-ui/client";

const base = process.argv[2] || "http://localhost:9889";
const agent = new HttpAgent({ url: `${base}/agui/v1/run` });

const rand = () => Math.random().toString(16).slice(2, 10);
const input = {
  threadId: `thread-${rand()}`,
  runId: `run-${rand()}`,
  state: {},
  messages: [],
  tools: [],
  context: [],
  forwardedProps: {},
};

console.log(`[stock-client] pinned @ag-ui/client HttpAgent -> POST ${base}/agui/v1/run`);

const frames = [];
await new Promise((resolve) => {
  const sub = agent.run(input).subscribe({
    next: (event) => {
      frames.push(event);
      console.log(`  frame: type=${event.type}`);
      if (frames.length >= 3) {
        sub.unsubscribe();
        resolve();
      }
    },
    error: (err) => {
      // The run plane keeps the SSE stream open for the live bus; a read
      // abort after the initial lifecycle frames is expected, not a failure.
      console.log(`[stock-client] stream ended: ${err?.message ?? err}`);
      resolve();
    },
    complete: () => resolve(),
  });
  setTimeout(() => {
    sub.unsubscribe();
    resolve();
  }, 8000);
});

console.log(`\n[stock-client] Received ${frames.length} frame(s).`);
if (frames.length === 0) {
  console.error("[stock-client] FAIL: no frames received from POST /agui/v1/run");
  process.exit(1);
}
if (frames[0].type !== "RUN_STARTED") {
  console.error(`[stock-client] FAIL: first frame type=${frames[0].type} (expected RUN_STARTED)`);
  process.exit(1);
}
console.log("[stock-client] PASS: >=1 post-connect frame via the pinned @ag-ui/client SDK.");
process.exit(0);
