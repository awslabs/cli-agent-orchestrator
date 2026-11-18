"""J7: clean up stale sessions (P3), including the NEGATIVE journey.

Negative journey: 3 stale sessions and 1 live one; tick ONE stale session, confirm, and assert through the
CAO that exactly that session is gone and nothing else was touched.
"""

from __future__ import annotations

import re

import helpers as h
from playwright.sync_api import expect


def test_j7_cleanup_dialog_starts_unticked_and_cancel_sends_nothing(shelf_on_fake, fake) -> None:
    page = shelf_on_fake
    button = page.get_by_role("button", name=re.compile(r"Clean up stale \(9\)"))
    expect(button).to_be_visible(timeout=40000)
    button.click()
    dialog = page.get_by_role("dialog")
    boxes = dialog.get_by_role("checkbox")
    expect(boxes.first).to_be_visible(timeout=20000)
    assert boxes.count() == 9
    assert all(not boxes.nth(i).is_checked() for i in range(boxes.count()))
    expect(dialog.get_by_role("button", name=re.compile(r"^Stop \d+ session"))).to_be_disabled()
    dialog.get_by_role("button", name="Cancel").click()
    assert fake.writes() == []


def test_j7_negative_fake_tick_one_of_nine_exactly_one_delete(shelf_on_fake, fake) -> None:
    page = shelf_on_fake
    page.get_by_role("button", name=re.compile(r"Clean up stale \(9\)")).click()
    dialog = page.get_by_role("dialog")
    boxes = dialog.get_by_role("checkbox")
    expect(boxes.first).to_be_visible(timeout=20000)
    label = boxes.nth(2).locator("xpath=ancestor::label").locator("b").inner_text()
    boxes.nth(2).check()
    dialog.get_by_role("button", name=re.compile(r"^Stop 1 session")).click()
    h.wait_until(lambda: fake.writes(), 20, what="the delete call")
    assert fake.writes() == [("DELETE", f"/sessions/cao-{label}")]


def test_j7_negative_scratch_three_stale_one_live_exactly_one_gone(
    shelf_on_scratch, scratch
) -> None:
    page = shelf_on_scratch
    stale = ["e2e-stale-a", "e2e-stale-b", "e2e-stale-c"]
    for name in stale:
        scratch.session(name)
        scratch.backdate(name, 60)  # test-only rewrite of terminals.last_active in the scratch DB
    scratch.session("e2e-live")
    before = scratch.names()
    h.open_shelf(page)
    button = page.get_by_role("button", name=re.compile(r"Clean up stale \(3\)"))
    expect(button).to_be_visible(timeout=60000)
    button.click()
    dialog = page.get_by_role("dialog")
    boxes = dialog.get_by_role("checkbox")
    expect(boxes.first).to_be_visible(timeout=20000)
    assert boxes.count() == 3
    assert all(not boxes.nth(i).is_checked() for i in range(3))
    pick = dialog.get_by_text("e2e-stale-b", exact=False).first
    pick.locator("xpath=ancestor::label").get_by_role("checkbox").check()
    dialog.get_by_role("button", name=re.compile(r"^Stop 1 session")).click()
    h.wait_until(
        lambda: "cao-e2e-stale-b" not in scratch.names(), 40, what="the ticked session to be gone"
    )
    after = scratch.names()
    assert sorted(set(before) - set(after)) == ["cao-e2e-stale-b"], (before, after)
    assert set(after) == set(before) - {"cao-e2e-stale-b"}
