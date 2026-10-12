# Flow — the `cao schedule` Lifecycle

**Primary feature: `cao schedule`** — recurring, cron-based agent sessions managed
through `add` / `list` / `run` / `disable` / `enable` / `remove`.

This example walks the full lifecycle of one scheduled flow, `local-task-demo`: a
harmless local task (append a timestamp to a file) gated by a deterministic script
that can allow or skip a run. It replaces the previous trivia-only sample, which
only showed a flow file — not the lifecycle commands, the gating contract, or cleanup.

## `cao schedule` vs. `cao workflow`

Both are documented in full elsewhere ([docs/flows.md](../../docs/flows.md),
[docs/workflows.md](../../docs/workflows.md)); this example is about `cao schedule`
specifically. They are not interchangeable:

| | `cao schedule` (this example) | `cao workflow` |
|---|---|---|
| Purpose | recurring, cron-driven launch of **one** agent session | a saved, multi-step pipeline across **one or more** agent steps |
| Trigger | automatic on cron (needs `cao-server` running) or manual (`cao schedule run`) | manual (`cao workflow run`) |
| Definition | one Markdown file: frontmatter (`name`/`schedule`/`agent_profile`/`script`) + a prompt | a Python script driving `run_step`/`emit_output` (or a limited YAML sequence) |
| Boundary contract | a gating script returns `{"execute": bool, "output": {...}}`, merged into `[[placeholders]]` | step outputs are consumed directly by the next step in the script |

If you want ad hoc, multi-agent, branching pipelines, use `cao workflow`. If you want
one agent session that runs on a schedule (or on demand), use `cao schedule` — that's
what this example demonstrates.

## Files

- [`local-task.md`](local-task.md) — the flow definition: cron schedule, `agent_profile`,
  and a gating `script`.
- [`gate.sh`](gate.sh) — deterministic gating script. Returns `{"execute": true, "output":
  {...}}` by default, or `{"execute": false, "output": {}}` when a flag file exists —
  both paths are exercised below.
- [`run-lifecycle.sh`](run-lifecycle.sh) — runnable entry point: exercises every
  lifecycle command end to end, isolated from any real CAO state, with cleanup on exit.
- [`test_schedule_lifecycle.py`](test_schedule_lifecycle.py) — focused automated
  coverage (parsing, lifecycle commands, the gating contract, cleanup). No wall-clock
  waits, no live agent provider required.

## Setup

`developer` is a built-in agent profile (ships with CAO), so no install step is
strictly required. To install your own copy first (matches [docs/flows.md](../../docs/flows.md)):

```bash
cao install developer
```

## Run it

```bash
./examples/flow/run-lifecycle.sh
```

This does **not** require `cao-server` to be running: `add` / `list` / `disable` /
`enable` / `remove` only touch the flow database directly, and `cao schedule run`
bootstraps its own event pipeline in-process (see `_run_flow_with_pipeline` in
`cli/commands/schedule.py`). `cao-server` (and a durable, non-isolated `CAO_HOME_DIR`)
only matter for unattended, cron-triggered runs.

## Walkthrough

```bash
# 1. Add the flow from its file
cao schedule add examples/flow/local-task.md
# -> Flow 'local-task-demo' added successfully
#    Schedule: */10 * * * *
#    Agent: developer
#    Next run: <10 minutes from now>

# 2. Inspect it
cao schedule list
# -> shows schedule, agent, last run (Never), next run, enabled=Yes

# 3. Trigger it manually — skip path (deterministic, no session launched)
mkdir -p /tmp/cao-examples-flow && touch /tmp/cao-examples-flow/skip
cao schedule run local-task-demo
# -> Flow 'local-task-demo' skipped (execute=false)
rm /tmp/cao-examples-flow/skip

# 4. Disable it
cao schedule disable local-task-demo
cao schedule list          # enabled=No, next run unchanged (stale until re-enabled)

# 5. Re-enable it — next run is recalculated from now
cao schedule enable local-task-demo

# 6. Trigger it manually — allow path (launches a session)
cao schedule run local-task-demo
# -> Flow 'local-task-demo' executed successfully
tmux attach -t cao-flow-local-task-demo   # observe the agent append the timestamp

# 7. Clean up
cao schedule remove local-task-demo
cao shutdown --session cao-flow-local-task-demo
```

Notes:

- **Generated session name**: manual and scheduled runs both launch (or recycle)
  a tmux session named `cao-flow-<flow-name>` — here, `cao-flow-local-task-demo`.
- **Gating**: `local-task.md`'s `script: ./gate.sh` is resolved relative to the flow
  file, run with a 30s timeout, and its stdout is parsed as the `{"execute", "output"}`
  contract (see `execute_flow` in `services/flow_service.py`). `output` becomes the
  `[[timestamp]]` / `[[log_file]]` values in the prompt template.
- **Observable result**: on the allow path, the agent appends a line containing
  the timestamp to `/tmp/cao-examples-flow/local-task.log` — `cat` it afterwards.

## Testing

```bash
uv run pytest --no-cov examples/flow/test_schedule_lifecycle.py -v
```

This file lives outside `test/` (the project's default pytest `testpaths`), so the
main suite never collects it — run it explicitly, as above.
