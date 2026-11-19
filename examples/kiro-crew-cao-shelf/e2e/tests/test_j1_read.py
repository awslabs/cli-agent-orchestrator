"""J1: read the shelf (fake CAO busy day, and the scratch real CAO), and answer a waiting helper (P3)."""

from __future__ import annotations

import re

import helpers as h
from playwright.sync_api import expect


def test_j1_read_fake_busy_day(shelf_on_fake, fake) -> None:
    page = shelf_on_fake
    expect(page.get_by_text(re.compile(r"NEEDS YOU \(1\)", re.I))).to_be_visible(timeout=30000)
    expect(page.get_by_text(re.compile(r"LIVE NOW \(1\)", re.I))).to_be_visible()
    expect(page.get_by_text(re.compile(r"PARKED, IDLE UNDER 48 H \(1\)", re.I))).to_be_visible()
    expect(page.get_by_role("button", name=re.compile(r"Clean up stale \(9\)"))).to_be_visible()
    needs = h.card(page, "payments-fix")
    expect(needs).to_be_visible()
    expect(needs).to_contain_text("waiting")
    for text in page.locator(".cs-card[role=article]").all_inner_texts():
        assert not re.search(r"\brunning\b", text, re.I), "a card must never say 'running'"
    # reads only: nothing was written to CAO
    assert fake.writes() == []


def test_j1_read_scratch_real_cao_waiting_mock_cli(shelf_on_scratch, scratch) -> None:
    page = shelf_on_scratch
    ids = scratch.waiting_helper("e2e-read")
    h.open_shelf(page)
    c = h.card(page, "e2e-read")
    expect(c).to_be_visible(timeout=40000)
    expect(c).to_have_attribute("aria-label", re.compile("needs you", re.I), timeout=40000)
    expect(c).to_contain_text("APPROVAL_REQUIRED: push to origin?")
    expect(page.get_by_text(re.compile(r"NEEDS YOU \(\d+\)", re.I))).to_be_visible()
    assert scratch.terminal_status(ids["helpers"][0]) == "waiting_user_answer"


def test_j1_answer_scratch_sends_y_and_the_terminal_leaves_waiting(
    shelf_on_scratch, scratch
) -> None:
    page = shelf_on_scratch
    ids = scratch.waiting_helper("e2e-answer")
    h.open_shelf(page)
    c = h.card(page, "e2e-answer")
    expect(c).to_be_visible(timeout=40000)
    c.get_by_role("button", name="Approve").click()
    h.wait_until(
        lambda: scratch.terminal_status(ids["helpers"][0]) != "waiting_user_answer",
        30,
        what="terminal to leave waiting_user_answer",
    )
    # the shelf no longer lists this session under NEEDS YOU (poll interval is 5 s)
    expect(c).not_to_have_attribute("aria-label", re.compile("needs you", re.I), timeout=40000)
