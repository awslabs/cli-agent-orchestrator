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
2. The supervisor's terminal ID is automatically appended to the task message (disable with `CAO_ENABLE_SENDER_ID_INJECTION=false`) and recorded as the `caller_id` on the worker terminal. One exception: a task for a **kiro-cli** worker that starts with `@` (a prompt invocation such as `@agent-sop:code-analysis query="..."`) is delivered unmodified, because kiro reads any trailing text as parameter values. Such a worker receives no callback instruction from CAO, so the SOP or its arguments must tell it to report back with `send_message` (omitting `receiver_id` still routes to the assigning terminal). Workers on every other provider read a leading `@path` as a file mention and keep the suffix.
3. When done, the worker sends results back via `send_message` -- omitting `receiver_id` routes the reply to the assigning terminal automatically.
4. Messages queue in the supervisor's inbox if it is busy.

### Callback guarantee

By default (`expect_callback=true`) the supervisor always hears back. The server records the
assignment when the worker is created, and once the task has been delivered it watches the worker:

- If the worker goes idle without calling `send_message`, the server forwards its last response to
  the supervisor's inbox. The message begins with `[CAO auto-forward]`, names the worker and its
  profile, says the text was read from the terminal and may be partial, and gives the commands to
  continue (`send_message` to the worker) or close it (`delete_terminal`).
- The forward waits until the worker has started on the last message delivered to it, so the
  supervisor never gets the answer to an earlier message. With `CAO_EAGER_INBOX_DELIVERY=true`, a
  message can reach a worker in the middle of a turn, and the forward can then send the answer of
  that turn. If the worker shows no sign of work for 300 seconds after a delivery
  (`CAO_DELEGATION_PICKUP_TIMEOUT_SECONDS`), the message may not have been picked up, or the turn
  was too fast to show; the server then forwards the last response with a header that says so and
  asks the supervisor to check the worker's terminal before sending the message again.
- If the worker is deleted or reaches an error state before replying, the supervisor receives a
  `[CAO worker ended]` notice, with the worker's last response when it can still be read. For an
  error state, the notice says the worker may still be running.
- If a message to the worker cannot be delivered (the terminal refuses input outside a dialog, or
  the paste fails), or cao-server restarts before the assigned task was delivered, the supervisor
  receives a `[CAO delivery failed]` notice that names the worker and asks it to check the worker's
  terminal before sending again.
- A worker that is waiting on a sub-worker it assigned is not forwarded until that sub-worker has
  replied.
- Any message from the worker to the supervisor counts as the reply and closes the wait, including a
  progress note or a question. If the worker then goes idle without a final reply, nothing more is
  forwarded.
- A worker that has no `send_message` tool (no `cao-mcp-server` in any MCP config its CLI loads)
  still gets the reply instruction in its task text; its last response is forwarded when it goes
  idle.

Pass `expect_callback=false` for a fire-and-forget worker. For a terminal you did not assign, pass
`expect_reply=true` to `send_message` when you are waiting for its reply.

Scope: the guarantee covers local assigns only. Cross-node assigns (`target_host`) and elastic
workers are not covered. A cycle of idle terminals that are each waiting on another is logged but
not resolved.

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
  model: str = None,       # override the agent's model
  expect_callback: bool = True  # forward the last response if the worker goes idle without replying
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
  receiver_id: str = None,   # target terminal ID (8-char hex); omit to route to caller's parent
  expect_reply: bool = False # forward the terminal's last response if it goes idle without replying
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
