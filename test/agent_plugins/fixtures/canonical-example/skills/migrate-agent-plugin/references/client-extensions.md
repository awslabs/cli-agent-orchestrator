# Client Extensions

Agent Plugins v1 keeps the portable core small. Client extensions provide an escape hatch for hooks, agents, commands, LSP, UI, and other behavior that has not become portable.

## Rules

1. Extension namespaces are reverse-domain identifiers, such as `com.vendor.client`.
2. The client that owns the namespace defines its fields, files, validation, and runtime behavior.
3. Manifest extension data belongs under `extensions` in root `plugin.json`.
4. Extension files belong in a top-level directory whose name exactly matches the namespace.
5. Other clients ignore namespaces they do not implement without losing valid portable components.
6. An extension is not a way for a plugin author to make up fields that existing clients will automatically understand.

## Manifest data

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "your-plugin",
  "extensions": {
    "com.vendor.client": {
      "settingDefinedByThatClient": true
    }
  }
}
```

The portable specification validates only that each namespace value is an object. The owning client defines everything inside it.

## Extension files

```text
your-plugin/
├── plugin.json
├── skills/
└── com.vendor.client/
    ├── hooks/
    │   └── hooks.json
    └── agents/
        └── reviewer.md
```

This layout has effect only if `com.vendor.client` actually implements those paths.

## Choose the right compatibility strategy

### The client documents an Agent Plugins extension namespace

Use its exact namespace, fields, and directory layout. Test failures in the extension separately from the portable skills and MCP configuration.

### The client supports Agent Plugins core but still discovers legacy add-ons

Keep the root manifest conforming. Follow the client's documented additive loading behavior for legacy hooks or agents, and label those files as client-specific. If that layout conflicts with strict portable packaging, generate a separate client distribution from the portable source.

### The client does not support Agent Plugins core

Keep the legacy plugin working and add a portable sibling package. Share underlying skill text, scripts, and server code where safe, but avoid symlinks that resolve outside either package root.

## Hooks

Hooks are a common extension candidate, but Agent Plugins v1 does not define their event names, input/output protocol, command format, security model, or discovery path. Preserve the existing hook until the target client documents a replacement. Review hook scripts as executable code and test approval, denial, failure, and timeout behavior after migration.

## Do not put client fields at the manifest top level

These examples are nonconforming in an Agent Plugins v1 root manifest:

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "your-plugin",
  "hooks": "hooks.json",
  "agents": "agents/"
}
```

Use a documented extension or a compatibility package instead.
