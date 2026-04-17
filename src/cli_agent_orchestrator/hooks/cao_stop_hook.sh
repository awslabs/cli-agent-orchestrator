#!/bin/bash
# CAO Stop Hook — fires every N human messages, triggers agent self-save
# Installed to ~/.aws/cli-agent-orchestrator/hooks/ by cao-server startup.
# Registered as a Claude Code "Stop" hook and Codex "stop" hook.

HOOK_DIR="${HOME}/.aws/cli-agent-orchestrator/hooks"
HOOK_FLAG="${HOOK_DIR}/.cao_stop_hook_active_${CAO_TERMINAL_ID}"

# Recursion guard: if flag file exists, this is a re-entrant call
# triggered by the save response itself. Exit silently — unless the
# flag is stale (older than 5 minutes), which indicates a prior crash.
if [ -f "$HOOK_FLAG" ]; then
  if [ "$(uname)" = "Darwin" ]; then
    FLAG_AGE=$(( $(date +%s) - $(stat -f %m "$HOOK_FLAG" 2>/dev/null || echo 0) ))
  else
    FLAG_AGE=$(( $(date +%s) - $(stat -c %Y "$HOOK_FLAG" 2>/dev/null || echo 0) ))
  fi
  if [ "$FLAG_AGE" -lt 300 ]; then
    exit 0
  fi
  # Stale flag (>5 min) — previous save likely crashed. Remove and proceed.
  rm -f "$HOOK_FLAG"
fi

# Count human messages in the most recent JSONL transcript.
# Claude Code stores transcripts in ~/.claude/projects/<path>/<uuid>.jsonl
TRANSCRIPT=$(find ~/.claude/projects -name "*.jsonl" -newer /tmp/cao_session_start 2>/dev/null | tail -1)
if [ -z "$TRANSCRIPT" ]; then exit 0; fi
MSG_COUNT=$(grep -c '"type":"human"' "$TRANSCRIPT" 2>/dev/null || echo 0)

# Fire every N=15 messages
N=15
if [ $((MSG_COUNT % N)) -eq 0 ] && [ "$MSG_COUNT" -gt 0 ]; then
  touch "$HOOK_FLAG"
  echo '{"decision":"block","reason":"AUTO-SAVE checkpoint. Save key topics, decisions, quotes, and code from this session to your memory system. Organize into appropriate categories. Use verbatim quotes where possible. Continue conversation after saving."}'
fi
