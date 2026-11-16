"""Keep the real host sampler (a background thread and, on macOS, ``sysctl``) out of tests.

Any test that drives ``/agui/v1/stream`` reaches ``host_resources.sample_host``. Without this
fixture the first such call would start a real sampler thread. ``test_host_resources.py`` and
the runner-id registry test exercise the real sampler on purpose and opt out here.
"""

from __future__ import annotations

import pytest

#: Modules that drive the real sampler themselves.
REAL_SAMPLER_MODULES = frozenset({"test_host_resources", "test_runner_id_projections"})


class NoThreadHostSampler:
    """Same surface as ``HostSampler``; never starts a thread and never probes."""

    def sample(self):
        return []

    def refresh(self):
        return []

    def reset(self):
        return None


@pytest.fixture(autouse=True)
def no_real_host_sampler(request, monkeypatch):
    if request.module.__name__.rsplit(".", 1)[-1] in REAL_SAMPLER_MODULES:
        yield
        return
    from cli_agent_orchestrator.services import host_resources

    monkeypatch.setattr(host_resources, "_sampler", NoThreadHostSampler())
    yield
