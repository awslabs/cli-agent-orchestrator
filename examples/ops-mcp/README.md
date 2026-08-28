# Ops MCP Example

This example drives a complete CAO session lifecycle from a process that is
**outside** CAO, using the typed tools of `cao-ops-mcp-server` over stdio MCP.
It discovers profiles, launches a session, waits for it to be ready, sends a
follow-up, reads the result, and shuts down — without building a single shell
command and without ever becoming a CAO terminal.

This is **not** an orchestration example. Nothing here uses `handoff`, `assign`,
or `send_message`; those are in-session tools that require a terminal CAO
already manages. For delegation between agents see [`../assign/`](../assign/)
and [`../orchestration/`](../orchestration/). For choosing between control
surfaces see [Control planes](../../docs/control-planes.md).

## Naming note

The bundled profile is prefixed `ops_mcp_` so `cao install` cannot overwrite a
built-in profile such as `developer`.

## Pattern Overview

| Concern | External management (`cao-ops-mcp`) | In-session orchestration (`cao-mcp-server`) |
|---|---|---|
| Who calls it | Any MCP client outside CAO | An agent inside a CAO terminal |
| Terminal context | None — never reads `CAO_TERMINAL_ID` | Requires it; each agent knows only its own |
| Creates sessions | Yes, `launch_session` | No |
| Talks to an agent | `send_session_message` by terminal id | `handoff` / `assign` / `send_message` |
| Ends a session | Yes, `shutdown_session` | No |

The two planes are complements, not alternatives: this example creates the
session that in-session tools then work inside.

## Setup

```bash
# 1. Start the CAO server (leave running in its own terminal)
cao-server

# 2. Install the example's worker profile
cao install examples/ops-mcp/ops_mcp_worker.md
```

The example spawns `cao-ops-mcp-server` itself as a stdio subprocess, so it does
not need to be started separately. It does need `cao-server` reachable at
`http://127.0.0.1:9889` (override with `CAO_API_PORT`), because the ops server
talks to CAO over that API.

## Usage

```bash
# Minimal run
python3 examples/ops-mcp/run.py --task "Reply with the word ready and stop."

# Pin a provider and a working directory, and send a second message
python3 examples/ops-mcp/run.py \
  --profile ops_mcp_worker \
  --provider claude_code \
  --working-directory /tmp/ops-mcp-demo \
  --task "Create hello.txt containing the word ready, then confirm." \
  --follow-up "Now tell me the file size in bytes."
```

Exit codes: `0` the lifecycle completed, `1` a lifecycle step failed, `2` the
server did not expose the expected tools.

## What the run does

```
list_profiles          -> confirm the profile is installed before launching
launch_session         -> create a named session, deliver the task on launch
get_terminal_status    -> poll until THIS turn finishes (see below)
read_session_output    -> record the turn boundary before a follow-up
send_session_message   -> optional follow-up
get_terminal_status    -> poll until the follow-up turn finishes
read_session_output    -> read the agent's last response
shutdown_session       -> always, including when a step above failed
get_session_info       -> verify the session is actually gone
```

Three details are load-bearing rather than incidental.

**`launch_session` returns immediately.** It hands back `session_name` and
`terminal_id` while provider startup and message delivery continue in the
background, so the caller must poll. `--timeout` bounds each turn.

**A ready status is not proof your turn ran.** This is the subtle one. `idle` can
precede delivery of a queued message, and `completed` can belong to the
*previous* turn — the messaging tool only promises the message was queued, so a
cached status may still hold its pre-dispatch value. Accepting the first ready
enum therefore reads the wrong turn's output, and can shut the session down while
the follow-up is still pending. So the example requires evidence that *this* turn
ran: activity observed while waiting, output produced past a recorded baseline,
or `completed` when the baseline was not already a ready state (reaching
completed requires dispatched input, so it cannot predate the first turn).

**Cleanup is verified, not assumed.** Shutdown runs in a `finally`, and the
session is then confirmed absent with `get_session_info`. An unverified cleanup
fails the run with exit 1, because reporting success while a session is still
alive leaves behind exactly the orphan this example is meant to prevent. A
session that is already gone counts as verified however it got there.

## Reading status honestly

A reported status is inferred from the rendered terminal screen, not from a
structured protocol, so it can disagree with reality. This example polls status
and then reads output, which is the minimum discipline; before reporting
progress to a human, corroborate the two. See
[`cao-session-liveness`](../../skills/cao-session-liveness/SKILL.md) for the
dead-session signatures and the reason a completed `handoff` leaves no worker
terminal behind.

## Expected failures

| Situation | What you get |
|---|---|
| `cao-server` not running | `launch failed: ... Connection refused` |
| Profile not installed | `profile 'ops_mcp_worker' is not installed`, with the install command; nothing is launched |
| Working directory does not exist | Launch succeeds, then the agent fails confusingly — pass an absolute path that exists |
| Agent never produces evidence of the turn | `TimeoutError` naming the last status, then shutdown still runs |
| Session still present after shutdown | `cleanup not verified`, exit 1 — deliberately a failure, not a warning |
| Session already gone at cleanup | Counted as verified cleanup; the run reports its result normally |

## Tests

Protocol-level tests run in CI without provider credentials or a running
server — `run_lifecycle` takes any object with `call_tool`, so a recording
double stands in for a live MCP session:

```bash
uv run pytest test/examples/test_ops_mcp_example.py -v
```

They lock the tool ordering, the turn-evidence rules, the verified-cleanup
guarantee, result parsing across `structuredContent` / JSON text / plain text /
error results, and — by AST assertion — that the example never reads
`CAO_TERMINAL_ID`.

Two test classes exist specifically to keep earlier mistakes from returning:
`TestTurnEvidence` proves a stale `idle` or a previous turn's `completed` does not
end the wait, and `TestVerifiedCleanup` proves an unverified cleanup fails the run
rather than reporting success.

## See Also

- [Control planes](../../docs/control-planes.md) — choosing between CLI, MCP, and Web UI
- [HTTP API](../../docs/api.md) — the REST surface the ops server calls
- [`cao-session-liveness`](../../skills/cao-session-liveness/SKILL.md) — verifying a session is alive
- [`../assign/`](../assign/) and [`../orchestration/`](../orchestration/) — in-session delegation
- [`../headless-ci/`](../headless-ci/) — the same lifecycle driven by shell instead of MCP
