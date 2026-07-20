# Agent Profile Format

Agent profiles are Markdown files with YAML frontmatter. The frontmatter
configures CAO and the provider; the Markdown body becomes the agent's system
prompt.

## Structure

```markdown
---
name: developer
description: Implements scoped code changes
role: developer
provider: claude_code
---

You are a developer agent.
```

## Required fields

- `name` (string): profile identifier.
- `description` (string): concise purpose of the profile.

CAO's named-profile loader supplies the filename as `name` and an empty
description when either value is absent, but explicit values keep profiles
portable and make profile listings useful.

## Optional fields

### CAO behavior

- `provider` (string): provider preference for this profile.
- `role` (string): named tool-access role, such as `supervisor`, `developer`,
  or `reviewer`.
- `allowedTools` (array of strings): explicit CAO tool allowlist; when present,
  it overrides the role defaults.
- `capabilities` (array of strings): profile-discovery statements, with at most
  32 strings and 128 characters per string.
- `tags` (array of strings): profile-discovery keywords, with at most 32 values;
  each value must match `A-Za-z0-9_-` and contain at most 64 characters.
- `skills` (array of strings): exact names or case-sensitive
  [`fnmatch`](https://docs.python.org/3/library/fnmatch.html) patterns limiting
  the advertised skill catalog. Omit it for the full catalog; use `[]` for
  none.
- `container.path_maps` (array of `{host, guest}` objects): host-to-guest path
  translations for provider files used inside a container.
- `provider_init_timeout` (integer, seconds): per-profile provider
  initialization timeout.
- `prompt` (string): additional provider prompt text.

### Provider configuration

- `mcpServers` (object): MCP server definitions.
- `tools` (array), `toolAliases` (object), and `toolsSettings` (object):
  provider tool configuration.
- `resources` (array), `hooks` (object), and `useLegacyMcpJson` (boolean):
  provider-native configuration passed through where supported.
- `model` (string): provider model selection.
- `permissionMode` (string): Claude Code permission mode.
- `native_agent` (string): Claude Code native-agent name.
- `codexProfile` (string): named Codex configuration profile.
- `codexConfig` (object): inline Codex configuration overrides.
- `hermesProfile` (string): Hermes profile wrapper command.

Provider support for pass-through fields differs. Use the focused guides for
[Kiro CLI](kiro-cli.md), [Claude Code](claude-code.md),
[Codex CLI](codex-cli.md), [Antigravity CLI](antigravity-cli.md),
[Hermes](hermes.md), [Kimi CLI](kimi-cli.md),
[GitHub Copilot CLI](copilot-cli.md), [OpenCode CLI](opencode-cli.md), and
[Cursor CLI](cursor-cli.md) instead of relying on a duplicated compatibility
catalog here.

## Tool restrictions

If neither `role` nor `allowedTools` is set, CAO resolves the profile with the
default developer permissions. An explicit `allowedTools` list overrides role
defaults. Launch-time options can then alter those resolved restrictions.

See [Tool Restrictions](tool-restrictions.md) for built-in roles, the tool
vocabulary, launch overrides, provider enforcement, and limitations.

## Provider selection and precedence

Provider selection depends on the operation:

1. `cao install --provider` overrides the profile's `provider`; otherwise
   installation uses the profile value and then the default provider.
2. `cao launch --provider` overrides the profile for the initial session;
   otherwise launch uses the profile value and then the default provider.
3. For a worker created by an agent, a valid `provider` in the worker profile
   overrides the parent's provider. If it is absent or invalid, the worker
   inherits the parent provider.

The CLI help and provider resolver are the authoritative sources for this
precedence. Provider-specific launch flags and behavior belong in the focused
provider guides linked above.

## Container-wrapped agents

`container.path_maps` translates paths to temporary prompt or MCP files from
the host namespace to the namespace visible to a wrapped provider CLI. CAO
rewrites matching prefixes using the longest match; it does not create the
container or its bind mounts.

```yaml
container:
  path_maps:
    - host: /home/user/.aws/cli-agent-orchestrator/tmp
      guest: /workspace/cao-tmp
provider_init_timeout: 180
```

The container must mount the host directory at the documented guest path.
Current path-map consumption is provider-specific, so confirm support in the
relevant provider guide before depending on it.

## Installation

Install a profile by built-in name, local Markdown file, or HTTPS URL:

```bash
cao install developer
cao install ./my-agent.md
cao install https://raw.githubusercontent.com/awslabs/cli-agent-orchestrator/main/src/cli_agent_orchestrator/agent_store/developer.md
```

Packaged examples are available in the
[agent store](https://github.com/awslabs/cli-agent-orchestrator/tree/main/src/cli_agent_orchestrator/agent_store).

### Profile discovery

Search installed profiles by capability when the profile name is not known:

```bash
cao profile find "monitor sqs"
cao profile find "monitor sqs" --limit 3 --json
```

The CLI and the read-only `find_profiles` MCP tool search profile names,
descriptions, tags, and capabilities. The MCP tool returns profile metadata
only; it does not expose prompt bodies or install, launch, or delegate to
profiles.
