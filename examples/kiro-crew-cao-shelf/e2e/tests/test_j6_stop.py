"""J6: stop one session from its card (P3). Only the ticked session may go."""

from __future__ import annotations

import re

import helpers as h
from playwright.sync_api import expect


def _stop_one(page, name: str) -> None:
    c = h.card(page, name)
    expect(c).to_be_visible(timeout=40000)
    c.get_by_role("button", name=re.compile(r"^Stop")).click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    red = dialog.get_by_role("button", name=re.compile(r"^Stop \d+ session"))
    expect(red).to_be_disabled()  # nothing ticked yet
    dialog.get_by_role("checkbox").first.check()
    expect(red).to_be_enabled()
    red.click()


def test_j6_stop_fake_sends_one_delete_for_the_named_session(shelf_on_fake, fake) -> None:
    _stop_one(shelf_on_fake, "migrate-db")
    h.wait_until(lambda: fake.writes(), 20, what="a delete call")
    assert fake.writes() == [("DELETE", "/sessions/cao-migrate-db")]


def test_j6_stop_scratch_removes_exactly_that_session(shelf_on_scratch, scratch) -> None:
    page = shelf_on_scratch
    scratch.session("e2e-stop")
    scratch.session("e2e-keep")
    h.open_shelf(page)
    _stop_one(page, "e2e-stop")
    h.wait_until(
        lambda: "cao-e2e-stop" not in scratch.names(), 40, what="the stopped session to leave CAO"
    )
    assert "cao-e2e-keep" in scratch.names()
