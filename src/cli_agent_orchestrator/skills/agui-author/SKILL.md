---
name: agui-author
description: Author live dashboard UI from an agent via the `emit_ui` MCP tool. Emit
  one of six allow-listed components (approval_card, choice_prompt, diff_summary,
  progress, metric, agent_card) with JSON props and it renders in any AG-UI client
  watching the fleet. Use when you want the operator to see a decision, a diff, or a
  status readout instead of scrolling terminal text. Arbitrary HTML/markup is refused.
---

# Authoring generative UI over AG-UI

CAO exposes an **AG-UI** stream (`GET /agui/v1/stream`) that any dashboard —
CopilotKit, the AG-UI Dojo, or a plain `EventSource` — renders
without CAO-specific code. As an agent you can push a **declarative UI intent**
onto that stream with the `emit_ui` MCP tool. The operator sees a rendered card,
not raw text — and because every provider's intents render uniformly, they can't
tell (and don't need to) which CLI agent produced which card.

The surface must be enabled on the server (`CAO_AGUI_ENABLED=true` or
`CAO_MCP_APPS_ENABLED=true` — the two surfaces share one event source). When it
is disabled, `emit_ui` returns `{"ok": false, "reason": "AG-UI surface disabled…"}`
— treat that as a no-op, not an error.

## Safety model (why this is always safe to call)

You may emit **only** a closed allow-list of named components with JSON props.
There is **no HTML, no script, no `eval`, no iframe**. The intent is validated
**server-side** against the allow-list before it reaches the stream:

- An **off-list** component (e.g. `iframe`, `script`) is **refused** — the tool
  raises a `ValueError`; nothing is rendered.
- `props` must be **JSON-serializable** and are **bounded to 8 KB** — an oversized
  or non-serializable payload is **rejected** at the `emit_ui` boundary (HTTP 400,
  the tool raises a `ValueError`), so a bad payload never reaches the bus.
- If the AG-UI surface is disabled on the server, the tool **degrades gracefully**
  (no error) — so calling it is never fatal.
- The AG-UI stream is **metadata-only by contract**: never put message bodies,
  credentials, or file contents in props. Reference paths, not contents.

## The tool

```
emit_ui(component: str, props: dict) -> {"ok", "event_id", "component"}
```

`component` must be one of: `approval_card`, `choice_prompt`, `diff_summary`,
`progress`, `metric`, `agent_card`.

## When to use which component

Props below are what a conformant client renderer will display; unknown extra keys
are ignored, not refused.

| Component | Use it when… | Props |
|---|---|---|
| `approval_card` | you need a human to approve/reject a risky action before you proceed | `title` (str), `detail` (str, optional), `risk` (`"low"`/`"medium"`/`"high"`, optional) |
| `choice_prompt` | you want the operator to pick among options | `question` (str), `choices` (list of `{"label", "value"}` or plain strings) |
| `diff_summary` | you changed files and want a compact review | `title` (str), `files` (list of `{"path", "additions", "deletions"}`) |
| `progress` | a long step is running | `label` (str), `value` (0.0–1.0; omit for an indeterminate bar) |
| `metric` | you want to surface a single number | `label` (str), `value` (str/number), `unit` (str, optional) |
| `agent_card` | you want to advertise your identity/status in the fleet view | `name` (str), `provider` (str), `status` (str, optional) |

## Examples

```python
# Gate a risky action on human approval.
emit_ui("approval_card", {
    "title": "Deploy to production?",
    "detail": "3 files changed, 1 DB migration",
    "risk": "high",
})

# Ask the operator to choose.
emit_ui("choice_prompt", {
    "question": "Which base branch?",
    "choices": [{"label": "main", "value": "main"},
                {"label": "release", "value": "release"}],
})

# Summarize a change set.
emit_ui("diff_summary", {
    "title": "Refactor auth",
    "files": [{"path": "security/auth.py", "additions": 74, "deletions": 3}],
})

# Show progress / a metric / your identity.
emit_ui("progress", {"label": "Indexing repository", "value": 0.42})
emit_ui("metric", {"label": "tokens used", "value": 12840, "unit": "tok"})
emit_ui("agent_card", {"name": "reviewer", "provider": "claude_code", "status": "working"})
```

## L2 constructs (Phase 2)

The AG-UI surface also exposes **L2 constructs** — higher-level projections that
fold the raw event stream into structured views. As an agent you don't author L2
constructs, but you should know they exist because your `emit_ui` intents feed
them:

- **`SupervisorDashboardStream`** — folds `STATE_SNAPSHOT`/`STATE_DELTA` + your
  `agent_card` emits into a live fleet hierarchy view.
- **`MultiAgentSessionTimeline`** — reconstructs delegation/message timeline
  from `TOOL_CALL` lifecycle events.
- **`AgentHandoffWithApproval`** — the full interrupt lifecycle: provider prompt
  → reason classification → interrupt → approve/deny/edit → delivery.
- **`CrossProviderStateSync`** — convergence proof across providers.

The **run plane** (`POST /agui/v1/run`) streams these as stock AG-UI wire frames.
Interrupts (approval prompts) route through `POST /agui/v1/interrupts/{id}/resume`.

For details: [references/l2-constructs.md](references/l2-constructs.md) and
[references/run-plane.md](references/run-plane.md).

## Gotchas

1. **Emitting to a disabled surface** — if `CAO_AGUI_ENABLED` is unset, `emit_ui`
   returns `{"ok": false}` gracefully. Don't treat this as an error or retry — it's
   a no-op by design. The fix: always check `ok` in the return but never fail on it.

2. **Props over 8 KB are rejected** — the tool raises a `ValueError` and nothing
   renders. The fix: reference file paths instead of embedding content. Keep props
   to metadata (paths, counts, labels).

3. **No HTML sink exists** — strings in props render as plain text. Attempting to
   smuggle markup through props (e.g. `<script>`, `<iframe>`) won't render and
   looks broken. The fix: use structured props, not markup.

4. **One intent per meaningful moment** — emitting a `progress` card on every
   token or tool call floods the stream and degrades client rendering. The fix:
   emit at milestones (start, 25%, 50%, 75%, done) or once per logical phase.

5. **`approval_card` is display-only today** — it gives the operator an
   approve/reject affordance in the dashboard, but the action routes to the
   dashboard's command surface, not back to you. The fix: pair it with your
   provider's own wait-for-input mechanism (e.g. Kiro's trust prompts, Claude
   Code's permission dialog).

6. **Off-list components are refused server-side** — the allow-list is fixed
   (`approval_card`, `choice_prompt`, `diff_summary`, `progress`, `metric`,
   `agent_card`). A typo or new component name returns HTTP 400. The fix: use
   only the six listed names; check spelling.

## Verifying locally

```bash
# 1. Server with the surface on
CAO_AGUI_ENABLED=true uv run cao-server

# 2. Watch the stream (SSE frames print as they arrive)
curl -N 'http://localhost:9889/agui/v1/stream'

# 3. Emit from anywhere (the MCP tool does exactly this)
curl -sX POST http://localhost:9889/agui/v1/emit_ui \
  -H 'Content-Type: application/json' \
  -d '{"component":"progress","props":{"label":"demo","value":0.5}}'
```

A `GENERATIVE_UI` frame with your component appears on the stream; an off-list
component is refused with HTTP 400.

## See also

- `examples/ag-ui/ag-ui-dashboard/` — a runnable demo (`run.sh` + `showcase.sh`) that
  drives all six components live and shows the off-list refusal.
- `docs/agui.md` — the AG-UI stream and generative-UI reference.
- **`cao-mcp-apps` skill** — operate and extend the MCP Apps surface that renders
  your `emit_ui` intents inside host dashboards (Claude Desktop, VS Code, etc.).
- **`mcp-apps-builder` skill** — build new MCP App views that consume the AG-UI
  stream your emits feed into.
