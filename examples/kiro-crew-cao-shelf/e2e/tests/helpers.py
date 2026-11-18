"""Shared helpers for the e2e journeys: rig control, the scratch CAO, the fake CAO, page access.

Everything here talks only to the isolated Kiro Crew gateway (127.0.0.1:5499), the scratch cao-server
(127.0.0.1:9990) and fake CAOs on ephemeral loopback ports. It never uses 5476 or 9889.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from playwright.sync_api import expect

HERE = Path(__file__).resolve().parent
EXAMPLE = HERE.parents[1]
RIG = EXAMPLE / "e2e" / "rig"
ARTIFACTS = EXAMPLE / "e2e" / "artifacts"
sys.path.insert(0, str(RIG))

import seed_cao  # noqa: E402  (stdlib helper that also drives the scratch CAO)

SCRATCH = os.environ.get("SHELF_SCRATCH", str(Path.home() / "kc-shelf-e2e"))
KC_PORT = int(os.environ.get("SHELF_KC_PORT", "5499"))
CAO_PORT = int(os.environ.get("SHELF_CAO_PORT", "9990"))
ENV = {**os.environ, "SHELF_SCRATCH": SCRATCH}
APP_PATH = "/apps/cao-shelf"
GATEWAY = f"http://localhost:{KC_PORT}"

assert KC_PORT != 5476 and CAO_PORT != 9889, "the owner's ports are never used"


def rig(
    script: str, *args: str, check: bool = True, timeout: int = 900
) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [str(RIG / script), *args],
        env=ENV,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"{script} {' '.join(args)} failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
        )
    return proc


def owner_snapshot() -> str:
    out = subprocess.run(
        ["bash", "-c", f'source "{RIG}/lib.sh"; rig_owner_snapshot'],
        env=ENV,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return out.stdout


def gateway_alive() -> bool:
    return rig("kiro_crew.sh", "status", check=False).returncode == 0


def cao_alive() -> bool:
    return rig("cao.sh", "status", check=False).returncode == 0


def token_url() -> str:
    return rig("kiro_crew.sh", "token").stdout.strip().splitlines()[-1]


def work_dir() -> str:
    return seed_cao.workdir()


# ---------------------------------------------------------------------------------------------------
# Scratch CAO (a real cao-server with mock_cli)
# ---------------------------------------------------------------------------------------------------
class Scratch:
    """The scratch cao-server on 127.0.0.1:9990. Tracks the sessions it creates and removes them."""

    url = f"http://127.0.0.1:{CAO_PORT}"

    def __init__(self) -> None:
        self.cao = seed_cao.Cao(CAO_PORT)
        self.created: list[str] = []
        self.cwd = work_dir()

    def session(self, name: str, helpers: int = 0, caller_of_first: bool = True) -> dict[str, Any]:
        """Create ``name`` with a conductor and ``helpers`` helpers. Returns ids."""
        conductor = self.cao.create_session(name, self.cwd)
        self.created.append(name)
        cid = conductor["id"]
        out: dict[str, Any] = {"conductor": cid, "helpers": []}
        self.cao.wait_status(cid, {"idle", "completed"})
        for _ in range(helpers):
            helper = self.cao.add_helper(name, cid, self.cwd)
            self.cao.wait_status(helper["id"], {"idle", "completed"})
            out["helpers"].append(helper["id"])
        return out

    def waiting_helper(self, name: str) -> dict[str, Any]:
        """A session whose helper sits in ``waiting_user_answer`` (a scripted mock_cli approval)."""
        ids = self.session(name, helpers=1)
        hid = ids["helpers"][0]
        self.cao.send_input(hid, "__mock_approval__ push to origin?")
        status = self.cao.wait_status(hid, {"waiting_user_answer"}, 30)
        assert (
            status == "waiting_user_answer"
        ), f"mock_cli did not reach waiting_user_answer ({status})"
        return ids

    def busy(self, terminal_id: str, seconds: int = 1800) -> None:
        self.cao.send_input(terminal_id, f"__mock_sleep_{seconds}")

    def backdate(self, name: str, hours: float) -> None:
        seed_cao.cmd_backdate(
            name, hours, os.environ.get("SHELF_CAO_HOME") or f"{SCRATCH}/cao-home"
        )

    def names(self) -> list[str]:
        return sorted(s["name"] for s in self.cao.sessions())

    def terminal_status(self, tid: str) -> str:
        return self.cao.terminal_status(tid)

    def inbox(self, tid: str) -> list[dict[str, Any]]:
        return self.cao.call(
            "GET", f"/terminals/{tid}/inbox/messages", {"limit": "100", "order": "desc"}
        )

    def closed_helper(self, name: str) -> None:
        seed_cao.cmd_closed_helper(self.cao, name)

    def orphan(self, name: str) -> None:
        seed_cao.cmd_orphan(self.cao, name)

    def clear_all(self) -> None:
        for row in self.cao.sessions():
            self.cao.call("DELETE", f"/sessions/{row['name']}")

    def cleanup(self) -> None:
        for name in self.created:
            try:
                self.cao.call("DELETE", f"/sessions/cao-{name}")
            except SystemExit:
                pass  # already stopped by the journey itself
        self.created.clear()


# ---------------------------------------------------------------------------------------------------
# Fake CAO (subprocess of e2e/rig/fake_cao.py)
# ---------------------------------------------------------------------------------------------------
class FakeCao:
    def __init__(self, day: str = "busy") -> None:
        self.proc = subprocess.Popen(
            [
                sys.executable,
                str(RIG / "fake_cao.py"),
                "--day",
                day,
                "--time",
                "rebased",
                "--port",
                "0",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline().strip()
        m = re.fullmatch(r"FAKE_CAO_URL=(http://127\.0\.0\.1:\d+)", line)
        assert m, f"fake CAO did not announce a URL: {line!r}"
        self.url = m.group(1)

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.url + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None

    def mode(self, mode: str) -> None:
        self._call("POST", "/__mode", {"mode": mode})

    def calls(self) -> list[dict[str, Any]]:
        return self._call("GET", "/__calls")["calls"]

    def writes(self) -> list[tuple[str, str]]:
        return [(c["method"], c["path"]) for c in self.calls() if c["method"] != "GET"]

    def close(self) -> None:
        self.proc.terminate()  # our own child process, started above
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# ---------------------------------------------------------------------------------------------------
# Page access
# ---------------------------------------------------------------------------------------------------
def api(page: Any, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    """Call the App's API from the browser, through the gateway's reverse proxy (cookie auth)."""
    result = page.evaluate(
        """async ([method, path, body]) => {
            const init = {method, headers: {'Content-Type': 'application/json'}};
            if (body !== null) init.body = JSON.stringify(body);
            const r = await fetch('/apps/cao-shelf/api' + path, init);
            const text = await r.text();
            return [r.status, text];
        }""",
        [method, path, body],
    )
    status, text = result
    try:
        return status, json.loads(text) if text else None
    except ValueError:
        return status, text


def point_app_at(page: Any, cao_url: str) -> None:
    status, body = api(page, "PUT", "/config", {"cao_url": cao_url})
    assert status == 200, f"PUT /api/config -> {status} {body}"


def open_shelf(page: Any) -> None:
    page.goto(f"{GATEWAY}{APP_PATH}", wait_until="domcontentloaded")
    page.wait_for_selector("style[data-cao-shelf]", state="attached", timeout=30000)


def card(page: Any, name: str) -> Any:
    # a host Card is a div with role="article", so match the class, not the tag
    return page.locator(f'.cs-card[role="article"][aria-label^="session {name},"]')


def pick_option(page: Any, scope: Any, label: str, option: str, timeout: float = 20000) -> None:
    """Choose ``option`` in the host Select named ``label`` inside ``scope``.

    On a desktop browser the host Select is a Radix combobox whose list opens in a portal on
    ``document.body``, so the option is looked up on the page, not inside the dialog. The trigger
    stays disabled until its options have loaded.
    """
    trigger = scope.get_by_role("combobox", name=label)
    expect(trigger).to_be_enabled(timeout=timeout)
    trigger.click()
    page.get_by_role("option", name=option, exact=True).click()
    expect(trigger).to_contain_text(option)


def wait_until(predicate, seconds: float = 30.0, interval: float = 0.5, what: str = "condition"):
    end = time.monotonic() + seconds
    last = None
    while time.monotonic() < end:
        try:
            last = predicate()
            if last:
                return last
        except (urllib.error.URLError, OSError, SystemExit):
            pass
        time.sleep(interval)
    raise AssertionError(f"timed out after {seconds:.0f}s waiting for {what} (last={last!r})")


def show_tier(page: Any, label: str) -> None:
    """Turn a tier on (they are buttons with aria-pressed; Stale starts off)."""
    button = page.get_by_role("group", name="Tiers to show").get_by_role(
        "button", name=re.compile(f"^{label}", re.I)
    )
    if button.get_attribute("aria-pressed") != "true":
        button.click()
