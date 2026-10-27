# Shared Memory Coordination

## Where Durable Memory Lives

Durable, cross-intent portfolio knowledge — catalog facts, cross-intent
dependencies, decisions already made, and durable project/team LEARNINGS —
lives in CAO's native memory, written and read through the CAO memory MCP
tools (`memory_store` / `memory_recall`). It is NOT a hand-built flat-file
protocol: there is no canonical `project.md`/`team.md` compare-and-swap, no
SHA-256 baseline, and no portfolio lock serializing memory writes. CAO memory
owns durability and concurrency.

Each AI-DLC space still keeps its own `aidlc/spaces/<space>/memory/project.md`
and `team.md` inside the child worktree. Those files are the intent's LOCAL
scratch while it runs. They are not merged, not promoted automatically, and no
longer block session completion.

## Ownership

The portfolio supervisor is the only role that promotes a learning into
durable CAO memory. A durable learning is still REVIEWED by the supervisor
before it becomes canonical — the review is the supervisor deciding whether to
`memory_store` the runner's proposed rule, not a silent auto-write.

## Review Flow

1. A runner captures a durable learning in its worktree scratch, then renders
   `assets/templates/learning-proposal.yaml` and submits it with
   `learning propose`. The proposal records project, parent intent, AI-DLC
   space, destination, a target heading, one normalized rule, evidence, and the
   source stage. It carries no baseline revision.
2. The supervisor reviews the proposal with `learning list` for duplication,
   contradiction, and scope. Check `destination`: a `team` learning is
   delivered to every project's runners; reject and ask for a `project`
   proposal if it is project-specific.
3. To accept, the supervisor writes the rule into CAO memory with the
   `memory_store` MCP tool — scope `project`, memory_type `feedback` — then
   records that promotion with `learning promote --promoted-by <supervisor>
   --memory-key <key>`, using the key `memory_store` returned. The utility
   stamps the proposal `promoted` with the reviewer and the memory
   coordinates; it performs no memory file write. One key names one promoted
   learning.
4. To decline, the supervisor runs `learning reject --reason <text>`.
5. A proposal is reviewed once: a `promoted` or `rejected` proposal cannot be
   re-promoted.

Integration completion still blocks while any learning proposal is `pending`,
so no durable learning is left unreviewed.

## How Runners Receive Promoted Learnings

CAO does not give all terminals one project scope. `memory_store` and
`memory_recall` resolve the project scope from the calling terminal: first
`CAO_PROJECT_ID` or `memory.project_id`, then the `origin` remote of the
terminal's working directory, then a hash of that directory's path. In the
documented layout the supervisor runs in the portfolio root and each runner
runs in a worktree of its own repository, so each resolves a different scope.
A runner's own `memory_recall` therefore never returns what the supervisor
promoted, even when every intent uses one repository.

The supervisor delivers promoted learnings at dispatch instead:

1. It calls `memory_recall` in its own scope (`scope: "project"`,
   `memory_type: "feedback"`) and saves the JSON result.
2. `dispatch message --recall <file>` joins each recalled entry to the
   promotion ledger by memory key. An entry is delivered only if it names a
   promoted learning, its recalled scope and type match the promotion, and the
   learning applies to this dispatch: a `team` learning applies to every
   project, and a `project` learning applies to its own project and to
   projects linked to it by a catalog dependency or an intent `dependsOn`.
   Recalled memories that no promoted learning records, such as unrelated
   supervisor memories or a rule whose proposal was rejected, are excluded.
3. Delivered learnings use the recalled memory content, which is the current
   CAO version of the rule. They are bounded (at most 10 per assignment, each
   truncated to 1,000 characters), fenced so it cannot add sections to the
   message, and placed under "Promoted portfolio learnings" in the
   assignment's Relevant Context, labelled as reference data, not
   instructions.
4. The output lists `missing` promoted learnings the recall did not return
   (forgotten, or outside the recall's limit) and `pending` proposals that
   cannot be delivered until reviewed.

Delivery is a snapshot taken at dispatch. A runner already running does not
receive a learning promoted later; the next dispatch does. A catalog
dependency that blocks at `dispatch` gives the ordering the cross-project case
needs: the dependent project is dispatched after its target completes. Review
the target's pending proposals before rendering; `dispatch message` lists
them as `pending`.

Runners treat delivered learnings as data. They do not override the runner
profile, the AI-DLC engine, or a human decision, and a runner reports a
conflict to the supervisor instead of resolving it. A runner may still use
`memory_recall` for its own repository's scope.

### Alternative: One Project Id For The Server

An operator who wants every terminal to recall the same project scope can set
`CAO_PROJECT_ID` (or `memory.project_id` in CAO settings) for the CAO server
that starts the supervisor and the runners. All of its terminals then resolve
that one id, and a runner's own `memory_recall` returns the supervisor's
promoted learnings. Runners can then also write that scope, so a runner's
`memory_store` under a promoted key changes what later dispatches deliver;
the review step becomes advisory. The trade-off: the setting applies to every
terminal of that server, not only to this portfolio, so unrelated sessions
read and write the same project scope. The example does not require it and
still delivers learnings at dispatch.

## Accepted Trade-off

Because durable learnings live in CAO memory rather than in per-worktree files,
you lose per-worktree `git diff` inspectability of memory. This is intended:
CAO memory is the system of record. Inspect durable knowledge with
`memory_recall` in the supervisor's scope, not a feature-branch diff.
