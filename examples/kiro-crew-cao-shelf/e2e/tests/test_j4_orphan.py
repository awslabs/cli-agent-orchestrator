"""J4: a helper whose caller was deleted shows an orphan note (P2)."""

from __future__ import annotations

import helpers as h
from playwright.sync_api import expect


def test_j4_orphan_fake_fixture(shelf_on_fake) -> None:
    expect(h.card(shelf_on_fake, "payments-fix")).to_be_visible(timeout=40000)
    h.show_tier(shelf_on_fake, "Stale")  # search-tuning is idle 52 h, so it is in the stale tier
    c = h.card(shelf_on_fake, "search-tuning")
    expect(c.locator(".cs-note-orphan")).to_be_visible(timeout=40000)


def test_j4_orphan_scratch_real(shelf_on_scratch, scratch) -> None:
    page = shelf_on_scratch
    scratch.session("e2e-orphan")
    scratch.orphan("e2e-orphan")
    h.open_shelf(page)
    c = h.card(page, "e2e-orphan")
    expect(c.locator(".cs-note-orphan")).to_be_visible(timeout=60000)
    expect(c.locator(".cs-note-orphan")).to_contain_text("caller no longer exists")


def test_j4_conductor_deleted_scratch_real(shelf_on_scratch, scratch) -> None:
    """The conductor's terminal is deleted and its helpers stay: no conductor, Ask off, orphans flagged."""
    page = shelf_on_scratch
    ids = scratch.session("e2e-nocond", helpers=2)
    scratch.cao.call("DELETE", f"/terminals/{ids['conductor']}")
    before = len(scratch.cao.session("e2e-nocond")["terminals"])
    h.open_shelf(page)
    c = h.card(page, "e2e-nocond")
    expect(c).to_contain_text("no conductor", timeout=60000)
    expect(c.get_by_role("button", name="Ask")).to_be_disabled()
    expect(c.locator(".cs-note-orphan").first).to_contain_text("caller no longer exists")
    expect(c.get_by_text("ORPHAN").first).to_be_visible()
    assert (
        len(scratch.cao.session("e2e-nocond")["terminals"]) == before
    )  # nothing was sent or stopped
