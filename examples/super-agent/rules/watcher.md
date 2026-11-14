# Rule: the PR/issue watcher

The watcher is the zero-token poll loop in `PB` (`KIT/pr-watch`). It reads GitHub and decides
whether to wake the shepherd. The shepherd runs it; this file is the rule the shepherd follows.

**Hard rules**
- The loop never pushes, comments, reviews, labels, merges, closes or edits code. `poll.py`
  contains no GitHub write command.
- The only files the loop writes are under `PB`: `state/`, `reports/`, `digest.md` and `log.md`.
  The shepherd also edits `PB/watch.json` (owners, new PRs, `ignore_prs`), after a backup.
- **One owner for everything.** Every watched item routes to the shepherd. "Tell the user" in
  any rule means "tell the conductor"; the conductor decides what reaches the user.

## watch.json
Copy `PB/watch.example.json` to `PB/watch.json` and edit it:
- `repo`: `your-org/your-repo`.
- `identities.ours`: your GitHub login(s). The loop and the auto-merge gate both read it; an
  author not listed here is never treated as "ours".
- `prs` and `issues`: what to watch, keyed by number, each with an `owner`
  (`{"session": ..., "terminal": ...}` or `null` for report-only).
- `ignore_prs`: numbers the loop must skip (for example, merged PRs after the conductor acks).
- `known_ci_failures`: `{"<pr>": ["check name"]}` or `{"<pr>": {"checks": [...], "reported": true}}`
  for a failure already reported once. Prose descriptions never suppress a failure.

Your own open PRs that are not yet in `watch.json` show up as `unwatched_open_prs` on every
poll (`gh pr list --author @me`). PRs in the reviewer's ledger (`KIT/reviews/ledger.json`) are
watched for new commits and replies too.

## Each tick (the loop does this by itself)
1. Poll every watched PR and issue, then diff against the last snapshot in `PB/state/`.
2. Pass the report through `wake_rules.py`: `WAKE` for real events on your own PRs (feedback,
   red CI, review decisions, conflicts, state changes, an approved or armed PR that fell
   behind main) and for new commits, replies and state changes on PRs you reviewed. Everything
   else goes to the digest, which rides along with the next wake, or wakes once by itself when
   its oldest line is more than 24 hours old.
3. On `WAKE`, the loop exits with `WAKE <report> [retry=<report2>] :: <reasons>`, and the
   shepherd takes over (`shepherd-playbook.md` §2). Otherwise it logs one line and sleeps
   (30 minutes by default; `INTERVAL` seconds).

## Partial polls
`poll.py` saves state for every item it polled successfully, even when other items in the same
run errored. Withholding a partial poll would lose those items' events, because the next poll
diffs against the state already saved. Errored items are not saved, so their changes show up
on their next successful poll.
1. When `errors` is non-empty, the loop polls once more about 30 s later and keeps both
   reports: use the events from report 1, plus report 2's events for the items that errored in
   report 1.
2. **Always triage the events of every successfully polled item**, even if others still errored.
3. Track per-item failures (item → consecutive failed ticks) and reset an item's count when it
   polls cleanly.
4. Tell the conductor about poll errors **only** when:
   - (a) the same item fails 3 ticks in a row;
   - (b) more than 5 items fail in one tick;
   - (c) any auth, rate-limit or whole-poll failure, such as `gh` exiting before any item.

   Otherwise log the error in `PB/log.md` and say nothing.
