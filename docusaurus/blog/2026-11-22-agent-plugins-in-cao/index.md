---
title: "Agent Plugins in CAO: what exists, and a worked example on two agent CLIs"
authors: [plauzy]
date: 2026-11-22
tags: [deep-dive]
description: "What Agent Plugins 1.0.0 is, what CAO has merged for it, and a measured example of giving two agent CLIs the same skills and MCP server three ways, with its limits."
---

This post gives two agent CLIs, Claude Code and Kiro, the same skills and one MCP server in three ways, and reports what happened on one machine. `cao plugin add` was not the fastest way on any of the four legs by median time, and Claude Code at launch was not tested for it, so its result covers Kiro only. It is a worked example, not a benchmark. I am plauzy, and I wrote CAO's Agent Plugins client. The package I measured is a test package, not client work. I drafted this post with the Kiro CLI, and the measured numbers have not been independently reproduced.

{/* truncate */}

## The problem: copies drift

A team that uses more than one agent CLI tends to copy the same agent configuration into each CLI's own config by hand: a set of skills and the entry for an MCP server. When one copy changes and another does not, the copies drift apart, and each CLI ends up with a different tool set. Agent Plugins packages that configuration once, so each CLI reads it from one place. This post measures how much that changes on one machine; it does not measure drift itself.

## What Agent Plugins 1.0.0 is

Agent Plugins is an open, vendor-neutral package format: a directory with a required `plugin.json`, optional skills under `skills/`, and an optional `mcp.json` for MCP servers. The specification is at https://agent-plugins.org/specification and marks version 1.0.0 as Published. CAO's own conformance table in `docs/agent-plugins.md` has 28 rows for the 21 checklist items: 21 are Implemented, 5 are Delegated to the provider CLI, 1 is Not implemented and 1 is Not applicable. The one Not implemented row is skipping a skill that breaks the Agent Skills limits; CAO warns instead. The specification defines no conformance suite, so that table is CAO's own record, not a certification.

## What CAO has merged

Eight of the Agent Plugins pull requests, the ones this post follows, are merged. #584 merged on 2026-09-20 at 156cf1ed. #1200 merged on 2026-11-04 at 012d7d5e. #1236 merged on 2026-11-06 at cba4f89c. #1253 merged on 2026-11-09 at dd91e8a2. #1369 merged on 2026-11-17 at 58aea18b. #1392 merged on 2026-11-20 at 40f31ef0, #1428 merged on 2026-11-20 at 73012bbe, and #1454 merged on 2026-11-23 at 73253e38. The first five are the ones the Kiro count below covers.

At 79aa1a36 (2026-11-23) the repository holds four packages under `agent-plugin/`, and each declares version 2.5.3, as each also does at the v2.5.5 tag. v2.5.5 is tagged (e2f2978c, 2026-11-23); PyPI is still at 2.5.4, so the release is pending there. #1392 added a test that keeps package versions in step with the release, and the release workflow advances them. The `cao plugin` commands are off by default: without `CAO_AGENT_PLUGINS_ENABLED` the command group errors out and the HTTP routes return 404, and the docs call the surface built but not shipped. At 79aa1a36 the `plugin` verb does not appear in `cao --help`. Graduation and required checks are open maintainer decisions in issue #1393, titled "Agent Plugins: graduation and required checks (maintainer decisions, no default)", which was still open on 2026-11-23. The documentation lists OpenCode among the providers whose MCP configuration CAO writes at install time and Codex among those it recomputes at launch, and says the clients CAO exercises end to end are Kiro CLI and Claude Code. I did not run OpenCode or Codex.

## Built with Kiro

As of 2026-11-19, 34 commits carry a Kiro co-author trailer, counted over the commits of #584, #1200, #1236, #1253 and #1369, which are 35 distinct commits. The one commit without it is a merge by another maintainer, and every commit by plauzy has it. The trailer is `Co-authored-by: Kiro Agent`, which records credit and not who wrote which line, and I have not checked it against anything else. Commit lists:

- https://api.github.com/repos/awslabs/cli-agent-orchestrator/pulls/584/commits
- https://api.github.com/repos/awslabs/cli-agent-orchestrator/pulls/1200/commits
- https://api.github.com/repos/awslabs/cli-agent-orchestrator/pulls/1236/commits
- https://api.github.com/repos/awslabs/cli-agent-orchestrator/pulls/1253/commits
- https://api.github.com/repos/awslabs/cli-agent-orchestrator/pulls/1369/commits

I made my contributions to CAO with the Kiro CLI. On CAO's main branch at 79aa1a36 (2026-11-23), 44 commits carry the Kiro Agent co-author trailer. All 44 are authored by plauzy, and no other author has one. That is 44 of plauzy's 56 commits on main. This is how the work was built, not a claim about Kiro's quality or rank.

## A worked example

The package had 39 skills and 1 MCP server, and both CLIs, Claude Code 2.1.327 and Kiro CLI 3.4.0, got it three ways. By median time, arm c, `cao plugin add`, was the fastest on none of the four legs, and Claude Code at launch was not tested for it, so its result covers Kiro only.

Arm a follows each CLI's documented steps: for Claude Code https://code.claude.com/docs/en/skills and https://code.claude.com/docs/en/mcp, and for Kiro https://kiro.dev/docs/skills.md and https://kiro.dev/docs/mcp/configuration.md. Arm b is a 16-line shell script that installs the whole package each time. Arm c is `cao plugin add` with `CAO_AGENT_PLUGINS_ENABLED=1`, and it needs uv. The legs were a fresh install, an update to one skill, a remove, and a conflict leg where the CLIs already held a local skill of the same name, an MCP entry pinned to an older version and a hand-written Kiro agent. Each arm ran three times per leg in rotated order (a b c c b a b a c), on one macOS machine whose one-minute load average was 5.41 to 5.84 at the start of each leg. CAO was version 2.5.4 at commit 367f53f3, which is untagged and 196 commits after the v2.5.4 tag, and uv was 0.13.6. Both CLIs were real binaries with no login and no mock, and the measurement script's identity was not recorded.

|Leg|Arm|Median ms (min–max)|Commands|Failed|Files added/modified/removed|Claude Code matches|Kiro matches|
|---|---|---|---|---|---|---|---|
|fresh|a|473 (462–475)|4|0|905/0/0|3/3|3/3|
|fresh|b|750 (711–782)|1|0|904/0/0|3/3|3/3|
|fresh|c|635 (524–1048)|2|0|499/0/0|0/3|3/3|
|update|a|14 (14–16)|2|0|0/2/0|3/3|3/3|
|update|b|678 (673–687)|1|0|0/2/0|3/3|3/3|
|update|c|402 (393–431)|1|0|0/3/0|0/3|3/3|
|remove|a|614 (609–671)|4|0|0/2/902|3/3|3/3|
|remove|b|364 (346–366)|1|0|0/2/902|3/3|3/3|
|remove|c|691 (526–1600)|1|0|1/1/493|3/3|3/3|
|conflict|a|453 (442–475)|4|1|901/4/0|0/3|3/3|
|conflict|b|723 (677–745)|1|0|900/4/0|3/3|3/3|
|conflict|c|575 (569–577)|2|1|494/0/0|0/3|0/3|

Arm c wrote fewer files on the fresh leg, 499 added against 905 for arm a and 904 for arm b, and ran one command on update and remove but two on fresh. Arm c wrote nothing for Claude Code, so its Claude Code column reads 0 of 3, and its 3 of 3 for Kiro holds for one agent reading CAO's store through skill globs, not for every Kiro agent. "Matches package" is a content-hash check of the files on disk, not proof that either CLI loaded the skills. The update arms do different work: arm a copies one skill, arm b re-runs the whole install, and arm c runs `cao plugin add --force`, so those timings compare three different operations, and the update leg was not re-run. In the conflict leg `cao plugin add` exited 0 while skipping one colliding skill, `cao install` refused to overwrite the hand-written agent, and arm a's MCP add exited 1 and kept the older version while its skill copy silently overwrote the local skill. Installing CAO took a median of 5549 ms with a cold cache and 661 ms warm over three runs each, and it is not counted in the legs. This is one machine and three repetitions per arm and leg, and arm c's first repetition was slow on fresh (1048 ms) and remove (1600 ms), probably a cold start that I did not isolate. Kiro's `mcp list` needs a login, so its MCP entries were checked in its config file, and in arms a and b Claude Code's own MCP listing showed one MCP server connected. After remove, arm a left 3 files, arm b left 2 and arm c left 7.

A fourth install path is on `main` and in the v2.5.5 tag: the repository is installable as a plugin marketplace for Claude Code and Codex. Issue https://github.com/awslabs/cli-agent-orchestrator/issues/1409 proposed it, and https://github.com/awslabs/cli-agent-orchestrator/pull/1428 merged on 2026-11-20 at 73012bbe. The documentation (`docs/agent-plugins.md`, "Install from the marketplace") says the four packages install with each client's own commands, without cloning anything or enabling `CAO_AGENT_PLUGINS_ENABLED`, and that it was verified against Claude Code 2.1.328 and Codex CLI 0.170.0 from a local checkout. The catalog follows `main` rather than a release tag, so it can describe behavior that has not been released, and by my reading it needs no CAO install, because each client uses its own commands. It reaches only those two CLIs. I did not measure this path, so it has no row or number here.

## Try it

To install the packages through the clients' own marketplaces (not measured here; commands as given in `docs/agent-plugins.md`):

```bash
# Claude Code
/plugin marketplace add awslabs/cli-agent-orchestrator
/plugin install cao@cli-agent-orchestrator

# Codex
codex plugin marketplace add awslabs/cli-agent-orchestrator
codex plugin add cao@cli-agent-orchestrator
```

To install them through CAO, as measured above:

Install CAO first; I did not re-run its install for this post. I re-ran the commands below on 2026-11-23 against CAO built from commit 79aa1a36 (it reports version 2.5.4), with the home directory, `CAO_HOME_DIR`, the XDG variables and `CLAUDE_CONFIG_DIR` pointed at a temporary directory and `CAO_API_PORT` set to 9, so that no command could reach a running CAO server; no terminal was created, and `remove` read the machine's tmux session list. Without the gate, `cao plugin validate` refused and exited 1.

```bash
env CAO_AGENT_PLUGINS_ENABLED=1 cao plugin validate agent-plugin/cao-agent-plugin-lifecycle
env CAO_AGENT_PLUGINS_ENABLED=1 cao plugin add agent-plugin/cao-agent-plugin-lifecycle
env CAO_AGENT_PLUGINS_ENABLED=1 cao plugin list
env CAO_AGENT_PLUGINS_ENABLED=1 cao plugin remove cao-agent-plugin-lifecycle --yes
```

`validate` printed `cao-agent-plugin-lifecycle: loadable` with two skills, `add` printed that two skills were projected, `list` showed version 2.5.3, and `remove` exited 0 but logged a traceback because that home's database was empty. The packages declare 2.5.3 at the v2.5.5 tag and at 79aa1a36. #1454 (merged 2026-11-23, after v2.5.5) applied maintainer decision M4 and closed #1244: it renamed the event-plugin authoring skill `cao-plugin` to `cao-event-plugin`, and the `cao plugin` verb keeps its name. The skill names in the commands above did not change.

## Limits and security

An MCP server in a package is third-party code that runs as a subprocess on your machine, and CAO implements no trust model, signing or provenance check for plugins. `cao plugin validate` checks the manifest and MCP files against schema bytes pinned in the repository without using the network, checks each skill's whole tree for escaping symlinks, and judges each MCP server entry on its own. It reports the four Agent Skills limits only as warnings and still loads the skill. It does not show that a CLI loaded a skill or that an MCP server started: the conformance table marks starting and connecting as Delegated to the provider CLI. A component type CAO does not read is ignored, and one invalid skill or MCP entry is skipped with a finding while its siblings still load. Installing a plugin configures its MCP servers into managed profiles but does not add their tools to an `allowedTools` list that did not name them, though a profile with `"*"` or a matching `@` glob grants them anyway. If you use one CLI on one machine, a package gives you little over copying the skills by hand.
