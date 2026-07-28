# Coordination Rules

## Ownership

The portfolio supervisor owns catalog and dispatch state. A child runner owns
one AI-DLC intent and one worktree. The AI-DLC engine owns all child stage
routing and state transitions.

## Parent Lifecycle

The portfolio utility owns parent routing:

`Bootstrap -> Discover -> Confirm -> Plan -> Dispatch -> Integrate -> Learn`

Run `lifecycle status` after startup and callbacks. Advance only to the reported
next phase. Dispatch requires named human acceptance; integration and
completion require every reported blocker to be resolved. Never infer a parent
transition from child AI-DLC state.

## Convergence

Each child submits a structured result before completion. After all sessions
complete, run `convergence check` to compute upstream and downstream impact and
evaluate all eight dependency types. Missing results and incomplete dependency
ordering must be remediated. Unknown, deferred, and breaking findings require a
revision-bound human decision before Learn. Never treat a stale decision as
approval; the utility ignores it when the underlying risk changes.

## Dispatch

Before dispatch:

1. Validate the portfolio.
2. Confirm organization, business outcomes, capabilities, and dependencies
   through `discovery confirm`. Unknown and deferred facts must appear in the
   exact human acceptance lists.
3. Confirm the project and intent are registered.
4. Confirm the intent maps the project to one unique worktree and branch.
5. Check `dependsOn` and catalog dependencies whose `blockingAt` includes
   `dispatch`. A dependency's source project waits for its target project.
6. Confirm no active session already owns the child intent or worktree.
7. Confirm no pending human question packet exists for the child.
8. Run `harness verify` for the exact project and intent.
9. Pass only the relevant business, component, contract, and dependency slice.

Use non-blocking CAO assignment for independent work and pass the registered
absolute worktree as `working_directory`. Do not use CAO's inherited supervisor
directory, and do not use a blocking handoff to serialize work that can run
concurrently.

Record a returned terminal ID with `session update --status active`. If CAO
assignment fails, keep the session pending.

## Human Questions

Generated AI-DLC questions are immutable, human-owned decision points.

1. The runner submits unanswered Markdown with `question submit`; pre-answered
   packets are rejected and the session becomes `waiting`.
2. The supervisor offers Guide, Edit Markdown, or Chat.
3. The supervisor writes human answers verbatim without changing question text.
4. `question answer` verifies question integrity and complete answers, records
   the interaction mode and human decision maker, then returns the session to
   `active`.
5. The runner resumes only after observing the answered packet.

Neither permission bypass nor runner autonomy authorizes self-answering.

## Shared Memory

The supervisor is the sole approval authority for durable project and team
memory. The utility is the sole physical writer to canonical memory.

1. `dispatch check` captures the canonical SHA-256 baseline.
2. Runners treat worktree memory writes as temporary and submit structured
   proposals with evidence and the supplied baseline.
3. `learning approve` serializes writes under the portfolio lock.
4. A stale proposal cannot apply until the supervisor reviews current memory
   and records a `learning reconcile` note.
5. Equivalent rules are idempotent and do not append duplicate text.
6. The runner cleans worktree memory back to Git `HEAD` using its exact
   inspected revision.
7. Session completion is blocked while project or team memory remains in the
   feature diff.

Do not merge shared-memory changes opportunistically from feature branches.

## Contract Changes

A child may identify a shared-contract change but must escalate it before
downstream assumptions change. The supervisor records the proposed revision,
affected intents, compatibility policy, and approval. Resume affected children
only after the decision is durable.

## Recovery

CAO terminals are disposable; AI-DLC state is durable. On terminal loss, inspect
registered worktree and child status, then launch the same runner against the
same intent. Never birth a replacement intent merely because a terminal ended.

Portfolio mutations record the lock owner's process ID and timestamp. A later
mutation rejects a live owner and automatically reclaims a lock whose process
has exited; a newly created lock without valid owner metadata receives a short
grace period before reclamation.
