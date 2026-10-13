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
   contradiction, and scope.
3. To accept, the supervisor writes the rule into CAO memory with the
   `memory_store` MCP tool — scope `project`, memory_type `feedback` — then
   records that promotion with `learning promote --promoted-by <supervisor>`
   (optionally `--memory-key` naming the CAO memory entry). The utility stamps
   the proposal `promoted` with the reviewer and the memory coordinates; it
   performs no file write.
4. To decline, the supervisor runs `learning reject --reason <text>`.
5. A proposal is reviewed once: a `promoted` or `rejected` proposal cannot be
   re-promoted.

Integration completion still blocks while any learning proposal is `pending`,
so no durable learning is left unreviewed.

## Accepted Trade-off

Because durable learnings live in CAO memory rather than in per-worktree files,
you lose per-worktree `git diff` inspectability of memory. This is intended:
CAO memory is the system of record. Inspect durable knowledge with
`memory_recall`, not a feature-branch diff.
