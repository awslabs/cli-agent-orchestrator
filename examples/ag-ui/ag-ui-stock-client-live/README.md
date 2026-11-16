# AG-UI Stock Client Live Example

Proves the AG-UI run plane (`POST /agui/v1/run`) works with the **pinned upstream
`@ag-ui/client` SDK** (`HttpAgent`) speaking the stock protocol — the literal AC3
artifact. No CopilotKit page, no CAO-specific adapter, **zero CAO wire code**:
`HttpAgent` is the reference client.

## What it shows

- Booting `cao-server` with `CAO_AGUI_ENABLED=1`
- A stock `@ag-ui/client` `HttpAgent` POSTing a `RunAgentInput` to `/agui/v1/run`
- The SDK decoding the `data:`-only SSE stream into typed `BaseEvent`s
- Verifying lifecycle-legal frame order (RUN_STARTED first)
- At least one frame received from post-connect server activity

## Running

```sh
./examples/ag-ui/ag-ui-stock-client-live/run.sh
```

Requires **Node.js** (for the `@ag-ui/client` SDK) and the `[agui]` extra
(`uv sync --extra agui`). Uses `mock_cli` on PATH for credentials-free
operation. The server starts in the background, the SDK client connects and
verifies, then everything is cleaned up.

## What the run plane returns

A successful run produces this frame sequence, and the body ends after the
last frame:

```
RUN_STARTED        -> echo threadId/runId
STATE_SNAPSHOT     -> current fleet state
RUN_FINISHED       -> outcome: {type: "success"} or {type: "interrupt", ...}
```

The run plane does not stream live fleet frames (`STATE_DELTA`,
`SUBAGENT_*`, `TOOL_CALL_*`, `CUSTOM`). Those are on the ambient plane,
`GET`/`POST /agui/v1/stream`.

## The ambient stream (`verify-ambient.mjs`)

`run.sh` then checks the ambient stream with the same pinned SDK's
`verifyEvents`, the check every stock AG-UI client applies to what it receives:

1. A self-test: one upstream corpus stream that must fail and one that must pass,
   so a verifier that accepts everything cannot report green.
2. Every stream the conformance tests write when `CAO_AGUI_CAPTURE_DIR` is set
   (`test/api/test_agui_stream_conformance.py`).
3. A live `HttpAgent` run against `POST /agui/v1/stream` with
   `forwardedProps.cao.maxSeconds: 5`, which must echo the `runId` and end
   `RUN_FINISHED` with outcome `cancelled`.

```sh
node verify-ambient.mjs --captures "$CAO_AGUI_CAPTURE_DIR" --live http://localhost:9889
```

## Stock client code (minimal — `client.mjs`)

```js
import { HttpAgent } from "@ag-ui/client"; // pinned upstream SDK, no CAO wire code

const agent = new HttpAgent({ url: "http://localhost:9889/agui/v1/run" });
agent
  .run({ threadId: "t", runId: "r", state: {}, messages: [], tools: [], context: [], forwardedProps: {} })
  .subscribe((event) => console.log(event.type)); // RUN_STARTED, STATE_SNAPSHOT, ...
```

The `@ag-ui/client` version is pinned in `package.json`; `run.sh` installs it on
first run. Because the client is solely `@ag-ui/*` packages, a passing run proves
the run plane is stock-AG-UI-protocol compliant.

`package.json` also overrides `zod` to `^4.6.6`. The pinned `@ag-ui/*` packages
ask for zod 3, and zod 3 has no fix for CVE-2023-54404. The `@ag-ui/*` code does
not change, and the client still parses every frame with the SDK event schemas.

## Trace verification lane (`verify-trace.mjs`)

The same pinned SDK is also the node lane of the CAO Dojo trace check (#1116).
`verify-trace.mjs` feeds each recorded run through upstream's own
`enforceEvents()` and `verifyEvents()`, sends each stream card through
`enforceEvents()`, and fails on any verifier error or any enforcement warning
not listed in [`../cao-dojo/tools/known_gaps.json`](../cao-dojo/tools/known_gaps.json).
It first runs three vendored upstream conformance streams and checks each one's
declared outcome, so the lane is shown to fail before it is trusted to pass.

```sh
(cd examples/ag-ui/ag-ui-stock-client-live && npm ci)
uv run python examples/ag-ui/cao-dojo/tools/record_trace.py --offline --emit /tmp/offline.ndjson
node examples/ag-ui/ag-ui-stock-client-live/verify-trace.mjs /tmp/offline.ndjson
```

`make check-agui-trace` runs this, and CI runs it on the keyless `--mock`
recording in the `AG-UI stock-client live (AC3)` job.
