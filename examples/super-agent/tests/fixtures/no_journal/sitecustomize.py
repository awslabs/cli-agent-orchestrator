"""Test-only guard: kit tests can never reach the real journal, in process or in child Pythons.

This file is the one copy of the guard logic. conftest.no_real_hook_journal imports it (as
kit_journal_guard) and applies `patches()` with monkeypatch in the test process. conftest.base_env
puts this directory on PYTHONPATH and sets KIT_TEST_NO_JOURNAL to a record file, so a child Python
imports it as `sitecustomize` and the block at the bottom applies the same patches. Without that
variable, or when imported under another name, it patches nothing.

Blocked: starting the system logger (subprocess.Popen, which subprocess.run also uses), connecting
or sending to /dev/log or a systemd journal socket, and syslog. A blocked call raises OSError, which
the hooks treat as a sink failure, so no row is written; the attempt is recorded and the parent test
fails at teardown.

KIT_TEST_JOURNAL_DECOY names a directory whose `logger` file and `dev/log` socket the guard treats as
the real ones. The guard tests aim at the decoys, so a broken guard (or a mutant of it) runs a
harmless script or reaches a test socket instead of the operator's journal.
"""
import os
import shutil

SYSTEM_LOGGER = "/usr/bin/logger"
JOURNAL_SOCKETS = ("/dev/log", "/run/systemd/journal/")


def _decoy():
    return os.environ.get("KIT_TEST_JOURNAL_DECOY")


def is_logger(args):
    first = args if isinstance(args, (str, bytes, os.PathLike)) else (
        args[0] if isinstance(args, (list, tuple)) and args else None)
    if first is None or not os.fsdecode(first).strip():
        return False
    first = os.fsdecode(first).split()[0]
    if "/" not in first:
        first = shutil.which(first) or first
    targets = {os.path.realpath(SYSTEM_LOGGER)}
    if _decoy():
        targets.add(os.path.realpath(os.path.join(_decoy(), "logger")))
    return os.path.realpath(first) in targets


def is_journal_socket(address):
    if not isinstance(address, (str, bytes)):
        return False
    path = os.fsdecode(address)
    decoy = _decoy()
    return (path.startswith(JOURNAL_SOCKETS) or path.endswith("/dev/log")
            or bool(decoy and path.startswith(os.path.join(decoy, "dev"))))


def patches(blocked):
    """(owner, attribute, replacement) triples that route every journal path to `blocked`."""
    import socket
    import subprocess
    import syslog

    real_popen = subprocess.Popen
    real_connect, real_sendto = socket.socket.connect, socket.socket.sendto

    class GuardedPopen(real_popen):
        def __init__(self, args, *rest, **kwargs):
            if is_logger(args) or is_logger(kwargs.get("executable") or []):
                blocked("system_logger")
            super().__init__(args, *rest, **kwargs)

    def guarded_connect(self, address):
        if is_journal_socket(address):
            blocked("dev_log")
        return real_connect(self, address)

    def guarded_sendto(self, data, *rest):
        if rest and is_journal_socket(rest[-1]):
            blocked("dev_log")
        return real_sendto(self, data, *rest)

    def guarded_syslog(*args, **kwargs):
        blocked("syslog")

    guarded_syslog.kit_journal_guard = True
    return [(subprocess, "Popen", GuardedPopen), (socket.socket, "connect", guarded_connect),
            (socket.socket, "sendto", guarded_sendto), (syslog, "syslog", guarded_syslog),
            (syslog, "openlog", guarded_syslog)]


_RECORD = os.environ.get("KIT_TEST_NO_JOURNAL")

if _RECORD and __name__ == "sitecustomize":
    def _blocked(what):
        with open(_RECORD, "a", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()} {what}\n")
        raise OSError(f"kit test journal guard blocked {what}")

    for _owner, _name, _value in patches(_blocked):
        setattr(_owner, _name, _value)
