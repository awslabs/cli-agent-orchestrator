"""Provider-originated actor-issuance channel (the provider launcher shim).

The managed provider bridge spawns this module AS the recorded provider
process; the real provider binary is this process's child. The launcher
is therefore the kernel-verifiable root of the provider process tree:
it (and only its descendants) passes the actor broker's kernel peer +
lineage gate on the generation-private bridge socket.

Two duties, nothing more:

1. Byte-transparent stdio proxy between the bridge and the real provider
   child, so the JSON-RPC session is bit-identical to running the
   provider directly (the child's stderr is inherited verbatim).
2. One provider-originated channel to the bridge: the launcher connects
   to the generation-private socket, identifies itself, and acknowledges
   issuance requests. The bridge issues actor assertions only on THIS
   connection (kernel peer credentials + live provider-tree lineage), so
   every assertion is provably provider-originated; a conductor,
   collector, reconciler, or any same-UID sibling cannot open the
   channel, exactly as the broker's lineage gate requires.

No secret appears in argv or the environment: the socket path is not a
secret (0600, kernel-verified peers), and the broker's signing key never
leaves the bridge's memory.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Optional


def _read_available(stream, size: int) -> bytes:
    """One short read: whatever bytes are available now, up to ``size``.

    ``BufferedReader.read(n)`` blocks until n bytes or EOF, which wedges a
    byte-transparent pipe pump on normal short JSON-RPC lines. ``read1``
    returns after at most one underlying raw read, so short provider
    lines flow immediately with their exact bytes preserved.
    """
    read1 = getattr(stream, "read1", None)
    if read1 is not None:
        return bytes(read1(size))
    return bytes(stream.read(size))


def _pump_stdin(child: subprocess.Popen) -> None:
    """Forward our stdin to the provider child byte-transparently."""
    assert child.stdin is not None
    try:
        while True:
            block = _read_available(sys.stdin.buffer, 65536)
            if not block:
                break
            child.stdin.write(block)
            child.stdin.flush()
    except (BrokenPipeError, ValueError):
        pass
    finally:
        try:
            child.stdin.close()
        except (BrokenPipeError, ValueError):
            pass


def _pump_stdout(child: subprocess.Popen) -> None:
    """Forward the provider child's stdout to our stdout byte-transparently."""
    assert child.stdout is not None
    try:
        while True:
            block = _read_available(child.stdout, 65536)
            if not block:
                break
            sys.stdout.buffer.write(block)
            sys.stdout.buffer.flush()
    except (BrokenPipeError, ValueError):
        pass


def _channel_loop(sock: socket.socket) -> None:
    """Answer bridge issuance requests from inside the provider tree."""
    raw = bytearray()
    while True:
        try:
            block = sock.recv(65536)
        except OSError:
            return
        if not block:
            return
        raw.extend(block)
        if len(raw) > 4 * 1024 * 1024:
            return
        while b"\n" in raw:
            line, _, rest = bytes(raw).partition(b"\n")
            raw = bytearray(rest)
            try:
                command = json.loads(line)
            except json.JSONDecodeError:
                continue
            if command.get("op") == "issue-request" and isinstance(command.get("request_id"), str):
                ack = {"op": "issue-ack", "request_id": command["request_id"]}
                try:
                    sock.sendall(json.dumps(ack).encode() + b"\n")
                except OSError:
                    return


def _open_channel(socket_path: str, binding_identity: dict[str, str]) -> Optional[socket.socket]:
    """Connect to the generation-private bridge socket as the provider peer."""
    from cli_agent_orchestrator.services.managed_provider_bridge import (
        BridgeError,
        verify_rendezvous_binding,
    )

    sock: Optional[socket.socket] = None
    for _ in range(200):
        try:
            # Re-read the O_EXCL binding immediately before every connect
            # attempt; never carry an earlier verification across retries.
            verify_rendezvous_binding(socket_path, binding_identity)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(socket_path)
            break
        except BridgeError:
            return None
        except OSError:
            if sock is not None:
                sock.close()
            sock = None
            time.sleep(0.05)
    if sock is None:
        return None
    hello = {
        "rendezvous_identity": binding_identity,
        "request": {"op": "provider-channel", "pid": os.getpid()},
    }
    try:
        sock.sendall(json.dumps(hello).encode() + b"\n")
    except OSError:
        sock.close()
        return None
    return sock


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--identity-json", required=True)
    parser.add_argument(
        "provider_argv",
        nargs=argparse.REMAINDER,
        help="the real provider argv after --",
    )
    args = parser.parse_args(argv)
    try:
        binding_identity = json.loads(args.identity_json)
    except json.JSONDecodeError:
        parser.error("--identity-json must be valid JSON")
    if not isinstance(binding_identity, dict):
        parser.error("--identity-json must name an object")
    provider_argv = list(args.provider_argv)
    if provider_argv and provider_argv[0] == "--":
        provider_argv = provider_argv[1:]
    if not provider_argv:
        parser.error("the real provider argv is required after --")

    # Verify the full rendezvous tuple and establish the provider-originated
    # channel before the real provider child can have any effect.
    channel = _open_channel(args.socket, binding_identity)
    if channel is None:
        return 1
    try:
        child = subprocess.Popen(
            provider_argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # The child's stderr is inherited verbatim: the bridge's provider
            # stderr log line stream is unchanged by the shim.
            stderr=None,
            env=dict(os.environ),
        )
    except Exception:
        channel.close()
        raise

    def _forward_signal(signum, frame):  # noqa: ARG001 - signal handler shape
        if child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, _forward_signal)
    signal.signal(signal.SIGINT, _forward_signal)

    threading.Thread(target=_channel_loop, args=(channel,), daemon=True).start()

    threading.Thread(target=_pump_stdin, args=(child,), daemon=True).start()
    _pump_stdout(child)
    returncode = child.wait()
    channel.close()
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
