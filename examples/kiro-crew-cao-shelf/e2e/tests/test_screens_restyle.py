"""The page on Kiro Crew's App Kit, on the real host (design section 7, items C7 and C8, and part D).

Everything runs in the isolated Kiro Crew (5499) against the scratch CAO (9990), so the controls are the
host's own: its Card, Btn, Badge, ErrorNotice, Modal and Radix Select, inside the dashboard chrome.

The states are real, not patched: ``payments-fix`` is a scratch mock_cli session whose helper waits for
an answer (Needs you), ``docs-refresh`` has a conductor and a helper busy for 30 min (Live, with the
in-flight line) plus a helper whose caller was deleted (ORPHAN), and ``payments-fix`` is remote
controlled by a real relay chat whose tool request waits for the owner. The scratch home's
Claude Code allow rule for the CAO tools is taken out while this module runs (and put back after), so
the mock model's first tool call asks, which is what raises the pending-request notice. Only the
escaping check patches an answer, to put hostile text into the CAO error the banner shows.

Screenshots of record: e2e/artifacts/screens/restyle-<state>-<scheme>-<width>.png (git-ignored), for
the states page-needs-relay, explainer-open (the "What is remote control?" disclosure open), relay-dialog,
relay-dialog-open and start-remote, at 360 and 800 px in light and dark. Each shot also asserts no horizontal overflow and at least 4.5:1 contrast for the Shelf's own
text, measured in the page against the composited background, and 3:1 for the status dots (design
ruling 7). Host components are host-owned: the ErrorNotice is not measured (its dark figure is 4.07:1
by computation, design section 2), and the host Badges (tier tag, ORPHAN) are recorded and held to 3:1
only, since their colour is the theme's -text token (kiro-dark: about 3.05:1 and 3.57:1).
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from typing import Any, Iterator

import helpers as h
import pytest
from playwright.sync_api import expect
from test_j9_remote_control import (
    OPS,
    SCRATCH_SETTINGS,
    attach,
    gw,
    perm_meta,
    relay_of,
    rows,
    script,
)

NEEDS = "payments-fix"
LIVE = "docs-refresh"
PENDING_SHORT = "A tool request is waiting in the relay chat. Deny it unless you expect it."
OUT = h.ARTIFACTS / "screens"

# the composited background and contrast of an element's text, measured in the page
CONTRAST_JS = """([els, modes]) => {
  const cv = document.createElement('canvas'); cv.width = cv.height = 1
  const g = cv.getContext('2d', { willReadFrequently: true })
  const rgba = (c) => { g.clearRect(0, 0, 1, 1); g.fillStyle = '#000'; g.fillStyle = c; g.fillRect(0, 0, 1, 1)
    const d = g.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2], d[3] / 255] }
  const over = (top, bottom) => [0, 1, 2].map((i) => top[i] * top[3] + bottom[i] * (1 - top[3])).concat([1])
  const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4 }
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]) }
  // mode 'dot': the element's ::before dot (non-text) against the element's background
  return els.map((el, i) => {
    const layers = []
    for (let n = el; n; n = n.parentElement) {
      const c = rgba(getComputedStyle(n).backgroundColor)
      if (c[3] > 0) layers.push(c)
      if (c[3] >= 1) break
    }
    if (!layers.length || layers[layers.length - 1][3] < 1) layers.push([255, 255, 255, 1])
    let bg = layers[layers.length - 1]
    for (let i = layers.length - 2; i >= 0; i--) bg = over(layers[i], bg)
    const ink = modes[i] === 'dot' ? getComputedStyle(el, '::before').backgroundColor : getComputedStyle(el).color
    const fg = over(rgba(ink), bg)
    const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x)
    return { text: (el.textContent || '').trim().slice(0, 40), ratio: (a + 0.05) / (b + 0.05), mode: modes[i] }
  })
}"""

# `.cs-root` and the dialog panel, plus every element inside the panel that clips or scrolls sideways
# (the host's body region is overflow-x hidden, so text clipped there never reaches the panel's own
# scrollWidth). Form controls and the host's deliberate ellipsis truncation are left out.
OVERFLOW_JS = """() => {
  const over = (el) => (el ? el.scrollWidth - el.clientWidth : 0)
  const dlg = document.querySelector('[role=dialog]')
  const clipped = []
  if (dlg) {
    for (const el of [dlg, ...dlg.querySelectorAll('*')]) {
      const cs = getComputedStyle(el)
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) || cs.textOverflow === 'ellipsis') continue
      if (cs.overflowX !== 'visible' && el.clientWidth > 0 && el.scrollWidth - el.clientWidth > 1)
        clipped.push(`${el.tagName.toLowerCase()}.${el.className}`.slice(0, 80) + ` +${el.scrollWidth - el.clientWidth}px`)
    }
  }
  return { root: over(document.querySelector('.cs-root')), dialog: over(dlg), clipped }
}"""

# the Shelf's own text, every match of each: tier Badges, the question block and its age, the notice
# and its summary, chip labels, ORPHAN Badges and status words, the conductor line, the in-flight
# line, the orphan note, the relay box, the honest line and the section headers
# Each entry: selector and its kind. "text" is Shelf-owned text and must clear 4.5:1 (ruling 7: no
# allowance for host status tokens). "host" is a host Badge (the tier tag and ORPHAN): the host renders
# it, its figure is the theme's and is recorded; it must clear 3:1. "dot" is the status dot before a
# status sentence, a non-text cue that must clear 3:1 (WCAG 1.4.11).
MEASURED = [
    (".cs-tag", "host"),
    (".cs-chip > span[class]", "host"),
    (".cs-chip > span:not([class])", "text"),
    (".cs-hof", "text"),
    (".cs-hof", "dot"),
    (".cs-note-orphan", "text"),
    (".cs-note-orphan", "dot"),
    (".cs-need", "text"),
    (".cs-need em", "text"),
    (".cs-notice > span", "text"),
    (".cs-notice summary", "text"),
    (".cs-chip small", "text"),
    (".cs-cond small", "text"),
    (".cs-relay-on", "text"),
    (".cs-honest", "text"),
    (".cs-honest b", "text"),
    (".cs-sec", "text"),
    (".cs-rc-intro > p", "text"),
    (".cs-rc-intro summary", "text"),
]
FLOOR = {"text": 4.5, "host": 3.0, "dot": 3.0}


# ---------------------------------------------------------------------------------------------------
# the staged scratch state, once for the module
# ---------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def staged(browser, login_state) -> Iterator[dict[str, Any]]:
    scratch = h.Scratch()
    ctx = browser.new_context(storage_state=login_state, viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.set_default_timeout(30000)
    page.goto(h.GATEWAY + "/", wait_until="domcontentloaded")
    h.point_app_at(page, scratch.url)
    saved = SCRATCH_SETTINGS.read_text(encoding="utf-8")
    made: list[str] = []
    try:
        scratch.waiting_helper(NEEDS)
        live = scratch.session(LIVE, helpers=1)
        scratch.busy(live["conductor"])
        scratch.busy(
            live["helpers"][0]
        )  # conductor waiting on a working helper: the in-flight line
        scratch.orphan(LIVE)
        data = json.loads(saved)
        data["permissions"]["allow"] = [a for a in data["permissions"]["allow"] if OPS not in a]
        SCRATCH_SETTINGS.write_text(json.dumps(data, indent=2), encoding="utf-8")
        script()
        relay = attach(page, made, NEEDS)
        slot = relay["slot_key"]
        gw(page, "POST", "/api/chat?ws=1", {"slot": slot, "message": "status please"})
        h.wait_until(
            lambda: any(
                r["role"] == "permission" and not perm_meta(r).get("resolved")
                for r in rows(page, slot)["messages"]
            ),
            60,
            what="a permission request in the relay chat",
        )
        h.wait_until(
            lambda: relay_of(page, relay["relay_id"])["permission_pending"], 90, what="pending flag"
        )
        yield {"relay": relay, "scratch": scratch}
    finally:
        # ExitStack runs these last-in first-out and runs each even when one before it raised: the
        # scratch allow rule goes back first (later relay journeys rely on it), then the relays end,
        # the mock's script resets, the scratch sessions go, and the context closes
        with ExitStack() as undo:
            undo.callback(ctx.close)
            undo.callback(scratch.cleanup)
            undo.callback(script)
            for rid in made:
                undo.callback(h.api, page, "POST", f"/relays/{rid}/end", {"delete_chat": True})
            undo.callback(SCRATCH_SETTINGS.write_text, saved, encoding="utf-8")


def new_page(browser, login_state, width: int, height: int, scheme: str) -> Any:
    ctx = browser.new_context(
        storage_state=login_state, viewport={"width": width, "height": height}, color_scheme=scheme
    )
    page = ctx.new_page()
    page.set_default_timeout(30000)
    page.goto(h.GATEWAY + "/", wait_until="domcontentloaded")
    h.open_shelf(page)
    expect(h.card(page, NEEDS).get_by_text(PENDING_SHORT)).to_be_visible(timeout=60000)
    expect(h.card(page, LIVE).locator(".cs-hof")).to_be_visible(timeout=30000)
    expect(h.card(page, LIVE).locator(".cs-note-orphan")).to_be_visible(timeout=30000)
    theme = page.evaluate("document.documentElement.dataset.theme")
    assert theme.endswith(scheme), theme
    return page


def no_overflow(page: Any, where: str) -> None:
    over = page.evaluate(OVERFLOW_JS)
    ok = over["root"] <= 1 and over["dialog"] <= 1 and not over["clipped"]
    assert ok, f"horizontal overflow {over} ({where})"


# measured only once the remote control explainer is open: its paragraphs sit inside <details>, so in
# every other call they are hidden and would be measured against nothing a reader sees
EXPLAINER_OPEN = [(".cs-rc-intro details > p", "text")]


def contrast_ok(
    page: Any, scope_selector: str, where: str, extra: list[tuple[str, str]] | None = None
) -> None:
    els, modes, kinds, missing = [], [], [], []
    for sel, kind in MEASURED + (extra or []):
        handles = page.locator(f"{scope_selector} {sel}").element_handles()
        if not handles:
            missing.append(sel)
        els.extend(handles)
        modes.extend(["dot" if kind == "dot" else "text"] * len(handles))
        kinds.extend([kind] * len(handles))
    assert missing == [], f"measured kinds missing from the page ({where}): {missing}"
    found = page.evaluate(CONTRAST_JS, [els, modes])
    for f, kind in zip(found, kinds):
        f["kind"] = kind
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"restyle-contrast-{where.replace(' ', '-')}.json").write_text(
        json.dumps(found, indent=1)
    )
    # every figure is written next to the screenshots; host Badge figures are the theme's (host-owned)
    low = [f for f in found if f["ratio"] < FLOOR[f["kind"]]]
    assert not low, f"text below its floor ({where}): {low}"


def shot(page: Any, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(500)  # host enter animations
    page.screenshot(path=str(OUT / f"restyle-{name}.png"), full_page=False)


# ---------------------------------------------------------------------------------------------------
# D: the screenshot set, with overflow and contrast on every shot
# ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width,height", [(360, 800), (800, 900)])
def test_restyle_screens(
    browser, login_state, staged, scheme: str, width: int, height: int
) -> None:
    page = new_page(browser, login_state, width, height, scheme)
    tag = f"{scheme}-{width}"
    try:
        # 1. Needs you with a waiting helper, an active relay and the pending-request notice
        card = h.card(page, NEEDS)
        expect(card.get_by_text("Remote controlled from chat")).to_be_visible()
        expect(card.locator(".cs-need")).to_be_visible()
        # unscrolled: the header, the tier row, the honest line and the card's top edge are in view
        expect(page.locator(".cs-tiers")).to_be_in_viewport()
        head = page.get_by_test_id("page-header").bounding_box()
        assert card.bounding_box()["y"] >= head["y"] + head["height"], "the card's top is covered"
        no_overflow(page, f"page {tag}")
        contrast_ok(page, ".cs-root", f"page {tag}")
        shot(page, f"page-needs-relay-{tag}")
        if width < 700:
            # one column: a second shot with the card's lower part (relay box, notice, actions)
            card.evaluate("el => el.scrollIntoView({ block: 'end' })")
            expect(card.locator(".cs-acts")).to_be_in_viewport()
            shot(page, f"page-needs-relay-lower-{tag}")

        # 1b. the remote control explainer: one per page, closed at first, then open
        intro = page.get_by_role("note", name="About remote control")
        expect(intro).to_have_count(1)
        intro.scroll_into_view_if_needed()
        expect(intro).to_be_in_viewport()
        expect(intro.locator("details")).not_to_have_attribute("open", "")
        intro.get_by_text("What is remote control?").click()
        expect(intro.locator("details")).to_have_attribute("open", "")
        expect(intro.get_by_text("Idle does not mean done", exact=False)).to_be_visible()
        no_overflow(page, f"explainer open {tag}")
        contrast_ok(page, ".cs-root", f"explainer open {tag}", EXPLAINER_OPEN)
        figures = json.loads(
            (OUT / f"restyle-contrast-explainer-open-{tag}.json").read_text(encoding="utf-8")
        )
        assert sum(f["text"].startswith("Your messages go") for f in figures) == 1, figures
        shot(page, f"explainer-open-{tag}")
        intro.get_by_text("What is remote control?").click()
        expect(intro.locator("details")).not_to_have_attribute("open", "")

        # 2 and 3. the Remote control dialog, disclosure closed then open
        h.card(page, LIVE).get_by_role("button", name="Remote control", exact=True).click()
        dlg = page.get_by_role("dialog", name=f"Remote control {LIVE}?")
        expect(dlg).to_be_visible()
        expect(dlg.get_by_role("button", name="Close")).to_be_focused()
        no_overflow(page, f"relay dialog {tag}")
        shot(page, f"relay-dialog-{tag}")
        dlg.get_by_text("What the chat can reach").click()
        expect(dlg.locator("details.cs-disc")).to_have_attribute("open", "")
        no_overflow(page, f"relay dialog open {tag}")
        shot(page, f"relay-dialog-open-{tag}")
        dlg.get_by_role("button", name="Cancel").click()
        expect(dlg).to_be_hidden()

        # 4. Start with remote control ticked
        page.get_by_role("button", name="Start session").first.click()
        start = page.get_by_role("dialog", name="Start a session")
        expect(start.get_by_role("combobox", name="Profile")).to_be_enabled(timeout=20000)
        start.get_by_role("checkbox", name="and remote control it").check()
        start.get_by_role("checkbox", name="I accept this risk").scroll_into_view_if_needed()
        no_overflow(page, f"start dialog {tag}")
        shot(page, f"start-remote-{tag}")
    finally:
        page.context.close()


# ---------------------------------------------------------------------------------------------------
# C8: layout at 320, 360 and 800 px
# ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("width,height", [(320, 700), (360, 800), (800, 900)])
def test_restyle_layout(browser, login_state, staged, width: int, height: int) -> None:
    page = new_page(browser, login_state, width, height, "light")
    try:
        no_overflow(page, f"page {width}")
        boxes = page.evaluate("""() => [...document.querySelectorAll('.cs-card')].map((c) => {
                const r = c.getBoundingClientRect()
                return { card: c.getAttribute('aria-label'),
                         chips: [...c.querySelectorAll('.cs-chip')].map((x) => x.getBoundingClientRect())
                           .filter((b) => b.width > 0)
                           .map((b) => [b.left - r.left, r.right - b.right]) }
            })""")
        for b in boxes:
            for left, right in b["chips"]:
                assert (
                    left >= -0.5 and right >= -0.5
                ), f"a chip leaves its card {b['card']} at {width}"
        if width == 360:
            acts = h.card(page, NEEDS).locator(".cs-acts > button")
            tops: dict[str, float] = {}
            for i in range(acts.count()):
                box = acts.nth(i).bounding_box()
                tops[acts.nth(i).inner_text().strip()] = box["y"]
            # one row = tops within 4 px of each other (sub-pixel layout differs per button)
            rows_: list[float] = []
            for y in sorted(tops.values()):
                if not rows_ or y - rows_[-1] > 4:
                    rows_.append(y)
            assert len(rows_) <= 2, f"card actions on more than two rows: {tops}"
            mates = [n for n, y in tops.items() if n != "Stop..." and abs(y - tops["Stop..."]) <= 4]
            assert mates, f"Stop... alone on its row: {tops}"
            short = page.evaluate(
                """() => [...document.querySelectorAll('.cs-btn')].filter((b) => b.offsetParent)
                    .map((b) => [b.textContent.trim(), b.getBoundingClientRect().height])
                    .filter(([, hgt]) => hgt < 43.5)"""
            )
            assert short == [], f"buttons under 44 px at 360: {short}"
        h.card(page, LIVE).get_by_role("button", name="Remote control", exact=True).click()
        dlg = page.get_by_role("dialog", name=f"Remote control {LIVE}?")
        expect(dlg).to_be_visible()
        dlg.get_by_text("What the chat can reach").click()
        no_overflow(page, f"relay dialog {width}")
        box = dlg.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= width + 0.5, f"dialog off screen {box}"
    finally:
        page.context.close()


# ---------------------------------------------------------------------------------------------------
# C7: CAO text stays text inside the host ErrorNotice
# ---------------------------------------------------------------------------------------------------
HOSTILE = '<img src=x onerror="document.title=1"> see http://127.0.0.1:1/'


def test_restyle_cao_error_text_is_inert(browser, login_state, staged) -> None:
    def patch(route) -> None:
        resp = route.fetch()
        body = resp.json()
        body["stale_snapshot"] = True
        body["cao"] = {
            **body["cao"],
            "reachable": False,
            "error": {"code": "cao_error", "message": HOSTILE, "retryable": True},
        }
        route.fulfill(response=resp, body=json.dumps(body))

    ctx = browser.new_context(storage_state=login_state, viewport={"width": 800, "height": 900})
    page = ctx.new_page()
    try:
        page.goto(h.GATEWAY + "/", wait_until="domcontentloaded")
        page.route("**/apps/cao-shelf/api/shelf*", patch)
        h.open_shelf(page)
        alert = page.locator(".cs-root [role=alert]").first
        expect(alert).to_contain_text(HOSTILE, timeout=40000)
        assert page.locator(".cs-root a").count() == 0
        assert page.locator(".cs-root img").count() == 0
        assert page.title() != "1"
    finally:
        ctx.close()
