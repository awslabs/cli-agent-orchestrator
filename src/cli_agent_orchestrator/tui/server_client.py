"""Read-only HTTP seam for the ``cao tui`` thin shell (U4).

This module owns the *only* HTTP surface the TUI uses. :class:`ServerClient`
issues exclusively ``requests.get`` calls against the running ``cao-server``
(``API_BASE_URL``) and projects each JSON response into a small, frozen,
U4-local view model. It performs **no** mutating HTTP verb — every mutation in
the TUI is a shell-out to the ``cao`` CLI (U3), never an in-process POST/PUT/
DELETE/PATCH here (BR-1 / FR-4.1). The set of routes consumed is frozen at the
``f570de1`` contract (NFR-5); adding a verb, a route, or a changed response
shape requires a contract-change escalation, not an edit here.

Import rule (thin shell, enforced by ``test/tui/test_thin_shell_boundary.py``):
only the standard library, ``requests`` and ``cli_agent_orchestrator.constants``
may be imported here. The view models below are deliberately defined *in* this
package and are **not** ``cli_agent_orchestrator.models.*`` (a forbidden heavy
import, F-5).

Error policy:

* Any ``requests.exceptions.RequestException`` (connection refused, timeout, a
  non-2xx status via ``raise_for_status``) → :class:`ServerUnavailable`. The App
  renders the server-unreachable screen; the shell never crashes (BR-5 / FR-9.1).
* An unexpected / missing-key response shape → :class:`ServerClientError`. The
  failure is surfaced, never silently swallowed (BR-6, construction guardrail).
* ``GET /agents/profiles/{name}`` returning 404 → :class:`ProfileNotFound`, so a
  caller can render a "profile not found" message instead of a crash.

Every GET carries a bounded timeout (BR-7): a slow or hung server can never
block the TUI event loop indefinitely.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, NoReturn, Optional
from urllib.parse import quote

import requests

from cli_agent_orchestrator.constants import API_BASE_URL

logger = logging.getLogger(__name__)

# Default per-request timeout (seconds). Bounded I/O — BR-7. A hung server must
# never wedge the TUI event loop; 10s is generous for a localhost read.
DEFAULT_TIMEOUT: float = 10.0


# --------------------------------------------------------------------------- #
# Exceptions                                                                    #
# --------------------------------------------------------------------------- #


class ServerUnavailable(Exception):
    """The cao-server could not be reached (connection error, timeout, non-2xx).

    Raised for any ``requests.exceptions.RequestException`` — the App maps this
    to the server-unreachable screen (S-unreachable). Command building and copy
    keep working while the server is down (FR-9.1).
    """


class ServerAuthRequired(ServerUnavailable):
    """The server answered ``401``/``403`` — it is up, but requires authentication.

    A **subclass** of :class:`ServerUnavailable`, deliberately and not stylistically
    (FR-7.2). Six call sites already catch ``ServerUnavailable``
    (``app.py`` twice, ``navigation.py`` once, ``profiles_view.py`` three times); a
    sibling exception would escape five of them and a 401 raised while browsing
    Profiles would render a raw traceback. Subclassing means every existing catch
    keeps degrading exactly as before, and only the narrower branch — currently
    :meth:`App._preflight_text` — distinguishes *requires authentication* from
    *not reachable*.

    Bounded scope: this PR delivers the **message distinction only**. No token
    plumbing and no credential discovery — the TUI reads no auth variable.
    """


class ServerClientError(Exception):
    """The server answered, but the response shape was not what U4 expected.

    Raised when a payload is the wrong JSON type or is missing a required key.
    Surfaced (not swallowed) so a contract drift is visible rather than silent
    (BR-6).
    """


class ProfileNotFound(Exception):
    """``GET /agents/profiles/{name}`` returned 404 for the requested name.

    A distinct, non-fatal outcome: the caller renders a "profile not found"
    message rather than crashing (mirrors the CLI's 404 handling). Kept separate
    from :class:`ServerUnavailable` (the server *is* reachable) and from
    :class:`ServerClientError` (the response was well-formed, just empty).
    """


# --------------------------------------------------------------------------- #
# View models — frozen, read-only projections of the f570de1 GET responses.     #
# Defined here in tui/, NOT cli_agent_orchestrator.models.* (F-5).              #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HealthInfo:
    """Projection of ``GET /health`` — drives U1's liveness branch."""

    status: str
    service: str = ""
    terminal_backend: str = ""
    components: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderStatus:
    """Projection of one entry of ``GET /agents/providers``.

    No ``authenticated`` field: FU-1 is deferred (=A) and the field is not in
    the ``f570de1`` contract (BR-3). The provider set is exactly the endpoint's
    response — ``mock_cli`` and the web ``FALLBACK_PROVIDERS`` phantoms
    (``q_cli``/``gemini_cli``) are absent by construction (BR-2 / FR-5.2).
    """

    name: str
    binary: str
    installed: bool


@dataclass(frozen=True)
class SessionInfo:
    """Projection of one entry of ``GET /sessions``."""

    id: str
    name: str
    status: str = ""


@dataclass(frozen=True)
class TerminalInfo:
    """Projection of one entry of ``GET /sessions/{name}/terminals``."""

    id: str
    tmux_session: Optional[str] = None
    tmux_window: Optional[str] = None
    provider: Optional[str] = None
    agent_profile: Optional[str] = None
    last_active: Optional[str] = None


@dataclass(frozen=True)
class WorkflowSummary:
    """Projection of one entry of ``GET /workflows``."""

    name: str
    source_path: str = ""
    mode: str = ""
    step_count: Optional[int] = None
    description: str = ""
    indexed_at: str = ""


@dataclass(frozen=True)
class ProfileSummary:
    """Projection of one entry of ``GET /agents/profiles``.

    Displayed under the "Profiles" label (ADR-003 label-only); the API path
    stays ``/agents/...``.
    """

    name: str
    source: str = ""
    loadable: bool = True
    description: str = ""
    role: str = ""
    capabilities: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    duplicated_in: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProfileDetail:
    """Projection of ``GET /agents/profiles/{name}`` (a parsed AgentProfile)."""

    name: str
    description: str = ""
    provider: Optional[str] = None
    role: Optional[str] = None
    system_prompt: Optional[str] = None
    capabilities: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Shape helpers — raise ServerClientError on any unexpected/missing structure.  #
# --------------------------------------------------------------------------- #


def _as_list(value: object, context: str) -> List[Any]:
    """Return ``value`` as a list or raise ServerClientError (BR-6)."""

    if not isinstance(value, list):
        raise ServerClientError(f"{context}: expected a JSON array, got {type(value).__name__}")
    return value


def _as_mapping(value: object, context: str) -> Dict[str, Any]:
    """Return ``value`` as a dict or raise ServerClientError (BR-6)."""

    if not isinstance(value, dict):
        raise ServerClientError(f"{context}: expected a JSON object, got {type(value).__name__}")
    return value


def _require(item: Dict[str, Any], key: str, context: str) -> Any:
    """Return ``item[key]`` or raise ServerClientError on a missing key (BR-6)."""

    if key not in item:
        raise ServerClientError(f"{context}: missing required key '{key}'")
    return item[key]


def _raise_status_error(resp: requests.Response, label: str, exc: BaseException) -> NoReturn:
    """Convert a non-2xx ``raise_for_status`` failure into the right exception.

    A ``401``/``403`` means the server *is* up but demands credentials, so it maps
    to :class:`ServerAuthRequired` (FR-7.1) — the narrower type callers may branch
    on. Because ``ServerAuthRequired`` subclasses :class:`ServerUnavailable`, every
    existing ``except ServerUnavailable`` keeps degrading unchanged (FR-7.2).
    Anything else keeps the pre-existing generic mapping and message shape.

    Args:
        resp: The response whose status was rejected.
        label: The path/context used in the message (message shape preserved).
        exc: The originating ``RequestException``, chained as ``__cause__``.

    Raises:
        ServerAuthRequired: On ``401``/``403``.
        ServerUnavailable: On any other non-2xx status.
    """

    if resp.status_code in (401, 403):
        raise ServerAuthRequired(
            f"cao-server at {label} requires authentication (HTTP {resp.status_code})"
        ) from exc
    raise ServerUnavailable(f"cao-server error for {label}: {exc}") from exc


def _as_str_list(value: object) -> List[str]:
    """Coerce a value to a list of strings (tolerant of a missing/None field)."""

    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


# --------------------------------------------------------------------------- #
# ServerClient                                                                  #
# --------------------------------------------------------------------------- #


class ServerClient:
    """Read-only GET client for the running ``cao-server`` (U4's sole HTTP seam).

    Every method issues a single ``requests.get`` against a ``f570de1`` route
    and returns a frozen view model. There is intentionally no ``post``/``put``/
    ``delete``/``patch`` method anywhere on this class (BR-1). Construct once and
    reuse; the reads are live (no caching — BR / lifecycle: per-call
    projections).
    """

    def __init__(
        self,
        base_url: str = API_BASE_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """Build a client.

        Args:
            base_url: cao-server base URL. Defaults to the constants-derived
                ``API_BASE_URL`` (``CAO_API_HOST``/``CAO_API_PORT`` overridable).
            timeout: Per-request timeout in seconds (BR-7, bounded I/O).
        """

        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    @property
    def timeout(self) -> float:
        """The per-request timeout currently in force (seconds, BR-7)."""

        return self._timeout

    @contextmanager
    def bounded_timeout(self, seconds: float) -> Iterator["ServerClient"]:
        """Temporarily tighten this client's per-request timeout (FR-6.2).

        The App shares ONE :class:`ServerClient` across the pre-flight and the
        profiles browser, so a caller on the repaint path cannot be given its own
        client without splitting that seam. This context manager narrows the
        timeout for the duration of the block and restores the previous value on
        exit (including on an exception), so a read on the repaint path can be
        bounded far below :data:`DEFAULT_TIMEOUT` without affecting the
        off-repaint reads that share the client.

        Args:
            seconds: The timeout to apply inside the block.

        Yields:
            This same client, for convenience.
        """

        previous = self._timeout
        self._timeout = seconds
        try:
            yield self
        finally:
            self._timeout = previous

    # -- low-level GET ------------------------------------------------------ #

    def _get(self, path: str, *, params: Optional[Dict[str, Any]] = None) -> requests.Response:
        """Issue a GET and return the raw response.

        The only HTTP call site in U4. A connection error or timeout →
        :class:`ServerUnavailable` (BR-5). Callers decide whether to
        ``raise_for_status`` (the ``profile`` path inspects 404 first).
        """

        url = f"{self._base_url}{path}"
        try:
            return requests.get(url, params=params, timeout=self._timeout)
        except requests.exceptions.RequestException as exc:
            logger.debug("cao-server GET %s failed: %s", path, exc)
            raise ServerUnavailable(f"cao-server unreachable at {url}: {exc}") from exc

    def _get_json(self, path: str, *, params: Optional[Dict[str, Any]] = None) -> Any:
        """GET, raise_for_status, and parse JSON.

        A non-2xx status (via ``raise_for_status``) is a ``RequestException`` →
        :class:`ServerUnavailable` (BR-5), except ``401``/``403`` which classify to
        the narrower :class:`ServerAuthRequired` subclass first (FR-7.1). A
        non-JSON body → :class:`ServerClientError` (BR-6).
        """

        resp = self._get(path, params=params)
        try:
            resp.raise_for_status()
        except requests.exceptions.RequestException as exc:
            logger.debug("cao-server GET %s returned an error status: %s", path, exc)
            _raise_status_error(resp, path, exc)
        try:
            return resp.json()
        except ValueError as exc:
            raise ServerClientError(f"{path}: response was not valid JSON") from exc

    # -- read methods (GET only) ------------------------------------------- #

    def health(self) -> HealthInfo:
        """``GET /health`` → :class:`HealthInfo` (U1 startup liveness probe)."""

        payload = _as_mapping(self._get_json("/health"), "/health")
        components_raw = payload.get("components")
        components = (
            {str(k): str(v) for k, v in components_raw.items()}
            if isinstance(components_raw, dict)
            else {}
        )
        return HealthInfo(
            status=str(_require(payload, "status", "/health")),
            service=str(payload.get("service", "")),
            terminal_backend=str(payload.get("terminal_backend", "")),
            components=components,
        )

    def providers(self) -> List[ProviderStatus]:
        """``GET /agents/providers`` → list of :class:`ProviderStatus`.

        The SOLE provider source (BR-2 / FR-5.2 / ADR-002). No static list is
        ever read, so ``mock_cli`` and the ``FALLBACK_PROVIDERS`` phantoms cannot
        appear.
        """

        context = "/agents/providers"
        payload = _as_list(self._get_json(context), context)
        rows: List[ProviderStatus] = []
        for raw in payload:
            item = _as_mapping(raw, context)
            rows.append(
                ProviderStatus(
                    name=str(_require(item, "name", context)),
                    binary=str(_require(item, "binary", context)),
                    installed=bool(_require(item, "installed", context)),
                )
            )
        return rows

    def sessions(self) -> List[SessionInfo]:
        """``GET /sessions`` → list of :class:`SessionInfo`."""

        context = "/sessions"
        payload = _as_list(self._get_json(context), context)
        rows: List[SessionInfo] = []
        for raw in payload:
            item = _as_mapping(raw, context)
            rows.append(
                SessionInfo(
                    id=str(_require(item, "id", context)),
                    name=str(item.get("name", _require(item, "id", context))),
                    status=str(item.get("status", "")),
                )
            )
        return rows

    def terminals(self, session: str) -> List[TerminalInfo]:
        """``GET /sessions/{session}/terminals`` → list of :class:`TerminalInfo`.

        ``session`` is URL-encoded into the path (matching the CLI seam). Its
        provider/agent_profile fields are nullable (a plain window has neither).
        """

        context = f"/sessions/{session}/terminals"
        path = f"/sessions/{quote(session, safe='')}/terminals"
        payload = _as_list(self._get_json(path), context)
        rows: List[TerminalInfo] = []
        for raw in payload:
            item = _as_mapping(raw, context)
            rows.append(
                TerminalInfo(
                    id=str(_require(item, "id", context)),
                    tmux_session=_opt_str(item.get("tmux_session")),
                    tmux_window=_opt_str(item.get("tmux_window")),
                    provider=_opt_str(item.get("provider")),
                    agent_profile=_opt_str(item.get("agent_profile")),
                    last_active=_opt_str(item.get("last_active")),
                )
            )
        return rows

    def workflows(self) -> List[WorkflowSummary]:
        """``GET /workflows`` → list of :class:`WorkflowSummary`."""

        context = "/workflows"
        payload = _as_list(self._get_json(context), context)
        rows: List[WorkflowSummary] = []
        for raw in payload:
            item = _as_mapping(raw, context)
            step_count = item.get("step_count")
            rows.append(
                WorkflowSummary(
                    name=str(_require(item, "name", context)),
                    source_path=str(item.get("source_path", "")),
                    mode=str(item.get("mode", "")),
                    step_count=int(step_count) if isinstance(step_count, int) else None,
                    description=str(item.get("description", "")),
                    indexed_at=str(item.get("indexed_at", "")),
                )
            )
        return rows

    def profiles(self) -> List[ProfileSummary]:
        """``GET /agents/profiles`` → list of :class:`ProfileSummary`."""

        context = "/agents/profiles"
        payload = _as_list(self._get_json(context), context)
        rows: List[ProfileSummary] = []
        for raw in payload:
            item = _as_mapping(raw, context)
            rows.append(
                ProfileSummary(
                    name=str(_require(item, "name", context)),
                    source=str(item.get("source", "")),
                    loadable=bool(item.get("loadable", True)),
                    description=str(item.get("description", "")),
                    role=str(item.get("role", "")),
                    capabilities=_as_str_list(item.get("capabilities")),
                    tags=_as_str_list(item.get("tags")),
                    duplicated_in=_as_str_list(item.get("duplicated_in")),
                )
            )
        return rows

    def profile(self, name: str) -> ProfileDetail:
        """``GET /agents/profiles/{name}`` → :class:`ProfileDetail`.

        A 404 → :class:`ProfileNotFound` (renderable "profile not found", not a
        crash). ``name`` is URL-encoded into the path.
        """

        context = f"/agents/profiles/{name}"
        path = f"/agents/profiles/{quote(name, safe='')}"
        resp = self._get(path)
        if resp.status_code == 404:
            raise ProfileNotFound(f"agent profile '{name}' not found")
        try:
            resp.raise_for_status()
        except requests.exceptions.RequestException as exc:
            logger.debug("cao-server GET %s returned an error status: %s", path, exc)
            _raise_status_error(resp, context, exc)
        try:
            body = resp.json()
        except ValueError as exc:
            raise ServerClientError(f"{context}: response was not valid JSON") from exc

        item = _as_mapping(body, context)
        return ProfileDetail(
            name=str(_require(item, "name", context)),
            description=str(item.get("description", "")),
            provider=_opt_str(item.get("provider")),
            role=_opt_str(item.get("role")),
            system_prompt=_opt_str(item.get("system_prompt")),
            capabilities=_as_str_list(item.get("capabilities")),
            tags=_as_str_list(item.get("tags")),
        )


def _opt_str(value: object) -> Optional[str]:
    """Return ``str(value)`` unless the value is ``None`` (a nullable field)."""

    return None if value is None else str(value)
