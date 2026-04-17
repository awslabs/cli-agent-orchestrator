#!/bin/bash
# CAO Kiro UserPromptSubmit Hook — injects a memory save reminder every N prompts.
# Kiro adds STDOUT to the conversation context alongside the user's prompt.
#
# Input (stdin JSON): hook_event_name, cwd, session_id, prompt
# Requires: CAO_TERMINAL_ID set in the tmux session environment.

HOOK_DIR="${HOME}/.aws/cli-agent-orchestrator/hooks"
N=15

if [ -z "$CAO_TERMINAL_ID" ]; then
  exit 0
fi

COUNTER_FILE="${HOOK_DIR}/.prompt_count_${CAO_TERMINAL_ID}"

# Read and increment counter
COUNT=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)
COUNT=$((COUNT + 1))
echo "$COUNT" > "$COUNTER_FILE"

if [ $((COUNT % N)) -eq 0 ]; then
  echo "MEMORY CHECKPOINT: Before responding, save any key findings, decisions, code patterns, or user preferences from this session using memory_store. Then continue with your response."
fi

exit 0
