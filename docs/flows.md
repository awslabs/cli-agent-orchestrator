# Flows — Scheduled Agent Sessions

Flows let you schedule agent sessions to run automatically using cron expressions.

> **Command rename:** the command is now `cao schedule` ([#378](https://github.com/awslabs/cli-agent-orchestrator/issues/378)). `cao flow` still works as a deprecated alias and prints a warning to stderr; it will be removed in a future release. Update scripts and cron entries to `cao schedule`. Nothing else changes — flow files, `~/.cao/flows`, and stored schedules are untouched.

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
cao schedule add examples/flow/morning-trivia.md

# 3. List flows to see schedule and status
cao schedule list

# 4. Manually run a flow (optional - for testing)
cao schedule run morning-trivia

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
engine: v2          # Optional Kiro engine; v2 is the default, kas is rejected in Phase 0
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

### Where the script runs

By default the pre-script runs on the same host as the CAO server, with that
process's environment, exactly as it always has.

In a cluster deployment the server can be told to run it in an execution runtime
instead (`CAO_SCRIPT_RUNTIME=<runtime id>`, see the EKS example), which keeps user
code out of the container holding the database. The contract is unchanged — an
executable file whose shebang picks its interpreter, printing
`{"execute": …, "output": {…}}` — with one difference: the remote environment is
constructed rather than inherited. A script there gets `PATH`, `HOME`,
`CAO_API_BASE_URL` and `CAO_FLOW_NAME` and nothing else, so anything else your
script reads from the environment must move into the script or be fetched over the
API. The server still owns the schedule and the JSON verdict; a runtime that
disappears mid-script fails the run rather than being read as "skip". A runtime
that is named but not connected fails the run as well: once you have said where
this code runs, "run it on the server after all" is not a fallback, it is the
outcome the setting exists to prevent. Retry the run when the runtime is back.

`script:` is resolved **on the client**, at `cao schedule add` time: a relative
path resolves beside the flow file, an absolute one is taken as-is, and either
way the file is read there. Registering against a shared server
(`CAO_API_BASE_URL`) then uploads the pre-script's *contents*, not a path — the
flow file never travels, so a path would be meaningless on the server, and an
absolute server path is refused as an arbitrary-file execution vector. The
server writes its own copy beside the flow and runs that (or, with
`CAO_SCRIPT_RUNTIME` set, forwards the contents to the runtime, which never
needs a copy either). A missing script is reported at `cao schedule add` time,
while you are watching, rather than failing inside a scheduled run hours later.

### Where the agent runs

The session the flow launches is placed by its own variable,
`CAO_FLOW_RUNTIME=<runtime id>`, and defaults to this host. Set it in a cluster
deployment where the server container has no tmux of its own; it is separate from
`CAO_SCRIPT_RUNTIME` so a quick health check and a long-lived agent can sit in
different places.

Two behaviours follow the placement. The previous run's session is recycled where
it lives, so a busy agent in a runtime still blocks the next run and a teardown
that does not confirm defers it rather than launching a second agent beside the
first. And a named runtime that is not connected fails the run instead of falling
back to this host — a silent fallback would start the agent in the container the
variable exists to keep it out of. A flow that pins a non-default `engine` cannot
be launched remotely yet and says so.

## Flow commands

```bash
# Add a flow
cao schedule add daily-standup.md

# List all flows (shows schedule, next run time, enabled status)
cao schedule list

# Enable/disable a flow
cao schedule enable daily-standup
cao schedule disable daily-standup

# Manually run a flow (ignores schedule)
cao schedule run daily-standup

# Remove a flow
cao schedule remove daily-standup
```
