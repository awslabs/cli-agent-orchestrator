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
`STEP_STARTED/FINISHED`, `TOOL_CALL_*`, `CUSTOM`). Those are on the ambient
plane, `GET /agui/v1/stream`.

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
