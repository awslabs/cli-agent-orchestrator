# CLI Agent Orchestrator

A lightweight orchestration system for managing multiple AI agent sessions in tmux terminals. Enables Multi-agent collaboration via MCP server.

## Installation

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/)

2. Install CLI Agent Orchestrator:
```bash
uv tool install git+https://github.com/awslabs/cli-agent-orchestrator.git@launch --upgrade
```

## Quick Start

Initialize the database:
```bash
cao init
```

Launch a terminal with an agent profile:
```bash
cao launch --agents code_sup
```

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This project is licensed under the Apache-2.0 License.

