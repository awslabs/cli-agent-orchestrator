---
sidebar_position: 3
---

# Orchestration Patterns

CAO provides three orchestration primitives exposed as MCP tools. Each pattern serves a different coordination need -- synchronous delegation, asynchronous fire-and-forget, or direct inter-agent messaging.

## Pattern Comparison

| | **Handoff** | **Assign** | **Send Message** |
|---|---|---|---|
| Execution | Synchronous -- caller blocks | Asynchronous -- returns immediately | Asynchronous -- inbox delivery |
| Worker lifecycle | Created, used, auto-deleted on success | Created, runs independently | Targets an existing terminal |
| Result delivery | Return value to caller | Worker sends result back via `send_message` | Inbox queue, delivered on IDLE |
| Use case | Sequential pipeline steps | Parallel independent tasks | Ongoing collaboration, swarms |

## Handoff (Synchronous)

**Transfer control to a worker agent and wait for its result.**

```
Supervisor ──handoff──▶ Worker spawned
     │                      │
     │  (blocks)            │ processes task
     │                      │
     │◀──── result ─────────│
     │                  Worker auto-deleted
     ▼
Supervisor continues
```

### Behavior

1. Creates a new terminal with the specified agent profile.
2. Sends the task message and waits for the worker to reach COMPLETED status.
3. Extracts and returns the worker's output to the caller.
4. Automatically deletes the worker terminal (scrollback saved for `cao terminal restore`).

### When to use

- Sequential workflows where each step depends on the previous result.
- Code review chains (write code, then review it, then fix issues).
- Any task where you need the result before proceeding.

### MCP tool signature

```
handoff(
  agent_profile: str,      # e.g. "developer"
  message: str,            # the task to perform
  timeout: int = 600,      # seconds to wait (1-3600)
  working_directory: str = None,  # requires CAO_ENABLE_WORKING_DIRECTORY=true
  model: str = None        # override the agent's model
)
```

## Assign (Asynchronous)

**Spawn a worker agent to work independently. Returns immediately.**

```
Supervisor ──assign──▶ Worker spawned
     │                      │
     │  (returns ID)        │ processes task
     │                      │
     │  (continues)         │
     │                      │ done
     │◀── send_message ─────│ (worker calls back)
     ▼
Supervisor reads result from inbox
```

### Behavior

1. Creates a new terminal, sends the task with callback instructions, returns the terminal ID immediately.
2. The supervisor's terminal ID is automatically appended to the task message (disable with `CAO_ENABLE_SENDER_ID_INJECTION=false`) and recorded as the `caller_id` on the worker terminal.
3. When done, the worker sends results back via `send_message` -- omitting `receiver_id` routes the reply to the assigning terminal automatically.
4. Messages queue in the supervisor's inbox if it is busy.

### When to use

- Parallel execution -- assign N tasks to N workers concurrently.
- Fire-and-forget operations where timing is not critical.
- Long-running tasks where the supervisor should continue other work.

### MCP tool signature

```
assign(
  agent_profile: str,      # e.g. "developer"
  message: str,            # the task to perform
  working_directory: str = None,  # requires CAO_ENABLE_WORKING_DIRECTORY=true
  model: str = None        # override the agent's model
)
```

## Send Message (Direct)

**Deliver a message to an existing terminal's inbox.**

```
Agent A ──send_message──▶ Agent B's inbox
                              │
                              │ (delivered when B is IDLE)
                              ▼
                          Agent B processes message
```

### Behavior

1. Queues a message in the target terminal's inbox (stored in SQLite with status PENDING).
2. If the receiver is IDLE or COMPLETED, delivery is attempted immediately.
3. If the receiver is busy, the message stays PENDING until the InboxService detects an IDLE/COMPLETED status transition.
4. Supports **eager delivery** for providers that buffer input during processing (e.g., Claude Code) -- set `CAO_EAGER_INBOX_DELIVERY=true`.

### When to use

- Multi-turn conversations between agents.
- Swarm patterns where agents collaborate and exchange results.
- Sending instructions to a running worker without waiting for completion.

### MCP tool signature

```
send_message(
  message: str,              # the message content
  receiver_id: str = None    # target terminal ID (8-char hex); omit to route to caller's parent
)
```

When `receiver_id` is omitted, the message routes to the calling terminal's `caller_id` (the terminal that spawned it via `assign`).

## Combining Patterns

The three patterns compose naturally. A common recipe:

```
Supervisor:
  1. assign("analyst", "Analyze sales data")     # parallel worker 1
  2. assign("analyst", "Analyze user data")      # parallel worker 2
  3. handoff("reporter", "Create report template") # sync: get template
  4. Wait for analyst results via inbox
  5. handoff("reporter", "Fill template with: ...")  # sync: final report
```

See `examples/assign/` in the repository for a full worked example of this mixed-pattern workflow.
