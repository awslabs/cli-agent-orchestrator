"""A read-only client for CAO's REST API on loopback.

* ``http`` on 127.0.0.1, ``localhost`` or ``::1`` only (``validate_cao_url``).
* Redirects are refused, environment proxies are ignored, responses are capped at 4 MiB.
* Connect timeout 2 s, read timeout 10 s (a caller may pass a longer read timeout).
* Every id and name is validated before it is placed in a URL.
* CAO failures are mapped to the error codes of api-contract.md.

This class only ever sends ``GET``. The write calls (inbox message, input, start, stop) live in
``actions.py``, behind the steering endpoints, in a subclass that widens ``ALLOWED_METHODS``.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional

from .errors import ShelfError

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
CONNECT_TIMEOUT = 2.0
READ_TIMEOUT = 10.0
MAX_SESSIONS = 500
MAX_TERMINALS = 200
MAX_PROFILES = 500
SESSION_PREFIX = "cao-"

_LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1")
_CAO_NAME_RE = re.compile(r"cao-[A-Za-z0-9][A-Za-z0-9_-]{0,58}")
_TERMINAL_ID_RE = re.compile(r"[a-f0-9]{8}")
_OUTPUT_MODES = ("full", "last")


def _is_loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def validate_cao_url(url: Any) -> str:
    """Return the normalised base URL, or raise ``forbidden_host``.

    Only ``http`` with a loopback host, no credentials, no path, query or fragment. ``localhost``
    is resolved and every address it resolves to must be loopback.
    """

    def refuse(why: str) -> ShelfError:
        return ShelfError("forbidden_host", f"cao_url is not allowed: {why}")

    if not isinstance(url, str) or not url or len(url) > 200:
        raise refuse("empty or too long")
    try:
        parts = urllib.parse.urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise refuse("not a valid URL") from None
    if parts.scheme != "http":
        raise refuse("only http is allowed")
    if parts.username or parts.password:
        raise refuse("no credentials")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise refuse("no path, query or fragment")
    host = (parts.hostname or "").lower()
    if host not in _LOOPBACK_NAMES:
        raise refuse("host must be 127.0.0.1, localhost or ::1")
    if host == "localhost":
        try:
            found = {
                info[4][0] for info in socket.getaddrinfo(host, port or 80, type=socket.SOCK_STREAM)
            }
        except OSError:
            raise refuse("localhost does not resolve") from None
        if not found or not all(_is_loopback(str(a)) for a in found):
            raise refuse("localhost does not resolve to loopback")
    shown = f"[{host}]" if ":" in host else host
    return f"http://{shown}:{port}" if port else f"http://{shown}"


class _LoopbackConnection(http.client.HTTPConnection):
    """Connects with a short connect timeout, then reads with the request's timeout, and refuses a
    peer that is not on loopback even if a name resolved elsewhere."""

    def connect(self) -> None:
        read_timeout = self.timeout
        sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT)
        if not _is_loopback(str(sock.getpeername()[0])):
            sock.close()
            raise ConnectionRefusedError("peer is not on loopback")
        sock.settimeout(read_timeout if isinstance(read_timeout, (int, float)) else READ_TIMEOUT)
        self.sock = sock


class _LoopbackHandler(urllib.request.HTTPHandler):
    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(_LoopbackConnection, req)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def build_opener() -> urllib.request.OpenerDirector:
    """An opener with no proxies, no redirects, loopback-only connections and no cookies."""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _LoopbackHandler(), _NoRedirect()
    )


def cao_session_name(bare: str) -> str:
    """``payments-fix`` -> ``cao-payments-fix``. Always adds the prefix: a bare name that itself
    starts with ``cao-`` names a different session (``cao-cao-...``), never the one without it."""
    name = SESSION_PREFIX + bare if isinstance(bare, str) else ""
    if _CAO_NAME_RE.fullmatch(name) is None:
        raise ShelfError("invalid_request", "invalid session name")
    return name


def check_cao_name(name: Any) -> str:
    """A full CAO session name (``cao-...``), validated before it goes into a URL."""
    if not isinstance(name, str) or _CAO_NAME_RE.fullmatch(name) is None:
        raise ShelfError("invalid_request", "invalid session name")
    return name


def check_terminal_id(tid: Any) -> str:
    if not isinstance(tid, str) or _TERMINAL_ID_RE.fullmatch(tid) is None:
        raise ShelfError("invalid_request", "invalid terminal id")
    return tid


class CaoClient:
    """Reads from CAO. ``opener`` is anything with ``open(request, timeout=...)``."""

    ALLOWED_METHODS: FrozenSet[str] = frozenset({"GET"})

    def __init__(
        self,
        base_url: str,
        opener: Optional[Any] = None,
        *,
        observer: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.base_url = validate_cao_url(base_url)
        self._opener = opener if opener is not None else build_opener()
        self._observer = observer

    # ------------------------------------------------------------------ transport
    def _request(
        self,
        method: str,
        path: str,
        query: Optional[Mapping[str, Any]] = None,
        *,
        body: Optional[Any] = None,
        read_timeout: float = READ_TIMEOUT,
    ) -> Any:
        if method not in self.ALLOWED_METHODS:
            raise ShelfError("internal", "this client may not send that kind of request")
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(
                [(k, str(v)) for k, v in query.items() if v is not None],
                quote_via=urllib.parse.quote,
            )
        data = None
        headers = {"Accept": "application/json", "User-Agent": "cao-shelf"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        if self._observer is not None:
            self._observer(method, path)
        try:
            resp = self._opener.open(req, timeout=read_timeout)
            try:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
            finally:
                resp.close()
        except urllib.error.HTTPError as exc:
            exc.close()
            raise self._http_error(exc.code) from None
        except urllib.error.URLError as exc:
            raise self._transport_error(exc.reason) from None
        except (TimeoutError, OSError, http.client.HTTPException) as exc:
            raise self._transport_error(exc) from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ShelfError("cao_error", "CAO answered with more data than this app accepts")
        try:
            return json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            raise ShelfError(
                "cao_incompatible", "CAO answered with something that is not JSON"
            ) from None

    @staticmethod
    def _transport_error(reason: Any) -> ShelfError:
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return ShelfError("cao_timeout", "CAO did not answer in time")
        return ShelfError("cao_unreachable", "CAO is not reachable")

    @staticmethod
    def _http_error(code: int) -> ShelfError:
        if code in (401, 403):
            return ShelfError("cao_auth_required", "CAO requires authentication")
        if code == 404:
            return ShelfError("not_found", "CAO does not know that session or terminal")
        if 300 <= code < 400:
            return ShelfError("cao_error", "CAO answered with a redirect, which is refused")
        return ShelfError("cao_error", f"CAO answered {code}", detail={"cao_status": code})

    # ------------------------------------------------------------------ reads
    def health(self) -> Dict[str, Any]:
        data = self._request("GET", "/health")
        if not isinstance(data, dict):
            raise ShelfError("cao_incompatible", "CAO /health is not an object")
        return data

    def list_sessions(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "/sessions")
        if not isinstance(data, list):
            raise ShelfError("cao_incompatible", "CAO /sessions is not a list")
        return [
            r for r in data[:MAX_SESSIONS] if isinstance(r, dict) and isinstance(r.get("name"), str)
        ]

    def get_session(self, cao_name: str) -> Dict[str, Any]:
        name = check_cao_name(cao_name)
        data = self._request("GET", f"/sessions/{name}")
        terminals = data.get("terminals") if isinstance(data, dict) else None
        if not isinstance(terminals, list):
            raise ShelfError("cao_incompatible", "CAO session has no terminals list")
        data["terminals"] = [t for t in terminals[:MAX_TERMINALS] if isinstance(t, dict)]
        return data  # type: ignore[no-any-return]

    def get_terminal(self, terminal_id: str) -> Dict[str, Any]:
        tid = check_terminal_id(terminal_id)
        data = self._request("GET", f"/terminals/{tid}")
        if not isinstance(data, dict):
            raise ShelfError("cao_incompatible", "CAO terminal is not an object")
        return data

    def get_output(self, terminal_id: str, mode: str = "full") -> str:
        tid = check_terminal_id(terminal_id)
        if mode not in _OUTPUT_MODES:
            raise ShelfError("invalid_request", "invalid output mode")
        data = self._request("GET", f"/terminals/{tid}/output", {"mode": mode})
        text = data.get("output") if isinstance(data, dict) else None
        if not isinstance(text, str):
            raise ShelfError("cao_incompatible", "CAO output has no text")
        return text

    def inbox_messages(
        self,
        terminal_id: str,
        *,
        limit: int = 100,
        order: str = "desc",
        sender_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        tid = check_terminal_id(terminal_id)
        if order not in ("asc", "desc") or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ShelfError("invalid_request", "invalid inbox query")
        if sender_id is not None:
            check_terminal_id(sender_id)
        data = self._request(
            "GET",
            f"/terminals/{tid}/inbox/messages",
            {"limit": limit, "order": order, "sender_id": sender_id},
        )
        if not isinstance(data, list):
            raise ShelfError("cao_incompatible", "CAO inbox is not a list")
        return [r for r in data[:100] if isinstance(r, dict)]

    def list_profiles(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "/agents/profiles")
        if not isinstance(data, list):
            raise ShelfError("cao_incompatible", "CAO profiles is not a list")
        return [
            r for r in data[:MAX_PROFILES] if isinstance(r, dict) and isinstance(r.get("name"), str)
        ]
