#!/usr/bin/env python3
"""Look at the CAO Shelf page WITHOUT Kiro Crew: fake (or scratch) CAO + the real backend + a static host.

    python3 e2e/rig/dev_server.py [--cao fake|scratch|URL] [--day busy|quiet] [--port 8765] [--theme light|dark]

Starts, as children of this process only:
  * a fake CAO (e2e/rig/fake_cao.py, ``--time rebased``) unless --cao scratch|URL is given,
  * the real backend (backend/server.py) on an ephemeral loopback port with a random proxy secret and its
    data in $SHELF_SCRATCH/work-c/dev-data,
  * this HTTP server on 127.0.0.1:<port>: ``/`` is a host page with an import map, ``/vendor/*`` the React
    and SDK-stub modules (built once with esbuild from ui/node_modules), ``/bundle/index.mjs`` the built page,
    and ``/apps/cao-shelf/api/*`` a signing proxy to the backend (what the gateway does).
Ctrl-C (or SIGTERM) stops exactly the children it started. It needs ui/bundle/index.mjs (make build-ui) and
ui/node_modules (cd ui && npm ci) once.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLE = HERE.parents[1]
SCRATCH = Path(os.environ.get("SHELF_SCRATCH", str(Path.home() / "kc-shelf-e2e")))
WORK = SCRATCH / "work-c"
VENDOR = WORK / "dev-vendor"
PREFIX = "/apps/cao-shelf/api"

# The host's own tokens, copied from Kiro Crew v0.12.2 website/src/styles/design-tokens.css: the radius
# and font lines of the first :root (49, 47-48 without the script fallbacks), the -text rules (213-214,
# 229-230), and the colour, border and shadow lines of the dark (231-256) and light (257-282) blocks.
# The -text tokens key on [data-mode="light"], so <html> carries data-mode as the host's does.
TOKENS = """
:root{--radius-sm:6px;--radius-md:8px;--radius-lg:12px;--radius-xl:16px;
--font-body:'Space Grotesk',-apple-system,BlinkMacSystemFont,sans-serif;--mono:'JetBrains Mono',ui-monospace,SFMono-Regular,monospace}
:root{--warn-text:var(--warn)}[data-mode="light"]{--warn-text:color-mix(in srgb, var(--warn) 50%, #000)}
:root{--ok-text:var(--ok);--danger-text:var(--danger);--info-text:var(--info)}
[data-mode="light"]{--ok-text:color-mix(in srgb, var(--ok) 60%, #000);--danger-text:color-mix(in srgb, var(--danger) 55%, #000);--info-text:color-mix(in srgb, var(--info) 58%, #000)}
:root,[data-theme="dark"]{
--bg:#12141a;--bg-accent:#14161d;--bg-elevated:#1a1d25;--bg-hover:#262a35;
--card:#181b22;--card-fg:#f4f4f5;--panel:#12141a;--panel-strong:#1a1d25;
--text:#e4e4e7;--text-strong:#fafafa;--muted:#909098;--muted-fg:#000;--muted-strong:#52525b;
--border:#27272a;--border-strong:#3f3f46;--border-hover:#52525b;
--accent:#00d492;--accent-fg:#000;--accent-hover:#34d399;--accent-subtle:rgba(4,117,88,.2);--accent-glow:rgba(4,117,88,.35);--ring:#10b981;
--ok:#22c55e;--ok-fg:#000;--ok-subtle:rgba(34,197,94,.12);
--warn:#eab308;--warn-fg:#000;--warn-subtle:rgba(234,179,8,.12);
--danger:#ef4444;--danger-fg:#000;--danger-subtle:rgba(239,68,68,.12);
--info:#0891b2;--info-fg:#000;
--shadow-sm:0 1px 2px rgba(0,0,0,.2);color-scheme:dark}
[data-theme="light"]{
--bg:#fafafa;--bg-accent:#f5f5f5;--bg-elevated:#ffffff;--bg-hover:#f0f0f0;
--card:#ffffff;--card-fg:#18181b;--panel:#fafafa;--panel-strong:#f5f5f5;
--text:#3f3f46;--text-strong:#18181b;--muted:#6d6d75;--muted-fg:#fff;--muted-strong:#52525b;
--border:#e4e4e7;--border-strong:#d4d4d8;--border-hover:#a1a1aa;
--accent:#047558;--accent-fg:#fff;--accent-hover:#059669;--accent-subtle:rgba(4,117,88,.12);--accent-glow:rgba(4,117,88,.18);--ring:#047558;
--ok:#16a34a;--ok-fg:#000;--ok-subtle:rgba(22,163,74,.1);
--warn:#a16207;--warn-fg:#fff;--warn-subtle:rgba(161,98,7,.1);
--danger:#dc2626;--danger-fg:#fff;--danger-subtle:rgba(220,38,38,.1);
--info:#0891b2;--info-fg:#000;
--shadow-sm:0 1px 2px rgba(0,0,0,.06);color-scheme:light}
"""

HTML = """<!doctype html>
<html data-theme="__THEME__" data-mode="__THEME__"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CAO Shelf (dev host)</title>
<style>
__TOKENS__
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 var(--font-body);height:100vh;display:flex;flex-direction:column}
#root{display:flex;flex-direction:column;flex:1;min-height:0}
__HOST_KIT__
</style>
<script type="importmap">__IMPORTMAP__</script></head>
<body><div id="root"></div>
<script type="module">
import React from "react";
import { createRoot } from "react-dom/client";
import App from "/bundle/index.mjs";
createRoot(document.getElementById("root")).render(React.createElement(App));
</script></body></html>
"""

IMPORTS = {
    "react": "/vendor/react.mjs",
    "react-dom": "/vendor/react-dom.mjs",
    "react-dom/client": "/vendor/react-dom-client.mjs",
    "react/jsx-runtime": "/vendor/jsx-runtime.mjs",
    "@kirocrew/app-sdk": "/vendor/app-sdk.mjs",
    "@kirocrew/app-sdk/ui": "/vendor/app-sdk-ui.mjs",
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def build_vendor() -> None:
    esbuild = EXAMPLE / "ui" / "node_modules" / ".bin" / "esbuild"
    if not esbuild.exists():
        sys.exit("dev_server: ui/node_modules is missing; run: cd ui && npm ci")
    VENDOR.mkdir(parents=True, exist_ok=True)
    for old in VENDOR.glob("*.*js"):
        old.unlink()  # only the files this script generated, inside its own scratch directory
    entries = [
        str(HERE / "dev_host" / n)
        for n in (
            "react.mjs",
            "react-dom.mjs",
            "react-dom-client.mjs",
            "jsx-runtime.mjs",
            "app-sdk.mjs",
            "app-sdk-ui.mjs",
        )
    ]
    env = {**os.environ, "NODE_PATH": str(EXAMPLE / "ui" / "node_modules")}
    subprocess.run(
        [
            str(esbuild),
            *entries,
            "--bundle",
            "--splitting",
            "--format=esm",
            f"--outdir={VENDOR}",
            '--define:process.env.NODE_ENV="development"',
            "--out-extension:.js=.mjs",
            "--log-level=warning",
        ],
        check=True,
        env=env,
    )


def request_allowed(host: str | None, origin: str | None, port: int) -> bool:
    """Only the page this server itself serves may use it.

    ``Host`` must be ``127.0.0.1:<port>`` or ``localhost:<port>`` (a rebound hostname fails here)
    and ``Origin``, when sent, must be the matching ``http://`` origin (a form or fetch from another
    site fails here; a request with no ``Origin`` is a plain navigation or a non-browser client).
    """
    names = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if host is None or host.lower() not in names:
        return False
    return origin is None or origin.lower() in {f"http://{n}" for n in names}


class Proxy(BaseHTTPRequestHandler):
    server: "DevServer"

    def _allowed(self) -> bool:
        port = int(self.server.server_address[1])
        if request_allowed(self.headers.get("Host"), self.headers.get("Origin"), port):
            return True
        self._send(403, b"forbidden: this dev server only answers its own page", "text/plain")
        return False

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _api(self, method: str) -> None:
        sys.path.insert(0, str(EXAMPLE / "backend"))
        from cao_shelf.auth import signed_headers  # the helper A documents for the dev harness

        target = "/api" + self.path[len(PREFIX) :]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        headers = signed_headers(self.server.secret, method, target, body)
        headers["Content-Type"] = self.headers.get("Content-Type", "application/json")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.server.backend_port}{target}",
            data=body or None,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self._send(
                    resp.status, resp.read(), resp.headers.get("Content-Type", "application/json")
                )
        except urllib.error.HTTPError as exc:
            self._send(exc.code, exc.read(), exc.headers.get("Content-Type", "application/json"))
        except OSError:
            self._send(
                502,
                b'{"error":{"code":"cao_unreachable","message":"backend not reachable","retryable":true}}',
                "application/json",
            )

    def do_GET(self) -> None:  # noqa: N802
        if not self._allowed():
            return
        path = self.path.split("?", 1)[0]
        if self.path.startswith(PREFIX):
            return self._api("GET")
        if path in ("/", "/apps/cao-shelf"):
            theme = "dark" if "theme=dark" in self.path else self.server.theme
            html = (
                HTML.replace("__THEME__", theme)
                .replace("__TOKENS__", TOKENS)
                .replace("__HOST_KIT__", (HERE / "dev_host" / "host-kit.css").read_text())
                .replace("__IMPORTMAP__", json.dumps({"imports": IMPORTS}))
            )
            return self._send(200, html.encode(), "text/html; charset=utf-8")
        if path == "/bundle/index.mjs":
            return self._send(
                200, (EXAMPLE / "ui" / "bundle" / "index.mjs").read_bytes(), "text/javascript"
            )
        if path.startswith("/vendor/") and "/.." not in path:
            f = VENDOR / path[len("/vendor/") :]
            if f.is_file() and f.resolve().is_relative_to(VENDOR.resolve()):
                return self._send(200, f.read_bytes(), "text/javascript")
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if not self._allowed():
            return
        if self.path.startswith(PREFIX):
            return self._api("POST")
        self._send(404, b"not found", "text/plain")

    def do_PUT(self) -> None:  # noqa: N802
        if not self._allowed():
            return
        if self.path.startswith(PREFIX):
            return self._api("PUT")
        self._send(404, b"not found", "text/plain")


class DevServer(ThreadingHTTPServer):
    daemon_threads = True
    secret = ""
    backend_port = 0
    theme = "light"


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--cao",
        default="fake",
        help="fake (default), scratch (127.0.0.1:9990) or an http://127.0.0.1:<port> URL",
    )
    ap.add_argument("--day", choices=["quiet", "busy"], default="busy")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--theme", choices=["light", "dark"], default="light")
    args = ap.parse_args()
    if args.port in (5476, 9889):
        sys.exit("dev_server: refusing the owner's ports 5476 and 9889")
    if not (EXAMPLE / "ui" / "bundle" / "index.mjs").is_file():
        sys.exit("dev_server: ui/bundle/index.mjs is missing; run: make build-ui")

    children: list[subprocess.Popen] = []

    def stop(*_: object) -> None:
        for proc in children:
            proc.terminate()  # only processes this script started
        for proc in children:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        os._exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    cao_url = args.cao
    if args.cao == "fake":
        fake = subprocess.Popen(
            [
                sys.executable,
                str(HERE / "fake_cao.py"),
                "--day",
                args.day,
                "--time",
                "rebased",
                "--port",
                "0",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        children.append(fake)
        assert fake.stdout is not None
        cao_url = fake.stdout.readline().strip().split("=", 1)[1]
    elif args.cao == "scratch":
        cao_url = "http://127.0.0.1:9990"
    if "9889" in cao_url:
        stop()
        sys.exit("dev_server: refusing the owner's CAO (9889)")

    build_vendor()
    secret = secrets.token_hex(16)
    backend_port = free_port()
    data = WORK / "dev-data"
    data.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(WORK),
        "PORT": str(backend_port),
        "KIROCREW_PROXY_SECRET": secret,
        "CAO_SHELF_CAO_URL": cao_url,
        "CAO_SHELF_DATA_DIR": str(data),
    }
    backend = subprocess.Popen([sys.executable, str(EXAMPLE / "backend" / "server.py")], env=env)
    children.append(backend)

    server = DevServer(("127.0.0.1", args.port), Proxy)
    server.secret, server.backend_port, server.theme = secret, backend_port, args.theme
    print(
        f"DEV_URL=http://127.0.0.1:{args.port}/   (CAO: {cao_url}, backend port {backend_port})",
        flush=True,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    signal.pause()


if __name__ == "__main__":
    main()
