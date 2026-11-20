"""Screenshots at 1280 and 800 px of the states the review fixes changed (images go to
e2e/artifacts/screens/, git-ignored): the CAO-down banner, a stale session with an orphan, and a
session whose conductor was deleted. They are for a person to look at; the assertions only check
that the state is on the page and that nothing overflows sideways."""

from __future__ import annotations

import re

import helpers as h
import pytest
from playwright.sync_api import expect

SIZES = [(1280, 900), (800, 900)]


def _shot(browser, login_state, width: int, height: int, name: str, prepare):
    ctx = browser.new_context(
        storage_state=login_state, viewport={"width": width, "height": height}, color_scheme="light"
    )
    page = ctx.new_page()
    try:
        page.goto(h.GATEWAY + "/", wait_until="domcontentloaded")
        prepare(page)
        page.wait_for_timeout(500)
        overflow = page.evaluate(
            """() => { const r = document.querySelector('.cs-root'); return r ? r.scrollWidth - r.clientWidth : 0; }"""
        )
        assert overflow <= 1, f"horizontal overflow of {overflow}px at {width}px"
        out = h.ARTIFACTS / "screens"
        out.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out / f"{name}-{width}.png"), full_page=False)
    finally:
        ctx.close()


@pytest.mark.parametrize("width,height", SIZES)
def test_screen_cao_down_banner(browser, login_state, fake, width: int, height: int) -> None:
    def prepare(page) -> None:
        h.point_app_at(page, fake.url)
        h.open_shelf(page)
        expect(h.card(page, "payments-fix")).to_be_visible(timeout=40000)
        fake.mode("down")
        banner = page.get_by_role("alert").filter(has_text=re.compile("not reachable", re.I)).first
        expect(banner).to_be_visible(timeout=60000)
        expect(banner).to_contain_text(re.compile(r"last snapshot, .+ old"))
        expect(h.card(page, "payments-fix")).to_be_visible()  # the dimmed last snapshot stays

    _shot(browser, login_state, width, height, "cao-down", prepare)


@pytest.mark.parametrize("width,height", SIZES)
def test_screen_orphan_on_the_stale_tier(
    browser, login_state, fake, width: int, height: int
) -> None:
    def prepare(page) -> None:
        h.point_app_at(page, fake.url)
        h.open_shelf(page)
        h.show_tier(page, "Stale")
        c = h.card(page, "search-tuning")
        expect(c.locator(".cs-note-orphan")).to_be_visible(timeout=40000)
        c.scroll_into_view_if_needed()

    _shot(browser, login_state, width, height, "orphan", prepare)


@pytest.mark.parametrize("width,height", SIZES)
def test_screen_no_conductor(browser, login_state, scratch, width: int, height: int) -> None:
    ids = scratch.session("e2e-shot-nocond", helpers=2)
    scratch.cao.call("DELETE", f"/terminals/{ids['conductor']}")

    def prepare(page) -> None:
        h.point_app_at(page, scratch.url)
        h.open_shelf(page)
        c = h.card(page, "e2e-shot-nocond")
        expect(c).to_contain_text("no conductor", timeout=60000)
        expect(c.get_by_role("button", name="Ask")).to_be_disabled()

    _shot(browser, login_state, width, height, "no-conductor", prepare)
