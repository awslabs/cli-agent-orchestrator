"""Fixtures for the remote-EC2 sample's tests (helpers: remote_ec2_support.py).

The helpers live in a uniquely named module, not here, because several
example test folders have a conftest.py and `import conftest` would be
ambiguous when they run together.
"""

from __future__ import annotations

from remote_ec2_support import sandbox  # noqa: F401  (a pytest fixture)
