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

Write a small script as a **draft file anywhere you like**, install it with
`cao workflow create` (which validates it before writing), and run it. You never write into
the workflows directory yourself:
`create` sends the draft's **text** to the server, which decides where the spec lands
through the guarded write path described in [The lifecycle](#the-lifecycle).

```python
# ./hello.py — a draft; its own path and name are yours and are not kept
from cao_workflow import run_step, emit_output

# Step 1 — a developer writes a note.
note = run_step("claude_code", "developer", "Write a one-line hello note. Return it only.")

# Step 2 — a reviewer critiques it (read-only role: it READS and RETURNS).
review = run_step("claude_code", "reviewer", f"Critique this note in one line: {note.output}")

emit_output({"note": note.output, "review": review.output})
```

```bash
# create validates the draft, installs it under the name you give it, and prints the content_hash
cao workflow create hello --from-file ./hello.py

# run it by its stem, with a pre-announced run-id
cao workflow run hello --run-id hello-1
# -> refused: 403 approval_required, carrying the plan_id. This is by design — a plan
#    identifier does not exist until a run computes it. Present the plan_id to the user
#    and stop; approval is a human act (see Plan approval below).

# the USER approves at their terminal, then you run again
cao workflow approve plan-v1:<digest-from-the-refusal>
cao workflow run hello --run-id hello-1
```

The workflow is run **by its stem** (`hello`), so the name you pass to `create` must be a
bare name with no path separators — the draft file's own name is irrelevant. A same-stem
cross-tier collision (a `hello.yaml` beside the `hello.py` this creates) is refused by
`create` **before anything is written**. Writing the file into the workflows directory
yourself remains possible and is an explicitly acknowledged alternative with real costs —
see step 1 of [The lifecycle](#the-lifecycle) before choosing it.

## The lifecycle

Every workflow follows the same path. No step is optional.

1. **Author** — write the script as a **draft anywhere you like**, then install it through
   the guarded write path: `cao workflow create <name> --from-file <draft>` (to replace an
   existing spec: `cao workflow update <name> --from-file <draft> --expected-hash <hash>`,
   with the hash from `get <name>`). The draft's **text** is sent to the server and the
   **server** decides where the spec lands. Every protection lives on that path, and every
   check precedes any write, so a refusal leaves the filesystem byte-identical:
   - **path containment** — the write cannot land outside the spec directory, whatever the
     request asks for;
   - **a source size cap** — enforced before any file is created;
   - **validation + lint before the file exists** — CAO never writes a spec it would refuse
     to run, so a failed `create` wrote nothing;
   - **atomic publication** — `create` publishes a fully written temp inode with an atomic
     link and `update` atomically replaces, so a concurrent reader never sees a partial spec;
   - **a same-stem cross-tier pre-check** — creating `<name>.py` beside an existing
     `<name>.yaml` is refused up front; a raw write would succeed and leave a spec that
     every later `get`/`run` of `<name>` fails to reach with `TierCollisionError` — a file
     unreachable the moment it lands.

   `create` refuses to overwrite an existing spec (use `update`) and prints the new
   `content_hash`, which a later `update` requires.

   Writing the file into `~/.aws/cli-agent-orchestrator/workflows/` yourself is an
   explicitly acknowledged alternative, and it gives up every layer above: no path
   containment, no size cap, no pre-write validation or lint gate, no atomic publication
   (a reader can see the half-written file), and no tier-collision pre-check. It remains
   possible because the directory is yours; it is not the recommended path.
2. **Validate (mandatory gate)** — `create` and `update` validate the draft source
   server-side before writing. They refuse to write a spec with a blocking error. For a spec
   already installed in the workflows directory, `cao workflow validate <path>` runs the same
   linter; a relative `<path>` is resolved against that directory. Findings are **load-bearing**,
   not style nits:
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
   - **Scripts over 131,072 characters (128 KiB) fail validation** with a `syntax` error
     before they are parsed. The limit is a crash guard for CPython 3.10's parser, not a
     style rule; split a script that large into smaller ones. The spec byte budget
     (256 KiB) still applies on top.
3. **Run** — with an explicit, pre-announced `--run-id` so it can be cancelled.
   **Workflows are NEVER auto-run by an agent.** The user approves each run.
4. **Status / cancel / resume** — `cao workflow status <run-id>`,
   `cao workflow cancel <run-id>`, `cao workflow resume <run-id>`.

A validation that reports `valid` (status `pass` or `pass_reserved`) exits 0; a failing
spec exits 1 and lists each error.

## Parameterized workflows (inputs)

Instead of editing a constant per run, declare inputs once and pass values at invocation
time — this is what makes a workflow reusable as a **tool**: author once, invoke with
different inputs.

A workflow declares a **module-level `INPUTS` dict** and reads the resolved values at
runtime with `get_inputs()`:

```python
# ./summarize.py — a draft; `cao workflow create` decides where the spec lands
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

Install the draft and run it with `--input key=value` (repeatable):

```bash
cao workflow create summarize --from-file ./summarize.py
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
and which condition fired; `str(exc)` now shows `(decision_required)` and the message too.

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

## Re-running one step

`cao workflow step <run-id> <step-id>` re-executes **one** step of a recorded run — usually
one that already finished (or failed) — without re-running the workflow. Authoring a long
workflow used to
mean a full re-run per prompt edit — fixing two sentences in step 55 of 57 cost reaching
step 55 again. This verb closes that loop:

```bash
# re-run step 55 exactly as recorded
cao workflow step run-abc123 draft-summary

# re-run it with an edited prompt (from a file, or inline)
cao workflow step run-abc123 draft-summary --prompt-file /tmp/better-prompt.md
cao workflow step run-abc123 draft-summary --prompt-override 'Be terse. {{steps.research.output.notes}}'
```

The step's prompt is resolved from the **recorded** run — its journaled inputs and its
predecessors' outputs — so a step that templates off earlier steps runs with the same
values it saw originally. A replacement prompt replaces the *template*: `{{...}}`
references inside it still resolve against that run, which is what makes an edited prompt
runnable in place.

The source run is **never modified**: no journal row is written, no event is emitted, and
the recorded step's own output stays readable. Replay as many times as it takes. The
resolved prompt is printed alongside the output, because a wrong-looking output is usually
a prompt that resolved differently than expected.

A **still-running** run is fair game too, deliberately: the same immutability is what makes
it safe, so you can iterate on step 12's prompt while the run is still grinding through
step 30. Concurrent replays — of the same step, even — are independent; each one gets its
own private slot for structured output.

Limits: YAML-tier runs only (a script-tier step's inputs come from the script, not from a
spec snapshot), and a step whose predecessor never produced output cannot be replayed —
that fails with the missing reference named rather than running with a blank. An empty
`--prompt-override` (or an empty `--prompt-file`) is rejected rather than run: omit the flag
to reuse the recorded prompt. A replay is not cancellable — one watched step runs to the
step timeout. Exit code is 0 when the step ran, 1 when it failed.

Replays are deliberately absent from the journal and the event stream, which is what keeps
them non-destructive — so they are also invisible to anything reading those as an execution
audit log. Each one is logged at INFO by the server; that is the trail.

## CLI reference

All sixteen verbs live under `cao workflow`.

| Verb | Flags | Description |
| --- | --- | --- |
| `validate <file>` | `--json` | Validate an installed spec without running it. The path is resolved against the workflows directory. Exit 0 valid, 1 invalid. Use `create` or `update` to validate and install a draft. |
| `create <name>` | `--from-file <path>` (required), `--json` | Create a **new** Python spec named `<name>` from a source file. Refuses to overwrite an existing spec — use `update`. Python only; a YAML-tier name is refused with a message naming the restriction. The source is sent as text and the **server** decides where it lands (validation, lint gate, path containment, atomic write, index update all happen server-side). Prints the new `content_hash`, which is what `update` wants next. Exit 0 created, 1 refused/errored. |
| `update <name>` | `--from-file <path>` (required), `--expected-hash <hash>` (required), `--json` | Replace an **existing** spec's source, refusing a stale update. Refuses to create — use `create`. `--expected-hash` is required and is never computed for you: it is your assertion about what you believe you are replacing, and a hash derived from the file about to be overwritten would always match. There is no `--force`. Get the current hash from `get <name>` (the `Hash:` line). |
| `list` | `--dir <path>`, `--json` | List indexed workflows (rebuilt from spec files on disk). Script-tier rows show `-` for step count. |
| `get <name>` | `--json` | Show the parsed/validated spec for a name or file path. |
| `delete <name>` | `--yes` / `-y`, `--json` | Delete a workflow's spec file and index row (prompts unless `--yes`). |
| `run <name_or_path>` | `--input k=v` (repeatable), `--run-id <id>`, `--working-directory <path>`, `--detach`, `--wait`, `--json` | Submit a run and follow it to a terminal state. `--detach` submits and exits; `--wait` blocks inline. Exit 0 completed, 1 failed/cancelled. `--json` emits `{run_id, state}` — see the breaking-change note above. |
| `status <run_id>` | `--json` | Point-in-time status snapshot for a run (full detail, including steps). |
| `runs` | `--state <state>`, `--limit <n>`, `--json` | List recorded runs from the durable journal, newest first. |
| `wait <run_id>` | `--json` | Follow an already-submitted run by polling until terminal. Same exit codes as `run`. |
| `result <run_id>` | `--json` | The complete `WorkflowRunResult` for a run — the full-detail surface `run --json` no longer prints. Answers for an **in-flight** run too (the steps settled so far), not only a finished one, and works for a detached or post-restart run because it is assembled from the journal. |
| `events <run_id>` | `--follow/--no-follow`, `--after-seq <n>`, `--json` | Stream live per-run ordered progress (SSE). `--no-follow` does a one-shot batch read. Requires the events route from issue #504 — on a build without it, both modes report that the stream is unavailable and point at `wait`/`status`, rather than claiming the run is unknown. |
| `resume <run_id>` | `--decide STEP_ID=rerun\|skip` (repeatable), `--json` | Resume a crashed/failed run from its journal (blocks). Each step is replayed, executed, or halted. `--decide` resolves a halted step and authorises **exactly one attempt** — see Halts and `--decide` above. |
| `step <run_id> <step_id>` | `--prompt-file <path>`, `--prompt-override <text>`, `--json` | Re-execute ONE recorded step live (blocks), resolving its prompt from the recorded run. Leaves the run untouched. The two prompt flags are mutually exclusive. YAML tier only. Exit 0 ran, 1 failed. |
| `cancel <run_id>` | — | Cooperatively cancel a running workflow. |
| `approve <plan_id>` | `--json` | Approve a plan identifier so runs of that plan may start. Idempotent — a repeat reports the original approver and timestamp rather than overwriting them. **There is no revoke.** Requires the `cao:admin` scope when auth is enabled. Exit 0 approved (whether newly or already), 1 rejected/unreachable. See [Plan approval](#plan-approval-script-tier) below. |

## MCP tool reference (from inside an agent session)

Fifteen workflow tools are exposed over MCP. Each returns a structured `{ok, ...}` envelope on
every path and never raises into the agent loop.

| Tool | Description |
| --- | --- |
| `workflow_create` | Create a **new** Python spec from source **text** (not a path). Thin client over `POST /workflows`; refuses to overwrite an existing spec. |
| `workflow_update` | Replace an existing spec's source, refusing a stale update. Requires `expected_hash` — the hash you believe the spec currently has. |
| `workflow_get` | Read a spec back, including its source and `content_hash`. FR-10's "inspect" operation: what the workflow currently **is**, not what you last sent. |
| `workflow_validate` | Lint spec source **without creating anything**. Takes text, so a draft can be checked before it exists anywhere — validate, revise, then create. |
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
| Create a spec | `create --from-file <path>` | `workflow_create` (source **text**) |
| Update a spec | `update --from-file <path> --expected-hash` | `workflow_update` (source **text**) |
| Read a spec | `get <name>` | `workflow_get` |
| Validate | `validate <file>` (installed spec) | `workflow_validate` (source **text**, including drafts) |
| List workflow **specs** | `list` | *(none — MCP has no spec-listing tool)* |
| List workflow **runs** | `runs` | `workflow_list` |
| Submit asynchronously | `run` (the default) | `workflow_start` |
| Run inline / blocking | `run --wait` | `workflow_run` |
| **Grant** a plan approval | `approve <plan_id>` | *(none — deliberately)* |
| **Report** a plan's approval | *(none)* | `workflow_plan_approval` |

> **The four authoring pairs line up by name but not by input.** `create` and `update` take a
> **draft file path** on the CLI, while `validate` takes a path to a spec already in the workflows
> directory; all three take **source text** over MCP. That is deliberate — an agent holds a draft in
> context and has nowhere to put a file, whereas a person already has one on disk — but it means the
> CLI flag and the tool argument are not interchangeable. In both directions the server does the
> validating, containment-checking and writing; neither surface writes to the spec directory itself.
>
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

A script plan needs human approval before it can run: an operator approves it
with `cao workflow approve <plan_id>` using the exact identifier from the `403`
refusal, then runs it again. The first run of a new or changed script plan is
refused by design. An agent must present the identifier to the user and stop;
there is no agent-side tool that grants approval.

**Nothing in this section applies to YAML workflows.** Script-tier approval enforcement is active by
default.

For a working directory outside any Git repository, the plan identifier records the directory's canonical path and nothing about its contents.

A script-tier run freezes an **execution manifest** at run start — the workflow's source hash, its
resolved inputs, the repository/worktree baseline, and a digest of the installed agent profile set —
and derives a **plan identifier** (`plan_id`, of the form `plan-v2:<digest>`) from the
execution-affecting fields. Change any of them and the `plan_id` changes, which is the mechanism by
which a changed plan needs its own approval. What the manifest actually persists is the resulting
opaque `plan_id`, not the profile-set digest itself — the digest is computed, folded into `plan_id`,
and discarded; it is never written to the manifest as its own value.

> **The agent profile set is part of plan identity, and the whole set is.** A step's behaviour and tool
> policy come from whichever agent profile it names, resolved from a file outside the script at launch
> time, so editing a profile changes how an approved plan executes. Script-tier steps are discovered by
> executing the Python, so at approval time there is no list of which profiles a run will name — the
> digest therefore covers **every installed profile**, not just the ones a script happens to use. The
> practical consequence: `cao install <anything>`, or editing any profile in your agent store or any
> file `cao install` wrote for a profile (the Kiro agent JSON, the Copilot or OpenCode agent file, the
> shared OpenCode `opencode.json`, the installed context copy), invalidates every existing approval and
> the next run of each plan is refused until re-approved. The packaged built-in profiles are in the set
> too, so a CAO upgrade that changes a bundled profile also moves every `plan_id`.

> **Migration: new runs move to `plan-v2`; approved `plan-v1` resumes still pass.** A `plan_id` computed after
> upgrading always uses the new `plan-v2:` scheme, so the first NEW run of every plan is refused until
> re-approved under its `plan-v2:` identifier — a `plan-v1` approval attested to a plan whose agent
> profiles were never examined, so it cannot satisfy the new scheme. This does **not** reach a run that
> was already frozen and approved before the upgrade and left resumable: resume reuses that run's
> frozen `plan_id` verbatim rather than recomputing it, so an approved `plan-v1` resume still proceeds
> on its original approval. Its per-run launch guard record carries no scheme marker, so each of its
> agent launches is still checked under the rule it was admitted with (the profile text alone, without
> the installed files); new runs record `launch-guard-v2` and are checked under the full rule.
> Re-approve each plan once it is run fresh.

### It is on by default

Approval enforcement is **on by default**.

> **This is a behaviour change, not a fix.** A script-tier plan that ran yesterday under no approval
> is refused today. If you run script-tier workflows non-interactively, approve each plan first with
> `cao workflow approve <plan_id>`, or opt out as shown below. YAML-tier workflows are unaffected.

Nothing is required to enable it. The environment variable still works and is now useful for
re-asserting the default rather than switching it on:

```bash
# the default -- the gate is already on
cao-server

# re-assert it explicitly (for example in a script whose settings.json you do not control)
CAO_WORKFLOW_REQUIRE_APPROVAL=1 cao-server

# invoke the CLI client normally
cao workflow run my-script
```

#### Opting out

Only the server's `settings.json` can turn the gate off:

```json
{
  "workflow": {
    "require_approval": false
  }
}
```

#### Recovering from malformed settings

With `CAO_WORKFLOW_REQUIRE_APPROVAL` unset, unreadable, undecodable, malformed JSON, or invalid
approval settings **fail closed**: the gate remains on. Repair `settings.json` and restart or reload
the CAO server. A valid explicit JSON boolean `workflow.require_approval: false` is the only
settings-file opt-out; `null`, strings, and numbers are invalid and do not disable enforcement.

#### What a refusal looks like

The refusal causes are deliberately distinct, because the operator's next action differs:

| Status | Meaning | What to do |
|---|---|---|
| `403` | The plan's `plan_id` has no approval. | `cao workflow approve <plan_id>`, then run again. |
| `409` | `plan_inputs_changed`: while approval enforcement is on, the run's recorded agent profile or memory setting changed, its launch state is unreadable, approval enforcement was turned on after this run started, or approval settings could not be read. This surfaces from an agent step's `POST /terminals/run-step`, not the initial run attempt. | Start a new run for changed inputs, unreadable launch state, or newly enabled enforcement. If approval settings could not be read, repair `settings.json`, then resume. |
| `503` | CAO could not compute a plan identifier: untracked files exceed 64 MiB or contain unsupported entries, Git is unavailable/unreadable/timed out, files changed during the snapshot, profiles are unreadable, the freeze failed, or an older run has no recorded identity. | Follow the message; retry only when it says the cause was transient. No approval will help. |
| `503` | The approval store could not be read, so CAO cannot determine whether the plan is approved. | Restore database access, then retry. The run remains refused. |

An operator may choose the settings-file [opt-out](#opting-out) as an explicit policy decision. An agent must follow the refusal message and must not disable approval enforcement.

The missing-approval and missing-identity refusal bodies carry the identifier as a **field**, not only in the message
(issue #583 Bolt 3), so a script or agent branches on it rather than parsing prose:

```json
{
  "detail": {
    "kind": "approval_required",
    "plan_id": "plan-v2:9f3c…",
    "message": "Plan 'plan-v2:9f3c…' has not been approved. …"
  }
}
```

`kind` is authoritative and the status mirrors it: `approval_required` → 403, and
`plan_identity_unavailable` → 503 with `plan_id: null`, because in that case there is no identifier to
approve. `cao workflow run` and the `workflow_run` tool both read that field and print — or return — the
exact `approve` command.

Note that a `plan_id` is computed at run start, so the **first run of a new or changed plan is
refused by design** — that is the mechanism by which a changed plan needs its own approval.

An unavailable approval store returns `503` with a string `detail` explaining the database fault.
It does not ask you to approve the plan again; check the server log and restore database access.

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
cao workflow approve plan-v2:<digest>
cao workflow run my-script          # starts
```

Bolt 3 ships the conversational authoring sequence around this loop: create or update the script
(which validates it before writing), present it for review, make the plan-discovery run above, and
surface the refused run's `plan_id` for a human to approve before retrying. The discovery run remains
necessary because the `plan_id` does not exist before run admission computes it.

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
  is that a changed script, changed inputs or a changed repository state cannot execute unnoticed
  under an approval granted for a different plan. It does not bind agent profiles or memory content;
  see [What an approval does not cover](#what-an-approval-does-not-cover).
- **Fail-closed while enabled**: a run whose manifest is missing or unreadable is refused rather than
  admitted.

### Memory is frozen too, and the copy the agent sees is redacted

A script-tier run resolves CAO memory **once**, at its first terminal, and records the content, its source and a
hash of the **full** resolved content into the same manifest. Every later terminal of that run — and every resume,
however long afterwards — is given that recorded copy. **Editing CAO memory after a failure therefore does not
change what a resumed run sees.**
Memory content is not part of plan identity; the memory-enabled setting is recorded at the
approval check while approval enforcement is on, and changing it stops further agent launches for
that run while enforcement remains on.

One consequence is worth stating plainly, because it differs from a non-workflow terminal:

> **On a workflow-driven terminal the injected memory block is the frozen, REDACTED copy**, and it is truncated if
> the manifest's size bound bites (recorded as `memory.truncated`). A terminal you start yourself still receives
> the live, unredacted block. The frozen copy is what makes a replay byte-identical to the original run — and it
> means a secret sitting in curated memory stops reaching the agent's context on this path.

The recorded hash covers the full resolved content, taken **before** redaction, so it identifies what was resolved
rather than what survived the redaction rules of the day.

### What an approval does not cover

An approval binds four things: the script source, its resolved inputs, the state of the run's
working directory when the run is admitted, and the installed agent profile set (every profile's
source text plus the files `cao install` wrote for it, including the shared OpenCode `opencode.json`,
which is bound whole because OpenCode reads the whole file, so an edit to your own key in it also moves
every `plan_id`). For a Git repository the directory state is
the commit and the worktree changes, not the path, so two checkouts in the same state share one
approval. For a directory outside any Git repository it is only the directory's canonical path.

Everything else is read when a step runs, so a new run of an approved plan can do something different
from what was reviewed and still match the same `plan_id`:

- **Agent profile edits within one run.** A profile edit after a run is admitted is caught at the
  next agent launch in that run (`plan_inputs_changed`); the next NEW run gets a new `plan_id` and is
  refused until re-approved. What a profile edit cannot change is a run that already launched its
  steps.
- **Memory content.** CAO memory is resolved at a run's first agent terminal and is not part of the
  `plan_id`. Memory added or edited after approval, including through the `memory_store` tool, reaches
  the next run's steps under the original `plan_id`. Only the memory-enabled setting is checked, and
  only within a run. Tracked in #828.
- **Files in a non-Git working directory.** The `plan_id` records the directory's path, not its
  contents. Files added, changed or removed there after approval are used by the next run under the
  original `plan_id`.
- **Managed `.env` values a profile references.** A profile's `${VAR}` placeholders are resolved from
  the CAO-managed `.env` file (`cao env set`) when the step launches. The placeholder text is bound; the
  value is not, because that file is where credentials live and a digest of the substituted text would
  carry a hash of each credential into an identifier that is logged and displayed. Changing a referenced
  value with `cao env set` does not change the `plan_id`.
- **MCP servers from installed agent plugins, for providers that receive them at launch.** Where
  `cao install` materialises agent-plugin MCP servers into a file the provider reads (the Kiro agent
  JSON, the shared OpenCode `opencode.json`), they ARE bound: an agent-plugin install or uninstall
  rewrites those files for every installed profile, so it moves every `plan_id`. For the providers that
  receive agent-plugin MCP servers at launch instead (`with_plugin_mcp`: Claude Code, Codex, Copilot,
  Kimi, Cursor, Grok, OMP, Antigravity, MiniMax), nothing on disk changes for the profile, so adding or
  removing an agent plugin does not change the `plan_id` there.
- **Kiro launch inputs `cao install` does not write.** The files a Kiro agent's `resources` point at,
  other than the installed context copy, and the global Kiro `mcp.json` when a profile opts in with
  `useLegacyMcpJson` (`includeMcpJson`), are read by Kiro at launch and are not part of the `plan_id`.
- **Installed skills.** The skill catalog under the CAO skills directory is injected into a launching
  agent's context and is not part of the `plan_id`.
- **A same-name profile shadowed in a lower-priority directory.** Only the profile that wins
  resolution, plus the files `cao install` wrote for that name, is bound; a same-named file in a
  directory that loses resolution is not.
- **The reserved ephemeral namespace.** Ephemeral profiles are created by the run after admission and
  are bound on their own path, so they are outside the installed-set digest by design.

Each of these is a launch input the next run reads live. Binding any of them is a separate change from
#827; the `.env` case needs a design that does not leak values, and the others need a product ruling on
whether an operator's agent-plugin or skill change should invalidate every approval on every provider
(today it does so only where `cao install` writes the servers to disk).
- **Repository changes after admission.** The repository state is read once, when the run is
  admitted. Changes made after that, including while a submitted run waits for a free slot, are not
  detected.
- **Agents a step delegates to** with assign or handoff. They run with their current profiles,
  including creator-owned ephemeral agents through that separate claim/launch path. Fresh agent steps
  in an approved script run still refuse reserved ephemeral profiles until their definitions are bound.

With API authentication off, which is the default, any process running as you can change these inputs,
and can also run `cao workflow approve`. The gate makes sure that a changed script, changed inputs or
changed repository state can't run unnoticed under an approval granted for something else. It doesn't
detect changes to the inputs listed above.

1. **Six manifest fields are omitted rather than recorded** — provider, model, profile, permissions,
   limits and retry policy. Script-tier steps are discovered by executing the Python, so those values
   have no run-level existence at freeze time; they are covered transitively by the source hash,
   because changing any of them means editing the script.
   While approval enforcement is on, the profile set and the memory-enabled setting are recorded at
   the approval check; the run refuses to launch further agent steps if either changes while
   enforcement remains on.

> **Stale source-hash rejection now runs** (issue #583 Bolt 3). This section previously listed it as a
> second gap. `cao workflow update` and the `workflow_update` tool both require the `content_hash` you
> last read, and the write is refused if the spec has moved on since — so an edit made from a stale read
> is stopped rather than silently overwriting someone else's. Do not work around it by re-reading the
> hash immediately before writing: a hash read from the file you are about to overwrite always matches,
> which removes the check instead of satisfying it.

## See also

- [docs/workflow-scripts-authoring-guide.md](workflow-scripts-authoring-guide.md) — the
  shim-contract deep-dive: `run_step`/`emit_output`, the no-retry determinism obligation,
  fan-out and `step_id`, and the `reuse_terminal_id` 422 trap.
- [`docs/examples/`](examples/) — runnable scripts, each with a matching e2e test:
  - [`loop_example.py`](examples/loop_example.py) — sequential loop, default `step_id` counter.
  - [`conditional_example.py`](examples/conditional_example.py) — branching, explicit `step_id` per branch.
  - [`fanout_example.py`](examples/fanout_example.py) — concurrent fan-out via `ThreadPoolExecutor`.
  - [`loop_raw_http_example.py`](examples/loop_raw_http_example.py) — the same loop with no shim, raw `urllib` against the identity env vars.
- [`examples/workflows/pr-health/`](../examples/workflows/pr-health/) — a
  deterministic open-PR health scorer with comment-only notifications, an
  epoch-bounded notification lifecycle (fixed grace periods, a 90-day re-entry
  cooldown, and lifetime caps past which decisions are report-only), a
  journal-visible per-PR decision record, and a biweekly scheduled-flow example.
  Its apply mode is gated by plan approval rather than by prompt prose.
- [`skills/cao-workflow/SKILL.md`](../skills/cao-workflow/SKILL.md) — the agent-facing skill that teaches this lifecycle.
