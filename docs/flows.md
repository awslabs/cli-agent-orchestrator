# Flows — Scheduled Agent Sessions

Flows let you schedule agent sessions to run automatically using cron expressions.

## Prerequisites

Install the agent profile you want to use:

```bash
cao install developer
```

## Quick start

The example flow asks a simple world trivia question every morning at 7:30 AM.

```bash
# 1. Start the cao server
cao-server

# 2. In another terminal, add a flow
cao flow add examples/flow/morning-trivia.md

# 3. List flows to see schedule and status
cao flow list

# 4. Manually run a flow (optional - for testing)
cao flow run morning-trivia

# 5. View flow execution (after it runs)
tmux list-sessions
tmux attach -t <session-name>

# 6. Cleanup session when done
cao shutdown --session <session-name>
```

> **Important:** `cao-server` must be running for flows to execute on schedule.

## Example 1: simple scheduled task

A flow that runs at regular intervals with a static prompt (no script needed).

**File: `daily-standup.md`**

```yaml
---
name: daily-standup
schedule: "0 9 * * 1-5"  # 9am weekdays
agent_profile: developer
provider: kiro_cli  # Optional, defaults to kiro_cli
---

Review yesterday's commits and create a standup summary.
```

## Example 2: conditional execution with a health check

A flow that monitors a service and only executes when there's an issue.

**File: `monitor-service.md`**

```yaml
---
name: monitor-service
schedule: "*/5 * * * *"  # Every 5 minutes
agent_profile: developer
script: ./health-check.sh
---

The service at [[url]] is down (status: [[status_code]]).
Please investigate and triage the issue:
1. Check recent deployments
2. Review error logs
3. Identify root cause
4. Suggest remediation steps
```

**Script: `health-check.sh`**

```bash
#!/bin/bash
URL="https://api.example.com/health"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$URL")

if [ "$STATUS" != "200" ]; then
  # Service is down - execute flow
  echo "{\"execute\": true, \"output\": {\"url\": \"$URL\", \"status_code\": \"$STATUS\"}}"
else
  # Service is healthy - skip execution
  echo "{\"execute\": false, \"output\": {}}"
fi
```

## Flow commands

```bash
# Add a flow
cao flow add daily-standup.md

# List all flows (shows schedule, next run time, enabled status)
cao flow list

# Enable/disable a flow
cao flow enable daily-standup
cao flow disable daily-standup

# Manually run a flow (ignores schedule)
cao flow run daily-standup

# Remove a flow
cao flow remove daily-standup
```
