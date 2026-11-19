"""owner_enable.py: the rig gives session-approval consent on its OWN scratch gateway only."""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterator

import pytest

RIG = Path(__file__).resolve().parent
sys.path.insert(0, str(RIG))

import owner_enable  # noqa: E402


class _Gateway(http.server.BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []

    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        self.calls.append({"method": "GET", "path": self.path})
        self.send_response(200)
        if "token=good" in self.path:
            self.send_header("Set-Cookie", "mc_token=owner; Path=/; HttpOnly")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.calls.append(
            {
                "method": "POST",
                "path": self.path,
                "cookie": self.headers.get("Cookie"),
                "origin": self.headers.get("Origin"),
                "body": json.loads(body or b"{}"),
            }
        )
        ok = self.headers.get("Cookie") == "mc_token=owner"
        self.send_response(200 if ok else 403)
        self.end_headers()
        self.wfile.write(b'{"ok": true}' if ok else b'{"code": "owner_only"}')


@pytest.fixture()
def gateway() -> Iterator[int]:
    _Gateway.calls = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()


def test_enable_exchanges_the_link_for_a_cookie_and_posts_the_consent(gateway: int) -> None:
    status, _ = owner_enable.enable(gateway, "cao-shelf", "good")
    assert status == 200
    post = [c for c in _Gateway.calls if c["method"] == "POST"]
    assert post == [
        {
            "method": "POST",
            "path": "/api/apps/cao-shelf/enable",
            "cookie": "mc_token=owner",
            "origin": f"http://localhost:{gateway}",
            "body": {"sessionApprovalConsent": True},
        }
    ]


def test_a_link_without_a_session_cookie_stops_before_the_enable(gateway: int) -> None:
    with pytest.raises(SystemExit, match="no session cookie"):
        owner_enable.enable(gateway, "cao-shelf", "used")
    assert [c["method"] for c in _Gateway.calls] == ["GET"]


def test_the_owners_real_gateway_port_is_refused() -> None:
    with pytest.raises(SystemExit, match="owner's real gateway"):
        owner_enable.enable(5476, "cao-shelf", "good")


@pytest.mark.parametrize(
    "link",
    [
        "http://localhost:5476/?token=x",
        "http://example.com:5499/?token=x",
        "https://localhost:5499/?token=x",
        "http://localhost:5499/",
    ],
)
def test_a_link_for_another_gateway_or_without_a_token_is_refused(link: str) -> None:
    with pytest.raises(SystemExit):
        owner_enable.check_link(link, 5499)


def test_the_cli_refuses_port_5476_and_never_prints_the_link() -> None:
    with tempfile.NamedTemporaryFile("w", dir=Path.home(), delete=False) as fh:
        fh.write("http://localhost:5476/?token=secret-value\n")
    try:
        proc = subprocess.run(
            [sys.executable, str(RIG / "owner_enable.py"), "--port", "5476", "--app", "x"]
            + ["--login-url-file", fh.name],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        os.unlink(fh.name)
    assert proc.returncode == 2
    assert "secret-value" not in proc.stdout + proc.stderr


def test_install_app_passes_the_scratch_cao_url_and_gives_consent_on_exit_7() -> None:
    text = (RIG / "kiro_crew.sh").read_text(encoding="utf-8")
    assert '--cao-url "$cao_url"' in text
    assert 'cao_url="http://127.0.0.1:$CAO_PORT"' in text
    assert '"$rc" -eq 7' in text and "owner_enable" in text
