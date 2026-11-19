"""J2: ask a conductor (P3). The message goes through the inbox with sender cao-shelf."""

from __future__ import annotations

import re

import helpers as h
from playwright.sync_api import expect


def _ask(page, name: str, text: str) -> None:
    c = h.card(page, name)
    expect(c).to_be_visible(timeout=40000)
    c.get_by_role("button", name=re.compile(r"^Ask")).click()
    box = c.get_by_placeholder("Ask the conductor...")
    box.fill(text)
    c.get_by_role("button", name=re.compile(r"^Send")).click()
    expect(c.get_by_role("button", name="Sent")).to_be_visible(timeout=20000)


def test_j2_ask_fake_posts_exactly_one_inbox_message(shelf_on_fake, fake) -> None:
    fake.calls()
    _ask(shelf_on_fake, "migrate-db", "generic status please")
    posts = [c for c in fake.calls() if c["method"] == "POST"]
    assert [p["path"] for p in posts] == ["/terminals/f782a383/inbox/messages"]
    assert posts[0]["query"]["sender_id"] == "cao-shelf"


def test_j2_ask_scratch_conductor_receives_and_answers(shelf_on_scratch, scratch) -> None:
    page = shelf_on_scratch
    ids = scratch.session("e2e-ask")
    h.open_shelf(page)
    _ask(page, "e2e-ask", "generic status please")
    rows = h.wait_until(lambda: scratch.inbox(ids["conductor"]), 20, what="inbox row")
    assert rows[0]["sender_id"] == "cao-shelf"
    assert rows[0]["message"] == "generic status please"
    h.wait_until(
        lambda: scratch.terminal_status(ids["conductor"]) in {"completed", "idle"},
        30,
        what="conductor to finish the message",
    )


def test_j2_ask_a_busy_conductor_says_queued_not_sent(shelf_on_fake, fake) -> None:
    page = shelf_on_fake
    c = h.card(page, "payments-fix")  # its conductor is processing: CAO keeps the message pending
    expect(c).to_be_visible(timeout=40000)
    c.get_by_role("button", name=re.compile(r"^Ask")).click()
    c.get_by_placeholder("Ask the conductor...").fill("generic status please")
    c.get_by_role("button", name=re.compile(r"^Send")).click()
    expect(
        c.get_by_text(re.compile("Message queued: the conductor of payments-fix is busy"))
    ).to_be_visible(timeout=20000)
    expect(c.get_by_role("button", name="Sent")).to_have_count(0)
