# Antigravity CLI Provider

## Overview

The Antigravity CLI provider enables CAO to work with [Antigravity CLI](https://antigravity.google) (`agy`), Google's terminal-native AI coding agent and the successor to the Gemini CLI after Google retired the free Gemini Code Assist "Login with Google" path for the `gemini` binary (2026-06-18). `agy` runs as an interactive full-screen TUI that keeps scrollback history in tmux.

## Prerequisites

- **Antigravity CLI**: Install via `curl -fsSL https://antigravity.google/cli/install.sh | bash`
- **Authentication**: Run `agy` once and complete the Google sign-in + one-time onboarding (color scheme + Terms/Data-Use). The provider expects a fully onboarded CLI so no setup screens appear inside CAO-spawned sessions.
- **tmux 3.2+**

Verify installation:

```bash
agy --version
agy models   # confirms auth + lists model strings
```

## Backend Support

The provider currently requires the **tmux** backend (`cao-server --terminal tmux`, the default). It opts into screen-based status detection (`supports_screen_detection = True`), which is driven by the FIFO / pyte pipeline that tmux provides.

The **herdr** backend is **not yet supported**: herdr uses an event inbox and skips the FIFO pipeline for providers it has no native status integration for, so `agy`'s state is never observed and terminals time out. Generic herdr FIFO support for non-native providers is tracked as a follow-up; until then, run antigravity_cli on the tmux backend.

## Quick Start

```bash
# Launch with CAO
cao launch --agents reviewer_gemini --provider antigravity_cli
```

Set a model in the agent profile (`model:` field). Model names are the human-readable strings `agy models` prints, e.g. `"Gemini 3.1 Pro (High)"`, `"Gemini 3.5 Flash (High)"`, `"Claude Sonnet 4.6 (Thinking)"`.

## Launch Command

```
agy --dangerously-skip-permissions [--model "<model>"] [-i "<system prompt>"]
```

- `--dangerously-skip-permissions` auto-approves tool calls so orchestrated (handoff / assign) flows do not block on per-tool approval prompts.
- `--model` selects the model (profile `model:` wins over a constructor override).
- `-i` (`--prompt-interactive`) injects the agent profile's system prompt (+ skill catalog, + security prompt when tool-restricted) as the first message, with an explicit "acknowledge your role and wait" guard so the agent adopts its role without exploring on launch.

## Status Detection

The provider classifies `agy` states from the tmux output buffer (footer-anchored, render-stable):

| Status | Pattern | Description |
|--------|---------|-------------|
| **PROCESSING** | Footer `esc to cancel` (or a braille spinner line, e.g. `⣽ Working...`) | Response streaming or tool executing |
| **IDLE** | Footer `? for shortcuts`, no turn delivered yet | Ready for first input |
| **COMPLETED** | Footer `? for shortcuts`, ≥1 turn delivered | Turn finished |
| **WAITING_USER_ANSWER** | Approval / picker prompt (`[y/n]`, `↑/↓ navigate`, …) | Blocked on user input |
| **ERROR** | `Error:`, `panic:`, `Traceback` patterns | Error detected |

The TUI footer is identical for IDLE and COMPLETED, so the two are split on an internal turn counter (`mark_input_received`), exactly as the Cursor CLI provider does. This preserves the "wait for IDLE before delivering the task" contract right after initialization.

### TUI Structure

```
─────────────────────────────   (full-width U+2500 rule)
> <user question>
  <assistant response>            (2-space indented)
─────────────────────────────   (input box top rule)
>                                 (idle input prompt)
─────────────────────────────   (input box bottom rule)
? for shortcuts            <model>
```

Response extraction returns the text between the last echoed `> <query>` line and the next full-width separator, with TUI chrome filtered out: banner, separators, footer, tips, spinner, tool-call lines (`●  …`), and the collapsed-thought header (`▸ Thought for …`) together with the single auto-generated title line that follows it.

## MCP Servers

`agy` reads MCP servers from `~/.gemini/config/mcp_config.json` (top-level `mcpServers` key). There is no per-invocation MCP config flag, a custom agent's `mcpServers` is not a working alternative, and a separate `HOME` per worker would need its own sign-in, so every CAO worker shares that one file — and every `agy` process starts **every** server listed in it.

The provider therefore writes entries that carry no terminal identity:

- `cao-mcp-server` gets `CAO_TERMINAL_ID` from the `agy` process itself. The backend sets it in the terminal's pane environment, and `agy` passes its environment to its stdio MCP servers (an entry's `env` is merged on top). Each worker's `agy` runs its own copy of the server, and each copy reports its own terminal. A `CAO_TERMINAL_ID` in a profile's server `env` is dropped with a warning.
- Workers whose profiles declare the same server config share one entry, keyed `<server>-<8-hex digest of its config>`. A different config for the same server name gets its own key. The key stays the same across terminals and restarts, so the tool names `agy` shows the model do too.
- Each entry lists the terminals using it under `caoTerminalIds`. `cleanup()` removes its own terminal, and the entry is removed with its last user. Registration also releases terminals that are no longer live. Fields you add to an entry (`disabled`, `disabledTools`, ...) are kept, and your own entries are never touched.

Before #940 the provider wrote one entry per terminal (`<server>-<terminal_id>`) with `CAO_TERMINAL_ID` pinned in its `env`. A worker launched while another was alive also started that worker's entry, so its `send_message`, `handoff` and `assign` calls could reach the supervisor under the other terminal's id. Registration removes entries in that old shape. An `agy` that is already running keeps the servers it started, because `agy` reads the file when it launches.


### Agent-plugin MCP working directory

Its MCP config format documents a working-directory key, so CAO writes an agent plugin's declared `cwd` there directly.
See [Agent Plugins](agent-plugins.md) for the full per-provider table.

## Tool Restrictions

`agy` is in `SOFT_ENFORCEMENT_PROVIDERS`: tool restrictions are advisory. When a profile is not allowed every tool (e.g. the read-only reviewer), the security prompt is appended to the injected system prompt. There is no native hard-block flag.

## Exit

`agy` exits on the `/quit` slash command (also Ctrl-D pressed twice).
