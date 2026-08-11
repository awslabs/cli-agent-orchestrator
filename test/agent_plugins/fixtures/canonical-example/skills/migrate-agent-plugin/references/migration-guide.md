# Migration Guide

Use this guide to map an existing plugin into the Agent Plugins v1 portable core while keeping client-specific behavior available.

## 1. Inventory before conversion

Locate all plugin manifests and component roots. Common legacy or client-specific artifacts include:

- `.claude-plugin/plugin.json`, `.plugin/plugin.json`, `.github/plugin/plugin.json`, `.codex-plugin/plugin.json`, or a root manifest without the Agent Plugins `$schema`.
- Skills under `skills/`, `.agents/skills/`, `.github/skills/`, `.claude/skills/`, or a configured custom path.
- MCP configuration in `.mcp.json`, `.github/mcp.json`, another client config, or inline manifest fields.
- Hooks in `hooks.json`, `hooks/hooks.json`, a settings file, or inline manifest fields.
- Commands, prompts, custom agents, LSP servers, UI assets, authentication declarations, and marketplace catalogs.

Do not delete or move anything until its consumer and replacement are known.

## 2. Map every artifact

| Existing artifact | Agent Plugins v1 destination | Compatibility action |
| --- | --- | --- |
| Plugin identity and metadata | Root `plugin.json` | Retain a legacy manifest only if a target client still requires it. Generate copies from one source when possible. |
| Reusable skill | `skills/<name>/SKILL.md` | Normalize frontmatter and keep scripts, references, and assets inside the skill directory. |
| MCP server | Root `mcp.json` | Convert client-specific fields and declare an explicit transport type. Keep a client adapter only for unsupported fields or transports. |
| Hook | No portable v1 destination | Use a client-owned extension namespace or retain a client compatibility package. |
| Custom agent or persona | No portable v1 destination | Keep it in a client extension or compatibility package. Convert to a skill only when on-demand instructions truly preserve its semantics. |
| Command or prompt | No portable v1 destination | Convert reusable task instructions to a skill when appropriate; otherwise retain the client feature. |
| LSP server | No portable v1 destination | Retain it as a client extension or compatibility package. |
| UI or app integration | No portable v1 destination | Retain it as a client extension or compatibility package. |
| Marketplace entry, install policy, signing | Outside the portable package | Keep it in the platform's distribution repository or release process. |

## 3. Create the portable manifest

Start with the smallest valid root manifest:

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "your-plugin"
}
```

Allowed optional fields are `version`, `description`, `author`, `homepage`, `repository`, `license`, `keywords`, and `extensions`. The schema is closed. Unknown top-level fields are nonconforming even when a particular client historically accepted them.

Plugin names are 1–64 characters, use lowercase ASCII letters, digits, hyphens, and periods, begin and end with an alphanumeric character, and contain neither `--` nor `..`.

## 4. Normalize skills

Each discoverable skill must be an immediate child of `skills/`:

```text
skills/
└── deploy/
    ├── SKILL.md
    ├── scripts/
    ├── references/
    └── assets/
```

The `SKILL.md` name must match its parent directory. Keep skill-relative dependencies inside that directory and update references after moving files. Do not rely on recursive discovery of nested skill directories.

## 5. Convert MCP configuration

Portable MCP configuration belongs in root `mcp.json`, not inline in `plugin.json`:

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
  "mcpServers": {
    "example": {
      "type": "stdio",
      "command": "node",
      "args": ["${PLUGIN_ROOT}/server/index.js"],
      "cwd": "${PLUGIN_ROOT}"
    }
  }
}
```

Use a single executable token for `command`; do not put a shell command line in that field. A bundled executable uses a plugin-relative `./path`. Non-loopback remote servers use HTTPS. Do not embed secrets in remote headers.

## 6. Preserve platform behavior

Migration should be additive first:

1. Add the portable root manifest and components.
2. Leave the working client package intact.
3. Make portable files the source of truth.
4. Generate or copy legacy adapters only when client documentation requires them.
5. Test every supported client.
6. Remove old files only after their consumers have migrated.

A repository can keep the portable plugin and client adapters as siblings:

```text
repository/
├── plugin/                 # Agent Plugins v1 portable package
└── client-adapters/        # Generated or maintained platform packages
    ├── client-a/
    └── client-b/
```

An adapter is not part of the portable core. Clearly label which files are canonical and automate synchronization when multiple manifests or layouts must ship.

## 7. Test the migration

Test at least:

- Loading the plugin with a conforming Agent Plugins client.
- Skill discovery and activation.
- Every MCP transport and tool, when present.
- Legacy installation and all retained hooks, agents, commands, LSP, or UI behavior.
- Upgrade and rollback from the last released client-specific package.

The migration is complete only when the portable core validates and the promised client behaviors still work.
