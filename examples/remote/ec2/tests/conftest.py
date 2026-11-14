"""Fixtures for the remote-EC2 sample's tests (helpers: remote_ec2_support.py).

The helpers live in a uniquely named module, not here, because several
example test folders have a conftest.py and `import conftest` would be
ambiguous when they run together.
"""

from __future__ import annotations

import os

import pytest
from remote_ec2_support import sandbox  # noqa: F401  (a pytest fixture)


@pytest.fixture(autouse=True)
def fixed_umask():
    """Every test (and every script it runs) sees umask 022, whatever the host's.

    The scripts refuse group-writable folders, so a host umask of 002 would
    otherwise make the fake roots the tests build look unsafe.
    """
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)
