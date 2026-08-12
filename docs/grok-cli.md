# Grok Build CLI Provider

## Overview

The `grok_cli` provider runs the official [xAI Grok Build
CLI](https://docs.x.ai/build) as a long-lived, multi-turn agent in a tmux
window. Community Grok command-line clients and direct xAI API wrappers are
not supported by this provider.

CAO launches Grok's interactive TUI with inline rendering, adds the selected
agent profile and CAO skill catalog as rules, and exposes CAO orchestration
tools through MCP. Grok's own subagent system is disabled so `assign` and
`handoff` remain the only agent-delegation paths in a CAO session.

The integration was developed and tested with Grok Build `1.0.0` and the
`grok-4.5` model. Newer Grok versions may change TUI markers or native tool
names; report status or extraction regressions with `grok --version` output.

## Prerequisites

- tmux 3.3 or later
- The official `grok` executable on `PATH`
- An authenticated Grok account or an xAI API key

Install the CLI using xAI's installer:

```bash
curl -fsSL https://x.ai/cli/install.sh | bash
grok --version
```

Authenticate once in a normal terminal before launching it through CAO:

```bash
grok login
grok models
```

For a remote machine without a browser, use `grok login --device-auth`. Grok
also accepts an API key from `XAI_API_KEY`:

```bash
export XAI_API_KEY="xai-..."
grok models
```

Do not put an API key in an agent profile or commit it to a repository.

## Quick Start

Start `cao-server`, then install and launch a profile for Grok:

```bash
cao install developer --provider grok_cli
cao launch --agents developer --provider grok_cli
```

Profile instructions use the normal Markdown format. The body is appended to
Grok's native system prompt with `--rules`, together with the runtime CAO skill
catalog. This preserves Grok's coding-agent behavior while applying the
profile's role and protocols.

Set a default model in profile frontmatter:

```yaml
---
name: grok_developer
description: Developer backed by Grok Build
provider: grok_cli
model: grok-4.5
role: developer
---

Implement the requested change and verify it.
```

An explicit launch override takes precedence:

```bash
cao launch --agents grok_developer --provider grok_cli --model grok-4.5
```

Use `grok models` to discover model IDs available to the authenticated
account.

## Runtime Behavior

The command has this shape:

```text
grok --no-alt-screen --always-approve --no-subagents \
  [--model MODEL] [--rules RULES] [--deny RULE ...]
```

- `--no-alt-screen` keeps the rendered conversation observable by CAO.
- `--always-approve` prevents ordinary tool approval prompts from blocking
  unattended orchestration.
- Native `--deny` rules still override auto-approval and provide hard tool
  restrictions.
- `--no-subagents` prevents Grok-native workers from bypassing CAO roles,
  permissions, callbacks, or terminal accounting.
- A single Enter submits bracketed-paste input. `/quit` exits the session.

The empty `❯` composer may remain visible while Grok is working. CAO therefore
prioritizes current `Waiting for response…` and `Esc:cancel` markers over the
composer. A settled turn has a `Worked for ...` boundary, which CAO also uses
to extract only the latest response in a multi-turn session.

## MCP Isolation

CAO creates a private Grok home for every terminal and launches Grok with
`GROK_HOME` pointing to it. The terminal root is mode `0700`; CAO writes its
generated config atomically with mode `0600`. It does not run `grok mcp add`
and does not modify the user's `~/.grok/config.toml`.

The isolated config contains the profile's MCP servers. CAO injects the
terminal-specific `CAO_TERMINAL_ID` into stdio MCP server environments so
`cao-mcp-server` can route `assign`, `handoff`, and `send_message` correctly.
Existing login state is reused without copying credential contents into CAO
logs or the repository. Generated state is removed when the terminal is
cleaned up.

A newly isolated home can show Grok's `Help improve Grok` telemetry choice.
The banner is non-blocking and is ignored by CAO's status and response
extraction logic.

## Tool Restrictions

Grok is a hard-enforcement provider. CAO translates missing capabilities into
native Grok deny rules:

| CAO capability | Grok tools denied when absent |
|---|---|
| `execute_bash` | `Bash` |
| `fs_read` | `Read`, `NotebookRead` |
| `fs_write` | `Edit`, `Write`, `NotebookEdit` |
| `fs_list` | `Grep`, `Glob` |
| `web_fetch` | `WebFetch`, `WebSearch`, with web search disabled |

`allowedTools: ["*"]` adds no restrictive deny rules. For a restricted role,
deny rules are applied alongside `--always-approve`; auto-approval does not
turn a denied tool back on. The provider also always passes `--no-subagents`
to close the native-subagent escape path.

`@cao-mcp-server` follows CAO's current shared MCP limitation: it records the
profile's orchestration intent, but individual MCP tools are not blocked at
the provider level. See [Tool Restrictions](tool-restrictions.md).

## Assign and Handoff Example

Install all profiles for this provider before running the full orchestration
example:

```bash
cao install examples/assign/data_analyst.md --provider grok_cli
cao install examples/assign/report_generator.md --provider grok_cli
cao install examples/assign/analysis_supervisor.md --provider grok_cli
cao launch --agents analysis_supervisor --provider grok_cli --auto-approve
```

`--auto-approve` skips CAO's launch confirmation but retains role-based tool
restrictions. Do not substitute `--yolo` when validating supervisor safety.

## Known Limitations

- The provider targets Grok Build's interactive TUI and currently requires the
  tmux backend. Headless `-p` and ACP modes are not CAO transports.
- TUI parsing is calibrated against Grok Build 1.0.0. A future layout change
  may require updated status and extraction fixtures.
- CAO reuses existing Grok authentication. Complete interactive login first;
  CAO does not drive account or device-code login screens.
- Per-tool MCP gating is not available. `@cao-mcp-server` does not selectively
  hide `assign`, `handoff`, or `send_message`.
- Grok-created non-secret files inside the private `0700` home can use their
  own modes; the `0600` guarantee applies to CAO-authored config files.

## Troubleshooting

### Login or model errors

Run `grok login` and `grok models` outside CAO. On a headless host, use
`grok login --device-auth` or set `XAI_API_KEY`. If a profile selects an
unavailable model, replace it with an ID printed by `grok models`.

### MCP tools are missing or time out

Confirm `cao-mcp-server` is installed in the same environment as `cao-server`.
Inspect the Grok terminal for an MCP startup error, then recreate the terminal
so CAO regenerates its isolated config and terminal ID.

### Terminal remains processing

Attach to the tmux session and check whether Grok still shows
`Waiting for response…` or `Esc:cancel`. If Grok is visibly settled but CAO
does not report completion, include a scrubbed pane capture and `grok --version`
in the bug report.

### Permission or telemetry prompt is visible

The telemetry banner is non-blocking. An actual permission picker should be
reported as waiting for user input; answer it in tmux. Restricted tool calls
should be denied automatically rather than prompting.

### Broken rendering

Use tmux 3.3 or later and a normal color terminal such as
`TERM=xterm-256color` or `TERM=tmux-256color`. Verify `grok --no-alt-screen`
works in a standalone tmux pane.

## Validation

```bash
# Provider unit tests
uv run pytest test/providers/test_grok_cli_unit.py -v -o "addopts="

# All Grok lifecycle, permissions, skills, and orchestration e2e tests
uv run pytest -m e2e test/e2e/ -k Grok -v -o "addopts="

# Maintainer-required three-analyst workflow
uv run pytest -m e2e \
  test/e2e/test_supervisor_orchestration.py \
  -k GrokCliSupervisorOrchestration -v -o "addopts="
```
