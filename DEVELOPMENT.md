# Development Guide

This guide covers setting up your development environment and running tests for the CLI Agent Orchestrator project.

## Prerequisites

- Python 3.10 or higher
- [uv](https://docs.astral.sh/uv/) - Fast Python package installer and resolver
- Git
- tmux 3.2+ (for running the orchestrator and integration tests)

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
uv run pytest test/providers/test_q_cli_unit.py -v -k "test_initialization"
```

## Running Tests

### Unit Tests

Unit tests are fast (< 1 second) and use mocked dependencies:

```bash
# Run all unit tests
uv run pytest test/providers/test_q_cli_unit.py -v

# Run with coverage report
uv run pytest test/providers/test_q_cli_unit.py --cov=src/cli_agent_orchestrator/providers/q_cli.py --cov-report=term-missing -v

# Run specific test class
uv run pytest test/providers/test_q_cli_unit.py::TestQCliProviderStatusDetection -v

# Run specific test
uv run pytest test/providers/test_q_cli_unit.py::TestQCliProviderStatusDetection::test_get_status_idle -v
```

### Integration Tests

Integration tests require Q CLI to be installed and authenticated:

```bash
# Run all integration tests (requires Q CLI setup)
uv run pytest test/providers/test_q_cli_integration.py -v

# Skip integration tests
uv run pytest test/providers/ -m "not integration" -v
```

**Requirements for Integration Tests:**
- Q CLI must be installed (`q` command available)
- Q CLI must be authenticated (AWS credentials configured)
- tmux 3.2+ must be installed

### Run All Tests

```bash
# Run all tests
uv run pytest -v

# Run tests with coverage for all modules
uv run pytest --cov=src --cov-report=term-missing -v

# Run tests in parallel (faster)
uv run pytest -n auto
```

### Test Markers

Tests are organized with pytest markers:

```bash
# Run only integration tests
uv run pytest -m integration -v

# Skip slow tests
uv run pytest -m "not slow" -v

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
uv run pytest -v
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
# Run unit tests (fast)
uv run pytest test/providers/test_q_cli_unit.py -v

# Run all tests
uv run pytest -v
```

### 5. Check Code Quality

```bash
uv run black src/ test/
uv run isort src/ test/
uv run mypy src/
```

### 6. Commit and Push

```bash
git add .
git commit -m "Add feature: description"
git push origin feature/your-feature-name
```

### 7. Create Pull Request

Create a pull request on GitHub. CI/CD will automatically run tests.

## Working with the Q CLI Provider

### Regenerate Test Fixtures

If Q CLI output format changes:

```bash
uv run python test/providers/fixtures/generate_fixtures.py
```

### Test Against Real Q CLI

```bash
# Ensure Q CLI is available
which q

# Ensure Q CLI is authenticated
q status

# Run integration tests
uv run pytest test/providers/test_q_cli_integration.py -v
```

## Troubleshooting

### Import Errors

If you encounter import errors when running tests:

```bash
# Re-sync dependencies
uv sync

# If that doesn't work, remove the virtual environment and start fresh
rm -rf .venv
uv sync
```

### Test Failures

```bash
# Run with verbose output
uv run pytest -vv

# Run a specific failing test
uv run pytest test/path/to/test.py::test_name -vv

# Show print statements
uv run pytest -s
```

### Coverage Issues

```bash
# Generate detailed coverage report
uv run pytest --cov=src --cov-report=html
# Open htmlcov/index.html in your browser

# Show missing lines
uv run pytest --cov=src --cov-report=term-missing
```

## Adding New Dependencies

### Runtime Dependencies

```bash
# Add a new runtime dependency
uv add package-name

# Add with version constraint
uv add "package-name>=1.0.0"
```

### Development Dependencies

```bash
# Add a new development dependency
uv add --dev package-name
```

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
│       ├── providers/              # Agent providers (Q CLI, Claude Code)
│       ├── services/               # Business logic services
│       └── utils/                  # Utility functions
├── test/                           # Test suite
│   └── providers/                  # Provider tests
│       ├── fixtures/               # Test fixtures
│       ├── test_q_cli_unit.py     # Unit tests
│       └── test_q_cli_integration.py  # Integration tests
├── docs/                           # Documentation
├── examples/                       # Example workflows
├── pyproject.toml                  # Project configuration
└── uv.lock                         # Locked dependencies
```

## Resources

- [Project README](README.md)
- [Test Documentation](test/providers/README.md)
- [Contributing Guidelines](CONTRIBUTING.md)
- [uv Documentation](https://docs.astral.sh/uv/)
- [pytest Documentation](https://docs.pytest.org/)
