#!/bin/bash
# CAO PreCompact Hook — always fires before context compression
# Installed to ~/.aws/cli-agent-orchestrator/hooks/ by cao-server startup.
# Registered as a Claude Code "PreCompact" hook.

echo '{"decision":"block","reason":"EMERGENCY SAVE before context compression. Save all key findings, decisions, and facts via memory_store before compaction summarizes and loses detail. This is your last chance before the context window shrinks."}'
