"""Native CLI attach to a terminal over the server's WebSocket (#745/#776).

The client needs no tmux and no server filesystem: it speaks the same
protocol as the bundled browser viewer against ``WS /terminals/{id}/ws`` —
JSON ``{"type": "input"|"resize", ...}`` up, raw terminal bytes down — and the
server relays to the local PTY (local terminal) or over the runtime channel
(remote terminal). Detach with the session's own detach key (tmux: Ctrl-b d),
which ends the server-side attach and closes the socket.
"""

import json
import os
import select
import shutil
import signal
import sys
import termios
import threading
import tty
from typing import Optional

import click


def _ws_url(base_url: str, terminal_id: str) -> str:
    scheme_swapped = base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    return f"{scheme_swapped}/terminals/{terminal_id}/ws"


def attach_remote_terminal(terminal_id: str, base_url: str, token: Optional[str] = None) -> None:
    """Attach this TTY to a terminal through the server's WS relay.

    Raises click.ClickException on connection/handshake failure; restores the
    local TTY state on every exit path.
    """
    from websockets.exceptions import ConnectionClosed, InvalidStatus
    from websockets.sync.client import connect

    url = _ws_url(base_url, terminal_id)
    # The credential goes in the handshake HEADER, never in the URL. The server
    # accepts ``?token=`` too, but only because a browser cannot set a header on
    # a WebSocket handshake; this client can. A query parameter is logged by
    # uvicorn and by any proxy in between, and the access log's redaction filter
    # is keyed on the parameter names the browser paths use, so a native attach
    # would have persisted a reusable bearer token in plaintext (Copilot review
    # on #802). ``token`` is now redacted as well, for the browser path that has
    # no alternative — but the native path should not need the redaction.
    headers = {"Authorization": f"Bearer {token}"} if token else None

    if not sys.stdin.isatty():
        raise click.ClickException("interactive attach requires a TTY (use --headless)")

    try:
        ws = connect(url, max_size=None, additional_headers=headers)
    except InvalidStatus as exc:
        raise click.ClickException(f"server refused the attach handshake: {exc}")
    except OSError as exc:
        raise click.ClickException(f"could not reach {url}: {exc}")

    stdin_fd = sys.stdin.fileno()
    saved = termios.tcgetattr(stdin_fd)
    stop = threading.Event()

    def _send_resize(*_args) -> None:
        size = shutil.get_terminal_size()
        try:
            ws.send(json.dumps({"type": "resize", "rows": size.lines, "cols": size.columns}))
        except Exception:  # noqa: BLE001 — a dead socket ends the loop below
            pass

    def _pump_output() -> None:
        try:
            while not stop.is_set():
                message = ws.recv()
                if isinstance(message, bytes):
                    os.write(sys.stdout.fileno(), message)
        except (ConnectionClosed, OSError):
            pass
        finally:
            stop.set()

    reader = threading.Thread(target=_pump_output, daemon=True)
    old_winch = signal.getsignal(signal.SIGWINCH)
    try:
        tty.setraw(stdin_fd)
        signal.signal(signal.SIGWINCH, _send_resize)
        _send_resize()
        reader.start()
        while not stop.is_set():
            # Poll rather than block in os.read: the remote side detaching (or
            # the socket dying) sets `stop` from the reader thread, and a plain
            # blocking read would sit here until the user happened to press a
            # key — the detach would look like a hang.
            if not select.select([stdin_fd], [], [], 0.2)[0]:
                continue
            data = os.read(stdin_fd, 1024)
            if not data:
                break
            try:
                ws.send(json.dumps({"type": "input", "data": data.decode(errors="replace")}))
            except (ConnectionClosed, OSError):
                break
    finally:
        stop.set()
        signal.signal(signal.SIGWINCH, old_winch)
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
        click.echo("\r\n[detached]")
