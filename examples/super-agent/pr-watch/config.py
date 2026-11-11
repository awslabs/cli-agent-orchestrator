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

if __name__ == '__main__':
    print(load()['watch_dir'])
