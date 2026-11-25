import json
import os
from pathlib import Path
import shutil
import socket
import sys
import pytest
SOURCE=Path(os.environ.get('PR_WATCH_SOURCE',Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(SOURCE))
# The watcher's tests run as the operator "your-login" (the fixture identity in their data); every
# subprocess that copies os.environ inherits it. Tests that build their own env add it themselves.
WORKPLACE_FIXTURE=Path(__file__).resolve().parent/'fixtures'/'workplace.local.json'
os.environ['CAO_WORKPLACE_CONFIG']=str(WORKPLACE_FIXTURE)


@pytest.fixture(autouse=True)
def writer_world(tmp_path,monkeypatch,request):
    if request.module.__name__=='test_writer_fence':
        return
    state=tmp_path/'fixture-kitstate';state.mkdir()
    (state/'WRITER').write_text(f'host={socket.gethostname()} since=2026-11-13T00:00:00Z\n')
    (tmp_path/'state').mkdir(exist_ok=True)
    config=tmp_path/'config.json';config.write_text(json.dumps({'watch_dir':str(tmp_path)}))
    monkeypatch.setenv('SUPER_AGENT_STATE_DIR',str(state))
    monkeypatch.setenv('PR_WATCH_CONFIG',str(config))
    # Original loop tests copy only watcher modules; keep the guarded kit helper beside them.
    helper=tmp_path.parent/'super-agent/bin';helper.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(SOURCE.parent/'bin/writer_lease.py',helper/'writer_lease.py')
    shutil.copyfile(SOURCE.parent/'bin/workplace.py',helper/'workplace.py')
    shutil.copyfile(SOURCE.parent/'workplace.json',helper.parent/'workplace.json')  # the kit folder is the workplace
