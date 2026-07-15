# Authoring `cao workflow` scripts

This guide is for anyone writing a Python script to run under
`cao workflow run --script <path>` (the "script tier" — a full Python
program driving one or more agent steps, as opposed to the declarative YAML
tier). It covers the `cao_workflow` shim's contract, when to reach for a
script instead of YAML, the determinism obligation resume relies on, and the
resume boundary itself.

## The contract: `run_step` and `emit_output`

Your script's entire interface to the CAO server is the `cao_workflow`
package — a thin, stdlib-only client library (`urllib` transport, zero
`cli_agent_orchestrator` imports) that runs *inside your script's own
process*, not inside the server.

```python
from cao_workflow import run_step, emit_output

handle = run_step("kiro_cli", "reviewer", "review this diff")
print(handle.output)   # handle.terminal_id, handle.status also available

emit_output({"reviewed": True})
```

- **`run_step(provider, agent, prompt, *, step_id=None, timeout=None, **opts) -> StepHandle`**
  runs one agent step through the same shared substrate the server's own
  handoff path uses. It resolves your run's identity from the environment,
  posts to `/terminals/run-step`, and returns a `StepHandle` with
  `.step_id`, `.terminal_id`, `.output`, `.status`.
- **`emit_output(value)`** prints the run-level `CAO_WORKFLOW_OUTPUT:{json}`
  sentinel line the server scans once your script exits — a one-line
  convenience so you never hand-format the prefix/JSON encoding yourself.
  It is pure `print()`; no HTTP call, no state.
- **Errors are typed and never retried:** `ShimIdentityError` (identity env
  missing — nothing was attempted), `ShimTransportError` (network failure,
  wraps the underlying `urllib` error), `ShimHTTPError` (non-2xx response,
  carries `.status`/`.body`). All four (`ShimError` plus these three
  subclasses) are importable from `cao_workflow` directly:
  `from cao_workflow import run_step, ShimHTTPError`.
- **`step_id` is required for concurrent fan-out.** If you call `run_step`
  from more than one thread (e.g. via `concurrent.futures`), pass an
  explicit, stable `step_id` per call. See "Fan-out and `step_id`" below —
  this is not optional ergonomics, it is a correctness requirement.
- **`reuse_terminal_id` is not supported through `run_step`.** The shim
  always sends identity `env_vars`, and the server unconditionally rejects
  `env_vars` + `reuse_terminal_id` together (422) — `run_step` fails fast
  client-side with a `ShimError` instead of round-tripping an opaque 422. If
  you genuinely need terminal reuse without the identity fence, call the
  HTTP API directly.

## YAML vs. script: which tier should this workflow use?

Use the **YAML tier** (`cao workflow validate`/`list`/`get`/`run` against a
declarative spec) when your workflow is a fixed sequence or simple
branch/loop expressible in the YAML grammar — it is simpler to author,
lint, and reason about, and it is the tier most of CAO's tooling assumes by
default.

Use a **script** (this guide) when your workflow's control flow needs
something YAML can't express yet: nontrivial branching logic, real
concurrent fan-out (`concurrent.futures`/`threading`), or per-iteration
Python computation over agent output. A script is a full Python program —
more power, more responsibility (see the determinism obligation next).

If you're unsure, start with YAML. Reach for a script only when you hit a
concrete limitation.

## The determinism obligation (and repeated work on resume)

`run_step` never retries, backs off, or reconnects. A client-side retry after
an unknown-completion-state transport failure could issue the same agent work
twice, so failures are returned to your script unchanged.

This puts an obligation on **your script**: its control flow and the
prompts it sends must be deterministic across runs of the *same* script
source. If your script's behavior can vary run-to-run — a `random()` call
that isn't seeded, a prompt that embeds `datetime.now()`, branching on
wall-clock time — a **resume** can repeat different work from the original
attempt. The current runtime does not replay completed calls or reject that
divergence: it re-executes the frozen script from the top, including every
`run_step` call. Determinism makes that repeated work predictable, but your
workflow must tolerate it being issued again.

## Fan-out and `step_id`

`run_step`'s step key defaults to a lock-guarded sequential counter
(`call-1`, `call-2`, ...) when you omit `step_id`. That counter is
**race-free** under concurrent callers — the lock guarantees no two calls
ever get the same key — but it is **not safe for fan-out**. Thread
scheduling, not the counter's correctness, decides which call claims which
`call-N`, so two runs of the same fan-out script can assign `call-1`/
`call-2` to different logical calls depending on how the OS scheduled your
threads that run. On resume, that reassignment makes journal history and
diagnostics refer to different logical calls under the same `call-N` key.

**The rule:** any time you call `run_step` from more than one thread
(`concurrent.futures.ThreadPoolExecutor`, manual `threading.Thread`), pass
an explicit, stable `step_id` per call:

```python
def _run_shard(shard):
    return run_step("kiro_cli", "reviewer", f"review {shard}", step_id=f"shard-{shard}")

with ThreadPoolExecutor(max_workers=3) as pool:
    futures = [pool.submit(_run_shard, s) for s in ("alpha", "beta", "gamma")]
```

The linter (`cao workflow validate`) does not infer concurrency or check this
rule. It reports syntax errors, disallowed or unverifiable dynamic imports,
and imports associated with nondeterminism. Supplying stable `step_id` values
for fan-out is the author's responsibility.

## Resume re-executes the frozen script

`run_step()` behaves **identically** whether the surrounding script is a
fresh run or a resume drive. There is no `if resuming:` branch inside the
shim. `cao workflow resume` re-executes the frozen source snapshot from the
top with a new generation token, and each `run_step` call makes a new HTTP
request that executes the agent step again. Completed calls are journaled,
and a replay lookup primitive exists in the journal layer, but it is not
connected to the run-step route and cannot currently suppress repeated work.

Design scripts so repeated steps are acceptable, or add application-level
idempotency around external side effects. `CAO_WORKFLOW_RESUME=1` is present
in the resumed subprocess environment for code that must distinguish the
drive, but the `cao_workflow` shim itself does not branch on it.

## The `reuse_terminal_id` 422 trap

If you pass `reuse_terminal_id` through `run_step(..., reuse_terminal_id=...)`,
you'll get a `ShimError` immediately, before any network call:

```
ShimError: reuse_terminal_id is not supported by run_step() — the shim
always sends env_vars (RUN_ID/GENERATION/STEP_ID), and the server rejects
env_vars + reuse_terminal_id together (422). Omit reuse_terminal_id, or
call the HTTP API directly if you need to reuse a terminal without the
identity fence.
```

This isn't a shim bug or an arbitrary restriction: `run_step` **always**
populates the identity `env_vars` fence (`CAO_WORKFLOW_RUN_ID`/
`GENERATION`/`STEP_ID`), and the server's own request validator
unconditionally rejects any request carrying both `env_vars` and
`reuse_terminal_id` — the combination can never legitimately round-trip.
Passing it through `**opts` would always produce an opaque 422; the shim
fails fast instead so the mutual exclusivity is visible immediately. If you
need terminal reuse without the identity fence, that's a case for calling
`/terminals/run-step` directly over HTTP, outside the shim.

## Examples

Each example under [`docs/examples/`](examples/) demonstrates one of these
patterns end-to-end, with a matching e2e test proving it runs:

- [`loop_example.py`](examples/loop_example.py) — sequential loop, default
  `step_id` counter.
- [`conditional_example.py`](examples/conditional_example.py) — branching
  control flow, explicit `step_id` per branch.
- [`fanout_example.py`](examples/fanout_example.py) — concurrent fan-out via
  `ThreadPoolExecutor`, explicit `step_id` per shard (the fan-out rule
  above, applied).
- [`loop_raw_http_example.py`](examples/loop_raw_http_example.py) — the
  SAME loop shape with **no** `cao_workflow` import at all, using raw
  `urllib` directly against the identity env vars `cao workflow run`
  injects. Proves the shim is a convenience, not a requirement — a script
  is free to skip it entirely and talk to `/terminals/run-step` on its own.
