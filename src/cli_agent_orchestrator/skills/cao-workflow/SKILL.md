---
name: cao-workflow
description: Author and run CAO Python workflow scripts — multi-step, parameterized, fan-out
  orchestrations executed by `cao workflow run`. Use when the user wants a repeatable multi-step
  job (e.g. data analysis over many files, a review pipeline, a parameterized batch). Authoring
  ends at a validated script file; running it is a separate, user-approved step.
---

# CAO Workflows

A CAO workflow is a **Python script** you write, validate, and — only after asking the user —
run through `cao workflow run`. Each script drives one or more agent *steps* through CAO's
shared substrate, so you can fan work out across agents, collect their results, and resume a
run that was interrupted.

> Your job as an author ends at a **validated script file on disk**. Authoring does NOT run the
> workflow. Never claim a workflow ran, or will run, when all you did was write it. Running is a
> separate step the user must approve (see Lifecycle step c).

## When to use

Reach for this skill when the user asks to **build or run a multi-step or parameterized
workflow** — for example:

- "Analyze every file in `reports/` and summarize the findings."
- "Run a review pipeline: implement, then review, then verify."
- "Do the same batch job but with a different input directory each time."

If the work is a single one-off agent call, you don't need a workflow. Workflows earn their
keep when there are multiple steps, fan-out, parameterization, or a need to resume.

## The script API

Author scripts import from the `cao_workflow` package. This package runs **only in the script
subprocess** and imports nothing from `cli_agent_orchestrator.*` — it talks to CAO over HTTP.
Its public surface:

- `run_step(provider, agent, prompt, *, step_id=None, timeout=None, **opts) -> StepHandle` —
  run one agent step. `StepHandle` has `.step_id`, `.terminal_id`, `.output`, `.status`.
- `get_inputs() -> dict` — the run's resolved inputs (see Parameterized workflows). Returns
  `{}` when nothing was declared; never raises on absence.
- `emit_output(value)` — print the run-level `CAO_WORKFLOW_OUTPUT:` sentinel (the run's return).
- `ShimError` (and `ShimIdentityError`, `ShimTransportError`, `ShimHTTPError`) — the failure
  hierarchy `run_step` raises. Failures surface **unchanged** — the shim never retries.

## Lifecycle

Follow every step in order. **No step may be skipped** — validate is mandatory, and you must
ask before running.

### a. AUTHOR

Write a `.py` file to `~/.aws/cli-agent-orchestrator/workflows/<name>.py`. The workflow is
**run by its stem** (`<name>`), so:

- The name must be a bare stem — **no path separators**, no directory prefix.
- Do **not** create a same-stem `.yaml` sibling — a `<name>.yaml` next to `<name>.py` collides
  on the run surface.

### b. VALIDATE (mandatory gate)

```
cao workflow validate ~/.aws/cli-agent-orchestrator/workflows/<name>.py
```

Fix **every** finding before proceeding — the lint findings are **load-bearing**, not style
nits:

- **`import cli_agent_orchestrator` is banned.** The script runs in a separate subprocess and
  must reach CAO only over HTTP (the `cao_workflow` shim). Importing the server package breaks
  that boundary.
- **`random` / `time` / `datetime` / `uuid` warnings.** Resume **re-executes the script
  top-to-bottom** and replays journaled step results. Any nondeterministic value computed at
  the top level will differ on replay and raise `ReplayDivergenceError`. Keep the script
  deterministic: derive IDs from inputs, not from the clock or an RNG.

### c. ASK the user — NEVER auto-run

The script tier executes generated Python. **Never run a workflow without the user's explicit
approval.** Present the validated file and ask before doing anything in step d.

### d. RUN with an explicit, pre-announced run-id

Announce the run-id before you start so the user can cancel it:
"Starting run `kb-1` — cancel with `cao workflow cancel kb-1`."

Choose the invocation by how the run is triggered, because the two paths have very
different client-side ceilings:

- **`cao workflow run` (CLI)** uses a client socket timeout of **~8820s (~2.45h)** — the CLI
  itself won't give up early.
- **`workflow_run` MCP tool** is bounded by the **MCP host's own per-tool-call timeout** — a
  host-dependent, much-shorter limit that can **drop a long blocking call and lose its return
  value even though the server run keeps going**.

So:

- **Short runs**: call the `workflow_run` MCP tool (blocking) and read the result directly.
- **Long runs**: background the run and poll, rather than blocking on it —
  ```
  cao workflow run <name> --run-id <id> --json &
  ```
  Backgrounding keeps the run alive server-side without a short MCP host timeout silently
  dropping the return.

### e. RESUME

```
cao workflow resume <run-id>
```

Resume replays completed steps from the journal and continues from the first incomplete one.
Deterministic scripts (see step b) resume clean; nondeterministic ones diverge.

## Parameterized workflows

Instead of editing a constant per run, declare inputs once and pass values at invocation time.

Add a **module-level `INPUTS` dict** and read the resolved values at runtime with
`get_inputs()`:

```python
from cao_workflow import get_inputs

INPUTS = {
    "target_dir": {"type": "path", "required": True},
    "max_files":  {"type": "int",  "required": False, "default": 20},
    "verbose":    {"type": "bool", "required": False, "default": False},
}

inputs = get_inputs()
target_dir = inputs["target_dir"]
max_files = inputs.get("max_files", 20)
```

Each entry declares `type` (`string` | `int` | `bool` | `path`), `required`, and an optional
`default`. This makes one authored script reusable — "author once, invoke with inputs."

## Operational discipline

These rules are load-bearing. Each is paired with the reason it exists.

### R1 — Fan-out determinism

To run steps concurrently, use a `ThreadPoolExecutor` and give **every concurrent `run_step` an
explicit, stable `step_id`**. The sequential `call-N` counter fallback is race-free but *not*
deterministic across runs under concurrent scheduling — so resume would replay the wrong
results. Iterate over `sorted()` inputs so the mapping from item → step_id is stable.

Default `max_workers=2` for `claude_code` (measured: 4 starved the heaviest lens). Expose it as
a tunable input; higher values are fine when steps are light.

### R2 — Secrets as references, never literals

Inputs are **journaled in plaintext and replayed on resume**. Never pass a literal secret
(token, key, password) as an input. Pass a **name/reference** and resolve the actual secret at
step time (env var, secrets manager) inside the step.

### R3 — Role-capability matching

Only **write-capable roles** (e.g. `developer`) should be told to write files. A **read-only
role** (e.g. `reviewer`) instructed to write will **hang the full step budget** waiting on a
permission it can't get. Read-only steps must READ their inputs and **RETURN findings inline**.

### R4 — Per-unit fault tolerance

**Catch `ShimError` inside each fan-out unit** so one step's timeout degrades to a survivor set
rather than failing the whole run with a 504. Return a sentinel/`None` for the failed unit and
let the aggregate proceed.

### Big-outputs discipline

For large results, have the step **write to a file and return the path** — don't return
megabytes inline. Per-step output is `null` for schema-less steps; the files (and the aggregate
you build) are the source of truth.

### R5 (INTERIM) — Prefer a headless provider

Prefer **`claude_code`** as the step provider. `kiro_cli` currently launches an interactive TUI
that hangs `run_step`. **This is interim guidance** — a kiro mitigation is a tracked follow-up,
not a permanent verdict — but until it lands, use a headless provider.

### Projection ranking

The **runtime journal is the primary truth** for progress and UI — it reflects what actually
ran. A static script→YAML preview is **optional and lossy**; never treat it as the truth source
and never author against it.

## Handoff when you're read-only

If you lack write permission (you can't create the `.py` file), **hand off authoring to a
`developer` agent**, and pass this skill's name (`cao-workflow`) in the handoff message so the
developer follows the same lifecycle.

## Honesty discipline

- Never claim a workflow ran that didn't.
- Authoring ends at a **validated file**; running is a separate, user-approved step.
- Be honest about failures — surface `ShimError`s and non-zero validate findings; don't paper
  over them.

## Worked example — parameterized fan-out

A script that summarizes each file in a directory concurrently, with a stable `step_id` per
file, per-unit fault tolerance, and results written to disk:

```python
"""summarize_dir — fan out a summary step over every file in target_dir."""
import os
from concurrent.futures import ThreadPoolExecutor

from cao_workflow import run_step, emit_output, get_inputs, ShimError

# Parameterized: author once, invoke with different inputs.
INPUTS = {
    "target_dir":  {"type": "path", "required": True},
    "max_workers": {"type": "int",  "required": False, "default": 2},
}

inputs = get_inputs()
target_dir = inputs["target_dir"]
max_workers = inputs.get("max_workers", 2)

# sorted() → the item→step_id mapping is stable across runs (R1 determinism).
files = sorted(
    name for name in os.listdir(target_dir)
    if os.path.isfile(os.path.join(target_dir, name))
)


def summarize(filename: str):
    path = os.path.join(target_dir, filename)
    try:
        # Explicit, STABLE step_id per concurrent call (R1). Read-only role
        # RETURNS its summary inline (R3) — it does not write files.
        handle = run_step(
            provider="claude_code",          # headless (R5)
            agent="reviewer",
            prompt=f"Summarize the file at {path} in 3 bullet points. Return the summary only.",
            step_id=f"summarize:{filename}",
        )
        return filename, handle.output
    except ShimError as exc:
        # Per-unit tolerance (R4): one timeout degrades to a survivor, not a 504.
        return filename, f"ERROR: {exc}"


with ThreadPoolExecutor(max_workers=max_workers) as pool:
    results = dict(pool.map(summarize, files))

# Big output → write to a file, return the path (big-outputs discipline).
out_path = os.path.join(target_dir, "_summaries.json")
with open(out_path, "w") as fh:
    import json
    json.dump(results, fh, indent=2)

emit_output({"summarized": len(results), "output_file": out_path})
```

Validate it, ask the user, then run with a pre-announced run-id:

```
cao workflow validate ~/.aws/cli-agent-orchestrator/workflows/summarize_dir.py
# fix findings, then — after the user approves:
cao workflow run summarize_dir --run-id sum-1 --json &
```
