"""Fixtures for the e2e journeys.

Environment (all optional):
  SHELF_SCRATCH            scratch root (default ~/kc-shelf-e2e; never /tmp)
  PLAYWRIGHT_BROWSERS_PATH where chromium lives (Playwright's default cache when unset)
  SHELF_E2E_KEEP=1         leave the isolated gateway and the scratch CAO running afterwards
Run: python -m pytest e2e/tests -m e2e -q        (from examples/kiro-crew-cao-shelf/)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers as h  # noqa: E402

playwright_sync = pytest.importorskip("playwright.sync_api")


def pytest_collection_modifyitems(items):
    for item in items:
        if "e2e/tests" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(scope="session")
def stack() -> Iterator[dict]:
    """The isolated Kiro Crew with the App installed, and the scratch CAO. Started here when not up."""
    before = h.owner_snapshot()
    started = False
    if not (h.gateway_alive() and h.cao_alive()):
        h.rig("harness.sh", "up", "--no-seed", timeout=900)
        started = True
    else:
        # make sure the App in the scratch home is the one in this folder
        trust = (
            "--trust-app" if __import__("os").environ.get("SHELF_TRUST") == "app" else "--trust-all"
        )
        h.rig("kiro_crew.sh", "install-app", trust, timeout=300)
    scratch = h.Scratch()
    scratch.clear_all()  # only the scratch CAO on 9990; the seeded busy day is not wanted here
    info = {"gateway": h.GATEWAY, "cao": scratch.url, "started": started}
    yield info
    scratch.cleanup()
    if started and not __import__("os").environ.get("SHELF_E2E_KEEP"):
        h.rig("harness.sh", "down", timeout=300)
    after = h.owner_snapshot()
    assert (
        after == before
    ), f"the owner's kirocrew processes or listeners changed:\n--- before\n{before}\n--- after\n{after}"


@pytest.fixture(scope="session")
def browser(stack):
    with playwright_sync.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture(scope="session")
def login_state(browser, tmp_path_factory) -> str:
    """Sign in once with the token URL; every test context reuses the cookies."""
    path = tmp_path_factory.mktemp("auth") / "state.json"
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(h.token_url(), wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    ctx.storage_state(path=str(path))
    ctx.close()
    return str(path)


@pytest.fixture()
def page(browser, login_state) -> Iterator:
    ctx = browser.new_context(storage_state=login_state, viewport={"width": 1280, "height": 900})
    pg = ctx.new_page()
    pg.set_default_timeout(30000)
    pg.goto(h.GATEWAY + "/", wait_until="domcontentloaded")
    yield pg
    ctx.close()


@pytest.fixture()
def scratch(stack) -> Iterator[h.Scratch]:
    s = h.Scratch()
    assert h.cao_alive() or h.rig("cao.sh", "start").returncode == 0
    yield s
    s.cleanup()


@pytest.fixture()
def fake() -> Iterator[h.FakeCao]:
    f = h.FakeCao("busy")
    yield f
    f.close()


@pytest.fixture()
def shelf_on_fake(page, fake):
    h.point_app_at(page, fake.url)
    h.open_shelf(page)
    return page


@pytest.fixture()
def shelf_on_scratch(page, scratch):
    h.point_app_at(page, scratch.url)
    return page
