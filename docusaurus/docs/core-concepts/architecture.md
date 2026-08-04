---
sidebar_position: 1
---

# Architecture

CAO implements a **supervisor-worker** multi-agent system where one supervisor agent delegates tasks to specialized worker agents, each running in an isolated tmux window within the session. Communication flows through the Model Context Protocol (MCP), and the runtime is powered by an event-driven pipeline connected by a central pub/sub event bus.

## Supervisor-Worker Pattern

```
┌─────────────────────────────────────────────────────────┐
│                    Supervisor Agent                       │
│           (orchestration logic, task routing)             │
└────────┬───────────────────┬───────────────────┬─────────┘
         │ handoff/assign    │ send_message      │
         ▼                   ▼                   ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│   Worker 1   │    │   Worker 2   │    │   Worker N   │
│ (tmux window)│    │ (tmux window)│    │ (tmux window)│
│  claude_code │    │   kiro_cli   │    │    codex     │
└──────────────┘    └──────────────┘    └──────────────┘
```

- The **supervisor** holds the orchestration context and delegates domain work.
- Each **worker** runs a full CLI agent process with its own terminal, auth, and tools.
- Workers can run different providers in the same session (cross-provider orchestration).
- Humans can `tmux attach` to any session to observe or intervene at any time.

## Event-Driven Pipeline

Terminal output flows through a four-stage pipeline connected by the central event bus. No service calls another directly; the bus is the sole brokering mechanism.

```
┌─────────────┐         ┌───────────────────────────┐         ┌──────────────┐
│ FifoReader  │─publish─▶│        EVENT BUS          │─subscribe▶│  LogWriter   │
│  (thread)   │ terminal.│  pub/sub with wildcard    │ terminal. │   (async)    │
│             │ {id}.    │  topic matching           │ {id}.     │              │
│ tmux pipe─  │ output   │                           │ output    │ per-terminal │
│ pane → FIFO │         │                           │           │ debug logs   │
└─────────────┘         │                           │           └──────────────┘
                         │                           │
                         │                           │─subscribe─▶┌───────────────┐
                         │                           │  terminal.  │ StatusMonitor │
                         │                           │  {id}.      │   (async)     │
                         │◀──────────────────────────│  output     │               │
                         │  publish: terminal.{id}.  │             │ rolling 8KB   │
                         │  status                   │             │ buffer +      │
                         │                           │             │ pattern detect│
                         │                           │             └───────────────┘
                         │                           │
                         │                           │─subscribe─▶┌──────────────┐
                         │                           │  terminal.  │ InboxService │
                         │                           │  {id}.      │   (async)    │
                         │                           │  status     │              │
                         └───────────────────────────┘             │ delivers     │
                                                                   │ queued msgs  │
                                                                   └──────────────┘
```

### Pipeline stages

| Stage | Role | Publishes | Subscribes |
|-------|------|-----------|------------|
| **FifoReader** | Reads raw bytes from tmux `pipe-pane` via a named FIFO (one thread per terminal). Coalesces rapid-fire chunks into batched events. | `terminal.{id}.output` | -- |
| **StatusMonitor** | Accumulates output into a rolling 8 KB buffer, runs provider-specific pattern matching to detect status transitions (IDLE, PROCESSING, COMPLETED, etc.). | `terminal.{id}.status` | `terminal.{id}.output` |
| **LogWriter** | Appends terminal output to per-terminal log files for debugging. Batches writes to avoid I/O pressure under burst. | -- | `terminal.{id}.output` |
| **InboxService** | Delivers queued inbox messages when a terminal transitions to IDLE or COMPLETED. Supports eager delivery for providers that buffer input mid-turn. | -- | `terminal.{id}.status` |

### Central Event Bus

The event bus (`services/event_bus.py`) provides:

- **Wildcard topic matching** -- subscribe to `terminal.*.output` to receive all terminals' output.
- **Thread-safe publishing** -- FifoReader threads publish via `loop.call_soon_threadsafe`.
- **Async consumption** -- consumers await on bounded `asyncio.Queue` instances.
- **Back-pressure** -- per-subscriber queue cap (configurable via `CAO_EVENT_BUS_MAX_QUEUE_SIZE`, default 1024) with drop-and-log on overflow.

## Four Control Planes

CAO exposes four independent surfaces for controlling the orchestration runtime:

| Control Plane | Interface | Best For |
|---------------|-----------|----------|
| **CLI** | `cao session`, `cao launch`, `cao shutdown` | Scripting, CI pipelines, quick shell access |
| **Web UI** | Browser at `localhost:9889` | Visual monitoring, interactive management |
| **cao-ops-mcp** | MCP tools from an external agent | Agent-driven agent management (a primary agent spawns and monitors CAO sessions) |
| **Plugins** | Python observer extensions inside `cao-server` | Outbound event streaming (Discord, Slack, webhooks, audit logs) |

## Component Stack

```
┌──────────────────────────────────────────────────────────────────┐
│                        Control Planes                              │
│   CLI Commands  │  Web UI (:9889)  │  cao-ops-mcp  │  Plugins    │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                      ┌──────▼──────┐
                      │   FastAPI   │
                      │  HTTP API   │
                      │  (:9889)    │
                      └──────┬──────┘
                             │
                      ┌──────▼──────────────────┐
                      │     Services Layer       │
                      │  session_service         │
                      │  terminal_service        │
                      │  inbox_service           │
                      │  flow_service            │
                      │  memory_service          │
                      │  event_bus               │
                      └──────┬──────────────────┘
                             │
               ┌─────────────┴─────────────┐
               │                           │
        ┌──────▼──────┐             ┌──────▼──────┐
        │   Clients   │             │  Providers  │
        │  tmux.py    │             │  kiro_cli   │
        │  database   │             │  claude_code│
        └──────┬──────┘             │  codex      │
               │                    │  + 6 more   │
        ┌──────┴──────┐             └─────────────┘
        │ tmux / herdr│
        │   SQLite    │
        └─────────────┘
```

## Key Design Principles

- **Session isolation** -- every agent runs in its own tmux window with a unique `CAO_TERMINAL_ID`. Clean context separation prevents cross-contamination.
- **Provider agnostic** -- the abstract `BaseProvider` interface means any CLI tool that runs in a terminal can be orchestrated.
- **Event-driven** -- status detection and message delivery are reactive, driven by the pub/sub bus. Two slow polling loops sit underneath as safety nets: an OpenCode inbox poller and a periodic sweep that re-attempts delivery for messages left pending past a grace window.
- **Local-first** -- runs entirely on your machine, no cloud dependencies. The server binds to `127.0.0.1` by default.
- **Full CLI feature preservation** -- because agents are real CLI processes, they retain native features (Claude Code sub-agents, Kiro custom agents, provider auth).
