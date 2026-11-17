"""J5: start a session through the dialog (P3)."""

from __future__ import annotations

import helpers as h
from playwright.sync_api import expect


def _first_profile(page) -> str:
    status, body = h.api(page, "GET", "/profiles")
    assert status == 200 and body["profiles"], f"GET /api/profiles -> {status} {body}"
    return body["profiles"][0]["name"]


def _open_start(page):
    page.get_by_role("button", name="Start session").first.click()
    dialog = page.get_by_role("dialog", name="Start a session")
    expect(dialog).to_be_visible()
    trigger = dialog.get_by_role("combobox", name="Profile")
    expect(trigger).to_be_enabled(timeout=20000)
    return dialog, trigger


def _start(page, name: str) -> None:
    dialog, trigger = _open_start(page)
    # the dialog preselects the first profile, and the host's Radix select must keep it
    expect(trigger).to_contain_text(_first_profile(page))
    # the host Select: a combobox trigger with its list in a portal, not a native <select>
    h.pick_option(page, dialog, "Profile", "developer")
    dialog.get_by_label("Provider (optional)").fill("mock_cli")
    dialog.get_by_label("Working directory (required)").fill(h.work_dir())
    dialog.get_by_label("Session name (optional)").fill(name)
    dialog.get_by_role("button", name="Start", exact=True).click()


def test_j5_start_fake_posts_one_create_call(shelf_on_fake, fake) -> None:
    page = shelf_on_fake
    expect(page.get_by_role("button", name="Start session").first).to_be_visible(timeout=40000)
    _start(page, "e2e-started")
    h.wait_until(
        lambda: [c for c in fake.calls() if c["method"] == "POST" and c["path"] == "/sessions"],
        20,
        what="create call",
    )
    creates = [c for c in fake.calls() if c["method"] == "POST" and c["path"] == "/sessions"]
    assert len(creates) == 1 and creates[0]["query"]["session_name"].endswith("e2e-started")
    expect(h.card(page, "e2e-started")).to_be_visible(timeout=30000)


def test_j5_start_scratch_creates_a_real_session(shelf_on_scratch, scratch) -> None:
    page = shelf_on_scratch
    scratch.session("e2e-first")  # so the page shows the shelf, not the empty state
    h.open_shelf(page)
    expect(h.card(page, "e2e-first")).to_be_visible(timeout=40000)
    _start(page, "e2e-started")
    scratch.created.append("e2e-started")
    h.wait_until(lambda: "cao-e2e-started" in scratch.names(), 40, what="session in CAO")
    expect(h.card(page, "e2e-started")).to_be_visible(timeout=40000)


def test_j5_start_keeps_the_preselected_profile(shelf_on_fake, fake) -> None:
    """On the real host the Radix select once reported '' and undid the preselected profile."""
    page = shelf_on_fake
    expect(page.get_by_role("button", name="Start session").first).to_be_visible(timeout=40000)
    dialog, trigger = _open_start(page)
    first = _first_profile(page)
    expect(trigger).to_contain_text(first)
    page.wait_for_timeout(1000)  # the reset came after the list loaded; give it time to show
    expect(trigger).to_contain_text(first)
    start = dialog.get_by_role("button", name="Start", exact=True)
    expect(start).to_be_disabled()
    dialog.get_by_label("Working directory (required)").fill(h.work_dir())
    expect(start).to_be_enabled()  # no pick needed
    assert [c for c in fake.calls() if c["method"] == "POST"] == []


def test_j5_escape_in_the_open_profile_list_closes_only_the_list(shelf_on_fake, fake) -> None:
    """Gandalf ruling 6, condition 2: Escape in the Radix list must not reach the Modal."""
    page = shelf_on_fake
    expect(page.get_by_role("button", name="Start session").first).to_be_visible(timeout=40000)
    dialog, trigger = _open_start(page)
    trigger.click()
    listbox = page.get_by_role("listbox")
    expect(listbox).to_be_visible()
    page.keyboard.press("Escape")
    expect(listbox).to_be_hidden()
    # Escape must close only the open list: give a wrongly propagated Escape time to act, then prove
    # the dialog is still live by typing into it and reading the value back
    page.wait_for_timeout(600)
    expect(page.get_by_role("dialog", name="Start a session")).to_have_count(1)
    cwd = dialog.get_by_label("Working directory (required)")
    cwd.fill("/still-open", timeout=2000)
    expect(cwd).to_have_value("/still-open")
    cwd.fill("")  # clean again, so Escape may close it
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog", name="Start a session")).to_have_count(0)
    assert [c for c in fake.calls() if c["method"] == "POST"] == []
