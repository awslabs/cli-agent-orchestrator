"""Shared fixtures for the vault tests."""

import pytest

from cli_agent_orchestrator.services.vault import binding


@pytest.fixture(autouse=True)
def reset_unmapped_project_write_counter():
    """Start each vault test with an empty process-wide unmapped-write counter.

    ``binding`` keeps that counter at module level. Any earlier test in the same
    process (any directory) that performs a native project write leaves it
    non-zero, and ``binding_warnings`` then reports an extra
    ``unmapped_project_write`` warning. Serially that depends on file order; under
    pytest-xdist it depends on which tests a worker was handed (#1408).
    """
    binding._reset_unmapped_project_write_count()
    yield
    binding._reset_unmapped_project_write_count()
