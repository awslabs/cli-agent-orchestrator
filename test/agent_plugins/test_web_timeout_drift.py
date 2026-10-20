"""The web client's timeout must outlast the server's git budget (finding F6).

``installPlugin`` and ``validatePlugin`` aborted client-side at 120s while the
server's clone budget is ``GIT_TIMEOUT_S = 300``. A clone finishing between 121s
and 300s therefore showed the operator a failure while the backend went on to
commit a full install — and ``validatePlugin`` has the identical mismatch because
validation resolves (clones) the source first. The route awaits
``asyncio.to_thread(...)``, which cannot be cancelled and takes no ``Request``, so
there is no disconnect handling to fall back on: the only honest fix is for the
client budget to exceed the server's.

Two constants in two languages that must move together are exactly the kind of
coupling that rots silently, so this test reads both — importing the Python one and
parsing the TypeScript one — and fails the moment either crosses the other. In-repo
precedent for a cross-file guard: ``test_naming_migration.py`` reads the docs and
``test_packages.py`` byte-diffs trees.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cli_agent_orchestrator.agent_plugins.resolver import GIT_TIMEOUT_S

REPO_ROOT = Path(__file__).resolve().parents[2]
API_TS = REPO_ROOT / "web" / "src" / "api.ts"

#: The two web operations that resolve a source, and therefore may clone.
CLONING_OPERATIONS = ("installPlugin", "validatePlugin")


def _timeout_ms(operation: str) -> int:
    """Parse ``timeoutMs`` out of one operation's ``fetchJSON`` call in api.ts."""
    text = API_TS.read_text(encoding="utf-8")
    start = text.index(f"{operation}: (")
    body = text[start : start + 600]
    match = re.search(r"timeoutMs:\s*([0-9_]+)", body)
    assert match, f"no timeoutMs found for {operation} in {API_TS}"
    return int(match.group(1).replace("_", ""))


class TestClientTimeoutOutlastsTheServer:
    def test_the_api_client_exists_where_this_test_expects_it(self):
        """A moved file must fail loudly rather than silently stop guarding."""
        assert API_TS.is_file()

    @pytest.mark.parametrize("operation", CLONING_OPERATIONS)
    def test_the_client_waits_longer_than_the_git_budget(self, operation):
        client_ms = _timeout_ms(operation)

        assert client_ms > GIT_TIMEOUT_S * 1000, (
            f"web/src/api.ts {operation} aborts at {client_ms}ms while the server's "
            f"GIT_TIMEOUT_S is {GIT_TIMEOUT_S}s: a clone finishing in between shows the "
            f"operator a failure for an install the backend committed."
        )

    @pytest.mark.parametrize("operation", CLONING_OPERATIONS)
    def test_the_margin_is_not_absurd(self, operation):
        """A guard that passes for 24h would stop being a guard."""
        assert _timeout_ms(operation) <= (GIT_TIMEOUT_S + 120) * 1000

    def test_the_comment_names_the_constant_it_tracks(self):
        """The next reader must learn what the number is derived from."""
        text = API_TS.read_text(encoding="utf-8")
        assert "GIT_TIMEOUT_S" in text

    def test_the_non_cloning_operation_is_left_alone(self):
        """`uninstallPlugin` involves no git; widening it would be cargo-culting."""
        assert _timeout_ms("uninstallPlugin") < GIT_TIMEOUT_S * 1000
