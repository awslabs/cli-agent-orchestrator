# cao-discord

`cao-discord` is a CAO plugin that forwards inter-agent messages to a Discord channel through a webhook, rendering your CAO workflow as a live group chat of bots in Discord.

## Install

From the repository root, inside the CAO development virtual environment:

```bash
uv pip install -e examples/plugins/cao-discord
```

## Example `.env`

```dotenv
CAO_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1234567890/abcdef...
CAO_DISCORD_TIMEOUT_SECONDS=5.0
```

## Setup

1. Create a webhook in Discord: Channel -> Edit Channel -> Integrations -> Webhooks -> New Webhook -> Copy URL.
2. Install the plugin (from the repository root, inside the CAO development virtual environment):
   ```bash
   uv pip install -e examples/plugins/cao-discord
   ```
3. Create a `.env` file in the directory where you will run `cao-server`, or export the variables in your shell:
   ```dotenv
   CAO_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1234567890/abcdef...
   CAO_DISCORD_TIMEOUT_SECONDS=5.0
   ```
4. Start the server:
   ```bash
   cao-server
   ```
5. Launch a multi-agent workflow such as `cao flow ...` and watch the Discord channel for forwarded inter-agent messages.

## Configuration

| Variable | Required | Description |
| --- | --- | --- |
| `CAO_DISCORD_WEBHOOK_URL` | Yes | Full Discord webhook URL in the form `https://discord.com/api/webhooks/{id}/{token}`. |
| `CAO_DISCORD_TIMEOUT_SECONDS` | No | HTTP timeout in seconds for webhook POSTs. Defaults to `5.0`. |

## Troubleshooting

If `CAO_DISCORD_WEBHOOK_URL` is missing, `PluginRegistry.load()` logs a warning during `cao-server` startup and skips registering the plugin for the lifetime of that server process.

## Alternative: global tool install

If you prefer installing CAO as a global `uv` tool rather than working inside the development venv, you can bundle the plugin in a single install from the project root:

```bash
uv tool install --reinstall . \
  --with-editable ./examples/plugins/cao-discord
```

## Note

This plugin is provided as an example to demonstrate a plugin use case and implementation. It is not expected to be actively maintained. Users are encouraged to take the implementation as a starting point and build on it for their own use cases.
