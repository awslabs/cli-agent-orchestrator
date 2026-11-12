# Conductor playbook

The conductor's profile (`agents/cao_conductor.md`) says *what* to do. This file says *how*.
Read the section you need when you need it. `KIT` = `${SUPER_AGENT_HOME:-$HOME/super-agent}`
(your copy of this sample), `PB` = `KIT/pr-watch`, `REPO` = your repo (`your-org/your-repo` in
the shipped files), `API` = `http://localhost:9889` (the default `cao-server` address).

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
  in a loop. `cao_pr_reviewer` takes the same slot before every workflow run or retry. A `REFUSED STALE` marker
  (older than 3 h) goes to the conductor; nobody takes it over silently.
- **Never kill or pause anything we didn't start.** Apps and security agents belong to the
  user. Orphans we did start are listed to the conductor, which removes them by exact PID only
  after checking them (args **and** `lsof -a -p PID`).
- The shepherd records **one resource line per wake** in `pr-watch/log.md`.
- **Disk:** review scratch is 1–22 GiB each, so clean up by exact path after every run.

## 1. Each turn, in order
1. **A user message is in the batch?** Handle it first. Background work waits.
2. **Drain the inbox:** `curl -s "$API/terminals/$CAO_TERMINAL_ID/inbox/messages?status=pending&limit=100"`.
   Read every `message` in full, group by PR or task, and decide on the batch together.
   If an expected callback is overdue, check `GET $API/terminals/$CAO_TERMINAL_ID` for a status
   stuck on `processing` (the inbox only delivers while idle).
3. **Act** on the table below. Run the resource gate before creating, assigning or approving.
4. **Hand off waits:** start a background shell job or leave it to the shepherd. Never sleep or
   poll inside your own turn.
5. **Report:** one consolidated update to the user if anything real happened, otherwise nothing.

## 2. Event → action
| Message or event | Action |
|---|---|
| The user asks for work | Split it. Write a brief file. Gate. Dispatch a sized ephemeral (§3). Verify the result (§4). Ask before anything in `forbidden_without_user`. |
| The user asks for a PR review | Ask `cao_pr_reviewer` to take it. Add the 👀 reaction through a small publisher (reactions API, never edit the body). |
| Shepherd `WOULD <wid>: …` (observe mode) | `<wid>` is `<N>-<action>-<head8>-<k>`, a fresh id from `go_ledger.py next-id` for every request (a renewal after a policy bump, restart or expiry too); check its PR, action and head8 match the proposal. If `add` prints `row_refused:…:id:duplicate`, reply `NO <wid> id already used` and the shepherd sends a new WOULD. Verify the proposal (§4) against live state, including the head, the body's `shasum -a 256` and `policy_version`. Reply exactly `GO <wid> action=<a> pr=<N> branch=<name or null> head=<full sha> new_head=<full sha or null> body_sha256=<hex or null> policy_version=<v> expires_in=<minutes>` (your values, not copied unchecked; a push names the exact commit it may publish as `new_head`, checked with `git rev-parse`), or `NO <wid> <reason>`. **Record the GO yourself first** (only you write `state/go.jsonl`): `python3 KIT/bin/go_ledger.py add --writer-role conductor --id <wid> --action <a> --pr <N> --head <sha> --policy-version <v> --granted-by "conductor $CAO_TERMINAL_ID GO" --expires-in <minutes> [--branch <name> --new-head <sha>] [--body-sha256 <hex>]` (a push needs `--branch` and `--new-head`), then reply. Every worker re-checks the record right before its write. Log both "would" and the outcome in `PB/log.md`. |
| Shepherd `HOLD-FINDINGS #N …` | FYI. The shepherd dispatches the fixer itself. Step in only if a finding is P1, security or design. Real findings stay held until a fix commit, or until you relay the user's decline. |
| Shepherd `WOULD <wid>: clear_hold #N item=… body=<file>` | Read **the same body file** yourself and check `shasum -a 256` equals the WOULD. GO only if it truly lists no finding (e.g. "0 P1, 0 P2, 0 P3 … Findings: None."). A real finding is never cleared this way: `holds.py` refuses it. Record it yourself, `python3 KIT/bin/go_ledger.py add --writer-role conductor --id <wid> --action clear_hold --pr <N> --head <sha> --policy-version <v> --granted-by "conductor $CAO_TERMINAL_ID GO" --body-file <that file> --item <kind:id>`, then reply `GO <wid> action=clear_hold pr=<N> head=<sha> body_sha256=<hex> policy_version=<v>`; or `NO <wid> <reason>`. This GO is needed in act mode too. |
| Shepherd `ESCALATE_SIZE #N lines=… cap=…` | Second look, not a block. Check that the approvals cover the whole change, and get an independent large review if none exists. Then `GO` the shepherd's `WOULD`, or ask the user. |
| Shepherd escalation: P1, security or design | Dispatch a read-only architect or large/high reviewer. Ask the user one question if the choice is theirs. |
| Shepherd FYI: an external PR or issue event | Route it to `cao_pr_reviewer` (one line, ids and URLs only) if it is review work; otherwise relay it in the next user update. |
| Shepherd escalation: force-push, new PR, close, merge outside the gate, release, PR-body edit, flaky re-run | `forbidden_without_user` or uncovered: ask the user, one question. Releases are the maintainers' call. |
| The user OKs one such action once (e.g. "open that PR") | A **one-off**, never a reduced path: relay `GO <wid> action=<a> pr=<N or null> branch=<name or null> head=<sha> new_head=<sha or null> body_sha256=<hex> policy_version=<v> expires_in=<minutes> granted_by=user` with the user's words, bound to exactly that action, target, content and a short expiry. Take a fresh `<wid>` from `python3 KIT/bin/go_ledger.py next-id --pr <N> --action <a> --head <sha>` (`--branch <name>` when there is no PR). You record it yourself before relaying, for the shepherd's publisher and your own alike: `go_ledger.py add --writer-role conductor … --granted-by user --relayed-by $CAO_TERMINAL_ID`. The publisher's full `check_mandate.py` then prints `ALLOW oneoff go=<wid>`. Withdrawn: `go_ledger.py revoke --writer-role conductor --id <wid> --by <your id>`. |
| Shepherd: third bot-review round on #N | Ask the user whether to continue, or reply once and stop. |
| Shepherd: competing human PR has our change | Ask the user whether to close ours. Never close it yourself. |
| Shepherd `STALLED …` | Inspect (§7). Message the worker, or delete it and re-dispatch. |
| Shepherd `STOP` resources | Tell the user **once**, with the top memory holders. Create nothing. |
| Shepherd lists orphans we started | Check each (§8). Remove by exact PID only when every check holds. |
| `cao_pr_reviewer` reports a refused plan | Plan approval (§5). |
| `cao_pr_reviewer` `REVIEWED #N …` | Relay to the user in the next update. |
| Your own worker reports READY | Verify (§4) before you accept it. Then publish through a publisher if a mandate or the user covers it. |
| The user: "pause" / "resume" / "revoke X" / "switch the shepherd to act" / "standing OK for Y" | Edit mandates (§6). |

## 3. Dispatch sizing
| Work | Provider / tier / effort | Template |
|---|---|---|
| Code fix, CI fix, test fix | Codex `auto` / `auto` | `templates/fixer.md` |
| Merge main into a PR branch | Codex `auto` / `auto` | `templates/main-merger.md` |
| Independent pre-push or security review | Claude Code `large` / `high`, read-only | `templates/prepush-reviewer.md` |
| Push, post a prepared body, CI re-run | Claude Code `small` / `low`, exact commands | `templates/publisher.md` |
| Update-branch and arm auto-merge | Claude Code `small` / `low`, exact commands | `templates/automerge-armer.md` |
| A task that needs a fixed contract | the installed profile (e.g. `cao_pr_reviewer`) | — |

- `purpose` is snake_case (`pr101_ci_fixer`). Call `assign` straight after
  `create_ephemeral_agent`, because the profile expires in about 15 minutes. If the server
  refuses `effort`, drop that field.
- Codex can't `git push` or `gh pr view`, so pushes always go to a Claude Code publisher, and
  Codex reads PRs through files you give it.
- **THROTTLE:** one new worker at a time, smallest adequate tier, and delete finished workers first.
- After `assign`, name the worker to the user as role → terminal window (CAO session) → id. The
  window name starts with the ephemeral profile name.

## 4. Verification commands
Run these yourself; a worker's report is a claim.

```sh
W=<worktree>; B=<branch>; OLD=<remote head>; NEW=<new head>
git -C "$W" merge-base --is-ancestor "$OLD" "$NEW" && echo FF-OK          # fast-forward only
git -C "$W" -c core.hooksPath=/dev/null ls-remote --heads origin "$B"  # remote head
gh api repos/your-org/your-repo/pulls/<N> --jq .head.sha       # PR head
git -C "$W" range-diff <old_base>..<old_head> <new_base>..<new_head>       # rebase-only: all '='
```

**Change-set check after a main merge** (`OM` = old main, `OH` = old PR head,
`NM` = new main, `MG` = the merge commit):
```sh
cmp <(git -C "$W" diff --numstat "$OM" "$OH") <(git -C "$W" diff --numstat "$NM" "$MG") && echo NUMSTAT-SAME
for f in $(git -C "$W" diff --name-only "$OM" "$OH"); do
  cmp -s <(git -C "$W" diff "$OM" "$OH" -- "$f" | grep -vE '^(\+\+\+|---) ' | grep -E '^[+-]' | sort) \
         <(git -C "$W" diff "$NM" "$MG" -- "$f" | grep -vE '^(\+\+\+|---) ' | grep -E '^[+-]' | sort) \
    || echo "CHANGED $f"
done
git -C "$W" show --remerge-diff "$MG" | head -200
```
Only the intended conflict-resolution lines may differ. Also grep `git diff main HEAD` for
CHANGELOG or doc lines the PR replaced that now show up only as context.

**Posted body equals the file.** Never use `diff <(file) <(cat)`; it hangs.
```sh
python3 - posted.md prepared.md <<'PY'
import sys
n = lambda p: open(p, encoding="utf-8").read().replace("\r\n", "\n").rstrip("\n")
sys.exit(0 if n(sys.argv[1]) == n(sys.argv[2]) else 1)
PY
```

**Leak and internal-id grep** of every outward body and every diff:
```sh
grep -nE '/Users/|/home/|/tmp/|/private/|<your workspace folder names>|brief [0-9]|token|secret|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}' FILE
grep -noE '\b[0-9a-f]{8}\b' FILE   # each hit must be a commit of this PR (git cat-file -e), never a terminal id
```
A hit means fix it and scan again. Private names and labels stay out of public text.

**Tests.** The focused set must include the repo-root guards (`test/test_*.py`) and
`test/decisions/`. After a main merge or a restack, run the root guards on the new tip, plus the
CI-shaped full suite under `--acquire-heavy` or the both-ways signature and caller grep. Check
that the RED test failed before the fix, and that the named mutants die. Make sure the worker
left no background suite running: `pgrep -fl pytest` before you trust "green".

**No data change** in a nit round: compare the `repr` of the affected schema objects before
and after.

## 5. Reviewer plan approval (mandate M7; only with the optional reviewer)
1. `python3 KIT/bin/resource_gate.py`: STOP means no. THROTTLE means defer, unless the user is waiting.
2. Fetch the live author and base into a fresh scope file in your scratch directory `$D`:
   ```sh
   gh pr view <N> -R your-org/your-repo --json author,baseRefName > "$D/review-scope.json" || exit 1
   A=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("author", dict()).get("login") or "")' "$D/review-scope.json") || exit 1
   B=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("baseRefName") or "")' "$D/review-scope.json") || exit 1
   python3 KIT/bin/check_mandate.py --holder conductor --action approve_review_plan \
     --pr-author "$A" --base "$B"
   ```
   It must print `ALLOW M7-approve-review-plan`.
3. The workflow is exactly `pr-review` or `branch-review`.
4. Read the PR head:
   `gh api repos/your-org/your-repo/pulls/<N> --jq '.head.sha+" "+.base.ref+" "+.state+" "+.user.login'`.
   It must be OPEN with base `main`, and its head must equal the plan's `head_sha`, or the head
   the reviewer reported for `pr-review`.
5. **Follow-ups** (`branch-review`): `base_sha` must be the last reviewed head (see
   `KIT/reviews/ledger.json`) and an ancestor of the head:
   `gh api repos/your-org/your-repo/compare/<base_sha>...<head_sha> --jq '.status+" behind="+(.behind_by|tostring)'`
   → `ahead behind=0`. After a rebase, it must instead equal the merge base:
   `gh api repos/your-org/your-repo/compare/main...<head_sha> --jq .merge_base_commit.sha`.
6. Check both workflow files against their **trusted baselines** (recorded by you, the user,
   after you read the two scripts yourself, in `KIT/state/plan-approvals.jsonl`):
   `python3 KIT/bin/plan_hash_check.py ~/.aws/cli-agent-orchestrator/workflows/pr-review.py ~/.aws/cli-agent-orchestrator/workflows/branch-review.py`
   must print `MATCH` twice and exit 0. `REFUSE missing_baseline` or `REFUSE changed` means
   **refuse**, and ask the user (one question, with both hashes). **Never record a baseline
   yourself.** A new baseline is appended only on the user's explicit words, as
   `{"kind":"baseline","workflow","file","sha256","verified_at","verified_by":"user","evidence":"<their words>"}`,
   with `python3 KIT/bin/state_schema.py append-plan-row --row '<that JSON>'` (never by hand: it
   checks the row and the file first, and `ERROR` means nothing was written).
7. `cao workflow approve <plan_id>`. Append
   `{"kind":"approval","at","plan_id","workflow","pr","head","base","sha256":{…}}` to
   `KIT/state/plan-approvals.jsonl` with `state_schema.py append-plan-row --row '<that JSON>'`
   (approval rows are a log; the checker never trusts them) and one line to `PB/log.md`.
8. Tell the reviewer to retry, and tell the user in the next update.
Any mismatch, any other workflow, or any request to disable enforcement goes to the user.

## 6. Editing mandates (only on the user's explicit words)
1. Copy the file first: `cp KIT/mandates.json KIT/state/mandates.json.bak-$(date -u +%Y%m%dT%H%M%SZ)`.
2. Make exactly the change the user asked for:
   - **Pause:** `"paused": true, "paused_by": "user", "paused_at": "<UTC>", "reason": "<their words>"`.
   - **Resume:** `"paused": false`, and set the three fields back to `null`.
   - **Revoke:** that mandate's `"revoked": true`. Never delete it.
   - **Observe → act:** `"shepherd_mode": "act"`.
   - **New OK:** a new mandate with `granted_by: "user"`, `granted_at`, a `source`, and no
     wider scope than the user's words.
3. Bump `policy_version`. Check the file parses (`python3 -m json.tool KIT/mandates.json >/dev/null`),
   then run one `check_mandate.py` probe that shows the new effect.
4. Mirror the change to CAO memory with `memory_store`: key `super-agent-mandates`, the change,
   the user's words and the date.
5. Tell the shepherd in one line: `MANDATES CHANGED policy_version=<v>: <change>`.

## 7. Stalled-worker check (your own workers, when you are awake anyway)
For each worker you created: `curl -s $API/terminals/<id>` (`status`, `last_active`) and
`curl -s "$API/terminals/<id>/output?mode=last"`. A worker is stalled when any of these holds:
- its status is `error`, or the terminal is gone (404) and it never called back;
- its status is `waiting_user_answer`, or the output shows a permission or yes/no prompt;
- it is idle or completed with no callback after 10 minutes. It probably ended its turn while
  waiting on a background job;
- it is `processing` with `last_active` more than 45 minutes old, and its output tail hasn't
  changed since your last look.
Action: message it once with a precise instruction, or delete it (§8 rules) and re-dispatch.
Check the outside state first so no action is repeated.

## 8. Deleting workers and orphans
- **Delete a terminal** only when every queued job has called back (or you verified each
  outcome directly), and when its `tmux_session`/`tmux_window` differs from your own
  (`GET $API/terminals/$CAO_TERMINAL_ID`).
- **Orphan processes** (listed by the shepherd or found by you): only ones we started. Kill by
  exact PID only when (a) `ps -o args= -p PID` matches what we started **and** (b) for a server,
  `lsof -nP -a -p PID -iTCP:<port> -sTCP:LISTEN` succeeds, or for scratch, `lsof -a -p PID +D <scratch>`
  shows our own path. On any mismatch, print STOP and kill nothing. Never touch the user's apps
  or security agents.
- **Scratch:** delete by exact path, one kind at a time (`*-export`, `trees`, `home*`, `venv*`,
  caches, `mut*`), after `lsof +D` shows nobody uses it. Keep `*.md`, `*.json` and small logs.

## 9. Restart recovery (oldest first)
1. Read `KIT/mandates.json`. If `paused` is true, stay paused.
2. Read `KIT/state/outbox.jsonl`, the `KIT/state/outbox/*.pending` entries,
   `KIT/state/plan-approvals.jsonl`, `go.jsonl`, `holds.json`, the last 40 lines of
   `PB/log.md`, and your newest brief files. A `.pending` post is reconciled (publisher
   RECONCILE steps) before anything else is posted on that PR. Run
   `python3 KIT/bin/state_schema.py check` (exit 0 `OK`): any `state_corrupt:<file>:<line>:<why>`
   line is a damaged state file that every script refuses to act on. Inspect that row, repair it
   by hand from the evidence (or ask the user), and re-run `check` before anything else.
3. For every action that may or may not have happened (a push, a post, an approval, an arm),
   oldest first: check the live state **before** repeating it. Use `ls-remote` for heads,
   `gh api …/issues/<N>/comments` for an identical body, `outbox.py check`, and
   `gh pr view --json autoMergeRequest`.
4. Re-run `check_mandate.py` for each pending action.
5. List live sessions and terminals (`GET $API/sessions`). Keep workers that are still working.
   Never delete the old conductor record from inside the relaunched conductor.
6. Make sure the shepherd is alive. If it isn't, start a fresh one with `assign`.

## 10. Starting the shepherd (once)
1. Make sure the user has installed `pr_shepherd`. Installing is the user's step.
2. Stop any other tick loop: its owner stops its own background job. Then check that
   `pgrep -fl tick_loop.sh` prints nothing.
3. Gate, then `assign(agent_profile="pr_shepherd", message="Read and follow KIT/shepherd-playbook.md exactly (KIT = <the absolute kit path>). Start with §1 Setup. Report to me with send_message (receiver_id omitted).")`.
4. Tell the user the shepherd's window name. Expect `SHEPHERD READY …`, then quiet until a real event.
