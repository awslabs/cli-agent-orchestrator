# Super agent: one conductor, a PR shepherd and standing OKs as data

This sample is a real working setup, generalised: you talk to **one** agent, the conductor,
and CAO runs everything else behind it. A PR shepherd looks after your own open pull
requests until they merge, an optional reviewer reviews other people's, and short-lived
workers do each job. The rules that matter are small scripts, not prompt text, and the kit
starts in observe mode.

The story behind it, with one real day, is in the blog post
[Running my coding work as a dark factory on CAO](https://awslabs.github.io/cli-agent-orchestrator/blog/2026/11/09/dark-factory-with-cao).

## Why
You can follow three, maybe five, agent sessions at once. Past that you lose track of which
agent is waiting on what. This kit puts your attention on one agent and writes down the habits
that kept a busy few days of real work safe:
- small agents sized to the job;
- briefs written as files;
- every claim checked before anything goes public;
- clear standing OKs, stored as data.

It borrows a few good ideas from DeepSeek's
[super-agent prototype](https://github.com/deepseek-ai/dsh-superagent-prototype):
- handle the inbox in batches;
- standing OKs as records;
- an observe-first mode;
- an outbox that can't double-post;
- a pause that survives a restart;
- a written restart routine.

And one hard rule: **never overload the machine.**

## What you get
```
                         you
                          │   one question at a time
                          ▼
                 ┌──────────────────┐
                 │  cao_conductor   │  Claude Code · opus
                 │  plans, delegates│  verifies everything
                 │  edits mandates  │
                 └──┬──────────┬────┘
        CAO messages│          │CAO messages
                    ▼          ▼
     ┌──────────────────┐   ┌──────────────────┐
     │   pr_shepherd    │   │ cao_pr_reviewer  │ (optional)
     │ Claude · sonnet  │   │ other people's   │
     │ YOUR open PRs    │   │ PRs              │
     └──┬───────────┬───┘   └──────────────────┘
        │           │ creates, checks, deletes
        │           ▼
        │   short-lived workers: fixer (Codex) · main-merger (Codex)
        │   pre-push reviewer (Claude, large) · publisher / armer (Claude, small)
        ▼
  pr-watch/tick_loop.sh: the PR poll; zero tokens; wakes the shepherd on real events
```
- **The conductor** does general engineering: issue → design → build → review → PR. It is the
  only agent that asks you anything.
- **The shepherd** looks after your own open PRs until they merge. It fixes review feedback and
  red CI, merges main in when needed, and arms auto-merge, all within the standing OKs in
  `mandates.json`. Anything else goes to the conductor, which asks you.
- **The reviewer** (optional) reviews other authors' PRs through two CAO review workflows you
  supply. It starts in shadow mode: it writes the reviews it would post to files, and posts
  nothing until the conductor switches it on. The conductor approves its workflow plans (M7)
  only if you turn M7 on.

| Path | What it is |
|---|---|
| `agents/cao_conductor.md`, `agents/pr_shepherd.md` | The two main agent profiles. They are short; the detail is in the playbooks. |
| `agents/cao_pr_reviewer.md` | The optional reviewer. It takes the heavy slot before each review run and reports to whoever created it. |
| `conductor-playbook.md`, `shepherd-playbook.md` | Step-by-step procedures the agents read on demand. |
| `rules/` | The watcher, auto-merge and hold-on-findings rules the shepherd follows. |
| `mandates.json` | Your standing OKs, observe/act mode and the pause switch. |
| `resources.json` | The machine-load thresholds. |
| `bin/resource_gate.py` | Prints GO, THROTTLE or STOP from live memory, swap, disk, load and terminal counts. |
| `bin/check_mandate.py` | Says whether an action is covered by a standing OK. Workers re-run it right before every push, post or arm, so a pause or a moved head stops them. |
| `bin/go_ledger.py` | The conductor records each `GO` it gives (and your one-off OKs) here, so a worker can check its exact approval. Only the conductor writes it; the scripts refuse any other record. All agents run as the same user, so this is a discipline control, not an operating-system boundary. |
| `bin/automerge_gate.py` | Says whether a PR may have its branch updated, or auto-merge armed, now. Remembers holds. |
| `bin/api_envelope.py` | Saves a GitHub answer together with its HTTP status, so the gate can tell "not protected" from "couldn't read it". |
| `bin/outbox.py` | Reserves each post before it goes out, so nothing is posted twice, even after a crash. |
| `bin/plan_hash_check.py` | Checks the reviewer's workflow scripts against the trusted hashes you recorded. |
| `bin/holds.py` | Lets the shepherd mark one clean review or comment, by its exact text, as not holding, after the conductor reads it and says GO. |
| `bin/remap_owners.py` | Hands an older watcher's `watch.json` owners to the shepherd, in one write. Only needed when you migrate. |
| `bin/state_schema.py` | The one strict check for every file under `state/`. Every script reads and writes its state through it, so a damaged row stops the script instead of being skipped. `check` lists any damaged file. |
| `pr-watch/` | The zero-token poll loop (`tick_loop.sh`, `poll.py`, `wake_rules.py`, `digest_store.py`), its runbook and `watch.example.json`. |
| `templates/` | Fill-in specs and briefs for the five kinds of worker. |
| `tests/`, `pr-watch/tests/` | Tests for the scripts, plus a mutation checker. |
| `state/`, `work/`, `reviews/` | Created at run time: outbox, holds, GO records, heavy-job slot, worker registry, briefs, the reviewer's ledger. Kept out of git by `.gitignore`. |

## Quick start
You need CAO with `cao-server` running, the Claude Code provider (the workers also use Codex),
`gh` logged in to GitHub, and Python 3.11+. The conductor and shepherd create short-lived
helpers, so turn on [ephemeral agents](../../docs/ephemeral-agents.md) (an experimental
feature, off by default) and allow the providers you want to use.

1. **Copy the kit** to a working folder. The docs call that folder `KIT`; the agents find it
   through `SUPER_AGENT_HOME`, which defaults to `~/super-agent`:
   ```sh
   cp -R examples/super-agent ~/super-agent
   export SUPER_AGENT_HOME=~/super-agent    # only if you chose another folder
   ```
2. **Make it yours.** The shipped files use two placeholders: `your-org/your-repo` and
   `your-login`. Replace both everywhere (profiles, playbooks, templates, `mandates.json`):
   ```sh
   cd "${SUPER_AGENT_HOME:-$HOME/super-agent}"
   grep -rl 'your-org/your-repo\|your-login' --include='*.md' --include='*.json' . \
     | xargs sed -i.orig -e 's#your-org/your-repo#OWNER/REPO#g' -e 's#your-login#LOGIN#g'
   find . -name '*.orig' -delete
   cp pr-watch/watch.example.json pr-watch/watch.json         # then edit repo, prs, identities
   mkdir -p reviews && echo '{"days": {}, "prs": {}}' > reviews/ledger.json
   ```
   (Use your real `OWNER/REPO` and GitHub `LOGIN` in that command.) Read `mandates.json` once:
   each record is a permission you are about to grant. Check `resources.json` too, and set
   `disk_path` to `/` if you are not on macOS.
3. **Install the profiles:**
   ```sh
   cao install agents/cao_conductor.md
   cao install agents/pr_shepherd.md
   cao install agents/cao_pr_reviewer.md      # optional
   ```
4. **Check the machine:**
   ```sh
   python3 bin/resource_gate.py
   ```
   It prints one line, such as `GO` or `THROTTLE pressure=2>=2 swap_gib=18.0>16`.
5. **Start the conductor:**
   ```sh
   cao launch --agents cao_conductor --session-name cao-conductor --working-directory "$PWD"
   ```
6. **Start the shepherd.** Tell the conductor "start the shepherd". It checks that no other
   `tick_loop.sh` is running (only one loop may run), starts the shepherd with `assign`, and
   tells you the shepherd's window.
7. The shepherd starts in **observe mode**: it proposes `WOULD …` actions and the conductor
   answers `GO` or `NO`. Each `WOULD` has its own id, `<N>-<action>-<head8>-<k>`, from the
   read-only `go_ledger.py next-id`; a renewal (after a policy bump, restart or expiry) gets a new
   id, because a GO id is never reused. Watch a few of these before switching to act mode.

## How a PR moves through the gates
1. The zero-token loop polls GitHub every 30 minutes and wakes the shepherd only on a real
   event: feedback, red CI, a review decision, a conflict or a state change on your PRs, and
   new commits or replies on PRs you reviewed. Everything else goes into a digest that rides
   along with the next wake.
2. The shepherd runs the resource gate, triages the event, and sends a fixer (a short-lived
   Codex agent) to the PR's worktree with a brief file.
3. It checks the fixer's claim itself: fast-forward ancestry, the remote head, the tests, a
   leak grep.
4. Before any push or post, `check_mandate.py` must say `ALLOW` (act mode) or the conductor
   must have recorded a `GO` (observe mode). A small publisher then pushes exactly the approved
   commit to exactly the approved URL, re-checking the pause, the policy version and the
   remote head right before it writes.
5. When the PR is approved and green, `automerge_gate.py` decides. It says `ARM` only when the
   approval covers the last real commit, nothing since then lists findings, every required check
   is present and green, and the PR is under the size cap. Even then it only arms GitHub's own
   auto-merge, and GitHub does the merge.

## Everyday controls (say these to the conductor)
| You want to… | Say | What changes |
|---|---|---|
| Stop all pushes, posts and arming at once | "pause" | `mandates.json` gets `"paused": true` with your reason. It holds across restarts until you lift it. |
| Resume | "resume" | `"paused": false`. Held work is re-checked before anything goes out. |
| Withdraw one standing OK | "revoke the re-run OK" (or name another) | That mandate gets `"revoked": true`. It is kept, not deleted, as a record. |
| Let the shepherd act on its own | "switch the shepherd to act" | `"shepherd_mode": "act"`. Mandate-covered actions then go out without a GO. |
| Back to proposals only | "switch the shepherd to observe" | `"shepherd_mode": "observe"`. |
| Give a new standing OK | Say exactly what it covers | A new mandate, no wider than your words. |

The conductor is the only agent that edits `mandates.json`, and only on your words. It copies
the old file to `state/` first, bumps `policy_version`, and saves the change to CAO memory.

You can always check the current state yourself:
```sh
python3 -m json.tool mandates.json | head -8
python3 bin/check_mandate.py --holder shepherd --action ff_fix_push --pr-author your-login --base main --repo your-org/your-repo
# OBSERVE M1-ff-fix-push   (exit 3)
```

## Standing OKs: the mandates and the GO ledger
Your permissions live in `mandates.json` as records: who holds each one, which action it
covers, and its scope (repo, PR author, base branch). A revoked OK stays in the file, marked
revoked. These are the sample's standing OKs:

| Id | Holder | Action |
|---|---|---|
| M1 | shepherd | Fast-forward fix pushes on your own PRs (never forced) |
| M2 | shepherd | A short factual reply listing the fixes |
| M3 | shepherd | `gh run rerun --failed` on your PRs, for outside download outages only. You are told every time. |
| M4 | shepherd | Merging main into your PR branch (a merge commit plus an FF push) when BEHIND or DIRTY, after the change-set check |
| M5 | shepherd | Update-branch (a merge from main, never a rebase), per `rules/auto-merge.md`, only when `automerge_gate.py --action update_branch` says `UPDATE_BRANCH`. It may run while auto-merge is armed; the worker reports the new head and stops. |
| M6 | shepherd | Arming squash auto-merge, per `rules/auto-merge.md` and `rules/hold-on-findings.md`, only when `automerge_gate.py --action arm` says `ARM` on a fresh run, and never re-arming |
| M7 | conductor | Approving the optional reviewer's plans for its two review workflows, after checking them. **Ships revoked**: set `"revoked": false` only if you run the reviewer. |
| M8 | shepherd | Clearing one held review or comment the keyword scan misread (`holds.py clear`), bound to its exact text, only after the conductor reads it and says GO, in any mode. Real findings are never cleared this way. |

`never_prs` in M5 and M6 lists PR numbers that must never auto-merge (for example a release
PR). It ships empty.

**These always need you:**
- force-push;
- a new PR or issue;
- closing anything;
- merging outside the gate;
- security decisions;
- any other GitHub text;
- releases;
- consent and keys.

When you OK one of these once (say, "open that PR"), the conductor records it as a **one-off**:
a GO bound to exactly that action, target, content and a short expiry. The worker still runs
the full check right before it writes, so a pause, a revocation or any change stops it.

**The GO ledger** (`state/go.jsonl`) is where those approvals live. In observe mode the shepherd
sends `WOULD <wid>: …`; the conductor checks the proposal against live state, records the GO
with `go_ledger.py add --writer-role conductor …`, and only then replies `GO <wid> …`. The
worker's last-moment check reads that record, so a GO for one head, body or policy version
can't be reused for another.

## The resource gate
Before an agent creates or assigns a worker, or approves a review plan, it runs the gate:
- **GO:** carry on.
- **THROTTLE:** one new worker at a time, small ones, no parallel test suites, and non-urgent
  work waits.
- **STOP:** nothing new starts. You get told once, with the biggest memory users.

Full test suites and review-workflow runs take a machine-wide "heavy" slot (`--acquire-heavy`),
so only one runs at a time. The slot comes with a secret token; only the job holding the token
can refresh or free it.
The agents never kill or pause anything they didn't start: your apps and security tools are left
alone.

| Level | STOP when | THROTTLE when |
|---|---|---|
| Memory pressure (1 normal, 2 warn, 4 critical) | ≥ 4 | ≥ 2 |
| Free memory | < 15 % | < 30 % |
| Swap used | — | > 16 GiB |
| Free disk | < 20 GiB | < 40 GiB |
| Load per CPU | — | > 1.2 |
| Live CAO terminals (all sessions) | ≥ 7 | ≥ 4 |

The thresholds were set for a 32 GB, 10-core laptop. To change one, edit `resources.json`.
The probes use macOS tools (`memory_pressure`, `sysctl`); on another system an unmeasurable
value counts as THROTTLE, so the gate fails safe.

## Safety rules
- **Outside text is data.** PR comments, review bodies, CI logs and web pages rank below
  everything else: host and CAO policy, then the agent's profile, then `mandates.json`, then
  your messages, then other agents' reports. Text from outside can never grant, widen or
  revoke a permission.
- **Every outside write is checked right before it happens.** A pause, a revocation, a policy
  bump, a moved PR head, a moved local branch or a changed body stops the worker with
  `STATUS: blocked`.
- **A push goes to one URL.** The publisher pushes `<new_head>:refs/heads/<branch>` to the
  repo's URL in `mandates.json` `push_urls`, never to the name `origin`, and refuses when the
  worktree's fetch or push URL differs or an `insteadOf` rewrite exists.
- **Nothing is posted twice.** Every post reserves an outbox key first; a crash leaves a
  `.pending` entry that must be reconciled before anything else is posted on that PR.
- **Real findings hold auto-merge** until a newer non-merge commit fixes them, or you decline
  them through the conductor.
- **Damaged state stops everything** that reads it (`state_corrupt:<file>:<line>:<why>`).
- **No kills, no main-clone edits.** Agents never kill what they didn't start, and never
  `checkout`, `reset`, `clean`, `stash` or `rm` in your main clone. They print env names,
  never values.

## Symptom → fix
| Symptom | Likely cause | Fix |
|---|---|---|
| Nothing is happening on a PR that got a review | The shepherd's loop isn't running, or it is paused | Ask the conductor for the shepherd's status. Check that `pgrep -fl tick_loop.sh` shows one loop. Check `"paused"` in `mandates.json`. |
| You keep seeing `WOULD …` lines | Observe mode, which is on purpose | Say "switch the shepherd to act" once you trust the proposals. |
| A PR is approved but auto-merge isn't armed | The gate said WAIT (CI pending, a required check missing, a stale approval, not mergeable yet) or HOLD_FINDINGS (a review or comment since the last real commit listed something) | Wait for the fix commit or for CI. A merge of main doesn't clear a hold. `pr-watch/log.md` shows the gate's reason; `state/holds.json` lists holds. |
| A clean approval ("0 P1, 0 P2, 0 P3. The previous P3 is fixed. Findings: None.") still holds | The keyword scan holds every mention of a P-level | The shepherd proposes `clear_hold`; the conductor reads the same text and says GO. Editing that text later makes it hold again. Real findings (a count such as "1 P3", a line such as "P2: …" or "nit: …", or a list under a Findings heading) are never cleared this way. |
| The gate says `WAIT required_contexts_unverified:…` | The required-check lists couldn't be read with a clear answer from GitHub | On purpose: it never arms on a guess. A later wake refetches; if it repeats, the shepherd tells the conductor. |
| A PR was armed but is still BEHIND | On purpose: arming and updating are separate steps (arm first) | A later step updates the branch. Whether the approval survives depends on your branch protection; a fix push dismisses it. |
| "ESCALATE_SIZE" about a PR | The PR changes more than 1500 lines | The conductor takes a second look, then goes ahead or asks you. |
| Agents say "THROTTLE" or "STOP" | The machine is busy (swap, pressure, disk or terminals) | Close apps you don't need. Agents wait on their own; nothing is killed. |
| A worker reports `STATUS: waiting` on a suite | Another heavy job holds the slot | It runs after that job finishes. If the slot is reported STALE, check the old job and remove `state/heavy/active` by hand. |
| A worker reports `STATUS: blocked` before a push or post | Its last-moment check failed: paused, an OK or GO revoked or expired, `mandates.json` changed, the PR head moved, the local branch moved past the approved commit, the body changed, or the push destination isn't exactly the repo's URL in `mandates.json` `push_urls` | On purpose. The shepherd re-plans from fresh state. |
| Two loops, or duplicate reports | A second `tick_loop.sh` was started | Stop the extra one. The loop takes a lock, and the shepherd refuses to start while another is running. |
| Posting on a PR is stuck at "PENDING" | A publisher stopped between posting and recording, or is still working | A publisher first proves the owner's terminal gone (404, recorded), only then searches the PR afresh for the exact body, and records that reservation as sent, or as "failed" if that later search finds nothing. A live publisher's slot is never taken. |
| The conductor refuses a review plan: "missing_baseline" or "changed" | A workflow script differs from the hash you trusted | You decide. The conductor never trusts a new hash on its own. |
| The conductor seems deaf to callbacks | The CAO inbox thinks it is busy | Each turn it reads the pending inbox itself. Send any message to wake it. |
| A script refuses with `state_corrupt:<file>:<line>:<why>` | A file under `state/` is damaged: a cut-off line, a missing field or a wrong type | On purpose: nothing acts on a damaged file. The conductor runs `python3 bin/state_schema.py check`, reads the named row, and repairs it by hand (or asks you). |

## Limits
- **State files:** out of scope is an agent deliberately forging a well-formed state row (every agent runs as the same OS user); in scope is accidental damage, such as a truncated line, a missing field or a wrong type, which always fails closed (`state_corrupt:<file>:<line>:<why>`), and the fix is a hand repair by the conductor after it inspects the file (`python3 bin/state_schema.py check` names the file and row).
- **One OS user:** the permission and state checks only work because every agent runs them.
  They are not an operating-system boundary.
- **Modelled, not live:** the tests model GitHub and CAO message delivery with files and mocks;
  they don't call GitHub.
- **macOS first:** the resource probes and some commands (`top -l 1`) are macOS ones.

## FAQ
**Why a separate shepherd?** PR babysitting is constant and routine. Kept away from the
conductor, it can't crowd out your requests, and its permissions stay narrow.

**Does this add polling?** One poll, and it costs no model tokens: `pr-watch/tick_loop.sh`.
The "watchdog" is just a check the shepherd makes when it's already awake. There is one
existing exception, if you run the optional reviewer: the reviewer's 45-minute review cycle.

**Can a PR comment talk an agent into doing something?** No. Outside text (comments, CI logs,
web pages) is treated as data and ranks below everything else. Permissions come only from
`mandates.json`, which only you can change, through the conductor.

**What if the machine reboots mid-push?** Both agents run a restart routine first. For each
action that may or may not have happened, oldest first, they check git, GitHub, the outbox and
the log before repeating it.

**Can the shepherd merge my PR?** Only by arming squash auto-merge, when the gate says ARM. That
requires:
- the approval is on the last real (non-merge) commit or later, with only merges of main after it;
- no review or comment since the last real (non-merge) commit lists findings, and no hold stands;
- every check is green, and every required check is there;
- GitHub says the PR is mergeable or just behind main (CLEAN, HAS_HOOKS or BEHIND) and auto-merge isn't on yet;
- GitHub answered both required-check lists clearly (rules 200; classic protection 200, or 404 "Branch not protected");
- it's under 1500 lines;
- it isn't in `never_prs`.

It never merges directly.

**Do I need the reviewer?** No. Without it, skip its profile, leave M7 revoked, and the
shepherd and conductor work the same. The poll loop still watches PRs listed in
`reviews/ledger.json`, which stays empty.

**Do I have to use all of it?** No. A good order is: the conductor and one short-lived helper
first; then the pause switch; then the shepherd, in observe mode.

**How do I run the tests?** Run them on the sample as shipped (or on a fresh copy): a few of
them check the shipped placeholders in `mandates.json`.
```sh
cd "${SUPER_AGENT_HOME:-$HOME/super-agent}"
python3 -m pytest -p no:cacheprovider -q tests                     # the kit's scripts and docs
PYTHONPATH=pr-watch python3 -m pytest -p no:cacheprovider -q pr-watch/tests   # the poll loop
python3 tests/mutate.py /tmp/sa-mut ag_    # flips each gate condition; all should be KILLED
```
The mutation checker is slow (it runs the suite once per mutant), so run it a chunk at a time
by label prefix (`ag_`, `cm_`, `ob_` …), each with a fresh scratch folder.
