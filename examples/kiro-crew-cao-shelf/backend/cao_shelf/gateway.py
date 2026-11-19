"""The calls CAO Shelf makes to its own Kiro Crew gateway, for remote-control relay chats.

* The gateway's address is ``KIROCREW_GATEWAY_ORIGIN`` (set by Kiro Crew) and must be ``http`` on
  127.0.0.1 or ::1 with a port. When Kiro Crew also sets ``KIROCREW_GATEWAY_ORIGIN_PROOF`` it must
  be the HMAC of the origin under the App secret. Otherwise no call is made.
* The App secret (``KIROCREW_PROXY_SECRET``) is exchanged for an app token with
  ``POST /api/apps/<app>/token``; the token is kept in memory until a call answers 401, then
  fetched again once. Neither is ever stored or logged.
* Connections are loopback only, redirects and environment proxies are refused (the CAO client's
  opener), and responses are capped at 4 MiB.
* Logs carry the method, the route template and the status, never a slot key, a title, a message
  or a token.

The calls: create a chat slot on the ``cao-remote-control`` agent, set its trust mode to
``normal``, post an app-authored turn, delete a slot, read a slot's rows. It approves nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Mapping, Optional

from .cao import build_opener
from .errors import ShelfError

log = logging.getLogger("cao_shelf.gateway")

AGENT_NAME = "cao-remote-control"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
READ_TIMEOUT = 20.0
SLOT_ROWS_LIMIT = 200
ALLOWED_MODES = frozenset({"normal"})
_SLOT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}")
_APP_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_CODE_RE = re.compile(r"[a-z0-9_]{1,64}")


def check_slot(slot: Any) -> str:
    """A slot key, validated before it goes into a URL."""
    if not isinstance(slot, str) or _SLOT_RE.fullmatch(slot) is None:
        raise ShelfError("invalid_request", "invalid chat slot key")
    return slot


def _loopback_origin(origin: str) -> Optional[str]:
    """The origin when it is ``http://127.0.0.1:<port>`` or ``http://[::1]:<port>``, else None."""
    try:
        parts = urllib.parse.urlsplit(origin)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "http" or port is None or parts.username or parts.password:
        return None
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    try:
        if not ipaddress.ip_address(parts.hostname or "").is_loopback:
            return None
    except ValueError:
        return None
    return f"{parts.scheme}://{parts.netloc}"


class GatewayClient:
    """Kiro Crew chat calls as this App. ``opener`` is anything with ``open(request, timeout)``."""

    def __init__(self, environ: Mapping[str, str], *, opener: Optional[Any] = None) -> None:
        raw_origin = environ.get("KIROCREW_GATEWAY_ORIGIN", "")
        self._secret = environ.get("KIROCREW_PROXY_SECRET", "")
        app = environ.get("KIROCREW_APP_NAME", "cao-shelf")
        self._app = app if _APP_RE.fullmatch(app) else "cao-shelf"
        origin = _loopback_origin(raw_origin) if raw_origin else None
        proof = environ.get("KIROCREW_GATEWAY_ORIGIN_PROOF")
        if origin and proof is not None and self._secret:
            wanted = hmac.new(self._secret.encode(), raw_origin.encode(), hashlib.sha256)
            if not hmac.compare_digest(wanted.hexdigest(), proof.strip().lower()):
                origin = None
        self._origin = origin if self._secret else None
        self._port = urllib.parse.urlsplit(self._origin).port if self._origin else None
        self._opener = opener if opener is not None else build_opener()
        self._token: Optional[str] = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        """True when a loopback gateway origin and an App secret are known."""
        return self._origin is not None

    # ------------------------------------------------------------------ transport
    def _unavailable(self) -> ShelfError:
        return ShelfError(
            "gateway_unavailable",
            "the Kiro Crew gateway cannot be reached from this App (it needs a loopback gateway "
            "address and the App secret, which Kiro Crew sets when it starts the App)",
        )

    def _send(
        self,
        method: str,
        path: str,
        template: str,
        body: Optional[Dict[str, Any]],
        headers: Dict[str, str],
    ) -> Any:
        assert self._origin is not None
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self._origin + path,
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": self._origin,
                "User-Agent": "cao-shelf",
                **headers,
            },
        )
        status = 0
        try:
            resp = self._opener.open(req, timeout=READ_TIMEOUT)
            try:
                status = int(getattr(resp, "status", 200))
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
            finally:
                resp.close()
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read(64 * 1024)
            finally:
                exc.close()
            log.info("gateway %s %s %d", method, template, status)
            raise _HttpStatus(status, _error_code(raw)) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.info("gateway %s %s unreachable (%s)", method, template, type(exc).__name__)
            raise ShelfError(
                "gateway_unavailable", "the Kiro Crew gateway did not answer"
            ) from None
        log.info("gateway %s %s %d", method, template, status)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ShelfError("gateway_refused", "Kiro Crew answered with more data than expected")
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            raise ShelfError("gateway_refused", "Kiro Crew answered something that is not JSON")

    def _fetch_token(self) -> str:
        path = f"/api/apps/{self._app}/token"
        try:
            data = self._send(
                "POST", path, "/api/apps/{app}/token", None, {"X-App-Secret": self._secret}
            )
        except _HttpStatus as err:
            raise ShelfError(
                "gateway_refused",
                "Kiro Crew did not give this App a token",
                detail=err.detail(),
            ) from None
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise ShelfError("gateway_refused", "Kiro Crew did not give this App a token")
        return token

    def _call(
        self, method: str, path: str, template: str, body: Optional[Dict[str, Any]] = None
    ) -> Any:
        if not self.available():
            raise self._unavailable()
        for attempt in (1, 2):
            with self._lock:
                if self._token is None:
                    self._token = self._fetch_token()
                token = self._token
            try:
                return self._send(
                    method, path, template, body, {"Cookie": f"mc_token_{self._port}={token}"}
                )
            except _HttpStatus as err:
                if err.status == 401 and attempt == 1:
                    with self._lock:
                        if self._token == token:
                            self._token = None  # expired or rotated: fetch a new one once
                    continue
                if err.status == 404:
                    raise ShelfError(
                        "not_found", "Kiro Crew does not know that chat", detail=err.detail()
                    ) from None
                raise ShelfError(
                    "gateway_refused", "Kiro Crew refused the request", detail=err.detail()
                ) from None
        raise ShelfError("gateway_refused", "Kiro Crew refused the request")

    # ------------------------------------------------------------------ the calls
    def create_slot(self, name: str, title: str) -> str:
        """Create a chat on the ``cao-remote-control`` agent. Returns its slot key."""
        check_slot(name)
        data = self._call(
            "POST",
            "/api/chat/slots",
            "/api/chat/slots",
            {"name": name, "title": title[:200], "agent": AGENT_NAME, "agent_kind": "template"},
        )
        key = data.get("key") if isinstance(data, dict) else None
        if not isinstance(key, str) or _SLOT_RE.fullmatch(key) is None:
            raise ShelfError("gateway_refused", "Kiro Crew did not return the new chat's key")
        return key

    def set_mode(self, slot: str, mode: str) -> None:
        """Set the chat's trust mode. Only ``normal`` is ever sent."""
        if mode not in ALLOWED_MODES:
            raise ShelfError("invalid_request", "relay chats are only ever set to normal")
        data = self._call(
            "POST", "/api/chat/mode", "/api/chat/mode", {"slot": check_slot(slot), "mode": mode}
        )
        if isinstance(data, dict) and data.get("ok") is False:
            raise ShelfError("gateway_refused", "Kiro Crew did not set the chat's trust mode")

    def post_message(self, slot: str, text: str) -> None:
        """Post one app-authored turn (Kiro Crew records it with ``turnActor: app``)."""
        data = self._call(
            "POST", "/api/chat?ws=1", "/api/chat", {"slot": check_slot(slot), "message": text}
        )
        if isinstance(data, dict) and (data.get("ok") is False or "error" in data):
            raise ShelfError("gateway_refused", "Kiro Crew did not accept the message")

    def delete_slot(self, slot: str) -> bool:
        """Delete a chat. Returns False when it was already gone."""
        key = check_slot(slot)
        try:
            path = f"/api/chat/slots/{urllib.parse.quote(key, safe='')}"
            self._call("DELETE", path, "/api/chat/slots/{slot}")
        except ShelfError as err:
            if err.code == "not_found":
                return False
            raise
        return True

    def slot_rows(self, slot: str, limit: int = SLOT_ROWS_LIMIT) -> List[Dict[str, Any]]:
        """The newest rows of a chat. ``not_found`` when the chat no longer exists."""
        key = check_slot(slot)
        data = self._call(
            "GET",
            f"/api/chat/slots/{urllib.parse.quote(key, safe='')}?limit={int(limit)}",
            "/api/chat/slots/{slot}",
        )
        rows = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ShelfError("gateway_refused", "Kiro Crew did not return the chat's rows")
        return [r for r in rows if isinstance(r, dict)]


class _HttpStatus(Exception):
    """A non-2xx answer: its status and Kiro Crew's error code (never its text)."""

    def __init__(self, status: int, code: Optional[str]) -> None:
        super().__init__(status)
        self.status = status
        self.code = code

    def detail(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"gateway_status": self.status}
        if self.code:
            out["gateway_code"] = self.code
        return out


def _error_code(raw: bytes) -> Optional[str]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    code = data.get("code") if isinstance(data, dict) else None
    return code if isinstance(code, str) and _CODE_RE.fullmatch(code) else None
