---
name: cao-plugin
description: Create a new CAO (CLI Agent Orchestrator) plugin. Use this skill whenever the user wants to add a plugin that reacts to CAO lifecycle or messaging events, scaffold a plugin package, understand plugin requirements, or integrate an external system (Discord, Slack, dashboards, logging, metrics) with CAO. Also use when the user asks what plugin events are available, how plugin discovery works, or how to install a plugin into a CAO environment.
---

# CAO Plugin Creator

Guide for creating a new CAO plugin. A "plugin" is a Python package installed alongside CAO that subscribes to CAO lifecycle and messaging events via typed async hooks.

## What You're Building

A CAO plugin is a standalone Python package that:

1. **Subclasses `CaoPlugin`** from `cli_agent_orchestrator.plugins`
2. **Registers async hook methods** with `@hook("<event_type>")` decorators
3. **Is discovered via the `cao.plugins` Python entry-point group** at `cao-server` startup
4. **Runs fire-and-forget** — plugin exceptions are caught and logged as warnings, never propagated back into CAO

Typical uses: forwarding inter-agent messages to chat apps, logging/observability, external dashboards, metrics export, alerting on session or terminal lifecycle.

## Before You Start

Gather this information:

- Which events do you need? See `references/hook-events.md` for the full catalog.
- Does the plugin need persistent state across events? (HTTP client, DB connection, buffer) — if so, use `setup()` / `teardown()`.
- How is it configured? v1 has no injected config API — read env vars in `setup()`, optionally via `python-dotenv`.
- What are the failure semantics of your integration? Remember CAO swallows hook exceptions — you must decide whether to log, retry, or drop on your own.

## Hard Requirements

These are the non-negotiable contracts a plugin must satisfy to be loaded and dispatched to. Verify each one before calling your plugin complete.

### 1. Package layout

Minimum viable layout:

```
my-cao-plugin/
├── pyproject.toml          # Build config + entry-point declaration
├── my_cao_plugin/
│   ├── __init__.py         # Can be empty
│   └── plugin.py           # Contains the CaoPlugin subclass
├── tests/                  # Optional but strongly recommended
│   └── test_plugin.py
├── env.template            # Optional; only if the plugin reads env vars
└── README.md               # Optional; install + config instructions for users
```

See `examples/plugins/cao-discord/` in this repo for a complete reference implementation.

### 2. Plugin class contract

- Must subclass `CaoPlugin` from `cli_agent_orchestrator.plugins`.
- Must be zero-arg constructible — the registry instantiates plugins with `cls()`. Do NOT define `__init__` with required parameters.
- May override `async def setup(self) -> None` — called once at `cao-server` startup after instantiation.
- May override `async def teardown(self) -> None` — called once at `cao-server` shutdown.
- No other methods are required. Hooks are opt-in via the `@hook` decorator.

A raising `setup()` disables that plugin for the lifetime of the server process (warning logged, other plugins continue to load). A raising `teardown()` is logged and does not stop other plugins from tearing down.

### 3. Hook method contract

A hook method must:

- Be `async def` — sync hooks are not supported in v1.
- Be decorated with `@hook("<event_type>")` using the exact event-type string from `references/hook-events.md`.
- Accept exactly one positional argument: the typed event dataclass matching that event type.
- Return `None`.
- Be a regular method on the plugin class (the registry discovers hooks via `inspect.getmembers` on the instance).

Multiple hook methods on the same plugin may subscribe to the same event type — each is dispatched independently. Execution order across hooks is not guaranteed.

Exceptions raised inside a hook are caught by the registry and logged as warnings. They do not affect CAO's primary operation and they do not stop other hooks for the same event from running.

### 4. Entry-point registration

Declare the plugin class under the `cao.plugins` entry-point group in `pyproject.toml`:

```toml
[project.entry-points."cao.plugins"]
my_plugin = "my_cao_plugin.plugin:MyPlugin"
```

- The key (`my_plugin`) is the plugin name used in CAO's startup log (`Loaded CAO plugin: my_plugin`). It has no other runtime effect.
- The value must resolve to a class that is a subclass of `CaoPlugin`. Entry points whose target is not a `CaoPlugin` subclass are skipped with a warning.
- A single package may declare multiple entry points under `cao.plugins` if it ships multiple plugin classes.

No CAO-side configuration is required to enable a plugin — installation plus entry-point declaration is sufficient.

### 5. Build system

- Use `hatchling` as the build backend to match CAO's toolchain.
- Target Python `>=3.10`.
- Declare `cli-agent-orchestrator` as a runtime dependency so `CaoPlugin`, `hook`, and the event dataclasses are importable.
- Declare any external libraries (e.g. `httpx`, `aiohttp`, `python-dotenv`) your plugin uses.

Minimal `pyproject.toml`:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "my-cao-plugin"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "cli-agent-orchestrator",
    # ... your plugin's deps
]

[project.entry-points."cao.plugins"]
my_plugin = "my_cao_plugin.plugin:MyPlugin"
```

### 6. Configuration

CAO does not inject configuration into plugins in v1. Options:

- **Environment variables** — read inside `setup()` with `os.environ.get(...)`. Raise `RuntimeError` with a clear message if a required var is missing so the startup log points the user at the misconfiguration.
- **`.env` files** — use `python-dotenv` (`load_dotenv(find_dotenv(usecwd=True))`) inside `setup()`. Process-level env vars override `.env` values, which is the expected precedence.
- **Config files** — load any path you like inside `setup()`; you own the format.

Ship an `env.template` alongside the plugin if it reads env vars, documenting every variable, whether it's required, and its default.

### 7. Lifecycle guarantees

- `setup()` is awaited exactly once, after the plugin class is instantiated at server startup.
- `teardown()` is awaited exactly once at server shutdown, only for plugins whose `setup()` succeeded.
- There is no hot reload — changes to an installed plugin require restarting `cao-server`.
- Event dispatch only happens *after* the underlying CAO operation succeeds (e.g. `post_create_terminal` fires after the terminal is persisted, not before).
- There are no `pre_*` hooks today — you cannot veto or mutate an operation from a plugin.

### 8. Dispatch semantics

- All dispatch is async — hooks are awaited sequentially per event but no ordering is guaranteed across plugins or within a plugin.
- No filtering API — every hook registered for an event type receives every event of that type; filter inside the handler if you need to.
- No delivery guarantees beyond "invoked once per successful dispatch" — plugins are responsible for their own retries, buffering, and error handling.

## Step-by-Step Implementation

### Step 1: Scaffold the package

Create the layout from §1. Populate `pyproject.toml` per §5. `references/plugin-template.md` has a copy-paste skeleton.

### Step 2: Implement the plugin class

In `my_cao_plugin/plugin.py`:

```python
from cli_agent_orchestrator.plugins import CaoPlugin, PostSendMessageEvent, hook


class MyPlugin(CaoPlugin):
    async def setup(self) -> None:
        # Read config, open clients. Raise on misconfiguration.
        ...

    async def teardown(self) -> None:
        # Close clients, flush buffers. Safe to call after failed setup.
        ...

    @hook("post_send_message")
    async def on_message(self, event: PostSendMessageEvent) -> None:
        ...
```

Keep `teardown()` robust to partial `setup()` failures — guard any resource access with `hasattr` or an initialized flag. See `examples/plugins/cao-discord/cao_discord/plugin.py` for the pattern.

### Step 3: Pick events and write handlers

Consult `references/hook-events.md` for the current event catalog, each event type's string, the matching dataclass, and available fields. Import event dataclasses from `cli_agent_orchestrator.plugins`.

### Step 4: Register the entry point

Add the `[project.entry-points."cao.plugins"]` section to `pyproject.toml` per §4.

### Step 5: Install into the CAO environment

The plugin must be importable by the same Python environment that runs `cao-server`.

```bash
# Editable install into the CAO dev virtual environment
uv pip install -e ./my-cao-plugin

# Or, if CAO was installed as a tool:
uv tool install --reinstall cli-agent-orchestrator \
    --with-editable ./my-cao-plugin
```

### Step 6: Verify the plugin loads

Restart `cao-server` and check the startup log for one of:

- `Loaded CAO plugin: my_plugin` — success.
- `Failed to load plugin 'my_plugin'` — `setup()` raised; check the traceback logged alongside.
- `Plugin entry point 'my_plugin' is not a CaoPlugin subclass, skipping` — the entry-point target is wrong.
- `No CAO plugins registered (cao.plugins entry point group is empty)` — the entry point is not declared, or the package was not installed into the same environment as CAO.

### Step 7: Write unit tests

Unit tests for a plugin are straightforward because event dataclasses are zero-arg constructible:

```python
import pytest
from cli_agent_orchestrator.plugins import PostSendMessageEvent
from my_cao_plugin.plugin import MyPlugin


@pytest.mark.asyncio
async def test_on_message_dispatches(monkeypatch):
    plugin = MyPlugin()
    await plugin.setup()
    await plugin.on_message(
        PostSendMessageEvent(
            sender="a",
            receiver="b",
            message="hi",
            orchestration_type="send_message",
        )
    )
    await plugin.teardown()
```

For plugins that make HTTP calls, use `httpx.MockTransport` (see `examples/plugins/cao-discord/tests/test_plugin.py`) rather than real network calls. Assert side-effects (requests sent, logs emitted) — do not assert CAO-side dispatch wiring, which is covered by CAO's own registry tests.

## Install & Verify

Reload the plugin after code changes:

```bash
# Stop cao-server, then reinstall if dependencies changed:
uv pip install -e ./my-cao-plugin --force-reinstall --no-deps
# Then restart cao-server.
```

Troubleshooting checklist if `Loaded CAO plugin:` does not appear:

- [ ] Is the package installed in the same venv that runs `cao-server`? (`uv pip list | grep my-cao-plugin`)
- [ ] Does `pyproject.toml` contain `[project.entry-points."cao.plugins"]`?
- [ ] Does the entry-point value point to an actual `CaoPlugin` subclass? (`python -c "from my_cao_plugin.plugin import MyPlugin; print(MyPlugin.__mro__)"`)
- [ ] Did `setup()` raise? Check `cao-server` logs for a `Failed to load plugin` warning.
- [ ] If using `.env`, is it in the directory where `cao-server` was launched (or a parent)?

## File Checklist

When your plugin is complete, verify:

- [ ] `pyproject.toml` — `hatchling` build backend, `cli-agent-orchestrator` dependency, `cao.plugins` entry point
- [ ] `my_cao_plugin/__init__.py` — present (can be empty)
- [ ] `my_cao_plugin/plugin.py` — `CaoPlugin` subclass with zero-arg construction and at least one `@hook`-decorated async method
- [ ] `env.template` — if any env vars are read
- [ ] `tests/test_plugin.py` — setup/teardown happy and failure paths; at least one test per hook method
- [ ] `README.md` — install commands, config vars, troubleshooting
- [ ] Plugin installs and shows `Loaded CAO plugin:` in `cao-server` startup logs

## References

- `references/hook-events.md` — Full catalog of supported event types, their string identifiers, and event dataclass fields.
- `references/plugin-template.md` — Annotated minimal plugin skeleton.
- `examples/plugins/cao-discord/` — Complete reference plugin (webhook forwarder with `.env` config, HTTP client lifecycle, unit tests).
- `docs/feat-plugin-hooks-design.md` — Full design document for the plugin system.
