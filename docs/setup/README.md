# Setup guides an agent can follow

Some CAO features take many careful steps to set up by hand, and most of the ways they go wrong
are silent. A setup guide is a runbook for that case: you ask your agent to "set up X for me",
and it follows the guide step by step. Every step ends with a check, so a failure shows up at
the step that caused it. The guide names the steps that need your OK and the steps only you may
do (logins, keys, consent prompts, anything that costs money). Each guide is also plain enough
for you to read along.

A guide covers setup only. For what a feature is and how it behaves, it links the feature's own
doc.

- **Writing a guide?** Start from the [template](TEMPLATE.md), and add a row below.
- **Steps every guide shares** (back up settings, find the server's Python, check an add-on is
  in uv's receipt, restart and wait for health, reconnect MCP) are on one page:
  [Shared setup snippets](snippets.md).

The plan for the whole set is in issue
[#1516](https://github.com/awslabs/cli-agent-orchestrator/issues/1516).

## Status

- **Available**: the guide exists and is ready to follow.
- **Planned**: on the roadmap; the guide doesn't exist yet.

New guides are each followed end to end once, on Linux, before they're listed as available.
macOS is best effort unless a guide says otherwise.

## Available guides

These two predate this folder and keep their paths, so existing links still work.

| Guide | Feature | What it sets up | Status |
| --- | --- | --- | --- |
| [Set up ephemeral agents and a decider](../ephemeral-agents-agent-setup.md) | `ephemeral-agents` | Single-use helper agents, and a decider that picks their model size | Available |
| [Set up a fresh CAO box on EC2](../../examples/remote/ec2/AGENT-SETUP.md) | `remote-ec2` | One always-on CAO box in AWS, reached from your laptop over SSM. Includes the optional Telegram relay ([`telegram/`](../../examples/remote/ec2/telegram/README.md)) | Available |

## Planned guides

Listed in roadmap order. File names are the planned ones, so they aren't links yet.

| Planned guide | File | What it will cover | Status |
| --- | --- | --- | --- |
| Install and first launch | `install.md` | Python, uv and tmux 3.3+; `uv tool install`; start `cao-server` and check `/health`; the `cao` on `PATH` belongs to that server; a first supervisor with an explicit `--provider` | Planned |
| Providers | `providers.md` | The shared provider steps (install, log in outside CAO, first run, `PATH`, CAO's MCP tools), then Claude Code, Codex and Kiro v2 first; Hermes, Kimi and OpenCode next; then Copilot, Cursor, Antigravity, Qwen, MiniMax, OMP and Grok | Planned |
| Headless CI and GitHub Actions | `headless-ci.md` | Runner prerequisites, credentials as CI secrets (or the `mock_cli` provider), wait for `/health`, poll with `--json`, clean up, exit codes | Planned |
| Kiro CLI v3 engine | `kiro-v3.md` | The supported `kiro-cli` versions, workspace `.kiro/` checks, `cao install --engine v3`, and each refusal with its fix | Planned |
| Workflows: first run and plan approval | `workflows.md` | A headless provider, create from a draft, the discovery run, your approval, the real run, the result | Planned |
| Agent profiles, tool restrictions and settings | `profiles.md` | Install and verify a profile per provider, `role` or `allowedTools`, which providers enforce, writing settings safely | Planned |
| MCP Apps, AG-UI and outside MCP hosts | `mcp-apps.md` | Turning the surfaces on, host config for `cao-mcp-server` and `cao-ops-mcp-server`, the AG-UI flag, CORS and the `agui` extra | Planned |
| Control-plane auth and network | `auth.md` | Local-token and IdP modes, the web UI token, widening the allowed hosts and origins only as needed | Planned |
| Server add-ons and surviving `cao update` | `add-ons.md` | Event plugins and extras in the server's Python, recorded in uv's receipt; agent plugins; re-checking after `cao update` | Planned |
| Obsidian vault | `obsidian-vault.md` | The vault root and mappings, the managed folder, `memory.vault`, scan and reconcile, undo | Planned |
| OpenTelemetry | `otel.md` | The `otel` extra in the server's Python, the enable and exporter variables, a local collector, one span | Planned |
| Self-learning | `self-learning.md` | The flags, the supervisor profile lines, the retrospector, outcomes, a promotion dry run | Planned |
| herdr backends | `herdr.md` | The herdr TUI and herdr-app, pinned installs, the backend setting, health checks, switching back to tmux | Planned |
| Rust TUI | `tui.md` | Whether the install has the `cao tui` binary, and building it with `cargo` if not | Planned |
| Multi-machine setups | to be named | The fleet coordinator, EKS elastic workers, the self-hosted decision model and the super-agent kit, each with a cost OK and a teardown | Planned |

## Front matter

Every guide starts with YAML front matter, so tools can list guides and check them:

```yaml
---
feature: <short-id>          # the Feature column above
title: Set up <feature>
applies_to: <the commands and keys the guide's preflight probes for>
user_steps: [<step numbers only the user may do>]
consent_before: [<step numbers that need the user's OK first>]
touches: [<what it changes: settings.json, the server's Python, provider config, cloud>]
undo: yes
---
```

The [template](TEMPLATE.md) explains each key.
