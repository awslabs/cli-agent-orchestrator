# Spike 4 Result
- Verdict: **NEEDS-WORKAROUND**
- Summary: `claude: missing BYPASS_PROMPT_PATTERN; codex: missing IDLE_PROMPT_PATTERN, TRUST_PROMPT_PATTERN, WAITING_PROMPT_PATTERN, CODEX_WELCOME_PATTERN; gemini: blocked`


## claude
- Source: `src\cli_agent_orchestrator\providers\claude_code.py`
- `IDLE_PROMPT_PATTERN` = `[>❯][\s\xa0]`
- `TRUST_PROMPT_PATTERN` = `Yes, I trust this folder`
- `BYPASS_PROMPT_PATTERN` = `Yes, I accept`
- Plain capture length: `504`
- Escaped capture length: `1037`

| Pattern | Plain | `--escapes` |
|---|---|---|
| `IDLE_PROMPT_PATTERN` | `True` | `True` |
| `TRUST_PROMPT_PATTERN` | `True` | `False` |
| `BYPASS_PROMPT_PATTERN` | `False` | `False` |

```text
────────────────────────────────────────────────────────────────────────────────
 Accessing workspace:

 C:\dev\aws-cao

 Quick safety check: Is this a project you created or one you trust? (Like your
  own code, a well-known open source project, or work from your team). If not,
 take a moment to review what's in this folder first.

 Claude Code'll be able to read, edit, and execute files here.

 Security guide

 ❯ 1. Yes, I trust this folder
   2. No, exit

 Enter to confirm · Esc to cancel
```

## codex
- Source: `src\cli_agent_orchestrator\providers\codex.py`
- `IDLE_PROMPT_PATTERN` = `(?:❯|›|codex>)`
- `TRUST_PROMPT_PATTERN` = `allow Codex to work in this folder`
- `WAITING_PROMPT_PATTERN` = `^(?:Approve|Allow)\b.*\b(?:y/n|yes/no|yes|no)\b`
- `CODEX_WELCOME_PATTERN` = `OpenAI Codex`
- Plain capture length: `161`
- Escaped capture length: `232`

| Pattern | Plain | `--escapes` |
|---|---|---|
| `IDLE_PROMPT_PATTERN` | `False` | `False` |
| `TRUST_PROMPT_PATTERN` | `False` | `False` |
| `WAITING_PROMPT_PATTERN` | `False` | `False` |
| `CODEX_WELCOME_PATTERN` | `False` | `False` |

```text
⚠️ Process "codex" in domain "local" didn't exit cleanly
Exited with code 1.
This message is shown because exit_behavior="CloseOnCleanExit"
```

## gemini
- Source: `src\cli_agent_orchestrator\providers\gemini_cli.py`
- `IDLE_PROMPT_PATTERN` = `\*\s+Type your message`
- `WELCOME_BANNER_PATTERN` = `█████████.*██████████`
- `RESPONDING_WITH_PATTERN` = `Responding with\s+\S+`
- Runtime probe: blocked; `gemini` executable unavailable.
## Candidate Regex Patch Notes
```diff
--- a/src/cli_agent_orchestrator/providers/claude_code.py
+++ b/src/cli_agent_orchestrator/providers/claude_code.py
@@
-# Existing WezTerm probe did not match: BYPASS_PROMPT_PATTERN
+# Phase 2: either normalize WezTerm startup text or broaden these regexes: BYPASS_PROMPT_PATTERN
```

```diff
--- a/src/cli_agent_orchestrator/providers/codex.py
+++ b/src/cli_agent_orchestrator/providers/codex.py
@@
-# Existing WezTerm probe did not match: IDLE_PROMPT_PATTERN, TRUST_PROMPT_PATTERN, WAITING_PROMPT_PATTERN, CODEX_WELCOME_PATTERN
+# Phase 2: either normalize WezTerm startup text or broaden these regexes: IDLE_PROMPT_PATTERN, TRUST_PROMPT_PATTERN, WAITING_PROMPT_PATTERN, CODEX_WELCOME_PATTERN
```

## Gemini (re-tested after install)
- Re-test date: `2026-04-24`
- Source: `src\cli_agent_orchestrator\providers\gemini_cli.py`
- `IDLE_PROMPT_PATTERN` = `\*\s+Type your message`
- `WELCOME_BANNER_PATTERN` = `█████████.*██████████`
- `RESPONDING_WITH_PATTERN` = `Responding with\s+\S+`
- Runtime probe: still blocked; `gemini` is not available from PowerShell, `where.exe`, or `bash`.

```text
PowerShell: gemini --version
  The term 'gemini' is not recognized as a name of a cmdlet, function, script file, or executable program.

PowerShell: where.exe gemini
  INFO: Could not find files for the given pattern(s).

bash: command -v gemini
  <no output>
```

