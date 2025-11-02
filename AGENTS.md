# Repository Guidelines

## Project Structure & Module Organization
Key modules live in `src/cli_agent_orchestrator/`: `api/` (FastAPI), `cli/` (Click), `providers/` (CLI adapters), `services/` (business logic), `models/`, `agent_store/`, `utils/`, and `constants.py`. Tests mirror the tree under `test/`. Reference material sits in `docs/` and runnable demos in `examples/`.

## Build, Test, and Development Commands
- `uv sync` boots the env with runtime + dev deps.
- `uv run cao --help` confirms entry points resolve.
- `uv run pytest -v` (or `-m "not integration"`) executes suites with configured coverage.
- `uv run black src/ test/` and `uv run isort src/ test/` enforce formatting; `uv run mypy src/` applies strict typing.

## Coding Style & Naming Conventions
Write Python 3.10 with 4-space indents, type hints, snake_case modules/functions, PascalCase classes, and UPPER_SNAKE constants. Keep CLI flags hyphenated (`cao launch --agents`). Update `docs/` when new MCP tools, providers, or agent behaviors ship. Run formatters before review.

## Testing Guidelines
Mirror every provider/service change with tests under `test/`. Unit specs belong in `test_*_unit.py`; integration flows use `*_integration.py` and require tmux plus authenticated Q CLI. Mark long-running scenarios with `slow`, external deps with `integration`, async with `asyncio`. Maintain the default `--cov=src --cov-report=term-missing`.

## Commit & Pull Request Guidelines
Use imperative commit subjects with optional prefixes (`refactor:`) and reference issues/PR numbers (`(#42)`). PRs should outline motivation, verification commands, and user-visible deltas (logs or screenshots). Run `uv run black`, `isort`, `mypy`, and relevant pytest targets before requesting review; justify any skipped checks.

## Security & Configuration Tips
Never commit AWS credentials, MCP secrets, or tmux socket paths. Route configuration through environment variables or ignored `.env` files. Provide safe defaults when documenting agents and flag network-sensitive changes in PR descriptions.

## Codex CLI Integration
`codex_cli` now ships alongside `q_cli` and `claude_code`. The provider strips Codex TUI escape sequences, detects `Working … (esc to interrupt)` status updates, surfaces agent output blocks, and sends a double `Ctrl+C` to exit gracefully. `constants.PROVIDERS`, `ProviderType`, and the provider manager accept the new identifier, and flows can opt-in via a `provider` frontmatter key. MCP tools (`assign`, `handoff`) read that metadata so Codex supervisors can spin up Codex-backed workers automatically. Unit fixtures under `test/providers/fixtures/` capture idle, processing, and completed Codex panes; `test/providers/test_codex_cli_unit.py` keeps regressions tight, `test/mcp_server/test_server_terminal_creation.py` covers provider selection in the MCP server, and `test/cli/test_launch.py` makes sure the CLI auto-detects profile providers during `cao launch`.

## Next Steps
1. Exercise Codex CLI flows end-to-end (including approvals) once integration tests exist so we can validate `codex_cli` in scheduled runs.
2. Document any required Codex auth steps or config profiles in `docs/` if contributors report environment setup hurdles.
