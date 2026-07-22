# AG-UI Examples

Runnable examples demonstrating CAO's AG-UI integration — the streaming surface
that exposes multi-agent fleet observability to any stock AG-UI client.

## Prerequisites

- CAO server running with `CAO_AGUI_ENABLED=true`
- `uv sync --extra agui` (installs `ag-ui-protocol` SDK)
- Node.js 18+ (for the stock-client and construct-demos recorder only)

## Examples

| Directory | Description | Requires |
|-----------|-------------|----------|
| [`ag-ui-supervisor-dashboard/`](ag-ui-supervisor-dashboard/) | L2 `SupervisorDashboardStream` construct — folds STATE_SNAPSHOT/DELTA into a live fleet hierarchy | `uv run` |
| [`ag-ui-session-timeline/`](ag-ui-session-timeline/) | L2 `MultiAgentSessionTimeline` construct — reconstructs delegation + message timeline from TOOL_CALL lifecycle | `uv run` |
| [`ag-ui-handoff-approval/`](ag-ui-handoff-approval/) | L2 `AgentHandoffWithApproval` construct — full interrupt lifecycle (prompt → classify → interrupt → approve/deny/edit → delivery) | `uv run` |
| [`ag-ui-cross-provider-sync/`](ag-ui-cross-provider-sync/) | L2 `CrossProviderStateSync` construct — convergence proof across kiro_cli / claude_code / codex | `uv run` |
| [`ag-ui-stock-client-live/`](ag-ui-stock-client-live/) | AC3 verification — stock `@ag-ui/client` `HttpAgent` renders live frames from `POST /agui/v1/run` with zero CAO-specific code | Node.js |
| [`ag-ui-meta-dogfood/`](ag-ui-meta-dogfood/) | Real supervisor→developer→reviewer cross-provider fleet captured on the live AG-UI stream (metadata only, privacy boundary verified) | `uv run` + `cao-server` |
| [`ag-ui-dashboard/`](ag-ui-dashboard/) | L1 showcase — drives all 6 `emit_ui` components live and demonstrates off-list refusal | `uv run` |
| [`ag-ui-eventsource-viewer/`](ag-ui-eventsource-viewer/) | Browser-based SSE viewer for `GET /agui/v1/stream` — raw frame inspector | Browser |
| [`ag-ui-construct-demos/`](ag-ui-construct-demos/) | CI tooling — Playwright + ffmpeg recorder that generates the shift-left GIFs from the asserting examples above | Node.js |

## Running an example

Each example has a `run.sh` (or `capture.py`) that is self-contained:

```bash
# Start the server with AG-UI enabled
CAO_AGUI_ENABLED=true uv run cao-server &

# Run any example
./examples/ag-ui/supervisor-dashboard/run.sh
./examples/ag-ui/handoff-approval/run.sh
./examples/ag-ui/stock-client-live/run.sh
```

The L2 construct examples (`supervisor-dashboard`, `session-timeline`,
`cross-provider-sync`, `handoff-approval`) are **asserting** — they exit non-zero
on any drift, making them suitable for CI gates.

## Shift-left recordings

The `construct-demos/tools/` directory contains the recorder that generates the
demo GIFs referenced in the PR description and `docs/media/`. Regenerate:

```bash
cd examples/ag-ui/construct-demos/tools && npm ci && npm run record
```

## See also

- [`docs/agui.md`](../../docs/agui.md) — full AG-UI reference documentation
- [`skills/agui-author/`](../../skills/agui-author/) — skill for emitting `emit_ui` intents
- [AG-UI introduction](https://docs.ag-ui.com/introduction) — protocol overview
