"""JSON-RPC client for herdr-core, spoken through `hc connect --session <s>`.

`hc connect` is herdr-core's own cross-platform entry point (the Herdr app uses
it over SSH), so CAO never guesses the per-OS socket path.
"""

import itertools
import json
import os
import queue
import shutil
import subprocess
import threading
from typing import Any, Callable, Dict, Optional, cast

from cli_agent_orchestrator.backends.base import TerminalBackendError

SUPPORTED_GENERATION = 1
REQUIRED_METHODS = frozenset(
    {
        "hello",
        "server.status",
        "terminal.run",
        "terminal.list",
        "terminal.get",
        "terminal.kill",
        "terminal.rm",
        "terminal.read",
        "terminal.send",
        "terminal.keys",
        "terminal.label",
        "doc.get",
        "doc.set",
        "events.subscribe",
    }
)
# Inherited from an app terminal, these would point hc at the caller's core.
_STRIPPED_ENV = ("HC_TERMINAL_ID", "HC_TERMINAL_SOCKET", "HC_SCOPE")
_EOF = object()


class HcRpcError(TerminalBackendError):
    """herdr-core answered with an error; ``kind`` is its stable ``data.kind``."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"herdr-core {kind}: {message}")
        self.kind = kind
        self.message = message


class HcProtocolError(TerminalBackendError):
    """The core speaks a protocol generation or method set CAO does not support."""


def hc_binary() -> str:
    path = shutil.which("hc") or shutil.which("herdr-core")
    if not path:
        raise TerminalBackendError(
            "herdr-core is not installed: neither `hc` nor `herdr-core` is on PATH"
        )
    return path


def hc_env(session: str) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV}
    env["HC_SESSION"] = session
    return env


class _Connection:
    """One `hc connect` child; a reader thread feeds parsed lines into a queue."""

    def __init__(self, session: str) -> None:
        try:
            self._proc = subprocess.Popen(
                [hc_binary(), "connect", "--session", session],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=hc_env(session),
            )
        except OSError as exc:
            raise TerminalBackendError(f"could not start hc connect: {exc}") from exc
        self._lines: "queue.Queue[Any]" = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                self._lines.put(message)
        self._lines.put(_EOF)

    def alive(self) -> bool:
        return self._proc.poll() is None

    def write(self, message: Dict[str, Any]) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(message) + "\n")
        self._proc.stdin.flush()

    def get(self, timeout: float) -> Dict[str, Any]:
        try:
            item = self._lines.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"herdr-core sent nothing for {timeout}s") from None
        if item is _EOF:
            self._lines.put(_EOF)  # keep reporting EOF to later readers
            raise EOFError("herdr-core connection closed")
        return cast(Dict[str, Any], item)

    def close(self) -> None:
        if self.alive():
            self._proc.kill()
        self._proc.wait(timeout=5)
        if self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
            except OSError:
                pass


def _result(message: Dict[str, Any]) -> Any:
    if "error" in message:
        err = message["error"] or {}
        raise HcRpcError(
            str((err.get("data") or {}).get("kind", "error")), str(err.get("message", ""))
        )
    return message.get("result")


class HcRpcClient:
    """Request/response connection. Thread-safe; one exchange at a time."""

    def __init__(self, session: str) -> None:
        self.session = session
        self._lock = threading.Lock()
        self._conn: Optional[_Connection] = None
        self._ids = itertools.count(1)

    def start_server(self, drop_env: Optional[Callable[[str], bool]] = None) -> None:
        """Start the session's core if it is not running (idempotent).

        ``drop_env`` names keys to keep out of the core's environment, which
        every terminal it starts inherits. It only matters when this call is
        the one that starts the core.
        """
        env = hc_env(self.session)
        if drop_env is not None:
            env = {k: v for k, v in env.items() if k == "HC_SESSION" or not drop_env(k)}
        done = subprocess.run(
            [hc_binary(), "server", "start", "--json"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if done.returncode != 0:
            raise TerminalBackendError(f"hc server start failed: {done.stderr.strip()}")

    def call(
        self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 10.0
    ) -> Any:
        with self._lock:
            rid = next(self._ids)
            request = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
            for attempt in (0, 1):
                if self._conn is None or not self._conn.alive():
                    self._drop()
                    self._conn = _Connection(self.session)
                try:
                    self._conn.write(request)
                    break
                except (BrokenPipeError, OSError):
                    self._drop()  # not sent yet, so a resend is safe
                    if attempt:
                        raise TerminalBackendError(f"herdr-core unreachable for {method}") from None
            assert self._conn is not None
            try:
                while True:
                    message = self._conn.get(timeout)
                    if message.get("id") == rid:
                        break
            except (EOFError, TimeoutError) as exc:
                # Sent but unanswered: it may have run, so never resend.
                self._drop()
                raise TerminalBackendError(f"herdr-core lost {method}: {exc}") from None
        return _result(message)

    def hello(self) -> Dict[str, Any]:
        info = self.call("hello")
        if not isinstance(info, dict):
            raise HcProtocolError(f"herdr-core hello returned {info!r}")
        generation = info.get("generation")
        if generation != SUPPORTED_GENERATION:
            raise HcProtocolError(
                f"herdr-core speaks protocol generation {generation}; CAO supports {SUPPORTED_GENERATION}"
            )
        missing = sorted(REQUIRED_METHODS - set(info.get("methods") or []))
        if missing:
            raise HcProtocolError(f"herdr-core lacks methods CAO needs: {', '.join(missing)}")
        return info

    def _drop(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - already broken
                pass
        self._conn = None

    def close(self) -> None:
        with self._lock:
            self._drop()


class HcSubscription:
    """A connection used only for `events.subscribe` (it then carries only notifications)."""

    def __init__(self, session: str, timeout: float = 10.0) -> None:
        self._conn = _Connection(session)
        self._conn.write({"jsonrpc": "2.0", "id": 1, "method": "events.subscribe", "params": {}})
        reply = self._conn.get(timeout)
        self.snapshot: Dict[str, Any] = _result(reply)

    def next_notification(self, timeout: float) -> Dict[str, Any]:
        """Next notification; raises TimeoutError when silent, EOFError when closed."""
        return self._conn.get(timeout)

    def close(self) -> None:
        self._conn.close()
