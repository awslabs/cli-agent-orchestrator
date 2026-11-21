# Super agent: one conductor, a PR shepherd and standing OKs as data

This example is a real working setup, generalised: you talk to **one** agent, the conductor,
and CAO runs everything else behind it. A PR shepherd looks after your own open pull
requests until they merge, and short-lived workers do each job. The rules that matter are
small scripts, not prompt text, and the kit starts in observe mode.

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
                 │  edits mandates  │  runs the tick supervisor
                 └────────┬─────────┘
              CAO messages│
                          ▼
                 ┌──────────────────┐
                 │   pr_shepherd    │  Claude Code · sonnet
                 │  YOUR open PRs   │
                 └──┬───────────┬───┘
                    │           │ creates, checks, deletes
                    │           ▼
                    │   short-lived workers: fixer (Codex) · main-merger (Codex)
                    │   pre-push reviewer (Claude, large) · publisher / armer (Claude, small)
                    ▼
  pr-watch/tick_supervisor.sh → tick_loop.sh: the PR poll; zero tokens; wakes the shepherd on real events
```
- **The conductor** does general engineering: issue → design → build → review → PR. It is the
  only agent that asks you anything. It also owns the poll loop: it starts the tick supervisor
  that keeps one loop running.
- **The shepherd** looks after your own open PRs until they merge. It fixes review feedback and
  red CI, merges main in when needed, and arms auto-merge, all within the standing OKs in
  `mandates.json`. Anything else goes to the conductor, which asks you.
- Reviewing other people's pull requests is not part of this kit. Use your own reviewers or a
  review bot for that; the poll loop can still watch the PRs you reviewed.

| Path | What it is |
|---|---|
| `agents/cao_conductor.md.tmpl`, `agents/pr_shepherd.md.tmpl` | The two agent profiles, as `{{TOKEN}}` templates that `bin/workplace.py render` fills in. They are short; the detail is in the playbooks. |
| `conductor-playbook.md`, `shepherd-playbook.md` | The judgment calls, each with its reason. The exact commands live in the helpers and the templates. |
| `rules/` | The watcher, auto-merge and hold-on-findings rules the shepherd follows. |
| `workplace.json`, `bin/workplace.py` | Your install's values (login, repo, CAO URL) in one place. You write `workplace.local.json` beside it; every script reads it through `workplace.py`. |
| `mandates.example.json` | The example standing OKs, each with a tier, plus observe/act mode and the pause switch. You copy it to `mandates.json`. |
| `resources.json`, `resources.linux.json` | The machine-load thresholds, for macOS and for Linux. |
| `bin/resource_gate.py` | Prints GO, THROTTLE or STOP from live memory, swap, disk, load and terminal counts, and hands out the heavy-job slots. |
| `bin/check_mandate.py` | Says whether an action is covered by a standing OK. Workers re-run it right before every push, post or arm, so a pause or a moved head stops them. |
| `bin/go_ledger.py` | The conductor records each `GO` it gives (and your one-off OKs) here, so a worker can check its exact approval. Only the conductor writes it; the scripts refuse any other record. All agents run as the same user, so this is a discipline control, not an operating-system boundary. |
| `bin/automerge_gate.py` | Says whether a PR may have its branch updated, or auto-merge armed, now. Remembers holds. |
| `bin/api_envelope.py` | Saves a GitHub answer together with its HTTP status, so the gate can tell "not protected" from "couldn't read it". |
| `bin/outbox.py` | Reserves each post before it goes out, so nothing is posted twice, even after a crash. |
| `bin/holds.py` | Lets the shepherd mark one clean review or comment, by its exact text, as not holding, after the conductor reads it and says GO. |
| `bin/changeset_check.py` | After a merge of main, compares the PR's change set before and after: `SAME`, or `DIFF: <files>`. |
| `bin/body_equals.py` | Checks that a posted body equals the prepared file: `BODY-OK` or `BODY-DIFF`. |
| `bin/leak_check.py` | The one leak check for every public body and diff: local paths, internal ids, account ids, host names. Prints `CLEAN` or `HIT <kind> line=<n>`, never the matched text. |
| `bin/check_brief.py` | Refuses a filled-in brief or assign message that still holds a `{placeholder}`. |
| `bin/test_env.py` | Runs a test command in a disposable, isolated environment and cleans it up by exact path. |
| `bin/writer_lease.py`, `bin/state_write.py` | The one-host writer lease (`state/WRITER`), and the only way agents write their own state files and logs: atomic, locked and lease-checked. |
| `bin/state_schema.py` | The one strict check for every file under `state/`. Every script reads and writes its state through it, so a damaged row stops the script instead of being skipped. `check` lists any damaged file. |
| `bin/remap_owners.py` | Hands an older watcher's `watch.json` owners to a new shepherd, in one write. |
| `bin/top_mem.py` | The five biggest memory users, for the one STOP message. |
| `pr-watch/` | The zero-token poll loop (`tick_supervisor.sh`, `tick_loop.sh`, `poll.py`, `wake_rules.py`, `digest_store.py`), its runbook and `watch.example.json`. |
| `templates/` | Fill-in specs and briefs for the five kinds of worker. |
| `tests/`, `pr-watch/tests/` | Tests for the scripts and the docs, plus a mutation checker. |
| `state/`, `work/`, `reviews/` | Created by you at setup, then filled at run time: outbox, holds, GO records, heavy-job slots, worker registry, briefs, the PRs you reviewed. Kept out of git by `.gitignore`. |

## Quick start
You need CAO with `cao-server` running, the Claude Code provider (the workers also use Codex),
`gh` logged in to GitHub, and Python 3.11+. The conductor and shepherd create short-lived
helpers, so turn on [ephemeral agents](../../docs/ephemeral-agents.md) (an experimental
feature, off by default) and allow the providers you want to use.

1. **Copy the kit** to a working folder. The docs call that folder `KIT`, and the poll loop's
   folder inside it `PB` (`KIT/pr-watch`):
   ```sh
   cp -R examples/super-agent ~/super-agent && cd ~/super-agent
   ```
2. **Make it yours.** Your login and repo go in one file, and your standing OKs start from the
   example:
   ```sh
   printf '{"github_login": "LOGIN", "repos": ["OWNER/REPO"]}\n' > workplace.local.json
   sed -e 's#your-org/your-repo#OWNER/REPO#g' -e 's#your-login#LOGIN#g' mandates.example.json > mandates.json
   mkdir -p state work reviews pr-watch/state pr-watch/reports
   python3 bin/writer_lease.py claim --host "$(python3 -c 'import socket; print(socket.gethostname())')"
   echo '{"days": {}, "prs": {}}' > reviews/ledger.json
   sed -e 's#your-org/your-repo#OWNER/REPO#g' -e 's#your-login#LOGIN#g' pr-watch/watch.example.json > pr-watch/watch.json
   python3 bin/workplace.py show
   ```
   (Use your real `OWNER/REPO` and GitHub `LOGIN` in those commands.) Read `mandates.json` once:
   each record is a permission you are about to grant. Check `resources.json` (macOS) or
   `resources.linux.json` (Linux) too: the thresholds and disk paths are examples.
3. **Render and install the profiles.** They are templates, so never `cao install` them
   directly:
   ```sh
   mkdir -p work/profiles
   for p in cao_conductor pr_shepherd; do
     python3 bin/workplace.py render agents/$p.md.tmpl work/profiles/$p.md --profile $p && cao install work/profiles/$p.md
   done
   ```
4. **Check the machine:**
   ```sh
   python3 bin/resource_gate.py
   ```
   It prints one line, such as `GO` or `THROTTLE pressure=2>=2 swap_gib=18.0>16`, and on a
   refusal a `hint:` line that says what to do.
5. **Start the conductor:**
   ```sh
   cao launch --agents cao_conductor --session-name cao-conductor --working-directory "$PWD"
   ```
6. **Start the shepherd.** Tell the conductor "start the shepherd". It checks that no other
   `tick_loop.sh` is running (only one loop may run), starts the shepherd with `assign`, and
   after the shepherd's `SHEPHERD READY` it starts `pr-watch/tick_supervisor.sh`, which keeps
   the loop running. It tells you the shepherd's window.
7. The shepherd starts in **observe mode**: it proposes `WOULD …` actions and the conductor
   answers `GO` or `NO`. Each `WOULD` has its own id, `<N>-<action>-<head8>-<k>`, from the
   read-only `go_ledger.py next-id`; a renewal (after a policy bump, restart or expiry) gets a new
   id, because a GO id is never reused. Watch a few of these before switching to act mode.

## How a PR moves through the gates
1. The zero-token loop polls GitHub every 30 minutes and wakes the shepherd only on a real
   event: feedback, red CI, a review decision, a conflict or a state change on your PRs, and
   new commits or replies on PRs you reviewed. Everything else goes into a digest that rides
   along with the next wake. The tick supervisor posts each wake to the shepherd's inbox and
   starts the next loop.
2. The shepherd runs the resource gate, triages the event, and sends a fixer (a short-lived
   Codex agent) to the PR's worktree with a brief file.
3. It checks the fixer's claim itself: fast-forward ancestry, the remote head, the tests, the
   change-set check after a main merge, and the leak check.
4. Before any push or post, `check_mandate.py` must say `ALLOW` (act mode) or the conductor
   must have recorded a `GO` (observe mode, and always for a tier-2 OK). A small publisher
   then pushes exactly the approved commit to exactly the approved URL, re-checking the pause,
   the policy version, the PR's live author and base, and the remote head right before it
   writes.
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
| Let the shepherd act on its own | "switch the shepherd to act" | `"shepherd_mode": "act"`. Tier-1 actions then go out without a GO; tier-2 ones still need one. |
| Back to proposals only | "switch the shepherd to observe" | `"shepherd_mode": "observe"`. |
| Give a new standing OK | Say exactly what it covers | A new mandate, no wider than your words. |

The conductor is the only agent that edits `mandates.json`, and only on your words. It backs up
the old file to `state/` first (through `state_write.py`), bumps `policy_version`, and saves the
change to CAO memory.

You can always check the current state yourself:
```sh
python3 -m json.tool mandates.json | head -8
python3 bin/check_mandate.py --holder shepherd --action ff_fix_push --pr-author LOGIN --base main
# OBSERVE M1-ff-fix-push   (exit 3)
```

## Standing OKs: the mandates and the GO ledger
Your permissions live in `mandates.json` as records: who holds each one, which action it
covers, its scope (repo, PR author, base branch) and its **tier**. A revoked OK stays in the
file, marked revoked.

- **Tier 1** is mechanical: the script checks the scope, heads, pins and caps (a fast-forward,
  the merge state, the head, the size cap), and the rest is the shepherd's judgment, written in the
  OK's conditions. In act mode it goes out without a GO.
- **Tier 2** needs someone to read text: a reply's wording, or whether a review really lists no
  finding. It needs the conductor's GO for each use, in act mode too.

These are the example's standing OKs:

| Id | Tier | Holder | Action |
|---|---|---|---|
| M1 | 1 | shepherd | Fast-forward fix pushes on your own PRs (never forced) |
| M2 | 2 | shepherd | A short factual reply listing the fixes |
| M3 | 1 | shepherd | `gh run rerun --failed` on your PRs, for outside download outages only. You are told every time. |
| M4 | 1 | shepherd | Merging main into your PR branch (a merge commit plus an FF push) when BEHIND or DIRTY, after the change-set check |
| M5 | 1 | shepherd | Update-branch (a merge from main, never a rebase), per `rules/auto-merge.md`, only when `automerge_gate.py --action update_branch` says `UPDATE_BRANCH`. It may run while auto-merge is armed; the worker reports the new head and stops. |
| M6 | 1 | shepherd | Arming squash auto-merge, per `rules/auto-merge.md` and `rules/hold-on-findings.md`, only when `automerge_gate.py --action arm` says `ARM` on a fresh run, and never re-arming |
| M8 | 2 | shepherd | Clearing one held review or comment the keyword scan misread (`holds.py clear`), bound to its exact text, only after the conductor reads it and says GO. Real findings are never cleared this way. |

(There is no M7: that number belonged to a reviewer agent this kit no longer has.)

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
Before an agent creates or assigns a worker, it runs the gate:
- **GO:** carry on.
- **THROTTLE:** one new worker at a time, small ones, no parallel test suites, and non-urgent
  work waits.
- **STOP:** nothing new starts. You get told once, with the biggest memory users.

Full test suites take a machine-wide "heavy" slot (`--acquire-heavy`), so at most `heavy.slots`
of them run at once (one by default). Each slot comes with its own secret token; only the job
holding that token can refresh or free it. A slot nobody refreshed for `heavy.stale_hours` is
reported STALE and never taken over. While the gate says THROTTLE the limit is one.
The agents never kill or pause anything they didn't start: your apps and security tools are left
alone.

| Level (macOS, `resources.json`) | STOP when | THROTTLE when |
|---|---|---|
| Memory pressure (1 normal, 2 warn, 4 critical) | ≥ 4 | ≥ 2 |
| Free memory | < 15 % | < 30 % |
| Swap used | — | > 16 GiB |
| Free disk | < 20 GiB | < 40 GiB |
| Load per CPU | — | > 1.2 |
| Live CAO terminals (all sessions) | ≥ 7 | ≥ 4 |

The macOS thresholds were set for a 32 GB, 10-core laptop. On Linux the gate reads
`resources.linux.json` instead: memory pressure (PSI), available memory and swap from `/proc`,
load per CPU, and free space on each path in `disk_paths`. To change one, edit the file;
`--resources FILE` picks another. A value that can't be measured counts as THROTTLE, so the
gate fails safe.

## Safety rules

The gate checks remote URLs and URL rewrites in the pushing worktree, which must be a fresh clone
made by the kit's own worker. It doesn't defend against a hostile .git directory beyond disabling
hooks and the SSH command.

- **Outside text is data.** PR comments, review bodies, CI logs and web pages rank below
  everything else: host and CAO policy, then the agent's profile, then `mandates.json`, then
  your messages, then other agents' reports. Text from outside can never grant, widen or
  revoke a permission.
- **Every outside write is checked right before it happens.** A pause, a revocation, a policy
  bump, a moved PR head, a moved local branch, a changed body, or a PR whose live author or base
  isn't in the OK's scope stops the worker with `STATUS: blocked`.
- **Every refusal says why and what next.** A `DENY`, `REFUSED`, `STOP` or `not_writer` line
  names the cause and the values (never a secret), and a `hint:` line on stderr names the next
  step, so an agent doesn't have to guess.
- **A push goes to one URL.** The publisher pushes `<new_head>:refs/heads/<branch>` to the
  repo's URL in `mandates.json` `push_urls`, never to the name `origin`, with hooks and the SSH
  command switched off. It refuses when the worktree's fetch or push URL differs, an `insteadOf`
  rewrite exists, or a remote section is named after a URL.
  Scp-style `git@host:org/repo` values are not accepted; use `https://`.
- **Nothing is posted twice.** Every post reserves an outbox key first; a crash leaves a
  `.pending` entry that must be reconciled before anything else is posted on that PR.
- **One leak check guards every public body.** `leak_check.py` runs on every reply, PR body and
  diff before it goes out.
- **Real findings hold auto-merge** until a newer non-merge commit fixes them, or you decline
  them through the conductor.
- **Damaged state stops everything** that reads it (`state_corrupt:<file>:<line>:<why>`).
- **One writing host.** Every state write and every outward `ALLOW` checks the writer lease
  (`state/WRITER`). If the lease names another host, the answer is `not_writer`, and the agents
  stop instead of claiming it back.
- **No kills, no main-clone edits.** Agents never kill what they didn't start, and never
  `checkout`, `reset`, `clean`, `stash` or `rm` in your main clone. They print env names,
  never values.

## Symptom → fix
| Symptom | Likely cause | Fix |
|---|---|---|
| Nothing is happening on a PR that got a review | The tick supervisor or its loop isn't running, or the kit is paused | Ask the conductor for the shepherd's status. Check that `pgrep -fl tick_loop.sh` shows one loop and `pr-watch/state/tick_supervisor.pid` names a live supervisor. A `pr-watch/state/tick_supervisor.stopped` file with a `NOT_WRITER` line means it stopped because the writer lease moved; a `tick_supervisor.pending` file holds wakes not delivered yet. Check `"paused"` in `mandates.json`. |
| You keep seeing `WOULD …` lines | Observe mode, which is on purpose; or a tier-2 action, which always asks | Say "switch the shepherd to act" once you trust the proposals. Tier-2 actions keep asking. |
| A PR is approved but auto-merge isn't armed | The gate said WAIT (CI pending, a required check missing, a stale approval, not mergeable yet) or HOLD_FINDINGS (a review or comment since the last real commit listed something) | Wait for the fix commit or for CI. A merge of main doesn't clear a hold. `pr-watch/log.md` shows the gate's reason; `state/holds.json` lists holds. |
| A clean approval ("0 P1, 0 P2, 0 P3. The previous P3 is fixed. Findings: None.") still holds | The keyword scan holds every mention of a P-level | The shepherd proposes `clear_hold`; the conductor reads the same text and says GO. Editing that text later makes it hold again. Real findings (a count such as "1 P3", a line such as "P2: …" or "nit: …", or a list under a Findings heading) are never cleared this way. |
| The gate says `WAIT required_contexts_unverified:…` | The required-check lists couldn't be read with a clear answer from GitHub | On purpose: it never arms on a guess. A later wake refetches; if it repeats, the shepherd tells the conductor. |
| A PR was armed but is still BEHIND | On purpose: arming and updating are separate steps (arm first) | A later step updates the branch. Whether the approval survives depends on your branch protection; a fix push dismisses it. |
| "ESCALATE_SIZE" about a PR | The PR changes more than 1500 lines | The conductor takes a second look, then goes ahead or asks you. |
| Agents say "THROTTLE" or "STOP" | The machine is busy (swap, pressure, disk or terminals) | Close apps you don't need. Agents wait on their own; nothing is killed. |
| A worker reports `STATUS: waiting` on a suite | Other heavy jobs hold every slot (the refusal lists each holder and its age) | It runs after one of them finishes. If a slot is reported STALE, check that old job and remove only its holder file under `state/heavy/active/` by hand. |
| A worker reports `STATUS: blocked` before a push or post | Its last-moment check failed: paused, an OK or GO revoked or expired, `mandates.json` changed, the PR head moved, the PR's author or base is out of scope, the local branch moved past the approved commit, the body changed, or the push destination isn't exactly the repo's URL in `mandates.json` `push_urls` | On purpose. The `hint:` line says what to do; the shepherd re-plans from fresh state. |
| Anything says `not_writer` | `state/WRITER` is missing or names another host | On purpose: two writing hosts is the one failure nothing later can undo. Check with `python3 bin/writer_lease.py show`. Only you move the lease, after stopping the writers on the old host. |
| Two loops, or duplicate reports | A second `tick_loop.sh` was started | Stop the extra one. The loop takes a lock, the supervisor refuses to start twice, and the shepherd never starts a loop. |
| Posting on a PR is stuck at "PENDING" | A publisher stopped between posting and recording, or is still working | A publisher first proves the owner's terminal gone (404, recorded), only then searches the PR afresh for the exact body, and records that reservation as sent, or as "failed" if that later search finds nothing. A live publisher's slot is never taken. |
| A brief is refused with `LEFT …` | `check_brief.py` found a `{placeholder}` that wasn't filled in | Fill it with the value `python3 bin/workplace.py show` prints, and check again. |
| The conductor seems deaf to callbacks | The CAO inbox thinks it is busy | Each turn it reads the pending inbox itself. Send any message to wake it. |
| A script refuses with `state_corrupt:<file>:<line>:<why>` | A file under `state/` is damaged: a cut-off line, a missing field or a wrong type | On purpose: nothing acts on a damaged file. The conductor runs `python3 bin/state_schema.py check`, reads the named row, and repairs it by hand (or asks you). |

## Limits
- **State files:** out of scope is an agent deliberately forging a well-formed state row (every agent runs as the same OS user); in scope is accidental damage, such as a truncated line, a missing field or a wrong type, which always fails closed (`state_corrupt:<file>:<line>:<why>`), and the fix is a hand repair by the conductor after it inspects the file (`python3 bin/state_schema.py check` names the file and row).
- **One OS user:** the permission and state checks only work because every agent runs them.
  They are not an operating-system boundary.
- **One host:** the writer lease is a cooperative fence for one writing host, not a distributed
  lock.
- **Modelled, not live:** the tests model GitHub and CAO message delivery with files and mocks;
  they don't call GitHub.

## FAQ
**Why a separate shepherd?** PR babysitting is constant and routine. Kept away from the
conductor, it can't crowd out your requests, and its permissions stay narrow.

**Does this add polling?** One poll, and it costs no model tokens: `pr-watch/tick_loop.sh`,
kept running by `tick_supervisor.sh`. The "watchdog" is just a check the shepherd makes when
it's already awake.

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

**What about reviewing other people's PRs?** An earlier version of this kit had a reviewer
agent under the conductor. It's gone: reviews are better done by your own reviewers or a
review bot that runs on its own. The poll loop still watches the PRs listed in
`reviews/ledger.json`.

**Do I have to use all of it?** No. A good order is: the conductor and one short-lived helper
first; then the pause switch; then the shepherd, in observe mode.

**How do I run the tests?** Run them on the example as shipped (or on a fresh copy): a few of
them check the shipped placeholders in `mandates.example.json` and `workplace.json`.
```sh
cd examples/super-agent
python3 -m pytest -p no:cacheprovider -q tests                     # the kit's scripts and docs
PYTHONPATH=pr-watch python3 -m pytest -p no:cacheprovider -q pr-watch/tests   # the poll loop
python3 tests/mutate.py "${TMPDIR:-/tmp}/sa-mut-ag" ag_    # flips each gate condition; all should be KILLED
```
The mutation checker is slow (it runs the suite once per mutant), so run it a chunk at a time
by label prefix (`ag_`, `cm_`, `ob_` …), each with a fresh scratch folder.
