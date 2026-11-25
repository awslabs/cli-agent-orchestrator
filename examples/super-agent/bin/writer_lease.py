#!/usr/bin/env python3
"""Cooperative single-host writer fence. Stop all writers before transferring this lease.

One authoritative file: state/WRITER, host=<hostname> since=<UTC>. State is provisioned
by the operator, never by claim or a fenced tool. This is a discipline guard, not an
OS boundary or a distributed lock; copied files do not arbitrate two independent hosts.
SUPER_AGENT_STATE_DIR selects the state directory for isolated tests and migrations.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from functools import wraps
import json
import threading
import os
import re
import socket
import sys
import tempfile

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class NotWriter(ValueError):
    pass


def state_dir():
    return os.environ.get('SUPER_AGENT_STATE_DIR') or os.path.join(KIT, 'state')


def require_state(path=None):
    path = path or state_dir()
    if not os.path.isdir(path):
        raise NotWriter('not_writer state_missing_or_not_directory')
    return path


def read_lease(path=None):
    path = require_state(path)
    try:
        with open(os.path.join(path, 'WRITER'), encoding='utf-8') as fh:
            raw = fh.read()
    except FileNotFoundError:
        raise NotWriter('not_writer WRITER_missing') from None
    except (OSError, UnicodeError):
        raise NotWriter('not_writer WRITER_unreadable') from None
    match = re.fullmatch(r'host=([^\s=]+) since=([^\s]+)\n', raw)
    if match is None:
        raise NotWriter('not_writer WRITER_malformed')
    try:
        stamp = datetime.fromisoformat(match[2].replace('Z', '+00:00'))
        if stamp.utcoffset() != timezone.utc.utcoffset(stamp):
            raise ValueError('not UTC')
    except ValueError:
        raise NotWriter('not_writer WRITER_malformed') from None
    return match[1], raw


def require_writer(path=None):
    host, _ = read_lease(path)
    if host != socket.gethostname():
        raise NotWriter('not_writer WRITER_names_another_host')


_held = threading.local()


@contextmanager
def writer_locked(path=None):
    """Hold the shared lease lock through a complete write; release/claim take EX.

    Nested writers reuse the lock in this thread so lock ordering stays lease -> data.
    """
    path = os.path.realpath(path or state_dir())
    require_writer(path)  # Do not create a lock on a refused host.
    held = getattr(_held, 'paths', {})
    if path in held:
        yield
        return
    with open(os.path.join(path, 'WRITER.lock'), 'a') as fh:
        fcntl.flock(fh, fcntl.LOCK_SH)
        require_writer(path)  # A release may have won while we waited.
        _held.paths = held
        held[path] = fh
        try:
            yield
        finally:
            del held[path]


def writer_section(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        with writer_locked():
            return fn(*args, **kwargs)
    return guarded


@contextmanager
def lease_lock(path):
    # Stable lock inode; never unlink it on release.
    require_state(path)
    with open(os.path.join(path, 'WRITER.lock'), 'a') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def claim(host, force=False, path=None):
    path = require_state(path)
    if re.fullmatch(r'[^\s=]+', host) is None:
        raise NotWriter('not_writer invalid_host')
    if host != socket.gethostname() and not force:
        raise NotWriter('not_writer claim_host_is_not_this_host')
    with lease_lock(path):
        previous = 'missing'
        try:
            held, raw = read_lease(path)
        except NotWriter as exc:
            previous = 'unreadable_or_malformed' if str(exc) != 'not_writer WRITER_missing' else 'missing'
            if str(exc) != 'not_writer WRITER_missing':
                if not force:
                    raise
        else:
            previous = held
            if held == host and not force:
                return raw
            if held != host and not force:
                raise NotWriter('not_writer WRITER_names_another_host')
        if force:
            print('WARNING: FORCE replacing WRITER; stop all writers first!', file=sys.stderr)
            audit(path, 'force', previous, host)
        raw = f'host={host} since={datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}\n'
        fd, tmp = tempfile.mkstemp(prefix='.WRITER-', dir=path)
        try:
            with os.fdopen(fd, 'w') as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, os.path.join(path, 'WRITER'))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return raw


def audit(path, action, previous, host):
    # Write the intent before replacement: an audit failure must not transfer the lease.
    row = {'time': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
           'action': action, 'from': previous, 'to': host,
           'by': os.environ.get('CAO_TERMINAL_ID') or socket.gethostname()}
    with open(os.path.join(path, 'WRITER.log'), 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(row, sort_keys=True) + '\n')
        fh.flush()
        os.fsync(fh.fileno())


def release(path=None):
    path = require_state(path)
    with lease_lock(path):
        require_writer(path)
        os.unlink(os.path.join(path, 'WRITER'))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='command', required=True)
    sub.add_parser('show')
    c = sub.add_parser('claim'); c.add_argument('--host', required=True); c.add_argument('--force', action='store_true')
    sub.add_parser('release')
    args = ap.parse_args(argv)
    try:
        if args.command == 'show':
            host, raw = read_lease()
            print(raw, end='')
            print('this host: ' + ('yes' if host == socket.gethostname() else 'no'))
        elif args.command == 'claim':
            print(claim(args.host, args.force), end='')
        else:
            release(); print('RELEASED WRITER')
    except (NotWriter, OSError) as exc:
        print(str(exc) if isinstance(exc, NotWriter) else 'not_writer lease_io_error')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
