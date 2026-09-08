# Workflow Example

A focused gallery example for the `cao workflow` lifecycle: a parameterized Python
script that runs a sequential step and then a concurrent fan-out, submitted as a
durable, resumable, cancellable run. It is intentionally small — one script, three
concurrent checks — so the lifecycle stays the point, not the payload.

For the shim contract itself (`run_step`/`emit_output`, determinism, fan-out
`step_id` rules) see [docs/workflow-scripts-authoring-guide.md](../../docs/workflow-scripts-authoring-guide.md).
For the full CLI/MCP reference see [docs/workflows.md](../../docs/workflows.md). This
README only covers what's specific to this example.

## Files

- [`workflow.py`](workflow.py) — the workflow. Declares typed `INPUTS`, reads them
  via `get_inputs()`, runs one sequential `run_step` (a review plan), then fans out
  three more `run_step` calls concurrently (`style`/`security`/`performance` checks)
  via `ThreadPoolExecutor`, and calls `emit_output()` with a structured result.
- [`fixtures/inputs.json`](fixtures/inputs.json) — the demo input values for reference.
  `cao workflow run` takes inputs only as repeated `--input k=v` flags, so the commands
  below and `run.sh` mirror these values by hand rather than reading this file.
- [`run.sh`](run.sh) — non-interactive entry point: copies `workflow.py` into the CAO
  workflows directory, validates it, then runs it and exits with the run's own exit
  code. Needs a real `cao-server` and an authenticated `claude_code` CLI.
- [`tests/`](tests/) — `test_workflow_example.py` (deterministic, no real server or
  provider — a fake `/terminals/run-step` HTTP server stands in) and
  `test_workflow_live.py` (gated, needs both; skipped by default).

## What it demonstrates

1. **Typed inputs** — `target` (string, required), `concurrency` (int, default `2`),
   `strict` (bool, default `false`).
2. **Sequential then concurrent** — one `run_step` plan call, then a
   `ThreadPoolExecutor` fan-out over a fixed check list, each with an explicit,
   pairwise-distinct `step_id` derived from `target` and the check name
   (`check-<target>-<check>`, with `target` put through `_slug` to satisfy both
   halves of what `/terminals/run-step` requires: the `[A-Za-z0-9_-]` charset and
   the 1-64 character length) — the shape used in
   [`docs/examples/fanout_example.py`](../../docs/examples/fanout_example.py).
   A `target` short enough to fit is only charset-mapped, so it keeps a readable
   `step_id`. One long enough to overflow the budget is truncated and given a
   short digest of the original, because bare truncation would map every long
   `target` sharing a prefix onto a single `step_id` — and a reused `step_id` in a
   fan-out is a correctness bug rather than a cosmetic one.
3. **Per-unit fault tolerance** — each fan-out call is wrapped in its own
   `try`/`except ShimHTTPError`; one failing check is dropped into
   `failed_checks` and the run still completes with the survivors.
4. **A conservative concurrency ceiling** — `max_workers` is
   `min(requested_concurrency, 2)`. Passing `--input concurrency=3` does **not**
   raise the actual worker count above the declared default of `2` — it can only
   lower it. This mirrors the `claude_code` fan-out guidance in
   [docs/workflows.md](../../docs/workflows.md#fan-out-concurrency) (measured: higher
   values starved the heaviest step).
5. **A structured result** — `emit_output({"target", "strict", "max_workers", "plan",
   "checks", "failed_checks"})`, printed as the `CAO_WORKFLOW_OUTPUT:` sentinel line.

## Prerequisites

- `cao-server` running (`cao-server` in one terminal).
- The `claude_code` CLI installed and authenticated — the example's steps use
  `provider="claude_code", agent="reviewer"` (a built-in profile; nothing to
  install). `claude_code` is used deliberately instead of `kiro_cli`: `kiro_cli`
  currently hangs on an interactive prompt inside a workflow step (see the
  "Operational tips" in [docs/workflows.md](../../docs/workflows.md)).

## Author and validate

Workflows are addressed by file, and a relative path resolves against the
**configured workflows directory** (`~/.aws/cli-agent-orchestrator/workflows`, or
`$CAO_HOME_DIR/workflows` if set) — never the shell's cwd. Copy the example there
first, exactly as [docs/workflows.md](../../docs/workflows.md)'s own quick start
does for every workflow:

```bash
mkdir -p ~/.aws/cli-agent-orchestrator/workflows
cp examples/workflow/workflow.py ~/.aws/cli-agent-orchestrator/workflows/workflow.py

cao workflow validate ~/.aws/cli-agent-orchestrator/workflows/workflow.py
```

`validate` never runs the script or creates a terminal — it is a pure static check
(disallowed/dynamic imports, nondeterminism warnings) and exits `0` for this example
(`pass`) before any run is ever submitted.

## Run, observe, cancel

```bash
# Submit + follow to a terminal state. Prints the run id immediately, then polls.
cao workflow run workflow --run-id demo-1 --input target=myapp --input concurrency=3

# From another terminal while it's running:
cao workflow status demo-1

# Cooperatively cancel it:
cao workflow cancel demo-1
```

`--run-id demo-1` is pre-announced deliberately — the run id is retained from the
moment it's printed, so `status`/`wait`/`result`/`cancel` can all address it, whether
the run is still going, finished, or the terminal that started it is gone.

`events`/`wait`/`result` apply too, for the same run:

```bash
cao workflow events demo-1 --no-follow   # one-shot batch read of ordered progress
cao workflow wait demo-1                 # (re-)follow an already-submitted run
cao workflow result demo-1 --json        # the retained result assembled from the journal
```

## Output shapes

`cao workflow run` **submits asynchronously and follows** by default — Ctrl-C
detaches, it never cancels. Three invocations, three `--json` shapes (see
[docs/workflows.md](../../docs/workflows.md#running-submit-and-follow-detach-or-block)
for the complete table):

| Invocation | `--json` shape |
| --- | --- |
| `cao workflow run workflow --run-id demo-1 ...` (default) | `{run_id, state}` — the narrow terminal object |
| `cao workflow run workflow --run-id demo-1 ... --detach` | the 202 submit body: `{run_id, state, links}` |
| `cao workflow run workflow --run-id demo-1 ... --wait` | the complete `WorkflowRunResult` (`steps[]`, `output`, `warnings`, ...) |

Only the `--wait` path carries the run-level `output` `emit_output()` produced.
Run-level output is not journaled: `status`'s snapshot model has no `output` field
at all, and `result` (`/workflows/runs/{id}/result`) explicitly drops the key from
its response rather than advertise a field it can never populate — so a plain
(non-`--wait`) `run`, `status`, and `result` all omit it. Per-step `output` is
unaffected and always present on `steps[]`.

## `cao workflow` vs. `cao schedule`

They solve different problems:

- **`cao workflow`** (this example) runs **one on-demand, durable, resumable
  execution** of a script or spec, addressed by a run id — `status`/`cancel`/
  `resume`/`result` all operate on that one run's journal row.
- **`cao schedule`** (see [`examples/flow/`](../flow/) and
  [docs/flows.md](../../docs/flows.md)) registers a markdown-defined agent session
  to fire on a **cron schedule** via `cao-server`'s scheduler. There is no run id, no
  resumable journal, and no per-run status/cancel — it's "run this agent prompt
  every morning at 7:30," not "run this pipeline and let me track it."

If what you need is "run this multi-step, parameterized pipeline right now and
track it," that's `cao workflow`. If it's "run this prompt on a recurring cadence,"
that's `cao schedule`.

## Resume — current behavior

`cao workflow resume demo-1` re-executes the **frozen script from the top** on a new
generation, and every `run_step` call makes a real HTTP request — there is no
`if resuming:` branch in the script. What changes is what the *server* does with each
request. Each call lands on one of three outcomes: **replayed** (the journaled result
is returned and nothing runs), **executed** (the step runs again for real), or
**halted** (the server stops the run at that step and waits for a human). A fourth
outcome ends the run rather than one step: if the script changed at a step's key, that
step diverges and the run fails with `ReplayDivergenceError`.

So a resume does **not** repeat completed work by default. What that means for this
example specifically: it calls only `run_step`, never `step(...)`, so every step here
is **undeclared** — it has no `recovery=` policy. An undeclared step still replays,
because replay executes nothing; but wherever the alternative would be re-execution,
it **halts for a human** instead of silently running again. Concretely, if a check was
dispatched but never settled before the run died, resuming stops there rather than
re-dispatching it.

One trap worth knowing when you copy this example: a replayed `StepHandle` has
`.replayed == True` and its `.terminal_id` names a terminal that no longer exists, so
check `replayed` before you touch `terminal_id`. See
"Resume re-executes the script, and the server decides each step" in
[docs/workflow-scripts-authoring-guide.md](../../docs/workflow-scripts-authoring-guide.md#resume-re-executes-the-script-and-the-server-decides-each-step)
for the full explanation.

## Tests

```bash
# Deterministic — fake /terminals/run-step server, no real cao-server/tmux/provider.
uv run pytest --no-cov -p no:cacheprovider -q examples/workflow/tests/test_workflow_example.py

# Gated live-provider test — needs a real cao-server + authenticated claude_code.
# Skipped by default; opt in explicitly:
CAO_RUN_LIVE_PROVIDER_TESTS=1 uv run pytest --no-cov -q examples/workflow/tests/test_workflow_live.py
```

`test_workflow_example.py` adapts the `_RunStepFakeHandler` pattern from
[`test/e2e/examples/test_examples_gallery_e2e.py`](../../test/e2e/examples/test_examples_gallery_e2e.py),
with one deliberate difference: this fake also emits `replayed`, because the shim reads
`data["replayed"]` by direct indexing and raises `KeyError` without it. Otherwise the
approach is the same:
a minimal stdlib `http.server` stands in for `/terminals/run-step`, so the real
`cao_workflow` HTTP transport and the real `run_script_workflow` subprocess engine
are exercised without a live tmux-backed `cao-server` or provider credentials. It
asserts: `validate` passes lint; a missing `target` is rejected by `_validate_inputs`
before any worker/step exists; concurrent fan-out calls carry pairwise-distinct
`step_id`s; one forced-failing check is dropped into `failed_checks` without losing
the other survivors; and the `CAO_WORKFLOW_OUTPUT:` sentinel line is on the spawned
subprocess's real stdout.

## Manual run

```bash
./run.sh myapp demo-1 3
```

## See also

- [docs/workflows.md](../../docs/workflows.md) — the full lifecycle, CLI/MCP
  reference, and fan-out guidance.
- [docs/workflow-scripts-authoring-guide.md](../../docs/workflow-scripts-authoring-guide.md) —
  the shim contract, the determinism obligation, and the resume boundary.
- [`skills/cao-workflow/SKILL.md`](../../skills/cao-workflow/SKILL.md) — the
  agent-facing authoring skill (this example follows its R1/R3/R4/R5 rules).
- [`docs/examples/fanout_example.py`](../../docs/examples/fanout_example.py) — the
  minimal fan-out shape this example builds on.
