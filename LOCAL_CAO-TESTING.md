# Local CAO Testing Notes

Date: 2026-05-10

Working directory used for validation: `C:\dev\marc-hq`

## Summary

CAO is partially functional in this environment. Session discovery, the CAO MCP server, skill loading, and basic inbox messaging are available. Worker task delivery is currently failing: both synchronous `handoff` and asynchronous `assign` fail with HTTP 500 when CAO tries to post input to worker terminals.

This blocks the full Coding Supervisor workflow because implementation work cannot be delivered to the `developer` worker, and therefore cannot proceed to `reviewer`.

## Environment

- Shell: PowerShell
- Supervisor working directory: `C:\dev\marc-hq`
- Current supervisor terminal ID: `315a0bcf`
- Active CAO session: `cao-e165cbce`
- CAO session conductor profile: `code_supervisor`
- Provider shown by CAO status: `codex`

Relevant environment variable:

```powershell
$env:CAO_TERMINAL_ID
```

Observed output:

```text
315a0bcf
```

## CAO Tools Discovered

The CAO MCP server exposed these relevant tools:

- `load_skill`
- `assign`
- `handoff`
- `send_message`

The following CAO-only skills loaded successfully through `load_skill`:

- `cao-session-management`
- `cao-supervisor-protocols`

The local skill paths advertised in the session prompt did not exist on disk:

- `C:\Users\marc\.agents\skills\cao-session-management\SKILL.md`
- `C:\Users\marc\.agents\skills\cao-supervisor-protocols\SKILL.md`

This is not necessarily fatal because `mcp__cao_mcp_server__.load_skill` returned the skill content successfully.

## Commands Run

### CAO Terminal ID

```powershell
$env:CAO_TERMINAL_ID
```

Observed output:

```text
315a0bcf
```

### Session List

```powershell
cao session list
```

Observed output:

```text
SESSION                   CONDUCTOR    STATUS          TERMINALS
-----------------------------------------------------------------
cao-e165cbce              315a0bcf     idle            2
```

### Session Status With Workers

```powershell
cao session status cao-e165cbce --workers
```

Initial observed output after first failed handoff:

```text
Session:  cao-e165cbce
Terminal: 315a0bcf
Agent:    code_supervisor
Provider: codex
Status:   idle

Last response:
gpt-5.5 default · C:\dev\marc-hq

ID           AGENT                PROVIDER        STATUS
-----------------------------------------------------------------
77039d13     developer            codex           N/A
```

Observed output after the later failed async assignment:

```text
Session:  cao-e165cbce
Terminal: 315a0bcf
Agent:    code_supervisor
Provider: codex
Status:   idle

Last response:
gpt-5.5 default · C:\dev\marc-hq

ID           AGENT                PROVIDER        STATUS
-----------------------------------------------------------------
77039d13     developer            codex           N/A
868cdedf     developer            codex           N/A
```

### Session Status JSON

```powershell
cao session status cao-e165cbce --json
```

Observed output:

```json
{
  "session": "cao-e165cbce",
  "conductor": {
    "id": "315a0bcf",
    "agent_profile": "code_supervisor",
    "provider": "codex",
    "status": "idle",
    "last_output": "gpt-5.5 default \u00b7 C:\\dev\\marc-hq"
  }
}
```

Note: the JSON status did not include worker details, even though `--workers` showed worker terminals.

### CAO Info

```powershell
cao info
```

Observed output:

```text
Database path: C:\Users\marc\.aws\cli-agent-orchestrator\db\cli-agent-orchestrator.db
Session ID: cao-e165cbce
Active terminals: 3
```

### CAO Help

```powershell
cao --help
```

Observed top-level commands:

```text
Commands:
  env         Manage CAO environment variables.
  flow        Manage scheduled agent flows.
  info        Display information about the current session.
  init        Initialize CLI Agent Orchestrator database.
  install     Install an agent from local store, built-in store, URL, or...
  launch      Launch cao session with specified agent profile.
  mcp-server  Start the CAO MCP server.
  session     Manage CAO sessions.
  shutdown    Shutdown tmux sessions and cleanup terminal records.
  skills      Manage installed skills.
```

## Validation Task File Created

A task file was created at:

```text
C:\dev\marc-hq\.cao-validation\tasks\developer-smoke.md
```

Purpose: provide a minimal implementation task to the `developer` worker using an absolute task file path, matching the Coding Supervisor protocol.

Task content requested the developer to create:

```text
C:\dev\marc-hq\.cao-validation\artifacts\cao-smoke.ps1
```

Expected script output:

```text
CAO developer smoke test passed
```

The artifact was not created because worker input delivery failed before the developer received the task.

## Failed Worker Delivery Tests

### Blocking Handoff

Tool call attempted:

```text
mcp__cao_mcp_server__.handoff(
  agent_profile = "developer",
  message = "Please complete the CAO validation task described at absolute path C:\\dev\\marc-hq\\.cao-validation\\tasks\\developer-smoke.md. Work from C:\\dev\\marc-hq. Return the absolute paths of all files created or changed and the verification command/output. Do not read or output secrets.",
  timeout = 180
)
```

Observed result:

```json
{
  "success": false,
  "message": "Handoff failed: 500 Server Error: Internal Server Error for url: http://127.0.0.1:9889/terminals/77039d13/input?...",
  "output": null,
  "terminal_id": null
}
```

The error URL targeted worker terminal:

```text
77039d13
```

### Async Assign

Tool call attempted:

```text
mcp__cao_mcp_server__.assign(
  agent_profile = "developer",
  message = "Please complete the CAO validation task described at absolute path C:\\dev\\marc-hq\\.cao-validation\\tasks\\developer-smoke.md. Work from C:\\dev\\marc-hq. Return the absolute paths of all files created or changed and the verification command/output. Do not read or output secrets. When done, send results back to terminal 315a0bcf using the send_message tool."
)
```

Observed result:

```json
{
  "success": false,
  "terminal_id": null,
  "message": "Assignment failed: 500 Server Error: Internal Server Error for url: http://127.0.0.1:9889/terminals/868cdedf/input?..."
}
```

The error URL targeted worker terminal:

```text
868cdedf
```

### Direct CLI Send To Worker

Command attempted:

```powershell
cao session send cao-e165cbce "Please report your current status only. Do not perform code changes." --terminal 77039d13 --timeout 30
```

Observed output:

```text
Error: Failed to connect to cao-server: 500 Server Error: Internal Server Error for url: http://127.0.0.1:9889/terminals/77039d13/input?message=Please+report+your+current+status+only.+Do+not+perform+code+changes.
```

This shows the failure is not specific to the MCP `handoff` or `assign` wrapper. The CLI also fails when posting input to the worker terminal through the same CAO server endpoint.

## Worker Terminal Details

### Worker `77039d13`

Command:

```powershell
cao session status cao-e165cbce --terminal 77039d13
```

Observed output:

```text
Session:  cao-e165cbce
Terminal: 77039d13
Agent:    developer
Provider: codex
Status:   idle

Last response:
gpt-5.5 default · C:\dev\marc-hq
```

### Worker `868cdedf`

Command:

```powershell
cao session status cao-e165cbce --terminal 868cdedf
```

Observed output:

```text
Session:  cao-e165cbce
Terminal: 868cdedf
Agent:    developer
Provider: codex
Status:   idle

Last response:
gpt-5.5 default · C:\dev\marc-hq
```

## Successful Messaging Test

The MCP `send_message` tool worked for a supervisor-to-self message.

Tool call:

```text
mcp__cao_mcp_server__.send_message(
  receiver_id = "315a0bcf",
  message = "CAO self-message smoke test from supervisor terminal 315a0bcf. No action required."
)
```

Observed result:

```json
{
  "success": true,
  "message_id": 1,
  "sender_id": "315a0bcf",
  "receiver_id": "315a0bcf",
  "created_at": "2026-05-10T23:13:44.791820"
}
```

This suggests the CAO MCP server and inbox storage path are at least partially working. The failing area appears to be terminal input delivery through:

```text
http://127.0.0.1:9889/terminals/{terminal_id}/input
```

## Git Working Tree Observations

Command:

```powershell
git status --short
```

Observed output from `C:\dev\marc-hq`:

```text
 M .base/data/state.json
?? .cao-validation/
```

The `.base/data/state.json` modification was pre-existing or not created as part of this validation. The `.cao-validation/` directory was created during this CAO validation attempt.

## Current Diagnosis

What is healthy:

- CAO CLI is installed and callable.
- CAO session exists and is discoverable.
- Supervisor terminal ID is set.
- CAO MCP server exposes the expected orchestration tools.
- CAO MCP `load_skill` works.
- CAO MCP `send_message` can write a message to the supervisor inbox.
- Worker terminals are created or registered and show as idle.

What is broken:

- Posting input to worker terminals fails with HTTP 500.
- The failure happens through both MCP and direct CLI paths.
- Full developer-to-reviewer workflow cannot be validated until worker input delivery works.

Most likely fault area:

- CAO server endpoint handling terminal input:

```text
POST /terminals/{terminal_id}/input
```

or the backing mechanism that injects input into the worker terminal process.

## Suggested Next Debug Steps

1. Inspect CAO server logs around the HTTP 500 requests to `/terminals/{terminal_id}/input`.
2. Restart or relaunch the CAO session and CAO MCP server, then retry a direct worker send before testing `handoff`.
3. Check whether the worker terminals are fully attached to input-capable processes, since status reports them as idle but input posting fails.
4. Check whether the CAO database has stale or mismatched terminal records for `77039d13` and `868cdedf`.
5. Re-run a minimal direct CLI test after cleanup:

```powershell
cao session send cao-e165cbce "status only" --terminal <developer-terminal-id> --timeout 30
```

6. Once worker input delivery is fixed, rerun the smoke flow:

- supervisor writes task file
- `developer` creates the script artifact
- supervisor sends created artifact to `reviewer`
- reviewer approves or requests changes
- repeat until approved

