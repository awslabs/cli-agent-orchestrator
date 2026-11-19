"""J3: the history drawer lists closed helpers, rebuilt from the conductor's inbox (P2)."""

from __future__ import annotations

import re

import helpers as h
from playwright.sync_api import expect


def test_j3_history_fake_counts_match_the_fixture(shelf_on_fake) -> None:
    c = h.card(shelf_on_fake, "payments-fix")
    expect(c).to_be_visible(timeout=40000)
    expect(c.get_by_role("button", name=re.compile(r"History \(3\)"))).to_be_visible(timeout=20000)


def test_j3_history_scratch_real_closed_helper(shelf_on_scratch, scratch) -> None:
    """A helper that closes AFTER the page's first read still shows when the drawer opens."""
    page = shelf_on_scratch
    scratch.session("e2e-hist")
    h.open_shelf(page)
    c = h.card(page, "e2e-hist")
    expect(c).to_be_visible(timeout=40000)
    h.wait_until(
        lambda: h.api(page, "GET", "/sessions/e2e-hist/history")[0] == 200, what="history read"
    )
    scratch.closed_helper("e2e-hist")
    cao = scratch.cao.session("e2e-hist")
    conductor = cao["terminals"][0]["id"]
    sender = scratch.inbox(conductor)[0]["sender_id"]
    h.wait_until(  # the poller sees the terminal gone, and the 15-second drawer refresh window passes
        lambda: sender not in {t["id"] for t in scratch.cao.session("e2e-hist")["terminals"]}
        and h.api(page, "GET", "/shelf")[0] == 200,
        what="helper gone",
    )
    page.wait_for_timeout(16000)
    c.get_by_role("button", name=re.compile(r"^History")).click()
    expect(c.get_by_text(f"helper {sender[:4]}")).to_be_visible(timeout=30000)
