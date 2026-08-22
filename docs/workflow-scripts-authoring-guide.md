# Authoring `cao workflow` scripts

This guide is for anyone writing a Python script to run under
`cao workflow run <name>` (the "script tier" — a full Python program driving
one or more agent steps, as opposed to the declarative YAML tier). Scripts
live in `~/.aws/cli-agent-orchestrator/workflows/` and are run **by their
stem**: `<name>.py` there runs as `cao workflow run <name>`. There is no
`--script` flag, and a path outside that directory is rejected.

It covers the `cao_workflow` shim's contract, when to reach for a script
instead of YAML, the determinism obligation resume relies on, how to declare
a recovery policy, and the resume boundary itself.

## The contract: `step`, `run_step` and `emit_output`

Your script's entire interface to the CAO server is the `cao_workflow`
package — a thin, stdlib-only client library (`urllib` transport, zero
`cli_agent_orchestrator` imports) that runs *inside your script's own
process*, not inside the server.

```python
from cao_workflow import step, emit_output

handle = step("kiro_cli", "reviewer", "review this diff", recovery="idempotent")
print(handle.output)   # handle.terminal_id, handle.status, handle.replayed also available

emit_output({"reviewed": True})
```

- **`step(provider, agent, prompt, *, recovery, step_id=None, timeout=None, **opts) -> StepHandle`**
  runs one agent step **and declares what re-running it would mean**.
  `recovery` is keyword-only with no default, so omitting it is a plain
  `TypeError` at the call site, and `cao workflow validate` reports it as the
  blocking `missing-recovery-policy` error. See "Declaring a recovery policy"
  below — the value is a claim you are making, not a protection you are
  getting.
- **`run_step(provider, agent, prompt, *, step_id=None, timeout=None, **opts) -> StepHandle`**
  is the same call **declaring no policy**, and that is the only difference
  between the two. Both run the step through the same shared substrate the
  server's own handoff path uses: they resolve your run's identity from the
  environment, post to `/terminals/run-step`, and return a `StepHandle`.
  **A `recovery=` passed to `run_step` is validated by the server but not by the
  shim** — see the recovery section for when a bad value is caught.
- **`StepHandle` has five fields:** `.step_id`, `.terminal_id`, `.output`,
  `.status`, and `.replayed`. **`.replayed` qualifies `.terminal_id`.** When it
  is `True` the server returned a stored result and ran nothing at all, and
  `.terminal_id` is the ORIGINAL id — it names a terminal that **no longer
  exists**. That flag is the only thing standing between you and reading,
  writing to, or waiting on a dead id, so check it before you touch
  `.terminal_id`. It defaults to `False`, so a `StepHandle` you construct
  yourself in a test keeps working unchanged.
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
- **`step_id` is required for concurrent fan-out.** If you call `step` or
  `run_step` from more than one thread (e.g. via `concurrent.futures`), pass an
  explicit, stable `step_id` per call. See "Fan-out and `step_id`" below —
  this is not optional ergonomics, it is a correctness requirement.
- **`reuse_terminal_id` is not supported through either surface.** The shim
  always sends identity `env_vars`, and the server unconditionally rejects
  `env_vars` + `reuse_terminal_id` together (422) — the call fails fast
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

## The determinism obligation

Neither `step` nor `run_step` retries, backs off, or reconnects. A client-side
retry after an unknown-completion-state transport failure could issue the same
agent work twice, so failures are returned to your script unchanged.

This puts an obligation on **your script**: its control flow and the
prompts it sends must be deterministic across runs of the *same* script
source. If your script's behavior can vary run-to-run — a `random()` call
that isn't seeded, a prompt that embeds `datetime.now()`, branching on
wall-clock time — then on a **resume** its step calls no longer match what
the first attempt recorded, and the run fails with
`ReplayDivergenceError` rather than quietly doing different work.

That is what the linter's `nondeterminism` warning is about, and why it is
load-bearing rather than stylistic: a resume re-executes the script from the
top, so anything computed at module level is computed again. Derive step ids
and prompts from the run's inputs, not from the clock, an RNG, or `uuid4()`.

## Fan-out and `step_id`

The step key of both `step` and `run_step` defaults to a lock-guarded
sequential counter (`call-1`, `call-2`, ...) when you omit `step_id`. That
counter is **race-free** under concurrent callers — the lock guarantees no two
calls ever get the same key — but it is **not safe for fan-out**. Thread
scheduling, not the counter's correctness, decides which call claims which
`call-N`, so two runs of the same fan-out script can assign `call-1`/
`call-2` to different logical calls depending on how the OS scheduled your
threads that run.

On resume that reassignment is a correctness failure, not just a confusing
journal: the server decides each key against what was recorded under it, so a
shard arriving at another shard's key either **diverges** (the run fails with
`ReplayDivergenceError`) or, worse, **replays the wrong shard's stored
result** — a result your script has no way to tell apart from its own.

**The rule:** any time you call `step` or `run_step` from more than one thread
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
imports associated with nondeterminism, and the three recovery-policy rules
(see above: `missing-recovery-policy`, `unverifiable-recovery-policy`,
`unenforced-recovery-policy`). Supplying stable `step_id` values for fan-out
is the author's responsibility.

## Declaring a recovery policy

`recovery=` is **your claim about the step, and nothing more.** CAO has no
mechanism to prove what a step does to the outside world, and is not required
to. **A recovery policy DECLARES what re-running this step would mean; it
never grants permission.** Nothing in CAO verifies the claim, before or after
you make it — the resume gate simply acts on your word.

Read the three values as sentences *you* are asserting:

| Value | What you are asserting |
| --- | --- |
| `"idempotent"` | re-running this step has the same effect as running it once |
| `"reconcile"` | re-running it needs a reconciliation step first (**deferred** — today the gate treats it exactly like `idempotent`) |
| `"manual"` | do not decide this one without me — halt and ask |

**`"idempotent"` grants nothing and protects nothing.** It does not make a step
safe to re-run, and it does not ask CAO to make it safe. It tells the resume
gate that *you* believe the step already is safe — and wherever the gate would
otherwise stop and ask a human, it re-executes the step on your word instead.
Declare it on a step that charges a card, sends mail, files a ticket, or
appends to a ledger and CAO will do that again, exactly as instructed. If you
cannot show the step is safe to repeat, `"manual"` is the honest declaration —
it costs you one prompt on resume and nothing else.

Omitting a policy is a **fourth, distinct state**, never silently read as
`"manual"`. Use `run_step` when you mean it: an undeclared step still replays,
because replay executes nothing, but wherever the alternative would be
re-execution it halts for a human instead.

### `recovery=` on `run_step` is checked late, not never

`run_step` has no `recovery` parameter, so a `recovery=` you pass it is
swallowed by `**opts` and forwarded to the server as an ordinary body field.
The server stores it and the resume gate honours it, and the route's request
model types that field as the closed policy enum — **so an unknown value is
rejected with a `422`.** The value is validated; what `run_step` does not do is
validate it *early*.

`step()` checks the value against the closed set **before any HTTP attempt** and
raises `ShimError` on the spot. `run_step` has no such check, so a typo survives
until the request reaches the server and then fails **that step, mid-run** — after
whatever earlier steps have already run and had their effects.

```python
step("kiro_cli", "reviewer", prompt, recovery="idempotant")      # ShimError, before any HTTP
run_step("kiro_cli", "reviewer", prompt, recovery="idempotant")  # 422 from the server, mid-run
```

Neither surface has its *value* checked by the linter — `cao workflow validate`
sees the keyword, not what is in it. It flags the `run_step` form as
`unenforced-recovery-policy` (a warning) for exactly that reason. Use `step()`
to declare a policy, and `run_step` only to declare none. For the same reason,
`step(..., **opts)` with the policy hidden inside `opts` cannot be verified
statically either, and validates as `unverifiable-recovery-policy` — pass
`recovery=` explicitly if you want it seen.

## Resume re-executes the script, and the server decides each step

`step()` and `run_step()` behave **identically** whether the surrounding script
is a fresh run or a resume drive. There is no `if resuming:` branch inside the
shim, and you must not write one. `cao workflow resume` re-executes the frozen
source snapshot **from the top** with a new generation token, and every step
call makes a real HTTP request; what changes is what the *server* does with
that request. Each call lands on one of three outcomes:

| Outcome | What happens |
| --- | --- |
| **replayed** | the journaled result is returned and **nothing runs**. `StepHandle.replayed` is `True`, and its `.terminal_id` names a terminal that no longer exists |
| **executed** | the step runs again for real |
| **halted** | the server will not decide alone: the run stops at that step and waits for a human |

A fourth outcome ends the run rather than one step: if the script changed at a
step's key, that step **diverges** and the run fails with
`ReplayDivergenceError`.

Replay is decided from the step's *execution-affecting* inputs — the provider,
the agent profile name, the prompt, and the other fields that determine what
actually runs. Change one of them at the same step key and the step diverges
rather than replaying. Only the agent profile's **name** counts, not its
contents: edit the profile and the step still replays under its old
definition.

So a resume no longer repeats completed work by default, and the old advice to
"design for repeated steps" is now a fallback rather than the rule. What you
still owe: determinism (above), a truthful `recovery=` on anything with an
external effect, and a `replayed` check before you touch `terminal_id`.
`CAO_WORKFLOW_RESUME=1` is present in the resumed subprocess environment for
code that must distinguish the drive, but the `cao_workflow` shim itself does
not branch on it.

### Resolving a halt

A step halts when its outcome is genuinely unknown or unverifiable: it was
dispatched and never settled and no declared policy permits re-execution; its
stored result is unreadable; its recorded provenance cannot be verified under
the current scheme; or its author declared `recovery="manual"` and asked to see
it.

Inside your script a halt arrives as a `ShimHTTPError` with `.status == 409`,
whose `.body` names `kind: "decision_required"`, the `step_id`, and which
condition fired. Resolve it by naming a decision per halted step and resuming
again:

```bash
cao workflow resume <run-id> --decide <step_id>=rerun   # re-execute that step
cao workflow resume <run-id> --decide <step_id>=skip    # accept its stored result
```

`--decide` is repeatable, one per halted step. Over MCP, `workflow_resume`
takes the same thing as a `decisions` map, `{step_id: "rerun"|"skip"}`.

**A decision authorises exactly ONE attempt.** If that attempt crashes before
it settles, the next resume asks again rather than re-executing on the old
consent. **Consent does not persist across resumes** — one `rerun` is never
standing authorisation for a later one, and must never be presented to a user
as though it were.

A divergence arrives the same way, as a `409` with `kind: "diverged"`, but it
has a different remedy: there is no decision to make, because the fix is to
look at what changed in the script at that step key.

**Do not let a blanket `except ShimError` swallow either one.**
`ShimHTTPError` is a `ShimError`, so a catch-all written for per-unit timeout
tolerance also absorbs a `409` — and the run then finishes with a sentinel
standing in for a result a human was supposed to decide on. Re-raise when
`.status == 409`:

```python
try:
    handle = step("kiro_cli", "reviewer", prompt, recovery="idempotent")
except ShimHTTPError as exc:
    if exc.status == 409:
        raise                      # a halt or a divergence — never absorb it
    return f"ERROR: {exc}"
except ShimError as exc:
    return f"ERROR: {exc}"         # timeout/transport — degrade to a survivor
```

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

The restriction is identical on `step()`, and the message names whichever
function you actually called.

This isn't a shim bug or an arbitrary restriction: both surfaces **always**
populate the identity `env_vars` fence (`CAO_WORKFLOW_RUN_ID`/
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
