# Changelog — `cao`

This file is **generated** by `scripts/build_agent_plugin.py`. Edit the package
configuration in that script, not this file; `make check-agent-plugin` fails on
any hand edit.

## 2.4.1

Targets the [Agent Plugins 1.0.0](https://agent-plugins.org/specification)
specification. Version is synced from CAO's own package metadata, so the plugin
and the CAO release it corresponds to cannot diverge.

Skills:

- `cao-session-management`
- `cao-agent-routing`
- `cao-supervisor-protocols`
- `cao-worker-protocols`

Excluded: `cao-provider` and the event-plugin authoring skill (both moved to cao-contributor, where they serve the contributor story); `cao-memory`, `cao-learning`, `cao-workflow` (useful, but they expand the surface before the delivery path is proven); `skills/vendor/ext-apps/*` (Apache-2.0 vendored content with its own NOTICE attribution obligations, which redistributing in a second package would multiply for no benefit); `agui-author`, `cao-mcp-apps`, `mcp-apps-builder` (adjacent features with their own consumers).
