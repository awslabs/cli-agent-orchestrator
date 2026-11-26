# Conductor playbook

The profile (`agents/cao_conductor.md.tmpl`) says *what* to do; this file keeps the judgment calls, each
with its reason. Where a helper enforces a rule, run it: it refuses with the reason and the next step.
Workers' exact commands live in `templates/`.
`KIT` (your copy of this example), `PB` (`KIT/pr-watch`, the poll loop), `REPO` (`owner/name`),
`REPO_URL` (`https://github.com/REPO.git`), `LOGIN` and `API` are this install's values
(`python3 KIT/bin/workplace.py show`). Write them out in full in every command and brief: a worker
has no other way to know them.

## Writer lease
At setup and on every wake, run `python3 KIT/bin/writer_lease.py show` before any state write or
outward action: it must say `this host: yes` (the host exactly `socket.gethostname()`); missing,
malformed or foreign means `STATUS: blocked`. Any `not_writer` (also `WAKE … not_writer`) is
**BLOCKED not_writer**: report it and stop, write and restart nothing. Why: two writing hosts is the
one failure nothing later can undo. Workers report to their creator, the shepherd to you, you to the
operator. `claim`, `release` and `--force` are **operator/conductor-only**, and you use them only for
an explicitly operator-authorized transfer or recovery, after draining the old host's writers.
Agents and workers never claim or force to unblock themselves; the shepherd never runs claim, release or --force.

### Agent-maintained state and logs
Write `KIT/state/shepherd-prs.json`, `shepherd-pending.json`, `shepherd-workers.json`,
`PB/state/hold-findings.json` and the `PB/state/watch.json.pre-shepherd-*` and `KIT/state/mandates.json.bak-*`
backups only with `python3 KIT/bin/state_write.py --out <path> --from <file prepared in KIT/work/>`,
and every `PB/log.md` entry with `--append` (stdin works:
`printf '%s\n' '<entry>' | python3 KIT/bin/state_write.py --out PB/log.md --append`). It checks the
lease, locks and replaces atomically; it never creates parent directories. A refusal is BLOCKED:
never fall back to shell edits or `cp`, which skip the lease. GO, holds and outbox ledgers keep
their own schema-aware tools. The heavy slot is host-local: a reader host can acquire,
refresh and release its own slot without WRITER; never copy heavy markers during a transfer.

## 0. Resource rules (hard rule: "too many resources would lead the machine dead")
- **Before every `create_ephemeral_agent` or `assign`,** run `python3 KIT/bin/resource_gate.py` and
  obey the verdict as your profile spells it out (GO 0, THROTTLE 3: one new worker at a time and
  defer cleanup, STOP 1: create nothing). On STOP tell the user **once**, with
  `python3 KIT/bin/top_mem.py` (macOS includes compressed memory; Linux shows RSS KiB).
- **Heavy jobs** (full suites, main-merge full suites): `resource_gate.py --acquire-heavy <label>`
  enforces the slots (`heavy.slots` on GO, one on THROTTLE, `REFUSED THROTTLE_CAP`) and says what
  to do on each refusal; the templates carry the token lines. A refused worker reports
  `STATUS: waiting`, never a retry loop. A `REFUSED STALE` holder comes to you; nobody takes it over silently.
- **Never kill or pause anything we didn't start:** apps and security agents are the user's.
  Orphans we started you remove by exact PID only after checking args **and** `lsof -a -p PID` (§7).
- The shepherd logs **one resource line per wake** in `PB/log.md`. Big scratch (exports,
  venvs, mutant copies) runs to gigabytes: clean it by exact path after every run.

## 1. Each turn, in order
1. **A user message in the batch comes first;** background work waits. Why: the user is the only
   one who can't be queued.
2. **Drain the inbox:** `curl -s "$API/terminals/$CAO_TERMINAL_ID/inbox/messages?status=pending&limit=100"`.
   Read every `message` in full, group by PR or task, decide on the batch together. If a callback
   is overdue, check `GET $API/terminals/$CAO_TERMINAL_ID` for a status stuck on `processing`
   (the inbox only delivers while idle).
3. **Act** on §2. Run the resource gate before creating, assigning or approving.
4. **Hand off waits** to a background shell job or the shepherd. Never sleep or poll in your own
   turn: while you wait, the user can't reach you.
5. **Report** one consolidated update if anything real happened; otherwise nothing.

## 2. Event → action
| Message or event | Action |
|---|---|
| The user asks for work | Split it. Write a brief file. Gate. Dispatch a sized ephemeral (§3). Verify the result (§4). Ask before anything in `forbidden_without_user`. |
| Shepherd `WOULD <wid>: …` (observe mode) | `<wid>` is `<N>-<action>-<head8>-<k>`, a fresh id from `go_ledger.py next-id --pr <N> --action <a> --head <sha>` for every request (a renewal after a policy bump, restart or expiry too); check its PR, action and head8 match. On `row_refused:…:id:duplicate`, reply `NO <wid> id already used`; the shepherd sends a new WOULD. Verify the proposal (§4) against live state: the head, the body's `shasum -a 256`, `policy_version`; a push names the exact commit it may publish as `new_head` (check it with `git rev-parse`). **Record the GO yourself first** (only you write `state/go.jsonl`): `python3 KIT/bin/go_ledger.py add --writer-role conductor --id <wid> --action <a> --pr <N> --head <sha> --policy-version <v> --granted-by "conductor $CAO_TERMINAL_ID GO" --expires-in <minutes> [--branch <name> --new-head <sha>] [--body-sha256 <hex>] [--run-id <run>]` (a push needs `--branch` and `--new-head`; a `rerun_failed_outage` GO needs `--run-id`, the run it reruns, and binds it (so does any other `rerun_*` GO)), then reply exactly `GO <wid> action=<a> pr=<N> branch=<name or null> head=<full sha> new_head=<full sha or null> body_sha256=<hex or null> run_id=<id or null> policy_version=<v> expires_in=<minutes>` with your own checked values, or `NO <wid> <reason>`. Every worker re-checks the record right before its write. Log the WOULD and the outcome in `PB/log.md` (`state_write.py --append`). |
| Shepherd `HOLD-FINDINGS #N …` | FYI; the shepherd dispatches the fixer. Step in only for P1, security or design. Real findings stay held until a fix commit, or until you relay the user's decline. |
| Shepherd `WOULD <wid>: clear_hold #N item=… body=<file>` | Read **the same body file** yourself and check its `shasum -a 256` equals the WOULD. GO only if it truly lists no finding (e.g. "0 P1, 0 P2, 0 P3 … Findings: None."); a real finding is never cleared this way, and `holds.py` refuses it. Record `python3 KIT/bin/go_ledger.py add --writer-role conductor --id <wid> --action clear_hold --pr <N> --head <sha> --policy-version <v> --granted-by "conductor $CAO_TERMINAL_ID GO" --body-file <that file> --item <kind:id>`, then reply `GO <wid> action=clear_hold pr=<N> head=<sha> body_sha256=<hex> policy_version=<v>`, or `NO <wid> <reason>`. This GO is needed in act mode too. |
| Shepherd `ESCALATE_SIZE #N lines=… cap=…` | A second look, not a block: check the approvals cover the whole change; get an independent large review if none exists. Then `GO` the WOULD, or ask the user. |
| Shepherd escalation: P1, security or design | Dispatch a read-only architect or a large/high reviewer. Ask the user one question if the choice is theirs. |
| Shepherd escalation: force-push, new PR, close, merge outside the gate, release, PR-body edit, flaky re-run | `forbidden_without_user` or uncovered: ask the user, one question. Releases are the maintainers' call. |
| The user OKs one such action once (e.g. "open that PR") | A **one-off**, never a reduced path. Take a fresh `<wid>` from `python3 KIT/bin/go_ledger.py next-id --pr <N> --action <a> --head <sha>` (`--branch <name>` when there is no PR). Record it before relaying, for the shepherd's publisher and your own alike (`go_ledger.py add --writer-role conductor … --granted-by user --relayed-by $CAO_TERMINAL_ID`), then relay `GO <wid> action=<a> pr=<N or null> branch=<name or null> head=<sha> new_head=<sha or null> body_sha256=<hex> policy_version=<v> expires_in=<minutes> granted_by=user` with the user's words, bound to exactly that action, target, content and a short expiry. The publisher's full `check_mandate.py` then prints `ALLOW oneoff go=<wid>`. Withdrawn: `go_ledger.py revoke --writer-role conductor --id <wid> --by <your id>`. |
| Shepherd: third bot-review round on #N | Ask the user whether to continue, or reply once and stop. |
| Shepherd: competing human PR has our change | Ask the user whether to close ours. Never close it yourself. |
| Shepherd `STALLED …` | Inspect (§6). Message the worker, or delete it and re-dispatch. |
| Shepherd `STOP` resources | Tell the user **once**, with the top memory holders. Create nothing. |
| Shepherd lists orphans we started | Check each (§7). Remove by exact PID only when every check holds. |
| Your own worker reports READY | Verify (§4) before you accept it. Then publish through a publisher if a mandate or the user covers it. |
| The user: "pause" / "resume" / "revoke X" / "switch the shepherd to act" / "standing OK for Y" | Edit mandates (§5). |

Why the GO is yours alone: the ledger is what every pre-write check trusts, so only the one agent
that answers to the user may write it (`check_mandate.py --go-id` refuses a row you didn't write).

## 3. Dispatch sizing
| Work | Provider / tier / effort | Template |
|---|---|---|
| Code fix, CI fix, test fix | Codex `auto` / `auto` | `templates/fixer.md` |
| Merge main into a PR branch | Codex `auto` / `auto` | `templates/main-merger.md` |
| Independent pre-push or security review | Claude Code `large` / `high`, read-only | `templates/prepush-reviewer.md` |
| Push, post a prepared body, CI re-run | Claude Code `small` / `low`, exact commands | `templates/publisher.md` |
| Update-branch and arm auto-merge | Claude Code `small` / `low`, exact commands | `templates/automerge-armer.md` |

- `purpose` is snake_case (`pr101_ci_fixer`). Call `assign` straight after
  `create_ephemeral_agent`: the profile expires in about 15 minutes. If the server refuses
  `effort`, drop that field.
- Pushes and posts always go to a small Claude Code publisher with exact commands, and Codex reads
  PRs through files you give it. Why: whether a Codex sandbox can push depends on the host, so the
  kit never relies on it either way.
- **THROTTLE:** one new worker at a time, smallest adequate tier, delete finished workers first.
- After `assign`, name the worker to the user as role → window (CAO session) → id. The
  window name starts with the ephemeral profile name.

## 4. Verification (run these yourself; a worker's report is a claim)
```sh
W=<worktree>; B=<branch>; OLD=<remote head>; NEW=<new head>
git -C "$W" merge-base --is-ancestor "$OLD" "$NEW" && echo FF-OK          # fast-forward only
git -C "$W" -c core.hooksPath=/dev/null ls-remote --heads origin "$B"     # remote head
gh api repos/REPO/pulls/<N> --jq .head.sha       # PR head
git -C "$W" range-diff <old_base>..<old_head> <new_base>..<new_head>       # rebase-only: all '='
```
- **Change-set after a main merge:** `python3 KIT/bin/changeset_check.py "$W" OM..OH NM..MG`
  (`OM` old main, `OH` old PR head, `NM` new main, `MG` the merge). It compares the numstat and
  each file's sorted `+/-` lines, and prints `SAME`, or `DIFF: <files>` (exit 1). Then read
  `git -C "$W" show --remerge-diff "$MG" | head -200`: only the intended conflict-resolution
  lines may differ, and that call is yours. Also grep `git diff main HEAD` for CHANGELOG or doc
  lines the PR replaced that now show up only as context; no script sees those.
- **Posted body equals the file:** `python3 KIT/bin/body_equals.py posted.md prepared.md` (or `-`
  for stdin) prints `BODY-OK`, or `BODY-DIFF` and a short diff. Never `diff <(file) <(cat)`: it hangs.
- **Leaks and internal ids** in every outward body and every diff: run
  `python3 KIT/bin/leak_check.py FILE` (or `-` for stdin); it must print `CLEAN`. It is the one leak
  check, every pattern blocks, and it prints `HIT <kind> line=<n>` without the matched value: local
  paths, 8-hex ids (a terminal id looks like a short commit), e-mail addresses, credential shapes,
  account and instance ids, host names and HTML comments. Add the words that are private to you to
  its pattern list. Bodies use full 40-character SHAs, because an 8-character short SHA matches the
  internal-id pattern. A hit means fix it and scan again.
- **Tests:** the focused set includes your repo's root-level guard tests (the ones that check the
  whole tree, not one module). After a main merge or a restack, run those guards on the new tip, plus the CI-shaped full
  suite under `--acquire-heavy` or the both-ways signature and caller grep. Check that the RED test
  failed before the fix and that the named mutants die. Run `pgrep -fl pytest` before you trust
  "green": a worker's leftover background suite can still turn red.
- **No data change** in a nit round: compare the `repr` of the affected schema objects before and after.

## 5. Editing mandates (only on the user's explicit words)
1. Back up first: `python3 KIT/bin/state_write.py --from KIT/mandates.json --out KIT/state/mandates.json.bak-$(date -u +%Y%m%dT%H%M%SZ)`.
2. Prepare a changed copy in `KIT/work/`, then `state_write.py --out KIT/mandates.json --from <prepared-file>`.
   Make exactly the change the user asked for, no wider:
   - **Pause:** `"paused": true, "paused_by": "user", "paused_at": "<UTC>", "reason": "<their words>"`.
   - **Resume:** `"paused": false`, and the three fields back to `null`.
   - **Revoke:** that mandate's `"revoked": true`. Never delete it: the history is the audit.
   - **Observe → act:** `"shepherd_mode": "act"`.
   - **New OK:** a new mandate with `granted_by: "user"`, `granted_at`, a `source`, a `tier` (1, or
     2 when its use needs a reading of text; a missing tier refuses), and no wider scope than the
     user's words.
3. Bump `policy_version` (it voids every older GO). Check it parses
   (`python3 -m json.tool KIT/mandates.json >/dev/null`), then run one `check_mandate.py` probe
   that shows the new effect.
4. Mirror it to CAO memory with `memory_store`: key `super-agent-mandates`, the change, the
   user's words and the date.
5. Tell the shepherd in one line: `MANDATES CHANGED policy_version=<v>: <change>`.

## 6. Stalled-worker check (your own workers, when you are awake anyway)
For each worker you created: `curl -s $API/terminals/<id>` (`status`, `last_active`) and
`curl -s "$API/terminals/<id>/output?mode=last"`. It is stalled when:
- its status is `error`, or the terminal is gone (404) and it never called back;
- its status is `waiting_user_answer`, or the output shows a permission or yes/no prompt;
- it is idle or completed with no callback after 10 minutes (it probably ended its turn waiting
  on a background job);
- it is `processing`, `last_active` is over 45 minutes old, and its output tail hasn't changed
  since your last look.
Then message it once with a precise instruction, or delete it (§7) and re-dispatch. Check the
outside state first, so no action runs twice.

## 7. Deleting workers and orphans
- **Delete a terminal** only when every queued job has called back (or you verified each outcome
  directly), and when its `tmux_session`/`tmux_window` differs from yours
  (`GET $API/terminals/$CAO_TERMINAL_ID`). Why: deleting a terminal that shares your pane kills you.
- **Orphan processes** (the shepherd's list or yours): only ones we started. Kill by exact PID
  only when (a) `ps -o args= -p PID` matches what we started **and** (b) for a server,
  `lsof -nP -a -p PID -iTCP:<port> -sTCP:LISTEN` succeeds, or for scratch,
  `lsof -a -p PID +D <scratch>` shows our own path. On any mismatch print STOP and kill nothing.
  Never touch the user's apps or security agents.
- **Scratch:** delete by exact path, one kind at a time (`*-export`, `trees`, `home*`, `venv*`,
  caches, `mut*`), after `lsof +D` shows nobody uses it. Keep `*.md`, `*.json` and small logs.

## 8. Restart recovery (oldest first)
1. Read `KIT/mandates.json`. If `paused` is true, stay paused.
2. Read `KIT/state/outbox.jsonl`, the `KIT/state/outbox/*.pending` entries,
   `go.jsonl`, `holds.json`, the last 40 lines of `PB/log.md`
   and your newest brief files. A `.pending` post is reconciled (publisher RECONCILE steps) before
   anything else is posted on that PR. Run `python3 KIT/bin/state_schema.py check` (exit 0 `OK`):
   a `state_corrupt:<file>:<line>:<why>` line is a damaged file every script refuses. Inspect the
   row, repair it by hand from the evidence (or ask the user), and re-run `check` before anything else.
3. For every action that may or may not have happened (push, post, approval, arm), oldest first,
   check the live state **before** repeating it: `ls-remote` for heads,
   `gh api …/issues/<N>/comments` for an identical body, `outbox.py check`,
   `gh pr view --json autoMergeRequest`. Why: a repeat is a public double post or push.
4. Re-run `check_mandate.py` for each pending action; the mandates may have changed.
5. List live sessions and terminals (`GET $API/sessions`). Keep workers that are still working.
   Never delete the old conductor record from inside the relaunched conductor.
6. Make sure the shepherd is alive; if it isn't, replace it (§9). If only the supervisor is gone,
   check `PB/state/tick_loop.lock`: if its PID is not a live `tick_loop.sh`, run
   `python3 PB/config.py --loop-lock <checked pid>` then `--loop-unlock <checked pid>` to clear the stale lock,
   and start the supervisor again (§9 First start step 4), unless its stopped file says `NOT_WRITER`
   (the lease moved; §Writer lease) or `LOOP_ERROR` (inspect the last error and fix the cause first).
   A new supervisor archives that stopped file as `PB/state/tick_supervisor.stopped.prev`.

## 9. Starting the shepherd and its tick supervisor
You own the tick loop, not the shepherd. `PB/tick_supervisor.sh` runs `tick_loop.sh` detached, posts
each WAKE to the shepherd's inbox and restarts the loop itself; it is keyed to `SHEPHERD_ID`, writes
`PB/state/tick_supervisor.pid` and stops for good on `not_writer` or after five error exits
without a clean poll or exit between them (`LOOP_ERROR` in `PB/state/tick_supervisor.stopped`).
Why: a loop run as an agent's
background job dies with that job, or loses its wake when the agent's turn ends.
On start, an old stopped reason moves to `PB/state/tick_supervisor.stopped.prev`.

**First start:**
1. Make sure the user has installed `pr_shepherd`. Installing is the user's step.
2. One loop, one shepherd: `pgrep -fl 'tick_[l]oop\.sh'` prints nothing, `PB/state/tick_loop.lock` has no
   PID except a live `tick_loop.sh` (a dead holder is stale; run `python3 PB/config.py --loop-lock <checked pid>`
   then `--loop-unlock <checked pid>` to clear it), no supervisor runs (no pid
   file, or its pid's `ps -o args=` isn't `tick_supervisor.sh`), and no other `pr_shepherd` is live.
   A loop you didn't start belongs to its owner: ask it to stop it; don't kill it.
3. Gate, then `assign(agent_profile="pr_shepherd", message="Read and follow KIT/shepherd-playbook.md exactly. Start with §1 Setup. Remap owners --from <previous owner id>. Report to me with send_message (receiver_id omitted).")`.
   The previous owner is the old shepherd's id, or the terminal the `PB/watch.json` owners hold.
4. After `SHEPHERD READY <id> …`, start the supervisor from a **script file** in `KIT/work/` (an
   inline guard matches its own command text):
   `env -i HOME="$HOME" USER="$USER" LOGNAME="$LOGNAME" LANG="$LANG" PATH="$PATH" SHEPHERD_ID=<shepherd id> SENDER_ID=$CAO_TERMINAL_ID python3 KIT/bin/start_tick_supervisor.py PB/tick_supervisor.sh`.
   Check the pid file names a live `tick_supervisor.sh`; if it does not, read
   `PB/state/tick_supervisor.launch.log`, fix the reported error, and do not retry blindly. Tell the user the shepherd's window name;
   expect quiet until a real event.

**Replacing the shepherd** (new profile, dead terminal, planned restart):
1. Ask the old shepherd, if alive, to write its state and a handover file. It rightly refuses to
   stop the loop: it didn't start it.
2. `kill -TERM` the supervisor (the pid file's pid, args checked), then the loop (its `pgrep` pid,
   args checked). TERM ends an interval sleep promptly, but a running `poll.py` can take about
   2 minutes to finish. Confirm the
   pid file and `PB/state/tick_loop.lock` are gone and `pgrep` prints nothing.
3. `delete_terminal` the old shepherd (§7), then First start steps 3–4 with `--from <old shepherd id>`.
