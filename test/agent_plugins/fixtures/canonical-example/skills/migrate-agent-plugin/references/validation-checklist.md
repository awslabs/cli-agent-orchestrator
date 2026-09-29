# Validation Checklist

## Package

- [ ] `plugin.json` is a regular file at the plugin root.
- [ ] Every packaged or resolved path remains inside the plugin root.
- [ ] Symlinks, junctions, and reparse points do not escape the package.
- [ ] No credentials, tokens, or private keys are embedded in the package.

## Manifest

- [ ] `$schema` is `https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`.
- [ ] `name` satisfies the v1 length and character rules.
- [ ] Only `$schema`, `name`, `version`, `description`, `author`, `homepage`, `repository`, `license`, `keywords`, and `extensions` appear at the top level.
- [ ] Every `extensions` member is an object keyed by a reverse-domain namespace.
- [ ] Optional metadata has the type required by the schema.

## Skills

- [ ] Each skill is an immediate child directory of `skills/`.
- [ ] Each skill contains a regular file named exactly `SKILL.md`.
- [ ] The frontmatter `name` matches the directory name and Agent Skills naming rules.
- [ ] `description` explains both what the skill does and when to use it.
- [ ] Referenced scripts, references, and assets exist within the skill directory.
- [ ] Each skill validates independently; one invalid skill should not hide failures in another.

## MCP, when present

- [ ] Root `mcp.json` uses `https://agent-plugins.org/schemas/1.0.0/mcp.schema.json`.
- [ ] Its specification version matches `plugin.json`.
- [ ] Every server declares exactly one supported transport variant.
- [ ] `command` is one executable token, not a shell command string.
- [ ] Plugin-relative executable paths and working directories begin with `./`.
- [ ] `${PLUGIN_ROOT}` and `${PLUGIN_DATA}` appear only in fields where expansion is defined.
- [ ] Remote non-loopback URLs use HTTPS and contain no embedded credentials.

## Client compatibility

- [ ] Every client extension uses a namespace implemented and documented by its owning client.
- [ ] Hooks, agents, commands, LSP, UI, and marketplace metadata are not presented as portable v1 components.
- [ ] A compatibility package remains available for clients that still require a legacy layout.
- [ ] Portable and legacy manifests are generated from one metadata source where practical.
- [ ] Installation, update, rollback, and behavior smoke tests pass for every supported client.

## Handoff

- [ ] The migration report maps every original artifact to its new owner and location.
- [ ] Removed files have a verified replacement and recovery path.
- [ ] Remaining limitations and manual release steps are documented.
