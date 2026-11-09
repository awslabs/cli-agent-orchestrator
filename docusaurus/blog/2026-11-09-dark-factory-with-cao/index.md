---
title: "Turning my daily coding work into a dark factory with CAO"
authors: [fanhongy]
tags: [case-study, orchestration-patterns]
description: One conductor I talk to, a shepherd for my PRs, a reviewer for everyone else's, and rules written as code. Here's what it took to let them run with the lights off.
---

The reviewer's report opened with good news: every normal flow passed. Then it listed three ways the kit could still do something nobody had approved.

The first was a push. The kit lets one of my agents push fix commits to my own pull requests, under a standing OK I wrote down. Before it pushes, it checks the remote. But that check asked git where `origin` *fetches* from, and `git push origin` goes wherever the *push* URL points. Git lets those two be different.

So the reviewer set up two scratch repositories, both at the same commit, A. It pointed the push URL at the wrong one before any check ran, then followed the kit's real publishing steps. The check said ALLOW, and the push succeeded. The approved repository stayed at A; the other one received the new commit, B. Nothing noticed until after it was published.

The fix compares every push destination with the one URL in my standing OKs and refuses anything else, including a second push URL or a rewrite rule. The same steps now stop with `push_url_mismatch`, and neither repository moves.

That was one finding from one review. The kit got **seven** "not ready" verdicts before a reviewer finally called it ready.

Why build it? In my words: "This super agent is a piece of art, turn my daily coding work into a code dark-factory." A dark factory runs with the lights off. Mine runs my pull requests with as few human hands as I can safely manage, built on [CAO](https://github.com/awslabs/cli-agent-orchestrator), the open-source CLI agent orchestrator.

{/* truncate */}

## The three agents and their workers

I talk to one agent: the conductor. It plans, writes each task as a brief file, hands the work to short-lived agents, and checks every result before anything goes public. It's the only agent that asks me anything, one question at a time, so each fits on my phone.

Two more long-lived agents report to it, never to me:

- **The PR shepherd** looks after my own open pull requests until they merge. It reads feedback and red CI, decides what needs fixing, and creates the worker that fixes it. Anything outside routine, such as a serious finding, a security question or a force-push, goes to the conductor, which asks me.
- **The PR reviewer** reviews other people's pull requests. The conductor approves each of its review plans only after checking the PR head and comparing the review scripts with their trusted hashes.

The actual work is done by ephemeral agents, sized to the job and deleted when done: a fixer and a main-merger on Codex, a pre-push reviewer on a large Claude model, and a small Claude agent that publishes replies or arms auto-merge. Underneath, one zero-token poll loop wakes the shepherd only when something real happens.

![The user talks to the conductor; the conductor talks to the PR shepherd and the PR reviewer; the shepherd creates short-lived workers; a zero-token poll loop wakes the shepherd](./agents.svg)
*One conversation for me; everything else reports to the conductor.*

The way of working behind it is real. From 6 to 9 November, working this way, my account merged 8 pull requests, filed 5 issues, and posted reviews on 6 other people's pull requests.

## One PR's day

The everyday case is small. On 8 November, a maintainer approved #1263, a code-comment change, with one P3 nit about wording. An approval that lists a finding isn't a green light, so the PR was held.

A fixer changed the comment only, and a check confirmed the code's syntax tree was unchanged. It went out as a fast-forward push with a short reply. The push dismissed the approval, so the PR waited for a fresh one. The next day the maintainer merged it, with that fix as the head.

To be honest about timing: that round ran under my earlier PR watcher, before this kit existed. The kit writes that routine down; it didn't invent it. But the maintainer's real review text became a test fixture, and every later review of the kit replayed it, from hold to ready-to-arm.

![One PR's path: a new review wakes the shepherd, the P3 holds the PR, the resource gate says GO, a fixer works, the permission check runs right before the push, a fast-forward push and a reply go out, the approval is dismissed, and a maintainer merges; a dashed step shows where the kit would arm auto-merge after a clean re-approval](./pr-through-the-gates.svg)
*#1263 through the gates. The live round ended in a maintainer's merge; the dashed step is what the kit does next time.*

## Rules written as code, not prompts

An agent can talk itself past a rule in a prompt. It can't argue with an exit code. So every rule that matters is a small script, and every worker re-runs the permission check right before each push, post or arm, so a pause, a revoked OK or a moved PR head stops it.

My standing OKs are data, not prose: eight records, each saying who holds it, what action it covers, and its scope (this repo, my pull requests, the main branch). A revoked OK stays in the file, marked revoked. Only the conductor edits that file, only on my words, and it bumps the policy version every time.

Some things always need me: force-pushes, new pull requests or issues, closing anything, merging outside the gate, security decisions, other GitHub text, releases, and keys. A one-off OK from me is bound to that exact action, target and content, and expires soon.

The auto-merge gate arms a PR only when all of these hold:
- the approval covers the last real commit;
- no review or comment since then lists findings;
- every required check is present and green;
- GitHub gave a clear answer about which checks are required;
- the PR changes fewer than 1,500 lines.

Even then it only arms GitHub's own auto-merge; it never merges itself.

Three more ideas hold it together:
- **Observe mode.** Instead of acting, the shepherd proposes a "WOULD …" action, and the conductor answers GO or NO.
- **A durable pause.** "Pause" holds across restarts until I lift it.
- **A fixed order of authority.** CAO's own policy comes first, then the agent's profile, my standing OKs, my messages, and other agents' reports. PR comments, CI logs and web pages come last. They're data, and they can never grant, widen or revoke a permission.

## Keeping a 32 GB laptop alive

All of this runs on a 32 GB laptop, which made one rule non-negotiable. In my words: "Too many resource would lead the machine dead and entire workflow failed. Which is unbearable."

So an agent runs a resource gate first, before it creates a worker, approves a review plan or starts a full test suite. The gate reads memory pressure, free memory, swap, disk, load and the number of live agent terminals, then prints GO, THROTTLE or STOP:
- **STOP:** nothing new starts. That happens at critical memory pressure, under 15% free memory, under 20 GiB of free disk, or at seven live terminals.
- **THROTTLE:** one small worker at a time. That happens at warning pressure, above 16 GiB of swap, or at four terminals.

A reading it can't trust counts as THROTTLE, never GO. That rule came from a review: the first version treated a memory-pressure reading of 0 or −1 as healthy and said GO.

Full test suites and review runs share one "heavy" slot, held with a secret token, so only one runs at a time. And the agents never kill or pause anything they didn't start.

## Waking up for the right things

I also wanted fewer pointless wake-ups. I asked it to "pick the feedback and CI status from the PR that I owned and the pr that we reviewed have an update." The old loop also woke for other people's PRs falling behind main, and for comments on issues.

The new rules wake the shepherd for feedback, CI, review decisions, conflicts and state changes on my PRs, and for new commits or replies on PRs we reviewed. Everything else goes into a digest that rides along with the next real wake.

Replayed over 118 recorded polls, the old rules woke 25 times and the new ones 26:
- **4 old wakes were dropped,** all noise: other PRs falling behind main, and issue comments.
- **5 new wakes appeared:** new commits on a PR we had reviewed, which the old rules missed.
- **188 lines** went to the digest instead of waking anyone.

So the win is the right wake-ups, not far fewer. Every event the replay had to keep still woke the shepherd, including two approvals and that P3. A later review found that a crash between "seen" and "woken" could lose a wake; it now replays from a journal on restart.

## What we took from DeepSeek's super agent, and what we didn't

At the start of November, DeepSeek published a [super-agent research prototype](https://github.com/deepseek-ai/dsh-superagent-prototype) and a [design article](https://x.com/DeepSeekHarness/status/2116857257245987236): one always-on router plus one worker per ticket, answering with pull requests. Its README calls it research code. We studied it before writing the kit.

**What we took:** batching the inbox; a code filter before any model call; standing OKs as structured records; observe mode before automatic mode; an outbox that can't post twice; a pause that survives a restart; a written restart routine; one slot kept free for a person; and the rule that outside text is data.

**What we adapted:** their rule that an approval must cover the head commit, and their PR size cap, enforced in code. Theirs live in prompt text; their docs say "These instructions govern the agent, not GitHub's merge rules." We also kept a watchdog idea, but read-only.

**What we didn't take:** workers that act without approval as the operator's own user; "fix before anyone asked" pull requests; unprompted posting in shared channels; long-lived sessions per ticket (we prefer fresh agents); and one fixed model per role (we size each task).

**What we added:** an independent check of every worker's claim before anything goes public, a human gate on anything irreversible or public, and shared memory across sessions. Their only published evaluation is a small synthetic test that they call "indicative only", so I don't quote any numbers from it.

## The seven reviews that stopped the install

Every review was independent. Each reviewer hadn't written the kit, worked read-only, and reproduced each finding before reporting it. Over 8 and 9 November the reviews returned seven NOT-READY verdicts, then READY. The serious (P1) findings per verdict went 4, 3, 3, 1, 3, 2, 1, then 0. That's 17 P1s, 12 P2s and one P3 in all.

Besides the wrong-repository push, a few catches stand out:
- **A duplicate post after a crash.** A publisher could post and die before recording it; a search taken earlier said "not found", so a second publisher could post the same reply again.
- **Damaged records reviving a revoked permission.** Deleting one field from a revocation, or appending an exact copy of the original grant, turned DENY back into ALLOW.
- **A finding on an older commit.** A review on an older commit saying "P2: fix this before merge" was missed, and the gate said ARM.

The opposite failure mattered too: a gate so strict nothing clean gets through. The agent supervising the build caught that a clean approval ("0 P1, 0 P2, 0 P3 … Findings: None.") would hold a PR forever, and added clear-by-reading: one conductor GO bound to that exact text. The next review still found a deadlock: a re-approval saying "The previous P3 is fixed." stayed stuck.

The tests grew with the reviews. 138 kit tests passed at the first review and 1,291 at the last, plus 100 for the PR watch. Mutation runs deliberately break each gate condition and check that a test notices. In one round, two of 243 broken versions slipped through at first; the tests were tightened, and then all 243 were caught. Survivors in other rounds got the same treatment: a tighter test, or dead code removed.

None of this was quick: each round fixed the last findings, and a fresh reviewer found the next layer.

## What it still can't do

The kit is installed and running in observe mode. The shepherd proposes; the conductor says GO. Switching it to act mode is my call, after watching a few proposals.

Every agent runs as the same operating-system user, so the permission and state checks are discipline, not an operating-system boundary. A deliberately forged, well-formed record is out of scope; accidental damage, like a cut-off line, fails closed.

The reviews modelled GitHub merges and message delivery rather than running them live. And the big calls stay with people: I decide every new pull request or issue, every security question, and anything outside my standing OKs. Releases stay the maintainers' call.

## Try CAO

- [CAO on GitHub](https://github.com/awslabs/cli-agent-orchestrator): the orchestrator behind the conductor, the shepherd, the reviewer and their short-lived workers.
- [CAO's ephemeral-agents docs](https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/ephemeral-agents.md): size one-shot helpers to a task yourself.
- DeepSeek's [super-agent prototype](https://github.com/deepseek-ai/dsh-superagent-prototype) and [design article](https://x.com/DeepSeekHarness/status/2116857257245987236): where several of the ideas above came from.

For the phone half: [herdr-remote](https://github.com/dcolinmorgan/herdr-remote), the open-source project that carries CAO's notifications and approvals over Telegram.

## About the author

Stan Fan (`fanhongy`) is a Solutions Architect at AWS. He works with teams on agentic
systems and developer tooling, with a particular interest in how agents accumulate and reuse
context reliably across sessions, models, and tools, without turning memory into an
unbounded transcript.
