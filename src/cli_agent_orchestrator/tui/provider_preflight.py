"""Provider pre-flight display for the ``cao tui`` thin shell (U4).

The pre-flight footer answers one question: which CLI provider binaries are
installed and on ``PATH``? Its data comes from **exactly one** source — the live
``GET /agents/providers`` endpoint via :class:`~.server_client.ServerClient`
(BR-2 / FR-5.2 / ADR-002). It never reads ``constants.PROVIDERS`` (which carries
the internal ``mock_cli``) nor the web layer's ``FALLBACK_PROVIDERS`` (which
carries the phantom ``q_cli``/``gemini_cli`` names that no longer ship). Because
the endpoint is the sole source, those names can never appear in a pre-flight
row by construction.

Install status renders as the TEXT "yes"/"no" (NFR-6) — never colour alone, so
the shell stays legible on a monochrome terminal and to a screen reader.

Import rule (thin shell, enforced by ``test/tui/test_thin_shell_boundary.py``):
only the standard library and this package's own modules are imported here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from cli_agent_orchestrator.tui.server_client import ServerClient

# TEXT status literals (NFR-6, BR-8). Rendered verbatim — never a colour code.
INSTALLED_YES = "yes"
INSTALLED_NO = "no"


@dataclass(frozen=True)
class PreflightRow:
    """A single pre-flight display row (display projection of a ProviderStatus).

    No ``authenticated`` field: FU-1 is deferred (=A) and PATH/install is the
    only signal the ``f570de1`` contract carries (BR-3).
    """

    name: str
    binary: str
    installed_text: str


class ProviderPreflight:
    """Builds the provider pre-flight rows from the live endpoint only.

    Sole-source guarantee (BR-2): :meth:`rows` calls
    :meth:`ServerClient.providers` and maps its result verbatim. No static or
    frontend provider list is consulted, so ``mock_cli``/``q_cli``/``gemini_cli``
    cannot leak into the output.
    """

    def __init__(self, client: Optional[ServerClient] = None) -> None:
        """Build a pre-flight helper.

        Args:
            client: The read-only server client. Defaults to a fresh
                :class:`ServerClient` bound to the constants-derived base URL.
        """

        self._client = client or ServerClient()

    def rows(self) -> List[PreflightRow]:
        """Return one :class:`PreflightRow` per endpoint-reported provider.

        Maps each ``{name, binary, installed}`` to a row whose ``installed_text``
        is the TEXT "yes"/"no" (NFR-6). Order is preserved from the endpoint.

        Raises:
            ServerUnavailable: if cao-server is unreachable (BR-5); the caller
                renders the unreachable state.
            ServerClientError: if the providers payload is malformed (BR-6).
        """

        return [
            PreflightRow(
                name=provider.name,
                binary=provider.binary,
                installed_text=INSTALLED_YES if provider.installed else INSTALLED_NO,
            )
            for provider in self._client.providers()
        ]
