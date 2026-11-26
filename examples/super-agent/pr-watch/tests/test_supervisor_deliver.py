"""tick_supervisor.sh never drops a WAKE: it is kept before delivery, retried, and delivered once."""
import http.server
import os
import shutil
import signal
import subprocess
import threading
import time
import urllib.parse

import pytest

from conftest import SOURCE
from test_loop import setup as setup_loop

WAKE = "WAKE reports/tick-x.json :: pr#9:new own PR"
NOT_WRITER = "WAKE none :: not_writer"


def stub_loop(line, rc=0):
    """A stand-in tick_loop.sh: counts its runs; the first run prints `line`, later runs are quiet."""
    return f"""#!/bin/bash
S="$(dirname -- "$0")/state"
echo run >> "$S/stub_runs"
if [ "$(wc -l < "$S/stub_runs")" -eq 1 ]; then echo '{line}'; fi
exit {rc}
"""


class Inbox(http.server.BaseHTTPRequestHandler):
    """Refuses the first `fail` POSTs with 503, then accepts and records each message."""
    fail = 0
    attempts = 0
    received = []

    def do_POST(self):
        cls = type(self)
        cls.attempts += 1
        if cls.attempts <= cls.fail:
            self.send_response(503)
            self.end_headers()
            return
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
        cls.received.append((self.path.split("?")[0], q["sender_id"][0], q["message"][0]))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(cls.response_body)

    def log_message(self, *args):
        pass


@pytest.fixture
def inbox():
    handler = type("TestInbox", (Inbox,), {"fail": 0, "attempts": 0, "received": [],
                                          "response_body": b'{"success": true}'})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield handler, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def tree(tmp_path, line=WAKE, rc=0):
    pb = tmp_path / "wp" / "pr-watch"
    (pb / "state").mkdir(parents=True)
    shutil.copyfile(SOURCE / "tick_supervisor.sh", pb / "tick_supervisor.sh")
    (pb / "tick_loop.sh").write_text(stub_loop(line, rc))
    return pb


def env_for(api, cycles, tries, backoff=0):
    return {"PATH": "/usr/bin:/bin", "SHEPHERD_ID": "aaaa1111", "SENDER_ID": "bbbb2222",
            "CAO_API": api, "DELIVER_TRIES": str(tries), "DELIVER_BACKOFF": str(backoff),
            "SUPERVISOR_CYCLES": str(cycles), "SUPERVISOR_SLEEP": "0", "SUPERVISOR_ERROR_SLEEP": "0"}


def supervise(pb, api, cycles, tries):
    return subprocess.run(["bash", str(pb / "tick_supervisor.sh")], env=env_for(api, cycles, tries),
                          capture_output=True, text=True, timeout=60)


def runs(pb):
    f = pb / "state" / "stub_runs"
    return len(f.read_text().splitlines()) if f.exists() else 0


def messages(handler):
    return [m for _, _, m in handler.received]


def wait_for(predicate, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_a_failed_post_is_kept_and_arrives_exactly_once_on_a_later_cycle(tmp_path, inbox):
    handler, api = inbox
    handler.fail = 3  # cycle 1: both tries refused, the WAKE stays kept; cycle 2: one more refusal, then 2xx
    pb = tree(tmp_path)
    r = supervise(pb, api, cycles=3, tries=2)
    assert r.returncode == 0, r.stdout + r.stderr
    assert handler.attempts == 4
    assert len(handler.received) == 1
    path, sender, message = handler.received[0]
    assert path == "/terminals/aaaa1111/inbox/messages" and sender == "bbbb2222"
    assert message.startswith(WAKE + "  [from tick_supervisor")
    assert not (pb / "state" / "tick_supervisor.pending").exists()
    log = (pb / "state" / "tick_supervisor.log").read_text()
    assert "kept WAKE in" in log and "delivered a kept WAKE" in log


def test_no_new_loop_starts_while_a_wake_is_still_pending(tmp_path, inbox):
    handler, api = inbox
    handler.fail = 100
    pb = tree(tmp_path)
    r = supervise(pb, api, cycles=4, tries=2)
    assert r.returncode == 0, r.stdout + r.stderr
    assert handler.received == []
    assert runs(pb) == 1  # the first loop printed the WAKE; no loop ran while it stayed undelivered
    kept = (pb / "state" / "tick_supervisor.pending").read_text().splitlines()
    assert len(kept) == 1 and kept[0].startswith(WAKE)


def test_a_loop_does_not_start_before_wakes_kept_by_an_earlier_supervisor_are_delivered(tmp_path, inbox):
    handler, api = inbox
    handler.fail = 100
    pb = tree(tmp_path)
    (pb / "state" / "tick_supervisor.pending").write_text("WAKE reports/old.json :: pr#7:review\n")
    r = supervise(pb, api, cycles=3, tries=1)
    assert r.returncode == 0, r.stdout + r.stderr
    assert runs(pb) == 0


def test_kept_wakes_are_delivered_oldest_first_before_any_new_one(tmp_path, inbox):
    handler, api = inbox
    pb = tree(tmp_path)
    (pb / "state" / "tick_supervisor.pending").write_text(
        "WAKE reports/old1.json :: pr#7:review\nWAKE reports/old2.json :: pr#8:ci\n")
    r = supervise(pb, api, cycles=2, tries=1)
    assert r.returncode == 0, r.stdout + r.stderr
    got = messages(handler)
    assert got[:2] == ["WAKE reports/old1.json :: pr#7:review", "WAKE reports/old2.json :: pr#8:ci"]
    assert len(got) == 3 and got[2].startswith(WAKE)
    assert not (pb / "state" / "tick_supervisor.pending").exists()


def test_a_first_try_success_posts_once_and_keeps_nothing(tmp_path, inbox):
    handler, api = inbox
    pb = tree(tmp_path)
    r = supervise(pb, api, cycles=2, tries=4)
    assert r.returncode == 0, r.stdout + r.stderr
    assert handler.attempts == 1 and len(handler.received) == 1
    assert not (pb / "state" / "tick_supervisor.pending").exists()


def test_five_start_error_wakes_then_stop_the_supervisor(tmp_path, inbox):
    handler, api = inbox
    pb = tree(tmp_path, "WAKE none :: configuration_error", rc=2)
    (pb / "tick_loop.sh").write_text('''#!/bin/bash
echo run >> "$(dirname "$0")/state/stub_runs"
echo 'WAKE none :: configuration_error'
exit 2
''')
    r = supervise(pb, api, cycles=6, tries=1)
    assert r.returncode == 2, r.stdout + r.stderr
    assert runs(pb) == 5 and len(messages(handler)) == 6
    assert handler.received[-1][0] == f"/terminals/{env_for(api, 0, 1)['SENDER_ID']}/inbox/messages"
    assert messages(handler)[-1].startswith("LOOP_ERROR ")
    stopped = (pb / "state" / "tick_supervisor.stopped").read_text()
    assert "LOOP_ERROR " in stopped and "configuration_error" in stopped
    assert not (pb / "state" / "tick_supervisor.pid").exists()


def test_error_wakes_back_off_then_stop_with_one_notice(tmp_path, inbox):
    handler, api = inbox
    pb = tree(tmp_path, "WAKE reports/retry.wake.json :: poll_errors=1/1", rc=2)
    (pb / "tick_loop.sh").write_text('''#!/bin/bash
echo run >> "$(dirname "$0")/state/stub_runs"
echo 'WAKE reports/retry.wake.json :: poll_errors=1/1'
exit 2
''')
    sleep_dir = tmp_path / "bin"
    sleep_dir.mkdir()
    (sleep_dir / "sleep").write_text(f'#!/bin/bash\necho "$1" >> "{tmp_path}/pauses"\n')
    (sleep_dir / "sleep").chmod(0o755)
    env = env_for(api, 6, 1)
    env.update(PATH=f"{sleep_dir}:/usr/bin:/bin", SUPERVISOR_SLEEP="5", INTERVAL="40")
    result = subprocess.run(["bash", str(pb / "tick_supervisor.sh")], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2, result.stdout + result.stderr
    assert runs(pb) == 5
    assert [message.split("  [")[0] for message in messages(handler)[:5]] == [
        "WAKE reports/retry.wake.json :: poll_errors=1/1"] * 5
    assert len([message for message in messages(handler) if message.startswith("LOOP_ERROR")]) == 1
    assert (tmp_path / "pauses").read_text().splitlines() == ["5", "10", "20", "40"]
    assert "supervisor stopped; report BLOCKED" in messages(handler)[4]


def test_a_clean_exit_resets_the_error_backoff(tmp_path, inbox):
    _, api = inbox
    pb = tree(tmp_path)
    (pb / "tick_loop.sh").write_text('''#!/bin/bash
S="$(dirname "$0")/state"; echo run >> "$S/stub_runs"
case "$(wc -l < "$S/stub_runs")" in
1|2|4) echo 'WAKE reports/error.json :: rule_error'; exit 2;;
esac
exit 0
''')
    sleep_dir = tmp_path / "bin"
    sleep_dir.mkdir()
    (sleep_dir / "sleep").write_text(f'#!/bin/bash\necho "$1" >> "{tmp_path}/pauses"\n')
    (sleep_dir / "sleep").chmod(0o755)
    env = env_for(api, 5, 1)
    env.update(PATH=f"{sleep_dir}:/usr/bin:/bin", SUPERVISOR_SLEEP="5", INTERVAL="40")
    result = subprocess.run(["bash", str(pb / "tick_supervisor.sh")], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "pauses").read_text().splitlines() == ["5", "10", "5", "5", "5"]


def test_error_pause_is_capped_at_the_poll_interval(tmp_path, inbox):
    _, api = inbox
    pb = tree(tmp_path, "WAKE none :: poll_errors=1/1", rc=2)
    (pb / "tick_loop.sh").write_text('''#!/bin/bash
echo run >> "$(dirname "$0")/state/stub_runs"
echo 'WAKE none :: poll_errors=1/1'; exit 2
''')
    sleep_dir = tmp_path / "bin"
    sleep_dir.mkdir()
    (sleep_dir / "sleep").write_text(f'#!/bin/bash\necho "$1" >> "{tmp_path}/pauses"\n')
    (sleep_dir / "sleep").chmod(0o755)
    env = env_for(api, 6, 1)
    env.update(PATH=f"{sleep_dir}:/usr/bin:/bin", SUPERVISOR_SLEEP="5", INTERVAL="6")
    result = subprocess.run(["bash", str(pb / "tick_supervisor.sh")], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2, result.stdout + result.stderr
    assert (tmp_path / "pauses").read_text().splitlines() == ["5", "6", "6", "6"]


def test_quiet_ticks_between_error_exits_reset_the_count(tmp_path, inbox):
    _, api = inbox
    pb = tree(tmp_path)
    (pb / "tick_loop.sh").write_text('''#!/bin/bash
S="$(dirname "$0")/state"; echo run >> "$S/stub_runs"
if [ "$(wc -l < "$S/stub_runs")" -gt 1 ]; then touch "$S/tick_loop.clean"; fi
echo 'WAKE none :: poll_errors=1/1'; exit 2
''')
    result = supervise(pb, api, cycles=6, tries=1)
    assert result.returncode == 0, result.stdout + result.stderr
    assert runs(pb) == 6


def test_clean_exit_then_one_error_does_not_stop_after_four_errors(tmp_path, inbox):
    _, api = inbox
    pb = tree(tmp_path)
    (pb / "tick_loop.sh").write_text('''#!/bin/bash
S="$(dirname "$0")/state"; echo run >> "$S/stub_runs"
if [ "$(wc -l < "$S/stub_runs")" -eq 5 ]; then
  echo 'WAKE reports/clean.json :: pr#9:review'; exit 0
fi
echo 'WAKE none :: poll_errors=1/1'; exit 2
''')
    result = supervise(pb, api, cycles=6, tries=1)
    assert result.returncode == 0, result.stdout + result.stderr
    assert runs(pb) == 6


def test_error_without_wake_reports_no_wake_to_conductor(tmp_path, inbox):
    handler, api = inbox
    pb = tree(tmp_path, line="", rc=2)
    result = supervise(pb, api, cycles=6, tries=1)
    assert result.returncode == 2, result.stdout + result.stderr
    assert runs(pb) == 5 and len(messages(handler)) == 1
    assert "exit_status_2" in messages(handler)[0]
    assert "no wake was sent" in messages(handler)[0]


def test_planned_stop_during_fifth_error_does_not_write_stop_reason(tmp_path, inbox):
    handler, api = inbox
    pb = tree(tmp_path)
    (pb / "tick_loop.sh").write_text('''#!/bin/bash
S="$(dirname "$0")/state"; echo run >> "$S/stub_runs"
if [ "$(wc -l < "$S/stub_runs")" -eq 5 ]; then
  echo $$ > "$S/fifth_pid"
  trap 'exit 143' TERM
  sleep 30 >/dev/null & wait $!
  exit 143
fi
echo 'WAKE none :: poll_errors=1/1'; exit 2
''')
    proc = subprocess.Popen(["bash", str(pb / "tick_supervisor.sh")], env=env_for(api, 0, 1),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_for(lambda: (pb / "state" / "fifth_pid").exists())
        loop_pid = int((pb / "state" / "fifth_pid").read_text())
        assert wait_for(lambda: subprocess.run(["pgrep", "-P", str(loop_pid), "-x", "sleep"],
                                               capture_output=True).returncode == 0)
        sleepers = subprocess.run(["pgrep", "-P", str(loop_pid), "-x", "sleep"],
                                  capture_output=True, text=True).stdout.splitlines()
        proc.terminate()
        os.kill(loop_pid, signal.SIGTERM)
        for sleeper in sleepers:
            os.kill(int(sleeper), signal.SIGTERM)
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    assert not (pb / "state" / "tick_supervisor.stopped").exists()
    assert not any(message.startswith("LOOP_ERROR") for message in messages(handler))


def test_new_supervisor_archives_previous_stop_reason(tmp_path, inbox):
    _, api = inbox
    pb = tree(tmp_path, line="", rc=0)
    stopped = pb / "state" / "tick_supervisor.stopped"
    stopped.write_text("LOOP_ERROR old reason\n")
    result = supervise(pb, api, cycles=1, tries=1)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not stopped.exists()
    assert (pb / "state" / "tick_supervisor.stopped.prev").read_text() == "LOOP_ERROR old reason\n"


@pytest.mark.parametrize("response", [b'{"success": false}', b'not json'])
def test_unsuccessful_http_200_keeps_wake(tmp_path, inbox, response):
    handler, api = inbox
    handler.response_body = response
    pb = tree(tmp_path)
    result = supervise(pb, api, cycles=3, tries=1)
    assert result.returncode == 0, result.stdout + result.stderr
    assert runs(pb) == 1
    assert (pb / "state" / "tick_supervisor.pending").read_text().startswith(WAKE)


def test_real_loop_with_failing_gh_stops_after_five_wakes(tmp_path, inbox):
    handler, api = inbox
    pb = tree(tmp_path)
    env = setup_loop(pb, [{"errors": [{"error": "unauthorized"}]}])
    shutil.copyfile(SOURCE / "tick_supervisor.sh", pb / "tick_supervisor.sh")
    helper = pb.parent / "bin"
    helper.mkdir()
    for name in ("writer_lease.py", "workplace.py"):
        shutil.copyfile(SOURCE.parent / "bin" / name, helper / name)
    shutil.copyfile(SOURCE.parent / "workplace.json", pb.parent / "workplace.json")
    (pb / "poll.py").write_text('''import json, subprocess
result = subprocess.run(['gh', 'api', 'user'], capture_output=True)
print(json.dumps({'polled_at': '2026-11-09T00:00Z',
                  'errors': [{'error': 'unauthorized'}] if result.returncode else []}))
''')
    stubs = tmp_path / "bin"
    stubs.mkdir()
    (stubs / "gh").write_text('#!/bin/sh\nexit 1\n')
    (stubs / "sleep").write_text(f'#!/bin/sh\necho "$1" >> "{tmp_path}/pauses"\n')
    for name in ("gh", "sleep"):
        (stubs / name).chmod(0o755)
    env.update(env_for(api, 6, 1))
    env.update(PATH=f"{stubs}:/usr/bin:/bin", INTERVAL="40", RETRY_DELAY="0",
               SUPERVISOR_SLEEP="5")
    result = subprocess.run(["bash", str(pb / "tick_supervisor.sh")], env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 2, result.stdout + result.stderr
    assert len([message for message in messages(handler) if message.startswith("WAKE") and
                "poll_errors=1/1" in message]) == 5
    assert len([message for message in messages(handler) if message.startswith("LOOP_ERROR")]) == 1
    assert (tmp_path / "pauses").read_text().splitlines().count("5") == 1
    assert [pause for pause in (tmp_path / "pauses").read_text().splitlines() if pause != "0"] == [
        "5", "10", "20", "40"]


def test_supervisor_and_loop_stop_during_interval_sleep(tmp_path, inbox):
    _, api = inbox
    pb = tree(tmp_path)
    env = setup_loop(pb, [{"polled_at": "2026-11-09T00:00Z", "errors": []}])
    shutil.copyfile(SOURCE / "tick_supervisor.sh", pb / "tick_supervisor.sh")
    helper = pb.parent / "bin"
    helper.mkdir()
    for name in ("writer_lease.py", "workplace.py"):
        shutil.copyfile(SOURCE.parent / "bin" / name, helper / name)
    shutil.copyfile(SOURCE.parent / "workplace.json", pb.parent / "workplace.json")
    env.update(env_for(api, 0, 1))
    env.update(INTERVAL="8", RETRY_DELAY="0")
    proc = subprocess.Popen(["bash", str(pb / "tick_supervisor.sh")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    loop_pid = None
    try:
        lock_pid = pb / "state" / "tick_loop.lock" / "pid"
        assert wait_for(lambda: lock_pid.exists())
        loop_pid = int(lock_pid.read_text().strip())
        assert wait_for(lambda: subprocess.run(["pgrep", "-P", str(loop_pid), "-x", "sleep"],
                                               capture_output=True).returncode == 0)
        proc.terminate()
        os.kill(loop_pid, signal.SIGTERM)
        assert wait_for(lambda: proc.poll() is not None and
                        not (pb / "state" / "tick_supervisor.pid").exists(), timeout=3)
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=15)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
def test_a_stop_or_kill_during_the_retry_window_keeps_the_wake_for_exactly_one_later_delivery(tmp_path, inbox, sig):
    handler, api = inbox
    handler.fail = 10_000
    pb = tree(tmp_path)
    log = pb / "state" / "tick_supervisor.log"
    proc = subprocess.Popen(["bash", str(pb / "tick_supervisor.sh")], env=env_for(api, 0, 4, backoff=2),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_for(lambda: log.exists() and "deliver failed (attempt 1 of 4)" in log.read_text())
        proc.send_signal(sig)
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    if sig == signal.SIGTERM:
        assert proc.returncode == 0
    kept = (pb / "state" / "tick_supervisor.pending").read_text().splitlines()
    assert len(kept) == 1 and kept[0].startswith(WAKE)
    handler.fail = 0
    r = supervise(pb, api, cycles=2, tries=1)
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(handler.received) == 1 and messages(handler)[0].startswith(WAKE)
    assert not (pb / "state" / "tick_supervisor.pending").exists()


def test_blank_lines_in_the_pending_file_are_skipped_never_posted(tmp_path, inbox):
    handler, api = inbox
    pb = tree(tmp_path)
    (pb / "state" / "tick_supervisor.pending").write_text("\n  \nWAKE reports/old.json :: pr#7:review\n\n")
    r = supervise(pb, api, cycles=2, tries=1)
    assert r.returncode == 0, r.stdout + r.stderr
    got = messages(handler)
    assert "" not in got and all(m.strip() for m in got)
    assert got[0] == "WAKE reports/old.json :: pr#7:review" and len(got) == 2


def unusable(pb, r, handler):
    assert r.returncode == 4, r.stdout + r.stderr
    assert "STOP: pending file unusable" in r.stdout
    assert "STOP: pending file unusable" in (pb / "state" / "tick_supervisor.log").read_text()
    assert handler.attempts == 0 and runs(pb) == 0


def test_a_pending_path_that_is_a_directory_stops_without_spinning(tmp_path, inbox):
    handler, api = inbox
    pb = tree(tmp_path)
    (pb / "state" / "tick_supervisor.pending").mkdir()
    unusable(pb, supervise(pb, api, cycles=3, tries=1), handler)
    assert (pb / "state" / "tick_supervisor.pending").is_dir()


def test_a_pending_path_that_is_a_symlink_stops(tmp_path, inbox):
    handler, api = inbox
    pb = tree(tmp_path)
    target = tmp_path / "elsewhere.txt"
    target.write_text("WAKE reports/old.json :: pr#7:review\n")
    os.symlink(target, pb / "state" / "tick_supervisor.pending")
    unusable(pb, supervise(pb, api, cycles=3, tries=1), handler)
    assert (pb / "state" / "tick_supervisor.pending").is_symlink()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_an_unreadable_pending_file_stops_and_is_kept_as_it_is(tmp_path, inbox):
    handler, api = inbox
    pb = tree(tmp_path)
    pending = pb / "state" / "tick_supervisor.pending"
    pending.write_text("WAKE reports/old.json :: pr#7:review\n")
    pending.chmod(0)
    try:
        unusable(pb, supervise(pb, api, cycles=3, tries=1), handler)
    finally:
        pending.chmod(0o600)
    assert pending.read_text() == "WAKE reports/old.json :: pr#7:review\n"


def test_not_writer_with_the_api_down_keeps_the_notice_and_says_so(tmp_path, inbox):
    handler, api = inbox
    handler.fail = 100
    pb = tree(tmp_path, line=NOT_WRITER)
    r = supervise(pb, api, cycles=0, tries=1)
    assert r.returncode == 3, r.stdout + r.stderr
    kept = (pb / "state" / "tick_supervisor.pending").read_text().splitlines()
    assert len(kept) == 1 and kept[0].startswith(NOT_WRITER)
    stopped = (pb / "state" / "tick_supervisor.stopped").read_text().splitlines()
    assert len(stopped) == 1 and stopped[0].startswith("NOT_WRITER ")
    assert "the notice waits in" in stopped[0]
    assert "NOT_WRITER " in (pb / "state" / "tick_supervisor.log").read_text()
    assert "NOT_WRITER " in r.stdout
    assert not (pb / "state" / "tick_supervisor.pid").exists()


def test_not_writer_with_the_api_up_tells_the_shepherd_and_records_the_stop(tmp_path, inbox):
    handler, api = inbox
    pb = tree(tmp_path, line=NOT_WRITER)
    r = supervise(pb, api, cycles=0, tries=1)
    assert r.returncode == 3, r.stdout + r.stderr
    assert messages(handler)[0].startswith(NOT_WRITER)
    assert "the shepherd was told" in (pb / "state" / "tick_supervisor.stopped").read_text()
    assert not (pb / "state" / "tick_supervisor.pending").exists()
