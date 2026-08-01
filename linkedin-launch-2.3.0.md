# LinkedIn Post - CAO v2.3.0

## Recommended Post

🚀 CAO v2.3.0 is live: introducing **CAO Harness** for durable, observable multi-agent CLI workflows.

As coding agents become more capable, the challenge shifts from launching one assistant to coordinating a team across long-running work, scheduled runs, shared memory, inbox delivery, and operator UIs.

Cli-agent-orchestrator (CAO) is an open-source control plane for terminal-based AI coding agents. It runs agents in isolated tmux/herdr terminal sessions and lets supervisors coordinate work through MCP tools: assign, handoff, and send_message.

What's new in 2.3.0:

🧰 **CAO Harness**  
A shared execution layer for multi-agent teams: workflow, schedule, memory, skills, plugins, tmux/herdr, CAO MCP Apps, Web UI, CLI, MCP, and HTTP APIs.

🧭 **Durable workflow execution**  
`cao workflow` adds script-tier orchestration with run journals, resumable execution, workflow linting, and run-step environment guards.

⏰ **CAO Schedule**  
`cao flow` is now `cao schedule`, making recurring agent flows clearer alongside the new workflow engine.

🧠 **Memory federation and portability**  
`FEDERATED` scope, OKF export/import, `cao memory heal`, and Web UI memory management make CAO memory easier to share, repair, and move.

📱 **CAO MCP Apps**  
Host-rendered operator views for MCP App-capable clients: dashboard, agent detail, and event stream via `ui://cao/*`.

⚡ **Event-driven execution stack**  
Event-driven inbox delivery, herdr backend support, pyte status detection, and provider hardening reduce stuck handoffs and improve observability.

🌐 **Fleet examples**  
Cross-node fleet coordinator and web control panel examples show how to manage many CAO nodes from one place.

🤝 **Provider updates**  
New and expanded support across Hermes Agent, Cursor CLI, Antigravity CLI, Kimi CLI, plus Kiro CLI 2.11 and status-detection fixes.

⭐ CAO has crossed **900+ GitHub stars**, **55+ contributors**, and is now a **top 40 awslabs open-source project by stars**.

Thanks to everyone contributing code, filing issues, testing providers, and pushing the design forward.

Release notes: https://github.com/awslabs/cli-agent-orchestrator/releases/tag/v2.3.0

#AI #AgenticAI #DeveloperTools #OpenSource #MultiAgentSystems

## Shorter Version

🚀 CAO v2.3.0 is live: **CAO Harness** for durable, observable multi-agent CLI workflows.

What's new:

🧰 CAO Harness as the shared execution layer  
🧭 `cao workflow` with resumable runs and journals  
⏰ `cao schedule` for recurring agent flows  
🧠 federated memory, OKF portability, and `cao memory heal`  
📱 CAO MCP Apps: dashboard, agent detail, event stream  
⚡ event-driven inbox delivery + herdr backend  
🌐 cross-node fleet coordinator examples  
🤝 Hermes Agent, Cursor CLI, Antigravity CLI, Kimi CLI, and Kiro CLI 2.11 hardening

⭐ 900+ GitHub stars  
👥 55+ contributors  
🏆 Top 40 awslabs open-source project by stars

Release notes: https://github.com/awslabs/cli-agent-orchestrator/releases/tag/v2.3.0

#AI #AgenticAI #DeveloperTools #OpenSource

## X Posts

### Recommended Thread

1/7  
🚀 CAO v2.3.0 is live.

Introducing CAO Harness: the shared execution layer for durable, observable multi-agent CLI workflows.

#AI #AgenticAI #OpenSource

2/7  
The problem is shifting.

Not "can one coding agent help?"

But "can a team of agents coordinate long-running engineering work?"

CAO is built for that.

3/7  
🧰 CAO Harness brings the operating layer together:

🧭 Workflow  
⏰ Schedule  
🧠 Memory  
🖥️ tmux + herdr  
📱 CAO MCP Apps  
🌐 Fleet examples  
🧩 Skills + Plugins

4/7  
🧭 cao workflow

Script-tier orchestration for multi-agent runs:

✅ run journals  
✅ resumable execution  
✅ workflow linting  
✅ run-step environment guards

5/7  
🧠 Memory gets more portable.

v2.3.0 adds:

✅ FEDERATED scope  
✅ OKF export/import  
✅ cao memory heal  
✅ Web UI memory management

6/7  
📱 CAO MCP Apps

Host-rendered operator views for MCP App-capable clients:

✅ dashboard  
✅ agent detail  
✅ event stream via `ui://cao/*`

Plus event-driven inbox delivery and herdr backend support.

7/7  
⭐ CAO has crossed:

📈 900+ GitHub stars  
👥 55+ contributors  
🏆 Top 40 awslabs open-source project by stars

Release notes: https://github.com/awslabs/cli-agent-orchestrator/releases/tag/v2.3.0

### Single X Post

🚀 CAO v2.3.0 is live.

Introducing CAO Harness for durable, observable multi-agent CLI workflows.

🧭 cao workflow  
⏰ cao schedule  
🧠 federated memory + OKF  
📱 CAO MCP Apps  
🖥️ tmux + herdr  
🌐 fleet examples

⭐ 900+ stars · 55+ contributors · top 40 awslabs

## Notes

- The publishable LinkedIn post is intentionally concise and emoji-led; keep detailed release bullets out of the main copy.
- The X thread is split for readability and should stay under the X character limit per post.
- The hook now formally introduces CAO Harness. The strongest v2.3.0 proof point under that umbrella is durable workflow execution.
- "CAO Harness" is framed from the Sydney meetup deck as the portable layer around every agent: workflow, schedule, centralized skills, persistent memory, plugins, tmux/herdr, and agent profiles.
- Be precise on naming: the current shipped cross-node layer is documented as fleet/coordinator examples, not a confirmed `cao cluster` CLI command.
- Be precise on naming: CAO MCP Apps is a host-rendered operator surface, not the same thing as a top-level `cao fleet` command.
- Be precise on release scope: `cao profile` exists on current main, but the official v2.3.0 release body says profile lifecycle is following after 2.3.0. In the post, it is presented as a CAO Harness concept rather than a v2.3.0 shipped bullet.
- Public GitHub metrics checked on 2026-07-17: 901 stars, 172 forks, #38 among awslabs repositories by stars. Contributor count is project-reported as 55+.
- Traffic metrics were not accessible via the current GitHub token, so do not include visit counts unless you have verified analytics.
- Breaking change to mention only if useful in a technical audience post: Amazon Q CLI and Gemini CLI providers were removed; users should migrate pinned profiles to `kiro_cli` or `antigravity_cli`.
