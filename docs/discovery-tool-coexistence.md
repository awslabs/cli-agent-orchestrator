# Discovery Tool Coexistence (first-pass write-up)

**Status: first-pass draft**, written per tedswinyar's request on [issue #432](https://github.com/awslabs/cli-agent-orchestrator/issues/432) (comment, 2026-07-18T00:29:56Z). This documents where things landed between klabulan and tedswinyar on the two design questions tedswinyar raised; it has not yet been reviewed by @anilkmr-a2z, @call-me-ram, or @gutosantos82, who were explicitly asked for objections or alternatives before #433 revises in this direction. Treat this as the starting point for that review, not a settled contract.

## The two topologies

CAO now has two distinct ways for agents to reach each other:

1. **Supervisor hierarchy** — `handoff`, `assign`, `send_message`. Structural: a worker's `caller_id` records who spawned it, and `send_message` uses that (or an explicit `receiver_id`/assign callback) to route replies. This is the shape CAO's `role`/`allowedTools` system, and the built-in `supervisor`/`developer`/`reviewer` roles, were designed around.
2. **Flat peer layer** — `group`, `list_siblings`, `update_metadata` (issue #432). Non-hierarchical: any terminal with a `group` set can discover and message any OTHER terminal sharing a leading prefix of that group, with no supervisor in the loop.

These are genuinely different capabilities with different risk profiles. A profile that should be able to orchestrate workers (topology 1) is not automatically a profile that should be able to see and message arbitrary peers (topology 2), and vice versa. That's the premise behind keeping them separately grantable.

## The opt-in marker

`list_siblings` and `update_metadata` require a new CAO tool-vocabulary token, `discovery`, in the calling terminal's resolved `allowedTools` (or `*`/unrestricted). This is **layered under**, not instead of, the existing `@cao-mcp-server` MCP-server-level gate:

- `@cao-mcp-server` still controls whether the `cao-mcp-server` MCP process is wired into the provider's launch command at all (via the profile's own `mcpServers` frontmatter block). Without it, none of `cao-mcp-server`'s tools — including `list_siblings`/`update_metadata` — are reachable, same as today.
- `discovery` is a second, independent check specifically for the two discovery tools, evaluated when they're called (see "Enforcement mechanism" below). A profile can have `@cao-mcp-server` without `discovery` (orchestration, no peer discovery — this is every existing built-in role's default) or, in principle, `discovery` without the rest of orchestration if some future profile shape wanted that.

None of the built-in roles (`supervisor`, `developer`, `reviewer`) grant `discovery` by default. A profile author adds `"discovery"` to `allowedTools` explicitly:

```yaml
---
name: my_peer_aware_worker
role: developer
allowedTools: ["@builtin", "fs_*", "execute_bash", "web_fetch", "@cao-mcp-server", "discovery"]
---
```

## Enforcement mechanism

CAO's existing tool-restriction system has two enforcement paths:

- **Native, hard blocking** for provider built-in tools (`execute_bash`, `fs_read`, etc.) — `get_disallowed_tools` maps a CAO vocabulary token to concrete provider-native tool names (`Bash`, `Read`, ...) and passes them to the provider's own `--disallowedTools`-style flag. The tool is genuinely unavailable to the model.
- **No existing per-MCP-tool mechanism.** `cao-mcp-server` is one shared FastMCP process; every profile that wires it in gets the same static tool list (`handoff`, `assign`, `send_message`, `list_siblings`, `update_metadata`, `load_skill`, `answer_user_prompt`, ...). There is currently no per-caller filtering of which of *that* server's tools a given connection sees.

Building genuine hard-blocking for `discovery` specifically — hiding `list_siblings`/`update_metadata` from the model entirely for a profile that lacks the marker — would need either splitting discovery into its own MCP server (a second `mcpServers` entry a profile opts into, mirroring how `@cao-mcp-server` itself works) or teaching `get_disallowed_tools` about individual MCP tool names (e.g. Claude Code's `mcp__cao-mcp-server__list_siblings` naming) on providers that support disallowing them. Both are real options for a follow-up; neither is done in this first pass.

What's implemented instead: a **runtime authorization check inside the tool handler** (`_require_discovery_marker` in `mcp_server/server.py`). Both tools are still visible in the MCP tool list regardless of the marker, but calling either without it returns a structured `{"success": False, "error": "..."}` naming the missing `discovery` requirement, rather than performing the discovery/write. This is soft in the sense that the tool is still *offered*; it is hard in the sense that the call cannot succeed without the marker — the model cannot argue its way past it. The check fails closed: if the caller's own `allowed_tools` can't be resolved (network error, unexpected response shape), discovery is denied, not granted.

This mirrors CAO's own precedent for capability enforcement that a provider can't do natively (see `SOFT_ENFORCEMENT_PROVIDERS` for kimi_cli/codex/antigravity_cli, which rely on a prompt-level security constraint rather than provider-native blocking) — a documented, deliberate trade-off, not an oversight, made explicit here so it can be revisited alongside those.

## `group` is organizational, not a security boundary

Independent of the marker discussion: `group`-prefix matching, `discovery`, and session-scoping (below) are all **organizational**, not isolation guarantees. On a default CAO install (auth disabled, localhost trust model), a worker already has local shell access to the CAO API — nothing about `group` or `discovery` changes that. A consumer building a real multi-tenant boundary on top of `group` would be building on an assumption CAO does not provide. See docs/api.md and docs/tool-restrictions.md, where this is now stated explicitly.

## Session scoping (the second design question)

Not strictly a "coexistence" question, but resolved in the same discussion and worth summarizing here since it shapes the same feature surface: `list_siblings` is session-scoped by default (an implicit, non-bypassable filter on the caller's own `tmux_session`, layered on top of the group-prefix match), with server-wide/cross-session discovery available only via an explicit `cross_session=true` opt-in. This closes the failure mode where two unrelated CAO sessions reusing the same `group` prefix (naming collision, copy-pasted template, coincidentally-shared tenant/project id) would otherwise silently discover each other — see the issue #432 comment thread for the incident history that motivated treating this as a hard default rather than a documented caveat.

## Open questions for the wider maintainer review

- Is `discovery` the right vocabulary-token name, and is layering it under `@cao-mcp-server` (rather than a fully separate MCP server) the right shape long-term, or should Phase 2 move to a second `mcpServers` entry for real hard-blocking?
- Should `discovery` ever be part of a built-in role's defaults (e.g. a future `peer` role), or should it always require an explicit profile-level opt-in as implemented here?
- Is a runtime-checked soft gate acceptable as the permanent mechanism, or is native per-MCP-tool blocking (where a provider supports it) worth building before this ships broadly?

This write-up implements the two changes klabulan and tedswinyar converged on in the issue #432 thread. It has not been reviewed or agreed to by the other three tagged maintainers — their objections or alternatives are still open, and this document (and the code implementing it) should be read as a proposal for that review, not a final decision.
