# Event-Driven Architecture

## Overview

CAO uses an event-driven architecture for terminal output processing, status detection, and inbox message delivery. Terminal output streams through a pipeline of components connected by an in-process pub/sub event bus, replacing the previous watchdog-based file polling approach.

## Architecture

```
┌───────────────────┐  publish   ┌─────────────────────────┐  subscribe   ┌─────────────┐
│ FifoReader        │───────────▶│       EVENT BUS          │─────────────▶│ LogWriter   │
│ (thread)          │  terminal. │                          │  terminal.   │ (async)     │
│                   │  {id}.     │  pub/sub with wildcard   │  {id}.       │             │
│ tmux pipe-pane    │  output    │  topic matching          │  output      │ writes to   │
│  ▼ Named FIFO    │            │                          │              │ log files   │
│  ▼ os.read()     │            │                          │              └─────────────┘
└───────────────────┘            │                          │
                                 │                          │  subscribe   ┌───────────────┐
                                 │                          │─────────────▶│ StatusMonitor │
                                 │                          │  terminal.   │ (async)       │
                                 │                          │  {id}.       │               │
                                 │                          │  output      │ rolling buffer│
                                 │                          │              │ + detection   │
                                 │                          │◀─────────────│               │
                                 │                          │  publish     └───────────────┘
                                 │                          │  terminal.
                                 │                          │  {id}.
                                 │                          │  status
                                 │                          │
                                 │                          │  subscribe   ┌─────────────┐
                                 │                          │─────────────▶│InboxService │
                                 │                          │  terminal.   │ (async)     │
                                 │                          │  {id}.       │             │
                                 │                          │  status      │ delivers    │
                                 └──────────────────────────┘              │ messages    │
                                                                           └─────────────┘
```

All inter-service communication flows through the event bus. No service calls another service directly for event processing — the bus is the sole brokering mechanism.

## Event Bus (`services/event_bus.py`)

The event bus is the **central brokering mechanism** that connects all publishers and consumers. It implements an in-process pub/sub router with wildcard topic matching, thread-safe publishing, and async consumption via `asyncio.Queue`.

Every component in the pipeline communicates exclusively through the event bus — publishers never call consumers directly. This decouples components, allows new consumers to be added without modifying publishers, and ensures a clear data flow through the system.

**Topics:**

| Topic | Publisher | Consumers |
|-------|----------|-----------|
| `terminal.{id}.output` | FifoReader | StatusMonitor, LogWriter |
| `terminal.{id}.status` | StatusMonitor | InboxService |

**Subscription patterns:**

- Exact: `terminal.abc12345.output`
- Wildcard: `terminal.*.output` (matches any terminal ID)

**Thread safety:** Publishers call `bus.publish()` from any thread. The event bus uses `loop.call_soon_threadsafe()` to dispatch events into the asyncio event loop registered at startup via `bus.set_loop()`.

## Component Roles

Each service has a clearly defined role as a **publisher**, **consumer**, or **both**:

| Component | Role | Subscribes To | Publishes To |
|-----------|------|---------------|--------------|
| **FifoReader** | Publisher only | — (reads from OS FIFO) | `terminal.{id}.output` |
| **StatusMonitor** | Publisher + Consumer | `terminal.*.output` | `terminal.{id}.status` |
| **LogWriter** | Consumer only | `terminal.*.output` | — |
| **InboxService** | Consumer only | `terminal.*.status` | — (delivers via `send_input`) |

- **Pure publishers** (FifoReader) are the data sources that inject events into the bus.
- **Pure consumers** (LogWriter, InboxService) react to events and perform side effects (writing logs, delivering messages).
- **Publisher + Consumer** (StatusMonitor) transforms events: it consumes raw output, derives status, and publishes status change events for downstream consumers.

> **Warning: Threading and event loop discipline.** Publisher and consumer implementations must take great care when managing threading. The FifoReader runs in a dedicated OS thread (blocking `os.read` on the FIFO) and publishes into the asyncio loop via `call_soon_threadsafe`. All consumers (`StatusMonitor`, `LogWriter`, `InboxService`) run as asyncio tasks on the main event loop. Consumer `run()` methods must **always yield back to the event loop** (via `await queue.get()`) and avoid long-running synchronous operations that would block other consumers from processing events. If a consumer needs to perform blocking I/O, it should offload to a thread pool via `asyncio.to_thread()`.

## Components

### FIFO Reader (`services/fifo_reader.py`)

**Role:** Publisher

Creates a named pipe (FIFO) per terminal and starts a daemon reader thread. tmux's `pipe-pane` writes terminal output to the FIFO; the reader thread reads 4KB chunks and publishes them to the event bus.

- **Create:** `fifo_manager.create_reader(terminal_id)` — called during terminal creation
- **Stop:** `fifo_manager.stop_reader(terminal_id)` — called during terminal deletion; unblocks the reader by briefly opening the write side, then joins the thread and deletes the FIFO file
- **Reconnect:** On EOF (tmux closes the write side), the reader reopens the FIFO to handle tmux restarts

### Status Monitor (`services/status_monitor.py`)

**Role:** Publisher + Consumer

Accumulates terminal output into a rolling buffer (8KB max) per terminal and detects status changes. Two detection modes:

1. **Pre-init (no provider registered):** Matches a generic shell prompt pattern (`[$#%>]\s`) against the last 500 bytes
2. **Post-init (provider registered):** Delegates to `provider.get_status(buffer)` for provider-specific detection

Only publishes `terminal.{id}.status` events when the status actually changes, avoiding redundant notifications.

Also serves as the source of truth for terminal status via `status_monitor.get_status(terminal_id)`.

### Log Writer (`services/log_writer.py`)

**Role:** Consumer

Appends terminal output chunks to per-terminal log files (`~/.cao/logs/terminal/{id}.log`) for debugging. Runs as a simple async consumer with no state.

### Inbox Service (`services/inbox_service.py`)

**Role:** Consumer

Delivers queued inbox messages when terminals become ready (IDLE or COMPLETED). One message is delivered per terminal per status change to avoid flooding an agent with multiple messages simultaneously.

**Delivery flow:**

1. Subscribes to `terminal.*.status` events
2. On IDLE or COMPLETED status, calls `deliver_pending(terminal_id)`
3. Queries the database for the oldest pending message for that terminal
4. Double-checks the terminal's current status via `status_monitor.get_status()`
5. Sends the message via `terminal_service.send_input()`
6. Updates message status to DELIVERED (or FAILED on error)

**Immediate delivery:** When a new inbox message is created via the API, the endpoint calls `inbox_service.deliver_pending()` for best-effort immediate delivery if the terminal is already idle.

## Startup & Shutdown

During server startup (`api/main.py` lifespan):

1. Register the asyncio event loop with the event bus: `bus.set_loop(loop)`
2. Start consumer tasks: `StatusMonitor.run()`, `LogWriter.run()`, `InboxService.run()`

During shutdown:

1. Cancel all consumer tasks
2. `asyncio.gather()` with `return_exceptions=True` to wait for clean exit

FIFO readers are started/stopped per-terminal by `terminal_service` during create/delete operations.

## Previous Architecture (Watchdog)

The previous implementation used:

- **watchdog `PollingObserver`** to poll terminal log files for changes (5-second interval)
- **`LogFileHandler`** to detect file modifications and trigger inbox message delivery
- **`aiofiles`** for async file I/O

Limitations of the watchdog approach:

- **Latency:** 5-second polling interval meant messages could wait up to 5 seconds before delivery
- **Coupling:** Status detection required reading and parsing the log file on every poll
- **Dependencies:** Required `watchdog` and `aiofiles` packages

The event-driven approach eliminates polling, delivers messages within milliseconds of status changes, and removes the `watchdog` and `aiofiles` dependencies.
