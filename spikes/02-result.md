# Spike 2 Result

- Verdict: **NEEDS-WORKAROUND**
- Per-CLI verdicts: `claude: neither, codex: neither, gemini: blocked`
- Mode A: `wezterm cli send-text --no-paste -- '/help\n'`
- Mode B: `wezterm cli send-text -- '/help\n'`

## Recommendation
- `claude`: prefer `custom workaround needed`
- `codex`: prefer `custom workaround needed`

## Evidence
### claude
- Status: `fail`
- Accepted mode: `neither`
```text
[A --no-paste]
 Quick safety check: Is this a project you created or one you trust? (Like your
  own code, a well-known open source project, or work from your team). If not,
 take a moment to review what's in this folder first.

 Claude Code'll be able to read, edit, and execute files here.

 Security guide

 ❯ 1. Yes, I trust this folder
   2. No, exit

 Enter to confirm · Esc to cancel

[B default paste]
 Quick safety check: Is this a project you created or one you trust? (Like your
  own code, a well-known open source project, or work from your team). If not,
 take a moment to review what's in this folder first.

 Claude Code'll be able to read, edit, and execute files here.

 Security guide

 ❯ 1. Yes, I trust this folder
   2. No, exit

 Enter to confirm · Esc to cancel
```
### codex
- Status: `fail`
- Accepted mode: `neither`
```text
[A --no-paste]
⚠️ Process "codex" in domain "local" didn't exit cleanly
Exited with code 1.
This message is shown because exit_behavior="CloseOnCleanExit"

[B default paste]
⚠️ Process "codex" in domain "local" didn't exit cleanly
Exited with code 1.
This message is shown because exit_behavior="CloseOnCleanExit"
```
### gemini
- Status: `blocked`
- Accepted mode: `blocked`
```text
command not installed or not on PATH
```

## Environment Notes
- `gemini` could not be tested because the executable is unavailable in this environment.

## Gemini (re-tested after install)
- Re-test date: `2026-04-24`
- Status: `blocked`
- Accepted mode: `blocked`
```text
[PATH checks]
PowerShell: gemini --version
  The term 'gemini' is not recognized as a name of a cmdlet, function, script file, or executable program.

PowerShell: where.exe gemini
  INFO: Could not find files for the given pattern(s).

bash: command -v gemini
  <no output>
```
- Result: the binary is still unavailable on this machine, so spike 2 remains blocked for Gemini.
