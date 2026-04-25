#!/bin/bash
# CAO PreCompact Hook — fires before Claude Code context compression.
# Installed to ~/.aws/cli-agent-orchestrator/hooks/ by cao-server startup.
# Registered as a Claude Code "PreCompact" hook.
#
# CONTRACT: This hook MUST NOT cancel compaction. In Claude Code's PreCompact
# hook contract, returning {"decision":"block"} cancels compaction, which
# leaves the agent stuck at the context limit. We return {} (empty no-op)
# so compaction always proceeds.
#
# The save-before-compaction intent is handled non-blockingly by the Phase 2
# U8 in-process flush (see services/terminal_service.py + providers/*.py
# get_context_usage_percentage). That path sends the save reminder via
# send_input when the context threshold is crossed, without relying on a
# blocking hook return value.
echo '{}'
