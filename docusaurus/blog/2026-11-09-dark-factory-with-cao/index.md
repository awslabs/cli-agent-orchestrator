---
title: "Running my coding work as a dark factory on CAO"
authors: [fanhongy]
tags: [case-study, orchestration-patterns]
description: I can only hold a few issues in my head, so I give all my attention to one agent, the conductor, and let CAO hold the rest. Here's one real day of it, and how it keeps running with the lights off.
---

On 9 November I ran my coding work from my phone. I gave one agent a few unrelated jobs in one conversation. All of it cost me a few one-line answers: yes, test it; yes, file it.

It wasn't always like that. For months the bottleneck was me.

{/* truncate */}

:::tip TL;DR
I can follow three, maybe five, agent sessions at once, so I talk to one conductor agent and let CAO run a PR shepherd, a PR reviewer and short-lived workers behind it. That makes it a lot easier. The rules that matter are small scripts instead of prompts, and the kit starts in observe mode. Seven reviews said NOT-READY before one said READY, and it still runs as one OS user.

Unfamiliar word? There is a short glossary at the end.
:::

## Too many agents to follow

When I run many agents, I can follow three to five sessions at once, and that's barely. Past that, I lose track of which agent is waiting on what, and which one is about to do something I didn't mean.

Every agent needs me once in a while: a question here, an approval there. Ten agents meant ten small interruptions, and none of them waited politely. Keeping up with all of them was impossible.

And under all of it was a worry. If I let agents work while I was away, what would they do wrong without me watching?

## One conductor, with CAO behind it

I stopped trying to watch every agent. Now all my attention goes to **one** agent, the conductor. It plans the work, hands each job to a short-lived helper agent, checks what comes back, and is the only agent that ever asks me a question.

Everything else sits behind it, and [CAO](https://github.com/awslabs/cli-agent-orchestrator), the open-source CLI agent orchestrator, is what holds it up. The conductor gives each job to a helper sized to that job. A "shepherd" agent looks after my own pull requests. A "reviewer" agent reviews other people's. My standing permissions are written as data, not prose. And a loop that costs no model tokens keeps it going day and night.

That's what I mean by a dark factory: a factory that runs with the lights off. I put it to the agent in one line: "This super agent is a piece of art, turn my daily coding work into a code dark-factory."

The whole setup is in the CAO repo as a sample, [`examples/super-agent`](https://github.com/awslabs/cli-agent-orchestrator/tree/main/examples/super-agent): the three agent profiles, their playbooks, the scripts and the poll loop, with my names and paths taken out.

## 9 November, from my phone

The day started with a handover. The conductor I'd been talking to had passed its work to a new one. A conductor's memory is one long conversation, so a handover has to be a file. The old conductor wrote down everything in it: the open pull requests, who was waiting on whom, and the rules that should follow. The new conductor read it, started its two long-lived helpers, and wrote a short file back to say it had taken over. The old one was watching for exactly that file. Before it left, it had stopped its poll loop, because only one may run at a time.

Then I started handing it work. Get Kiro CLI 3.1.0 ready to use, with a status comment on the issue that tracks it. And give me a guide an agent could follow to set up ephemeral agents (short-lived helpers) and the Jev decider (the example decider in CAO's setup page).

The conductor gave each job to its own short-lived agent and checked each result. Along the way it caught mistakes before they reached me.

### The runbook that was wrong

Kiro CLI 3.1.0 is installed on my laptop, but CAO refuses to launch Kiro v3 on that version by default. An operator has to run a 19-check checklist on it live first (or opt in to unverified versions). That run is mine to do, so the conductor had an agent write me a step-by-step runbook.

The runbook said CAO adds its own MCP server to an installed agent at launch. The conductor didn't take its word for it. It copied a built-in profile, installed the copy into a scratch folder, and read the file CAO produced.

The server was there because the profile declares it. The ten test profiles in the draft didn't declare it. Several checks would have failed, and for the wrong reason. I would have spent an evening debugging the runbook instead of Kiro.

The same check turned up two more problems. Two test agents were meant to show "no file writes" and "no web access", but they still had a shell, and a shell can do both legitimately. I would have read a shell write as a FAIL. Those two lost the shell. And a clean-up step used a command that prints the environment of every matching process, tokens included. The fix uses checks that print no values.

### The guide that already existed

The second job went differently. The guide I'd asked for already existed: a docs page merged the day before. So the conductor offered to test the page instead of writing a new one. I said yes.

An agent followed the page end to end, the way an agent would for a real user. It used a sandbox with its own install and its own server, and it stopped where the page needs my consent. Its verdict was "Yes, with friction". It found two real gaps.

First, the restart check can run before the server is up. An agent would read a slow start as a failed restart and stop to ask. Second, the page lets the agent run a key check, but Jev's key is stored once per OS user. On a test install next to the real one, the decider's key commands would act on my real key.

The conductor asked me once, then filed the issue. A docs-only fix was written and tested in bash and zsh. Its pull request went up as soon as I said yes, and it was merged that evening.

## How the agents are arranged

To recap, I only talk to one agent. It writes each task as a brief file, hands the work to short-lived agents, and expects each to report back in a fixed format. Nothing goes public until the conductor has checked the result.

Two more long-lived agents report to it, never to me.

The **PR shepherd** looks after my own open pull requests until they merge. It reads feedback and red CI, decides what needs fixing, and creates the worker that fixes it. Anything outside routine, such as a security question or a force-push, goes up to the conductor, which asks me.

The **PR reviewer** runs an independent review on pull requests my colleagues open. It keeps watching the repo continuously.

The shepherd's workers are sized to the job and deleted when done: a fixer and a main-merger on Codex, a pre-push reviewer on a large Claude model, and a small Claude agent that publishes replies or arms auto-merge.

![The user talks to the conductor; the conductor talks to the PR shepherd and the PR reviewer; the shepherd creates short-lived workers; a zero-token poll loop wakes the shepherd](./agents.svg)
*One conversation for me; everything else reports to the conductor.*

This isn't a demo. From 6 November until early on 9 November (UTC), my account merged
8 pull requests, filed 5 issues, and posted reviews on 5 other people's pull requests.

## One small pull request, start to finish

Most days are small. On 8 November, a maintainer approved #1263, a code-comment change, with one minor wording nit. Reviewers grade findings from P1 (serious) to P3 (minor), and this was a P3. An approval that lists a finding isn't a green light, so the PR was held.

A fixer changed the comment and nothing else. A check confirmed the code's syntax tree was unchanged. The fix went out as a fast-forward push, meaning it added a commit on top without rewriting history, with a short reply. On that repo the push dismissed the old approval. (Whether a push does that depends on the repo's branch protection setting for stale approvals.) The next day the maintainer merged it, with that fix as the head.

To be honest about timing: that round ran under my earlier PR watcher, before this kit existed. But the maintainer's real review text became a test fixture, and every later review of the kit replayed it.

![How the kit would handle #1263's real P3 review: a wake and hold, a resource gate, a fixer, permission checks, a fast-forward push and reply, then a modelled clean re-approval and conductor clear before the auto-merge gate can say ARM](./pr-through-the-gates.svg)
*How the kit would handle #1263's real review. The live round used the earlier watcher and ended in a maintainer's merge.*

## Why I needed scripts

A rule written in a prompt only works if the agent keeps choosing to follow it. An agent can talk itself past it with a reason that sounds fine at the time. I didn't want anything that matters to depend on that, so I moved those rules into small scripts that return explicit results. I call them gates.

The first script checks permission. Every worker re-runs it right before each push, post or arm. If I paused, revoked an OK or the PR head moved, the worker stops on the spot, whatever it had planned.

The second problem was where my permissions live. Prose in a prompt can be reread and reargued, so my standing OKs are data: eight records, each saying who holds it, what action it covers, and its scope. A revoked OK stays in the file, marked revoked. Only the conductor edits that file, only on my words, and it bumps the policy version every time. Some things always need me: force-pushes, new pull requests or issues, closing anything, merging outside the gate, security decisions, other GitHub text, releases, and keys. A one-off OK from me is bound to that exact action, target and content, and expires soon.

The auto-merge gate is the strictest script. It arms a PR only when the approval covers the last real commit, nothing since then lists findings, every required check is present and green, GitHub answered clearly which checks are required, and the PR changes no more than 1,500 lines. Even then it only arms GitHub's own auto-merge, which must be enabled on the repo, and lets GitHub do the merge. The kit never merges anything itself.

The last problem was text from outside. A PR comment can say anything, including "ignore your instructions". So the text has a fixed rank. CAO's own policy comes first, then the agent's profile, my standing OKs, my messages and other agents' reports. PR comments, CI logs and web pages come last. They're data, and they can never grant a permission.

## How the poll loop decides to wake the shepherd

An always-on factory has a quiet cost: something has to notice when a pull request changes. I don't want a model reading GitHub all night, so a poll loop does it with zero model tokens. I told it what I cared about: "pick the feedback and CI status from the PR that I owned and the pr that we reviewed have an update."

It wakes the shepherd for feedback, CI that needs action, review decisions, conflicts and state changes on my PRs. It also wakes it for new commits, replies and state changes on PRs we reviewed. A PR falling behind main wakes it only when approved or already armed. Everything else goes into a digest that rides along with the next wake. If the digest's oldest line has waited more than 24 hours, it causes one wake itself.

I replayed 118 recorded polls through the old rules and the new ones. The old rules woke 25 times and the new ones 26. That sounds like no change, but four noisy wakes were dropped, five missed ones appeared (new commits on PRs we had reviewed), and 188 lines went to the digest instead of waking anyone. The gain is in which wakes happen.

## Restarts, pauses and observe mode

Agents will crash sometimes. Laptops reboot. Conductors get replaced. Four more habits keep the factory going.

**Restart recovery.** Both long-lived agents run a restart routine before anything else. For each action that may or may not have happened, oldest first, they check git, GitHub, the outbox and the log before repeating it. Every post is reserved in an outbox first, so nothing goes out twice. Wakes replay from a journal.

**Pause and resume.** I say "pause" and every push, post and arm stops. It holds across restarts until I say "resume". Held work is re-checked before anything goes out.

**Observe, then act.** The shepherd starts in observe mode. Instead of acting, it proposes a "WOULD …" action, and the conductor answers GO or NO. Switching it to act mode is my call, after watching a few proposals.

**One question at a time.** Only the conductor asks me anything, one question per message, so each fits on my phone. I answer over Telegram, and the conductor stays quiet when nothing has changed.

## What I took from DeepSeek's super agent

At the end of October, DeepSeek published a [super-agent research prototype](https://github.com/deepseek-ai/dsh-superagent-prototype) and a [design article](https://x.com/DeepSeekHarness/status/2116857257245987236): one always-on router plus one worker per ticket, answering with pull requests. Its README calls it research code.

What we took:

- batching the inbox
- a code filter before any model call
- standing OKs as records
- observe mode first
- an outbox that can't post twice
- a pause that survives a restart
- a written restart routine
- the rule that outside text is data

What we adapted:

- their rule that an approval must cover the head commit
- their PR size cap, enforced in code (theirs lives in prompt text; their docs say "These instructions govern the agent, not GitHub's merge rules.")

What we didn't take:

- workers that run shell commands without approval, as the operator's OS user
- unprompted fix pull requests (the article shows them; the prototype gates them behind team-written rules)
- long-lived sessions per ticket (we prefer fresh agents)
- one fixed model per role (we size each task)

Their only published evaluations are two small experiments on synthetic workloads, which they call "indicative only", so I don't quote any numbers from them.

## Independent reviewers found real holes

Reviewers who hadn't written the kit kept finding real holes in it. The worst one would have sent my code to a repository I never approved.

They went over the kit read-only and reproduced each finding before reporting it. Over 8 and 9 November they returned seven NOT-READY verdicts, then READY. In all: 17 serious (P1) findings, 12 P2s and one P3.

The worst one worked like this. Before pushing, the kit asked git where `origin` *fetches* from.
But a push goes wherever the *push* URL points, and git lets those two differ. A reviewer pointed
the push URL at a second scratch repository, and the push landed there. My kit had checked the
wrong address and felt safe. The fix compares every push destination with the one URL in my
standing OKs, and refuses anything else.

If you build guardrails for agents, have someone who didn't write them try to break them.
The tests grew with the reviews: 138 kit tests at the first review, 1,338 now, plus
101 for the PR watch. I also run mutation tests, which break each gate condition on purpose
and check that a test notices.

## What it can't do yet

The kit is running in observe mode. The shepherd proposes; the conductor says GO.

Every agent runs as the same operating-system user, so the permission and state checks only work if every agent runs them. They are not an operating-system boundary. A deliberately forged, well-formed record is out of scope. Accidental damage, like a cut-off line, fails closed.

The reviews modelled GitHub merges and message delivery rather than running them live. And the big calls stay with people. I decide every new pull request or issue, every security question, and anything outside my standing OKs. Releases stay the maintainers' call.

## Start small

You don't need the whole kit to feel the shift. Add one piece at a time:

1. One conductor profile and one short-lived helper, using CAO's ephemeral agents. See what changes when you only talk to one agent.
2. Add a pause switch that survives a restart.
3. Only then add a shepherd or reviewer, and start it in observe mode.

Each piece is in [`examples/super-agent`](https://github.com/awslabs/cli-agent-orchestrator/tree/main/examples/super-agent), with a quick start. Its standing OKs ship as placeholders, and the shepherd starts in observe mode.

## Links

- [CAO on GitHub](https://github.com/awslabs/cli-agent-orchestrator): the orchestrator behind the conductor, the shepherd, the reviewer and their short-lived workers.
- [CAO's ephemeral-agents docs](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/ephemeral-agents.md): an experimental feature (off by default) that lets an agent create a short-lived helper with a model tier and effort sized to the task.
- DeepSeek's [super-agent prototype](https://github.com/deepseek-ai/dsh-superagent-prototype) and [design article](https://x.com/DeepSeekHarness/status/2116857257245987236): where several of the ideas above came from.

## Glossary

- **Conductor:** the one agent I talk to. It plans, hands out jobs and checks results.
- **Shepherd:** a long-lived agent that looks after my own pull requests.
- **Reviewer:** a long-lived agent that reviews other people's pull requests.
- **Worker / ephemeral agent:** a short-lived helper, sized to one job and deleted after.
- **Gate:** a small script that returns an explicit answer, instead of a rule in a prompt.
- **Standing OK:** a permission I granted, stored as a data record.
- **P1 / P2 / P3:** finding severity, from serious to minor.
- **Fast-forward push:** a push that adds commits on top without rewriting history.
- **Arm auto-merge:** switch on GitHub's own auto-merge for a PR. GitHub does the merge.
- **Observe mode:** the agent proposes a "WOULD ..." action; it does not act.
- **Poll loop / digest:** a zero-token loop that watches GitHub; the digest batches quiet updates.
- **Outbox:** a record that reserves each post first, so nothing goes out twice.
- **Mutation test:** break a gate condition on purpose and check that a test notices.

## About the author

Stan Fan (`fanhongy`) is a Solutions Architect at AWS. He works with teams on agentic
systems and developer tooling, with a particular interest in how agents accumulate and reuse
context reliably across sessions, models, and tools, without turning memory into an
unbounded transcript.
