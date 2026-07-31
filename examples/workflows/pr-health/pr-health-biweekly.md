---
name: pr-health-biweekly
schedule: "0 9 * * 0"
agent_profile: developer
provider: codex
script: ./pr_health_biweekly_guard.py
---

This is a pre-authorized scheduled PR-health dry run.

Run exactly this command and do not change, omit, or add arguments:

```bash
cao workflow run pr_health --run-id [[run_id]] \
  --input repo=[[repo]] \
  --input as_of=[[as_of]] \
  --input snapshot_id=[[snapshot_id]] \
  --input importance_analysis=true \
  --input importance_provider=claude_code \
  --input importance_agent=reviewer \
  --input mode=dry_run \
  --input close_allowlist= \
  --json
```

Wait for the command to finish and report its structured result. Do not perform
any GitHub mutation. The empty closure allowlist is intentional and must remain
empty. If the run ID already exists, inspect its status instead of creating a
duplicate run.
