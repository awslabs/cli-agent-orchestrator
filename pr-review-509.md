# PR #509 Review

PR: https://github.com/awslabs/cli-agent-orchestrator/pull/509  
Reviewed head: `5d8d796`  
Verdict: do not approve yet

## Confirmed Finding

[P1] Antigravity concurrent launches can still cross-wire `CAO_TERMINAL_ID`.

File: `src/cli_agent_orchestrator/providers/antigravity_cli.py`

The PR correctly serializes the `mcp_config.json` read-modify-write with `_MCP_CONFIG_WRITE_LOCK`, but the critical section ends before the `agy` process is started. The sequence is:

1. `_build_agy_command()` writes `~/.gemini/config/mcp_config.json` with this provider instance's `CAO_TERMINAL_ID`.
2. The lock is released and `_build_agy_command()` returns.
3. `initialize()` later calls `get_backend().send_keys(..., command)` to start `agy`.
4. `agy` reads the fixed shared config file at process startup.

With concurrent Antigravity initialization, another provider can overwrite the same `cao-mcp-server` entry between steps 2 and 4. The first `agy` process can then start with the second terminal's `CAO_TERMINAL_ID`, so MCP calls from worker A are attributed to worker B. That breaks core CAO routing for `assign`, `handoff`, and callback flows.

This matters more after this PR because converting startup handling to real async removes the previous event-loop blocking that mostly serialized provider initialization. `terminal_service.create_terminal()` also explicitly allows deferred init tasks to run concurrently for parallel assigns.

## Evidence

On PR head, I reproduced the shared-config overwrite with two Antigravity providers using the same MCP server name:

```text
after first build: terminal-a
after second build: terminal-b
```

If provider A sends `agy` after provider B's write, provider A's process reads `terminal-b` from the shared file.

The new lock prevents torn/lost JSON writes, but not this write-before-consume race.

## Suggested Fix

Prefer a per-terminal/per-launch MCP config if Antigravity supports it.

If Antigravity only supports the fixed global file, serialize the launch window, not just the JSON write. The lock needs to cover the config write plus the point where `agy` has definitely consumed the config. Holding it only through `send_keys` may still be racy unless we can prove `agy` reads the file synchronously before another launch can overwrite it.

Add a regression test for two concurrent Antigravity initializations with MCP enabled, asserting each launched process observes its own `CAO_TERMINAL_ID`.

## Suggested PR Comment

```md
Thanks for making the startup prompt handlers nonblocking. I double-checked the Antigravity MCP config path, and I still think there is a blocking race here.

The new `_MCP_CONFIG_WRITE_LOCK` serializes the `mcp_config.json` read-modify-write, but it is released before `initialize()` sends the `agy` command. Since `agy` reads `~/.gemini/config/mcp_config.json` at process startup, another concurrent Antigravity init can overwrite the same `cao-mcp-server` entry between the first provider's config write and the first `agy` process reading it.

I reproduced the sequence on PR head with two providers:

```text
after first build: terminal-a
after second build: terminal-b
```

So if provider A starts after provider B's write, A's `cao-mcp-server` gets `CAO_TERMINAL_ID=terminal-b`. That cross-wires MCP routing for assign/handoff/callback flows.

Suggested fix: use a per-terminal/per-launch MCP config if `agy` supports it. If Antigravity only supports the fixed global config, serialize the whole launch window through the point where `agy` has definitely consumed the config, not just the JSON write. Please also add a regression for two concurrent Antigravity inits with MCP enabled.
```

## Validation

Focused provider tests passed on PR head:

```bash
uv run pytest test/providers/test_startup_handler_nonblocking.py test/providers/test_startup_prompt_idle_gap.py test/providers/test_copilot_cli_unit.py test/providers/test_antigravity_cli_unit.py test/providers/test_kimi_cli_unit.py -q
```

Result: `207 passed`.
