---
name: migrate-agent-plugin
description: Migrate an existing Claude, Copilot, Codex, Cursor, Kiro, VS Code, or other client-specific agent plugin to the portable Agent Plugins v1 structure while preserving platform-specific hooks, agents, commands, LSP, UI, and marketplace behavior. Use when auditing, converting, or modernizing an agent plugin.
license: MIT
metadata:
  version: "1.0.0"
---

# Migrate an Agent Plugin

Convert an existing plugin to the Agent Plugins v1 portable core without prematurely removing behavior required by its current clients.

## Source of truth

Use the current [Agent Plugins specification](https://agent-plugins.org/specification) as the normative source.

Read these references before editing:

- [Migration guide](references/migration-guide.md)
- [Client extensions](references/client-extensions.md)
- [Validation checklist](references/validation-checklist.md)

## Workflow

1. Inventory the current plugin before moving files.
   - Record every manifest, skill, prompt or command, agent, MCP server, hook, LSP server, UI resource, script, secret requirement, and marketplace entry.
   - Identify the clients that currently load each artifact and the install paths or discovery rules they require.
   - Run existing tests or capture a manual smoke-test baseline.

2. Classify each artifact.
   - Portable core: root `plugin.json`, Agent Skills in `skills/`, and MCP servers in root `mcp.json`.
   - Client extension: additional behavior loaded through a reverse-domain namespace owned and documented by a client.
   - Compatibility layer: legacy files or a generated client package retained until that client supports the portable or namespaced form.
   - Distribution metadata: marketplace catalogs, install policy, signing, and release configuration; these are outside the portable package format.

3. Add the portable manifest.
   - Create `plugin.json` at the plugin root.
   - Set `$schema` to `https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`.
   - Include `name` and only supported metadata fields.
   - Do not put component paths or client fields such as `hooks`, `agents`, `skills`, or `mcpServers` at the top level.

4. Normalize portable components.
   - Put each skill at `skills/<skill-name>/SKILL.md`; only immediate children of `skills/` are discovered.
   - Make each skill name match its parent directory and the Agent Skills naming rules.
   - If MCP is present, convert it to root `mcp.json`, declare the matching v1.0.0 schema, and give every server an explicit `stdio`, `streamable-http`, or `sse` type.
   - Use `${PLUGIN_ROOT}` for packaged read-only resources and `${PLUGIN_DATA}` for persistent writable state where the MCP schema permits expansion.

5. Preserve non-core behavior.
   - Use a client extension only when the target client publishes a reverse-domain namespace and its semantics.
   - If the client still requires a legacy layout, keep or generate a separate compatibility package. Treat the portable files as the source of truth and avoid manually maintaining divergent copies.
   - Do not invent a vendor namespace and assume an unrelated client will load it.

6. Validate and test incrementally.
   - Validate the portable manifest, every skill, optional MCP configuration, and package path containment.
   - Test each supported client independently, including hooks and other compatibility behavior.
   - Remove legacy artifacts only after the replacement passes the same behavior checks.

## Required migration report

Before finishing, report:

- The discovered source format and target clients.
- A mapping from every original artifact to portable core, extension, compatibility layer, distribution metadata, or removal.
- Files added, moved, generated, retained, and intentionally omitted.
- Validation and client smoke-test results.
- Remaining client-specific risks or manual steps.

Prefer an additive, reversible migration. Never claim that hooks, agents, commands, LSP servers, UI, or marketplace metadata became portable Agent Plugins v1 components.
