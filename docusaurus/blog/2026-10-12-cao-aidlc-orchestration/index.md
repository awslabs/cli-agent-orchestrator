---
title: "Running AI-DLC on CAO: shared memory and multi-intent orchestration"
authors: [fanhongy]
tags: [orchestration-patterns]
description: You already run AI-DLC for one intent at a time. CAO is the orchestrator that runs many of them, with a shared memory layer and isolated worktrees.
---

If you run [AI-DLC](https://github.com/awslabs/aidlc-workflows), you drive one development intent
through a structured, audited lifecycle, with a human in the loop at every decision that matters.
That focus is deliberate. AI-DLC is not a fully autonomous agent, and it is not a scheduler for many
intents at once. One intent, done well.

Real product work rarely stays inside one intent. An API change needs a client change. A migration
has to land before a release. And even a single intent accumulates a lot of context as it moves
through its stages. At that point you want two things AI-DLC does not set out to provide: a place
to keep what each intent learns, and a way to run several intents without them colliding.

That is what this post is about. You can run AI-DLC on top of CAO, and CAO supplies exactly those
two things.

{/* truncate */}

## What CAO is

[CAO](https://github.com/awslabs/cli-agent-orchestrator) (CLI Agent Orchestrator) runs and
coordinates multiple CLI coding agents across isolated git worktrees and sessions. It gives every
agent the same shared memory layer, the same handoff primitives, and one surface to
watch and steer them from. It is agent-agnostic, so the same orchestration works whether the agent
underneath is Kiro CLI, Claude Code, or Codex.

In other words: AI-DLC is the disciplined lifecycle for one intent, and CAO is the thing that can
run many of those lifecycles at once and remember what they learn.

The example below targets CAO `v2.5.4` and AI-DLC Workflows `2.x`. Its portfolio utility is covered
by the example's own tests, and the memory behavior it depends on, one project scope per terminal,
was checked against CAO `v2.5.4`'s own `memory_store` and `memory_recall` code. The hand-off of
learnings at dispatch has not yet been run in a live CAO session. The example lives in
[`examples/aidlc-portfolio`](https://github.com/awslabs/cli-agent-orchestrator/tree/main/examples/aidlc-portfolio),
and it is being updated alongside this post.

## How the pieces line up

Before the benefits, here is how your AI-DLC concepts map onto CAO. Nothing in your workflow has to
change its meaning. CAO hosts each piece.

| In AI-DLC | What it is | On CAO |
| --- | --- | --- |
| An intent | One bounded outcome driven through the lifecycle | One worktree, branch, and session |
| A space's `memory/*.md` | The intent's working knowledge files | Local scratch, with durable facts promoted into CAO memory |
| A question packet | A decision deferred to a human, with the run paused | A human-approval step the agent cannot answer itself |
| A stage transition | The lifecycle advancing one step | Still owned by the AI-DLC engine, inside the intent's session |
| Running two intents | Doable, but awkward: the root `aidlc/` tree is shared, so parallel worktrees contend over it | Each intent in its own worktree and session, coordinated by one parent, with no shared-root contention |

The rest of the post walks the two things you gain: memory and orchestration. It closes with the
one rule to keep when CAO sits between AI-DLC and you.

## Memory: keep what each intent learns

AI-DLC already keeps memory. It keeps a thin one. This section covers what your memory is today, and
what a shared layer adds.

### What AI-DLC memory is today

AI-DLC keeps an intent's working knowledge in a two-tier set of Markdown files under one space:
`aidlc/spaces/<space>/memory/team.md` holds what your team has affirmed, and `project.md` holds
what is specific to this one project. This works while you stay inside one intent in one worktree.

It stops working the moment several intents need to share what they learn. A contract rule you
discover in the API intent is one the client intent needs before it starts. A convention you agree
once should carry into later intents. A per-space file cannot do that. It is local to its worktree,
it knows nothing about other intents, and nothing coordinates concurrent writes.

The first version of the portfolio example showed how much work that gap costs. To make the
per-space files act like a shared, durable store, the example built a protocol on top of them: a
revision recorded at dispatch to detect drift, a propose-and-approve flow for durable learnings, a
lock so approval was replay-safe, and a compare-and-swap barrier at completion. That was careful
work, and a lot of it, all to give flat files the behavior a memory system is supposed to provide.
The example now keeps only the review step and leaves storage to CAO memory.

### What CAO memory adds

CAO owns a memory layer that every agent uses through the same tools. It stores distilled
conclusions, not raw transcripts. Agents write and read it with `memory_store` and `memory_recall`,
not by hand-editing files, and it persists across sessions and restarts. The
[first post in this series](../2026-10-08-cao-memory-design/index.md) covers how that layer works.

One detail decides how it carries knowledge between intents: CAO resolves the project scope per
terminal. It uses `CAO_PROJECT_ID` if you set one, and otherwise the git remote or the path of the
terminal's working directory. In the portfolio layout, the parent runs in a portfolio root and each
intent's runner runs in a worktree of its own repository, so each one lands in a different project
scope. A runner's own `memory_recall` does not see what the parent stored. Sharing across intents is
not automatic.

So the example delivers it. At dispatch, the parent recalls the reviewed learnings from its own
scope and puts the ones that apply to that intent into the runner's assignment, bounded and labelled
as reference data rather than instructions. That is how the API intent's contract rule reaches the
client intent: when the client waits on the API contract, the parent dispatches it after the API
intent completes, and, once the parent has reviewed the API intent's learnings, its assignment
carries the rule. A runner that is already running does not get a learning promoted after its
dispatch. If
you would rather have every runner recall one scope directly, set `CAO_PROJECT_ID` for the CAO
server. That works, but it applies to every terminal of that server, not only to the portfolio.

Pointed at AI-DLC, CAO memory works two ways:

- **Amplify.** Keep your per-space `project.md` and `team.md` as the intent's local scratch. Promote
  the durable learnings into CAO memory instead of into a hand-built approve-and-sync protocol.
  Persistence and concurrent writes become the store's job, not yours. Handing a learning to the
  next intent is the orchestrator's job, done at dispatch.
- **Replace.** For portfolio-level knowledge, let CAO memory be the store. That is the project
  catalog, the dependencies between intents, and the decisions already made. Nothing to journal by
  hand: the parent that owns that knowledge works from one place, so its own recall spans every
  intent and survives a restart.

The point is not that CAO also has memory. It is that your intent-local memory and CAO's
cross-session memory compose. The intent keeps its scratch. Everything worth carrying forward lives
in a store the parent can recall after a restart, and the parent hands it to the next intent when it
dispatches it.

## Orchestration: run several intents at once

AI-DLC runs one intent at a time. CAO runs many without them colliding. This section covers worktree
isolation and the single surface that coordinates them.

Each intent gets its own git worktree, branch, and session. Several AI-DLC runs proceed in parallel
without clobbering each other's files or state. The root `aidlc/` tree stops being a shared point of
contention, because each intent has its own.

A CAO parent sits above the runs. You watch and steer every intent from one place instead of
babysitting many terminals. The parent also carries what a single intent cannot see: which intent
waits for which, whether a contract change is backward compatible, and where two intents share a
release. The example already models this with typed dependencies such as `contract`, `data`, and
`release`, each tied to a lifecycle checkpoint.

## Keep the human gate human

This is the one rule not to break. AI-DLC's human-in-the-loop is a mechanism. At every decision that
needs a person, the run stops, writes a question packet, and waits for a human, and the answer is
attributed in the audit trail.

When CAO coordinates several intents, the orchestrator sits between each run and you. It must stay a
relay for those questions. It must never answer one itself. The example states this clearly, and the
runner never calls `question answer`, but a relay enforced by prompt alone is one drift away from a
fabricated, human-attributed approval.

So model that boundary in the protocol, not just in a prompt. The example's `question answer` now takes a
`--approval-id`, bound to a single-use approval record for that exact packet, instead of a free-text
`--answered-by` string. The answerer identity comes from that record, and no CLI subcommand in the
example lets an agent mint one. That's a real step up from a self-asserted name, and it's a shape worth
copying: route the question onto CAO's human-approval surface and bind the resolution to who actually
responded there. The sample's approval is still a writable file, so it only models that shape; a real
guarantee needs the record outside the agent's write reach or signed with a key the agent lacks.
CAO's human-approval surface provides that boundary. Memory and parallelism both assume the human
decisions were real, so getting this binding right is what keeps the rest honest.

## Summary

You can run AI-DLC on CAO today, and it gives AI-DLC two things worth having. CAO's memory layer
keeps what one intent learns across restarts, through the same `memory_store` and `memory_recall`
every CAO agent uses, and the orchestrator hands the reviewed learnings to the next intent when it
dispatches it. CAO's orchestration runs several intents at
once, each in its own worktree, coordinated and watched from one surface. The one rule to carry
across: wherever CAO sits between AI-DLC and you, keep the orchestrator a relay for AI-DLC's human
gates. Bind answers to a human approval record, and put that record outside the agent's write reach
when you need the boundary to be structural.

For the portfolio example itself, see
[`examples/aidlc-portfolio`](https://github.com/awslabs/cli-agent-orchestrator/tree/main/examples/aidlc-portfolio).

## About the author

Stan Fan (`fanhongy`) is a Solutions Architect at AWS. He works with teams on agentic systems and
developer tooling, with a particular interest in how agents accumulate and reuse context reliably
across sessions, models, and tools, without turning memory into an unbounded transcript.
