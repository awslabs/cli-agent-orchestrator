"""Shared configuration for the runtime channel tests (#745).

A test that starts a real uvicorn server (the ``server`` fixture, used by
``http`` and ``start_runtime`` too) is an integration test, as in
``test/api/test_workflow_lifecycle_realserver.py``: the contributor suite
(``uv run pytest``) leaves it out, and CI (``-m "not e2e"``) runs it. Run them
locally with ``uv run pytest test/runtime_channel -m "not e2e"``.
"""

from pathlib import Path

import pytest

_HERE = Path(__file__).parent


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    # Before the marker selection runs. Called with every collected item, so
    # only this directory's are marked.
    for item in items:
        if _HERE in item.path.parents and "server" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.integration)
