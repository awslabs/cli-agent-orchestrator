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
4. The human actions CAO's human-approval surface. That surface — and only that
   surface — mints an approval record bound to the packet, carrying the
   VERIFIED human identity. No agent-facing command can write such a record.
5. `question answer --approval-id <id>` verifies question integrity and complete
   answers, then binds the resolution to that human-surface approval: the
   answerer identity comes FROM the approval record, not from a caller-supplied
   argument. A packet with no matching, unconsumed, human-surface approval is
   refused, so an agent cannot self-answer by asserting an identity. The
   approval is single-use.
6. The runner resumes only after observing the answered packet.

The real enforcement is this binding, not prompt text. The "never answer on the
human's behalf" rule remains in the profiles as defense-in-depth, but even an
agent that ignored it cannot resolve a packet without a human-surface approval.

## Shared Memory

Durable portfolio knowledge lives in CAO native memory, written and read
through the `memory_store` / `memory_recall` MCP tools. The supervisor is the
sole authority that promotes a learning into that memory.

1. Runners treat worktree `project.md`/`team.md` as LOCAL scratch and submit
   durable learnings as structured proposals with evidence (`learning propose`).
   There is no dispatch SHA-256 baseline and no canonical-file compare-and-swap.
2. The supervisor reviews proposals (`learning list`) for duplication,
   contradiction, and scope.
3. To accept, the supervisor writes the rule into CAO memory with `memory_store`
   (scope project, memory_type feedback), then records that promotion with
   `learning promote --promoted-by <supervisor>`. To decline, use
   `learning reject`.
4. A proposal is reviewed once; a promoted or rejected proposal cannot be
   re-promoted.
5. Integration completion blocks while any proposal is still `pending`.

Per-space worktree memory files no longer block session completion and are not
merged from feature branches. Durable knowledge is inspected with
`memory_recall`, not a git diff.

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
