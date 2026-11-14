"""bin/wait-herdr-socket.sh: cao-server starts only once herdr's socket exists."""

from __future__ import annotations

import shutil
import socket
import tempfile
from pathlib import Path

import pytest
from remote_ec2_support import FAKE_INSTANCE_ID

SESSION = f"cao-{FAKE_INSTANCE_ID}"


@pytest.fixture()
def short_home():
    # Unix socket paths are limited to about 104 bytes on macOS: use /tmp.
    folder = Path(tempfile.mkdtemp(prefix="re2-", dir="/tmp"))
    try:
        yield folder
    finally:
        shutil.rmtree(folder)


def box_env(sandbox) -> str:
    path = sandbox.root / "box.env"
    path.write_text(f"CAO_BOX_SESSION={SESSION}\nCAO_HERDR_SESSION={SESSION}\n", encoding="utf-8")
    return str(path)


def test_passes_when_the_socket_exists(sandbox, short_home) -> None:
    sock_dir = short_home / ".config" / "herdr" / "sessions" / SESSION
    sock_dir.mkdir(parents=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(sock_dir / "herdr.sock"))
        result = sandbox.run(
            "bin/wait-herdr-socket.sh",
            HOME=str(short_home),
            BOX_ENV_FILE=box_env(sandbox),
            HERDR_SOCKET_WAIT_SECONDS="1",
        )
    finally:
        server.close()
    assert result.returncode == 0, result.stderr
    assert "socket ready" in result.stdout


def test_fails_when_the_socket_never_appears(sandbox, short_home) -> None:
    result = sandbox.run(
        "bin/wait-herdr-socket.sh",
        HOME=str(short_home),
        BOX_ENV_FILE=box_env(sandbox),
        HERDR_SOCKET_WAIT_SECONDS="1",
    )
    assert result.returncode == 1
    assert "not starting cao-server" in result.stderr


def test_a_plain_file_is_not_a_socket(sandbox, short_home) -> None:
    sock_dir = short_home / ".config" / "herdr" / "sessions" / SESSION
    sock_dir.mkdir(parents=True)
    (sock_dir / "herdr.sock").write_text("", encoding="utf-8")
    result = sandbox.run(
        "bin/wait-herdr-socket.sh",
        HOME=str(short_home),
        BOX_ENV_FILE=box_env(sandbox),
        HERDR_SOCKET_WAIT_SECONDS="0",
    )
    assert result.returncode == 1


def test_a_missing_or_bad_box_env_fails(sandbox, short_home) -> None:
    missing = sandbox.run(
        "bin/wait-herdr-socket.sh", HOME=str(short_home), BOX_ENV_FILE=str(sandbox.root / "nope")
    )
    assert missing.returncode == 1
    bad = sandbox.root / "bad.env"
    bad.write_text("CAO_BOX_SESSION=../../etc\n", encoding="utf-8")
    result = sandbox.run("bin/wait-herdr-socket.sh", HOME=str(short_home), BOX_ENV_FILE=str(bad))
    assert result.returncode == 1
    assert "malformed" in result.stderr


def test_a_herdr_session_that_differs_from_the_box_session_fails(sandbox, short_home) -> None:
    # CAO uses CAO_HERDR_SESSION: if it pointed elsewhere, CAO would find no
    # socket there and start a herdr of its own. The gate must refuse.
    sock_dir = short_home / ".config" / "herdr" / "sessions" / SESSION
    sock_dir.mkdir(parents=True)
    env_file = sandbox.root / "split.env"
    env_file.write_text(
        f"CAO_BOX_SESSION={SESSION}\nCAO_HERDR_SESSION=cao-i-0000000000000000b\n", encoding="utf-8"
    )
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(sock_dir / "herdr.sock"))
        result = sandbox.run(
            "bin/wait-herdr-socket.sh",
            HOME=str(short_home),
            BOX_ENV_FILE=str(env_file),
            HERDR_SOCKET_WAIT_SECONDS="1",
        )
    finally:
        server.close()
    assert result.returncode == 1
    assert "not starting cao-server" in result.stderr
    assert "differ" in result.stderr


def test_an_extra_line_in_box_env_fails(sandbox, short_home) -> None:
    env_file = sandbox.root / "extra.env"
    env_file.write_text(
        f"CAO_BOX_SESSION={SESSION}\nCAO_HERDR_SESSION={SESSION}\nLD_PRELOAD=/dev/shm/x.so\n",
        encoding="utf-8",
    )
    result = sandbox.run(
        "bin/wait-herdr-socket.sh", HOME=str(short_home), BOX_ENV_FILE=str(env_file)
    )
    assert result.returncode == 1
