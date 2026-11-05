---
title: "How CAO shipped 32 reviewed PRs, approved from my phone"
authors: [fanhongy]
tags: [case-study]
description: Twelve days, 32 PRs, at least 18 independent reviews, and a Telegram bot that asked me one question at a time.
---

I was on a family holiday in Bali, half watching the kid and half watching my phone, when it started buzzing about CAO repo might need a look:

CI had gone red on two new HIGH-severity Dependabot alerts in the docs site's dependencies. Dependabot had already opened its own fix for each one, and both were red — each fix failed the scanner on the *other* alert.

A combined fix landed in one pull request, and it still wasn't green. This time on two MEDIUM alerts, in a dependency called minimatch.

CAO's architect — the agent whose job is to weigh security and policy calls, not just write code — looked into why a gate set for HIGH and CRITICAL severities was failing on a MEDIUM. In about 15 minutes, this agent was able to found the root cause: the CI step that uploads the security report ignores its own severity filter once it switches to a certain output format. Then it proved both MEDIUM alerts were unreachable code in this repo — it flagged, but never can be called.

The fix was a scoped well, and it named the exact advisory and the exact two files, lastely, it set an expiry date three months out so it can't be forgotten. This information/state was handoff to an independent reviewer checked it against the exact advisory and the live gate, with no shortcuts: no blocking issues, four minor nits to tidy up.

![A Telegram message from the Herdr bot, asking whether to push a fast-forward update, with a one-tap "Push it" button](./approval.png)
*The moment of the tap: the Herdr bot asking whether to push #1184's formatting fix as a fast-forward.*

Three of those nits went out in the next push, folded into one short follow-up commit. In about 10 minute, there was one more catch: the Code Quality check flagged a formatting slip in the new guard test, same agent picked that up, get it fixed, approved (by me) and push.

By the time I'm writing this, every check is green — Security Scan included — and, after another contributor's (thanks for reviewing my PR!) review and approval, it's merged.

That was one Saturday. Over twelve days, the same loop — [CAO](https://github.com/awslabs/cli-agent-orchestrator), the open-source CLI agent orchestrator, running a small team of agents against its own repo — shipped 32 pull requests by itself, ask me for approval to push the PR (or anything that reach the remote, or touch something that not expected).

{/* truncate */}

## The numbers

Here are some statics: 

| | |
|---|---|
| PRs opened | 32 (23 merged, 8 open, 1 closed) |
| Commits in those PRs | 218 |
| PRs merged in the window | 30 |
| Issues filed | 29 (8 closed, 21 open) |
| Window | 12 days, Oct 21 – Nov 1 |

A few honest caveats. Commits per PR ranged from 1 to 38 — some PRs carry nothing but review-fix commits stacked on top of the first one. Eight of the 32 are still open, waiting on review (by the time I am writing this blog post, all merged); A big PR, Kiro-related ones, also wait on an operator checklist I run myself. And none of this shipped unattended: a human approved every push, every PR, and every security call.

## The setup

At home, my Mac runs CAO's server and one conductor agent, inside [herdr](https://herdr.dev), the terminal session manager CAO's agents run in. CAO plans, sizes, and delegates every task; herdr is just where the terminals live. For notifications and approvals away from the desk, [herdr-remote](https://github.com/dcolinmorgan/herdr-remote) — an open-source project, credited and linked here — adds a small relay on the same Mac. A herdr plugin called `herdr-push` streams agent events into it, and the relay runs a Telegram bot carrying notifications and one-tap approvals to my phone. Most importantly: no public IP, no SSH — just outbound internet to Telegram.

![CAO's conductor plans and delegates at home; herdr-remote and its Telegram bot carry approvals to the phone](./workflow.svg)

The one rule that makes this work on a phone: the conductor asks **one question at a time**. A Telegram message with three questions in it gets truncated, so every approval is its own message, most urgent first. I learned that rule the slow way, by watching a multi-question prompt arrive on my phone as half a question. The question itself was never "what should we do" — the conductor had already decided that and handed it to a worker. It was more like "is it safe to do the thing we already agreed on."

Every task the conductor hands out is a file instead a chat message: the exact git commit to start from, the exact steps, the gates that must pass, and the one line a worker sends back when it's done. Workers report back through CAO's own inbox, and a message only lands when the receiver is free to read it — so the conductor sends one consolidated question per check-in, instead of three half-finished ones stacking up.

## Who does what

This is CAO's own agent model at work. The conductor never writes code (otherwise it would flooded my Telegram channel); everything else is delegated to a role CAO defines:

| Role | Job |
|---|---|
| Conductor | Plans, writes task briefs, delegates. No code of its own. |
| Ephemeral helper | A one-shot agent, sized small, medium, or large to the task, then deleted when it's done |
| Long-lived developer | Carries a multi-day track across several PRs |
| Reviewer | An independent agent, run through a CAO workflow with mutation testing (more on that below) |
| Architect | Weighs security and policy calls before anyone touches a file |
| Publisher | Pushes and opens PRs against an exact, checksum-pinned brief, never on its own judgment |
| PR watcher | Tracks every open PR, and auto-merges only our own approved, all-green ones |

## How CAO keeps the quality up

Before anything gets pushed, a fresh reviewer agent gets the exact commit range and the developer's own test evidence, with isolated context, the reviewer will review the code with a lot less bias, and it re-runs the tests itself. Findings come back ranked P1 (must fix), P2 (should fix), or P3 (nit), each with details: file, which line, and a concrete way it could go wrong, and go back to the developer as one fix commit with its own short follow-up review.

Part of every review is mutation testing: deliberately breaking the code in small, specific ways, then checking whether the tests notice — always against an unbroken "control" copy that has to pass cleanly first. 

A broken version the tests miss isn't a footnote. It becomes a new test.

The numbers, from every review still on disk. With several round of back and force, we reach some stage that ready to be reviewed by another maintainer (or agent), nearly all of findings are already fixed, a Publisher agent will wrapped up today with four minor nits, three of which went out in the very next push.

One catch worth naming. On the work to support Kiro CLI's newest engine (biggest PR pushed with my phone), a reviewer found that a file path with a specific character would make a standard Unix tool misread it as a variable assignment and launch the wrong program. This problem was caught before any human saw the code, and get fixed with a test. This across that track's second phase, three review rounds deliberately broke the isolation and launch code 111 separate ways. 

## What I never hand over

Four things gated by me, no matter how the rest is delegated: pushes, GitHub posts, security decisions, and live checks on my own machine. Everything else can be delegated and re-delegated. 

## What broke

In no particular order: there was a network issue in an agent mid-task; a review scratch directory filled the disk; a callback arrived twice and nearly got acted on twice; and a local security check passed while CI failed, because the local run used the documented flags, not the ones CI actually runs with.

## Jev picked each helper's model size

Partway through this, we ran a live test of CAO's decision platform: a feature that lets a small model decide how big a helper agent needs to be, instead of a human guessing every time. A third-party decider, Jev, sized three ephemeral helpers for three sample tasks — a docs typo, a bug fix, a security review. In shadow mode, where the platform watches but doesn't decide anything yet, Jev picked small, medium, and large, matching the answers those test cases expect — they're Jev's own routing cases, with expected sizes. Once switched on, CAO launched the three helpers on those tiers — haiku, sonnet, and opus — each decision landing in 200 to 500 milliseconds.

It's a small result, but the right shape: the tool we were building to size agents for other work, sizing agents for the work of building itself. The public record is in [issue #1068's results comment](https://github.com/awslabs/cli-agent-orchestrator/issues/1068) and [issue #1182](https://github.com/awslabs/cli-agent-orchestrator/issues/1182), which tracks what's left before anyone else can rely on it: a release, a guide, a published decider.

## Try CAO

- [CAO on GitHub](https://github.com/awslabs/cli-agent-orchestrator) — the orchestrator behind this entire workflow: the conductor, the cast of agent roles, and the workflow reviews.
- [CAO's ephemeral-agents docs](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/ephemeral-agents.md) — size one-shot helpers to a task yourself.
- [Issue #1182](https://github.com/awslabs/cli-agent-orchestrator/issues/1182) — what's left before decider-sized helpers, like Jev above, ship in a release.

For the phone half: [herdr-remote](https://github.com/dcolinmorgan/herdr-remote), the open-source project that carries CAO's notifications and approvals over Telegram.

## About the author

Stan Fan (`fanhongy`) is a Solutions Architect at AWS. He works with teams on agentic
systems and developer tooling, with a particular interest in how agents accumulate and reuse
context reliably across sessions, models, and tools, without turning memory into an
unbounded transcript.
