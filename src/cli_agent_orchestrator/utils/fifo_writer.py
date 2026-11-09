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

The writer's lifetime belongs to its pane: tmux closes the writer's stdin when
the pipe is closed, the pane dies or the server exits, and the writer must then
exit. A plain blocking ``open()`` of a FIFO with no reader never returns and never
looks at stdin, so a writer started while its reader was not there (a reader
thread that had not opened yet, or had exited on an error) outlived its pane and
its tmux server as an orphan. So the open is non-blocking and retried, and while
it waits the writer watches stdin and exits as soon as stdin closes.
"""

import errno
import os
import select
import stat
import sys

_CHUNK = 64 * 1024
#: How long one wait for a reader lasts before the open is retried, in ms.
_WAIT_FOR_READER_MS = 50


def _stdin_closed(src_fd: int) -> bool:
    """Has ``src_fd`` hung up? Never waits.

    Pending data does not count: it stays in the pipe for ``copy`` to forward
    once a reader is there. Only a hang-up (with or without unread data left
    behind) means the pane is gone. The check is a zero-timeout poll, and the
    caller sleeps separately. Readable data could make a hang-up poll return at
    once on a platform that reports readable data to it; even then the wait
    cannot become a busy loop.
    """
    poller = select.poll()
    poller.register(src_fd, select.POLLHUP | select.POLLERR)
    for _, events in poller.poll(0):
        if events & (select.POLLHUP | select.POLLERR | select.POLLNVAL):
            return True
    return False


def open_fifo_for_writing(path: str, src_fd: "int | None" = None):
    """Open ``path`` write-only without following symlinks; require a FIFO.

    Raises ``OSError`` (``ELOOP`` for a symlink, ``EEXIST`` for a non-FIFO,
    ``ENOENT`` for a missing path) so the caller can decide how to report it.

    Like ``cat >>`` before it, this waits until a reader has the pipe open, but
    without blocking in ``open()``: ``O_NONBLOCK`` fails with ``ENXIO`` while no
    reader is attached, and the open is retried. When ``src_fd`` is given, the
    wait ends as soon as ``src_fd`` hangs up, and ``None`` is returned: the pane
    the writer serves is gone, so there is nothing left to deliver. Once open, the
    fd is switched back to blocking, so writes behave as before.
    """
    while True:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            break
        except OSError as exc:
            if exc.errno != errno.ENXIO:
                raise
            # ENXIO also answers a non-blocking open of a socket (Linux). Only a
            # FIFO is worth waiting for; anything else is refused as before.
            if not stat.S_ISFIFO(os.lstat(path).st_mode):
                raise OSError(errno.EEXIST, f"{path} is not a FIFO; refusing to write to it")
        if src_fd is not None and _stdin_closed(src_fd):
            return None
        select.select([], [], [], _WAIT_FOR_READER_MS / 1000)
    try:
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):
            raise OSError(errno.EEXIST, f"{path} is not a FIFO; refusing to write to it")
        os.set_blocking(fd, True)
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
        dst_fd = open_fifo_for_writing(path, src_fd=src_fd)
    except OSError as exc:
        sys.stderr.write(f"fifo_writer: {path}: {exc.strerror or exc}\n")
        return 1
    if dst_fd is None:
        # The pane went away before any reader attached: nothing to deliver.
        return 0
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
