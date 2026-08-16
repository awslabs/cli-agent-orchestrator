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
and started), and the durable projection behind all of them. The legacy stop
route still behaves exactly as documented in §4.0. Fire Marshal cutover remains
deferred.

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
as its own button rather than a modifier. There is deliberately **no safe-Pause
CLI command or dashboard button**: a safe Pause consumes evidence only M3-D can
produce, and offering a button that quietly did a force Pause instead is the
mislabelling the whole split exists to prevent.

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
