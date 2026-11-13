"""Single-pane browser view for the herdr backend.

A herdr TUI client always shows the server's focused tab, and every client
shares that focus. Attaching the browser terminal to ``herdr`` directly
therefore made every web terminal show whichever agent was focused last, and
the Workspace tab's split panes all showed the same agent.

This module is the program the browser terminal's PTY runs instead. It shows
exactly one herdr pane and nothing of herdr's own UI:

- **Output.** The pane's visible screen is read over herdr's socket API
  (``pane.read``, ANSI format) and repainted when it changes. Lines are cut
  at the browser's width rather than wrapped; the bottom rows are kept when
  the browser is shorter than the pane, since that is where an agent's prompt
  is.
- **Input.** Keystrokes go, byte for byte, to a hidden herdr client this
  process owns, so herdr's own key handling applies (Enter, Escape, arrows,
  Ctrl keys, bracketed paste). cao-server focuses this view's tab before it
  writes each input frame and serialises input per herdr session
  (``HerdrBackend.prepare_web_attach_input``), so the keys reach this pane.

The pane is found by its workspace and tab labels (the CAO session and window
names) on every lookup, because herdr renumbers pane ids when a pane in the
workspace closes. When the tab is gone the view says so and exits, so the
browser sees the connection close, as with tmux.

Run as ``python -m cli_agent_orchestrator.backends.herdr_web_view``.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import pty
import select
import signal
import socket
import struct
import sys
import termios
import time
import tty
from typing import Any, Dict, List, Optional, Tuple

POLL_INTERVAL_S = 0.1
RESOLVE_INTERVAL_S = 2.0
SOCKET_TIMEOUT_S = 5.0
# Escape sequences that put the browser terminal in the state this view draws in:
# alternate screen, cursor hidden, no autowrap (long pane lines are cut, not wrapped).
_ENTER = "\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[H\x1b[2J"
_LEAVE = "\x1b[0m\x1b[?7h\x1b[?25h\x1b[?1049l"


class HerdrSocketError(Exception):
    """A herdr socket request failed or returned an error."""


def herdr_call(socket_path: str, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """One request on a fresh connection; herdr answers one request per connection."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(SOCKET_TIMEOUT_S)
    try:
        sock.connect(socket_path)
        sock.sendall((json.dumps({"id": "web-view", "method": method, "params": params}) + "\n").encode())
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


def resolve_pane(socket_path: str, workspace_label: str, tab_label: str) -> Optional[str]:
    """Pane id of the tab ``tab_label`` in workspace ``workspace_label``, or None."""
    snapshot = herdr_call(socket_path, "session.snapshot", {}).get("snapshot", {})
    workspace_ids = {
        w.get("workspace_id")
        for w in snapshot.get("workspaces", [])
        if isinstance(w, dict) and w.get("label") == workspace_label
    }
    tab_ids = {
        t.get("tab_id")
        for t in snapshot.get("tabs", [])
        if isinstance(t, dict) and t.get("workspace_id") in workspace_ids and t.get("label") == tab_label
    }
    for pane in snapshot.get("panes", []):
        if isinstance(pane, dict) and pane.get("tab_id") in tab_ids and pane.get("pane_id"):
            return str(pane["pane_id"])
    return None


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


def _window_size(fd: int) -> Tuple[int, int]:
    try:
        rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
        return rows, cols
    except OSError:
        return 24, 80


def _set_window_size(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
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


def run(herdr_session: str, socket_path: str, workspace_label: str, tab_label: str) -> int:
    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    rows, cols = _window_size(stdout_fd)

    pane_id = resolve_pane(socket_path, workspace_label, tab_label)
    if pane_id is None:
        os.write(stdout_fd, f"No herdr tab '{tab_label}' in '{workspace_label}'.\r\n".encode())
        return 1

    client_pid, client_fd = _spawn_input_client(herdr_session, rows, cols)
    resized = {"pending": False}

    def _on_winch(_signum: int, _frame: Any) -> None:
        resized["pending"] = True

    signal.signal(signal.SIGWINCH, _on_winch)
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
    try:
        while True:
            if resized["pending"]:
                resized["pending"] = False
                rows, cols = _window_size(stdout_fd)
                _set_window_size(client_fd, rows, cols)
                last_screen = None

            timeout = max(0.0, next_poll - time.monotonic())
            try:
                readable, _, _ = select.select([stdin_fd, client_fd], [], [], timeout)
            except InterruptedError:
                continue

            if stdin_fd in readable:
                try:
                    data = os.read(stdin_fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    return 0
                _write_all(client_fd, data)
                next_poll = 0.0  # show the echo promptly

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
            try:
                if now - last_resolve >= RESOLVE_INTERVAL_S:
                    last_resolve = now
                    pane_id = resolve_pane(socket_path, workspace_label, tab_label)
                    if pane_id is None:
                        _write_all(stdout_fd, (_LEAVE + "The agent's herdr tab closed.\r\n").encode())
                        return 0
                screen = read_screen(socket_path, pane_id)
            except HerdrSocketError:
                last_resolve = 0.0  # look the pane up again on the next poll
                continue
            if screen != last_screen:
                last_screen = screen
                _write_all(stdout_fd, render(screen, rows).encode("utf-8", "replace"))
    finally:
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
    parser.add_argument("--workspace", required=True, help="CAO session name (herdr workspace label)")
    parser.add_argument("--tab", required=True, help="CAO window name (herdr tab label)")
    args = parser.parse_args(argv)
    return run(args.herdr_session, args.socket, args.workspace, args.tab)


if __name__ == "__main__":
    sys.exit(main())
