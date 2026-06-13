# Hermes Provider

CAO can launch Hermes Agent as a built-in provider. By default it starts the
main `hermes` command. A CAO agent profile can optionally set `hermesProfile`
to route that agent through a specific Hermes profile wrapper.

## Prerequisites

- Hermes Agent is installed and authenticated.
- Hermes Agent is on `PATH`.
- For CAO multi-agent orchestration from inside Hermes, configure the CAO MCP
  server in the selected Hermes profile. CAO does not rewrite Hermes
  `config.yaml` or inject `mcpServers` automatically.
- Optional: a Hermes profile wrapper is on `PATH` if you want this CAO profile
  to use a non-default Hermes profile:

```bash
hermes profile alias test-worker
which test-worker
```

## CAO Profile

Create a CAO agent profile that selects the Hermes provider:

```yaml
---
name: hermes_default
description: Developer backed by the default Hermes profile
provider: hermes
role: developer
---

You are a helpful developer agent.
```

To use a specific Hermes profile wrapper, add `hermesProfile`:

```yaml
---
name: hermes_developer
description: Developer backed by a Hermes worker profile
provider: hermes
hermesProfile: test-worker
role: developer
---

You are a helpful developer agent.
```

`hermesProfile` is the shell command CAO launches instead of `hermes`. In the
example above it is the profile alias created by `hermes profile alias
test-worker`.

Keep this field separate from `codexProfile`. Codex profiles name
`[profiles.<name>]` blocks in `~/.codex/config.toml` and are passed as
`codex --profile <name>`. Hermes profile aliases are executable wrapper
commands, so CAO launches the alias directly as `<alias> chat ...`. Using a
Hermes-specific field keeps that command-wrapper behavior explicit.

## Launch

```bash
cao launch --agents hermes_developer --auto-approve
cao launch --agents hermes_developer --yolo
```

Without `hermesProfile`, CAO starts Hermes with:

```bash
hermes chat --yolo --accept-hooks --source cao
```

With `hermesProfile: test-worker`, CAO starts Hermes with:

```bash
test-worker chat --yolo --accept-hooks --source cao
```

If the CAO agent profile sets `model`, CAO appends `--model <value>`.

## MCP Configuration

Hermes reads MCP servers from the selected Hermes profile configuration. CAO
launches Hermes with the right `CAO_TERMINAL_ID` environment variable, but it
does not mutate Hermes profile files or create a temporary overlay config.

To let a Hermes supervisor call CAO orchestration tools such as `assign`,
`handoff`, and `send_message`, add `cao-mcp-server` to the Hermes profile used
by `hermesProfile`:

```yaml
mcp_servers:
  cao-mcp-server:
    enabled: true
    command: cao-mcp-server
    env:
      CAO_TERMINAL_ID: ${CAO_TERMINAL_ID}
```

If `cao-mcp-server` is not on `PATH` for Hermes, use an absolute path:

```yaml
mcp_servers:
  cao-mcp-server:
    enabled: true
    command: /absolute/path/to/cao-mcp-server
    env:
      CAO_TERMINAL_ID: ${CAO_TERMINAL_ID}
```

Do this in the Hermes profile selected by `hermesProfile` (for example
`~/.hermes/profiles/test-worker/config.yaml` when using a `test-worker` alias).
The `CAO_TERMINAL_ID` environment entry is required so each Hermes-launched MCP
server can identify the CAO terminal it belongs to when calling `send_message`,
`assign`, or `handoff`.

## Prompt Detection

Hermes themes can customize the visible prompt, prompt symbol, and assistant
divider. The provider therefore avoids hard-coding concrete prompt strings.
Defaults prefer stable status-bar signals over prompt symbols:

- idle: the status-bar idle timer `⏲ <duration>` is unchanged across consecutive polls
- processing: prompt placeholder/status text such as `msg=interrupt`, `/queue`, `/bg`, `Ctrl+C cancel`, `musing...`, `Initializing agent`, or active timer hints
- response extraction: assistant divider when present, otherwise the last non-status content block after the last user message

If your Hermes profile uses a very different theme, override the patterns:

```bash
export CAO_HERMES_IDLE_PROMPT_REGEX='^my-worker > $'
export CAO_HERMES_PROCESSING_REGEX='working|thinking|interrupt'
export CAO_HERMES_ASSISTANT_HEADER_REGEX='^--- assistant ---$'
export CAO_HERMES_USER_PREFIX_REGEX='^User: '
```

## Interactive Prompt Answers

Hermes is currently the only in-tree provider that reports
`WAITING_USER_ANSWER` and uses key-based navigation for structured approval
prompts and clarify pickers. Supervisors can answer those prompts with
`answer_user_prompt(terminal_id, answer)`.

For clarify pickers, numeric answers select the corresponding option with
`Down`/`Enter` key presses. Free-form answers navigate to the `Other` option
and then submit text input. Other providers may show prompts in their terminal
output, but they do not currently expose the same structured
`WAITING_USER_ANSWER` behavior. When CAO adds that behavior to another
provider, this document should be updated with that provider's prompt contract.

Any agent with access to `cao-mcp-server` and a target terminal ID can answer a
waiting Hermes prompt. CAO does not currently enforce a parent/supervisor
relationship for `answer_user_prompt`, so profile authors should expose the MCP
server only to agents that are trusted to coordinate the session.

## Tool Restrictions

Hermes does not currently expose a CAO-native hard-deny flag equivalent to
Claude Code `--disallowedTools` or Copilot `--deny-tool`. CAO launches the
configured Hermes command in `--yolo` mode for unattended orchestration.
Restrict tools inside the selected Hermes profile itself when you need a
narrower worker.

## Notes

- Hermes does not have a CAO-native hard-deny flag for tool restrictions. Keep
  strict tool policy inside the selected Hermes profile.
- Runtime skills and MCP servers must be configured in the selected Hermes
  profile. CAO deliberately avoids mutating Hermes profile configuration so
  Hermes session history stays attached to the user's chosen profile.
