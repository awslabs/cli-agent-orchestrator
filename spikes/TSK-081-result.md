# TSK-081 — Task 10 result

## Files touched
- `pyproject.toml`
- `test/smoke/__init__.py`
- `test/smoke/README.md`
- `test/smoke/conftest.py`
- `test/smoke/test_wezterm_basics.py`
- `test/smoke/test_claude_startup.py`
- `test/smoke/test_codex_direct_spawn.py`
- `test/smoke/test_inbox_poller.py`

## pytest marker registration
```toml
[tool.pytest.ini_options]
markers = [
    "asyncio: marks tests that use asyncio",
    "integration: marks integration tests",
    "e2e: marks end-to-end tests",
    "slow: marks tests as slow (deselect with '-m \"not slow\"')",
    "smoke: opt-in tests that require real wezterm + provider CLIs on PATH; not run by default",
]
asyncio_mode = "strict"
testpaths = ["test"]
python_files = "test_*.py"
python_classes = "Test*"
python_functions = "test_*"
addopts = "--cov=src --cov-report=term-missing -m 'not e2e and not smoke'"
```

## Smoke tests added
- `test_spawn_send_get_kill`: spawn a real WezTerm pane, send text, read history, and kill the session.
- `test_trust_prompt_acceptance`: launch Claude in WezTerm, accept the trust prompt, and wait for the idle prompt.
- `test_codex_direct_spawn_two_step_send`: launch Codex through `build_launch_spec`, send `/help`, and verify the command lands.
- `test_pipe_pane_captures_rapid_output`: attach `pipe_pane`, emit rapid markers, and confirm the polled log captures all of them.

## Default-run verification
- full (excl. e2e): 1107 pass / 43 fail / 16 skip — matches the required 43 failures; smoke tests not run.

## Opt-in collection
- `pytest --collect-only -q` smoke line count: 0
- `pytest test/smoke -m smoke --collect-only -q`: 4 tests

## Deviations
- Preserved the existing default `not e2e` gate by merging smoke exclusion into the existing marker expression as `not e2e and not smoke`.
- The literal whole-suite collection command on Windows reports 0 smoke lines because the new default `addopts` excludes smoke tests during collection.
- Whole-suite `--collect-only` also hits a pre-existing Windows collection error in `test/api` (`ModuleNotFoundError: No module named 'fcntl'`). Scoped opt-in smoke collection succeeds and lists the four smoke tests.
