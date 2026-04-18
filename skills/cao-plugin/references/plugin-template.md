# CAO Plugin Template

Copy-paste-ready skeleton for a new CAO plugin. Replace every `my_cao_plugin` / `MyPlugin` / `MY_*` token with your plugin's names.

## Directory layout

```
my-cao-plugin/
├── pyproject.toml
├── my_cao_plugin/
│   ├── __init__.py          # empty
│   └── plugin.py
├── env.template             # only if your plugin reads env vars
├── tests/
│   ├── __init__.py          # empty
│   └── test_plugin.py
└── README.md
```

## `pyproject.toml`

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "my-cao-plugin"
version = "0.1.0"
description = "Short one-line description"
requires-python = ">=3.10"
dependencies = [
    # REQUIRED: gives access to CaoPlugin, @hook, and event dataclasses.
    "cli-agent-orchestrator",
    # Add any libraries your plugin uses, e.g.:
    # "httpx>=0.27",
    # "python-dotenv>=1.0",
]

# REQUIRED: this is how CAO discovers the plugin. The key is the plugin name
# shown in cao-server startup logs. The value is "<pkg.module>:<ClassName>"
# and MUST resolve to a CaoPlugin subclass.
[project.entry-points."cao.plugins"]
my_plugin = "my_cao_plugin.plugin:MyPlugin"
```

## `my_cao_plugin/plugin.py`

```python
"""Skeleton CAO plugin."""

import logging
import os

from cli_agent_orchestrator.plugins import (
    CaoPlugin,
    PostCreateSessionEvent,
    PostCreateTerminalEvent,
    PostKillSessionEvent,
    PostKillTerminalEvent,
    PostSendMessageEvent,
    hook,
)

logger = logging.getLogger(__name__)


class MyPlugin(CaoPlugin):
    """Describe what the plugin does in one line."""

    async def setup(self) -> None:
        """Called once at cao-server startup, after __init__.

        Read config, open clients, warm caches. Raise here if the plugin cannot
        run — the registry will log a warning and skip this plugin for the
        lifetime of the server process; other plugins continue to load.
        """
        # Example: required env var
        value = os.environ.get("MY_PLUGIN_CONFIG")
        if not value:
            raise RuntimeError(
                "MY_PLUGIN_CONFIG is not set. "
                "Set it in the environment or a .env file before starting cao-server."
            )
        self._config = value

    async def teardown(self) -> None:
        """Called once at cao-server shutdown.

        Must be safe to call even when setup() raised before fully initializing
        — guard any resource access. Exceptions here are logged and swallowed.
        """
        # Example: close a client only if it was created
        # if hasattr(self, "_client"):
        #     await self._client.aclose()

    # ---- Hook methods ----
    # Each handler must be `async def`, decorated with @hook("<event_type>"),
    # take exactly one event argument, and return None. Exceptions are caught
    # by the registry and logged as warnings.

    @hook("post_send_message")
    async def on_send_message(self, event: PostSendMessageEvent) -> None:
        logger.info(
            "message %s -> %s (%s): %s",
            event.sender,
            event.receiver,
            event.orchestration_type,
            event.message,
        )

    @hook("post_create_session")
    async def on_create_session(self, event: PostCreateSessionEvent) -> None:
        ...

    @hook("post_kill_session")
    async def on_kill_session(self, event: PostKillSessionEvent) -> None:
        ...

    @hook("post_create_terminal")
    async def on_create_terminal(self, event: PostCreateTerminalEvent) -> None:
        ...

    @hook("post_kill_terminal")
    async def on_kill_terminal(self, event: PostKillTerminalEvent) -> None:
        ...
```

Delete any `@hook` methods you don't need — there are no "required" events.

## `env.template` (optional)

Only ship this if your plugin reads environment variables. Document each one:

```dotenv
# my-cao-plugin configuration
#
# Copy to `.env` in the directory where you launch `cao-server` from (or any
# parent — python-dotenv walks upward from CWD). Real shell env vars override
# values here.

# Required.
MY_PLUGIN_CONFIG=replace-me

# Optional. Defaults to 5.0.
#MY_PLUGIN_TIMEOUT_SECONDS=5.0
```

## `tests/test_plugin.py`

```python
import pytest

from cli_agent_orchestrator.plugins import PostSendMessageEvent
from my_cao_plugin.plugin import MyPlugin


@pytest.mark.asyncio
async def test_setup_raises_when_config_missing(monkeypatch):
    monkeypatch.delenv("MY_PLUGIN_CONFIG", raising=False)
    plugin = MyPlugin()
    with pytest.raises(RuntimeError, match="MY_PLUGIN_CONFIG"):
        await plugin.setup()


@pytest.mark.asyncio
async def test_on_send_message_handles_event(monkeypatch):
    monkeypatch.setenv("MY_PLUGIN_CONFIG", "test-value")
    plugin = MyPlugin()
    await plugin.setup()
    try:
        await plugin.on_send_message(
            PostSendMessageEvent(
                sender="a",
                receiver="b",
                message="hi",
                orchestration_type="send_message",
            )
        )
    finally:
        await plugin.teardown()


@pytest.mark.asyncio
async def test_teardown_safe_after_failed_setup(monkeypatch):
    monkeypatch.delenv("MY_PLUGIN_CONFIG", raising=False)
    plugin = MyPlugin()
    with pytest.raises(RuntimeError):
        await plugin.setup()
    # Teardown must not raise even though setup never completed.
    await plugin.teardown()
```

## Install

From the CAO development environment:

```bash
uv pip install -e ./my-cao-plugin
```

If CAO was installed as a tool:

```bash
uv tool install --reinstall cli-agent-orchestrator \
    --with-editable ./my-cao-plugin
```

Restart `cao-server` and confirm `Loaded CAO plugin: my_plugin` appears in the startup log.
