"""The dev harness signs requests for the backend, so it must only answer its own page (L2).

A web page open in the developer's browser can send a simple cross-site request to
``127.0.0.1:<port>`` (CSRF) or reach it through a rebound hostname; ``Host`` and ``Origin`` tell
those apart from the page the server itself serves.
"""

from __future__ import annotations

import http.client
import http.server
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
import dev_server  # noqa: E402


class _Backend(http.server.BaseHTTPRequestHandler):
    seen: List[Tuple[str, str]] = []

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        type(self).seen.append((self.command, self.path))
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = _answer  # noqa: N815


def _serve(handler: Any) -> http.server.ThreadingHTTPServer:
    last: Optional[Exception] = None
    for port in range(9991, 9996):  # the only ports the tests may use
        try:
            return http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError as exc:
            last = exc
    raise RuntimeError(f"no free test port in 9991-9995: {last}")


@pytest.fixture()
def dev() -> Iterator[Tuple[int, List[Tuple[str, str]]]]:
    _Backend.seen = []
    backend = _serve(_Backend)
    proxy = dev_server.DevServer(("127.0.0.1", 0), dev_server.Proxy)
    proxy.secret, proxy.backend_port = "test-secret", int(backend.server_address[1])
    threads = [
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        for s in (backend, proxy)
    ]
    for t in threads:
        t.start()
    try:
        yield int(proxy.server_address[1]), _Backend.seen
    finally:
        for s in (proxy, backend):
            s.shutdown()
            s.server_close()


def send(port: int, method: str, path: str, headers: Dict[str, str], body: bytes = b"") -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        return conn.getresponse().status
    finally:
        conn.close()


API = "/apps/cao-shelf/api/sessions"


def test_its_own_page_is_answered(dev: Tuple[int, List[Tuple[str, str]]]) -> None:
    port, seen = dev
    own = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}"}
    assert send(port, "GET", API, {"Host": own["Host"]}) == 200
    assert send(port, "POST", API, {**own, "Content-Type": "application/json"}, b"{}") == 200
    assert send(port, "PUT", API, {**own, "Content-Type": "application/json"}, b"{}") == 200
    localhost = {"Host": f"localhost:{port}", "Origin": f"http://localhost:{port}"}
    assert send(port, "GET", API, localhost) == 200
    assert len(seen) == 4


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "evil.example", "Origin": "http://evil.example"},  # a rebound name
        {"Host": "{own}", "Origin": "http://evil.example"},  # a cross-site fetch
        {"Host": "{own}", "Origin": "null"},  # a sandboxed frame
        {"Host": "127.0.0.1:1", "Origin": "http://127.0.0.1:1"},  # another local port
        {"Host": "{own}", "Origin": "http://127.0.0.1"},  # the right host, no port
    ],
)
def test_other_hosts_and_origins_are_refused_and_never_signed(
    dev: Tuple[int, List[Tuple[str, str]]], headers: Dict[str, str]
) -> None:
    port, seen = dev
    given = {k: v.replace("{own}", f"127.0.0.1:{port}") for k, v in headers.items()}
    for method in ("GET", "POST", "PUT"):
        body = b"" if method == "GET" else b'{"working_directory":"/home"}'
        assert send(port, method, API, {**given, "Content-Type": "text/plain"}, body) == 403
    assert send(port, "GET", "/", given) == 403  # the page itself too
    assert seen == []  # nothing reached the backend


def test_the_check_is_a_pure_function() -> None:
    allowed = dev_server.request_allowed
    assert allowed("127.0.0.1:8765", None, 8765)
    assert allowed("LOCALHOST:8765", "http://localhost:8765", 8765)
    assert not allowed(None, None, 8765)
    assert not allowed("127.0.0.1:8765", "https://127.0.0.1:8765", 8765)
    assert not allowed("[::1]:8765", None, 8765)


KIT_NAMES = Path(__file__).resolve().parents[2] / "ui" / "test" / "kit-names.ts"
ESBUILD = Path(__file__).resolve().parents[2] / "ui" / "node_modules" / ".bin" / "esbuild"


@pytest.mark.skipif(not ESBUILD.exists(), reason="ui/node_modules is missing (cd ui && npm ci)")
def test_the_built_dev_vendor_exports_every_kit_name(tmp_path: Path, monkeypatch: Any) -> None:
    """`make dev` links the page against the built stub; a name it lacks breaks the page at load."""
    import re

    monkeypatch.setattr(dev_server, "VENDOR", tmp_path / "dev-vendor")
    dev_server.build_vendor()
    built = (tmp_path / "dev-vendor" / "app-sdk-ui.mjs").read_text()
    exported = set()
    for block in re.findall(r"export\s*\{([^}]*)\}", built):
        for part in block.split(","):
            if part.strip():
                exported.add(part.strip().split(" as ")[-1].strip())
    wanted = set(re.findall(r"'(\w+)'", KIT_NAMES.read_text()))
    assert len(wanted) == 13
    assert wanted <= exported, f"missing from the dev vendor: {sorted(wanted - exported)}"
