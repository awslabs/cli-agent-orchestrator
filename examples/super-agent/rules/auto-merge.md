# Rule: arm auto-merge and update the branch on approved, green PRs

This rule is the user's standing OK behind mandates M5 (`update_branch`) and M6
(`arm_automerge`). It overrides the watcher's "never merge" **only** for these two actions.
Every other rule still stands. `KIT/bin/automerge_gate.py` enforces it in code; this file says
what the gate checks and why.

## Scope: all of these must hold
- The PR is in `watch.json`, owned by the shepherd, **and its author is in `identities.ours`**.
  For anyone else's PR, the shepherd sends the conductor a "ready to merge" FYI instead.
- `baseRefName == "main"`. **Never** on a stacked PR whose base is another branch: auto-merge
  would merge it into that branch.
- `state == OPEN`, `isDraft == false`, and `reviewDecision == APPROVED`.
- **All green:** no check is pending, every completed check is SUCCESS, SKIPPED or NEUTRAL, and
  every required check is present.
- `mergeable != CONFLICTING`. A conflicting PR goes to a main-merger, never to this rule.
- The PR changes no more than `max_changed_lines` (1500 in the shipped mandates; above that the
  gate says `ESCALATE_SIZE` and the conductor takes a second look). For updates, the cap is
  `min(1500, every M6 cap, revoked ones included)`.
- The PR is not listed in the mandate's `never_prs`.
- The hold-on-findings rule (`hold-on-findings.md`) finds nothing.

## Actions, in this order, each at most once per head, one worker each
1. **If `autoMergeRequest` is null:** `gh pr merge <n> -R your-org/your-repo --auto --squash
   --match-head-commit <current head sha>`. Use the repository's default squash title and
   message: don't pass `--subject` or `--body`. A BEHIND PR is armed first.
2. **If the PR is armed and `mergeStateStatus == BEHIND`,** at a later step:
   `gh pr update-branch <n> -R your-org/your-repo`. That is a merge update, never `--rebase`.
   Record the new head SHA as ours.
3. **Read it back:** `gh pr view <n> --json autoMergeRequest,headRefOid,mergeStateStatus,reviewDecision`.
   If an update dismissed the approval (your repo's branch protection decides that), report it;
   otherwise report `AUTOMERGE <n> armed head=<sha> updated=<yes|no>`.

GitHub does the merge. Auto-merge must be enabled on the repository.

## Never
- Merge directly, without `--auto`.
- Force-push, or change the merge method.
- Disable or re-arm an auto-merge someone else armed.
- Act on a PR outside the scope above.
- On any `gh` error from these commands, stop and report the exact stderr. Don't retry more
  than once.

Back up `watch.json` before changing it, and log every action in `PB/log.md`.
