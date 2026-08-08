# Session lifecycle

A CAO session has a declared lifecycle state. The fire marshal fires only on
sessions in **none** of the declared states, which makes this document's real
subject not "what states exist" but **"can a declared state be wrong?"** —
because every declared state is a suppressor for the recovery path.

Status: phased. The lifecycle storage, the HTTP/CLI routes, and the
row-preserving stop/archive collection described here are **implemented**
(by this work). Two things remain **deferred**: resuming a stopped session
(relaunching each worker against its recorded native session — see §4), and
the Fire Marshal cutover that consumes these declarations. The lifecycle
foundation is a prerequisite for both, sequenced first because without it a
deliberately-stopped session is indistinguishable from a stalled one.

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
