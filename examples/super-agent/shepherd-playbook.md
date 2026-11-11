# Shepherd playbook

The shepherd's profile (`agents/pr_shepherd.md`) says *what* to do. This file says *how*.
`KIT` = `${SUPER_AGENT_HOME:-$HOME/super-agent}` (your copy of this sample), `PB` = `KIT/pr-watch`,
`REPO` = your repo (`your-org/your-repo` in the shipped files), `API` = `http://localhost:9889`.
Rule files: `KIT/rules/watcher.md`, `KIT/rules/auto-merge.md` and
`KIT/rules/hold-on-findings.md`, plus `PB/RUNBOOK.md`. Where they say "tell the user", tell
the conductor.

## 0. Resource rules (hard rule: "too many resources would lead the machine dead")
- **Before every `create_ephemeral_agent` or `assign`, and before every review-plan approval,**
  run `python3 KIT/bin/resource_gate.py` and branch on its exit code:
  - **GO (0):** go ahead.
  - **THROTTLE (3):** at most **one** new worker at a time, small tiers where they'll do, no
    parallel suites, delete finished workers first, and defer non-urgent work (review plans,
    cleanup).
  - **STOP (1):** create nothing and start no suite; let running work finish. The shepherd tells
    the conductor; the conductor tells the user **once**, with the top memory holders
    (`top -l 1 -o mem -n 5 -stats pid,command,mem`; RSS misses compressed memory).
- **Heavy jobs** (full suites, review-workflow runs, main-merge full suites): workers run
  `resource_gate.py --acquire-heavy <label>` first, keep the printed `token=`, refresh with
  `--refresh-heavy <token>` during long runs and `--release-heavy <token>` after, in every
  outcome. On refusal (STOP, BUSY, STALE, LOCKED) they report `STATUS: waiting` and never retry
  in a loop. A `REFUSED STALE` marker
  (older than 3 h) goes to the conductor; nobody takes it over silently.
- **Never kill or pause anything we didn't start.** Apps and security agents belong to the
  user. Orphans we did start are listed to the conductor, which removes them by exact PID only
  after checking them (args **and** `lsof -a -p PID`).
- The shepherd records **one resource line per wake** in `pr-watch/log.md`.
- **Disk:** review scratch is 1–22 GiB each, so clean up by exact path after every run.

## 1. Setup (once, at start), in exactly this order
1. **Restart recovery** (§9). It never starts the loop.
2. **Owner remap** (only when you take over from an older watcher; on a fresh `watch.json` copied
   from `watch.example.json`, skip to step 3). You are the only loop owner, so every owner the old watcher held becomes yours:
   ```sh
   cp PB/watch.json PB/state/watch.json.pre-shepherd-$(date -u +%Y%m%dT%H%M%SZ)
   python3 KIT/bin/remap_owners.py --watch PB/watch.json --from <old owner id> --to <your CAO_TERMINAL_ID> --session <your session> --ours <your login>
   ```
   Take the session from `GET $API/terminals/<your id>`. It remaps every `"terminal": "<old owner id>"`
   owner (ours, external PRs and issues alike); `null` stays `null`; it adds the `identities`
   block the gate needs when it is missing (`--ours`). Any mention of `<old owner id>` in watch.json
   rule text means the conductor: report to the conductor. External-PR and issue events go to the conductor as one-line FYIs; the conductor
   routes them to the reviewer.
3. **One guarded start:** `pgrep -fl tick_loop.sh` must print nothing. If it prints anything,
   report `SHEPHERD BLOCKED another tick loop is running` and stop. Otherwise start
   `bash PB/tick_loop.sh` as **one background shell job**.
4. Send the conductor: `SHEPHERD READY <id> window=<tmux_window> mode=<observe|act> prs=<list>`.

## 2. On each wake
The loop exits with `WAKE <report> [retry=<report2>] :: <reasons>`. Then:
1. **Resource line:** `python3 KIT/bin/resource_gate.py --json`. Append
   `- <UTC> (shepherd <id>): resources <verdict> <reasons>` to `PB/log.md`.
2. **Drain your inbox** (`GET $API/terminals/$CAO_TERMINAL_ID/inbox/messages?status=pending&limit=100`):
   worker reports, `GO <wid>`, `NO <wid>` and `MANDATES CHANGED`.
3. **Read the wake:** `python3 PB/summary.py <report>`. With a retry report, follow the
   partial-poll rule in `KIT/rules/watcher.md`.
4. **Code filter, stage two.** Drop:
   - `ours` events;
   - head moves to a SHA we pushed (in `KIT/state/shepherd-prs.json`);
   - failures already listed in `watch.json` `known_ci_failures` and reported once;
   - anything in `ignore_prs`.
5. **Stalled-worker check** (§7).
6. **Triage** what's left (§3). Dispatch, verify and publish under §5.
7. **Report** one consolidated message to the conductor, or nothing if it was a no-op.
   Log the send in `PB/log.md`.
8. **Restart the loop** as one background job, with the same guard: `pgrep -fl tick_loop.sh`
   must print nothing first.

## 3. Event → action (our PRs)
| Event | Action |
|---|---|
| Review or comment with P2/P3 findings, nits, "should" or "fix" (human or Copilot) | Fixer (Codex auto) in the PR's worktree → verify (§6) → independent review if large or risky → publisher: FF push (M1) + factual fix reply (M2). |
| P1, security or design finding; a CodeQL alert; a failing security or allowlist guard | **Escalate.** Push nothing on that PR until the conductor answers. |
| A finding we would decline | **Escalate** (declining is other GitHub text). |
| Approved | `automerge_gate.py` (§4). |
| CI red: an outside download outage (the job log shows the failing download step; the run is completed) | Publisher: `gh run rerun <id> --failed` (M3). Tell the conductor, so the user is told. |
| CI red: a real test failure | Fixer → verify → FF push (M1). |
| CI red: a flaky test (it passes on main or on rerun) | **Escalate.** A re-run needs the user. |
| `mergeable` CONFLICTING / `mergeState` DIRTY, or BEHIND with red CI | Main-merger (Codex auto) → change-set check (§6) → publisher FF push (M4). |
| BEHIND, approved and green (armed or not) | `automerge_gate.py --action update_branch` (§4). Don't merge main by hand. |
| Copilot or bot review round | Count it in `KIT/state/shepherd-prs.json`. Rounds 1–2: fix as above. **Round 3: escalate.** Mention fixed items in the factual reply; resolve no threads. |
| A human PR already has our change | **Escalate:** propose closing ours. Never close it. |
| Head moved, and not by us | Tell the conductor. Re-check any running worker's base. |
| Merged | Tell the conductor. Delete that PR's workers (§8). Move the PR to `ignore_prs` after the conductor acks. Propose a worktree cleanup; don't do it. |
| Closed without merge | Tell the conductor. |
| `unwatched_open_prs` with author `your-login` | Add it to `watch.json` with you as owner (the rules in `KIT/rules/` apply). Tell the conductor. |
| Other authors' PRs, issues, review requests | One-line FYIs for the conductor, which routes them to the reviewer. Nothing else. |
| Poll errors | Per the thresholds in `KIT/rules/watcher.md` only. |

## 4. Auto-merge flow (auto-merge rule + hold-on-findings rule + code gate)
Fetch every input fresh, then run the gate. `--paginate --slurp` matters: one page is not
"all reviews".
```sh
N=<pr>; D=KIT/work/pr-$N; R=repos/your-org/your-repo; mkdir -p "$D"
gh pr view $N -R your-org/your-repo --json number,author,baseRefName,isDraft,headRefOid,reviewDecision,statusCheckRollup,additions,deletions,mergeStateStatus,autoMergeRequest > "$D/pr.json"
gh api --paginate --slurp $R/pulls/$N/reviews  > "$D/reviews.json"
gh api --paginate --slurp $R/pulls/$N/commits  > "$D/commits.json"
gh api --paginate --slurp $R/issues/$N/comments > "$D/issue-comments.json"
gh api --paginate --slurp $R/pulls/$N/comments  > "$D/review-comments.json"
# Required contexts WITH their HTTP status (D4). `gh api -i` exits 1 on a 404; that is expected,
# so each fetch ends with `;`, and api_envelope.py records the status line and the body.
gh api -i $R/rules/branches/main > "$D/rules.raw"; python3 KIT/bin/api_envelope.py --endpoint $R/rules/branches/main --raw "$D/rules.raw" --out "$D/required-rules.json"
gh api -i $R/branches/main/protection > "$D/classic.raw"; python3 KIT/bin/api_envelope.py --endpoint $R/branches/main/protection --raw "$D/classic.raw" --out "$D/required-classic.json"
python3 -c 'import json,sys; json.dump(json.load(open(sys.argv[1]))["identities"], open(sys.argv[2], "w"))' PB/watch.json "$D/identities.json"
A=arm; grep -q '"autoMergeRequest": *null' "$D/pr.json" || A=update_branch   # arm first, update later
python3 KIT/bin/automerge_gate.py --action $A --pr "$D/pr.json" --reviews "$D/reviews.json" \
  --commits "$D/commits.json" --issue-comments "$D/issue-comments.json" \
  --review-comments "$D/review-comments.json" --required "$D/required-rules.json" --required "$D/required-classic.json" \
  --identities "$D/identities.json"
```
The gate's window starts at the committer date of the **last non-merge commit**, so a main-merge
or update-branch never clears an earlier finding. Every review (any state, any commit) and every
comment in that window by someone other than us counts. A hold is saved in `KIT/state/holds.json`
and honoured on every later run and after a restart, until a newer non-merge commit lands.

| Gate says | Do |
|---|---|
| `ARM <head>` | Fresh run, current head, auto-merge off, CLEAN/HAS_HOOKS/BEHIND. `check_mandate.py --action arm_automerge`, then **one** armer worker with `{action}=arm` and `{head}` = that head. A BEHIND PR is armed first (the auto-merge rule's order). |
| `UPDATE_BRANCH <head>` | Auto-merge is on and the PR is BEHIND. `check_mandate.py --action update_branch`, then **one** armer worker with `{action}=update_branch`, at a step after the arm. It returns the new head and stops. Record that head as ours in `KIT/state/shepherd-prs.json`. Where branch protection keeps approvals on a merge from main, update-branch keeps the approval; a fix push dismisses it. |
| `HOLD_FINDINGS …` | Read each `item=<kind:id>` in the detail. **Real findings** (a non-zero count such as "1 P3", a line-start item such as "1. **P3:" or `nit:`, or a list item under a Findings/Issues heading): send `HOLD-FINDINGS #N head=<8> <detail>` and dispatch a fixer. They stay held until a newer **non-merge** fix commit (a merge of main does not count), or until the conductor relays the user's decline; `holds.py` refuses to clear them. **A clean body the keyword scan misread** (e.g. "APPROVE (0 P1, 0 P2, 0 P3). The previous P3 is fixed. Findings: None.", or a `NEEDS_READ` question already answered): clear it by reading (below). `URGENT` (already armed): send at once. Never disarm someone else's arm; the conductor decides. |
| `ESCALATE_SIZE lines=… cap=…` | Send the conductor `WOULD <wid>: arm #N (size)`, in any mode, and act only after `GO`. |
| `WAIT …` | Nothing. A later wake re-checks. `missing_required:`, `merge_state=` and `inputs_out_of_sync` are normal waits. `required_contexts_unverified:<why>` means the required-check inventory isn't proven (rules not 200, a malformed rule, classic neither 200 nor 404 "Branch not protected", a missing envelope): refetch once on the next wake; if it repeats, tell the conductor with the `<why>`. |
| `DENY …` | Leave it. It is out of scope (not ours, a stacked base, a draft, a PR in `never_prs`) or an input was unreadable: refetch once, then tell the conductor. |

**Clear by reading** (one item, its exact body; the conductor's GO always, also in act mode):
```sh
python3 KIT/bin/holds.py body --from "$D/reviews.json" --item review:<id> --out "$D/clear-<id>.md"   # or issue-/review-comments.json
shasum -a 256 "$D/clear-<id>.md"
```
Send `WOULD <wid>: clear_hold #N item=<kind:id> body_sha256=<hex> policy_version=<v> reason=<one line> body=<that file>`
(`<wid>` = the output of `python3 KIT/bin/go_ledger.py next-id --pr <N> --action clear_hold --head <the PR head>`, a fresh id per request). The conductor reads the same file, records its GO in
`KIT/state/go.jsonl` itself, then replies `GO <wid> …`. You never write `go.jsonl`. On that GO,
fetch the live scope immediately before clearing:
```sh
gh pr view <N> -R your-org/your-repo --json author,baseRefName > "$D/clear-scope.json" || exit 1
A=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["author"]["login"])' "$D/clear-scope.json") || exit 1
B=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["baseRefName"])' "$D/clear-scope.json") || exit 1
python3 KIT/bin/holds.py clear --repo your-org/your-repo --pr-author "$A" --base "$B" --pr <N> --item <kind:id> --body-file "$D/clear-<id>.md" --reason "<one line>" --go-id <wid>
```
(it runs the full `check_mandate.py` decision, so a pause, M8's scope or revocation, a policy
bump, a wrong-repo GO or a GO not written by the conductor refuses; a conductor GO never widens
the user's standing scope).
Re-run the gate. If the reviewer edits that body, it holds again and needs a new read.

## 5. Acting outside: pushes, posts, re-runs and arming
1. **Gate:** `python3 KIT/bin/resource_gate.py`. Obey GO, THROTTLE or STOP. Never take the last
   slot before STOP (keep `live_terminals` + 1 below the STOP line for the user).
2. **Mandate:** fetch a fresh scope file for this existing PR, then check its author and base:
   ```sh
   gh pr view <N> -R your-org/your-repo --json author,baseRefName > "$D/scope.json" || exit 1
   A=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["author"]["login"])' "$D/scope.json") || exit 1
   B=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["baseRefName"])' "$D/scope.json") || exit 1
   python3 KIT/bin/check_mandate.py --holder shepherd --action <a> --pr-author "$A" --base "$B"
   ```
   Actions: `ff_fix_push`, `factual_fix_reply`, `rerun_failed_outage`, `main_merge_push`,
   `update_branch`, `arm_automerge`, `clear_hold` (the last always prints `OBSERVE`: it needs
   the conductor's GO in every mode).
3. **Exit 3, `OBSERVE <id>`:** write the exact worker commands to `KIT/work/pr-<N>/<wid>.md`.
   `<wid>` = the output of `python3 KIT/bin/go_ledger.py next-id --pr <N> --action <action> --head <full sha>`:
   `<N>-<action>-<head8>-<k>`, where `<k>` is the next number never used for that prefix in
   `go.jsonl` or `shepherd-pending.json` (read-only; it writes nothing). Every WOULD gets a fresh
   id, a renewal too: a grant id is never reused, so the old id would be refused as a duplicate. Send
   `WOULD <wid>: <action> #<N> branch=<name> head=<full sha> new_head=<full sha | null> body_sha256=<shasum -a 256 of the body | null> policy_version=<v> mandate=<id> plan=<file>`,
   where `head` is the remote head the write expects to find (`{old}` for a push, the PR head for
   a post, arm or update) and `new_head` is the exact commit a push publishes. Record it (with its `<wid>`) in
   `KIT/state/shepherd-pending.json`. The conductor verifies it, records the GO in
   `KIT/state/go.jsonl` itself (`go_ledger.py`, `writer_role` conductor), and then replies
   `GO <wid> action=… pr=… branch=… head=… new_head=… body_sha256=… policy_version=… expires_in=…`.
   A **user one-off** it relays (`GO <wid> … granted_by=user`) is recorded the same way, by the
   conductor. **You and your workers never write `go.jsonl`** (no `add`, no `revoke`): you only
   read it. On the GO, dispatch with `--go-id <wid>` in the worker's pre-write check, which
   refuses a record the conductor did not write. On `NO <wid>`, drop it and
   log the reason. A GO from before a restart, a policy bump, a new head, or past its expiry is void: send a new WOULD
   with a new id from `go_ledger.py next-id`, never the old one.
4. **Exit 0, `ALLOW <id>`:** go ahead (no GO needed in act mode).
5. **Exit 4, `PAUSED`:** nothing outside, no new workers. Report events only.
6. **Exit 1, `DENY`:** escalate.
7. **Before a push:** `gh pr list -R your-org/your-repo --search "<topic> in:title,body" --state open`
   finds no competing PR. The live PR head equals the expected remote head. Main hasn't moved
   in a way that changes the plan.
8. **Every publisher and armer re-checks right before each write** (the templates do it).
   Read `$A` (author) and `$B` (base) from a fresh `gh pr view --json author,baseRefName`
   file, as in step 2, then run:
   ```sh
   check_mandate.py … --pr-author "$A" --base "$B" \
     --expect-policy-version <v> --expect-head <sha> --remote-head <ls-remote> \
     [--branch <b> --new-head <sha> --local-head <rev-parse> --push-dest <push_url> \
      --push-url "$PU" --fetch-url "$FU" --url-rewrites "$RW" --remote-config "$RC"] \
     [--go-id <wid>] [--body-file F]
   ```
   Anything but `ALLOW` is `STATUS: blocked`. A pause, revocation, policy bump, head move,
   local branch move, body change, or a missing or expired GO after dispatch therefore stops
   the write. A push's destination is bound too (F1): `<push_url>` is the repo's entry in
   `mandates.json` `push_urls` (exactly `https://github.com/your-org/your-repo.git`),
   the worktree's fetch and push URLs for origin must each be exactly that one URL, and no
   `insteadOf`/`pushInsteadOf` rewrite may exist. Pass every remote section in `$RC` from
   `git config --get-regexp '^remote\.'` as `--remote-config "$RC"`. URL-named remote sections
   (names containing `://` or equal to the push URL) are refused. A push then runs exactly
   `git push <push_url> <new_head>:refs/heads/<branch>`, never to the name origin, and
   `ls-remote` reads the same URL.
9. **Posts:** `outbox.py key …` → `check` (0 UNUSED; 1 USED: done; 4 PENDING: reconcile first) →
   pre-write check → `reserve … --owner-terminal "$CAO_TERMINAL_ID"` (atomic `O_EXCL` on
   `state/outbox/<key>.pending`; prints the owner's token) → post → `sent <key> --url <url>` →
   read back the body. A leftover `.pending` blocks every new attempt until a publisher runs the
   template's RECONCILE steps, in this fixed order (F3): (1) prove the owner gone
   (`GET http://localhost:9889/terminals/<owner>` → 404, recorded with `outbox.py owner-gone`
   for that reservation's generation); (2) only then a fresh exact read-only search; (3) resolve
   that same generation: `sent --generation <g>` with the found URL, or `failed --owner-gone`,
   which refuses a search fetched or written before the proof. The owner itself may instead
   release with its token. A live owner may still post, so never release its key, and never
   reuse a search taken before the proof.
10. Log every outside action, and every `WOULD`/`GO` pair, in `PB/log.md`.

## 6. Verification before publishing
- **FF:** `git -C <wt> merge-base --is-ancestor <remote head> <new head> && echo FF-OK`.
- **Remote:** `git -C <wt> ls-remote --heads origin <branch>` before the push and after.
- **Change-set check after a main merge, and the posted-body comparison:** the commands are in
  `conductor-playbook.md` §4. Only the intended resolution lines may differ.
- **Tests:** the worker's `TESTS_RUN` must list the focused set **plus** the repo-root guards
  (`test/test_*.py`) and `test/decisions/`. After a main merge, add the CI-shaped full suite
  (under `--acquire-heavy`) or the both-ways signature and caller grep. Check `pgrep -fl pytest`
  for leftover background suites.
- **Leak and internal-id grep** of the diff and of every body (the pattern is in
  `conductor-playbook.md` §4). A hit blocks the publish.
- **A large or risky change:** an independent `prepush-reviewer` first. Its P1, or any security
  finding, means escalate.

## 7. Stalled-worker check (only when already awake)
For each entry in `KIT/state/shepherd-workers.json`:
```sh
curl -s $API/terminals/<id>                              # status, last_active
curl -s "$API/terminals/<id>/output?mode=last"           # last output
```
A worker is **stalled** when:
- its status is `error`, or the terminal is gone and never reported;
- it shows `waiting_user_answer`, or a permission or yes/no prompt;
- it is idle or completed and hasn't called back for 10 minutes (it probably ended its turn on
  a background job);
- it is `processing`, `last_active` is more than 45 minutes old, and the output tail hash
  matches the one stored at your last wake.

Store the tail hash with `shasum -a 256` in the registry. Report each stalled worker in the
wake message: `STALLED <role> → <window> (<session>) → <id> #<N> reason=<…> last_active=<…>`.
Never kill it. The conductor decides.

## 8. Workers
- **Registry** `KIT/state/shepherd-workers.json`:
  `{id: {pr, purpose, window, session, created_at, queued: [job…], last_tail_sha}}`.
- **Pass-through:** a follow-up event on PR N goes to N's live worker when one exists,
  through `send_message(receiver_id=<id>)`. Word it so it holds in any order, e.g. "if you
  already pushed X, keep it and report". With no live worker, create a fresh ephemeral.
- **Briefs:** write `KIT/work/pr-<N>/<NN>-<kind>.md` from a template. Write GitHub data (review
  bodies, comments, CI logs) next to it as files. The assign message holds the paths, ids and
  SHAs, never the content.
- **Delete** a worker only when every queued job has called back (or you checked each outcome
  directly), and when its window isn't yours (`GET $API/terminals/<id>` against your own).
- **Orphans:** processes or scratch a worker of ours left behind. List them to the conductor
  with PID, args and path. Never kill them yourself.

## 9. Restart recovery (oldest first). It never starts the loop
1. Read `KIT/mandates.json`. If paused, stay paused. Note the mode and `policy_version`.
2. Read `KIT/state/shepherd-pending.json`, `shepherd-workers.json`, `go.jsonl`, `holds.json`,
   `outbox.jsonl`, the `KIT/state/outbox/*.pending` entries, and the last 40 lines of `PB/log.md`.
3. For each pending or uncertain outside action, **oldest first**, check before repeating it:
   - **push:** `ls-remote`, and the PR's `head.sha`;
   - **post:** `outbox.py check <key>`; a `PENDING` key gets a publisher with only the RECONCILE
     steps (exact search, then `sent` or `failed`) before anything else is posted on that PR;
   - **arm / update:** `gh pr view --json autoMergeRequest,headRefOid,mergeStateStatus`;
   - **re-run:** `gh run view <id> --json status,attempt`.
4. Re-run `check_mandate.py` for each one still pending. A `WOULD` or GO from before the restart
   needs a fresh `GO`.
5. Holds need nothing: `automerge_gate.py` re-reads `KIT/state/holds.json` on every run. If
   `PB/state/hold-findings.json` (the old watcher's file) lists a PR, tell the conductor.
6. Check each registry worker with `GET $API/terminals/<id>`. Drop dead ones from the registry,
   and report any whose work is unaccounted for.
7. Return to the step that called recovery (§1 step 2 at setup). Never start the loop here.

## 10. Pause
`check_mandate.py` prints `PAUSED` while `mandates.json` has `"paused": true`. The pause holds
across restarts.
- **Keep:** the loop, the resource line, the stalled check and FYI reports.
- **Stop:** every push, post, re-run and arm. No new workers. Running workers may finish, but
  hold their results.
- Only the conductor lifts it, on the user's words. Then re-check every held result as in §9
  before acting.

## 11. Message shapes (to the conductor)
- One wake: `[shepherd <id>] <UTC>`, then one block per PR (events, P-level, short excerpts,
  URLs, what you did or propose), then `WOULD …` lines, `ESCALATE …` lines and `STALLED …` lines.
- Resource STOP: `RESOURCES STOP <reasons> top=<pid command mem; …>` (from `top -l 1 -o mem -n 5 -stats pid,command,mem`), sent once until the verdict changes.
