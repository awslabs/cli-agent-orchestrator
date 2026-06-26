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

`agy` reads MCP servers from `~/.gemini/config/mcp_config.json` (top-level `mcpServers` key). The provider merges the agent profile's `mcpServers` into that file at launch — preserving any existing, non-CAO entries — and forwards `CAO_TERMINAL_ID` into each server's env so `cao-mcp-server` can resolve the current terminal for handoff / assign. Entries are removed on `cleanup()`. There is no per-invocation MCP config flag, but CAO serializes launches (initialize waits for the agent to become ready), so each `agy` process reads the config and spawns its MCP subprocess with the correct terminal id before the next terminal writes.

## Tool Restrictions

`agy` is in `SOFT_ENFORCEMENT_PROVIDERS`: tool restrictions are advisory. When a profile is not allowed every tool (e.g. the read-only reviewer), the security prompt is appended to the injected system prompt. There is no native hard-block flag.

## Exit

`agy` exits on the `/quit` slash command (also Ctrl-D pressed twice).
