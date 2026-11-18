# Qwen Code provider

CAO can launch Qwen Code with `--provider qwen_cli`. This continues the work
in [PR #412](https://github.com/awslabs/cli-agent-orchestrator/pull/412)
by SparkyWen, tracked in
[issue #376](https://github.com/awslabs/cli-agent-orchestrator/issues/376).

## Install and authenticate

Install Qwen Code following its
[official installation guide](https://qwenlm.github.io/qwen-code-docs/en/users/quickstart/).
Use Qwen **0.28 or later** (`--json-file` is required).
Check `qwen --version`, start `qwen` outside CAO, and complete the interactive
login and first-run setup before launching agents. The provider uses the
existing native login; it neither copies credentials nor replaces `HOME`.

Qwen account authentication, including **Qwen Subscription**, and
OpenAI-compatible API authentication are handled by Qwen itself. A Qwen account
subscription and an Alibaba Coding Plan API key are different authentication
paths. An API key is not required when a working native subscription login is
already selected. See the
[Qwen authentication documentation](https://qwenlm.github.io/qwen-code-docs/en/users/configuration/auth/).

The original fixtures were recorded on Qwen 0.19.x. Qwen 0.28 uses a different
final-response marker; the provider recognizes its `◆` final answers and excludes
`∴` thought panels. Native JSON output is used for response extraction.

## Launch

```bash
cao install developer --provider qwen_cli
cao launch --agents developer --provider qwen_cli --auto-approve
```

Use the **tmux** backend. This provider has not been validated on herdr.
A profile can select Qwen directly:

```yaml
---
name: qwen-developer
description: Implement and verify the assigned task
provider: qwen_cli
role: developer
# Optional: use an id available to your selected native authentication.
# model: qwen4-max
mcpServers:
  cao-mcp-server:
    type: stdio
    command: cao-mcp-server
    args: []
---
Implement the assigned task and report changes and verification results.
```

An explicit launch model overrides the profile model. If neither is set, Qwen
uses its native selection. CAO does not change the authentication method or
reasoning settings. Use Qwen's own supported configuration for those settings.

## Profiles, skills, and MCP

CAO appends the role, skill catalog and advisory tool policy with
`--append-system-prompt`; it does not replace Qwen's built-in coding prompt.
Native global Qwen settings and skills remain available. Project context follows
Qwen's configuration; configure the context filenames if your projects use
`AGENTS.md` rather than Qwen's default `QWEN.md`.

Each terminal gets a private, mode-0600 `--mcp-config` file. Server fields are
preserved, optional `args` and `env` are normalized, and `CAO_TERMINAL_ID` is
injected. CAO Agent Plugin **stdio** MCP servers are merged into that file.
Remote MCP configured natively in Qwen remains Qwen's responsibility; remote
transport delivery through CAO's plugin mapper is not declared by this adapter.
Configuration files are removed on cleanup and on failed initialization.

Qwen has a native team `send_message` tool. Its name collides with CAO's MCP
`send_message`; launch excludes the native tool with `--exclude-tools send_message`.
Agents should use `mcp__cao-mcp-server__send_message` for CAO callbacks.

## Status and response handling

Every launch also gets a private mode-0600 `--json-file` journal under
`CAO_HOME_DIR/tmp/qwen/<terminal-id-hash>/` (mode 0700). The durable path lets
a restarted CAO reconnect to a CLI that is still running; it does not restart
the CLI or start another conversation. Final output comes from native assistant text blocks; thinking, tool
results and nested-agent events are excluded. New dispatches checkpoint the
journal before paste, so the previous answer cannot satisfy a new turn. Partial
JSON records wait for a newline; a missing/corrupt journal fails closed. Raw
terminal output is never substituted as the final answer. The journal can contain
private context and is deleted with the terminal; do not publish it as a fixture.

The provider uses the composed terminal screen for readiness, including processing rechecks,
so a stale spinner in the raw append-only log does not keep a finished turn busy.
Keep `CAO_PYTE_STATUS=true` (the default). The literal value `true` is required;
this provider does not change the shared environment-boolean parser.

- A ready composer before the first input is `idle`; after a native user event
  acknowledges the current dispatch and the composer returns, it is `completed`.
- An active `esc to cancel` indicator is `processing`.
- An approval prompt without the normal ready composer is `waiting_user_answer`.
  Phrases such as “Do you want to proceed” in a finished answer are not dialogs.
- Recognized fatal CLI errors are `error`; unrecognized frames remain `unknown`.

Startup polling and terminal calls yield the server's event loop. Theme/workspace
setup retains the existing startup behavior; authentication pickers are never
answered automatically. Authenticate in Qwen before creating CAO terminals.
One Enter submits bracketed-paste input. Response extraction removes tool panels
and picker rows, and rejects thought-only/empty responses instead of returning
raw terminal output as an answer.

## Tool restrictions and limits

The CLI is launched with `--approval-mode yolo`. CAO `allowedTools` restrictions
are **prompt-only**, not an enforced sandbox. A restricted role on this provider
must be treated as having unrestricted tool access. See
[tool restrictions](tool-restrictions.md#provider-enforcement).

Native `qwen --resume` exists, but Qwen session resume is not exposed through
CAO's HTTP resume contract.

## Development checks

```bash
uv sync --locked --all-extras --dev
uv run pytest test/providers/test_qwen_cli_unit.py test/providers/test_qwen_cli_regressions.py -o addopts=
# Use the ONE native auth store; put test conversations in a separate directory.
export QWEN_HOME="${QWEN_HOME:-$HOME/.qwen}"
export QWEN_RUNTIME_DIR="$(mktemp -d)"
uv run pytest test/e2e/ -k QwenCli -m e2e -o addopts=
```

Live tests start a separate CAO server with disposable CAO state. They explicitly
reuse `QWEN_HOME` and isolate conversations/runtime with `QWEN_RUNTIME_DIR`.
Native Qwen configuration (including hooks and MCP) applies. Use an authenticated
account configured for these tests. **Do not copy subscription credentials**:
refresh tokens can rotate, leaving the original store stale; file symlinks are
also rejected by Qwen. No credential contents enter the test fixtures or logs.
