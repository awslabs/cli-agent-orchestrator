---
title: "Agent Plugins in CAO: what exists, and a worked example on two agent CLIs"
authors: [plauzy]
date: 2026-11-28
tags: [deep-dive]
description: "What Agent Plugins 1.0.0 is, what CAO has merged for it, and an attempted three-way comparison on one machine of giving two agent CLIs the same skills and MCP server, with its limits."
---

When a skill or MCP server entry is copied into several agent CLIs and one copy changes, the copies drift apart, and each CLI ends up with a different tool set. This post reports an attempted three-way comparison on one machine, mine: giving two agent CLIs, Claude Code and Kiro, the same skills and one MCP server in three ways. In the third way, `cao plugin add`, arm c wrote nothing for Claude Code (0 of 3 on fresh, update and conflict; 3 of 3 on remove), so its result covers Kiro only, and "matches package" is a content-hash check of the files on disk, not proof that either CLI loaded the skills. It is a worked example, not a benchmark. I am plauzy, and I wrote CAO's Agent Plugins client. The package I measured is a test package, not client work. I drafted this post with the Kiro CLI, and the measured numbers have not been independently reproduced.

## TL;DR

- **What it does:** `cao plugin add` puts one Agent Plugins 1.0.0 package into CAO's skill store and hands its MCP servers to the providers that (a) lists as receiving them.
- **Measured (author-reported):** by median time, `cao plugin add` was fastest on none of the four legs, and Claude Code at launch was not tested, so the result covers Kiro only.
- **Gate:** the `cao plugin` commands are off by default behind `CAO_AGENT_PLUGINS_ENABLED`; graduation is tracked in [#1393](https://github.com/awslabs/cli-agent-orchestrator/issues/1393).
- **Try it:** install from the marketplace in Claude Code or Codex, or install and clone CAO, then run `cao plugin add` with the gate on.

{/* truncate */}

## Why this matters

<video width="100%" controls loop muted playsInline preload="metadata" poster={replayPoster} aria-label="Replay of a captured terminal transcript: CAO 2.5.5 from PyPI validates, installs, lists and removes the cao-agent-plugin-lifecycle package from CAO main 6234f1f5; every command exits 0.">
  <source src={replayMp4} type="video/mp4" />
  Replay of a captured terminal transcript: CAO 2.5.5 from PyPI validates, installs, lists and removes the cao-agent-plugin-lifecycle package from CAO main 6234f1f5; every command exits 0.
</video>

*A replay of a captured transcript, recorded on 2026-11-24 (UTC): `cao` 2.5.5 from PyPI, run by its full path from a temporary install with `CAO_AGENT_PLUGINS_ENABLED=1` set, validates, adds, lists and removes the `cao-agent-plugin-lifecycle` package from CAO main 6234f1f5. Output is verbatim; only blank lines are drawn differently. `remove` printed no traceback. This is not the measured 2.5.4 run below.*

**(a) What a team on more than one CLI can do now.** Issue [#573](https://github.com/awslabs/cli-agent-orchestrator/issues/573) asked CAO to package its ecosystem as portable plugins and to add an install surface. [#584](https://github.com/awslabs/cli-agent-orchestrator/pull/584), merged on 2026-09-20, added both. As of 2026-11-23 (`79aa1a36`), the documentation says MCP servers are written at install time for Kiro CLI and OpenCode and recomputed at launch for Claude Code, Codex and eight more; Hermes and the `mock_cli` test provider get none. I measured the Kiro path only, on my machine (see the worked example below).

**(b) Why an open format on real CLI processes.** CAO runs each provider's own CLI as a full process in its own terminal session, with that CLI's native login, so a package has to reach the files that real CLI reads. Agent Plugins 1.0.0 is a portable package format with a published specification. Where a client has its own format, the difference shows: the specification defines no marketplace format, so CAO's repository carries one catalog file for Claude Code and another for Codex, both generated from the same package list.

**(c) What it unlocks for CAO.** CAO now ships itself in this format: four packages under `agent-plugin/`, with its contributor skills, its plugin authoring and audit skills, and `cao-ag-ui-builder`, which carries the skill agents use to send generative UI to AG-UI clients such as the AG-UI Dojo ([#1116](https://github.com/awslabs/cli-agent-orchestrator/issues/1116)). The documentation says the same directory is installable by any client that implements Agent Plugins 1.0.0, and that support in a client other than Kiro CLI and Claude Code is that client's claim to make, not something CAO verifies. The worked example below does not install these four packages: it measures a separate test package (39 skills and 1 MCP server), comparing installing it through `cao plugin add` with the two other routes on one machine, for two CLIs.

## The problem: copies drift

The worked example does not measure drift itself.

## What Agent Plugins 1.0.0 is

Agent Plugins is an open, portable package format: a directory with a required `plugin.json`, optional skills under `skills/`, and an optional `mcp.json` for MCP servers. The specification is at https://agent-plugins.org/specification and marks version 1.0.0 as Published. CAO's own conformance table in `docs/agent-plugins.md` has 28 rows for the 21 checklist items: 21 are Implemented, 5 are Delegated to the provider CLI, 1 is Not implemented and 1 is Not applicable. The one Not implemented row is skipping a skill that breaks the Agent Skills limits; CAO warns instead. The specification defines no conformance suite, so that table is CAO's own record, not a certification.

## What CAO has merged

Eight of the Agent Plugins pull requests, the ones this post follows, are merged.

- [#584](https://github.com/awslabs/cli-agent-orchestrator/pull/584) merged on 2026-09-20 at 156cf1ed.
- [#1200](https://github.com/awslabs/cli-agent-orchestrator/pull/1200) merged on 2026-11-04 at 012d7d5e.
- [#1236](https://github.com/awslabs/cli-agent-orchestrator/pull/1236) merged on 2026-11-06 at cba4f89c.
- [#1253](https://github.com/awslabs/cli-agent-orchestrator/pull/1253) merged on 2026-11-09 at dd91e8a2.
- [#1369](https://github.com/awslabs/cli-agent-orchestrator/pull/1369) merged on 2026-11-17 at 58aea18b.
- [#1392](https://github.com/awslabs/cli-agent-orchestrator/pull/1392) merged on 2026-11-20 at 40f31ef0,
- [#1428](https://github.com/awslabs/cli-agent-orchestrator/pull/1428) ([#1409](https://github.com/awslabs/cli-agent-orchestrator/issues/1409)) merged on 2026-11-20 at 73012bbe, and
- [#1454](https://github.com/awslabs/cli-agent-orchestrator/pull/1454) merged on 2026-11-23 at 73253e38.

The first five are the ones the Kiro count below covers.

As of 2026-11-24, each of the four packages under `agent-plugin/` declares version 2.5.5 at `main` 6234f1f5 and 2.5.3 in v2.5.5 (commit d094b4ba). CAO's v2.5.5 GitHub release was published on 2026-11-24 at 04:47 UTC, and its PyPI files were uploaded that day.

[#1392](https://github.com/awslabs/cli-agent-orchestrator/pull/1392) added a test that keeps package versions in step with the release, and the release workflow advances them. The `cao plugin` commands are off by default: without `CAO_AGENT_PLUGINS_ENABLED` the command group errors out and the HTTP routes return 404, and the docs call the surface built but not shipped. At 79aa1a36 the `plugin` verb does not appear in `cao --help`.

Graduation and required checks are maintainer decisions in issue [#1393](https://github.com/awslabs/cli-agent-orchestrator/issues/1393), which was still open on 2026-11-25.

The documentation says the clients CAO exercises end to end are Kiro CLI and Claude Code. I did not run OpenCode or Codex.

## Built with Kiro

As of 2026-11-23, 34 commits carry a Kiro co-author trailer, counted over the commits of [#584](https://github.com/awslabs/cli-agent-orchestrator/pull/584), [#1200](https://github.com/awslabs/cli-agent-orchestrator/pull/1200), [#1236](https://github.com/awslabs/cli-agent-orchestrator/pull/1236), [#1253](https://github.com/awslabs/cli-agent-orchestrator/pull/1253) and [#1369](https://github.com/awslabs/cli-agent-orchestrator/pull/1369), which are 35 distinct commits. The one commit without it is a merge by another maintainer, and every commit by plauzy has it. The trailer is `Co-authored-by: Kiro Agent`, which records credit and not who wrote which line, and I have not checked it against anything else. Commit lists:

- https://api.github.com/repos/awslabs/cli-agent-orchestrator/pulls/584/commits
- https://api.github.com/repos/awslabs/cli-agent-orchestrator/pulls/1200/commits
- https://api.github.com/repos/awslabs/cli-agent-orchestrator/pulls/1236/commits
- https://api.github.com/repos/awslabs/cli-agent-orchestrator/pulls/1253/commits
- https://api.github.com/repos/awslabs/cli-agent-orchestrator/pulls/1369/commits

I made my contributions to CAO with the Kiro CLI. As of 2026-11-23, on CAO's main branch at 79aa1a36, 44 commits carry the Kiro Agent co-author trailer. All 44 are authored by plauzy, and no other author has one. That is 44 of plauzy's 56 commits on main. This is how the work was built, not a claim about Kiro's quality or rank.

## A worked example

The measurements in this section are author-reported: I took them on my machine, and the raw evidence is not public. The package had 39 skills and 1 MCP server, and both CLIs, Claude Code 2.1.327 and Kiro CLI 3.4.0, got it three ways. By median time, arm c, `cao plugin add`, was the fastest on none of the four legs, and Claude Code at launch was not tested for it, so its result covers Kiro only.

- **Arms** (author-reported): Arm a follows each CLI's documented steps: for Claude Code https://code.claude.com/docs/en/skills and https://code.claude.com/docs/en/mcp, and for Kiro https://kiro.dev/docs/skills.md and https://kiro.dev/docs/mcp/configuration.md. Arm b is a 16-line shell script that installs the whole package each time. Arm c is `cao plugin add` with `CAO_AGENT_PLUGINS_ENABLED=1`, and it needs uv.
- **Legs** (author-reported): The legs were a fresh install, an update to one skill, a remove, and a conflict leg where the CLIs already held a local skill of the same name, an MCP entry pinned to an older version and a hand-written Kiro agent.
- **Runs** (author-reported): Each arm ran three times per leg in rotated order (a b c c b a b a c), on my one macOS machine, whose one-minute load average was 5.41 to 5.84 at the start of each leg.
- **Versions** (author-reported): CAO was version 2.5.4 at commit 367f53f3, which is untagged and 196 commits after the v2.5.4 tag, and uv was 0.13.6. Both CLIs were real binaries with no login and no mock, and the measurement script's identity was not recorded.

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

These observations are also author-reported, from the same runs.

- **Matches** (author-reported): Arm c wrote nothing for Claude Code (0 of 3 on fresh, update and conflict; 3 of 3 on remove), and Kiro's 3 of 3 (0 of 3 on conflict) holds for one agent reading CAO's store through skill globs, not for every Kiro agent. "Matches package" is a content-hash check of the files on disk, not proof that either CLI loaded the skills.
- **Update leg** (author-reported): The update arms do different work: arm a copies one skill, arm b re-runs the whole install, and arm c runs `cao plugin add --force`, so those timings compare three different operations, and the update leg was not re-run.
- **Conflict leg** (author-reported): In the conflict leg `cao plugin add` exited 0 while skipping one colliding skill, `cao install` refused to overwrite the hand-written agent, and arm a's MCP add exited 1 and kept the older version while its skill copy silently overwrote the local skill.
- **Scope** (author-reported): Installing CAO took a median of 5549 ms with a cold cache and 661 ms warm over three runs each, and it is not counted in the legs. Arm c's first repetition was slow on fresh (1048 ms) and remove (1600 ms), probably a cold start that I did not isolate.
- **End state** (author-reported): Kiro's `mcp list` needs a login, so its MCP entries were checked in its config file, and in arms a and b Claude Code's own MCP listing showed one MCP server connected. After remove, arm a left 3 files, arm b left 2 and arm c left 7.

A fourth install path is on `main` and in v2.5.5: the repository is installable as a plugin marketplace for Claude Code and Codex. The documentation (`docs/agent-plugins.md`, "Install from the marketplace") says the four packages install with each client's own commands, without cloning anything or enabling `CAO_AGENT_PLUGINS_ENABLED`, and that it was verified against Claude Code 2.1.328 and Codex CLI 0.170.0 from a local checkout. The catalog follows `main` rather than a release tag, so it can describe behavior that has not been released, and installing from it needs no CAO install, though using the `cao` package's operator tools does. It reaches only those two CLIs. I did not measure this path, so it has no row or number here.

## Try it

To install from the clients' own marketplaces (not measured here; commands as given in `docs/agent-plugins.md`): these commands install the `cao` operator package; the catalog lists `cao-contributor`, `cao-agent-plugin-lifecycle` and `cao-ag-ui-builder` separately. The `cao` package needs `uv` on PATH (its MCP server launches through `uvx`) and a running `cao-server` at `http://127.0.0.1:9889`. Installing needs no CAO install; using the operator tools does.

In Claude Code, type these at its prompt (they are slash commands, not shell commands):

```text
/plugin marketplace add awslabs/cli-agent-orchestrator
/plugin install cao@cli-agent-orchestrator
```

For Codex, in a shell:

```bash
codex plugin marketplace add awslabs/cli-agent-orchestrator
codex plugin add cao@cli-agent-orchestrator
```

To install through CAO, the route measured above (these commands install one of the four packages, `cao-agent-plugin-lifecycle`):

Install CAO first: `uv tool install 'cli-agent-orchestrator[agui]==2.5.5'` (not run as written; only dry-run resolved, on macOS arm64 with CPython 3.12). On my machine, I re-ran the commands below on 2026-11-23 against CAO built from commit 79aa1a36 (it reports version 2.5.4), with the home directory, `CAO_HOME_DIR`, the XDG variables and `CLAUDE_CONFIG_DIR` pointed at a temporary directory and `CAO_API_PORT` set to 9, so that no command could reach a running CAO server; no terminal was created, and `remove` read the machine's tmux session list. Without the gate, `cao plugin validate` refused and exited 1.

The example commands below use a path inside a CAO checkout. Clone [awslabs/cli-agent-orchestrator](https://github.com/awslabs/cli-agent-orchestrator) and run them from the repository root, so that `agent-plugin/cao-agent-plugin-lifecycle` resolves.

```bash
env CAO_AGENT_PLUGINS_ENABLED=1 cao plugin validate agent-plugin/cao-agent-plugin-lifecycle
env CAO_AGENT_PLUGINS_ENABLED=1 cao plugin add agent-plugin/cao-agent-plugin-lifecycle
env CAO_AGENT_PLUGINS_ENABLED=1 cao plugin list
```

To remove the package when you are done (if a live session uses one of its skills, `remove` lists it and asks before removing):

```bash
env CAO_AGENT_PLUGINS_ENABLED=1 cao plugin remove cao-agent-plugin-lifecycle
```

In that author-reported run, `validate` printed `cao-agent-plugin-lifecycle: loadable` with two skills, `add` printed that two skills were projected, `list` showed version 2.5.3, and `remove`, run with `--yes` against that isolated CAO home, exited 0 but logged a traceback because that home's database was empty. [#1454](https://github.com/awslabs/cli-agent-orchestrator/pull/1454) (merged 2026-11-23, in v2.5.5) closed [#1244](https://github.com/awslabs/cli-agent-orchestrator/issues/1244): it renamed the event-plugin authoring skill `cao-plugin` to `cao-event-plugin`, and the `cao plugin` verb keeps its name.

## Limits and security

An MCP server in a package is either a local server, run as a subprocess, which is third-party code on your machine, or a remote HTTP endpoint, which is a third-party service that receives your requests; assess both. CAO implements no trust model, signing or provenance check for plugins. Specification 1.0.0 calls `env` (9.2) and `headers` (7.2.1) values "visible package data", says plugins MUST NOT embed credentials or other secrets there, and defines no "portable credential-reference fields" (7.2.1).

`cao plugin validate` checks the manifest and MCP files against schema bytes pinned in the repository without using the network, checks each skill's whole tree for escaping symlinks, and judges each MCP server entry on its own. It reports the four Agent Skills limits only as warnings and still loads the skill. It does not show that a CLI loaded a skill or that an MCP server started: the conformance table marks starting and connecting as Delegated to the provider CLI. A component type CAO does not read is ignored, and one invalid skill or MCP entry is skipped with a finding while its siblings still load.

Installing a plugin configures its MCP servers into managed profiles but does not add their tools to an `allowedTools` list that did not name them, though a profile with `"*"` or a matching `@` glob grants them anyway. If you use one CLI on one machine, a package gives you little over copying the skills by hand.

import replayMp4 from './agent-plugins-cli-replay-2.5.5.mp4';
import replayPoster from './agent-plugins-cli-replay-2.5.5-poster.jpg';
