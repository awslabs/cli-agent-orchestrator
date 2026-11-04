---
name: pr-health-biweekly-apply
schedule: "0 9 * * 0"
agent_profile: developer
provider: codex
script: ./pr_health_biweekly_guard.py
---

> **STANDING UNATTENDED COMMENT GRANT.** Registering this flow authorizes
> recurring, unattended comments on pull requests in `[[repo]]` under the
> operator's `gh` identity. Comments are the only authorized GitHub mutation.

This flow is an explicit standing authorization to post eligible PR-health
comments on its scheduled runs. It does not authorize any PR-status change.

**This wording is not the authorization boundary.** Apply mode is gated by CAO's
plan-approval gate: enable `workflow.require_approval`, run once, and approve the
refused `plan_id` with `cao workflow approve <plan_id>`. The run `inputs` are part
of the plan identifier, so this `mode=apply` plan is a different plan than the
dry-run flow's and approving one does not authorize the other. On a CAO build
without that gate, or with `workflow.require_approval` left off, the separate
registration of this template is the only control.

Run exactly this command and do not change, omit, or add arguments:

```bash
cao workflow run pr_health --run-id [[run_id_apply]] \
  --input repo=[[repo]] \
  --input as_of=[[as_of]] \
  --input snapshot_id=[[snapshot_id_apply]] \
  --input importance_analysis=true \
  --input importance_provider=claude_code \
  --input importance_agent=reviewer \
  --input mode=apply \
  --json
```

`cao workflow run` blocks until completion, and `--json` returns the
deterministic full result JSON. Report that complete JSON result, including its
`decisions`, `decisions_file`, `artifact_dir`, and `posted_github_comments`
fields. The command's repository is exactly `[[repo]]`; each target PR number is
selected by the workflow from that repository and live-revalidated before
commenting. Do not add labels, change PR state, submit reviews, write commit
statuses or checks, or make any other GitHub mutation. Do not add any command
arguments. If the run ID already exists, inspect its status instead of creating a
duplicate run — a replayed run re-emits its frozen manifest and posts nothing.
