# MiniMax Code Provider

## Overview

The `minimax_code` provider runs the interactive MiniMax Code CLI (`mcode`) as
a long-lived, multi-turn agent in a CAO tmux window. CAO injects the selected
agent profile and skill catalog in a bootstrap turn, then exposes orchestration
tools such as `handoff`, `assign`, and `send_message` through a terminal-local
MiniMax Plugin.

The provider targets the public `@minimax-ai/code` package and its full-screen
TUI. Status and response extraction are calibrated against `0.1.2`; a later
release can require fixture updates if its visible markers change.

## Install and authenticate

Install the public package and authenticate once outside CAO:

```bash
npm install -g @minimax-ai/code
mcode login
mcode --version
```

MiniMax Code can also use provider credentials configured in its normal
`config.yaml`. Never put access tokens or API keys in a CAO profile or commit
them to a repository.

## Quick start

```bash
cao install developer --provider minimax_code
cao-server
cao launch --agents developer --provider minimax_code
```

Pin a profile to this provider with frontmatter:

```yaml
---
name: minimax_developer
description: Developer backed by MiniMax Code
provider: minimax_code
role: developer
---

Implement the requested change and verify it.
```

MiniMax Code's public interactive CLI does not currently expose a per-session
model flag. CAO therefore rejects `model:` in a `minimax_code` profile and
`--model` at launch instead of silently ignoring them. Select the model in the
normal MiniMax Code configuration before launching CAO.

## Runtime behavior

CAO launches a command equivalent to:

```text
env MINIMAX_DATA_DIR=<private-terminal-data> TERM=xterm-256color \
  mcode '<profile, skill catalog, policy, and bootstrap instruction>'
```

The bootstrap asks MiniMax Code to retain the supplied instructions and reply
with `CAO_MCODE_READY`. CAO waits for that turn to settle before delivering the
first task. Later inbox messages use one Enter after bracketed paste. MiniMax
Code can queue input while it is processing, so eager inbox delivery is
supported.

CAO recognizes the TUI's `Loading` or `Running` activity line as processing,
the approval picker as waiting for a user answer, and `Completed in` followed
by the composer as completion. Last-message extraction returns the final
assistant block only; thinking rows, tool chrome, duration notes, and the
composer are omitted.

## Authentication and Plugin isolation

Every terminal receives a deterministic private directory below:

```text
<CAO_HOME_DIR>/providers/minimax_code/<sha256-terminal-id>/
```

CAO copies only these existing authentication/configuration paths from
`MINIMAX_DATA_DIR` (or `~/.minimax`):

- `config.yaml`
- `local-runtime.auth.json`
- `cli-auth/`

It does not copy user-installed Plugins. The private root and directories use
mode `0700`; copied and generated files use mode `0600`. CAO deletes the exact
terminal directory during normal cleanup and can reconstruct that cleanup
after a `cao-server` restart.

For profiles with `mcpServers`, CAO generates a local MiniMax Plugin containing
`servers.mcp.json`. Each stdio server receives the terminal-specific
`CAO_TERMINAL_ID`, and tool-call timeout is set to 600 seconds so synchronous
handoffs are not cut off by the default MCP timeout. Absolute executable paths
are converted to a bare command plus a terminal-local `PATH` prefix because
the MiniMax Plugin schema requires PATH-resolved commands.

## Tool restrictions

MiniMax Code has no public native flag for CAO's `allowedTools` vocabulary.
Restricted profiles receive the shared CAO security instructions in the
bootstrap prompt. This is soft, advisory enforcement: the model can ignore the
instructions, so do not use `minimax_code` for security-critical restricted
workers. `--yolo` still resolves the CAO profile to unrestricted `['*']`.

The generated Plugin includes only MCP servers declared by the selected
profile. This narrows the injected Plugin surface but does not hard-disable
MiniMax Code's built-in tools.

## Troubleshooting

### Login screen or authentication error

Run `mcode login` in a normal terminal and confirm `mcode` starts successfully.
If `MINIMAX_DATA_DIR` is set, authenticate in that same data directory.

### Orchestration tools are missing

Confirm `cao-mcp-server` is installed in the same Python environment as
`cao-server`, then recreate the terminal. CAO regenerates the Plugin and its
terminal ID at launch.

### Terminal remains processing or output is incomplete

Attach to the tmux session and inspect the visible `Loading`, `Running`,
approval, and `Completed in` markers. Include a scrubbed pane capture and
`mcode --version` when reporting a parsing regression.

## Validation

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  test/providers/test_minimax_code_unit.py \
  test/providers/test_provider_manager_unit.py -k minimax_code \
  -q -o 'addopts='

PYTHONPATH=src .venv/bin/python -m pytest -m e2e \
  test/e2e/test_handoff.py test/e2e/test_assign.py \
  test/e2e/test_send_message.py test/e2e/test_allowed_tools.py \
  test/e2e/test_supervisor_orchestration.py \
  -k MiniMaxCode -v -o 'addopts='
```
