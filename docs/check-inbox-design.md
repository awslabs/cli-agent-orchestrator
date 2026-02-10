# Design: Supervisor Inbox — Scatter-Gather for `assign`

## Executive Summary

**Problem:** Supervisors using `assign()` to dispatch parallel workers have no way to collect results. There is no `check_inbox` MCP tool. Worker results are typed into tmux as raw text, losing sender identity and conversation context.

**Solution:** Add a `check_inbox` MCP tool backed by a backend-agnostic message queue inside the CAO server. Replace the push model (watchdog types into tmux) with a pull model (supervisor reads structured JSON on demand).

**Queue backend:** asyncio.Queue for v1 (zero dependencies, instant delivery, same event loop). Swap to RabbitMQ when durability or horizontal scaling is needed — same `MessageQueueService` interface, no changes to MCP tools or API consumers.

**Related discovery:** ACP (Agent Client Protocol) could replace CAO's tmux-based transport layer entirely — all CAO providers are in the ACP registry. ACP solves the *transport problem* (how to talk to agents) but not the *inbox problem* (how agents collect results from each other). `check_inbox` is needed regardless of ACP adoption. See [ACP_Discovery.md](ACP_Discovery.md) for the full analysis.

**Key changes:**
- New file: `services/message_queue.py` — `MessageQueueService` ABC + `AsyncioQueueBackend`
- New MCP tool: `check_inbox` — long-polls `GET /inbox/messages/wait`
- New API endpoint: `GET /terminals/{id}/inbox/messages/wait` — blocks until messages arrive
- Modified: `POST /inbox/messages` — enqueues to asyncio.Queue instead of watchdog delivery
- Removed: `PollingObserver` + `LogFileHandler` (watchdog for inbox delivery)

---

## 1. Problem

### What is broken

When a supervisor uses `assign()` to dispatch parallel workers, it has **no way to collect their results**. The supervisor's conversation turn ends before workers finish. Worker results arrive later as disconnected, context-less inputs.

The supervisor itself described the problem:

> "There's no 'read_inbox' tool available in the MCP toolset to poll for incoming messages. Since I couldn't programmatically check my inbox, I moved ahead and gave the report generator the values myself — which undermined the whole point of delegating to the analysts."

### `handoff` vs `assign`

| | `handoff` | `assign` |
|---|---|---|
| Blocking? | Yes — blocks until worker completes | No — returns immediately |
| Result delivery | Tool return value (synchronous) | Worker calls `send_message()` → inbox (async) |
| Supervisor context | Preserved — same turn | Lost — results arrive in new turns |
| Parallel workers? | No — sequential | Yes — N workers concurrently |

### Three root causes

**1. No read-side tool.** The supervisor has `send_message` (write) but no `check_inbox` (read).

**2. Delivery conflates with turn initiation.** When the watchdog delivers a message, it calls `terminal_service.send_input(terminal_id, message.message)` — literally typing into tmux. Each result starts a new conversation turn with no context about the original task.

**3. Sender identity is stripped.** Only `message.message` (raw text) is delivered. The `message.sender_id` is stored in SQLite but never reaches the supervisor. Observed behavior: the supervisor received two identical payloads and could not tell if they came from the same worker (bug) or two different workers (valid).

---

## 2. Current Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                     CAO Server (FastAPI :9889)                      │
│                                                                    │
│  ┌────────────┐   ┌──────────────────┐   ┌───────────────────┐    │
│  │  REST API   │   │  Inbox Service    │   │ Terminal Service  │    │
│  │            │   │                  │   │                   │    │
│  │ POST /inbox│──>│ Watchdog (5s poll)│──>│ send_input()      │    │
│  │ GET  /inbox│   │ LogFileHandler    │   │ tmux send-keys    │    │
│  └────────────┘   └──────────────────┘   └───────────────────┘    │
│        │                   │                       │               │
│        v                   v                       v               │
│  ┌────────────┐   ┌──────────────┐        ┌──────────────┐       │
│  │ SQLite DB   │   │ Log files     │        │ tmux sessions │       │
│  │ inbox table │   │ (per worker)  │        │ (per worker)  │       │
│  └────────────┘   └──────────────┘        └──────────────┘       │
└────────────────────────────────────────────────────────────────────┘
```

### Current message flow (`assign` + `send_message`)

```
Supervisor                    CAO Server                     Worker
     │                            │                              │
     │── assign(analyst, task) ──>│── create terminal + input ──>│
     │<── {terminal_id: "W1"} ───│                              │
     │                            │                              │
     │ (turn ends, goes IDLE)     │          (working...)        │
     │                            │                              │
     │                            │<── send_message(S, result) ──│
     │                            │   INSERT SQLite (PENDING)    │
     │                            │   Is supervisor IDLE? YES    │
     │<── send_input(result) ─────│   tmux paste-buffer          │
     │                            │   UPDATE status=DELIVERED    │
     │                            │                              │
     │ (NEW turn — no context)    │                              │
```

### What breaks at scale (20-50 workers)

| Problem | Detail |
|---------|--------|
| Context loss | Each result = separate conversation turn, never synthesized together |
| Watchdog bottleneck | ~100 subprocess calls per 5s cycle (50× `tail` + 50× `tmux capture-pane`) |
| SQLite write contention | Single write lock serializes 50 concurrent `send_message()` calls |
| One-at-a-time delivery | `get_pending_messages(limit=1)` → worst case 250s to deliver 50 messages |
| No read-side tool | Supervisor is blind to its inbox |

---

## 3. Solution Research

### Overview

To solve the supervisor inbox problem, we researched three dimensions: (1) how existing multi-agent frameworks handle the scatter-gather pattern, (2) whether ACP (Agent Client Protocol) could replace CAO's tmux-based transport layer, and (3) what queue backends could carry messages inside the CAO server.

#### Multi-agent frameworks researched

We studied 3 multi-agent coordination systems and 1 foundational design pattern. We excluded single-agent subagent systems (Claude Code Subagents, Kiro CLI delegate) since those are internal to one agent — not multi-agent coordination across separate processes.

**Google A2A** (Agent-to-Agent) is the emerging standard protocol for inter-agent communication over HTTP. It uses `tasks/send` to dispatch work and `tasks/get` to collect results, with support for polling, SSE streaming, and webhook push. Both **LangGraph** and **CrewAI** support A2A as their inter-agent protocol. Internally, they also have their own in-process orchestration mechanisms — LangGraph uses state reducers to auto-merge parallel branch results; CrewAI uses dependency graphs where downstream tasks block until upstream tasks complete. These internal mechanisms are Python SDK patterns that don't apply directly to CLI agent orchestration, but A2A as the shared protocol layer is relevant. For CAO, A2A's full protocol (agent discovery, task state machine, multiple delivery modes) is over-engineered for a single-machine setup. However, its long-poll `tasks/get` pattern maps directly to what CAO needs for `check_inbox`.

**Claude Code Agent Teams** is the most directly comparable system. It coordinates independent Claude Code processes that communicate via a Mailbox (`message` for 1:1, `broadcast` for 1:all) and a shared task list with file locking. It also provides quality gate hooks (`TeammateIdle`, `TaskCompleted`) that can block actions and send feedback. However, Agent Teams is experimental (behind a feature flag), its internal transport mechanism is not documented, and Anthropic's own docs report reliability issues — task status lags and teammates fail to mark tasks as completed.

**Classic Scatter-Gather** (Enterprise Integration Patterns) is the established design pattern for this problem class. A dispatcher sends requests to a recipient list, and an aggregator collects responses using correlation IDs with a completeness condition (all-received or timeout). It is a pattern, not a product — but it is the blueprint that CAO's `assign + check_inbox` implements.

**What we learned:** Across all systems, results flow back as structured data through the framework — none of them type results into a terminal. Push-based delivery is fragile: both Claude Code Agent Teams (auto-delivery when idle) and CAO's watchdog (type into tmux when idle) struggle because the framework decides when to deliver, not the agent. Pull-based collection, where the supervisor explicitly requests messages and receives structured JSON, avoids these timing problems.

#### ACP (Agent Client Protocol) researched

We also researched ACP — an editor↔agent protocol (like LSP for AI agents) using JSON-RPC 2.0 over stdio. All CAO providers are in the ACP registry. ACP could replace CAO's entire tmux-based transport layer with structured JSON-RPC, but it has **no agent-to-agent messaging** — it cannot replace the MCP orchestration tools (`handoff`, `assign`, `send_message`, `check_inbox`). The `check_inbox` design is needed regardless of ACP adoption. See [ACP_Discovery.md](ACP_Discovery.md) for the full deep dive (protocol spec, agent ecosystem, hybrid architecture, migration strategy).

#### Queue backends researched

We evaluated 4 queue backends for the message transport inside the CAO server. We also evaluated NATS but excluded it because its core pub/sub is fire-and-forget — messages are lost if no subscriber is listening when published.

- **RabbitMQ** (`aio-pika`) is an external Erlang-based broker with per-terminal durable queues. It provides built-in acknowledgment, dead-letter exchange, TTL, priority routing, and a management UI on `:15672`. It excels at durability and horizontal scaling. The trade-off is operational overhead: an external Erlang VM (~100-150 MB idle RAM) that must be running before `cao-server` starts, plus an `aio-pika` pip dependency.

- **ZeroMQ** (`pyzmq`) is a brokerless C library that provides in-process PUSH/PULL sockets via `inproc://` transport. It adds ~2-5 MB overhead with sub-millisecond latency and requires no external process. However, it has no persistence, is limited to a single process, and provides no management UI. It adds a native C dependency without meaningful advantage over Python's standard library for this use case.

- **asyncio.Queue** (Python stdlib) uses an in-process `Dict[str, asyncio.Queue]` — one queue per terminal, running in the same event loop as FastAPI. It has zero dependencies, instant delivery, and no race conditions (single-threaded asyncio). It naturally fits CAO's existing architecture where all MCP servers already communicate via HTTP to one central FastAPI process. The trade-off is no persistence (mitigated by keeping SQLite as a crash-recovery layer) and single-process only (acceptable since CAO already runs as one server process).

- **SQLite + polling** (patching the current system) keeps the existing SQLite inbox and watchdog, adding a `check_inbox` tool that polls `GET /inbox/messages?status=pending`. It requires no new infrastructure, but it doesn't solve the fundamental problems: two delivery paths (watchdog push + check_inbox pull) needing deduplication, 5-second polling latency, the watchdog still running ~100 subprocesses per cycle at 50 workers, and SQLite write contention.

#### Recommendation

Given the research above, we recommend **asyncio.Queue for v1** and **RabbitMQ for v2** (when needed), behind a backend-agnostic `MessageQueueService` interface:

1. **asyncio.Queue for v1** — it unblocks the critical missing feature (`check_inbox`) with zero new dependencies, fits the existing single-process FastAPI architecture, and has no race conditions. Its lack of persistence is mitigated by SQLite as a crash-recovery layer.
2. **RabbitMQ for v2** — it becomes worthwhile when specific triggers arise: multiple server instances (horizontal scaling), message durability is critical (cannot lose worker results on crash), or the management UI is needed for debugging complex 50-agent workflows.
3. **Backend-agnostic interface** — the `MessageQueueService` ABC means swapping from asyncio.Queue to RabbitMQ requires changing only the backend implementation. No changes to MCP tools, API endpoints, or agents.
4. **ZeroMQ: not recommended** — same single-process constraint as asyncio.Queue but adds a C dependency with no meaningful benefit over stdlib.
5. **SQLite + polling: not recommended** — preserves the watchdog bottleneck and introduces a second delivery path requiring deduplication logic.

The detailed evaluation tables follow below.

### 3.1 Multi-agent frameworks

| Framework | How it dispatches | How it collects results | Pros | Cons | Recommendation for CAO |
|---|---|---|---|---|---|
| **Google A2A** (adopted by LangGraph, CrewAI) | `tasks/send` to agent URLs | `tasks/get` poll, SSE stream, or webhook push. LangGraph also auto-merges via state reducers; CrewAI also blocks via dependency graph. | Emerging standard protocol. Flexible delivery (poll, stream, push). Adopted by major frameworks. LangGraph guarantees completeness at join; CrewAI blocks until deps resolve. | Full protocol is over-engineered for single-machine CAO (agent discovery, task state machine, multiple delivery modes). LangGraph/CrewAI internal patterns are Python SDK, not CLI agent orchestration. | **Adopt the long-poll pattern.** A2A's `tasks/get` with blocking read is exactly what `GET /inbox/messages/wait` does. Skip SSE/webhook/agent discovery. Learn from LangGraph (all results collected before proceeding) and CrewAI (block until deps resolve). |
| **Claude Code Agent Teams** | Spawn teammates (separate processes) | Mailbox: `message()` 1:1, `broadcast()` 1:all. Shared task list with file locking. | Peer-to-peer messaging. Task coordination with dependencies. Quality gate hooks (`TeammateIdle`, `TaskCompleted`). Most directly comparable to CAO. | Experimental (feature flag). Transport undocumented — can't evaluate reliability. Known bugs: task status lag, failed task marking. Not battle-tested. | **Borrow concepts** (pull-based inbox, task labels, quality gates). **Don't copy implementation** — it's a prototype with documented reliability issues. |
| **Classic Scatter-Gather** (EIP) | Recipient list + parallel dispatch | Aggregator with correlation ID + completeness condition (all-received or timeout) | Well-understood pattern. Correlation IDs solve the "who sent what" problem. Timeout handles stragglers. | A pattern, not a product — must be implemented from scratch. | **This is the pattern CAO should implement.** `assign()` = scatter, `check_inbox()` = gather with timeout. `sender_id` = correlation ID. |

**Key insight:** All systems return results as **structured data** through the framework — none of them type results into a terminal. CAO's current approach (typing into tmux) is the outlier. The fix: results flow through `check_inbox` as structured JSON.

**Out of scope:** Single-agent subagent systems (Claude Code Subagents, Kiro CLI delegate) are internal to one agent session — analogous to CAO's `handoff`. They don't inform the scatter-gather design.

### 3.2 ACP (Agent Client Protocol)

| Aspect | Detail |
|--------|--------|
| **What it is** | Editor↔agent protocol (like LSP for AI agents). JSON-RPC 2.0 over stdio. |
| **What it is NOT** | Not agent↔agent. No messaging, task assignment, or work queues between agents. |
| **Relationship to MCP** | Complementary. ACP = editor↔agent. MCP = agent↔tools. |
| **Agent support** | All CAO providers + 7 others in ACP registry (12 total). |
| **Relevance to check_inbox** | None directly. ACP could replace tmux transport layer (see Section 4.7). |

For full details (protocol spec, agent ecosystem, SDKs, hybrid architecture, migration strategy), see [ACP_Discovery.md](ACP_Discovery.md).

### 3.3 Claude Code Agent Teams (deep dive)

> Source: https://code.claude.com/docs/en/agent-teams and https://code.claude.com/docs/en/hooks

Agent Teams is **experimental** (behind `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`). Each teammate is a fully separate Claude Code instance (own context window, own process).

**Documented architecture:**

| Component | Storage | Purpose |
|-----------|---------|---------|
| Team Lead | Main Claude Code session | Creates team, coordinates |
| Teammates | Separate Claude Code instances | Independent workers |
| Task List | `~/.claude/tasks/{team-name}/` | File-locked task claiming |
| Mailbox | Internal (transport NOT documented) | Inter-agent messaging |

**What the docs say about messaging:**
- *"Messages are delivered automatically to recipients. The lead doesn't need to poll."*
- `message` (1:1) and `broadcast` (1:all, token-expensive)
- Internal transport mechanism is **not documented** (no details on filesystem watch, IPC, or other)

**Quality gate hooks:** `TeammateIdle` and `TaskCompleted` hooks fire at lifecycle points. Exit code 2 blocks the action and sends stderr as feedback to the agent.

**Documented limitations:**
- Task status can lag — teammates fail to mark tasks as completed
- No session resumption for in-process teammates
- One team per session, no nested teams, lead is fixed
- Shutdown can be slow

**Takeaways for CAO:**

| Lesson | Application |
|--------|-------------|
| Push delivery has problems (both Claude Code and CAO's watchdog) | Use pull (`check_inbox`) — supervisor controls when to read |
| Shared task list is powerful | Future: `list_tasks`, `claim_task` MCP tools |
| Quality gate hooks | Future: pre-send-message validation |
| Transport is undocumented — can't evaluate reliability | Design from first principles, not copying |

### 3.4 Queue backend options

All MCP servers already communicate with the CAO server via HTTP. The queue sits **inside** the CAO server — an implementation detail invisible to agents:

```
Agent 1 → MCP Server 1 ─── HTTP ───┐
Agent 2 → MCP Server 2 ─── HTTP ───┤
...                                  ├──> CAO Server (FastAPI)
Agent 50 → MCP Server 50 ── HTTP ──┘       │
                                     ┌──────┴──────┐
                                     │ Queue       │
                                     │ Backend     │
                                     └─────────────┘
```

| Option | What it is | Pros | Cons | Recommendation |
|--------|-----------|------|------|----------------|
| **A: RabbitMQ** (`aio-pika`) | External Erlang-based broker. Per-terminal durable queues. Management UI on `:15672`. | Durable queues survive crashes. Built-in ack, DLX, TTL, priority, fanout. Management UI for debugging. Horizontal scaling ready. | External process (~100-150 MB idle Erlang VM). Must run before `cao-server`. `aio-pika` pip dep. Cannot embed in Python. | **v2 — use when** CAO needs multiple server instances, message durability is critical, or debugging requires the management UI. |
| **B: ZeroMQ** (`pyzmq`) | Brokerless C library. In-process PUSH/PULL sockets via `inproc://`. | No external process. ~2-5 MB overhead, sub-ms latency. pip install only. | No persistence — messages lost on crash. Single-process only (`inproc://`). No management UI. | **Not recommended.** Same single-process constraint as asyncio.Queue but adds a C dependency (`pyzmq`) with no meaningful benefit over stdlib. |
| **C: asyncio.Queue** (stdlib) | In-process `Dict[str, asyncio.Queue]` — one queue per terminal. Same event loop as FastAPI. | Zero dependencies. Instant delivery (same event loop). No race conditions (single-threaded asyncio). Simplest implementation. | No persistence (add SQLite for crash recovery). Single-process only. No management UI. Not distributed. | **v1 — use now.** Simplest path to unblock `assign + check_inbox`. The API layer abstracts the backend — all options slot in behind the same interface. |
| **D: SQLite + polling** (status quo + patch) | Keep current SQLite inbox and watchdog. Add `check_inbox` that polls `GET /inbox/messages?status=pending`. | No new infrastructure. Already implemented. | Two delivery paths (watchdog push + check_inbox pull) — needs dedup. 5s polling latency. Watchdog still runs ~100 subprocesses/cycle at 50 workers. SQLite write contention. | **Not recommended.** Doesn't solve the fundamental problems (watchdog bottleneck, dual delivery paths, polling latency). |

**NATS** was also evaluated but excluded: core pub/sub is fire-and-forget — messages lost if no subscriber is listening when published. Worker may finish before supervisor calls `check_inbox`. JetStream adds persistence but comparable complexity to RabbitMQ.

### 3.5 Comparison

| Criteria | RabbitMQ | ZeroMQ | asyncio.Queue | SQLite+poll |
|----------|----------|--------|---------------|-------------|
| External process | Yes (Erlang) | No | No | No |
| Idle RAM | ~100-150 MB | ~2-5 MB | ~0 | ~0 |
| pip dependency | `aio-pika` | `pyzmq` | None | None |
| Latency | Instant | Sub-ms | Instant | 0-5s poll |
| Persistence | Built-in | No | No (add manually) | Built-in |
| Ack / DLX / TTL | Yes | No | No | No |
| Distributed | Yes | Partial | No | No |
| Scale (50 workers) | Easily | Easily | Easily | Bottleneck |
| Delivery paths | 1 (pull) | 1 (pull) | 1 (pull) | 2 (needs dedup) |

### 3.6 Recommendation

**v1: asyncio.Queue.** Simplest path to unblock `assign + check_inbox`. Zero dependencies. The API layer abstracts the backend — all options slot in behind the same interface.

**v2: RabbitMQ** when any of these become true: multiple server instances, message durability critical, need management UI, need DLX/TTL/priority.

**Migration path:** Swap `AsyncioQueueBackend` → `RabbitMQBackend` (same `MessageQueueService` interface). No changes to MCP tools, API endpoints, or agents.

---

## 4. Recommended Design

### 4.1 Message Queue Service

A `MessageQueueService` abstract base class with four methods:

| Method | Purpose |
|--------|---------|
| `connect()` / `close()` | Lifecycle — called during FastAPI lifespan startup/shutdown |
| `put(receiver_id, sender_id, message)` | Enqueue message for receiver. Called by `POST /inbox/messages` |
| `get(terminal_id, timeout, batch_window)` | Block until messages arrive, then batch-drain. Called by `GET /inbox/messages/wait` |
| `cleanup(terminal_id)` | Remove queue for terminated terminal |

**v1 backend:** `AsyncioQueueBackend` — `Dict[str, asyncio.Queue]`, one queue per terminal, same event loop as FastAPI. Zero dependencies.

**v2 backend:** `RabbitMQBackend` — per-terminal durable queues via `aio-pika`. Same interface, swap via `CAO_QUEUE_BACKEND=rabbitmq` env var.

**Backend selection:** Factory function reads `CAO_QUEUE_BACKEND` env var (default: `asyncio`). Module-level singleton.

### 4.2 API Endpoints

**Modified — `POST /terminals/{receiver_id}/inbox/messages`:**
- Persist to SQLite (crash recovery)
- `await message_queue.put(receiver_id, sender_id, message)`
- Remove: `inbox_service.check_and_send_pending_messages()` call

**New — `GET /terminals/{terminal_id}/inbox/messages/wait`:**
- Long-poll endpoint for `check_inbox` MCP tool
- Params: `timeout` (int), `batch_window` (float, default=2.0)
- Behavior: blocks until first message arrives, waits `batch_window` for more, drains all available, returns as JSON array
- Each message includes: `id`, `sender_id`, `message`, `created_at`
- No tmux interaction — messages returned as structured data

**Unchanged — `GET /terminals/{terminal_id}/inbox/messages`:**
- Still works for querying message history (debugging, E2E tests)

### 4.3 `check_inbox` MCP Tool

New MCP tool registered in `mcp_server/server.py`.

**Parameters:**
- `timeout` (int, default=60, 0-600): Max seconds to wait for messages. 0 = immediate check.

**Behavior:**
- Reads `CAO_TERMINAL_ID` from environment (auto-set, not agent-supplied)
- Long-polls `GET /terminals/{id}/inbox/messages/wait`
- Returns all messages in one response with sender metadata
- Messages are consumed (will not be returned again)

**Return format:**

```json
{
  "success": true,
  "terminal_id": "abc123ef",
  "messages": [
    {"id": 42, "sender_id": "W1", "message": "[Task: dataset_A] mean=3.0", "created_at": "..."},
    {"id": 43, "sender_id": "W2", "message": "[Task: dataset_B] mean=7.5", "created_at": "..."}
  ],
  "total": 2
}
```

### 4.4 Message Correlation

Three layers:

| Layer | Purpose | Mechanism |
|-------|---------|-----------|
| `sender_id` | Who sent it | Auto-set from `CAO_TERMINAL_ID` — matches `assign()`'s returned `terminal_id` |
| `id` | Deduplication | Unique integer per message |
| Task label | Which task | Convention: supervisor embeds label in assign message, worker echoes it back |

### 4.5 Watchdog Removal

Remove `PollingObserver` + `LogFileHandler` from `api/main.py` lifespan. The watchdog was needed when the only delivery path was "type into tmux when IDLE." With `check_inbox`, messages are pulled as structured data — no tmux interaction needed for inbox delivery.

### 4.6 Persistence (Crash Recovery)

For asyncio.Queue backend (volatile):
- On `POST /inbox/messages`: INSERT into SQLite (`status=PENDING`) + `queue.put()`
- On `GET /inbox/messages/wait` (consumed): UPDATE `status=CONSUMED`
- On server restart: load PENDING messages into asyncio.Queue

For RabbitMQ backend: durable queues + persistent delivery mode — no additional layer needed.

### 4.7 Future: ACP Adoption (Separate Initiative)

ACP could replace CAO's entire tmux scraping layer (status regex, `send-keys`, ANSI parsing, idle prompt patterns) with structured JSON-RPC over stdio. MCP orchestration tools (`handoff`, `assign`, `send_message`, `check_inbox`) stay unchanged — they are passed to agents via ACP's `session/new` MCP server configuration.

`check_inbox` and ACP are independent: `check_inbox` fixes the scatter-gather problem (MCP tool level), ACP fixes the agent transport problem (protocol level). Evaluate ACP adoption as a separate design doc after `check_inbox` is implemented.

For the full ACP analysis — protocol spec, what it replaces, what it doesn't, hybrid architecture, migration strategy, prerequisites, and risks — see [ACP_Discovery.md](ACP_Discovery.md).

---

## 5. Target Flow

```
Supervisor                  CAO Server                           Workers
     │                          │                                    │
     │── assign(analyst_1) ────>│── create terminal + input ────────>│ W1
     │<── {terminal_id: "W1"} ─│                                    │
     │── assign(analyst_2) ────>│── create terminal + input ────────>│ W2
     │<── {terminal_id: "W2"} ─│                                    │
     │── assign(analyst_3) ────>│── create terminal + input ────────>│ W3
     │<── {terminal_id: "W3"} ─│                                    │
     │                          │                                    │
     │── check_inbox(120) ─────>│   await queue.get() (blocks)      │
     │                          │                       W1 finishes  │
     │                          │<── send_message(S, result_1) ─────│
     │                          │   queue.put(msg_1) → unblocks     │
     │                          │   batch window 2s...               │
     │                          │                       W2, W3 finish│
     │                          │<── send_message(S, result_2) ─────│
     │                          │<── send_message(S, result_3) ─────│
     │                          │   drain → 3 messages               │
     │                          │                                    │
     │<── {messages: [1,2,3]} ─│                                    │
     │                          │                                    │
     │ (same turn — full        │                                    │
     │  context preserved)      │                                    │
```

---

## 6. Supervisor Agent Profile Pattern

The supervisor agent profile should document the `assign → check_inbox` workflow:

1. Read own `CAO_TERMINAL_ID` environment variable
2. Call `assign()` for each worker — note returned `terminal_id`s
3. Call `check_inbox(timeout=120)` — blocks until results arrive
4. Match each message's `sender_id` to `assign()`'s `terminal_id` to correlate
5. Synthesize all results and present final output

**Task label convention:** Include a label in the assign message (e.g., `[Task: dataset_A]`). Instruct the worker to echo it back in `send_message`. This enables robust correlation at scale — `sender_id` tells you *who*, the label tells you *what task*.

**Partial results:** If `check_inbox` times out before all workers respond, note missing `sender_id`s, call `check_inbox` again with shorter timeout, then proceed with available results.

**When to use which:**
- `handoff` — one worker, blocking, result in tool response, no correlation needed
- `assign + check_inbox` — N workers in parallel, collect all, use task labels for correlation

---

## 7. Files to Modify

| File | Change | Why |
|------|--------|-----|
| `services/message_queue.py` | **New** — `MessageQueueService` ABC + `AsyncioQueueBackend` + factory | Core message transport |
| `mcp_server/server.py` | Add `check_inbox` tool + `_check_inbox_impl()` | New MCP tool |
| `api/main.py` | Add `GET /inbox/messages/wait`; modify `POST /inbox/messages` to use queue; remove watchdog | API + queue integration |
| `services/inbox_service.py` | Remove or deprecate | Watchdog no longer needed |
| `models/inbox.py` | Add `CONSUMED` to `MessageStatus` | Track consumed messages |
| `clients/database.py` | Keep for persistence; add reload-on-startup | Crash recovery |

---

## 8. Implementation Plan

| Phase | What | Depends on |
|-------|------|-----------|
| 1 | `MessageQueueService` ABC + `AsyncioQueueBackend` + factory + unit tests | — |
| 2 | `GET /inbox/messages/wait` endpoint + modify `POST /inbox/messages` + lifespan integration + PENDING reload on startup | Phase 1 |
| 3 | `check_inbox` MCP tool + unit tests | Phase 2 |
| 4 | Remove watchdog (`PollingObserver` + `LogFileHandler`) + deprecate `inbox_service.py` | Phase 3 |
| 5 | Update supervisor agent profiles + E2E tests (N=3, then N=10-20) | Phase 4 |
| 6 | `RabbitMQBackend` (optional dep, same interface, env-var selection) | When needed |
