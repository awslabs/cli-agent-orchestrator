# Verification Checklist

Complete checklist for testing, code quality, security, and documentation before committing a new provider. Nothing ships until every box is checked.

---

## Testing

### Unit Tests
- [ ] All unit tests pass: `uv run pytest test/providers/test_<provider>_unit.py -v`
- [ ] Coverage >90%: `uv run pytest test/providers/test_<provider>_unit.py --cov=src/cli_agent_orchestrator/providers/<provider>.py --cov-report=term-missing -v`
- [ ] Tall-terminal test included (`test_get_status_idle_tall_terminal`)
- [ ] CAO_TERMINAL_ID forwarding tests included

### Full Unit Suite (No Regressions)
- [ ] `uv run pytest test/ -v --ignore=test/e2e --ignore=test/providers/test_q_cli_integration.py`

### E2E Tests — New Provider
- [ ] `uv run pytest -m e2e test/e2e/test_handoff.py -v -k <provider>`
- [ ] `uv run pytest -m e2e test/e2e/test_assign.py -v -k <provider>`
- [ ] `uv run pytest -m e2e test/e2e/test_send_message.py -v -k <provider>`

### E2E Tests — All Providers (No Regressions)
- [ ] `uv run pytest -m e2e test/e2e/ -v`
- [ ] Verify handoff, assign, send_message pass for: kimi_cli, claude_code, codex, kiro_cli, AND new provider

---

## Code Quality

- [ ] Formatting: `uv run black --check src/ test/`
- [ ] Import sorting: `uv run isort --check-only src/ test/`
- [ ] Type checking: `uv run mypy src/`

---

## Security

- [ ] Dependency audit: `uv run pip-audit` OR `trivy fs --severity HIGH,CRITICAL .`
- [ ] No secrets in code (no API keys, tokens, credentials in committed files)
- [ ] `shlex.join()` used for command building (no shell injection)
- [ ] Temp files created securely (`tempfile.mkdtemp`) and cleaned up in `cleanup()`

---

## Code Comments

- [ ] Module docstring explains the CLI tool, its key characteristics, and status detection strategy
- [ ] Every pattern constant has a comment explaining what it matches and why
- [ ] `IDLE_PROMPT_TAIL_LINES` comment explains the padding behavior and why the value was chosen
- [ ] `_build_command()` documents CAO_TERMINAL_ID forwarding
- [ ] `get_status()` documents the detection priority order
- [ ] `extract_last_message_from_script()` documents the extraction strategy and thinking filter

---

## Documentation Updates

A new provider requires updates to **all** of these files. Check each one carefully for consistency — no stale values, no duplication, no missing entries.

### New Files
- [ ] `docs/<provider>.md` — Full provider documentation
- [ ] `.github/workflows/test-<provider>.yml` — CI workflow
- [ ] `test/providers/fixtures/<provider>_*.txt` — 5 test fixture files
- [ ] `test/providers/test_<provider>_unit.py` — Unit tests

### Updated Files
- [ ] `README.md` — Add to provider table + launch examples
- [ ] `CHANGELOG.md` — Add new provider entry under `[Unreleased]` or version section
- [ ] `CODEBASE.md` — Add `<provider>.py` to directory structure AND provider list in architecture diagram
- [ ] `DEVELOPMENT.md` — Update total test count, add provider test commands, add E2E commands, add to prerequisites, add to project structure, add docs entry
- [ ] `docs/api.md` — Add `<provider>` to provider type (appears in 2 places: POST params and terminal object schema)
- [ ] `src/.../providers/base.py` — Add to docstring listing implemented providers
- [ ] `src/.../providers/manager.py` — Add import + elif
- [ ] `src/.../cli/commands/launch.py` — Add to `PROVIDERS_REQUIRING_WORKSPACE_ACCESS`
- [ ] `test/providers/README.md` — Add test section (tests, fixtures, run commands), update structure diagram, CI workflow table, test metrics
- [ ] `test/e2e/conftest.py` — Add `require_<provider>` fixture
- [ ] `test/e2e/test_handoff.py` — Add test class
- [ ] `test/e2e/test_assign.py` — Add test class
- [ ] `test/e2e/test_send_message.py` — Add test class
- [ ] `docs/assets/cao_architecture.mmd` — Add to CLI Tools node
- [ ] `docs/assets/cao_architecture.png` — Re-render: `npx -p @mermaid-js/mermaid-cli mmdc -i docs/assets/cao_architecture.mmd -o docs/assets/cao_architecture.png -s 6 -b transparent`

### Documentation Consistency Checks
- [ ] `IDLE_PROMPT_TAIL_LINES` value in `docs/<provider>.md` matches the actual code constant
- [ ] Test count in `DEVELOPMENT.md` matches actual `pytest` output
- [ ] Test count in `test/providers/README.md` matches actual count
- [ ] Provider name used consistently everywhere (no typos, no mixups between display name and enum value)
- [ ] No duplicate entries (e.g., provider listed twice in a table)
- [ ] Architecture PNG resolution matches or exceeds the original dimensions
