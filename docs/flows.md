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
schedule: "0 9 * * mon-fri"  # 9am weekdays
agent_profile: developer
provider: kiro_cli  # Optional, defaults to kiro_cli
engine: v2          # Optional Kiro engine; v2 is the default, v3 (alias kas) is not launchable yet
---

Review yesterday's commits and create a standup summary.
```

The scheduler (APScheduler) counts the day of week from Monday as `0`, so write weekdays as names (`mon-fri`), because a numeric `1-5` runs Tuesday through Saturday.

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

## Manage flows over the API

`cao-server` has routes to check, create, and edit a flow, so you do not have to write the flow file yourself. [API Overview](api.md#flows) lists every flow route and its scopes. These three are the authoring routes.

**Check a definition.** `POST /flows/validate` checks a definition and writes nothing. It takes `name`, `schedule`, `agent_profile`, `provider` (default `kiro_cli`), and `prompt_template`, and it needs the read, write, or admin scope.

```bash
curl -s -X POST http://localhost:9889/flows/validate \
  -H 'Content-Type: application/json' \
  -d '{"name": "daily-standup", "schedule": "0 9 * * mon-fri", "agent_profile": "developer", "prompt_template": "Summarize yesterday."}'
```

`prompt_template` is required and must be a string (an empty string is fine), the same as for `POST /flows`.

The answer is `200` for any JSON object, whatever its values:

```json
{"valid": true, "errors": [], "next_runs": ["2026-11-23T09:00:00-08:00", "2026-11-24T09:00:00-08:00", "2026-11-25T09:00:00-08:00"]}
```

- `errors` holds one `{field, message}` entry for each field that fails: a name outside `^[A-Za-z0-9_-]{1,64}$`, a schedule the scheduler refuses, a schedule longer than 256 characters, a schedule that does not fire within 41 years (`this schedule never fires`), a control character in `schedule`, `agent_profile`, or `provider`, an empty or non-string `agent_profile` or `provider`, or a name that a registered flow already uses.
- `next_runs` holds the next three fire times in the server's local time zone, computed by the scheduler. It is empty when the schedule has an error.
- If `TZ` is not a zoneinfo name (for example `AEST-10` or `EST5EDT` without a zone file), the server logs a warning and shows fire times at the current UTC offset, with no daylight-saving rules, so a fire time after the next clock change shows the offset of today. Flows still run at the stored local wall time. Set a zoneinfo name such as `Australia/Sydney` in `TZ` to see the right offset for every date.
- The route never writes a file or a row, takes no `script` field, and checks no path.

**Create a flow.** `POST /flows` takes the same fields, plus an optional `enabled` boolean that defaults to `true`. Send `"enabled": false` to register the flow without arming it, then enable it with `POST /flows/{name}/enable` or `cao schedule enable`.

**Edit a flow.** `PUT /flows/{name}` changes `schedule`, `agent_profile`, `provider`, and `prompt_template`. Each is optional, and a field you leave out keeps its stored value.

```bash
curl -s -X PUT http://localhost:9889/flows/daily-standup \
  -H 'Content-Type: application/json' \
  -d '{"schedule": "30 8 * * mon-fri"}'
```

- The server merges your fields over the stored flow, validates the result with the same rules as validate, and rewrites the flow file through a temporary file and a rename.
- It edits only the front-matter keys you send, so `script`, `engine`, and any other key stay as they are. The server re-serializes the whole file, so comments inside the front matter are not kept.
- An enabled flow gets a new `next_run`. A disabled flow stays disabled and keeps its stored `next_run`.
- If the flow's file is not under `$CAO_HOME_DIR/flows` (by default `~/.aws/cli-agent-orchestrator/flows`), or no longer exists, the server answers `409` and does not touch the file. That includes flows you added with `cao schedule add ~/.cao/flows/<file>.md`. To edit such a flow through the API, move its file into `$CAO_HOME_DIR/flows`, then run `cao schedule remove <name>` and `cao schedule add <new path>`. The remove drops only the registration, and adding the flow again enables it. You can also edit the file by hand.
- The body accepts only those four fields. Any other field, including `script`, answers `422`.
- It needs the write or admin scope.

### Changes for existing callers

These changes affect `POST /flows` and `cao schedule add`. A request with a valid definition behaves as before.

- **A bad definition answers 422 and writes nothing.** Before, a schedule the scheduler refused answered `404` after the flow file was already written. Now the route validates first and answers `422` with `{"detail": {"errors": [{"field": "...", "message": "..."}]}}`, and no file is created.
- **The 422 body has more than one shape.** A body that fails parsing (a missing field, a wrong type, a `name` that contains `/`, `\`, or `..`, a control character in `schedule`, `agent_profile`, or `provider`, or an empty `agent_profile` or `provider`) keeps FastAPI's shape: `detail` is a list of `{loc, msg, type}` entries. A definition that parses but fails validation uses `detail.errors`. Any other `ValueError` while the server registers the file answers `422` with `detail` as a message string (it was `404`). Check whether `detail` is a list, an object, or a string before you read it.
- **A name that is already registered answers 409.** Before, the route overwrote the existing flow file and then answered `500`. Now it answers `409` and leaves the file and the row unchanged. It also answers `409` when the new flow's file is the file another flow is registered from. A definition error takes precedence: a taken name with a bad schedule answers `422`.
- **`enabled` is new and optional.** Callers that omit it still create enabled flows.
- **`cao schedule add` applies the same checks.** It also refuses an empty `agent_profile` or `provider`, a control character in `schedule`, `agent_profile`, or `provider`, and a value that is not a string, such as an unquoted number as the name. The messages for a bad name or schedule start with the same text as before.

## Workflow scheduling example

[`examples/workflows/pr-health/`](../examples/workflows/pr-health/) combines a
parameterized Python workflow with a scheduled flow. Its weekly cron entry uses
a deterministic guard to produce an exact 14-day cadence across month
boundaries. Scheduled runs default to non-mutating dry-run mode.
Its separately registered apply template authorizes comments only; the workflow
cannot change labels, PR state, reviews, statuses, or checks.

Registering that apply template is not the authorization boundary — the flow
prompt says so explicitly. Because a run's `inputs` are part of the plan
identifier, `mode=apply` is a different plan than `mode=dry_run`, so it needs its
own `cao workflow approve <plan_id>` once `workflow.require_approval` is enabled.
The example also bounds its own noise: notifications are grouped into epochs with
a 90-day re-entry cooldown and lifetime caps, so a permanently stale PR receives a
small constant number of comments rather than one per run forever.
