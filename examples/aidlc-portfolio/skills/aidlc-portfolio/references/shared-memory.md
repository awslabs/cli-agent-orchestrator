# Shared Memory Coordination

## Ownership

The portfolio supervisor is the only role allowed to approve durable changes
to a project's canonical `project.md` or `team.md`. The portfolio utility is
the only component allowed to perform those writes.

Child AI-DLC sessions may update memory in their worktrees while they run.
Those files are temporary session snapshots and must not be integrated from a
feature branch.

## Proposal Flow

Each proposal records:

- project, parent intent, AI-DLC space, and destination;
- target heading and one normalized rule;
- evidence and source stage;
- the canonical SHA-256 revision observed at dispatch.

The supervisor checks the proposal against the latest canonical section. It
rejects unsupported rules, approves independent rules, and explicitly
reconciles stale proposals after checking for duplication, contradiction, and
scope.

`learning approve` checks the base revision and appends an ID marker while
holding the portfolio lock. The marker makes approval replay-safe after a
partial failure.

## Completion Barrier

Before a child session completes:

1. Submit every durable local learning as a proposal.
2. Resolve all relevant pending proposals.
3. Inspect worktree and canonical revisions for project and team memory.
4. Clean each worktree file back to Git `HEAD` using the exact inspected
   worktree revision.
5. Mark the session completed.

The clean compare-and-swap prevents the supervisor from discarding a memory
change made after inspection. The completion transition refuses any remaining
shared-memory feature diff. `memory refresh` is separate: it pulls current
canonical context into an active worktree at a safe stage boundary.
