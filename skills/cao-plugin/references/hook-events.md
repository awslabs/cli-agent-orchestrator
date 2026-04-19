# CAO Plugin Hook Events

This reference documents every event type currently emitted by CAO and the dataclass delivered to each hook handler. Import both the `@hook` decorator and event dataclasses from `cli_agent_orchestrator.plugins`.

## General contract

All events inherit from `CaoEvent`:

| Field | Type | Notes |
|---|---|---|
| `event_type` | `str` | The event identifier, e.g. `"post_send_message"`. Matches the string passed to `@hook(...)`. |
| `timestamp` | `datetime` | Timezone-aware UTC, set at event construction. |
| `session_id` | `str \| None` | Populated when the event originates from a known CAO session; `None` otherwise. |

All concrete events use the `post_` prefix to signal that they fire *after* the underlying operation has succeeded. There are no `pre_*` hooks today — a plugin cannot veto or mutate an operation in v1.

Dispatch is fire-and-forget: exceptions raised inside a hook are caught by the registry and logged as warnings, never propagated back into CAO. No ordering is guaranteed across hooks or plugins.

## Event catalog

### `post_send_message`

Fires after a message is successfully delivered to an agent's inbox. Emitted for all three orchestration paths — direct `send_message`, synchronous `handoff`, and asynchronous `assign`. Multi-step orchestrations (`handoff`, `assign`) emit one `PostSendMessageEvent` per delivery.

```python
@dataclass
class PostSendMessageEvent(CaoEvent):
    event_type: str = "post_send_message"
    sender: str = ""              # terminal_id of the sending agent
    receiver: str = ""            # terminal_id of the receiving agent
    message: str = ""             # raw message content delivered to the inbox
    orchestration_type: str = ""  # "send_message" | "handoff" | "assign"
```

Use cases: forwarding inter-agent messages to chat apps, audit logging, conversation replay.

### `post_create_session`

Fires after a CAO session is successfully created.

```python
@dataclass
class PostCreateSessionEvent(CaoEvent):
    event_type: str = "post_create_session"
    session_name: str = ""
```

`session_id` (inherited from `CaoEvent`) is populated with the newly created session's id.

Use cases: dashboard session tracking, alerting on new workflow start.

### `post_kill_session`

Fires after a CAO session is successfully killed.

```python
@dataclass
class PostKillSessionEvent(CaoEvent):
    event_type: str = "post_kill_session"
    session_name: str = ""
```

`session_id` (inherited) identifies the killed session.

Use cases: cleanup of external resources tied to a session, end-of-workflow notifications.

### `post_create_terminal`

Fires after a CAO terminal (an individual agent window inside a session) is successfully created.

```python
@dataclass
class PostCreateTerminalEvent(CaoEvent):
    event_type: str = "post_create_terminal"
    terminal_id: str = ""
    agent_name: str | None = None  # Agent profile name, if one was supplied
    provider: str = ""             # e.g. "claude_code", "kiro_cli", "codex", "q_cli"
```

Use cases: per-agent observability, provider-specific setup, external inventory of running agents.

### `post_kill_terminal`

Fires after a CAO terminal is successfully killed.

```python
@dataclass
class PostKillTerminalEvent(CaoEvent):
    event_type: str = "post_kill_terminal"
    terminal_id: str = ""
    agent_name: str | None = None
```

Use cases: per-agent cleanup, alerting on abnormal terminal exit (when combined with your own state tracking).

## Quick reference table

| Event type string | Dataclass | Extra fields (beyond `CaoEvent`) |
|---|---|---|
| `post_send_message` | `PostSendMessageEvent` | `sender`, `receiver`, `message`, `orchestration_type` |
| `post_create_session` | `PostCreateSessionEvent` | `session_name` |
| `post_kill_session` | `PostKillSessionEvent` | `session_name` |
| `post_create_terminal` | `PostCreateTerminalEvent` | `terminal_id`, `agent_name`, `provider` |
| `post_kill_terminal` | `PostKillTerminalEvent` | `terminal_id`, `agent_name` |

## Not yet supported

The following are explicitly out of scope in v1 (see `docs/feat-plugin-hooks-design.md` §2 for context). Do not write a plugin that depends on them today.

- **`pre_*` hooks** — no pre-event variants; plugins cannot veto or transform operations.
- **Per-hook filtering / priority** — all hooks for an event type receive all events; execution order is unordered.
- **Sync hooks** — all hooks must be `async def`.
- **Provider, flow, error, or MCP tool-invocation events** — not emitted today.
- **Injected plugin configuration** — plugins read their own env vars / config files in `setup()`.
