# CAO Workflows

A **workflow** is a saved, multi-step agent pipeline you author once and run on demand.
It drives one or more agent *steps* — fan work out across agents, collect their
results, and resume a run that was interrupted from a durable journal.

There are two authoring tiers:

- **Python script tier** (recommended, full power) — a `.py` program that drives agent
  steps through the `cao_workflow` shim. This is the **primary authoring path**: it
  supports real branching, concurrent fan-out, per-iteration Python over agent output,
  and parameterized inputs. The [`cao-workflow` skill](../skills/cao-workflow/SKILL.md)
  teaches it. (The old declarative `workflow-author` YAML skill is **retired**.)
- **YAML tier** (simpler, more limited) — a declarative spec for a fixed sequence. It is
  easier to author and lint, but its `parallel` / `pipeline` / `loop` modes are
  **reserved and not yet executable** in the current build (they validate as
  `pass_reserved`, they do not run). Reach for it only for a plain sequential spec; for
  anything with real control flow, write a script.

When in doubt, write a script.

For the shim-contract deep-dive (`run_step`/`emit_output`, retry/determinism, the
`reuse_terminal_id` trap), see
[docs/workflow-scripts-authoring-guide.md](workflow-scripts-authoring-guide.md).

## Quick start

Write a small script to the workflows directory, validate it, and run it.

```python
# ~/.aws/cli-agent-orchestrator/workflows/hello.py
from cao_workflow import run_step, emit_output

# Step 1 — a developer writes a note.
note = run_step("claude_code", "developer", "Write a one-line hello note. Return it only.")

# Step 2 — a reviewer critiques it (read-only role: it READS and RETURNS).
review = run_step("claude_code", "reviewer", f"Critique this note in one line: {note.output}")

emit_output({"note": note.output, "review": review.output})
```

```bash
# validate is mandatory — fix every finding before running
cao workflow validate ~/.aws/cli-agent-orchestrator/workflows/hello.py

# run it by its stem, with a pre-announced run-id
cao workflow run hello --run-id hello-1
```

The workflow is run **by its stem** (`hello`), so the filename must be a bare name with
no path separators, and you must not create a same-stem `hello.yaml` sibling — it would
collide on the run surface.

## The lifecycle

Every workflow follows the same path. No step is optional.

1. **Author** — write the `.py` file to `~/.aws/cli-agent-orchestrator/workflows/<name>.py`.
2. **Validate (mandatory gate)** — `cao workflow validate <path>`. Findings are
   **load-bearing**, not style nits:
   - **`import cli_agent_orchestrator` is banned.** The script runs in a separate
     subprocess and must reach CAO only over HTTP through the `cao_workflow` shim.
     Importing the server package breaks that boundary and fails validation.
   - **`random` / `time` / `datetime` / `uuid` warnings.** Resume **re-executes the
     script top-to-bottom** and replays journaled step results. Any nondeterministic
     value at module top level differs on replay and raises `ReplayDivergenceError`.
     Derive IDs from inputs, not from the clock or an RNG. (See the authoring guide for
     why there is no retry.)
3. **Run** — with an explicit, pre-announced `--run-id` so it can be cancelled.
   **Workflows are NEVER auto-run by an agent.** The user approves each run.
4. **Status / cancel / resume** — `cao workflow status <run-id>`,
   `cao workflow cancel <run-id>`, `cao workflow resume <run-id>`.

A validate that reports `valid` (status `pass` or `pass_reserved`) exits 0; a failing
spec exits 1 and lists each error.

## Parameterized workflows (inputs)

Instead of editing a constant per run, declare inputs once and pass values at invocation
time — this is what makes a workflow reusable as a **tool**: author once, invoke with
different inputs.

A workflow declares a **module-level `INPUTS` dict** and reads the resolved values at
runtime with `get_inputs()`:

```python
# ~/.aws/cli-agent-orchestrator/workflows/summarize.py
from cao_workflow import run_step, emit_output, get_inputs

INPUTS = {
    "target_file": {"type": "path", "required": True},
    "max_points":  {"type": "int",  "required": False, "default": 3},
    "verbose":     {"type": "bool", "required": False, "default": False},
}

inputs = get_inputs()                      # {} when nothing was declared; never raises
target_file = inputs["target_file"]        # canonicalized absolute path
max_points = inputs.get("max_points", 3)

review = run_step(
    "claude_code", "reviewer",
    f"Summarize {target_file} in {max_points} bullet points. Return the summary only.",
)
emit_output({"summary": review.output})
```

Run it with `--input key=value` (repeatable):

```bash
cao workflow validate ~/.aws/cli-agent-orchestrator/workflows/summarize.py
cao workflow run summarize --run-id sum-1 \
  --input target_file=/abs/path/to/report.md \
  --input max_points=5 \
  --input verbose=true
```

Each `INPUTS` entry declares a `type` (`string` | `int` | `bool` | `path`), whether it
is `required`, and an optional `default`. At run start, before any step or terminal is
created, values are **validated against the declaration** — an undeclared key, a
wrong-typed value, or a missing required input is a clear error (400) and nothing runs.
`path`-typed inputs are canonicalized through CAO's shared path validator (realpath +
blocked-dir rejection). The resolved map is **capped at 32 KiB** and is **journaled and
replayed verbatim on resume**, so a resumed run sees byte-identical inputs (deterministic).

The CLI coerces `--input` values ergonomically — `true`/`false` → bool, a bare integer →
int, everything else stays a string — but the engine still validates the coerced value
against the declared type, so a mismatch surfaces as an error rather than running with
the wrong value.

## Running: blocking vs. background

A run **blocks until the workflow finishes** — the `cao workflow run` command (and the
`workflow_run` MCP tool) wait for the whole run. Choose the invocation by how it's
triggered, because the two paths have very different client-side ceilings:

- **`cao workflow run` (CLI)** uses a client socket timeout of **~8820s (~2.45h)**, so the
  CLI itself won't give up early on a long run.
- **`workflow_run` MCP tool (from inside an agent session)** is bounded instead by the
  **MCP host's own per-tool-call timeout** — a host-dependent limit (anywhere from tens of
  seconds to a few minutes, depending on the host) that can **drop a long blocking call and
  lose its return value even though the server run keeps going**.

So:

- **Short runs**: run blocking and read the result directly.
- **Long runs**: **background the run** and poll, rather than blocking on it —

  ```bash
  cao workflow run <name> --run-id <id> --json &
  cao workflow status <id> --json
  ```

  Backgrounding keeps the run alive server-side without a short MCP host timeout silently
  dropping the return value.

Always **pre-announce the run-id** before starting, so you (or the user) can
`cao workflow status <id>` and `cao workflow cancel <id>`.

## Fan-out (concurrency)

To run steps concurrently, use a `concurrent.futures.ThreadPoolExecutor` and give
**every concurrent `run_step` an explicit, stable `step_id`**:

```python
from concurrent.futures import ThreadPoolExecutor
from cao_workflow import run_step, emit_output, ShimError

def summarize(name):
    try:
        h = run_step("claude_code", "reviewer",
                     f"Summarize {name}. Return the summary only.",
                     step_id=f"summarize:{name}")   # STABLE, explicit step_id (required)
        return name, h.output
    except ShimError as exc:                          # per-unit tolerance
        return name, f"ERROR: {exc}"

items = sorted(some_items)                            # sorted() → stable item→step_id map
with ThreadPoolExecutor(max_workers=2) as pool:       # 2 is a good default for claude_code
    results = dict(pool.map(summarize, items))
emit_output(results)
```

Why these rules:

- **Explicit `step_id` is required for fan-out.** The default `call-N` counter is
  race-*free* but **not deterministic across runs** under concurrent scheduling — thread
  timing decides which call claims which `call-N`, so a resume would replay the wrong
  results and raise `ReplayDivergenceError`. `validate` warns when it sees executor use
  without a `step_id`; treat the warning as load-bearing.
- **`sorted()` your inputs** so the item→`step_id` mapping is stable across runs.
- **`max_workers=2` is a sensible default for `claude_code`** (measured: higher values
  starved the heaviest step). Tune it — expose it as an input — when steps are light.

See [`docs/examples/fanout_example.py`](examples/fanout_example.py) for the pattern
end-to-end.

## Operational tips

- **Secrets are references, never literal inputs.** Inputs are journaled in plaintext and
  replayed on resume. Pass a *name* (env-var name, secret id) and resolve the actual
  secret inside the step, not as a `--input`.
- **Match the step to the agent's capability.** A **read-only role** (e.g. `reviewer`)
  told to *write* a file will hang the full step budget waiting on a permission it can't
  get. Read-only steps must READ their inputs and RETURN findings inline. Only
  write-capable roles (e.g. `developer`) should be told to write files.
- **Write big outputs to files, return the path.** Don't return megabytes inline — have
  the step write to disk and return the path.
- **Prefer a headless provider (`claude_code`).** `kiro_cli` currently hangs on an
  interactive prompt from a workflow step (a fix is planned); until it lands, use a
  headless provider.

## Resume

`cao workflow resume <run-id>` re-drives an interrupted run: it replays already-completed
steps from the durable journal and re-runs only the rest. Your script never checks "am I
resuming?" — it re-executes from the top, and the server transparently returns journaled
results for calls that already completed. A **deterministic** script (see Validate)
resumes cleanly with no code change; a nondeterministic one surfaces
`ReplayDivergenceError`.

## CLI reference

All eight verbs live under `cao workflow`.

| Verb | Flags | Description |
| --- | --- | --- |
| `validate <file>` | `--json` | Validate a spec file without running it. Exit 0 valid, 1 invalid. |
| `list` | `--dir <path>`, `--json` | List indexed workflows (rebuilt from spec files on disk). Script-tier rows show `-` for step count. |
| `get <name>` | `--json` | Show the parsed/validated spec for a name or file path. |
| `delete <name>` | `--yes` / `-y` | Delete a workflow's spec file and index row (prompts unless `--yes`). |
| `run <name_or_path>` | `--input k=v` (repeatable), `--run-id <id>`, `--json` | Run a workflow to completion (blocks). Exit 0 completed, 1 failed/cancelled. |
| `status <run_id>` | `--json` | Point-in-time status snapshot for a run. |
| `resume <run_id>` | `--json` | Resume a crashed/failed run from its journal (blocks). |
| `cancel <run_id>` | — | Cooperatively cancel a running workflow. |

## See also

- [docs/workflow-scripts-authoring-guide.md](workflow-scripts-authoring-guide.md) — the
  shim-contract deep-dive: `run_step`/`emit_output`, the no-retry determinism obligation,
  fan-out and `step_id`, and the `reuse_terminal_id` 422 trap.
- [`docs/examples/`](examples/) — runnable scripts, each with a matching e2e test:
  - [`loop_example.py`](examples/loop_example.py) — sequential loop, default `step_id` counter.
  - [`conditional_example.py`](examples/conditional_example.py) — branching, explicit `step_id` per branch.
  - [`fanout_example.py`](examples/fanout_example.py) — concurrent fan-out via `ThreadPoolExecutor`.
  - [`loop_raw_http_example.py`](examples/loop_raw_http_example.py) — the same loop with no shim, raw `urllib` against the identity env vars.
- [`skills/cao-workflow/SKILL.md`](../skills/cao-workflow/SKILL.md) — the agent-facing skill that teaches this lifecycle.
