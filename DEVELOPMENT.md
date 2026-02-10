# Development Guide

This guide covers setting up your development environment and running tests for the CLI Agent Orchestrator project.

## Prerequisites

- Python 3.10 or higher
- [uv](https://docs.astral.sh/uv/) - Fast Python package installer and resolver
- Git
- tmux 3.3+ (for running the orchestrator and integration tests)

## Getting Started

### 1. Clone the Repository

```bash
git clone https://github.com/awslabs/cli-agent-orchestrator.git
cd cli-agent-orchestrator/
```

### 2. Install Dependencies

The project uses `uv` for package management. Install all dependencies including development packages:

```bash
uv sync
```

This command:
- Creates a virtual environment (if one doesn't exist)
- Installs all project dependencies
- Installs development dependencies (pytest, coverage tools, linters, etc.)

### 3. Verify Installation

```bash
# Check that the CLI is available
uv run cao --help

# Run a quick test to ensure everything is working
uv run pytest test/providers/test_kiro_cli_unit.py -v -k "test_initialization"
```

## Running Tests

### Unit Tests

Unit tests use mocked dependencies and don't require CLI tools or servers:

```bash
# Run all unit tests (626 tests, excludes E2E and Q CLI integration)
uv run pytest test/ -v --ignore=test/e2e --ignore=test/providers/test_q_cli_integration.py

# Run provider-specific tests
uv run pytest test/providers/test_kiro_cli_unit.py -v
uv run pytest test/providers/test_claude_code_unit.py -v
uv run pytest test/providers/test_codex_provider_unit.py -v
uv run pytest test/providers/test_q_cli_unit.py -v
uv run pytest test/providers/test_kimi_cli_unit.py -v
uv run pytest test/providers/test_gemini_cli_unit.py -v

# Run other test modules
uv run pytest test/clients/ -v
uv run pytest test/services/ -v
uv run pytest test/mcp_server/ -v
uv run pytest test/cli/ -v
uv run pytest test/models/ -v
uv run pytest test/utils/ -v

# Run with coverage report
uv run pytest test/ --cov=src --cov-report=term-missing -v \
  --ignore=test/e2e --ignore=test/providers/test_q_cli_integration.py

# Run specific test class
uv run pytest test/providers/test_codex_provider_unit.py::TestCodexBuildCommand -v
```

### End-to-End Tests

E2E tests validate handoff, assign, and send_message flows against real CLI providers. They require a running CAO server and authenticated CLI tools:

```bash
# Start CAO server
uv run cao-server

# Install required agent profiles
cao install examples/assign/data_analyst.md
cao install examples/assign/report_generator.md

# Run all E2E tests (all providers)
uv run pytest -m e2e test/e2e/ -v

# Run for a specific provider
uv run pytest -m e2e test/e2e/ -v -k codex
uv run pytest -m e2e test/e2e/ -v -k claude_code
uv run pytest -m e2e test/e2e/ -v -k kiro_cli
uv run pytest -m e2e test/e2e/ -v -k kimi_cli
uv run pytest -m e2e test/e2e/ -v -k gemini_cli

# Run a specific test type
uv run pytest -m e2e test/e2e/test_handoff.py -v
uv run pytest -m e2e test/e2e/test_assign.py -v
uv run pytest -m e2e test/e2e/test_send_message.py -v
uv run pytest -m e2e test/e2e/test_supervisor_orchestration.py -v -o "addopts="
```

**Requirements for E2E Tests:**
- CAO server running (`uv run cao-server`)
- tmux 3.3+ installed
- At least one CLI tool installed and authenticated:
  - **Codex CLI**: `npm install -g @openai/codex` + `OPENAI_API_KEY` set
  - **Claude Code**: `npm install -g @anthropic-ai/claude-code` + `ANTHROPIC_API_KEY` set
  - **Kiro CLI**: `npm install -g @anthropic-ai/kiro-cli` + AWS credentials configured
  - **Kimi CLI**: `brew install kimi-cli` or `uv tool install kimi-cli` + `kimi login`
  - **Gemini CLI**: `npm install -g @google/gemini-cli` + OAuth or `GEMINI_API_KEY`
- Agent profiles installed: `analysis_supervisor`, `data_analyst`, `report_generator`

E2E tests are excluded from default `pytest` runs via `-m 'not e2e'` in `pyproject.toml`.

### Q CLI Integration Tests

Q CLI integration tests require the Q CLI to be installed and authenticated:

```bash
# Run Q CLI integration tests (requires Q CLI setup)
uv run pytest test/providers/test_q_cli_integration.py -v
```

### Run All Tests

```bash
# Run all unit tests (excludes E2E and Q CLI integration)
uv run pytest test/ -v --ignore=test/e2e --ignore=test/providers/test_q_cli_integration.py

# Run tests with coverage
uv run pytest test/ --cov=src --cov-report=term-missing -v \
  --ignore=test/e2e --ignore=test/providers/test_q_cli_integration.py
```

### Test Markers

Tests are organized with pytest markers:

```bash
# Run only E2E tests
uv run pytest -m e2e test/e2e/ -v

# Run only unit tests (exclude E2E)
uv run pytest -m "not e2e" test/ -v

# Run only async tests
uv run pytest -m asyncio -v
```

## Code Quality

### Formatting

The project uses `black` for code formatting:

```bash
# Format all Python files
uv run black src/ test/

# Check formatting without making changes
uv run black --check src/ test/
```

### Import Sorting

The project uses `isort` for organizing imports:

```bash
# Sort imports
uv run isort src/ test/

# Check import sorting without making changes
uv run isort --check-only src/ test/
```

### Type Checking

The project uses `mypy` for static type checking:

```bash
# Run type checker
uv run mypy src/
```

### Run All Quality Checks

```bash
# Format, sort imports, type check, and run tests
uv run black src/ test/
uv run isort src/ test/
uv run mypy src/
uv run pytest test/ -v --ignore=test/e2e --ignore=test/providers/test_q_cli_integration.py
```

## Development Workflow

### 1. Create a Feature Branch

```bash
git checkout -b feature/your-feature-name
```

### 2. Make Changes

Edit code in `src/cli_agent_orchestrator/`

### 3. Add Tests

Add or update tests in `test/`

### 4. Run Tests Locally

```bash
# Run unit tests
uv run pytest test/ -v --ignore=test/e2e --ignore=test/providers/test_q_cli_integration.py

# Run E2E tests if you changed provider code
uv run cao-server &
uv run pytest -m e2e test/e2e/ -v -k <provider>
```

### 5. Check Code Quality

```bash
uv run black --check src/ test/
uv run isort --check-only src/ test/
uv run mypy src/
```

### 6. Commit and Push

```bash
git add .
git commit -m "Add feature: description"
git push origin feature/your-feature-name
```

### 7. Create Pull Request

Create a pull request on GitHub. CI/CD will automatically run unit tests and code quality checks.

## Project Structure

```
cli-agent-orchestrator/
├── src/
│   └── cli_agent_orchestrator/     # Main source code
│       ├── api/                    # FastAPI server
│       ├── cli/                    # CLI commands
│       ├── clients/                # Database and tmux clients
│       ├── mcp_server/             # MCP server implementation
│       ├── models/                 # Data models
│       ├── providers/              # Agent providers (Kiro CLI, Claude Code, Codex, Kimi CLI, Gemini CLI, Q CLI)
│       ├── services/               # Business logic services
│       ├── agent_store/            # Built-in agent profiles (.md)
│       └── utils/                  # Utility functions
├── test/                           # Test suite
│   ├── cli/                        # CLI command tests
│   ├── clients/                    # Tmux and database client tests
│   ├── e2e/                        # End-to-end tests (all providers)
│   ├── mcp_server/                 # MCP server and handoff tests
│   ├── models/                     # Data model tests
│   ├── providers/                  # Provider unit tests
│   │   ├── fixtures/               # Test fixtures
│   │   ├── test_kiro_cli_unit.py
│   │   ├── test_claude_code_unit.py
│   │   ├── test_codex_provider_unit.py
│   │   ├── test_kimi_cli_unit.py
│   │   ├── test_gemini_cli_unit.py
│   │   └── test_q_cli_unit.py
│   ├── services/                   # Service layer tests
│   └── utils/                      # Utility tests
├── docs/                           # Documentation
│   ├── api.md                      # API reference
│   ├── agent-profile.md            # Agent profile format
│   ├── codex-cli.md                # Codex provider docs
│   ├── claude-code.md              # Claude Code provider docs
│   ├── kiro-cli.md                 # Kiro CLI provider docs
│   ├── kimi-cli.md                 # Kimi CLI provider docs
│   └── gemini-cli.md               # Gemini CLI provider docs
├── examples/                       # Example workflows
│   ├── assign/                     # Assign (async parallel) workflow
│   ├── codex-basic/                # Basic Codex usage
│   └── flow/                       # Scheduled flow examples
├── skills/                        # AI coding agent skills (single source of truth)
│   ├── build-cao-provider/        # Provider development lifecycle guide
│   └── skill-creator/             # Skill creation guide
├── pyproject.toml                  # Project configuration
└── uv.lock                         # Locked dependencies
```

## Resources

- [Project README](README.md)
- [Test Documentation](test/README.md)
- [Provider Test Documentation](test/providers/README.md)
- [API Documentation](docs/api.md)
- [Contributing Guidelines](CONTRIBUTING.md)
- [uv Documentation](https://docs.astral.sh/uv/)
- [pytest Documentation](https://docs.pytest.org/)
