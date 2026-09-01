<!-- An absolute raw URL, not a repo-relative path: this file is also the PyPI
     long description (see `readme` in pyproject.toml) and PyPI cannot resolve
     relative paths.

     The banner is used rather than the bare mark because it supplies its own
     background, so the mark's navy never has to survive a backdrop it cannot
     see. The bare mark would need a <picture>/prefers-color-scheme swap, since
     that navy contrasts at only 1.77:1 on GitHub's dark theme, and PyPI allows
     <img> but not <picture>, so that markup would either be dropped or escaped
     into visible tag soup depending on how it sanitizes.

     The same constraint means the banner cannot be swapped per theme either.
     It is light, matching PyPI and GitHub's light theme, and it shows as a
     bright panel on GitHub's dark theme. That is a deliberate trade, not an
     oversight. Keep in sync with README.zh-CN.md. -->
<p align="center">
  <img src="https://raw.githubusercontent.com/awslabs/cli-agent-orchestrator/main/docusaurus/static/img/cao-social-card.png"
       alt="" width="640">
</p>

# CLI Agent Orchestrator (CAO)

[English](README.md) | [简体中文](README.zh-CN.md)

[![PyPI version](https://img.shields.io/pypi/v/cli-agent-orchestrator.svg)](https://pypi.org/project/cli-agent-orchestrator/)
[![Python versions](https://img.shields.io/pypi/pyversions/cli-agent-orchestrator.svg)](https://pypi.org/project/cli-agent-orchestrator/)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/awslabs/cli-agent-orchestrator)

**CLI Agent Orchestrator (CAO)** coordinates multiple AI coding CLIs so a
supervisor can delegate work to specialist agents in parallel or sequence.

📚 **[Documentation](https://awslabs.github.io/cli-agent-orchestrator/)** —
guides, reference, and two interactive courses.

## What CAO does

CAO runs a local `cao-server`, starts provider CLIs in isolated terminal
sessions, and gives a supervisor tools for coordinating workers. The agents
remain full CLI processes with their native authentication and capabilities.
See [CODEBASE.md](CODEBASE.md) for the runtime architecture and package layout.

## Prerequisites

Install:

- Python 3.10 or later
- tmux 3.3 or later
- [uv](https://docs.astral.sh/uv/)
- At least one supported provider CLI, authenticated before you launch CAO:
  [Kiro CLI](docs/kiro-cli.md), [Claude Code](docs/claude-code.md),
  [Codex CLI](docs/codex-cli.md), [Antigravity CLI](docs/antigravity-cli.md),
  [Hermes](docs/hermes.md), [Kimi CLI](docs/kimi-cli.md),
  [MiniMax Code](docs/minimax-code.md),
  [GitHub Copilot CLI](docs/copilot-cli.md),
  [OpenCode CLI](docs/opencode-cli.md), [Oh My Pi(OMP) CLI](docs/omp-cli.md),
  [Cursor CLI](docs/cursor-cli.md), or
  [Grok Build CLI](docs/grok-cli.md)

The focused provider guides contain installation, authentication, and
provider-specific behavior.

## Install CAO

Install the current `main` branch as a uv tool:

```bash
uv tool install git+https://github.com/awslabs/cli-agent-orchestrator.git@main --upgrade
cao --help
```

For a tagged release, install
[`cli-agent-orchestrator` from PyPI](https://pypi.org/project/cli-agent-orchestrator/).
See [DEVELOPMENT.md](DEVELOPMENT.md) for a source checkout.
For container-based installation, see the
[devcontainer feature](docs/devcontainer-feature.md).

To update an existing CAO installation:

```bash
cao update
```

See [Updating CAO](docs/updating.md) for source-aware behavior and edge cases.

## First supervisor launch

The unqualified commands below use CAO's default Kiro CLI provider. If you
installed a different provider, follow its focused guide above for the
provider override while keeping the same sequence.

1. Install the built-in supervisor profile:

   ```bash
   cao install code_supervisor
   ```

2. In terminal A, start the local server and leave it running:

   ```bash
   cao-server
   ```

3. In terminal B, change to the project directory the agents should work in,
   then launch the supervisor:

   ```bash
   cd /path/to/your/project
   cao launch --agents code_supervisor
   ```

4. Observe the supervisor in the attached launch terminal, open the
   [Web UI](docs/web-ui.md) at `http://localhost:9889`, or follow the
   [tmux guide](docs/tmux.md) to attach to its session.

5. Stop the named session when finished:

   ```bash
   cao shutdown --session {session-name}
   ```

   To stop every CAO session instead, run `cao shutdown --all`.

## Where to go next

### Operate CAO

- [Control-plane selection](docs/control-planes.md): choose the Web UI, shell
  CLI, operations MCP server, or plugins.
- [Web UI](docs/web-ui.md) and [MCP Apps](docs/mcp-apps.md): browser and
  host-rendered fleet interfaces.
- [Flows](docs/flows.md) and [workflows](docs/workflows.md): scheduled runs and
  multi-step pipelines.
- [Skills](docs/skills.md): install, scope, and author reusable agent guidance.
- [Memory](docs/memory.md) and [self-learning](docs/self-learning.md):
  persistent cross-session memory, and the opt-in loop that turns workflow
  outcomes into lessons and promoted instructions.
- [AI-DLC portfolio example](examples/aidlc-portfolio/README.md): coordinate
  parallel AI-DLC intents across repositories and isolated worktrees.
- [Tool restrictions](docs/tool-restrictions.md): roles, allowlists, and
  provider enforcement.
- [Kubernetes deployment](examples/cao-clusters/kubernetes/eks/README.md): run a supervisor and worker fleet on
  Amazon EKS, with shared workspace, per-pod state, and credential delivery.
- [Updating CAO](docs/updating.md): update an installed uv tool.

### Configure and integrate

- [Agent profiles](docs/agent-profile.md): profile schema, discovery, provider
  selection, and overrides.
- [HTTP API and PTY WebSocket](docs/api.md): route-family overview and terminal
  streaming contract.
- [Plugins](docs/plugins.md): outbound events, installation, and authoring.
- Provider behavior:
  [Kiro CLI](docs/kiro-cli.md), [Claude Code](docs/claude-code.md),
  [Codex CLI](docs/codex-cli.md), [Antigravity CLI](docs/antigravity-cli.md),
  [Hermes](docs/hermes.md), [Kimi CLI](docs/kimi-cli.md),
  [MiniMax Code](docs/minimax-code.md),
  [GitHub Copilot CLI](docs/copilot-cli.md),
  [OpenCode CLI](docs/opencode-cli.md), [Oh My Pi(OMP) CLI](docs/omp-cli.md),
  [Cursor CLI](docs/cursor-cli.md), and
  [Grok Build CLI](docs/grok-cli.md).
- [Security policy](SECURITY.md): vulnerability reporting and deployment
  guidance.

### Contribute

- [Codebase guide](CODEBASE.md): runtime surfaces, package ownership, and data
  flow.
- [Development guide](DEVELOPMENT.md): local setup, testing, and verification.
- [Release guide](docs/RELEASING.md): maintainer release process.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [DEVELOPMENT.md](DEVELOPMENT.md)
before submitting changes. Documentation changes must also follow the
[documentation maintenance rule](CODEBASE.md#documentation-maintenance).

## License

This project is licensed under the Apache License 2.0. See
[LICENSE](LICENSE).
