# PR/issue watch: reading one tick

The loop (`tick_loop.sh`) does this on its own. Use this runbook to read a wake, or to run one
tick by hand while the loop is stopped. The watcher is read-only: it never pushes, comments,
merges or edits code. The shepherd and its workers do the work.

## 1. Poll
```sh
cd "${SUPER_AGENT_HOME:-$HOME/super-agent}/pr-watch"
python3 poll.py > reports/latest.json; python3 summary.py reports/latest.json
```
- If the poll reports errors (gh auth, network), follow the partial-poll rule in
  `../rules/watcher.md`.
- No events, no new ready-to-merge and no unwatched PRs means the tick is a no-op. Stop there
  and say nothing.

## 2. Triage each event
| Event | Action |
|---|---|
| External review or comment (not in `identities.ours`, not a bot) | Fetch the full body. Classify each ask: **P0** is broken, security, or data loss. **P1** must be fixed before merge. **P2** is a should-fix the reviewer asked for. **P3** is a nit or optional, deferrable. A label the reviewer uses (P0–P3) wins. |
| Bot review (Copilot, CodeQL/advanced-security) | A CodeQL alert is P0/P1. Copilot is P2/P3. |
| `ours` comment | Agents posted it. Don't act on it; use it to see that the work is already under way. |
| `ci` → FAILURE / `ci_failing` grows | Note the check names and URLs. |
| `mergeable` → CONFLICTING | Main moved, so a merge of main is needed. |
| `head` changed | Someone pushed. Note it; check it against the heads we pushed. |
| `ready_to_merge_new` | The PR is approved and green. The auto-merge gate decides the rest (`../rules/auto-merge.md`). |
| `merged` → true | Tell the conductor. Clean up that PR's workers, then move the PR to `ignore_prs` once the conductor acks. |
| `state` → CLOSED without merge | Tell the conductor. |
| Linked-issue external comment or state change | Report it with that tick's PR events. |
| `unwatched_open_prs` | Your own new PR: add it to `watch.json` with the shepherd as owner. |
| A PR with `owner: null` gets feedback | Report it; don't route it on a guess. |

## 3. Report
One consolidated message per tick, only when something real happened: each PR or issue with
its events, links, P-level and short excerpts. Record each send in `log.md`: the time, the
items and a one-line digest.
