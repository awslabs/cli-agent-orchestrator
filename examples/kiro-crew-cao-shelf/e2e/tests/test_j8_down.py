"""J8: CAO goes away: banner, no 'no sessions' lie, and the shelf recovers when CAO is back."""

from __future__ import annotations

import re

import helpers as h
from playwright.sync_api import expect


def test_j8_fake_down_banner_then_recovers(shelf_on_fake, fake) -> None:
    page = shelf_on_fake
    expect(h.card(page, "payments-fix")).to_be_visible(timeout=40000)
    fake.mode("down")
    expect(
        page.get_by_role("alert")
        .filter(has_text=re.compile("not reachable|Cannot reach", re.I))
        .first
    ).to_be_visible(timeout=60000)
    expect(page.get_by_text("No sessions", exact=False)).to_have_count(0)
    # the age of the snapshot is counted on the page: it is printed, and it is not a number frozen by the backend
    expect(page.get_by_role("alert").first).to_contain_text(re.compile(r"last snapshot, .+ old"))
    fake.mode("ok")
    expect(
        page.get_by_role("alert").filter(has_text=re.compile("not reachable|Cannot reach", re.I))
    ).to_have_count(0, timeout=60000)
    expect(h.card(page, "payments-fix")).to_be_visible()


def test_j8_scratch_cao_killed_and_restarted(shelf_on_scratch, scratch) -> None:
    page = shelf_on_scratch
    scratch.session("e2e-down")
    h.open_shelf(page)
    expect(h.card(page, "e2e-down")).to_be_visible(timeout=40000)
    h.rig(
        "cao.sh", "stop"
    )  # by recorded pid; this also ends the scratch tmux server and its sessions
    scratch.created.clear()
    expect(
        page.get_by_role("alert")
        .filter(has_text=re.compile("not reachable|Cannot reach", re.I))
        .first
    ).to_be_visible(timeout=60000)
    h.rig("cao.sh", "start")
    expect(
        page.get_by_role("alert").filter(has_text=re.compile("not reachable|Cannot reach", re.I))
    ).to_have_count(0, timeout=60000)
