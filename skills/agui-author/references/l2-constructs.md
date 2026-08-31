# AG-UI L2 Construct Library Reference

L2 constructs are higher-level projections that fold the raw AG-UI event stream
into structured, queryable views. They live in `services/agui/` and all subclass
`AguiConstruct`.

## Architecture

```
Raw events (L1 stream) → L2 constructs (fold/project) → Dashboard views
                                                       → Run plane frames
                                                       → Interrupt lifecycle
```

All constructs:
- Use a single read/write seam (no direct I/O)
- Follow apply-else-drop error isolation (a bad event never crashes the fold)
- Dedup via `BoundedSeen` (cap 10k, oldest-half eviction)
- Are privacy-preserving: metadata only, never message bodies

## SupervisorDashboardStream

**Purpose:** Folds `STATE_SNAPSHOT` and `STATE_DELTA` events into a live fleet
hierarchy view with supervisor/worker relationships and rolling status.

**Events consumed:**
- `STATE_SNAPSHOT` — full fleet state at a point in time
- `STATE_DELTA` — incremental updates (terminal status changes, new terminals)

**Agent interaction:** Your `agent_card` emits feed this construct. Emitting an
`agent_card` with your name, provider, and status makes you visible in the
dashboard hierarchy.

**Example:** `examples/ag-ui/ag-ui-supervisor-dashboard/`

## MultiAgentSessionTimeline

**Purpose:** Reconstructs the delegation + message timeline from the complete
`TOOL_CALL_START` → `TOOL_CALL_END` lifecycle.

**Events consumed:**
- `TOOL_CALL_START` — an orchestration tool was invoked (handoff, assign, send_message)
- `TOOL_CALL_END` / `TOOL_CALL_RESULT` — the tool completed

**Agent interaction:** Every `handoff` or `assign` you perform generates timeline
entries. The timeline shows delegation chains, parallel work, and message flow.

**Example:** `examples/ag-ui/ag-ui-session-timeline/`

## AgentHandoffWithApproval

**Purpose:** The full human-in-the-loop interrupt lifecycle for approval prompts.

**Flow:**
1. Provider enters `WAITING_USER_ANSWER` state (trust prompt, permission dialog)
2. `ApprovalBridge` detects the state and creates an interrupt
3. Reason is classified (`claude-code:permission_request`, `kiro:trust_prompt`, etc.)
4. An `approval_card` is emitted to the AG-UI stream
5. Operator approves/denies/edits via REST or run-plane resume
6. Decision is delivered to the terminal (keystroke via tmux)

**Resume surfaces:**
- REST: `POST /agui/v1/interrupts/{id}/resume` with `{"decision": "approve"|"deny"|"edit", "edited_text": "..."}`
- Run plane: `resume[]` mapping in the `POST /agui/v1/run` SSE stream

**Concurrency model (PR #485 hardening):**
- Per-interrupt delivery task (`_deliver_and_commit`), shielded from cancellation
- Per-terminal lock prevents keystroke interleaving across interrupts
- Delivery failure → `DeliveryError` → REST 502 `{retryable: true}` / run-plane `RUN_ERROR`
- Idempotent re-resume joins the existing delivery task

**Example:** `examples/ag-ui/ag-ui-handoff-approval/`

## CrossProviderStateSync

**Purpose:** Convergence proof: folding the snapshot+delta stream across multiple
providers yields a state deep-equal to `build_dashboard_snapshot`.

**Validated across:** `kiro_cli`, `claude_code`, `codex`

**Agent interaction:** Transparent — ensures your `emit_ui` intents render
identically regardless of which provider you run on.

**Example:** `examples/ag-ui/ag-ui-cross-provider-sync/`

## Common Patterns for Agents

### Feeding the dashboard
```python
# Make yourself visible in the fleet view
emit_ui("agent_card", {"name": "reviewer", "provider": "kiro_cli", "status": "reviewing"})

# Update your status as work progresses
emit_ui("agent_card", {"name": "reviewer", "provider": "kiro_cli", "status": "complete"})
```

### Feeding the timeline
Orchestration tool calls (`handoff`, `assign`, `send_message`) automatically
generate timeline entries. No explicit `emit_ui` needed.

### Working with approvals
If your action triggers an approval prompt (e.g. a destructive operation), emit
an `approval_card` to give the operator context:
```python
emit_ui("approval_card", {
    "title": "Delete production table?",
    "detail": "DROP TABLE users (142,000 rows)",
    "risk": "high"
})
```
The approval lifecycle is handled by `AgentHandoffWithApproval` — you just emit
the card to provide context.
