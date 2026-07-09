# Qwen Code Provider

## Overview

The Qwen Code provider enables CAO to work with [Qwen Code](https://github.com/QwenLM/qwen-code) (`qwen`), Alibaba's terminal-native AI coding agent — a fork of Google's Gemini CLI. `qwen` runs as an interactive full-screen Ink TUI that keeps scrollback history in tmux.

## Prerequisites

- **Qwen Code**: Install via `npm install -g @qwen-code/qwen-code` (Node.js 20+).
- **Authentication** (user-managed): `qwen` reads OpenAI-compatible credentials from the environment. Set all three before starting `cao-server`:

  ```bash
  export OPENAI_API_KEY="<your key>"
  export OPENAI_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"   # or your gateway
  export OPENAI_MODEL="qwen3-coder-plus"
  ```

  When all three are present, `qwen` auto-selects `openai` auth and skips the interactive auth dialog. `qwen-oauth` (browser login) also works interactively but **cannot complete headless/in-container**, so it is not suitable for CAO-spawned sessions. The provider never handles credentials itself.
- **tmux 3.2+**

Verify installation:

```bash
qwen --version
```

## Backend Support

The provider currently requires the **tmux** backend (`cao-server --terminal tmux`, the default). It opts into screen-based status detection (`supports_screen_detection = True`), which is driven by the FIFO / pyte pipeline that tmux provides.

The **herdr** backend is **not yet supported**: herdr uses an event inbox and skips the FIFO pipeline for providers it has no native status integration for, so `qwen`'s state is never observed and terminals time out. Generic herdr FIFO support for non-native providers is tracked as a follow-up; until then, run `qwen_cli` on the tmux backend.

## Quick Start

```bash
# Launch with CAO
cao launch --agents developer --provider qwen_cli
```

Set a model in the agent profile (`model:` field) or via `OPENAI_MODEL`. Model names are the ids your endpoint exposes, e.g. `qwen3-coder-plus`, `qwen3-max`.

## Launch Command

```
qwen --approval-mode yolo [--model "<model>"] [--append-system-prompt "<system prompt>"] [--mcp-config "<file>"]
```

- `--approval-mode yolo` auto-approves tool calls so orchestrated (handoff / assign) flows do not block on per-tool approval prompts.
- `--model` selects the model (profile `model:` wins over a constructor override); omit it to use `OPENAI_MODEL`.
- `--append-system-prompt` layers the agent profile's system prompt (+ skill catalog, + security prompt when tool-restricted) on top of qwen's built-in agent prompt. Unlike Gemini CLI's `-i`, this is a true system prompt, so no "acknowledge and wait" guard is needed.
- `--mcp-config` points at a per-terminal MCP config file (see [MCP Servers](#mcp-servers)).

## Status Detection

The provider classifies `qwen` states from the tmux output buffer (footer-anchored, render-stable):

| Status | Pattern | Description |
|--------|---------|-------------|
| **PROCESSING** | Spinner line `esc to cancel` (with an elapsed timer) | Response streaming or tool executing |
| **IDLE** | Ready input box (`YOLO mode (shift + tab to cycle)` / `Type your message or @…` / `? for shortcuts`), no turn delivered yet | Ready for first input |
| **COMPLETED** | Ready input box, ≥1 turn delivered | Turn finished (a transient `✕ [API Error …]` turn also returns here — it is retryable, not a fatal state) |
| **WAITING_USER_ANSWER** | Approval / picker prompt (`Allow execution`, `Do you want to proceed`, `[y/n]`, …) | Blocked on user input |
| **ERROR** | `Error:`, `panic:`, `Traceback` patterns | Hard error (crashed binary) |

The TUI is identical for IDLE and COMPLETED, so the two are split on an internal turn counter (`mark_input_received`), exactly as the Antigravity CLI provider does. This preserves the "wait for IDLE before delivering the task" contract right after initialization.

Because `qwen` redraws the spinner/footer in place, the raw append-only pipe-pane stream keeps a stale `esc to cancel` after a turn ends. The provider therefore opts into pyte rendered-screen detection (`supports_screen_detection = True` / `get_status_from_screen`), which resolves the in-place redraw so only the live frame is classified. Enable it with `CAO_PYTE_STATUS=1`.

### TUI Structure

```
> <user question>
  ● <assistant response>            (filled bullet marks assistant/tool output)
────────────────────────────────   (input box top rule, full-width U+2500)
*   Type your message or @path/to/file
────────────────────────────────   (input box bottom rule)
  ➜ <dir> · <model>
  YOLO mode (shift + tab to cycle)
```

Response extraction returns the text between the last echoed `> <query>` line and the next full-width separator, stripping the assistant bullet (`●`) and filtering TUI chrome (banner, separators, footer, tips, spinner).

## MCP Servers

The provider writes a **per-terminal** MCP config file (`{"mcpServers": {...}}`) to a temp path and passes it via `--mcp-config`. It forwards `CAO_TERMINAL_ID` into each server's `env` so `cao-mcp-server` can resolve the current terminal for handoff / assign. The file is removed on `cleanup()`. A per-instance file (rather than mutating the shared `~/.qwen/settings.json`) avoids cross-terminal write races when many terminals launch concurrently.

## Tool Restrictions

`qwen_cli` is in `SOFT_ENFORCEMENT_PROVIDERS`: tool restrictions are advisory. Under `--approval-mode yolo` there is no native CAO tool denylist, so when a profile is not allowed every tool (e.g. the read-only reviewer), the security prompt is appended to the injected system prompt. There is no native hard-block flag.

## Exit

`qwen` exits on the `/quit` slash command.
