# Working with tmux Sessions

All CAO agent sessions run in tmux. You can attach directly to a session to watch or interact with agents in real time.

## Useful commands

```bash
# List all sessions
tmux list-sessions

# Attach to a session
tmux attach -t <session-name>

# Detach from session (inside tmux)
Ctrl+b, then d

# Switch between windows (inside tmux)
Ctrl+b, then n          # Next window
Ctrl+b, then p          # Previous window
Ctrl+b, then <number>   # Go to window number (0-9)
Ctrl+b, then w          # List all windows (interactive selector)

# Delete a session (cleanly, via CAO)
cao shutdown --session <session-name>
```

## Interactive window selector

**List all windows (Ctrl+b, w):**

![Tmux Window Selector](./assets/tmux_all_windows.png)

## Forwarding env vars to spawned agents

By default, only a tight allowlist of env vars (`HOME`, `PATH`, `SHELL`, plus `CAO_*` / `KIRO_*` / `MISE_*` / `AWS_*` prefixes) reaches agents spawned inside tmux. The filter keeps the `tmux new-session -e` argv under the kernel limit and prevents nested-session loops when CAO itself runs inside a provider.

To forward additional vars to **the supervisor and every worker spawned later in the same session** (via `assign` / `handoff` / the web UI), pass `--env KEY=VALUE` to `cao launch`:

```bash
cao launch --agents code_supervisor \
  --env MNEMOSYNE_DIR=/root/mnemosyne \
  --env ISAAC_CHANNEL=room:engineering
```

The flag is repeatable. Values travel in the request body, not the URL, so secrets do not land in cao-server's HTTP access log.

Rejected at the CLI boundary:

- Keys matching `CLAUDE` / `CODEX_` / `__MISE_` (reserved for provider auth — the 6 `CLAUDE_CODE_USE_*` / `CLAUDE_CODE_SKIP_*` auth flags are explicitly allowlisted).
- Keys that decide what the pane runs before the provider CLI's first tool call: the `LD_*` and `DYLD_*` loader families and `GCONV_PATH`; `PATH`, `HOME` and `SHELL`, which pick the program and rc files the shell starts with; the shell hooks `BASH_ENV`, `ENV`, `ZDOTDIR`, `PROMPT_COMMAND`, `PS0`, `PS1`, `PS2`, `PS4`; the interpreter hooks `PYTHONSTARTUP`, `PYTHONPATH`, `PYTHONHOME`, `PYTHONUSERBASE`, `PERL5OPT`, `PERL5LIB`, `PERLLIB`, `NODE_OPTIONS`, `NODE_PATH`, `RUBYOPT`, `RUBYLIB`; and `AWS_CONFIG_FILE` / `AWS_SHARED_CREDENTIALS_FILE`, which the AWS SDK reads when the provider CLI authenticates at startup (a profile's `credential_process` runs the command the file names). A value in any of these would execute as the operator before the agent's first tool call, so there is no allowlist. This is a denylist and will trail new providers and startup hooks. Variables that only act when the agent itself runs a program (`GIT_SSH_COMMAND`, `EDITOR`, `PAGER`) are not refused: whether the agent may run programs is the tool policy's decision.
- Keys outside `[A-Za-z_][A-Za-z0-9_]*` (non-POSIX names break the shell).
- Values ≥ 2048 bytes (per-var cap that keeps the tmux argv under the kernel limit — see PR #246).

Forwarded vars are held in process memory on cao-server and dropped when the session is deleted; restarting cao-server wipes them.

### From the ops-MCP `launch_session` tool

An external agent driving CAO through the `cao-ops` MCP server forwards the same
vars via an `env_vars` mapping on `launch_session` — the identical mechanism,
validation, and request-body delivery as `cao launch --env`:

```python
launch_session(
    agent_profile="code_supervisor",
    env_vars={
        "MNEMOSYNE_DIR": "/root/mnemosyne",
        "ISAAC_CHANNEL": "room:engineering",
    },
)
```

The same rules are enforced at the tool boundary — blocked
`CLAUDE` / `CODEX_` / `__MISE_` prefixes (with the 6 `CLAUDE_CODE_USE_*` /
`CLAUDE_CODE_SKIP_*` flags allowlisted), the loader/shell/interpreter startup
keys above, non-POSIX keys, and values ≥ 2048 bytes — so an entry the server
would refuse fails the tool call loudly instead of vanishing. The CLI, the
ops-MCP tool and `POST /sessions` itself share one validator
(`utils/forwarded_env.py`) so the paths cannot drift; a direct HTTP caller gets
a 422 naming the key.

## Notes

- CAO session names are automatically prefixed with `cao-`. Use the prefixed name (e.g. `cao-my-task`) when referencing a session in `tmux attach`, `cao session send`, or `cao shutdown`. Teardown never leaves that namespace: `cao shutdown --session my-task` and `DELETE /sessions/my-task` act on `cao-my-task`, and CAO refuses to kill a tmux session whose name lacks the prefix, so a personal session that shares the operator's tmux server is out of reach.
- Prefer `cao shutdown` over `tmux kill-session`: `cao shutdown` exits each provider cleanly before tearing down the tmux session, which avoids leaked CLI processes.
