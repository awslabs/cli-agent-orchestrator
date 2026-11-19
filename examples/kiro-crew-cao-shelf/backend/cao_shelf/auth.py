"""Verification of the gateway's proxy signatures (v1, and v2 with a nonce cache).

v1: ``X-KiroCrew-Proxy: <ts>:<hmac>`` over ``ts:method:target:sha256(body)``.
v2: ``X-KiroCrew-Proxy-V2: <ts>:<hmac>`` and ``X-KiroCrew-Proxy-Nonce`` over
``v2:ts:nonce:method:target:sha256(body)``. A request needs both: v1 and v2 must verify and the
nonce must be new, so a captured request cannot be replayed (a request that carries only v1 is
refused, or dropping the v2 headers would defeat the nonce cache). Kiro Crew 0.12.2, the app's
minimum, sends both on every proxied request.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import threading
import time
from typing import Callable, Dict, Optional, Protocol

MAX_SKEW_SECONDS = 60
_TS_RE = re.compile(r"[0-9]{1,12}")
_NONCE_RE = re.compile(r"[0-9a-f]{32}")
# 60 seconds of nonces for 100 requests a second (the page polls once per 5 s per tab).
DEFAULT_NONCE_ENTRIES = 6000

HEADER_V1 = "X-KiroCrew-Proxy"
HEADER_V2 = "X-KiroCrew-Proxy-V2"
HEADER_NONCE = "X-KiroCrew-Proxy-Nonce"


class Headers(Protocol):
    """Anything with a case-insensitive ``get``, such as ``http.client.HTTPMessage``."""

    def get(self, name: str, /) -> Optional[str]: ...


def _digest(secret: str, message: str) -> str:
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def body_hash(body: bytes) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def sign_v1(secret: str, ts: int, method: str, target: str, body: bytes) -> str:
    return f"{ts}:{_digest(secret, f'{ts}:{method}:{target}:{body_hash(body)}')}"


def sign_v2(secret: str, ts: int, nonce: str, method: str, target: str, body: bytes) -> str:
    message = f"v2:{ts}:{nonce}:{method}:{target}:{body_hash(body)}"
    return f"{ts}:{_digest(secret, message)}"


def signed_headers(
    secret: str,
    method: str,
    target: str,
    body: bytes = b"",
    *,
    now: Optional[float] = None,
    nonce: Optional[str] = None,
    v2: bool = True,
) -> Dict[str, str]:
    """Headers the gateway would add. For tests and the dev harness; never used by the server."""
    ts = int(now if now is not None else time.time())
    headers = {HEADER_V1: sign_v1(secret, ts, method, target, body)}
    if v2:
        nonce = (
            nonce
            or hmac.new(b"n", f"{ts}{time.monotonic_ns()}".encode(), "sha256").hexdigest()[:32]
        )
        headers[HEADER_NONCE] = nonce
        headers[HEADER_V2] = sign_v2(secret, ts, nonce, method, target, body)
    return headers


class ProxyVerifier:
    """Checks every request except ``/health``. An empty secret refuses everything."""

    def __init__(
        self,
        secret: str,
        *,
        max_entries: int = DEFAULT_NONCE_ENTRIES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._secret = secret
        self._max = max_entries
        self._clock = clock
        self._seen: Dict[str, float] = {}
        self._lock = threading.Lock()

    def _fresh(self, ts_text: str, now: float) -> bool:
        return _TS_RE.fullmatch(ts_text) is not None and abs(now - int(ts_text)) <= MAX_SKEW_SECONDS

    def verify(self, method: str, target: str, body: bytes, headers: Headers) -> bool:
        if not self._secret:
            return False
        now = self._clock()
        v1 = headers.get(HEADER_V1) or ""
        ts, sep, given = v1.partition(":")
        if not sep or not self._fresh(ts, now):
            return False
        want = _digest(self._secret, f"{ts}:{method}:{target}:{body_hash(body)}")
        if not hmac.compare_digest(given.encode(), want.encode()):
            return False
        # v2 is required. Kiro Crew 0.12.2 and later (the app's minimum) send the nonce and the v2
        # signature on every proxied request; accepting v1 alone would let anyone who captured one
        # request replay it for 60 seconds by dropping the two v2 headers.
        v2 = headers.get(HEADER_V2)
        nonce = headers.get(HEADER_NONCE)
        if v2 is None or nonce is None or _NONCE_RE.fullmatch(nonce) is None:
            return False
        ts2, sep2, given2 = v2.partition(":")
        if not sep2 or not self._fresh(ts2, now):
            return False
        want2 = _digest(self._secret, f"v2:{ts2}:{nonce}:{method}:{target}:{body_hash(body)}")
        if not hmac.compare_digest(given2.encode(), want2.encode()):
            return False
        with self._lock:
            for key in [k for k, expiry in self._seen.items() if expiry < now]:
                del self._seen[key]
            if nonce in self._seen or len(self._seen) >= self._max:
                return False  # replay, or full: fail closed rather than evict a live nonce
            self._seen[nonce] = int(ts2) + MAX_SKEW_SECONDS
        return True
