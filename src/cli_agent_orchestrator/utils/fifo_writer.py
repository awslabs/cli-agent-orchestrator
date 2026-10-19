"""Copy stdin into the per-terminal FIFO; refuse anything else at that path.

This is the write end of the pipe-pane pipeline: ``TmuxClient.pipe_pane`` runs
``<python> fifo_writer.py <fifo>`` as the ``pipe-pane -o`` command, where it
used to run ``cat >> <fifo>``. A shell ``>>`` opens the path with
``O_CREAT | O_APPEND`` and follows symlinks, so a same-user process that
replaced the FIFO with a symlink or a regular file received the pane's output
while the reader, hardened in ``services.fifo_reader``, stayed attached to the
old pipe. The writer needs the same two guarantees the reader has: open with
``O_NOFOLLOW`` so a symlink at the path fails the open, and check the opened
descriptor is a FIFO before writing a byte.

Standard library only, run by file path rather than ``-m`` and under ``-I -S``
(isolated, no ``site``), so starting it imports neither the
``cli_agent_orchestrator`` package nor anything the environment names:
pipe-pane re-arms on every liveness recovery, and the writer should cost
little more to start than ``cat`` (through ``sh -c`` as tmux runs it: ~37 ms
against ~19 ms).
Exit status is 0 when stdin closes or the reader goes away, 1 when the path is
refused; tmux discards the command's stderr either way, so the message is for
anyone running it by hand.
"""

import errno
import os
import stat
import sys

_CHUNK = 64 * 1024


def open_fifo_for_writing(path: str) -> int:
    """Open ``path`` write-only without following symlinks; require a FIFO.

    Raises ``OSError`` (``ELOOP`` for a symlink, ``EEXIST`` for a non-FIFO) so
    the caller can decide how to report it. Like ``cat >>`` before it, a plain
    blocking open waits until a reader has the pipe open; ``fifo_reader`` opens
    its read end before ``pipe_pane`` is called, so in practice this returns at
    once.
    """
    fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):
            raise OSError(errno.EEXIST, f"{path} is not a FIFO; refusing to write to it")
    except BaseException:
        os.close(fd)
        raise
    return fd


def copy(src_fd: int, dst_fd: int) -> None:
    """Copy ``src_fd`` to ``dst_fd`` until EOF or until the reader disappears."""
    while True:
        chunk = os.read(src_fd, _CHUNK)
        if not chunk:
            return
        view = memoryview(chunk)
        while view:
            try:
                written = os.write(dst_fd, view)
            except BrokenPipeError:
                # The reader closed its end (terminal torn down mid-write).
                # Nothing left to deliver to; stop quietly like cat would.
                return
            view = view[written:]


def run(path: str, src_fd: int = 0) -> int:
    try:
        dst_fd = open_fifo_for_writing(path)
    except OSError as exc:
        sys.stderr.write(f"fifo_writer: {path}: {exc.strerror or exc}\n")
        return 1
    try:
        copy(src_fd, dst_fd)
    finally:
        os.close(dst_fd)
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        sys.stderr.write("usage: fifo_writer.py <fifo-path>\n")
        return 2
    return run(argv[0])


if __name__ == "__main__":  # pragma: no cover - exercised by the subprocess test
    sys.exit(main())
