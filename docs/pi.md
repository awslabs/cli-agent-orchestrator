# Pi Provider

## Quick start

Install an agent profile for Pi, then launch it:

```bash
cao install developer --provider pi
cao launch --agents developer --provider pi
```

Before launching, install [Pi](https://pi.dev), make sure `pi` is discoverable
on `PATH`, and complete Pi's authentication and model setup in a regular
terminal. CAO does not install Pi or configure its credentials. At provider
construction, CAO resolves the executable from `PATH` and pins that exact
absolute path for the session.

## Profiles and models

Pi uses the standard CAO Markdown agent profile. Its Markdown body (or the
profile's `prompt:` fallback) and the runtime skill catalog become additional
system instructions.
Set a Pi model in frontmatter with `model:`:

```yaml
---
name: developer
description: Implements scoped code changes
provider: pi
model: openai/gpt-5
role: developer
---
```

`cao launch` has no per-launch model option. Supervisors using CAO's MCP tools
can supply the optional `model` argument to `handoff` or `assign`; CAO forwards
that model to the newly created Pi worker for that call. If no model is supplied
by the MCP call or profile, Pi uses its configured default model.

Profile `mcpServers`, `role`, `allowedTools`, and `skills` also apply. See
[Agent Profile Format](agent-profile.md), [Tool Restrictions](tool-restrictions.md),
and [Skills](skills.md).

## Terminal and lifecycle

CAO launches Pi's regular, attachable TUI in the selected terminal backend.
Bracketed-paste input is submitted with one Enter. In an attached terminal,
`C-d` exits Pi. CAO also uses that provider exit key during the explicit
`run_agent_step` exit path. Normal terminal deletion and `cao shutdown` instead
terminate the selected backend's window or pane; they do not first send Pi a
graceful `C-d`.

The bundled CAO extension records authoritative lifecycle state outside the
rendered TUI:

- `idle`: Pi is ready for input.
- `processing`: an agent turn has started.
- `completed`: the turn settled and the exact latest assistant text is ready.
- `error`: extension or MCP bridge startup/runtime failed.

CAO uses this state for status and final-answer extraction, so `LAST` output is
the assistant's answer rather than terminal chrome. Conservative TUI parsing is
used only during startup and as a failure fallback.

## Explicit Pi resource isolation

CAO starts Pi with ambient extensions, skills, and prompt templates disabled.
It explicitly loads only CAO's bundled Pi extension. Project-local Pi packages
are not approved. CAO does not directly write Pi credential or configuration
files, or directly install packages in Pi's own agent directory. Pi still owns
that directory (normally below `~/.pi`) and may migrate or update configuration
or install/manage Pi packages as part of its own runtime behavior.

Repository instruction context remains available. For example, Pi can still
discover `AGENTS.md` because CAO does not disable Pi's context-file discovery.
This distinction is intentional: Pi package/resource discovery is isolated,
while repository instructions remain part of the coding-agent context.

## MCP bridge

Pi has no native MCP client. CAO's bundled extension starts a bundled JSONL
proxy, which uses CAO's official Python MCP SDK dependency to connect to the
profile's MCP servers. The extension discovers each server's tools and
dynamically registers their original names and JSON Schema parameters with Pi.
Duplicate exposed tool names fail closed rather than selecting one
ambiguously.

The first version supports command-based stdio MCP servers only. CAO resolves
their commands and explicit environment, injects the current terminal identity,
and honors each resolved request timeout. URL/non-stdio transports are rejected
with a bridge configuration error.

### MCP permission boundary

`@cao-mcp-server` remains an all-or-nothing marker. CAO cannot currently allow
or block individual MCP tools such as `assign`, `handoff`, or `send_message`.
Pi's native `--exclude-tools` denylist does not change that MCP limitation.

A restricted parent may deliberately delegate to a child whose own profile has
broader permissions. The child resolves its own `role` or `allowedTools`; the
parent's Pi built-in denylist does not propagate to child agents. Review both
profiles when the delegated child crosses a privilege boundary.

## Tool restrictions

CAO translates its portable capability vocabulary to Pi's seven built-in tools:

| CAO capability | Pi built-ins |
|---|---|
| `execute_bash` | `bash` |
| `fs_read` | `read` |
| `fs_write` | `edit`, `write` |
| `fs_list` | `grep`, `find`, `ls` |
| `fs_*` | `read`, `edit`, `write`, `grep`, `find`, `ls` |

Pi has no core `web_fetch` tool. CAO computes the denied Pi built-ins from the
resolved profile policy and passes them through Pi's `--exclude-tools` flag.
This is hard enforcement for Pi's own built-in tools: a denied built-in is not
available to the agent runtime.

Hard built-in enforcement is not a sandbox and does not make MCP tools
individually restrictable. See [Tool Restrictions](tool-restrictions.md) for the
complete role and delegation model.

## Skills

Pi does not use ambient Pi-native skill discovery under CAO. Instead, CAO
generates the selected runtime skill catalog and appends it to the agent's
prompt. The agent retrieves full skill content with CAO's `load_skill` MCP tool.
The profile's `skills` field scopes what appears in the catalog; it is not an
access-control boundary for `load_skill`.

## Storage and security boundary

Pi and its MCP child processes run with the current user's operating-system
permissions. Neither CAO's tool flags nor Pi's `--no-approve` option provides a
filesystem or container sandbox. Use OS/container isolation separately when the
workload requires it.

Pi runtime data lives below `CAO_HOME_DIR/pi` (by default,
`~/.aws/cli-agent-orchestrator/pi`):

- CAO creates runtime and session directories with owner-only mode `0700`.
- Generated prompt, resolved MCP configuration, and lifecycle state files use
  mode `0600`.
- Provider cleanup removes the transient prompt, MCP configuration, and state
  files.
- Provider cleanup deliberately leaves each terminal's Pi session directory in
  place. These session directories currently persist indefinitely and require
  manual deletion when an operator no longer wants them.

The generated `CAO_PI_*` environment variables connect the provider and bundled
extension. They are internal implementation details, not supported user-facing
configuration. Configure the provider through CAO profiles and CLI options
instead.

## Troubleshooting

### `Pi executable 'pi' was not found on PATH`

Install Pi using the current instructions at [pi.dev](https://pi.dev), then
confirm `pi --version` works in the environment that starts `cao-server`. Restart
the server after changing `PATH`; CAO resolves and pins the executable when it
constructs the provider.

### No configured model or authentication failure

Run Pi directly in a regular terminal and complete its model/authentication
setup before launching CAO. If the profile sets `model:`, or an MCP `handoff` or
`assign` call supplies `model`, verify that exact model is available to Pi. CAO
does not copy credentials or directly write Pi's personal configuration, but
Pi itself may update its agent directory during normal operation.

### MCP bridge startup or configuration error

Check the profile's `mcpServers` entries. Pi V1 accepts command-based stdio
servers only; URL transports, missing commands, invalid environment values,
invalid timeouts, and duplicate exposed tool names fail closed. Do not place
secrets in commands or paste resolved bridge configuration into bug reports.

### Session is stuck or reports `error`

Inspect CAO's current view first:

```bash
cao session status cao-<session-name> --workers
```

Attach to the named tmux session to inspect the Pi TUI, or use the
[tmux guide](tmux.md) for pane and log inspection. Bridge failures also appear
in Pi's status UI and the CAO server logs. Preserve error text, but redact
credentials and explicit MCP environment values before sharing diagnostics.

When finished, shut down the CAO session normally:

```bash
cao shutdown --session cao-<session-name>
```

This deletes the terminal by terminating the selected backend's window or pane.
If an attached Pi TUI itself needs to exit gracefully, press `C-d` before
deletion.
