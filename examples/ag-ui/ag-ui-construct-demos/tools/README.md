# AG-UI construct demos — shift-left recorder

Build/CI tooling that produces a **gated** demo GIF for each new AG-UI L2
construct. It is not required to use the constructs.

## What it does

For each of the four L2 constructs it runs that construct's runnable example
(`examples/ag-ui/*/run.sh`) in offline / synthetic mode — no live provider,
network, or secrets — captures the terminal output, renders it into a
terminal-styled page recorded by headless Chromium, and exports an optimized GIF
to `docs/media/agui-<slug>-demo.gif`.

| Slug | Construct | Example |
|---|---|---|
| `agui-supervisor-dashboard` | `SupervisorDashboardStream` | `examples/ag-ui/ag-ui-supervisor-dashboard/run.sh` |
| `agui-session-timeline` | `MultiAgentSessionTimeline` | `examples/ag-ui/ag-ui-session-timeline/run.sh` |
| `agui-handoff-approval` | `AgentHandoffWithApproval` | `examples/ag-ui/ag-ui-handoff-approval/run.sh` |
| `agui-cross-provider-sync` | `CrossProviderStateSync` | `examples/ag-ui/ag-ui-cross-provider-sync/run.sh` |

## The shift-left gate

The recording **is** the test. A GIF is only exported if the example exits `0`
and prints its `PASS` marker. If a construct regresses, the example exits
non-zero, the recorder exits non-zero, and the CI job
`AG-UI construct demos (shift-left recordings)` goes red. A broken construct
cannot produce a green recording.

## Running

```sh
cd examples/ag-ui/ag-ui-construct-demos/tools
npm ci
npm run playwright:install
npm run record                      # all four features
ONLY=agui-session-timeline npm run record   # a single feature
```

`ffmpeg` is provided by the `ffmpeg-static` npm package (gif-capable), so no
system ffmpeg install is needed. Override with `FFMPEG_BIN=/path/to/ffmpeg`.

The GIFs are committed under `docs/media/` and re-generated + uploaded as the
`agui-construct-demos` CI artifact on every run.
