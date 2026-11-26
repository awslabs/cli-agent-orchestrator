#!/usr/bin/env python3
"""Fenced atomic state/log writes and backups. Input is a prepared file or stdin.

state_write.py --out PATH [--from FILE] [--append]
Never creates parent directories. Hold lease -> destination lock through read/replace.
Use for agent-maintained state and PB/log.md; schema-aware ledgers keep their own tools.
"""
import argparse
import fcntl
import importlib.util
import os
from pathlib import Path
import sys
import tempfile

path = Path(__file__).resolve().with_name('writer_lease.py')
spec = importlib.util.spec_from_file_location('state_write_lease', path)
WL = importlib.util.module_from_spec(spec)
spec.loader.exec_module(WL)


def write(path, data, append=False):
    target = os.path.realpath(path)
    with WL.writer_locked():
        with open(target + '.lock', 'a') as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            WL.require_writer()
            if append:
                try:
                    with open(target, 'rb') as fh:
                        data = fh.read() + data
                except FileNotFoundError:
                    pass
            fd, tmp = tempfile.mkstemp(prefix='.state-write-', dir=os.path.dirname(target))
            try:
                with os.fdopen(fd, 'wb') as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, target)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', required=True)
    ap.add_argument('--from', dest='source')
    ap.add_argument('--append', action='store_true')
    a = ap.parse_args(argv)
    try:
        # Refuse before waiting for stdin or reading the prepared input.
        WL.require_writer()
        data = Path(a.source).read_bytes() if a.source else sys.stdin.buffer.read()
        write(a.out, data, a.append)
    except (WL.NotWriter, OSError) as exc:
        print(str(exc) if isinstance(exc, WL.NotWriter) else 'ERROR state_write_io_error')
        return 1
    print('WROTE')
    return 0


if __name__ == '__main__':
    sys.exit(main())
