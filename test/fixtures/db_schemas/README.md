# Released database schemas

These DDL-only fixtures are generated offline from each tagged release's own
`init_db()` with isolated homes. Run `python test/fixtures/db_schemas/generate.py`
from the repository root after installing dev dependencies with `uv sync --all-extras --group dev`.
The generator creates detached scratch worktrees, uses a clean subprocess
environment and removes each worktree when done. SQL files contain no data.
The chained fixture runs each tag in order against one database.
