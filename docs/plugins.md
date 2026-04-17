# Plugins

CAO supports plugins that react to server-side events — session and terminal lifecycle changes, and message delivery between agents. Plugins run inside the `cao-server` process and are notified whenever one of those events occurs.

Typical uses today:

- Forwarding inter-agent messages to external chat (Discord, Slack)
- Audit logging
- Observability and metrics export

**Important:** plugins today are **observers only**. They receive events *after* the corresponding action has already happened, and cannot block, modify, or reject CAO operations. See [Future improvements](#future-improvements) for planned additions.

For ready-to-try reference plugins, see [`examples/plugins/`](../examples/plugins).

## Quick start: your first plugin event

This walkthrough takes you from a fresh clone to seeing plugin events fire end-to-end. It uses the bundled Discord example plugin, but the steps apply to any plugin.

1. **Install CAO and its prerequisites** — follow [README.md § Installation](../README.md#installation) (uv, tmux 3.3+, Python 3.10+, then `uv sync` for a dev checkout).
2. **Install the Discord plugin** into the same environment:
   ```bash
   uv pip install -e examples/plugins/cao-discord
   ```
3. **Configure the plugin** — create a `.env` file in the repo root (where you'll run `cao-server`):
   ```dotenv
   CAO_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/<id>/<token>
   ```
4. **Start the server** — the plugin is discovered automatically on startup:
   ```bash
   cao-server
   ```
   Confirm you see `Loaded CAO plugin: discord` in the server logs.
5. **Install agents and launch a session** — follow [README.md § Quick Start](../README.md#quick-start) steps 1 and 3 to install an agent profile and launch a supervisor terminal.
6. **Trigger a handoff or assign** in the supervisor terminal — watch the Discord channel for forwarded inter-agent messages.

For detailed installation options, configuration, and troubleshooting, continue reading below.

## Installing a plugin

Plugins are standard Python packages distributed with a `cao.plugins` entry point. Installing a plugin means installing that package into the same Python environment `cao-server` runs from, configuring it, and restarting the server. The Discord example plugin at [`examples/plugins/cao-discord`](../examples/plugins/cao-discord) is used throughout this section.

### 1. Install the plugin package

Install the plugin into the same environment that provides `cao-server`. For a published plugin:

```bash
uv pip install <plugin-package>
```

For the local Discord example:

```bash
uv pip install -e examples/plugins/cao-discord
```

Plugins are discovered at server startup via the `cao.plugins` Python entry-point group — there is no separate "register" step.

### 2. Configure the plugin

Each plugin owns its own configuration. Most read environment variables, and many support a `.env` file loaded from the directory you launch `cao-server` from (or a parent directory — `python-dotenv` walks upward from the CWD).

The Discord plugin, for example, requires a webhook URL:

```bash
# .env
CAO_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/<id>/<token>
```

Check the plugin's own README for its configuration keys.

### 3. Restart `cao-server`

Plugins are loaded once, at server startup. After installing or reconfiguring a plugin, restart `cao-server` for the change to take effect. There is no hot-reload mechanism today.

### 4. Verify it loaded

When `cao-server` starts, each plugin logs a setup line. Confirm the plugin you installed appears there, and watch for any `WARNING` entries — a misconfigured plugin is skipped with a warning rather than crashing the server.

## Troubleshooting

**Plugin installed but nothing happens.**
Most often, the plugin was installed into a different Python environment than `cao-server`. Check `which cao-server` and install the plugin into that same environment.

**Plugin logs a `WARNING` at startup.**
Usually a missing or malformed config value (e.g. an unset env var). CAO skips the plugin and keeps running. Fix the config and restart.

**Events don't seem to fire.**
Confirm which events the plugin subscribes to (see [Events](#events)) and that the action you're taking actually emits one of those events. For example, `post_send_message` fires on message delivery to an agent — not on agent output or status changes.

**Plugin appears inactive after a config change.**
Plugins are loaded at startup only. Restart `cao-server` after any install or configuration change.

## Events

When a plugin is installed, it can react to the following events. All events are dispatched *after* the associated operation has completed successfully.

### `post_send_message`

Fires after a message has been delivered to an agent's inbox. Emitted for all three orchestration modes — `send_message`, `handoff`, and `assign`. Multi-step orchestrations (e.g. `assign`) may emit more than one event across their lifecycle.

| Field                | Description                                                           |
|----------------------|-----------------------------------------------------------------------|
| `sender`             | Terminal ID of the sender                                             |
| `receiver`           | Terminal ID of the receiver                                           |
| `message`            | The message text delivered                                            |
| `orchestration_type` | One of `send_message`, `handoff`, `assign`                            |
| `session_id`         | Session the receiver belongs to                                       |
| `timestamp`          | UTC timestamp of the event                                            |

Example use: forward every inter-agent message to a Discord or Slack channel.

### `post_create_session`

Fires after a new session has been created.

| Field          | Description                         |
|----------------|-------------------------------------|
| `session_name` | Human-readable session name         |
| `session_id`   | Unique session identifier           |
| `timestamp`    | UTC timestamp of the event          |

Example use: post a "session started" notification to an external system.

### `post_kill_session`

Fires after a session — and all its terminals — has been shut down.

| Field          | Description                         |
|----------------|-------------------------------------|
| `session_name` | Human-readable session name         |
| `session_id`   | Unique session identifier           |
| `timestamp`    | UTC timestamp of the event          |

Example use: clean up external records tied to the session, or post a completion summary.

### `post_create_terminal`

Fires after a new terminal has been spawned inside a session.

| Field         | Description                                       |
|---------------|---------------------------------------------------|
| `terminal_id` | Unique terminal identifier                        |
| `agent_name`  | Name of the agent profile running in the terminal |
| `provider`    | CLI provider (e.g. `claude_code`, `kiro_cli`)     |
| `session_id`  | Session the terminal belongs to                   |
| `timestamp`   | UTC timestamp of the event                        |

Example use: maintain an external inventory of active agents.

### `post_kill_terminal`

Fires after a terminal has been shut down.

| Field         | Description                                       |
|---------------|---------------------------------------------------|
| `terminal_id` | Unique terminal identifier                        |
| `agent_name`  | Name of the agent profile that was running        |
| `session_id`  | Session the terminal belonged to                  |
| `timestamp`   | UTC timestamp of the event                        |

Example use: remove the terminal from an external inventory or dashboard.

## Authoring a plugin

This document focuses on installing and using plugins. For a full plugin-authoring guide — scaffolding a plugin package, subclassing `CaoPlugin`, wiring up `@hook` methods, and testing — see the [`cao-plugin` skill](../skills/cao-plugin/SKILL.md).

## Future improvements

The items below are **not available today** — they describe the direction the plugin system is expected to grow in.

- **`pre_*` events** — observe operations *before* they happen (e.g. `pre_send_message`, `pre_create_terminal`), giving plugins visibility into intent, not just outcome.
- **Event denial / veto** — let a plugin reject an in-flight operation via `pre_*` return values.
- **Event transformation** — let a plugin rewrite event payloads mid-flight (e.g. redact message content before it's delivered).
- **Plugin management CLI** — `cao plugin list / info / enable / disable / reload` to manage installed plugins without touching `pip` or restarting the server manually.
- **Hot reload** — pick up plugin install, upgrade, or config changes without restarting `cao-server`.
- **Improved discovery and installation UX** — a curated plugin index, a `cao plugin install <name>` wrapper, or a dedicated plugins directory that doesn't require sharing the server's Python environment.
- **First-class per-plugin configuration** — a CAO-delivered configuration channel so plugins no longer have to roll their own env-var / `.env` loading.
- **Richer event catalog** — additional events such as provider status changes, flow step transitions, and inbox reads.
