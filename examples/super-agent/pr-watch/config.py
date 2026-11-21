"""Local watcher paths, optionally overridden by PR_WATCH_CONFIG JSON."""
import json
import os
from pathlib import Path


def load():
    here = Path(__file__).resolve().parent
    path = Path(os.environ.get('PR_WATCH_CONFIG', here / 'config.json'))
    data = json.loads(path.read_text()) if path.exists() else {}
    base = path.resolve().parent
    root = Path(data.get('watch_dir', here))
    if not root.is_absolute(): root = base / root
    root = root.resolve()
    def configured(key, default):
        value = Path(data.get(key, default))
        return value if value.is_absolute() else root / value
    return {'watch_dir': root, 'watch': configured('watch_path','watch.json'),
            'ledger': configured('ledger_path', '../reviews/ledger.json'),
            'digest': configured('digest_path','digest.md'),
            'queue': configured('digest_queue_path','state/digest-pending.json'),
            'journal': configured('wake_journal_path','state/wake-journal.json')}



def _kit_module(key, name):
    """Load a kit helper from the kit's bin/ by path (pr-watch sits inside the kit; a copy beside it reads super-agent/bin)."""
    import importlib.util
    import sys
    here = Path(__file__).resolve().parent
    path = here.parent / (f'bin/{name}' if here.name == 'pr-watch' else f'super-agent/bin/{name}')
    mod = sys.modules.get(key)
    if mod is None or Path(mod.__file__) != path:
        spec = importlib.util.spec_from_file_location(key, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    return mod


_LOGIN = []


def login():
    """The operator's GitHub login (workplace.json / workplace.local.json), read once per process.

    Fails closed: raises ValueError (workplace_login_unset, ...) or OSError, so the loop stops with
    configuration_error instead of treating the operator's own PRs and comments as external."""
    if not _LOGIN:
        _LOGIN.append(_kit_module('watch_workplace', 'workplace.py').github_login())
    return _LOGIN[0]


def writer_lease():
    """Use the kit's one authoritative host fence, also when Python runs with -I."""
    import importlib.util
    import sys
    here = Path(__file__).resolve().parent
    path = here.parent / ('bin/writer_lease.py' if here.name == 'pr-watch' else 'super-agent/bin/writer_lease.py')
    key = 'watch_writer_lease'
    mod = sys.modules.get(key)
    if mod is None or Path(mod.__file__) != path:
        spec = importlib.util.spec_from_file_location(key, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    return mod


def require_writer():
    lease = writer_lease()
    lease.require_writer()
    lease.require_state(str(load()['watch_dir'] / 'state'))


def writer_section(fn):
    from functools import wraps
    @wraps(fn)
    def guarded(*args, **kwargs):
        require_writer()
        with writer_lease().writer_locked():
            require_writer()
            return fn(*args, **kwargs)
    return guarded


def loop_mutation(command, pid=None):
    """Fence shell-created loop state and tick-log appends through the shared lock."""
    import sys
    require_writer()
    with writer_lease().writer_locked():
        require_writer()
        root = load()['watch_dir']
        lock = root / 'state/tick_loop.lock'
        if command == '--append-log':
            import fcntl
            # Same destination lock and realpath as state_write.py's atomic append.
            target = os.path.realpath(root / 'log.md')
            with open(target + '.lock', 'a') as lk:
                fcntl.flock(lk, fcntl.LOCK_EX)
                require_writer()
                with open(target, 'a', encoding='utf-8') as fh:
                    fh.write(sys.stdin.read())
                    fh.flush()
                    os.fsync(fh.fileno())
        elif command == '--loop-lock':
            try:
                lock.mkdir()
            except FileExistsError:
                print('WAKE none :: another_loop_running')
                return 3
            (lock / 'pid').write_text(pid + '\n')
        elif lock.exists() and (lock / 'pid').read_text().strip() == pid:
            (lock / 'pid').unlink()
            lock.rmdir()
    return 0


if __name__ == '__main__':
    import sys
    command = sys.argv[1] if len(sys.argv) > 1 else None
    if command in ('--check-writer', '--append-log', '--loop-lock', '--loop-unlock'):
        try:
            if command == '--check-writer':
                require_writer()
            else:
                sys.exit(loop_mutation(command, sys.argv[2] if len(sys.argv) > 2 else None))
        except (ValueError, OSError) as exc:
            print(str(exc) if isinstance(exc, ValueError) else 'not_writer lease_io_error')
            sys.exit(1)
    else:
        try:
            root = load()['watch_dir']
            login()
        except (ValueError, OSError) as exc:
            print(f'configuration_error: {exc}', file=sys.stderr)
            sys.exit(1)
        print(root)
