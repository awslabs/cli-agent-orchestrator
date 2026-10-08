"""Owner-only, link-published HMAC keys shared by server and operator tools."""

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

TEMP_PREFIX = ".decision-hash-"
KeyStamp = tuple[int, int]


class InvalidKeyError(OSError):
    """The published hash key does not contain exactly 32 bytes."""


class InsecureKeyError(OSError):
    """The hash key file has group or other access and cannot be restricted to 0600."""


def _create_key(path: Path) -> tuple[bytes, KeyStamp] | None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.parent / (TEMP_PREFIX + secrets.token_hex(16))
    owned = False
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        owned = True
        with os.fdopen(fd, "wb") as stream:
            key = secrets.token_bytes(32)
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
            stat = os.fstat(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                return None
            return key, (stat.st_ino, stat.st_mtime_ns)
    finally:
        if owned:
            temporary.unlink(missing_ok=True)


def _restrict(fd: int, path: Path) -> None:
    # Owner-only, like a key created here; a key that cannot be restricted is not used.
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        else:  # pragma: no cover - platforms without fchmod
            os.chmod(path, 0o600)
    except OSError as error:
        raise InsecureKeyError("Decision hash key file cannot be restricted to 0600") from error


def _read_key(path: Path) -> tuple[bytes, KeyStamp]:
    for attempt in range(2):
        try:
            with path.open("rb") as stream:
                key = stream.read(33)
                stat = os.fstat(stream.fileno())
                if stat.st_mode & 0o077:
                    _restrict(stream.fileno(), path)
        except FileNotFoundError:
            try:
                created = _create_key(path)
            except FileNotFoundError:
                if attempt:
                    raise
                continue
            if created is not None:
                return created
            continue
        if len(key) != 32:
            raise InvalidKeyError("Decision hash key must contain exactly 32 bytes")
        return key, (stat.st_ino, stat.st_mtime_ns)
    raise OSError("Decision hash key publication did not complete")


def load_key(path: Path) -> bytes:
    return _read_key(path)[0]


def rotate_key(path: Path) -> None:
    path.unlink(missing_ok=True)


def cleanup_temps(path: Path) -> None:
    cutoff = time.time() - 60
    for temporary in path.parent.glob(TEMP_PREFIX + "*"):
        try:
            if temporary.stat().st_mtime < cutoff:
                temporary.unlink(missing_ok=True)
        except FileNotFoundError:
            pass


class KeyCache:
    """Refresh the server's key when the published inode or timestamp changes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._key: bytes | None = None
        self._stamp: KeyStamp | None = None

    def digest(self, message: str) -> tuple[str, str]:
        try:
            stat = self.path.stat()
            stamp = (stat.st_ino, stat.st_mtime_ns)
        except FileNotFoundError:
            stamp = None
        if self._key is None or stamp != self._stamp:
            self._key, self._stamp = _read_key(self.path)
        return (
            hmac.new(self._key, message.encode("utf-8"), hashlib.sha256).hexdigest(),
            hashlib.sha256(self._key).hexdigest()[:8],
        )
