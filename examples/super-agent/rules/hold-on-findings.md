# Rule: hold auto-merge when a review lists findings

An addition to `auto-merge.md`.

**Why:** an approving review can still list a minor finding (a P3, a nit). If auto-merge arms
the moment the approval lands, that finding merges unaddressed and needs a follow-up PR. So an
approval that lists findings is not a green light.

**The rule:** before arming auto-merge or running update-branch on one of our PRs, read
every review (any state, on any commit) and every issue or review comment posted by someone
other than us since the **last non-merge commit** of the PR. A merge of main or an
update-branch never clears an earlier finding.

**If any of them lists findings** (a P1, P2 or P3, or any "fix", "should" or "nit" item):
- **Don't arm.** The gate prints `HOLD_FINDINGS` and saves the hold in `KIT/state/holds.json`.
- **Report it to the conductor:** `HOLD-FINDINGS #<N> head=<8> <detail>`.
- **Dispatch a fixer.** The fix goes out as a fast-forward push under mandate M1, with a short
  factual reply under M2.
- **After that push,** if the PR is still approved and green, with no new findings, the gate
  says `ARM` on a later wake.

**Real findings** (a non-zero count such as "1 P3", a line-start item such as "1. **P3:" or
`nit:`, or a list item under a Findings/Issues heading) stay held until a newer non-merge fix
commit, or until the conductor relays the user's decline. `KIT/bin/holds.py` refuses to clear
them.

**A clean body the keyword scan misread** ("APPROVE (0 P1, 0 P2, 0 P3). The previous P3 is
fixed. Findings: None.") is cleared by reading, under mandate M8: the shepherd proposes
`WOULD … clear_hold`, the conductor reads the same body file and says GO (in every mode), and
`holds.py clear` binds the clear to that exact text. Editing the text later makes it hold again.

**Only an approval with no findings** auto-merges at once.

**Unchanged:** PRs in `never_prs` never auto-merge. Drafts are left alone.
