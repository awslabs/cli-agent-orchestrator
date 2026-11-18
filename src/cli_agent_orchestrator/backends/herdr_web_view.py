"""Single-pane browser view for the herdr backend.

A herdr TUI client always shows the server's focused tab, and every client
shares that focus. Attaching the browser terminal to ``herdr`` directly
therefore made every web terminal show whichever agent was focused last, and
the Workspace tab's split panes all showed the same agent.

This module is the program the browser terminal's PTY runs instead. It shows
exactly one herdr pane and nothing of herdr's own UI:

- **Output.** The pane's visible screen is read over herdr's socket API
  (``pane.read``, ANSI format) and repainted when it changes.
- **Size.** herdr sizes each tab separately, from the tab's geometry
  controller: one client, whose terminal size minus herdr's sidebar and tab
  bar becomes the tab's size. Every client views the focused tab, and a
  client resize changes only the tab it views, and only if it controls it.
  So each view, holding the herdr session's focus lock (see
  ``focus_lock_path``), focuses its own tab, makes its hidden client (below)
  that tab's controller (a terminal focus report, as a real terminal sends),
  and sizes the client to the browser terminal plus herdr's chrome. It then
  reads herdr's layout to correct its chrome estimate, which is valid only
  while its own tab is focused. herdr keeps each tab at its controller's size
  across later focus changes, so a tab keeps its view's size while the view
  is parked in the browser's pool, and only a view whose own size changed
  redoes this. (The agent's pane is one column narrower than the browser
  pane while herdr shows its scrollbar gutter.)
- **Input.** Keystrokes go, byte for byte, to a hidden herdr client this
  process owns, so herdr's own key handling applies (Enter, Escape, arrows,
  Ctrl keys, bracketed paste). cao-server focuses this view's tab before it
  writes each input frame, holding the same focus lock until the view has
  read the frame (``HerdrBackend.focus_web_input``), so the keys reach this
  pane and a view's resize never moves the focus under them.
- **Scrolling.** The view turns on mouse reporting and passes wheel events
  to its hidden client, moved past herdr's sidebar and tab bar so they land
  on the pane. herdr then does what its own UI does with a wheel: it scrolls
  the pane's scrollback, or hands the event to an agent that asked for the
  mouse (Claude Code scrolls its transcript itself). Other mouse events are
  dropped. (Text selection in the browser then needs Shift, as in any
  terminal app that uses the mouse.)

The pane is found by its workspace and tab labels, because those are what CAO
knows: the CAO session and window names. When the tab is gone the view says
so and exits, so the browser sees the connection close, as with tmux.

Run as ``python -m cli_agent_orchestrator.backends.herdr_web_view``.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import pty
import re
import select
import signal
import socket
import stat
import struct
import sys
import tempfile
import termios
import time
import tty
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

POLL_INTERVAL_S = 0.1
RESOLVE_INTERVAL_S = 2.0
SOCKET_TIMEOUT_S = 5.0
# A resize, holding the focus lock: wait out input that may still be on its
# way into herdr (a lone Escape waits up to this long in a herdr client,
# HerdrBackend's escape hold), focus this view's tab and let every client
# follow it, then claim the tab and resize, and let herdr lay out again.
INPUT_GRACE_S = 0.3
FOCUS_SETTLE_S = 0.15
RESIZE_SETTLE_S = 0.4
# How many times a view checks the result and corrects its estimate of
# herdr's chrome, and how long it waits for the lock another view holds.
CALIBRATION_ATTEMPTS = 4
LOCK_RETRY_S = 0.2
# herdr 0.9.5 defaults: a 26-column sidebar and a one-row tab bar.
DEFAULT_CHROME = (1, 26)
# Focus lost, then gained, as a real terminal reports it. herdr acts only on
# a change, and a gain makes the client the controller of the tab it views.
FOCUS_REPORT = b"\x1b[O\x1b[I"

# Alternate screen, cursor hidden, no autowrap (long lines are cut, not
# wrapped), and SGR mouse reporting so the wheel reaches the view.
_ENTER = "\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[?1000h\x1b[?1006h\x1b[H\x1b[2J"
_LEAVE = "\x1b[?1006l\x1b[?1000l\x1b[0m\x1b[?7h\x1b[?25h\x1b[?1049l"
# An SGR mouse report, ESC [ < button ; column ; row (M press | m release).
_SGR_MOUSE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)[Mm]")
# A report cut off at the end of a read; kept until the rest arrives.
_SGR_MOUSE_PARTIAL = re.compile(rb"\x1b(\[(<[\d;]*)?)?$")


class HerdrSocketError(Exception):
    """A herdr socket request failed or returned an error."""


def herdr_call(socket_path: str, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """One request on a fresh connection; herdr answers one request per connection."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(SOCKET_TIMEOUT_S)
    try:
        sock.connect(socket_path)
        sock.sendall(
            (json.dumps({"id": "web-view", "method": method, "params": params}) + "\n").encode()
        )
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
    except OSError as e:
        raise HerdrSocketError(f"{method}: {e}") from e
    finally:
        sock.close()
    try:
        message = json.loads(buf.decode("utf-8", "replace"))
    except json.JSONDecodeError as e:
        raise HerdrSocketError(f"{method}: unreadable answer") from e
    if not isinstance(message, dict) or "error" in message:
        error = message.get("error") if isinstance(message, dict) else None
        raise HerdrSocketError(f"{method}: {error}")
    result = message.get("result")
    return result if isinstance(result, dict) else {}


@dataclass
class PaneInfo:
    pane_id: str
    # herdr's layout area after its own chrome. herdr reports the foreground
    # client's area for every tab, so it is this tab's size only while the
    # tab is focused (``focused``) and this view's client is the foreground.
    area_rows: Optional[int]
    area_cols: Optional[int]
    tab_id: str = ""
    focused: bool = False


def resolve_pane(socket_path: str, workspace_label: str, tab_label: str) -> Optional[PaneInfo]:
    """The pane of tab ``tab_label`` in workspace ``workspace_label``, or None."""
    snapshot = herdr_call(socket_path, "session.snapshot", {}).get("snapshot", {})
    workspaces = {
        w.get("workspace_id"): w.get("focused") is True
        for w in snapshot.get("workspaces", [])
        if isinstance(w, dict) and w.get("label") == workspace_label
    }
    tabs = {
        t.get("tab_id"): workspaces[t.get("workspace_id")] and t.get("focused") is True
        for t in snapshot.get("tabs", [])
        if isinstance(t, dict)
        and t.get("workspace_id") in workspaces
        and t.get("label") == tab_label
    }
    for pane in snapshot.get("panes", []):
        if not (isinstance(pane, dict) and pane.get("tab_id") in tabs and pane.get("pane_id")):
            continue
        area: Dict[str, Any] = {}
        for layout in snapshot.get("layouts", []):
            if isinstance(layout, dict) and layout.get("tab_id") == pane["tab_id"]:
                area = layout.get("area") or {}
        return PaneInfo(
            pane_id=str(pane["pane_id"]),
            area_rows=area.get("height"),
            area_cols=area.get("width"),
            tab_id=str(pane["tab_id"]),
            focused=tabs[pane["tab_id"]],
        )
    return None


def focus_tab(socket_path: str, tab_id: str) -> None:
    """Move herdr's focus, and with it every client's view, to ``tab_id``."""
    herdr_call(socket_path, "tab.focus", {"tab_id": tab_id})


def read_screen(socket_path: str, pane_id: str) -> str:
    """The pane's visible screen with its colours."""
    result = herdr_call(
        socket_path,
        "pane.read",
        {"pane_id": pane_id, "source": "visible", "format": "ansi", "strip_ansi": False},
    )
    read = result.get("read", {})
    text = read.get("text", "") if isinstance(read, dict) else ""
    return text if isinstance(text, str) else ""


def render(screen: str, rows: int) -> str:
    """Repaint ``screen`` from the top-left: each row cleared to its end, the rest erased.

    herdr's ANSI read starts with a line break and ends rows with CR LF; the
    bottom ``rows`` lines are kept so the agent's prompt stays in view.
    """
    lines = screen.replace("\r\n", "\n").split("\n")
    if lines and lines[0] == "":
        lines = lines[1:]
    while lines and lines[-1].strip() == "":
        lines.pop()
    if rows > 0 and len(lines) > rows:
        lines = lines[-rows:]
    out: List[str] = []
    for index, line in enumerate(lines):
        out.append(f"\x1b[{index + 1};1H{line}\x1b[0m\x1b[K")
    out.append(f"\x1b[{len(lines) + 1};1H\x1b[0m\x1b[J")
    return "".join(out)


def split_mouse(data: bytes) -> Tuple[bytes, List[Tuple[int, int, int]], bytes]:
    """Take SGR mouse reports out of ``data``.

    Returns the key bytes, the wheel reports as ``(button, column, row)``, and
    a trailing partial report to prepend to the next read. Clicks and drags
    are dropped.
    """
    wheel: List[Tuple[int, int, int]] = []

    def _take(match: "re.Match[bytes]") -> bytes:
        button, column, row = (int(g) for g in match.groups())
        if button & 64 and not button & 32:  # wheel, not a drag
            wheel.append((button, column, row))
        return b""

    keys = _SGR_MOUSE.sub(_take, data)
    partial = _SGR_MOUSE_PARTIAL.search(keys)
    rest = b""
    if partial and partial.group(0).startswith(b"\x1b[<"):
        rest = partial.group(0)
        keys = keys[: partial.start()]
    return keys, wheel, rest


def _window_size(fd: int) -> Tuple[int, int]:
    try:
        rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
        return rows, cols
    except OSError:
        return 24, 80


def _set_window_size(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(rows, 1), max(cols, 1), 0, 0))
    except OSError:
        pass


def _spawn_input_client(herdr_session: str, rows: int, cols: int) -> Tuple[int, int]:
    """Start the hidden herdr client that turns this view's keystrokes into herdr input."""
    pid, master_fd = pty.fork()
    if pid == 0:  # child
        try:
            os.execvp("herdr", ["herdr", "--session", herdr_session])
        finally:
            os._exit(127)
    _set_window_size(master_fd, rows, cols)
    return pid, master_fd


def _write_all(fd: int, data: bytes) -> None:
    while data:
        try:
            written = os.write(fd, data)
        except InterruptedError:
            continue
        except BlockingIOError:
            select.select([], [fd], [], 1.0)
            continue
        data = data[written:]


def _private_dir(path: str) -> bool:
    """Create ``path`` as a 0700 directory, or accept it only if it already is one we own."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError:
        return False
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) & 0o077 == 0
    )


def session_state_dir(herdr_session: str) -> Optional[str]:
    """Private directory one herdr session's views and cao-server share (lock, chrome).

    It lives under the shared temp dir, so it is created 0700 and refused if
    another user made it first, is a symlink, or is group/world accessible.
    None means no usable directory: focus changes then go unserialised.
    """
    root = os.path.join(tempfile.gettempdir(), f"cao-herdr-web-view-{os.getuid()}")
    path = os.path.join(root, herdr_session)
    if _private_dir(root) and _private_dir(path):
        return path
    return None


def focus_lock_path(herdr_session: str) -> Optional[str]:
    """The lock held by whatever moves herdr's focus in this herdr session.

    cao-server holds it from focusing a view's tab until the view has read the
    input frame; a view holds it for its whole resize (see ``_Sizer``). So a
    resize never moves the focus while keys are on their way to a tab.
    """
    state_dir = session_state_dir(herdr_session)
    return os.path.join(state_dir, "focus.lock") if state_dir else None


def try_lock(path: str) -> Optional[int]:
    """The open, exclusively locked ``path``; None if another holder has it.

    Raises ``OSError`` when the file cannot be opened at all.
    """
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def unlock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class _Sizer:
    """Keeps this view's tab at this view's size (see the module docstring).

    One sequence, with the focus lock held throughout: wait out input still on
    its way into herdr, focus this view's tab, claim it for the hidden client
    and resize the client, then check herdr's layout and correct the chrome
    estimate until it fits. The layout is only this tab's while the tab is
    focused, so a check that finds another tab focused starts over instead of
    finishing. What a view measured is saved for the next view to start from.
    """

    def __init__(
        self,
        client_pid: int,
        client_fd: int,
        lock_path: Optional[str] = None,
        chrome_path: Optional[str] = None,
        focus_own_tab: Optional[Callable[[], None]] = None,
    ) -> None:
        self.client_pid = client_pid
        self.client_fd = client_fd
        self.lock_path = lock_path
        self.chrome_path = chrome_path
        self.focus_own_tab = focus_own_tab or (lambda: None)
        self.chrome_rows, self.chrome_cols = self._load_chrome()
        self.rows = 0
        self.cols = 0
        self.attempts = 0
        self.next_at = 0.0
        self._phase = "grace"
        self._lock_fd: Optional[int] = None

    # --- chrome, shared between the views of one herdr session ---

    def _load_chrome(self) -> Tuple[int, int]:
        try:
            with open(self.chrome_path or "", encoding="utf-8") as handle:
                saved = json.load(handle)
            return int(saved["rows"]), int(saved["cols"])
        except (OSError, ValueError, KeyError, TypeError):
            return DEFAULT_CHROME

    def _save_chrome(self) -> None:
        if not self.chrome_path:
            return
        try:
            with open(self.chrome_path, "w", encoding="utf-8") as handle:
                json.dump({"rows": self.chrome_rows, "cols": self.chrome_cols}, handle)
        except OSError:
            pass

    # --- the lock, held for one apply-and-verify sequence ---

    def _acquire(self) -> bool:
        if self._lock_fd is not None:
            return True
        if not self.lock_path:
            return True
        try:
            fd = try_lock(self.lock_path)
        except OSError:
            self.lock_path = None  # unlockable: resize unserialised rather than not at all
            return True
        if fd is None:
            return False
        self._lock_fd = fd
        return True

    def _release(self) -> None:
        if self._lock_fd is None:
            return
        fd, self._lock_fd = self._lock_fd, None
        unlock(fd)

    def wheel_report(self, button: int, column: int, row: int) -> bytes:
        """A wheel report at a view cell, moved to the same cell of herdr's pane area."""
        return f"\x1b[<{button};{column + self.chrome_cols};{row + self.chrome_rows}M".encode()

    def resize(self, rows: int, cols: int) -> None:
        """The browser terminal is now ``rows`` x ``cols``: start matching herdr to it."""
        self.rows, self.cols = rows, cols
        self.attempts = CALIBRATION_ATTEMPTS
        self.next_at = 0.0
        self._phase = "grace"

    def due(self) -> bool:
        return self.attempts > 0 and time.monotonic() >= self.next_at

    def step(self, read_state: "Callable[[], Optional[Tuple[int, int, bool]]]") -> None:
        """One step of the sequence (see the class docstring).

        ``read_state`` answers herdr's layout area and whether this view's tab
        is focused, ``(rows, cols, focused)``, or None if it cannot be read.
        """
        if not self._acquire():
            self.next_at = time.monotonic() + LOCK_RETRY_S  # another view or an input turn
            return
        if self._phase == "grace":
            self._phase = "focus"
            self.next_at = time.monotonic() + INPUT_GRACE_S
            return
        if self._phase == "focus":
            try:
                self.focus_own_tab()
            except HerdrSocketError:
                # Let input through meanwhile, and try again from the start.
                self.attempts -= 1
                self._phase = "grace"
                self._release()
                self.next_at = time.monotonic() + LOCK_RETRY_S
                if self.attempts <= 0:
                    self._finish()
                raise
            self._phase = "claim"
            self.next_at = time.monotonic() + FOCUS_SETTLE_S
            return
        if self._phase == "claim":
            self._phase = "verify"
            self._apply()
            return
        state = read_state()
        self.attempts -= 1
        if state is None:
            self._finish()
            return
        rows, cols, focused = state
        if focused and (rows, cols) == (self.rows, self.cols):
            self._save_chrome()
            self._finish()
            return
        if self.attempts <= 0:
            self._finish()
            return
        if not focused:
            # The layout is another tab's: take the focus back before trusting it.
            self._phase = "focus"
            self.next_at = 0.0
            return
        self.chrome_rows = max(self.chrome_rows + self.rows - rows, 0)
        self.chrome_cols = max(self.chrome_cols + self.cols - cols, 0)
        self._apply()

    def _apply(self) -> None:
        """Size the hidden client, and make it the controller of the tab it views."""
        _set_window_size(self.client_fd, self.rows + self.chrome_rows, self.cols + self.chrome_cols)
        try:
            os.kill(self.client_pid, signal.SIGWINCH)
        except OSError:
            pass
        try:
            _write_all(self.client_fd, FOCUS_REPORT)
        except OSError:
            pass
        self.next_at = time.monotonic() + RESIZE_SETTLE_S

    def _finish(self) -> None:
        self.attempts = 0
        self._release()


def _take_controlling_terminal(fd: int) -> None:
    """Make the PTY this view runs in its controlling terminal, so it receives SIGWINCH.

    cao-server starts the attach command in a new session without one; herdr
    took the terminal itself, this view has to as well.
    """
    try:
        fcntl.ioctl(fd, termios.TIOCSCTTY, 0)
    except OSError:
        pass  # the size is also polled, so a missing SIGWINCH only adds latency


def run(herdr_session: str, socket_path: str, workspace_label: str, tab_label: str) -> int:
    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    _take_controlling_terminal(stdin_fd)
    rows, cols = _window_size(stdout_fd)

    try:
        pane = resolve_pane(socket_path, workspace_label, tab_label)
    except HerdrSocketError as e:
        os.write(stdout_fd, f"Cannot reach herdr session '{herdr_session}': {e}\r\n".encode())
        return 1
    if pane is None:
        os.write(stdout_fd, f"No herdr tab '{tab_label}' in '{workspace_label}'.\r\n".encode())
        return 1

    client_pid, client_fd = _spawn_input_client(
        herdr_session, rows + DEFAULT_CHROME[0], cols + DEFAULT_CHROME[1]
    )
    state_dir = session_state_dir(herdr_session)
    current: PaneInfo = pane

    def _focus_own_tab() -> None:
        focus_tab(socket_path, current.tab_id)

    sizer = _Sizer(
        client_pid,
        client_fd,
        lock_path=focus_lock_path(herdr_session),
        chrome_path=os.path.join(state_dir, "chrome.json") if state_dir else None,
        focus_own_tab=_focus_own_tab,
    )
    sizer.resize(rows, cols)
    # Python retries select() after a handler returns (PEP 475), so SIGWINCH
    # does not wake the loop; it only must not end the process. The loop
    # checks the size on every pass, at least every POLL_INTERVAL_S.
    signal.signal(signal.SIGWINCH, lambda _signum, _frame: None)
    saved_tty = None
    try:
        saved_tty = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)
    except termios.error:
        pass

    _write_all(stdout_fd, _ENTER.encode())
    last_screen: Optional[str] = None
    last_resolve = time.monotonic()
    next_poll = 0.0
    carry = b""
    try:
        while True:
            size = _window_size(stdout_fd)
            if size != (rows, cols):
                rows, cols = size
                sizer.resize(rows, cols)
                last_screen = None

            timeout = max(0.0, next_poll - time.monotonic())
            readable, _, _ = select.select([stdin_fd, client_fd], [], [], timeout)

            if stdin_fd in readable:
                try:
                    data = os.read(stdin_fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    return 0
                keys, wheel, carry = split_mouse(carry + data)
                for report in wheel:
                    _write_all(client_fd, sizer.wheel_report(*report))
                if keys:
                    _write_all(client_fd, keys)
                next_poll = 0.0  # show the echo or the scroll promptly

            if client_fd in readable:
                try:
                    os.read(client_fd, 65536)  # herdr's own UI: drained, never shown
                except OSError as e:
                    if e.errno in (errno.EIO, errno.EBADF):
                        return 1  # the hidden client exited
                    raise

            now = time.monotonic()
            if now < next_poll:
                continue
            next_poll = now + POLL_INTERVAL_S

            def _read_state() -> Optional[Tuple[int, int, bool]]:
                """herdr's layout area and whether this tab is focused; None if unreadable."""
                nonlocal current, last_resolve
                last_resolve = time.monotonic()
                found = resolve_pane(socket_path, workspace_label, tab_label)
                if found is not None:
                    current = found
                if found is None or found.area_rows is None or found.area_cols is None:
                    return None
                return found.area_rows, found.area_cols, found.focused

            try:
                if sizer.due():
                    sizer.step(_read_state)
                if now - last_resolve >= RESOLVE_INTERVAL_S:
                    last_resolve = now
                    found = resolve_pane(socket_path, workspace_label, tab_label)
                    if found is None:
                        _write_all(
                            stdout_fd, (_LEAVE + "The agent's herdr tab closed.\r\n").encode()
                        )
                        return 0
                    current = found
                screen = read_screen(socket_path, current.pane_id)
            except HerdrSocketError:
                last_resolve = 0.0  # look the pane up again on the next poll
                continue
            if screen != last_screen:
                last_screen = screen
                _write_all(stdout_fd, render(screen, rows).encode("utf-8", "replace"))
    finally:
        sizer._finish()
        if saved_tty is not None:
            try:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved_tty)
            except termios.error:
                pass
        try:
            os.write(stdout_fd, _LEAVE.encode())
        except OSError:
            pass
        try:
            os.kill(client_pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            os.close(client_fd)
        except OSError:
            pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--herdr-session", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument(
        "--workspace", required=True, help="CAO session name (herdr workspace label)"
    )
    parser.add_argument("--tab", required=True, help="CAO window name (herdr tab label)")
    args = parser.parse_args(argv)
    return run(args.herdr_session, args.socket, args.workspace, args.tab)


if __name__ == "__main__":
    sys.exit(main())
