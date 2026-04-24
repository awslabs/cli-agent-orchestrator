# Spike 2b Result

- Verdict: **NEEDS-WORKAROUND**
- Goal status: `launch fixed, raw send-text submission still unresolved`
- Working launch command:
  ```powershell
  & 'C:\Users\marc\Downloads\WezTerm-windows-20260331-040028-577474d8\wezterm.exe' cli spawn --new-window --cwd C:\dev\aws-cao -- C:\Users\marc\scoop\apps\nodejs-lts\current\bin\codex.cmd -c hooks=[] --yolo --no-alt-screen --disable shell_snapshot
  ```
- TUI-ready latency: `2319 ms avg` across `1755 / 2469 / 2734 ms`
- Send-text verdict: `neither`

## What Worked
- The pane stayed alive and rendered the Codex TUI when launched via the Windows shim `codex.cmd`.
- CAO's tmux flags were still necessary: `--yolo --no-alt-screen --disable shell_snapshot`.
- A one-shot config override `-c hooks=[]` was also necessary on this machine because interactive Codex rejected the local `hooks` config schema during startup.

## Why Naive Spawn Exited
- `wezterm cli spawn --new-window -- codex` launched inside Marc's default WezTerm shell domain, which is `bash` in a Linux-style environment for this window.
- In that shell, `codex` resolved to `/mnt/c/.../codex`, then aborted with:
  `Error: Missing optional dependency @openai/codex-linux-arm64`
- When forced onto the Windows Codex shim, startup progressed but interactive Codex still aborted unless `-c hooks=[]` was added, due to:
  `invalid type: map, expected a sequence in hooks`

## Send-Text Probe
- Mode A: `wezterm cli send-text --pane-id <ID> --no-paste -- '/help\n'`
- Mode B: `wezterm cli send-text --pane-id <ID> -- '/help\n'`
- Result: both modes inserted text into Codex's composer, but neither mode visibly submitted the message or produced command output.
- Fallback text prompts behaved the same way: the prompt text appeared after `›`, but Codex did not execute it within the observation window.

```text
[A --no-paste]
› /help
  gpt-5.4 default · C:\dev\aws-cao

[B default paste]
› /help
  gpt-5.4 default · C:\dev\aws-cao
```

## Evidence
### Failing naive launch in WezTerm shell domain
```text
file:///mnt/c/Users/marc/scoop/persist/nodejs-lts/bin/node_modules/@openai/codex/bin/codex.js:100
Error: Missing optional dependency @openai/codex-linux-arm64. Reinstall Codex: npm install -g @openai/codex@latest
```

### Successful TUI launch with explicit Windows Codex
```text
╭─────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.124.0)              │
│ model:       gpt-5.4   /model to change │
│ directory:   C:\dev\aws-cao             │
│ permissions: YOLO mode                  │
╰─────────────────────────────────────────╯

⚠ failed to parse hooks config C:\Users\marc\.codex\hooks.json: expected value
⚠ failed to parse TOML hooks in C:\Users\marc\.codex\config.toml: invalid type: map, expected a sequence

› Summarize recent commits
  gpt-5.4 default · C:\dev\aws-cao
```

## WezTerm Backend Construction Diff
```diff
--- a/src/cli_agent_orchestrator/providers/codex.py
+++ b/src/cli_agent_orchestrator/multiplexers/wezterm.py
@@
- command = shlex.join(["codex", "--yolo", "--no-alt-screen", "--disable", "shell_snapshot"])
+ spawn_argv = [
+   resolve_windows_codex(),  # prefer codex.cmd on Windows; avoid bash/WSL shim resolution
+   "-c",
+   "hooks=[]",               # local interactive Codex rejected ~/.codex hooks schema on marcwin
+   "--yolo",
+   "--no-alt-screen",
+   "--disable",
+   "shell_snapshot",
+ ]
+ wezterm cli spawn --new-window --cwd <workdir> -- <spawn_argv...>
```

## Recommendation
- For WezTerm on Windows, do not rely on shell-resolved `codex`.
- Resolve the executable explicitly to the Windows shim (`codex.cmd`) before calling `wezterm cli spawn`.
- Carry forward CAO's existing flags unchanged.
- Keep a provider/backend-specific workaround slot for local Codex config overrides, because interactive startup can fail before the TUI becomes reachable.
