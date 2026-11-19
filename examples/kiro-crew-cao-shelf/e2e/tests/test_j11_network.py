"""J11: while the shelf is open, the browser talks only to the gateway: never to a CAO port, never off-box."""

from __future__ import annotations

from urllib.parse import urlparse

import helpers as h
from playwright.sync_api import expect


def test_j11_no_request_leaves_the_gateway_origin(page, fake) -> None:
    h.point_app_at(page, fake.url)
    seen: list[str] = []
    page.on("request", lambda r: seen.append(r.url))
    page.add_init_script("""window.__fetched = []; const f = window.fetch;
           window.fetch = function(...a){ try { window.__fetched.push(String(a[0] instanceof Request ? a[0].url : a[0])); } catch(e){}
           return f.apply(this, a); };""")
    h.open_shelf(page)
    expect(h.card(page, "payments-fix")).to_be_visible(timeout=40000)
    page.wait_for_timeout(12000)  # two polls
    gateway = urlparse(h.GATEWAY)
    foreign = [
        u
        for u in seen
        if urlparse(u).scheme in ("http", "https") and urlparse(u).netloc != gateway.netloc
    ]
    assert foreign == [], f"requests that left the gateway origin: {foreign}"
    ports = {urlparse(u).port for u in seen}
    assert h.CAO_PORT not in ports and 9889 not in ports and urlparse(fake.url).port not in ports
    app_calls = [u for u in seen if "/apps/cao-shelf/api/" in u]
    assert app_calls, "the page made no App API request at all"
    for u in seen:
        path = urlparse(u).path
        if "cao-shelf" not in path:
            continue
        # the App's own calls, the dashboard's own manifest/bundle fetches, and the page itself
        assert (
            path.startswith("/apps/cao-shelf/api/")
            or path.startswith("/api/apps/cao-shelf")
            or path.startswith("/apps/cao-shelf/art/")
            or path.rstrip("/") == "/apps/cao-shelf"
            or path.endswith((".mjs", ".js", ".css"))
        ), u
    fetched = page.evaluate("window.__fetched")
    for u in fetched:
        parsed = urlparse(u)
        assert parsed.netloc in ("", gateway.netloc), u
