# Pi Provider Design

## Goal

Add first-party support for the Pi coding agent to CLI Agent Orchestrator (CAO), including
attachable tmux sessions, profile prompts and models, hard restrictions for Pi's built-in
tools, reliable status and final-response reporting, and the CAO MCP orchestration tools.

## Verified target

- Pi executable: `pi` resolved from `PATH`; do not hard-code an installation path.
- Verified local version: `@earendil-works/pi-coding-agent` 0.84.1.
- Interactive mode uses a regular inline TUI by default and accepts bracketed-paste input.
- Pi supports `--session-id`, `--session-dir`, `--append-system-prompt`, `--model`,
  `--tools`, `--exclude-tools`, `--extension`, `--no-extensions`, and `--no-approve`.
- Pi extensions can register tools dynamically and receive `agent_start`, `message_end`,
  `agent_settled`, and session lifecycle events.
- Pi does not provide native MCP support or a sandbox.

## Architecture

`PiProvider` launches Pi's regular TUI in the existing terminal backend. It explicitly loads
a bundled CAO Pi extension while disabling ambient extension discovery. The extension owns
two integrations:

1. It records lifecycle state and the latest assistant text atomically in a CAO-owned state
   file. `PiProvider` uses that file as the authoritative status and extraction source, with
   conservative TUI parsing only for startup and failure fallback.
2. It starts a bundled Python JSONL proxy. The proxy uses CAO's existing official Python MCP
   SDK dependency to connect to profile-declared stdio MCP servers, list tools, and call them.
   The extension registers those schemas as native Pi tools. Duplicate tool names fail closed.

The provider writes its prompt, resolved MCP configuration, state, and session data under
CAO's own data directory. It does not install Pi packages or modify `~/.pi`.

## Launch contract

The provider launches a shell-escaped command equivalent to:

```text
env \
  CAO_PI_STATE_FILE=<private-state-file> \
  CAO_PI_MCP_CONFIG=<private-resolved-config> \
  CAO_PI_BRIDGE_PYTHON=<CAO interpreter> \
  pi --tui-mode regular --no-approve \
     --no-extensions --extension <bundled-extension> \
     --no-skills --no-prompt-templates \
     --session-id <terminal-id> --session-dir <CAO session directory> \
     --append-system-prompt <private-prompt-file> \
     [--model <model>] [--exclude-tools <native-denylist>]
```

Pi continues to load repository context files such as `AGENTS.md`; `--no-approve` prevents
unapproved project-local Pi packages, settings, and extensions from being loaded.

## Tool policy

CAO vocabulary maps to Pi built-ins as follows:

| CAO capability | Pi tools |
|---|---|
| `execute_bash` | `bash` |
| `fs_read` | `read` |
| `fs_write` | `edit`, `write` |
| `fs_list` | `grep`, `find`, `ls` |
| `fs_*` | `read`, `edit`, `write`, `grep`, `find`, `ls` |

Pi has no built-in `web_fetch`. CAO passes the computed native denylist through
`--exclude-tools`; Pi therefore belongs to the hard-enforcement category for its built-in
tools. MCP restriction semantics remain consistent with CAO's current all-or-nothing server
marker limitation.

## Proxy protocol

The extension and proxy communicate over newline-delimited JSON on the proxy's stdin/stdout.
Every request has a string `id` and one of these shapes:

```json
{"id":"1","type":"list_tools"}
{"id":"2","type":"call_tool","server":"cao-mcp-server","name":"handoff","arguments":{}}
{"id":"3","type":"shutdown"}
```

Responses preserve the request ID:

```json
{"id":"1","ok":true,"result":{"tools":[]}}
{"id":"2","ok":false,"error":"safe error text"}
```

The proxy merges each server's declared environment with the inherited process environment,
injects `CAO_TERMINAL_ID`, supports command-based stdio servers only in the first version,
uses the server's `requestTimeoutMs` or CAO's 1,200,000 ms orchestration default, and never
writes protocol diagnostics to stdout.

## State contract

The extension atomically writes a JSON object containing:

```json
{
  "status":"idle|processing|completed|error",
  "lastAssistantText":"",
  "error":"",
  "updatedAt":"ISO-8601 timestamp"
}
```

`agent_start` sets `processing`; the latest assistant `message_end` caches text; and
`agent_settled` sets `completed`. Bridge startup failures set `error` and make provider
initialization fail. `PiProvider.mark_input_received()` sets an in-memory processing guard so
there is no idle race before Pi emits its next frame.

## Security and lifecycle

- All generated files and directories are private to the current user (`0700` directories,
  `0600` files).
- Commands and paths are shell-quoted.
- The bridge config contains resolved commands and explicit environment only; errors must not
  serialize inherited environment values or credentials.
- Ambient Pi extensions are disabled. Only the bundled CAO extension is explicitly loaded.
- Pi runs with the user's OS permissions. CAO must not describe project trust or tool flags as
  a filesystem/container sandbox.
- `exit_cli()` returns `C-d`, which CAO delivers as a tmux special key.
- Provider cleanup terminates the bridge through Pi shutdown and removes temporary prompt,
  bridge-config, and state files. CAO-owned Pi session data follows CAO terminal retention.

## Acceptance criteria

1. `cao launch --agents developer --provider pi` reaches `IDLE` with the installed Pi CLI.
2. A prompt moves through `PROCESSING` to `COMPLETED`, and `get_output(LAST)` returns the exact
   last assistant text without terminal chrome.
3. Supervisor and reviewer profiles cannot invoke disallowed Pi built-in tools; developer
   profiles retain their allowed tools.
4. Pi sees and can call `assign`, `handoff`, and `send_message` from `cao-mcp-server`.
5. Provider unit tests, proxy protocol tests, registration tests, package-asset checks, and Pi
   orchestration E2E coverage pass.
6. Canonical provider, profile, skills, tool-restriction, and configuration documentation is
   updated in the same change.
