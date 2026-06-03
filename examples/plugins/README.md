# Plugins — Example Plugins

CAO plugins are observer-only extensions that react to server-side events (session/terminal lifecycle, message delivery) inside `cao-server`. They run in-process and are notified after each event occurs. For the full reference — installation, events, configuration, and troubleshooting — see [docs/plugins.md](../../docs/plugins.md).

## Available Examples

| Example | Description |
|---------|-------------|
| **[cao-discord](cao-discord/)** | Forwards inter-agent messages to a Discord channel via webhook, rendering your CAO workflow as a live group chat in Discord. |

## Authoring Your Own

To scaffold a new plugin from scratch, use the **cao-plugin** skill ([`skills/cao-plugin/SKILL.md`](../../skills/cao-plugin/SKILL.md)). For the plugin API and hook contract, see [docs/plugins.md § Authoring a plugin](../../docs/plugins.md#authoring-a-plugin).
