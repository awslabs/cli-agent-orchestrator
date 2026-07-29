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

import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from cli_agent_orchestrator.tui.server_client import ServerClient

# TEXT status literals (NFR-6, BR-8). Rendered verbatim — never a colour code.
INSTALLED_YES = "yes"
INSTALLED_NO = "no"

# How long a successful provider read stays fresh (FR-6.1). The footer is
# re-rendered on EVERY repaint, so an uncached read means a blocking HTTP GET per
# keystroke. Five seconds keeps the line meaningfully live while collapsing a
# burst of repaints into one round-trip.
PREFLIGHT_TTL_SECONDS = 5.0

# Per-request timeout for the pre-flight read (FR-6.2). Deliberately far below
# ``server_client.DEFAULT_TIMEOUT`` (10.0): this read sits on the repaint path, so
# even a first or cache-expiring read must not be able to freeze the UI for ten
# seconds. A timeout degrades to the existing "server not reachable" footer text.
PREFLIGHT_TIMEOUT_SECONDS = 2.0

# Monotonic clock seam. ``ProviderPreflight`` reads the time only through the
# callable it was constructed with, so a test can advance the TTL window
# deterministically instead of sleeping.
Clock = Callable[[], float]


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

    def __init__(
        self,
        client: Optional[ServerClient] = None,
        *,
        ttl_seconds: float = PREFLIGHT_TTL_SECONDS,
        clock: Optional[Clock] = None,
    ) -> None:
        """Build a pre-flight helper.

        Args:
            client: The read-only server client. Defaults to a fresh
                :class:`ServerClient` bound to the constants-derived base URL.
            ttl_seconds: How long a successful read stays fresh (FR-6.1).
                Defaults to :data:`PREFLIGHT_TTL_SECONDS`.
            clock: Monotonic clock used to age the cache. Defaults to
                :func:`time.monotonic`. Injectable so a test advances the TTL
                window deterministically rather than sleeping — the class never
                calls a clock inline.
        """

        self._client = client or ServerClient()
        self._ttl_seconds = ttl_seconds
        self._clock: Clock = clock if clock is not None else time.monotonic
        # ``None`` == cold cache. Only SUCCESSFUL reads are cached; a failure must
        # be retried on the next repaint rather than pinned for the TTL window.
        self._cached_rows: Optional[List[PreflightRow]] = None
        self._cached_at: float = 0.0

    def invalidate(self) -> None:
        """Drop any cached rows so the next :meth:`rows` call re-reads."""

        self._cached_rows = None
        self._cached_at = 0.0

    def rows(self) -> List[PreflightRow]:
        """Return one :class:`PreflightRow` per endpoint-reported provider.

        Maps each ``{name, binary, installed}`` to a row whose ``installed_text``
        is the TEXT "yes"/"no" (NFR-6). Order is preserved from the endpoint.

        **Cached with a TTL (FR-6.1).** This is called from the footer's text
        provider, which runs on every repaint; an uncached call meant a blocking
        HTTP GET per keystroke. A call inside the :data:`PREFLIGHT_TTL_SECONDS`
        window performs **no I/O at all** and returns the memoised rows. Only
        successful reads are cached — a failure is re-attempted next repaint.

        The read is issued with :data:`PREFLIGHT_TIMEOUT_SECONDS` rather than the
        client's shared :data:`~.server_client.DEFAULT_TIMEOUT` (FR-6.2), so even
        a cold or expiring read cannot freeze the UI for ten seconds.

        Raises:
            ServerUnavailable: if cao-server is unreachable (BR-5); the caller
                renders the unreachable state. ``ServerAuthRequired`` is a
                subclass of it and reaches callers that branch on the narrower
                type.
            ServerClientError: if the providers payload is malformed (BR-6).
        """

        now = self._clock()
        cached = self._cached_rows
        if cached is not None and (now - self._cached_at) < self._ttl_seconds:
            return list(cached)

        # NOTE: the read goes through ``self._client`` (not the value the context
        # manager yields) on purpose — a ``MagicMock`` client yields a *different*
        # mock from ``__enter__``, which would move the call off the double the
        # test asserts against.
        with self._client.bounded_timeout(PREFLIGHT_TIMEOUT_SECONDS):
            providers = self._client.providers()

        rows = [
            PreflightRow(
                name=provider.name,
                binary=provider.binary,
                installed_text=INSTALLED_YES if provider.installed else INSTALLED_NO,
            )
            for provider in providers
        ]
        self._cached_rows = rows
        self._cached_at = now
        return list(rows)
