# Hermes Provider

CAO can launch Hermes Agent as a built-in provider. By default it starts the
main `hermes` command. A CAO agent profile can optionally set `hermesProfile`
to route that agent through a specific Hermes profile wrapper.

## Prerequisites

- Hermes Agent is installed and authenticated.
- Hermes Agent is on `PATH`.
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

## Tool Restrictions

Hermes does not currently expose a CAO-native hard-deny flag equivalent to
Claude Code `--disallowedTools` or Copilot `--deny-tool`. CAO launches the
configured Hermes command in `--yolo` mode for unattended orchestration.
Restrict tools inside the selected Hermes profile itself when you need a
narrower worker.

## Notes

- Runtime skill catalogs from CAO are not injected into Hermes by this provider;
  configure skills on the default Hermes profile or the profile referenced by
  `hermesProfile`.
- MCP/handoff support should be configured through the CAO agent profile and the
  surrounding CAO session. Hermes-side profile customization remains isolated to
  the selected Hermes profile.
