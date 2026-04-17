#!/bin/bash
# CAO Kiro AgentSpawn Hook — injects CAO memory context into agent context at spawn time.
# Kiro adds STDOUT to the agent's context before the first user message.
#
# Input (stdin JSON): hook_event_name, cwd, session_id
# Requires: CAO_TERMINAL_ID set in the tmux session environment.

CAO_API="http://127.0.0.1:9889"

if [ -z "$CAO_TERMINAL_ID" ]; then
  exit 0
fi

# Fetch memory context from CAO API (plain text)
CONTEXT=$(curl -sf "${CAO_API}/terminals/${CAO_TERMINAL_ID}/memory-context" 2>/dev/null)

if [ -n "$CONTEXT" ]; then
  echo "$CONTEXT"
fi

exit 0
