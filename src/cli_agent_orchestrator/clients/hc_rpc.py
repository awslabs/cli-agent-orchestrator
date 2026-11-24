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
# Inherited from an app terminal (or set by the operator), these would point hc
# at the caller's core instead of the session's: herdr-core docs/security.md,
# "HC_SOCKET overrides the client endpoint".
CALLER_CORE_ENV = ("HC_TERMINAL_ID", "HC_TERMINAL_SOCKET", "HC_SCOPE", "HC_SOCKET")
_EOF = object()
# Reaping an `hc connect` child we just SIGKILLed: it holds no state to flush,
# so it exits at once; the bound only keeps a wedged kernel from hanging close().
CONNECT_REAP_TIMEOUT_S = 5
# `hc server start` returns once the core's socket accepts connections (hc
# 0.1.21 reports uptime_ms in single digits); 30 s covers a cold start on a
# loaded host and matches the e2e suite's `hc server stop` bound.
SERVER_START_TIMEOUT_S = 30


class HcRpcError(TerminalBackendError):
    """herdr-core answered with an error; ``kind`` is its stable ``data.kind``."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"herdr-core {kind}: {message}")
        self.kind = kind
        self.message = message


class HcProtocolError(TerminalBackendError):
    """The core speaks a protocol generation or method set CAO does not support."""


class HcNotSentError(TerminalBackendError):
    """The request never reached herdr-core: nothing it asked for can have run."""


def hc_binary() -> str:
    path = shutil.which("hc") or shutil.which("herdr-core")
    if not path:
        raise TerminalBackendError(
            "herdr-core is not installed: neither `hc` nor `herdr-core` is on PATH"
        )
    return path


def hc_env(session: str) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in CALLER_CORE_ENV}
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
        self._proc.wait(timeout=CONNECT_REAP_TIMEOUT_S)
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
    """Request/response connection. Thread-safe; one exchange at a time.

    herdr-core answers a plain-mode connection one request at a time
    (server.rs ``connection``), so a call that the core holds (``terminal.send``
    waiting for a person's draft) holds every later call on this client.
    """

    def __init__(self, session: str) -> None:
        self.session = session
        self._lock = threading.Lock()
        self._conn: Optional[_Connection] = None
        self._ids = itertools.count(1)
        self._closed = False

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
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=SERVER_START_TIMEOUT_S,
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
                    if self._closed:
                        raise HcNotSentError(f"herdr-core client closed before {method}")
                    try:
                        self._conn = _Connection(self.session)
                    except TerminalBackendError as exc:
                        # No `hc connect` child, so the request was never written.
                        raise HcNotSentError(f"herdr-core unreachable for {method}: {exc}") from exc
                    if self._closed:  # close() ran while the child started
                        self._drop()
                        raise HcNotSentError(f"herdr-core client closed before {method}")
                try:
                    self._conn.write(request)
                    break
                except (BrokenPipeError, OSError, ValueError):
                    self._drop()  # not sent yet, so a resend is safe
                    if attempt:
                        raise HcNotSentError(f"herdr-core unreachable for {method}") from None
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
        """End the connection now, without waiting for a call in flight.

        A call the core is holding (``terminal.send`` during a person's draft)
        gets EOF and fails as lost; plain mode cancels a send still in its
        draft hold when its caller goes away (server.rs ``connection``), so
        nothing is typed later. Calls after this one fail: a closed client
        never starts another ``hc connect``.
        """
        self._closed = True
        conn = self._conn
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - already broken
                pass


class HcSubscription:
    """A connection used only for `events.subscribe` (it then carries only notifications)."""

    def __init__(self, session: str, timeout: float = 10.0) -> None:
        self._conn = _Connection(session)
        try:
            self._conn.write(
                {"jsonrpc": "2.0", "id": 1, "method": "events.subscribe", "params": {}}
            )
            self.snapshot: Dict[str, Any] = _result(self._conn.get(timeout))
        except BaseException:
            # The caller never gets this object, so nobody else can reap the child.
            self._conn.close()
            raise

    def next_notification(self, timeout: float) -> Dict[str, Any]:
        """Next notification; raises TimeoutError when silent, EOFError when closed."""
        return self._conn.get(timeout)

    def close(self) -> None:
        self._conn.close()
