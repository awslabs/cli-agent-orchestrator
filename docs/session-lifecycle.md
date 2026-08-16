# Session lifecycle

A CAO session has a declared lifecycle state. The fire marshal fires only on
sessions in **none** of the declared states, which makes this document's real
subject not "what states exist" but **"can a declared state be wrong?"** —
because every declared state is a suppressor for the recovery path.

Status: phased. The lifecycle storage, the HTTP/CLI routes, and the
row-preserving stop/archive collection described here are **implemented**
(by this work). M3-C C1-C2 implements the cohort service described in §4.3, and
C4 adds the **public operator surfaces** described in §4.4: separate safe/force
Pause and Stop, operator-only Resume of a stopped session (paused, or paused
and started), and the durable projection behind all of them. M3-D (§4.5) adds
the task-occurrence seam, the safe drain that produces the receipt those safe
surfaces spend, and the content and delivery of the supervisor reconciliation
wake. The legacy stop route still behaves exactly as documented in §4.0. Fire
Marshal cutover remains deferred.

---

## 1. The model

Four fields, not one enum. Collapsing them loses information the UI and the
marshal both need.

| field | values | meaning |
|---|---|---|
| `lifecycle` | `working` · `complete` · `paused` · `stopped` | what the session is doing |
| `restore_to` | `working` · `complete` · `paused` | what a `stopped` session returns to on resume |
| `archived` | bool | hidden from the main UI; orthogonal to lifecycle |
| `kind` | `campaign` · `service` | how health is judged at all (§6) |

`archived` is a visibility flag rather than a fifth state because archiving a
*complete* session must not lose the fact that it was complete. Archiving forces
a stop and sets `restore_to` to the pre-stop lifecycle, so resuming an archived
complete session returns it to `complete` — where the operator may hand the
supervisor a new goal, which moves it to `working`.

### States

**`working`** — the supervisor holds a goal and is pursuing it. At least one
worker is doing something. *Waiting counts as doing something only under §5.*

**`complete`** — the supervisor has declared the goal achieved. Work stops, the
marshal is suppressed, and `/alert-colin` fires so a human can close the session
out. Complete is a **declaration, not a teardown**: a mistaken `complete` that
tore everything down would destroy the evidence needed to tell it was mistaken.
In practice a supervisor should already have retired its workers by this point,
leaving itself and possibly a memory curator — but that is an expectation, not
an invariant, because there are legitimate edge cases.

**`paused`** — an operator asked for a stop at a safe boundary; the supervisor
settled the fleet and flipped the state. Panes stay live and keep consuming
resources. See §3.

**`stopped`** — every pane is collected, including the supervisor's. Hibernate,
not shutdown: resuming relaunches each worker against its recorded native
session and bumps the supervisor with a resumption notice. See §4.

### Transitions

```
                  ┌──────────────────────────────────────────┐
                  │                                          │
  working ──pause-request──▶ pausing ──all-live-workers-ack──▶ paused
     │                          │  (deadline: NOT a suppressor)  │
     │                          └──expired──▶ marshal domain     │
     │                                                           │
     ├──supervisor declares──▶ complete ──/alert-colin           │
     │                                                           │
     └────────────────────── stop ◀──────────────────────────────┘
                              │
                        (records restore_to)
                              │
                           resume ──▶ restore_to
```

`deleted` is not a state; it is the removal of the session and its history.

---

## 2. Where the state lives, and who owns it

**The CAO database.** One row per session. The dashboard reads and writes it
directly, sessions with no conductor campaign still get a state, and it survives
a conductor state-root wipe. `conduct` becomes a client, exactly as it did for
the issue tracker.

**The supervisor owns the transitions that require judgement** — `complete`, and
the flip into `paused`. An operator *requests* a pause; only the supervisor can
declare the fleet actually settled, because only the supervisor knows whether the
work is at a resumable boundary.

**A declared state is trusted until explicitly changed.** It does not expire and
carries no heartbeat. That is a deliberate choice against the obvious
alternative: a continuous liveness check on a paused supervisor is itself a stall
detector, and two systems that can disagree about whether the same session is
healthy is worse than one system that can be stale.

**Staleness is checked at resume, not continuously.** Resuming a session whose
supervisor is gone puts it into an error state and asks the operator one
question: resume the preserved supervisor session, or start a new one. The
question gets asked exactly once, at the moment somebody is present to answer it.

---

## 3. The pause protocol

1. Operator presses Pause (or runs the CLI equivalent). Session enters
   `pausing`. **The button disables immediately** with "pause requested —
   settling", because settling takes minutes and a button that looks pressable
   invites a second press.
2. The supervisor receives the request and messages every worker.
3. Workers acknowledge.
4. The supervisor flips the session to `paused`. The button becomes Resume and
   re-enables.

**Settling counts live workers only.** There are 15 dead terminals on the live
fleet as of 2026-08-06. If pause required every worker to acknowledge, one dead
pane would block it forever. Dead workers are recorded as unreachable and do not
block the flip.

**`pausing` is not a suppressor and carries its own deadline.** A supervisor that
never settles a pause is precisely the unresponsive-supervisor case the marshal
exists for. On expiry the session returns to the marshal's domain with the
pending pause request as evidence.

---

## 4. Stop, and the resumability check

Stop collects every pane. It is only reversible for providers with a resume path.

CAO already models the honest answer as **two separate booleans** —
`ProviderResumeStatus.identity_available` and `.authority_supported`. Knowing a
session id and being allowed to resume it are different facts.

| resume path | providers |
|---|---|
| yes | `claude`, `codex`, `kimi`, `muse`, `glm` (via `validate_resume_argv` + the `*_native_launch` modules) |
| **no** | `opencode_cli`, `kiro_cli`, `copilot_cli`, `cursor_cli`, `antigravity_cli`, `hermes`, `mock_cli` |

**Stop checks every terminal before collecting anything, and requires explicit
confirmation naming the workers that will not come back** — in the dashboard as a
confirm dialog listing them, and on the CLI as a prompt that `--yes` satisfies.
Proceeding is allowed; proceeding *unknowingly* is not. A button labelled
hibernate must never silently be a one-way door.

Resume relaunches each resumable worker against its recorded native session id
and sends the supervisor a resumption bump.

### 4.0 Stop collects and preserves; deletion forgets

Collection is part of the stop, not a separate act the caller remembers to do
afterwards. `POST /sessions/{name}/lifecycle/stop` (and archive, which forces a
stop) tears the fleet down through the same event-driven teardown `DELETE`
uses — each terminal is snapshotted before its window is killed, so the
recovery artifacts survive — and it leaves behind everything a resume, a
recovery, or an investigation needs:

- the lifecycle row, in `stopped`, with its `restore_to` target preserved
  rather than recomputed on retry;
- the forwarded environment, so a resume relaunches each worker against the
  binary and credentials it ran with;
- the per-terminal snapshots and callback-recovery records.

The order is what makes a partial failure safe. The session-lifecycle claim is
held across the whole operation, so a concurrent create cannot add a window
between the stopped check and the teardown. The create path takes the same
claim for its *own* admission: a new session acquires it before its stopped-name
check and stale-env pre-clear, so a stop that wins the claim first leaves the
racing create to re-read `stopped` under the claim and refuse — zero physical
session, the preserved env untouched. That admission also fails closed on an
unreadable lifecycle store: `describe` returns `working` + `unreadable` for
observational marshal callers, but creation cannot proceed without knowing the
row isn't stopped, so an unreadable result is typed lifecycle unavailability and
the create refuses before any effect. An open callback recovery is refused
*before* anything is written or collected — collecting a terminal mid-recovery
would lose the one-shot refusal the recovery is adjudicating, and a stop
recorded while one is open would be a false state. The `stopped` declaration is
written *before* any pane is collected, so a write or admission failure deletes
nothing, and a fully collected fleet can never be left declared `working`. A
pane that refuses collection mid-stop leaves the row already stopped — a visible
divergence, not a silent one — and a retry re-collects what
remains, idempotently.

That last guarantee holds under concurrency too. Every lifecycle mutation — not
just the stop — takes a per-session *write* claim, and the stop holds it across
admission, the write, and collection. So a `declare(WORKING)` that races the
stop cannot commit over it mid-collection: it waits for the stop's critical
section, then sees `stopped`, and a stopped session cannot be declared live (its
panes are collected), so it leaves with a typed conflict rather than overwriting
the stop. The write claim is keyed by session name alone — the lifecycle module
stays backend- and tmux-free, and reads never take it — and it is named to sort
after the physical session claim and before any terminal-generation claim, which
fixes a single lock order (physical < write < generation) with no inversion.
Optional `expected_epoch` compare-and-swap is unchanged: a conflict stays a
conflict, never blind last-write-wins.

This is deliberately not symmetric with deletion. `DELETE /sessions/{name}`
remains the destructive operation that forgets the lifecycle row and clears the
forwarded env, releasing the name for reuse. A stop never does either, so an
ordinary hibernate can never become an accidental cleanup.

### 4.1 One owner per provider session

Two agents resuming the same native session interleave their turns into one
transcript and corrupt both histories. **CAO already prevents this**, and the
existing guard is stronger than a naive one:

`NativeSessionAttachmentModel` is keyed `(provider, native_session_id)`, so
exactly one owner holds a provider session regardless of how many terminals,
generations or execution modes reference it. `_refuse_live_owner` raises
`NativeAttachmentConflict` on a second acquirer. The owner tuple includes
`execution_mode` specifically so an ACP bridge and a native TUI can never both
hold one session — "refused rather than silently multiplexed." The claim is
written *before* provider launch, so a crash leaves a durable record to
adjudicate rather than a phantom.

Ownership is `(pid, start_marker)`, never a bare pid, because pids are recycled
and a stale one can "forge a survivor — or, worse, forge a *no*-survivor."

Session resume must go through this path for every worker it relaunches. It is
the reason resume is safe at all.

### 4.2 Claims are released, and an unresponsive owner can be adjudicated

Closed by `docs/native-session-claims.md`. Two things were wrong, and the
second was hiding behind the first.

**`release()` had no production caller at all.** Every claim the system ever
took stayed live: 258 rows on the reference install, every one at the same
epoch, not one carrying a release proof. Since `declare()` refuses a live
owner, that made every provider-native session on that install unresumable —
so resume could not have worked even for the providers that support it.
Teardown now closes the claim it opened, and a sweep closes the ones lost to
a server exiting, which runs no teardown at all.

Only a **provably absent pid** releases anything. A live owner is held, and
so is one whose start marker merely disagrees — the marker is naive local
wall-clock, and treating a mismatch as a recycled pid would turn a daylight
-saving rollover into a mass release of running workers.

**Ambiguity stays frozen against automation, and now has a human valve.**
`cao attachment adjudicate` records who decided, what evidence they looked
at, and what the system could still see, under a schema distinct from the
machine proof. Resuming past a live owner stays refused; resuming past an
*unresponsive* one is possible, deliberately, with a name attached.

### 4.3 Dark M3-C cohort service (C1-C2)

The fleet lifecycle needs a durable whole-cohort record before safe/force
effects can be made retryable. C1 adds three additive tables:

- `session_cohort_operations` binds a caller-minted operation ID and canonical
  request digest to the exact declared lifecycle epoch, an opaque SHA-256 of
  the sorted stable-agent ID/revision vector, and a digest of the full member
  snapshot. One exact lifecycle/roster slot has one winner.
- `session_cohort_members` retains every stable agent at the boundary. Agents
  already dormant/retired are visible but marked excluded, so fleet Resume
  cannot resurrect them accidentally. Nullable task-occurrence and boundary
  evidence carriers remain empty until M3-D supplies that authority.
- `session_cohort_transitions` is an append-only state-epoch CAS log. Safe mode
  cannot become force through an ordinary transition; explicit promotion
  requires and preserves a receipt digest.

The closed state vocabulary is `preparing`, `draining-to-boundary`,
`interrupting`, `tearing-down`, `paused`, `stopped`, `restoring`,
`reconciliation-required`, and `settled`. The generic transition function
cannot enter Stop teardown or terminal `paused`/`stopped` states. C2 adds two
paired database primitives instead:

- `begin_stop_teardown` revalidates the exact lifecycle epoch, lifecycle value,
  roster revision, and member snapshot before atomically claiming the M3-B
  barrier and appending the `tearing-down` transition. A still-safe Stop
  requires an opaque M3-D drain receipt. Safe-to-force promotion instead
  requires its own explicit promotion receipt and is recorded in that same
  barrier-paired transition; force Stop never waits for provider I/O here.
- `commit_terminal` accepts an opaque execution receipt, verifies every
  included member has an allowed terminal result, and atomically advances the
  cohort and declared lifecycle to `paused` or `stopped`. Stop additionally
  requires the exact barrier owned by that operation. A failed CAS rolls the
  lifecycle write back with the cohort write; terminal member evidence cannot
  later be rewritten, while exact response-loss replay still adopts. For safe
  Pause, members that were already idle or parked at the boundary are terminal
  alongside members explicitly drained by M3-D; they need no synthetic drain
  transition or needless work.

Caller-owned transactions stay rollback-capable even under SQLite's lazy
transaction behavior. A retry out of `reconciliation-required` still requires
a receipt, and safe-to-force recovery remains a distinct receipted path. C2
intentionally exposes no public mutation route and performs no tmux, provider,
wait-runner, inbox, or conductor effect. Read projections are available at
`GET /cohort-operations/{operation_id}` and
`GET /sessions/{session_name}/cohort-operations` without consulting tmux.

Later M3-C slices consume this service to perform and reconcile each physical
effect, then add operator-only partial Resume. M3-D remains the sole
task-occurrence and supervisor-drain authority; C2 treats its receipt as opaque
and does not infer task meaning from CAO's physical roster.

### 4.4 Operator surfaces and Resume (C4)

**Safe and force are separate surfaces, not a mode argument.** Six routes:

| route | scope | what it does |
|---|---|---|
| `POST /sessions/{name}/cohort/pause/safe` | admin | consumes M3-D's drain receipt + member classification; interrupts nothing |
| `POST /sessions/{name}/cohort/pause/force` | admin | interrupts every turn, workers before the supervisor |
| `POST /sessions/{name}/cohort/stop/safe` | admin | stops once the fleet drained to a boundary |
| `POST /sessions/{name}/cohort/stop/force` | admin | reaps now, without waiting for provider I/O |
| `POST /sessions/{name}/cohort/resume/paused` | admin | restores panes, **sends zero input** |
| `POST /sessions/{name}/cohort/resume/start` | admin | restores panes, then wakes the supervisor exactly once |
| `POST /sessions/{name}/cohort/resume/retry` | admin | continues *this* Resume out of `reconciliation-required` |

A mode flag is something a client library defaults, a script sets once, and a
retry carries forward. A different URL is not. `cao session cohort` mirrors the
split (`stop-safe` / `stop-force`), as does the dashboard, which renders force
as its own button rather than a modifier. Safe Pause had no CLI command or
dashboard button until M3-D existed to produce its evidence; §4.5 describes
what it now takes to reach one, and why it is two steps rather than one.

**Resume is the only thing that reopens a Stop barrier**, and only an operator
performs it. `begin_resume_restore` is `begin_stop_teardown`'s mirror image: it
releases the exact barrier the source Stop claimed and declares the recorded
target in one transaction. The ordering matches Stop's for the same reason —
Stop writes `stopped` before collecting a pane so a failure can never leave a
collected fleet declared live; Resume writes the target before creating one, so
a half-finished restore is a session declared live with missing panes (a
divergence `session_lifecycle.divergence` surfaces) rather than a fleet running
under a `stopped` row that refuses every effect it needs.

**Resume paused sends nothing at all** — no keystroke, no inbox payload, no
supervisor bump. It exists so an operator can look at a restored fleet before
anything moves. Zero input is structural: `execute_resume_paused` has no waker
parameter to set.

**Resume and start emits exactly one opaque reconciliation wake**, after every
member's outcome is durable and never before. The wake id is derived from the
operation id, so a retried Resume reuses it and the delivering seam adopts
rather than repeating. M3-D owns what the wake *means*; M3-C owns only that
there is exactly one and that it comes last.

**A partial restore settles.** `restored-exact`, `restored-fresh`, `failed` and
`unresumable` are all terminal member outcomes. One worker that could not come
back does not hold its restored siblings — or its supervisor — in
`reconciliation-required`; the fleet starts, and the single wake describes every
outcome including the failure. Only a genuinely *undecided* member (still
pending, or a restore whose physical result was ambiguous) blocks the terminal
commit. Fresh restore is never a silent fallback for a refused exact one:
exact restoration is what makes the resumed transcript the same transcript, and
a fresh authority must be supplied deliberately.

**A Resume that stops short is continued, not restarted.** Two things leave an
operation in `reconciliation-required`: a member whose physical result was
ambiguous, and a wake that did not land. Both leave real state behind — panes
that came back, a lifecycle already declared, a barrier already released — so
the repair finishes the same operation. `resume/retry` claims no new boundary,
re-observes no roster, releases no second barrier, and re-restores no member
that already has a decided outcome; the wake id is the operation's, unchanged,
so a wake delivered before is adopted rather than repeated. Without it the only
escape was to force-Stop a fleet that was in most cases already running.

Every transition identity is derived from `(operation_id, label, state_epoch)`
rather than minted by the caller. Each attempt begins at a distinct epoch, so
it gets distinct ids automatically, while a replay of the same attempt
recomputes identical ids and adopts. The earlier caller-minted scheme made this
a convention the caller had to honour, and it did not: a retry re-derived the
*restore* id, whose stored payload differed, so every retry died on "already
exists with different immutable request content".

The retry carries an opaque receipt derived from the durable evidence — the
state epoch and every member's recorded outcome — so it is reproducible under
response loss rather than minted per call. `provenance.retries` exposes each
retry with its receipt and actor, `provenance.retryable` says whether the
operation can still be continued, and `provenance.reconciliation_reason`
carries why it stopped. A surface that cannot tell "unfinished" from "finished"
sends operators to the force-Stop button.

**Only a delivered wake may be reported as one.** The dashboard says "the
supervisor was told" exactly where `wakeWasDelivered` holds: a settled Resume
whose target is not `paused`. Decided members are not sufficient evidence — an
operation that stopped *because* its wake did not land has entirely decided
members and a supervisor that knows nothing — and a Resume-paused wakes nobody
by design, so it must not borrow the sentence. Reconciliation states render the
retry control and the durable reason instead, alongside the decided failures,
which are reported as decided and are never retried.

**Rollback.** C4 adds no table and no column: it fills the
`source_operation_id` / `resume_target` carriers C1 already reserved and reuses
the barrier's existing `open` state. Rolling back to a build without Resume
leaves readable rows — the projections are shape-compatible — and that build
fails closed on them, because an unrecognised `operation_kind` has no allowed
transitions and therefore cannot half-advance a Resume it does not understand.
A released barrier is an ordinary `open` barrier, which an older build already
handles correctly, and every release bumps the row epoch so a lost update stays
detectable.

### 4.5 M3-D: task occurrences, safe drain, and what a resumed supervisor is told

M3-C's whole surface has one shape of hole in it: it knows which *panes* an
operation touched and refuses to say what any of it meant. Safe Pause and safe
Stop consume an "opaque M3-D receipt" that nothing produced, and Resume's single
wake was delivered by a seam whose honest answer was "this fork has no
supervisor-reconciliation authority". M3-D is that authority. It consumes M3-A's
stable agents/incarnations and M3-C's cohort receipts as **opaque evidence**; it
never creates a second physical roster and never reinterprets a fork effect.

**A task occurrence is not a generation and not a conversation.** Both name a
disposable physical effect, and a stable agent outlives many of each — an exact
reincarnation deliberately keeps the *same* native conversation across a *new*
generation. Keying a task on either is how a resumed pane silently inherits the
previous round, or how a late report lands on the wrong one.
`task_occurrences` therefore mints its own id and carries the exact
effect-incarnation reference (`incarnation_id` + `terminal_id`/`generation`)
alongside it, so a reader can still say which effect produced which evidence.

Four properties are load-bearing:

- **Stable-agent reuse never reopens a finalized occurrence.** A partial unique
  index on `agent_id WHERE state = 'open'` makes one agent hold at most one open
  occurrence, and a finalized row sits outside that index — so reuse opens a new
  occurrence rather than reviving a closed one. That index is *partial* on
  purpose: a full one would forbid an agent's second round entirely.
- **Current and finalized are separate column families.** A supervisor keeps
  revising an open round's boundary and seed; finalizing copies the accepted
  values into a write-once family. Shared columns would let a late write — the
  one a crashed-then-retried worker sends — rewrite what a finished round
  reported.
- **The seed states its own completeness.** `complete | truncated | empty` is
  required, never derived. Only `complete` licenses a fresh successor unattended.
  `truncated` is the dangerous one: it reads as context while missing the part
  that mattered.
- **Unknown extensions are preserved and routed, never interpreted or
  redispatched.** An extension carries an opaque versioned payload and names its
  decider. A future build's completion claim is stored verbatim, blocks
  *reporting* the round complete, and goes to its decider — turning it back into
  a dispatch would replay work nobody has ruled on.

**Safe drain is what a safe Pause/Stop actually spends.** The drain snapshots the
exact cohort boundary, steers each non-idle/non-parked worker **exactly once**
(the control id is derived from the drain and the agent, so a retry adopts rather
than typing a second steer), and requires **both** halves of a boundary: a report
or checkpoint digest *and* positive idle/parked proof. Either alone is a guess —
quiescence without evidence is a worker that stopped without saying where it got
to, and a report from a still-running turn is the Codex false-pause. The
supervisor drains **last**, only once every worker is decided; a supervisor that
parks first cannot attribute what its workers do next. For a Stop, CAO's teardown
is durably requested **before** the receipt exists and therefore before any Stop
can consume it — so a pane that vanishes mid-Stop is a recorded intention rather
than a lost pane to adjudicate.

A drain that cannot prove a boundary stays `pending`/`reconciliation-required`
with **no receipt**. It never degrades into a force operation: continuing is an
explicit retry, and abandoning the boundary is M3-C's explicit, receipted
safe-to-force promotion, whose receipt is derived from the stalled drain rather
than minted.

**A receipt is a reference, not a bearer token.** Spending one resolves it back
to the drain that issued it and re-validates three things, because a drain
proves a boundary the fleet reached *then*:

- **the intent matches.** A Pause drain and a Stop drain prove different
  things; only a Stop drain records CAO's teardown intent. A Pause receipt
  spent as safe Stop would collect panes nobody announced, so it is refused —
  as is a digest naming no drain at all.
- **the boundary is still current.** The claim binds the *drained* lifecycle
  epoch, roster revision and member snapshot rather than a fresh observation,
  so `claim_operation` performs the compare-and-swap and a fleet that moved
  loses the claim instead of committing on a stale classification.
- **the fleet is still quiescent.** Opening a task occurrence touches no
  stable-agent revision, so a supervisor dispatching another round is invisible
  to boundary binding alone. Each member's recorded occurrence and boundary
  digest are re-read at spend time; a new round, a different round, or a later
  boundary on the drained one is intervening later work and the answer is a
  fresh drain. Refusing is the entire behaviour — nothing promotes itself.

That is why the safe surfaces are two steps. `cao session drain` runs the drain
and prints the one spend command **matching its intent** — never both, because
offering the other teaches a command the server refuses. The dashboard renders
"Drain to a boundary" and only offers "Safe pause" once a spendable Pause
receipt exists. Both name the **drain**, not a digest: the receipt and the
per-member classification are then read from one durable row, where a
hand-carried digest beside an edited member list would spend a real receipt on
a claim it does not describe. `cohort/pause/safe` and `cohort/stop/safe` keep
M3-C's shape for the component that produced the evidence itself.

**The reconciliation wake.** M3-C guarantees exactly one wake per Resume and that
it comes last; M3-D owns what it says and whether it landed. The message is
rendered deterministically from the durable cohort record — including its
truncation, so a replay is byte-identical — and lists what the supervisor cannot
see for itself: exact-restored, fresh-fallback (with the completeness of the seed
each fresh worker was given), interrupted, parked, failed, and unresumable. It is
addressed to the supervisor's **current** incarnation, because an exact restore
returns the same conversation on a new pane. One ledger row per source operation
makes "exactly once" durable rather than per-process, and a delivered wake is
never downgraded by a later attempt. A wake that did not land leaves M3-C's
Resume in reconciliation, which is correct: the fleet is back and nobody has been
told. Resume-paused sends **zero** input and claims no wake at all; starting a
paused fleet later gets its own one-wake guarantee under a derived id.

**Supervisor-owned worker acts.** Retiring a worker finalizes its round *before*
the pane is collected — collecting first destroys the evidence at the moment
somebody needs it. Worker resume is exact-only; a refused restore is `failed`,
never a silent fresh relaunch, because the value of an exact resume is that the
transcript is the same transcript. Lost-pane recovery has three outcomes and the
third is the point: exact restoration, a fresh fallback **only** with the
supervisor summary, digest-bound artifacts, and an explicit `complete` seed, or
otherwise the worker stays **paused for reconciliation** — nothing launched, no
input replayed, no instruction reconstructed. A visible gap is recoverable; a
worker confidently doing the wrong thing is not.

A fresh fallback **admits before it launches**. In the realistic lost-pane case
the predecessor round is still *open*, because the thing that would have closed
it is the pane that vanished — so admission resolves that round (`lost`,
write-once) and retires the lost incarnation, and only a stable agent with no
open round and no live pane may have one created for it. That ordering is what
makes the retry safe: an agent that is live at admission time on an incarnation
the predecessor round does not name is one a previous attempt already launched,
so it is **adopted**, not launched again. Launching first and discovering the
open round afterwards left a pane nobody pointed at, and a retry made another.

**The reconciliation wake resends its claim, not a re-render.** The rendered
message is a proposal: it is persisted the first time an operation claims a
wake and ignored on every attempt after. Only the delivery *target* is
re-resolved, because the supervisor's pane legitimately moves while its words
do not. A resend under the same control id carrying different text would either
be suppressed as a duplicate of a message nobody saw, or land as a second
differently-worded version of the same wake — and the ledger would describe
neither. What is stored is what was sent, which is the only reason "the
supervisor was told X" is checkable.

**Rollback.** M3-D adds five tables and **no column to any table an older build
writes** — the cohort member carriers it fills (`task_occurrence_id`,
`boundary_digest`, `report_digest`, `checkpoint_digest`) were reserved by C1.
Rolling back leaves those rows simply unread, and an older build's own schema is
byte-identical to what it had before.

### 4.6 M3-E: handing a task back to the worker that had it

A provider handoff moves a task from worker A to worker B without destroying A's
native conversation. When quota or capability later makes A preferable again,
the supervisor hands the task **back**: it steers B to a safe boundary, resumes
A's original native session exactly, delivers a catch-up packet describing what
B did while A was dormant, and transfers the task. The packet catches A up; it
does not replace A's own history, which is the entire reason for resuming rather
than starting a successor.

A handback has four boundaries and each one is a separate durable step, because
each one is a point where the supervisor can still change its mind: begin,
deliver the packet, transfer, or roll back.

**The held window is its own row, not a state on the occurrence.** This is the
load-bearing placement. `task_occurrences.state` stays `open` for the donor's
whole life. A third state would take the donor's row out of
`open_occurrence_for_agent`, and the safe drain reads *no open occurrence* as
positive `parked` — so a drain running concurrently with a handback would record
the donor's **previous** round's digests as a proven boundary, fold them into
its receipt, and under Stop announce teardown for the very pane the handback is
keeping alive as its rollback insurance. `_later_work_refusals` could not catch
it either: its only "moved" signal is a different open id on the *same* agent,
and a donor in that state has no open id at all.

**Rollback restores the donor by having changed nothing.** Because a pending
handoff never writes to the task, restoring the donor's authority and its
managed input is one compare-and-swap on the handoff row. There is no
compensating write to get wrong and no window in which a half-undone transfer is
visible. Rollback also **always releases**: it reports whether the donor is
still a usable worker, but it never refuses, because a rollback that could
refuse would leave both agents held with no forward path — worse than an honest
"the donor is gone; recover it".

**A transfer mints a new occurrence; it never rewrites the donor's row.**
`agent_id`, `round_index` and the incarnation columns are the immutable content
`open_occurrence` adopts a response-loss retry against, two unique indexes are
keyed on them, and both `retire_worker_pane` and `admit_fresh_successor` read by
agent and then finalize by occurrence id with **no `agent_id` predicate** — so a
moved row lets one worker's teardown finalize another worker's live round.
Completing a handback finalizes the donor `superseded` and opens a fresh
occurrence for the recipient **in one transaction**, exactly as lost-pane
recovery already does for a successor. A crash between the two writes leaves the
handoff pending and still rollback-able rather than leaving the task unowned.
The recipient's round index is its own next one, derived rather than accepted:
`ix_task_occurrences_round` is not partial, so a *finalized* round of the
recipient's own past occupies the slot just as much as a live one.

**Exactly one authority, enforced by the database.** Three partial unique
indexes — one pending handoff per occurrence, per donor, and per recipient. A
settled row leaves all three, so the same pair may hand back again later.

**Both sides are held, for different reasons.** The donor is held because the
packet describes a state it must stop changing: a steer landing after the digest
was taken means the packet the recipient reads is already wrong. The recipient
is held because the supervisor's roster view can be stale — it believes that
agent dormant — and task input arriving before the packet has the recipient
acting on context it does not have, for a round it does not yet hold. The
recipient's exemption is exact: the one derived control id the packet is
delivered under, and nothing else.

The hold lives in `provider_byte_admission`, the narrowest point every task-byte
lane for a live managed pane already passes through, and it is reported as its
own refusal reason (`handoff-held`) rather than as a generation fence. The
distinction matters operationally: a fence is permanent and tells the caller to
advance to a successor generation, while this hold is reversible and applies to
the generation the caller already has. A caller that read one as the other would
abandon the pane the handback is preserving.

**What is deliberately absent.** No native session id is recorded anywhere in a
handoff. A handback never moves a native conversation between workers — the
recipient's own session is resumed exactly by M3-B, whose roster predicates
already refuse a cross-harness bind — so a copy here would be a second,
unenforced statement of a fact true of only one side. There is also no proof
that the donor left no running child processes, because nothing in this system
tracks them and the containment surface that would prove their absence fails
closed permanently; the evidence records that none are *tracked* rather than
claiming an absence nobody observed.

**Quiescence is proven where it can be and honest where it cannot.** A donor
with an `active` managed turn is refused outright. An `unknown` turn is accepted
and recorded as unproven, because not every installed provider runs a heartbeat
producer and refusing would forbid handback on exactly the routes that most need
it. Every read surface and the settled receipt carry the value, so a handback
taken on unproven quiescence is visible rather than silently equivalent to one
that was observed.

**Nothing starts a handback on its own.** It is one explicit supervisor
decision; no detector, recovery ladder, or lifecycle transition begins one.
`GET /task-handoffs/capability` publishes that as a block rather than leaving it
to be inferred from the routes' existence. `cao session handoff list|show` are
read-only and answer the two questions an operator actually has: what is in
flight, and why was my steer refused.

**Rollback.** M3-E adds one table and changes **nothing** M3-D owns: no column
on `task_occurrences`, no new occurrence state, no new disposition. Rolling back
leaves the handoff rows unread and an older build's schema and reading of every
existing row byte-identical to what they were.

---

## 5. Waiting, and why it is not yet safe

`working` permits idle workers when they are *waiting*. That permission is only
sound when every wait carries all three of:

- a **registered external trigger** — something CAO knows about that will wake it;
- a **deadline**, with the estimate optional and the maximum 8 hours;
- a **round counter** the supervisor can see.

Miss any one and "waiting" is "stalled with a nicer name."

None of it exists today. There is no `conduct/commands/trigger.py`: the
COND-0241/0267 typed external-trigger service was parked 2026-08-01 with
`continued_at: null` and its terminal long dead. Marshal triggers C and D depend
on it and are dead letters until it lands.

Two properties to preserve when it does:

**A worker may wait indefinitely but never silently.** On deadline expiry CAO
wakes the worker; the worker decides whether to keep waiting; either way it tells
the supervisor its intent, its total elapsed wait, and its round count. A
supervisor must never discover that a worker it believed was working has been
quiet for two days.

**8 hours × unlimited rounds is still forever.** There must be a total-elapsed
cap or a mandatory escalation at round K.

**Escalation does not route through the marshal.** A worker waiting on a PR whose
reviewer went on holiday is a deadlock *by design*: the instructions are correct
and are being followed. The marshal has nothing to diagnose and would file
`inconclusive` plus instrumentation for a non-defect. Wait-round escalation goes
straight to `/alert-colin`.

---

## 6. Session kind

`kind: service` exists for sessions that are not supervisor-managed campaigns —
initially a memory curator running as its own long-lived session, accepting
messages from several campaigns.

A service session must never be judged by campaign criteria. "No work item has
advanced in six hours" is a stall for a campaign and the normal condition for a
curator. Its health question is different: **is it responsive?** — are messages
being delivered, is the inbox draining, does it answer. A curator also never
declares itself `complete` unless told to externally.

The `kind` field is in scope now because it is cheap and it prevents a whole
class of false alarm. The service health model is deferred (§8).

---

## 7. Marshal suppression

The marshal fires on sessions in **none** of `working`-with-progress, `complete`,
`paused`, or `stopped`.

**If session state cannot be read, the marshal still fires**, recording
`session_state_unreadable` in its evidence quality so the investigator's first
question is whether this was a suppressed session. The reasoning is asymmetric
cost: the marshal is report-only, so a false alarm costs one investigation, while
a missed deadlock costs days. This is a deliberate exception to the
"unknown beats confidently wrong" principle that governs `workstate` — there,
unknown is presented to a human who can wait; here, silence is indistinguishable
from health.

---

## 8. Deferred

Filed as issues against the `cao-system` project when the tracker goes live.

| item | why deferred |
|---|---|
| ~~Operator adjudication for an `ambiguous` native-session attachment (§4.2)~~ | **shipped** — `cao attachment adjudicate`, and the release wiring underneath it that turned out to be missing entirely |
| A dashboard surface for session claims | the CLI and API exist; nothing renders them, and `run_manifest._attachment_projection` drops the two fields an adjudicating human needs |
| Memory curator as a shared service, with a responsiveness health model | needs a health contract of its own; the `kind` field unblocks it |
| Relaunching a historical agent into a new session from the Agents tab, with session-id autocomplete | UI plus a resume path for non-managed launches |
| Session forking | interacts with resume identity; `validate_resume_argv` explicitly forbids `--fork-session` today |
| The 8h wait cap, round counter and registered triggers | blocked on COND-0241/0267 |
| Total-elapsed cap or mandatory escalation at round K | same |
