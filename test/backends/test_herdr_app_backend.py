import hashlib
from test.backends.fake_hc import FakeHc, layout_with

import pytest

from cli_agent_orchestrator.backends.factory import ConfigurationError
from cli_agent_orchestrator.backends.herdr_app_backend import (
    HerdrAppBackend,
    cao_home_label,
    space_id_for,
)


@pytest.fixture
def hc():
    return FakeHc()


@pytest.fixture
def backend(hc, monkeypatch):
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.mcp_resolution.cao_server_env",
        lambda: {"CAO_API": "http://x"},
    )
    return HerdrAppBackend(herdr_app_session="cao", client=hc)


def test_space_id_is_deterministic():
    expected = "sp" + hashlib.sha256(b"cao:cao-a").hexdigest()[:32]
    assert space_id_for("cao-a") == expected
    assert space_id_for("cao-a") == space_id_for("cao-a")
    assert space_id_for("cao-b") != expected


def test_incompatible_core_is_a_configuration_error():
    with pytest.raises(ConfigurationError, match="generation 2"):
        HerdrAppBackend(client=FakeHc(generation=2))


def test_create_session_and_window_share_a_space(backend, hc, tmp_path):
    assert backend.create_session("cao-a", "sup", "aaaa0001", str(tmp_path)) == "sup"
    assert (
        backend.create_window("cao-a", "w1", "aaaa0002", str(tmp_path), extra_env={"FOO": "1"})
        == "w1"
    )
    runs = hc.calls_to("terminal.run")
    assert [r["name"] for r in runs] == ["cao-aaaa0001", "cao-aaaa0002"]
    for run, tid, window in zip(runs, ("aaaa0001", "aaaa0002"), ("sup", "w1")):
        assert run["no_parent"] is True and run["no_notify"] is True
        assert run["cwd"] == str(tmp_path)
        assert run["env"]["CAO_TERMINAL_ID"] == tid
        assert run["env"]["CAO_SESSION_NAME"] == "cao-a"
        labels = run["labels"]
        assert labels["herdr.space"] == space_id_for("cao-a")
        assert labels["herdr.tab"].startswith("tb") and len(labels["herdr.tab"]) == 34
        assert labels["cao.managed"] == "1"
        assert labels["cao.home"] == HOME
        assert (labels["cao.terminal_id"], labels["cao.session"], labels["cao.window"]) == (
            tid,
            "cao-a",
            window,
        )
    assert runs[1]["env"]["FOO"] == "1"
    assert runs[0]["labels"]["herdr.tab"] != runs[1]["labels"]["herdr.tab"]


def test_window_shell_is_passed_as_line(backend, hc, tmp_path):
    backend.create_session(
        "cao-a",
        "sup",
        "aaaa0001",
        str(tmp_path),
    )
    backend.create_window("cao-a", "w1", "aaaa0002", str(tmp_path), window_shell="kiro-cli chat")
    assert hc.calls_to("terminal.run")[1]["line"] == "kiro-cli chat"
    assert "line" not in hc.calls_to("terminal.run")[0]


def test_invalid_working_directory_raises(backend):
    with pytest.raises(ValueError):
        backend.create_session("cao-a", "sup", "aaaa0001", "/no/such/dir")


def test_names_written_when_app_adopted(backend, hc, tmp_path):
    sp = space_id_for("cao-a")
    hc.set_doc("herdr-app", layout_with(sp, [("tbX", "", "t1")]))
    backend.create_session("cao-a", "sup", "aaaa0001", str(tmp_path))
    doc = hc.docs["herdr-app"]["value"]
    assert doc["spaces"][0]["name"] == "cao-a"
    assert doc["spaces"][0]["tabs"][0]["name"] == "sup"
    # nothing else changed
    expected = layout_with(sp, [("tbX", "sup", "t1")], space_name="cao-a")
    assert doc == expected


def test_names_retry_on_conflict(backend, hc, tmp_path):
    hc.set_doc("herdr-app", layout_with(space_id_for("cao-a"), [("tbX", "", "t1")]))
    hc.conflicts_to_inject = 1
    backend.create_session("cao-a", "sup", "aaaa0001", str(tmp_path))
    assert hc.docs["herdr-app"]["value"]["spaces"][0]["name"] == "cao-a"
    assert len(hc.calls_to("doc.set")) == 2


def test_no_write_when_space_absent(backend, hc, tmp_path):
    backend.create_session("cao-a", "sup", "aaaa0001", str(tmp_path))
    assert hc.calls_to("doc.set") == []


def test_no_write_when_names_already_match(backend, hc, tmp_path):
    hc.set_doc("herdr-app", layout_with(space_id_for("cao-a"), [("tbX", "sup", "t1")], "cao-a"))
    backend.create_session("cao-a", "sup", "aaaa0001", str(tmp_path))
    assert hc.calls_to("doc.set") == []


def test_persistent_conflict_does_not_fail_creation(backend, hc, tmp_path, caplog):
    hc.set_doc("herdr-app", layout_with(space_id_for("cao-a"), [("tbX", "", "t1")]))
    hc.conflicts_to_inject = 99
    assert backend.create_session("cao-a", "sup", "aaaa0001", str(tmp_path)) == "sup"
    assert len(hc.calls_to("doc.set")) == 5
    assert "could not name" in caplog.text


from cli_agent_orchestrator.backends.base import (
    TerminalBackendError,
    TerminalCleanupOutcome,
    TerminalNotFoundError,
)
from cli_agent_orchestrator.constants import HC_READ_MAX_LINES
from cli_agent_orchestrator.models.terminal import TerminalStatus

HOME = cao_home_label()
FOREIGN_HOME = "f" * 16
# Variables an app terminal hands its programs; each points hc at the user's core.
CALLER_CORE_ENV = ("HC_TERMINAL_ID", "HC_TERMINAL_SOCKET", "HC_SCOPE", "HC_SOCKET")


def _labels(session, window, tid, home=HOME):
    return {
        "cao.managed": "1",
        "cao.home": home,
        "cao.session": session,
        "cao.window": window,
        "cao.terminal_id": tid,
        "herdr.space": space_id_for(session),
        "herdr.tab": "tb1",
    }


@pytest.fixture
def two_windows(hc):
    a = hc.add_terminal(_labels("cao-a", "sup", "aaaa0001"), name="cao-aaaa0001")
    b = hc.add_terminal(_labels("cao-a", "w1", "aaaa0002"), name="cao-aaaa0002")
    return a, b


class TestInput:
    def test_send_with_enter(self, backend, hc, two_windows):
        backend.send_keys("cao-a", "w1", "hello")
        assert hc.calls_to("terminal.send") == [{"terminal": "t2", "text": "hello", "force": True}]
        assert hc.calls_to("terminal.keys") == []

    def test_send_with_two_enters(self, backend, hc, two_windows):
        backend.send_keys("cao-a", "w1", "hello", enter_count=2)
        assert hc.calls_to("terminal.keys") == [{"terminal": "t2", "keys": ["enter"]}]

    def test_send_without_enter_presses_keys(self, backend, hc, two_windows):
        backend.send_keys("cao-a", "w1", "3", enter_count=0)
        assert hc.calls_to("terminal.keys") == [{"terminal": "t2", "keys": ["3"]}]
        assert hc.calls_to("terminal.send") == []

    def test_send_without_enter_rejects_long_text(self, backend, two_windows):
        with pytest.raises(TerminalBackendError):
            backend.send_keys("cao-a", "w1", "x" * 65, enter_count=0)

    @pytest.mark.parametrize(
        "key,expected",
        [
            ("Enter", "enter"),
            ("Escape", "esc"),
            ("C-c", "ctrl+c"),
            ("C-d", "ctrl+d"),
            ("C-u", "ctrl+u"),
            ("Down", "down"),
        ],
    )
    def test_special_keys(self, backend, hc, two_windows, key, expected):
        backend.send_special_key("cao-a", "w1", key)
        assert hc.calls_to("terminal.keys")[-1] == {"terminal": "t2", "keys": [expected]}

    def test_unknown_window(self, backend, two_windows):
        with pytest.raises(TerminalNotFoundError):
            backend.send_keys("cao-a", "nope", "x")


class TestOutput:
    def test_history_caps_lines(self, backend, hc, two_windows):
        two_windows[1]["text"] = "hi"
        assert backend.get_history("cao-a", "w1", tail_lines=200) == "hi"
        backend.get_history("cao-a", "w1", tail_lines=10**6)
        backend.get_history("cao-a", "w1", full_history=True)
        backend.get_history("cao-a", "w1", visible_only=True)
        lines = [c["lines"] for c in hc.calls_to("terminal.read")]
        assert lines == [200, HC_READ_MAX_LINES, HC_READ_MAX_LINES, 0]

    def test_cwd_and_command_from_foreground(self, backend, two_windows):
        two_windows[1]["foreground"] = {"name": "kiro-cli", "cwd": "/repo", "argv": ["kiro-cli"]}
        assert backend.get_pane_working_directory("cao-a", "w1") == "/repo"
        assert backend.get_pane_current_command("cao-a", "w1") == "kiro-cli"

    def test_cwd_falls_back_to_launch_cwd(self, backend, two_windows):
        two_windows[1]["foreground"] = None
        assert backend.get_pane_working_directory("cao-a", "w1") == "/tmp"

    def test_pipe_pane_is_a_noop(self, backend, hc, two_windows):
        before = len(hc.calls)
        backend.pipe_pane("cao-a", "w1", "/tmp/x")
        backend.stop_pipe_pane("cao-a", "w1")
        assert len(hc.calls) == before


class TestLifecycle:
    def test_session_exists_and_list(self, backend, two_windows):
        assert backend.session_exists("cao-a") is True
        assert backend.session_exists("cao-b") is False
        assert backend.list_sessions() == [{"id": "cao-a", "name": "cao-a", "status": "active"}]

    def test_strict_raises_on_failure(self, backend, hc):
        hc.fail_methods.add("terminal.list")
        assert backend.session_exists("cao-a") is False
        with pytest.raises(TerminalBackendError):
            backend.session_exists_strict("cao-a")

    def test_kill_window(self, backend, hc, two_windows):
        assert backend.kill_window("cao-a", "w1") is True
        assert "t2" not in hc.terminals
        assert backend.kill_window("cao-a", "w1") is False

    def test_kill_session_removes_space(self, backend, hc, two_windows):
        sp = space_id_for("cao-a")
        doc = layout_with(sp, [("tbA", "sup", "t1")], "cao-a")
        doc["spaces"].append({"id": "spOther", "name": "mine", "tabs": []})
        hc.set_doc("herdr-app", doc)
        assert backend.kill_session("cao-a") is True
        assert hc.terminals == {}
        assert [s["id"] for s in hc.docs["herdr-app"]["value"]["spaces"]] == ["spOther"]

    def test_kill_missing_session(self, backend):
        assert backend.kill_session("cao-x") is False


class TestExactCleanup:
    def test_one_match_deleted(self, backend, hc, two_windows):
        r = backend.cleanup_terminal_exact("aaaa0002")
        assert r.outcome == TerminalCleanupOutcome.DELETED and "t2" not in hc.terminals

    def test_absent(self, backend, two_windows):
        assert backend.cleanup_terminal_exact("ffff0000").outcome == TerminalCleanupOutcome.ABSENT

    def test_ambiguous_is_unknown(self, backend, hc, two_windows):
        hc.add_terminal(_labels("cao-a", "w9", "aaaa0002"))
        assert backend.cleanup_terminal_exact("aaaa0002").outcome == TerminalCleanupOutcome.UNKNOWN
        assert len(hc.calls_to("terminal.kill")) == 0

    def test_prove_only(self, backend, hc, two_windows):
        r = backend.cleanup_terminal_exact("aaaa0002", close=False)
        assert r.outcome == TerminalCleanupOutcome.STILL_PRESENT and "t2" in hc.terminals

    def test_lookup_failure_is_unknown(self, backend, hc):
        hc.fail_methods.add("terminal.list")
        assert backend.cleanup_terminal_exact("aaaa0002").outcome == TerminalCleanupOutcome.UNKNOWN


class TestStatusAndAttach:
    @pytest.mark.parametrize(
        "state,expected",
        [
            ("working", TerminalStatus.PROCESSING),
            ("blocked", TerminalStatus.WAITING_USER_ANSWER),
            ("done", TerminalStatus.COMPLETED),
            ("idle", TerminalStatus.IDLE),
            # herdr-core keeps `error` after a failed turn while the agent is
            # back at its prompt; ERROR would refuse all input, so the
            # provider's screen analysis decides instead.
            ("error", None),
            ("thinking", None),
        ],
    )
    def test_native_status(self, backend, two_windows, state, expected):
        two_windows[1]["status"] = {"app": "kiro-cli", "state": state}
        assert backend.get_native_status("cao-a", "w1") == expected

    def test_no_report_is_none(self, backend, two_windows):
        assert backend.get_native_status("cao-a", "w1") is None

    def test_event_inbox_and_pane_id(self, backend, two_windows):
        assert backend.supports_event_inbox() is True
        assert backend.get_pane_id("aaaa0002", "cao-a", "w1") == "t2"

    def test_web_attach_argv(self, backend, two_windows, monkeypatch):
        monkeypatch.setattr(
            "cli_agent_orchestrator.backends.herdr_app_backend.hc_binary", lambda: "/opt/bin/hc"
        )
        assert backend.prepare_web_attach("cao-a", "w1") == [
            "env",
            "-u",
            "HC_TERMINAL_ID",
            "-u",
            "HC_TERMINAL_SOCKET",
            "-u",
            "HC_SCOPE",
            "-u",
            "HC_SOCKET",
            "HC_SESSION=cao",
            "/opt/bin/hc",
            "attach",
            "t2",
        ]

    def test_web_attach_child_does_not_see_the_callers_core(
        self, backend, two_windows, monkeypatch, tmp_path
    ):
        # The browser PTY inherits cao-server's environment (api/main.py
        # _build_pty_env); run the argv with a stand-in hc that prints its env.
        import subprocess

        fake = tmp_path / "hc"
        fake.write_text("#!/bin/sh\nenv\n")
        fake.chmod(0o755)
        monkeypatch.setattr(
            "cli_agent_orchestrator.backends.herdr_app_backend.hc_binary", lambda: str(fake)
        )
        argv = backend.prepare_web_attach("cao-a", "w1")
        inherited = {
            "PATH": "/usr/bin:/bin",
            "HC_SESSION": "default",
            **{k: "/run/user/default.sock" for k in CALLER_CORE_ENV},
        }
        out = subprocess.run(argv, env=inherited, capture_output=True, text=True, check=True)
        seen = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
        assert seen["HC_SESSION"] == "cao"
        assert not set(CALLER_CORE_ENV) & set(seen)

    def test_attach_session_execs_hc(self, backend, two_windows, monkeypatch):
        for key in CALLER_CORE_ENV:
            monkeypatch.setenv(key, "/run/user/default.sock")
        monkeypatch.setattr(
            "cli_agent_orchestrator.backends.herdr_app_backend.hc_binary", lambda: "/opt/bin/hc"
        )
        seen = {}
        monkeypatch.setattr(
            "os.execvpe", lambda f, argv, env: seen.update(file=f, argv=argv, env=env)
        )
        backend.attach_session("cao-a")
        assert seen["file"] == "/opt/bin/hc"
        assert seen["argv"] == ["/opt/bin/hc", "attach", "t1"]
        assert seen["env"]["HC_SESSION"] == "cao"
        assert not set(CALLER_CORE_ENV) & set(seen["env"])


class TestInputDeviations:
    """Behavior measured live on hc 0.1.21 that the plan did not cover."""

    def test_space_is_named_space(self, backend, hc, two_windows):
        # hc rejects " " as a key name; it calls it "space".
        backend.send_keys("cao-a", "w1", "a b", enter_count=0)
        assert hc.calls_to("terminal.keys") == [{"terminal": "t2", "keys": ["a", "space", "b"]}]

    def test_empty_text_without_enter_sends_nothing(self, backend, hc, two_windows):
        backend.send_keys("cao-a", "w1", "", enter_count=0)
        assert hc.calls_to("terminal.keys") == []

    def test_input_calls_allow_a_draft_wait(self, backend, hc, two_windows, monkeypatch):
        # terminal.send / terminal.keys wait for a person's draft (up to ~60 s),
        # so they must not use the 10 s default request timeout.
        timeouts = {}
        real_call = hc.call

        def spy(method, params=None, timeout=10.0):
            timeouts[method] = timeout
            return real_call(method, params, timeout)

        monkeypatch.setattr(hc, "call", spy)
        backend.send_keys("cao-a", "w1", "hello", enter_count=2)
        backend.send_special_key("cao-a", "w1", "Escape")
        assert timeouts["terminal.send"] > 60 and timeouts["terminal.keys"] > 60
        assert timeouts["terminal.list"] == 10.0


def test_server_env_drops_blocked_keys(monkeypatch):
    """CAO-started cores do not hand provider-session vars to agent terminals."""
    from cli_agent_orchestrator.backends import herdr_app_backend as mod

    seen = {}

    class _Client:
        def __init__(self, session):
            pass

        def start_server(self, drop_env=None):
            seen["drop"] = drop_env

        def hello(self):
            return {}

    monkeypatch.setattr(mod, "HcRpcClient", _Client)
    HerdrAppBackend("cao")
    assert seen["drop"]("CLAUDECODE") is True
    assert seen["drop"]("CODEX_HOME") is True
    assert seen["drop"]("CLAUDE_CODE_USE_BEDROCK") is False
    # The core's own environment is the operator's: PATH etc. stay.
    assert seen["drop"]("PATH") is False
    assert seen["drop"]("AWS_PROFILE") is False


class TestInputConnection:
    """terminal.send / terminal.keys run on their own connection."""

    def test_slow_send_does_not_block_status_lookup(self, hc, monkeypatch):
        import threading

        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.mcp_resolution.cao_server_env", lambda: {}
        )
        release = threading.Event()
        sending = threading.Event()

        class LockingFakeHc(FakeHc):
            """Serializes calls like HcRpcClient; terminal.send waits for a draft."""

            def __init__(self):
                super().__init__()
                self._lock = threading.Lock()

            def call(self, method, params=None, timeout=10.0):
                with self._lock:
                    if method == "terminal.send":
                        sending.set()
                        assert release.wait(5), "test never released the send"
                    return super().call(method, params, timeout)

        main, inp = LockingFakeHc(), LockingFakeHc()
        inp.terminals = main.terminals  # one core, two connections
        rec = main.add_terminal(_labels("cao-a", "w1", "aaaa0002"))
        rec["status"] = {"app": "kiro-cli", "state": "working"}
        backend = HerdrAppBackend("cao", client=main, input_client=inp)

        sender = threading.Thread(target=backend.send_keys, args=("cao-a", "w1", "hi"))
        sender.start()
        try:
            assert sending.wait(5)
            result = {}
            looker = threading.Thread(
                target=lambda: result.update(s=backend.get_native_status("cao-a", "w1"))
            )
            looker.start()
            looker.join(2)
            assert not looker.is_alive(), "status lookup waited behind terminal.send"
            assert result["s"] == TerminalStatus.PROCESSING
        finally:
            release.set()
            sender.join(5)
        assert inp.calls_to("terminal.send") and not main.calls_to("terminal.send")

    def test_keys_use_input_connection(self, backend, hc, two_windows):
        other = FakeHc()
        other.terminals = hc.terminals
        backend._input_client = other
        backend.send_keys("cao-a", "w1", "3", enter_count=0)
        backend.send_special_key("cao-a", "w1", "Enter")
        backend.send_keys("cao-a", "w1", "x", enter_count=2)
        assert hc.calls_to("terminal.keys") == [] and hc.calls_to("terminal.send") == []
        assert len(other.calls_to("terminal.keys")) == 3
        assert len(other.calls_to("terminal.send")) == 1

    def test_injected_client_doubles_as_input_client(self, backend, hc):
        assert backend._input() is hc

    def test_default_input_client_is_a_second_lazy_connection(self, monkeypatch):
        from cli_agent_orchestrator.backends import herdr_app_backend as mod

        made = []

        class _Client:
            def __init__(self, session):
                self.session = session
                made.append(self)

            def start_server(self, drop_env=None):
                pass

            def hello(self):
                return {}

        monkeypatch.setattr(mod, "HcRpcClient", _Client)
        backend = HerdrAppBackend("cao-x")
        assert len(made) == 1
        first = backend._input()
        assert len(made) == 2 and first is not made[0] and first.session == "cao-x"
        assert backend._input() is first and len(made) == 2


class TestServerIdentity:
    """Two cao-servers with different CAO homes can share one core."""

    def test_home_label_hashes_the_resolved_home(self, monkeypatch, tmp_path):
        from cli_agent_orchestrator import constants

        monkeypatch.setattr(constants, "CAO_HOME_DIR", tmp_path / "x" / ".." / "home")
        expected = hashlib.sha256(str((tmp_path / "home").resolve()).encode()).hexdigest()[:16]
        assert cao_home_label() == expected and len(expected) == 16

    @pytest.fixture
    def mixed(self, hc, two_windows):
        foreign = hc.add_terminal(_labels("cao-a", "w1", "aaaa0002", FOREIGN_HOME))
        other = hc.add_terminal(_labels("cao-z", "sup", "zzzz0001", FOREIGN_HOME))
        return foreign, other

    def test_foreign_terminals_are_invisible(self, backend, mixed):
        assert backend.session_exists("cao-z") is False
        assert backend.list_sessions() == [{"id": "cao-a", "name": "cao-a", "status": "active"}]
        assert {t["address"] for t in backend.list_managed()} == {"t1", "t2"}
        assert backend.get_pane_id("aaaa0002") == "t2"
        assert backend.cleanup_terminal_exact("zzzz0001").outcome == TerminalCleanupOutcome.ABSENT

    def test_list_managed_can_include_finished_records(self, backend, hc, mixed):
        hc.terminals["t2"]["state"] = "finished"
        assert {t["address"] for t in backend.list_managed()} == {"t1"}
        found = backend.list_managed(include_finished=True)
        assert {t["address"] for t in found} == {"t1", "t2"}
        assert hc.calls_to("terminal.list")[-1]["all"] is True
        assert "all" not in hc.calls_to("terminal.list")[-2]

    def test_exact_cleanup_is_not_ambiguous_across_homes(self, backend, hc, mixed):
        r = backend.cleanup_terminal_exact("aaaa0002")
        assert r.outcome == TerminalCleanupOutcome.DELETED
        assert "t2" not in hc.terminals and mixed[0]["address"] in hc.terminals

    def test_kill_session_spares_foreign_terminals_and_shared_space(self, backend, hc, mixed):
        sp = space_id_for("cao-a")
        hc.set_doc("herdr-app", layout_with(sp, [("tbA", "sup", "t1")], "cao-a"))
        assert backend.kill_session("cao-a") is True
        assert set(hc.terminals) == {mixed[0]["address"], mixed[1]["address"]}
        # The other server still has a terminal in that space: keep it.
        assert [s["id"] for s in hc.docs["herdr-app"]["value"]["spaces"]] == [sp]
        assert backend.kill_session("cao-z") is False

    def test_kill_window_and_input_ignore_foreign(self, backend, hc, mixed):
        backend.send_keys("cao-a", "w1", "hi")
        assert hc.calls_to("terminal.send")[0]["terminal"] == "t2"
        assert backend.kill_window("cao-z", "sup") is False

    def test_naming_ignores_foreign_terminals(self, backend, hc, tmp_path):
        sp = space_id_for("cao-q")
        foreign = hc.add_terminal(_labels("cao-q", "theirs", "qqqq0001", FOREIGN_HOME))
        hc.set_doc("herdr-app", layout_with(sp, [("tbF", "", foreign["address"])]))
        backend.sync_layout_names()
        assert hc.calls_to("doc.set") == []
