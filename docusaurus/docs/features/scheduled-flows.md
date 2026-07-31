---
sidebar_position: 4
---

# Scheduled Flows

Flows let you schedule agent sessions to run automatically using cron expressions. A flow is a Markdown file with YAML frontmatter that specifies when to run, which agent profile to use, and optionally a gating script for conditional execution.

## Prerequisites

- `cao-server` must be running for scheduled execution.
- The agent profile referenced in the flow must be installed.

## Quick Start

```bash
# 1. Start the CAO server
cao-server

# 2. Add a flow
cao schedule add daily-standup.md

# 3. List flows to see schedule and status
cao schedule list

# 4. Manually trigger a flow (ignores the schedule; the gating script still runs)
cao schedule run daily-standup

# 5. View the running session (flows use a cao-flow- prefix)
tmux attach -t cao-flow-daily-standup
```

## Flow File Format

A flow file is Markdown with YAML frontmatter containing schedule metadata. The markdown body is the prompt sent to the agent.

### Simple scheduled task

**`daily-standup.md`**
```markdown
---
name: daily-standup
schedule: "0 9 * * 1-5"
agent_profile: developer
provider: kiro_cli
---

Review yesterday's commits and create a standup summary.
```

### Frontmatter fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique flow identifier |
| `schedule` | Yes | Cron expression (5-field: minute hour day month weekday) |
| `agent_profile` | Yes | Agent profile to launch |
| `provider` | No | Provider override (defaults to `kiro_cli`) |
| `script` | No | Path to a gating script for conditional execution |

## Conditional Execution with Gating Scripts

A gating script decides at runtime whether the flow should execute. The script must output JSON with an `execute` boolean and an `output` object whose keys become template variables in the prompt. Both keys are required — omitting either fails the flow.

**`monitor-service.md`**
```markdown
---
name: monitor-service
schedule: "*/5 * * * *"
agent_profile: developer
script: ./health-check.sh
---

The service at [[url]] is down (status: [[status_code]]).
Please investigate and triage the issue.
```

**`health-check.sh`**
```bash
#!/bin/bash
URL="https://api.example.com/health"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$URL")

if [ "$STATUS" != "200" ]; then
  echo "{\"execute\": true, \"output\": {\"url\": \"$URL\", \"status_code\": \"$STATUS\"}}"
else
  echo "{\"execute\": false, \"output\": {}}"
fi
```

When `execute` is `false`, the flow is skipped for that tick. Template variables (`[[key]]`) in the prompt body are replaced with values from the `output` object; a `[[key]]` with no matching entry raises an error and aborts the flow.

## Flow Commands

```bash
# Add a flow from file
cao schedule add daily-standup.md

# List all flows (schedule, next run, enabled status)
cao schedule list

# Enable a disabled flow
cao schedule enable daily-standup

# Disable a flow (keeps it registered but skips execution)
cao schedule disable daily-standup

# Manually run a flow (ignores the schedule; the gating script still runs)
cao schedule run daily-standup

# Remove a flow entirely
cao schedule remove daily-standup
```

## How Scheduling Works

1. `cao-server` runs a background scheduler that evaluates registered flows every minute.
2. When a flow's cron expression matches the current time and the flow is enabled:
   - If a `script` is defined, it is executed. The flow only proceeds if the output contains `"execute": true`.
   - A new CAO session is launched with the specified `agent_profile` and `provider`.
   - The prompt (with any template variables resolved) is sent to the agent.
3. The resulting session runs in tmux and can be attached to, monitored via the Web UI, or shut down with `cao shutdown`.

## Example: Morning Trivia

```markdown
---
name: morning-trivia
schedule: "30 7 * * *"
agent_profile: developer
---

Tell me an interesting fact about world history. Keep it to one paragraph.
```

```bash
cao schedule add examples/flow/morning-trivia.md
cao schedule list
```
