"""J9 and J10: screenshots at 320, 390 and 1280 px in light and dark, plus a no-horizontal-overflow check.

Images go to e2e/artifacts/screens/ (git-ignored); workstream D copies the ones it wants into its docs.
"""

from __future__ import annotations

import helpers as h
import pytest
from playwright.sync_api import expect

WIDTHS = [(320, 700), (390, 800), (1280, 900)]


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width,height", WIDTHS)
def test_screens_busy_day(browser, login_state, fake, scheme: str, width: int, height: int) -> None:
    ctx = browser.new_context(
        storage_state=login_state, viewport={"width": width, "height": height}, color_scheme=scheme
    )
    page = ctx.new_page()
    try:
        page.goto(h.GATEWAY + "/", wait_until="domcontentloaded")
        h.point_app_at(page, fake.url)
        h.open_shelf(page)
        expect(h.card(page, "payments-fix")).to_be_visible(timeout=40000)
        page.wait_for_timeout(500)
        theme = page.evaluate("document.documentElement.dataset.theme")
        assert theme.endswith(scheme), theme
        root_overflow = page.evaluate(
            """() => { const r = document.querySelector('.cs-root'); return r ? r.scrollWidth - r.clientWidth : 0; }"""
        )
        assert root_overflow <= 1, f"horizontal overflow of {root_overflow}px at {width}px"
        out = h.ARTIFACTS / "screens"
        out.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out / f"busy-{scheme}-{width}.png"), full_page=False)
    finally:
        ctx.close()
