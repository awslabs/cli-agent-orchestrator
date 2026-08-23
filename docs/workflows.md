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
   - **`missing-recovery-policy` is a blocking error.** A `step()` call that declares no
     `recovery=` fails validation. Two sibling warnings do not block:
     `unverifiable-recovery-policy` (a `step()` call passing `**kwargs`, where the linter
     cannot see whether a policy is inside) and `unenforced-recovery-policy` (a
     `recovery=` on `run_step`, which resume honours and the server validates with a
     `422`, but which the shim does not check before sending — so a typo fails that step
     mid-run rather than up front). See the authoring guide's recovery-policy section — a
     policy is a *declaration*, never a permission.
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

## Running: submit-and-follow, detach, or block

`cao workflow run` **submits the run asynchronously and then follows it** — it prints the
run id as soon as the run is durably recorded, then polls until the run reaches a terminal
state, exiting 0 on `completed` and 1 on `failed`/`cancelled`. Because the id is printed
before the run finishes, the run is addressable (`status`, `wait`, `result`, `cancel`) for
its whole life.

Three shapes:

| Invocation | Behavior |
| --- | --- |
| `cao workflow run <name>` | Submit, print the id, follow to terminal. **Ctrl-C detaches** — it never cancels; the run keeps going server-side. |
| `cao workflow run <name> --detach` | Submit, print the id, exit 0 immediately without following. |
| `cao workflow run <name> --wait` | The retained fully-blocking path: hold the socket until the run finishes. |

> **Breaking change (issue #505) — `--json` output shape.** `run --json` previously echoed
> the complete `WorkflowRunResult` (`run_id`, `workflow_name`, `state`, `steps[]`,
> timestamps, `kind`). Because the default path now *follows* rather than blocks, it emits
> only the stable terminal object:
>
> ```json
> { "run_id": "hello-1", "state": "completed" }
> ```
>
> A non-TTY plain `run` (no `--json`) also emits this JSON, so a piped invocation has one
> stable machine format. **Scripts that read `steps[]` or `workflow_name` off `run --json`
> must change**: fetch the full result explicitly with `cao workflow result <id> --json`
> (or `cao workflow status <id> --json` for a mid-run snapshot). Exit codes are unchanged
> and identical across TTY, non-TTY, and `--json`.
>
> **`--wait --json` is the exception**: it still emits the complete `WorkflowRunResult`.
> `--wait` is the retained fully-blocking path, so returning everything in one call is the
> reason to reach for it. Only the default follow path and `--detach` emit the narrow
> `{run_id, state}` object.

So there are two machine shapes, chosen by invocation:

| Invocation | `--json` shape |
| --- | --- |
| `cao workflow run <name>` (default follow) | `{run_id, state}` |
| `cao workflow run <name> --detach` | the 202 body — `{run_id, state, links}` |
| `cao workflow run <name> --wait` | the full `WorkflowRunResult` |
| `cao workflow result <id>` | the full `WorkflowRunResult` |
| `cao workflow status <id>` | a mid-run snapshot |

Note that no run-level `output` field is returned by `result`, `status`, or the
`workflow_result` / `workflow_wait` MCP tools: run-level output is not journaled, so it is
only available from the blocking `run --wait` path. Per-step outputs are always present on
`steps[].output`.

Choose the shape by how the run is triggered, because the client-side ceilings differ:

- **`cao workflow run` (CLI)** follows by polling, so no single request has to survive the
  whole run. `--wait` uses a client socket timeout of **~8820s (~2.45h)**.
- **`workflow_run` MCP tool (from inside an agent session)** blocks, and is bounded by the
  **MCP host's own per-tool-call timeout** — a host-dependent limit (tens of seconds to a
  few minutes) that can **drop a long blocking call and lose its return value even though
  the server run keeps going**. For a long run from an agent, prefer submit + poll:
  `workflow_run` with a pre-announced id, then `workflow_status` / `workflow_wait`.

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

`cao workflow resume <run-id>` re-drives an interrupted run. Your script never checks "am I
resuming?" — it **re-executes from the top**, every time, and the server decides each step
call as it arrives. Each step lands on one of three outcomes:

| Outcome | What happens |
| --- | --- |
| **replayed** | the stored result is returned and **nothing runs**. `StepHandle.replayed` is `True` |
| **executed** | the step runs again for real |
| **halted** | CAO will not decide this one alone: the run stops at that step and waits for a human |

A fourth outcome ends the run rather than one step: if the script changed at a step's key,
that step **diverges** and the run fails with `ReplayDivergenceError`. A **deterministic**
script (see Validate) resumes cleanly with no code change; a nondeterministic one diverges.

> **`replayed` qualifies `terminal_id`.** A replayed step's handle carries the ORIGINAL
> `terminal_id`, and that terminal **no longer exists**. `replayed` is the only thing that
> stops you reading, writing to, or waiting on a dead id — check it before you touch
> `terminal_id`.

Which steps can replay is decided from the step's *execution-affecting* inputs — the
provider, the agent profile name, the prompt, and the other fields that determine what
actually runs. Change one of them at the same step key and the step diverges rather than
replaying. That is why the determinism rules above are load-bearing.

### Halts and `--decide`

A step halts when its outcome is genuinely unknown or unverifiable: it was dispatched and
never settled and no declared recovery policy permits re-execution; its stored result is
unreadable; its recorded provenance cannot be verified under the current scheme; or its
author declared `recovery="manual"` and asked to see it. Inside the script the halt arrives
as a `409` — a `ShimHTTPError` whose body names `kind: "decision_required"`, the `step_id`,
and which condition fired.

Resolve it by naming a decision per halted step and resuming again:

```bash
cao workflow resume <run-id> --decide <step_id>=rerun   # re-execute that step
cao workflow resume <run-id> --decide <step_id>=skip    # accept its stored result
```

`--decide` is repeatable — one per halted step. Over MCP, `workflow_resume` takes the same
thing as a `decisions` map, `{step_id: "rerun"|"skip"}`.

> **A decision authorises exactly ONE attempt.** If that attempt crashes before it settles,
> the next resume asks again rather than re-executing on the old consent. **Consent does not
> persist across resumes** — one `rerun` is never standing authorisation for a later one.

Whether re-running a step is acceptable at all is something *you* declare with `step()`'s
`recovery=` keyword. CAO cannot verify such a claim and does not try: a recovery policy
declares what re-running the step would mean, and never grants permission to do it. See
[the authoring guide](workflow-scripts-authoring-guide.md) before declaring one.

Two warnings for the blanket `except ShimError` in the fan-out pattern above: `ShimHTTPError`
is a `ShimError`, so a catch-all absorbs a halt or a divergence and lets the run finish with
a sentinel where a human decision was required. Re-raise when `.status == 409`.

## CLI reference

All thirteen verbs live under `cao workflow`.

| Verb | Flags | Description |
| --- | --- | --- |
| `validate <file>` | `--json` | Validate a spec file without running it. Exit 0 valid, 1 invalid. |
| `list` | `--dir <path>`, `--json` | List indexed workflows (rebuilt from spec files on disk). Script-tier rows show `-` for step count. |
| `get <name>` | `--json` | Show the parsed/validated spec for a name or file path. |
| `delete <name>` | `--yes` / `-y` | Delete a workflow's spec file and index row (prompts unless `--yes`). |
| `run <name_or_path>` | `--input k=v` (repeatable), `--run-id <id>`, `--detach`, `--wait`, `--json` | Submit a run and follow it to a terminal state. `--detach` submits and exits; `--wait` blocks inline. Exit 0 completed, 1 failed/cancelled. `--json` emits `{run_id, state}` — see the breaking-change note above. |
| `status <run_id>` | `--json` | Point-in-time status snapshot for a run (full detail, including steps). |
| `runs` | `--state <state>`, `--limit <n>`, `--json` | List recorded runs from the durable journal, newest first. |
| `wait <run_id>` | `--json` | Follow an already-submitted run by polling until terminal. Same exit codes as `run`. |
| `result <run_id>` | `--json` | The complete `WorkflowRunResult` for a run — the full-detail surface `run --json` no longer prints. Answers for an **in-flight** run too (the steps settled so far), not only a finished one, and works for a detached or post-restart run because it is assembled from the journal. |
| `events <run_id>` | `--follow/--no-follow`, `--after-seq <n>`, `--json` | Stream live per-run ordered progress (SSE). `--no-follow` does a one-shot batch read. Requires the events route from issue #504 — on a build without it, both modes report that the stream is unavailable and point at `wait`/`status`, rather than claiming the run is unknown. |
| `resume <run_id>` | `--decide STEP_ID=rerun\|skip` (repeatable), `--json` | Resume a crashed/failed run from its journal (blocks). Each step is replayed, executed, or halted. `--decide` resolves a halted step and authorises **exactly one attempt** — see Halts and `--decide` above. |
| `cancel <run_id>` | — | Cooperatively cancel a running workflow. |
| `approve <plan_id>` | `--json` | Approve a plan identifier so runs of that plan may start. Idempotent — a repeat reports the original approver and timestamp rather than overwriting them. **There is no revoke.** Requires the `cao:admin` scope when auth is enabled. Exit 0 approved (whether newly or already), 1 rejected/unreachable. See [Plan approval](#plan-approval-script-tier) below. |

## MCP tool reference (from inside an agent session)

Eleven workflow tools are exposed over MCP. Each returns a structured `{ok, ...}` envelope on
every path and never raises into the agent loop.

| Tool | Description |
| --- | --- |
| `workflow_run` | Run a workflow to completion **inline** (blocking). Bounded by the MCP host's per-tool-call timeout — see the ceiling note above. |
| `workflow_start` | Submit a run **asynchronously**; returns the run id immediately without waiting. |
| `workflow_status` | Point-in-time status snapshot for one run. |
| `workflow_wait` | Poll a submitted run to a terminal state, then return `{ok, run_id, state, kind, steps}`. |
| `workflow_result` | The complete retained result for a run; answerable for a detached or post-restart run. |
| `workflow_list` | List recorded **runs** from the durable journal (not specs). |
| `workflow_events` | Read live per-run ordered progress. Needs the events route from issue #504. |
| `workflow_resume` | Resume a crashed/failed run from its journal. Each step is replayed, executed, or halted; a `decisions` map (`{step_id: "rerun"\|"skip"}`) resolves a halted step and authorises **exactly one attempt**. |
| `workflow_cancel` | Cooperatively cancel a running workflow. |
| `workflow_return` | Called by a worker to hand its structured step output back to the run. |
| `workflow_plan_approval` | **Read-only.** Report a run's plan identifier and whether that plan is approved. There is deliberately **no MCP tool that grants an approval** — see the asymmetry note below. |

### CLI ↔ MCP name mapping

The two surfaces grew separately and their names do **not** line up. Read this table
before assuming a verb and a tool with similar names do the same thing:

| Concept | CLI verb | MCP tool |
| --- | --- | --- |
| List workflow **specs** | `list` | *(none — MCP has no spec-listing tool)* |
| List workflow **runs** | `runs` | `workflow_list` |
| Submit asynchronously | `run` (the default) | `workflow_start` |
| Run inline / blocking | `run --wait` | `workflow_run` |
| **Grant** a plan approval | `approve <plan_id>` | *(none — deliberately)* |
| **Report** a plan's approval | *(none)* | `workflow_plan_approval` |

> **`list` and `workflow_list` are false friends.** The CLI's `list` lists **specs**; the
> MCP `workflow_list` lists **runs**. An agent reaching for "the list tool" expecting specs
> gets runs. The CLI equivalent of `workflow_list` is `cao workflow runs`.
>
> `run` and `workflow_run` are also not equivalent: the CLI's bare `run` submits
> asynchronously and follows, whereas the MCP `workflow_run` blocks inline. The MCP
> counterpart of the CLI default is `workflow_start`.
>
> **Approval is asymmetric on purpose, not by omission.** The CLI can grant an approval and MCP
> cannot; MCP can report one and the CLI has no dedicated verb for that (`cao workflow approve`
> reports the existing record when it finds one). An MCP grant tool would let an agent approve the
> plan it just wrote, which is precisely what the approval gate exists to prevent. An agent that meets
> a refusal should call `workflow_plan_approval` and ask the human to run `cao workflow approve`.

## Plan approval (script tier)

**Nothing in this section applies to YAML workflows**, and nothing in it is active by default.

A script-tier run freezes an **execution manifest** at run start — the workflow's source hash, its
resolved inputs, and the repository/worktree baseline — and derives a **plan identifier** (`plan_id`,
of the form `plan-v1:<digest>`) from the execution-affecting fields. Change any of them and the
`plan_id` changes, which is the mechanism by which a changed plan needs its own approval.

### Enabling it

Approval enforcement is **off by default**. With it off, `plan_id`s are still computed and frozen, and
runs start regardless of whether an approval exists.

```bash
# start the CAO server with approval enforcement enabled
CAO_WORKFLOW_REQUIRE_APPROVAL=1 cao-server

# invoke the CLI client normally
cao workflow run my-script

# alternatively, persist workflow.require_approval = true in the server's settings.json
```

> **The variable configures the server, not the CLI client.** Setting
> `CAO_WORKFLOW_REQUIRE_APPROVAL=1` only on a short-lived `cao workflow run` invocation does not
> configure an already-running CAO server. Set it in the environment that launches that server, or use
> `settings.json`.

> **The environment variable can only turn enforcement ON.** Setting it to `0`/`false` does **not**
> disable a gate that `settings.json` has enabled — only `settings.json` can turn it off. This departs
> from every other CAO setting, where the environment variable wins outright. The reason is that a
> control anything able to set an environment variable could switch off is not a control.

### The first run of any plan is refused

A `plan_id` does not exist until a run starts and computes it, so **a new or changed plan cannot have
been approved yet**. The loop is:

```bash
cao workflow run my-script          # refused; the error carries the plan_id
cao workflow approve plan-v1:<digest>
cao workflow run my-script          # starts
```

This friction is temporary rather than intrinsic — the authoring sequence that presents a plan and
takes approval *before* running is a later piece of work.

### What approval is, and is not

- **Idempotent.** Approving twice changes nothing and reports the original approver and timestamp, so
  "I just approved this" is distinguishable from "this was approved earlier by someone else".
- **Not revocable.** There is deliberately no revoke, update or expiry. An update path could point an
  existing approval at a changed plan, which would let work nobody reviewed execute with a genuine
  approval behind it.
- **Recorded with the local OS account as the approver.** That value is **provenance, not identity** —
  nothing verifies it.
- **A same-user local control, not a privilege boundary.** CAO runs as the invoking user, so a
  determined local script can edit the settings or the approval record directly. What the gate provides
  is that a changed plan cannot execute *unnoticed* under an approval granted for a different plan.
- **Fail-closed while enabled**: a run whose manifest is missing or unreadable is refused rather than
  admitted.

### Memory is frozen too, and the copy the agent sees is redacted

A script-tier run resolves CAO memory **once**, at its first terminal, and records the content, its source and a
hash of the **full** resolved content into the same manifest. Every later terminal of that run — and every resume,
however long afterwards — is given that recorded copy. **Editing CAO memory after a failure therefore does not
change what a resumed run sees.**

One consequence is worth stating plainly, because it differs from a non-workflow terminal:

> **On a workflow-driven terminal the injected memory block is the frozen, REDACTED copy**, and it is truncated if
> the manifest's size bound bites (recorded as `memory.truncated`). A terminal you start yourself still receives
> the live, unredacted block. The frozen copy is what makes a replay byte-identical to the original run — and it
> means a secret sitting in curated memory stops reaching the agent's context on this path.

The recorded hash covers the full resolved content, taken **before** redaction, so it identifies what was resolved
rather than what survived the redaction rules of the day.

### Two things not yet implemented

Stated here because their absence is easy to assume away:

1. **Stale source-hash rejection.** A `plan_id` changes when the source changes, but an update
   presenting a stale expected source hash is **not** rejected. Do not rely on such a check running.
2. **Six manifest fields are omitted rather than recorded** — provider, model, profile, permissions,
   limits and retry policy. Script-tier steps are discovered by executing the Python, so those values
   have no run-level existence at freeze time; they are covered transitively by the source hash,
   because changing any of them means editing the script.

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
