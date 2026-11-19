"""Give the session-approval consent to an App on the ISOLATED Kiro Crew, as its owner.

    owner_enable.py --port 5499 --app cao-shelf --login-url-file <file>

Kiro Crew's CLI refuses this consent on purpose (only the dashboard may grant control of the owner's
chats), so `install.sh` stops with exit 7. The rig is the owner of its own scratch gateway: it exchanges
the scratch login link (from `kc.sh kirocrew token`) for an owner session cookie and calls the owner API
`POST /api/apps/<app>/enable {"sessionApprovalConsent": true}`, the same call the dashboard makes.

Safety: the URL is built here from --port (127.0.0.1 only); 5476, the owner's real gateway, is refused, and a
login link whose host or port differ from --port is refused. The link is read from a file so it never appears
on a command line; nothing here prints it or the cookie.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

OWNER_GATEWAY_PORT = 5476
LOOPBACK = {"127.0.0.1", "localhost"}


def check_link(link: str, port: int) -> str:
    """Return the link's token; refuse a link for another host or port."""
    parsed = urllib.parse.urlparse(link)
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACK or parsed.port != port:
        raise SystemExit(f"owner_enable: the login link is not for 127.0.0.1:{port}; refusing")
    token = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
    if not token:
        raise SystemExit("owner_enable: the login link carries no token")
    return token


def enable(port: int, app: str, token: str, timeout: float = 30.0) -> tuple[int, str]:
    """Exchange the token for an owner cookie, then POST the enable with consent. Returns (status, body)."""
    if port == OWNER_GATEWAY_PORT:
        raise SystemExit(f"owner_enable: port {port} is the owner's real gateway; refusing")
    origin = f"http://localhost:{port}"
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    login = f"{origin}/?token={urllib.parse.quote(token, safe='')}"
    with opener.open(urllib.request.Request(login, method="GET"), timeout=timeout) as resp:
        resp.read()
    if not any(True for _ in jar):
        raise SystemExit("owner_enable: the login link gave no session cookie (expired or used?)")
    body = json.dumps({"sessionApprovalConsent": True}).encode()
    req = urllib.request.Request(
        f"{origin}/api/apps/{urllib.parse.quote(app, safe='')}/enable",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Origin": origin, "Referer": origin + "/"},
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--app", required=True)
    ap.add_argument("--login-url-file", required=True)
    args = ap.parse_args(argv)
    if args.port == OWNER_GATEWAY_PORT:
        print(f"owner_enable: port {args.port} is the owner's real gateway; refusing", file=sys.stderr)
        return 2
    with open(args.login_url_file, encoding="utf-8") as fh:
        lines = [ln.strip() for ln in fh if ln.strip().startswith("http")]
    if not lines:
        print("owner_enable: no login link in the file", file=sys.stderr)
        return 2
    token = check_link(lines[-1], args.port)
    status, text = enable(args.port, args.app, token)
    if status != 200:
        print(f"owner_enable: enable {args.app} -> HTTP {status}: {text[:300]}", file=sys.stderr)
        return 1
    print(f"owner_enable: {args.app} enabled with session-approval consent on 127.0.0.1:{args.port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
