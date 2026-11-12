"""Provider launch preparation happens on the execution host, not cao-server's (#745).

Two things Claude Code's launch used to do on the server's host: merge
``skipDangerousModePermissionPrompt`` into ``~/.claude/settings.json``, and read
the server's own uid to decide ``--dangerously-skip-permissions``. Under the
remote backend both are asked of the terminal's runner. Locally nothing changes.
"""

import json
import os
import stat
from test.runner.test_remote_backend import (  # noqa: F401  (fixtures)
    backend,
    hub,
    server_tmp,
    start_runner,
)

import pytest

from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from cli_agent_orchestrator.runner.protocol import RunnerReportedError
from cli_agent_orchestrator.utils import user_settings
from cli_agent_orchestrator.utils.user_settings import UserSettingsUnreadable, merge_user_json


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path / "home"


class TestMerge:
    def test_a_new_file_is_created_private(self, home):
        assert merge_user_json("claude_settings", {"skipDangerousModePermissionPrompt": True})
        path = home / ".claude" / "settings.json"
        assert json.loads(path.read_text()) == {"skipDangerousModePermissionPrompt": True}
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_existing_settings_and_mode_are_kept(self, home):
        path = home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"env": {"X": "1"}, "permissions": {"allow": ["Bash"]}}))
        os.chmod(path, 0o640)
        assert merge_user_json("claude_settings", {"skipDangerousModePermissionPrompt": True})
        assert json.loads(path.read_text()) == {
            "env": {"X": "1"},
            "permissions": {"allow": ["Bash"]},
            "skipDangerousModePermissionPrompt": True,
        }
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o640

    def test_nothing_is_written_when_already_set(self, home):
        merge_user_json("claude_settings", {"skipDangerousModePermissionPrompt": True})
        path = home / ".claude" / "settings.json"
        before = path.stat().st_mtime_ns
        assert (
            merge_user_json("claude_settings", {"skipDangerousModePermissionPrompt": True}) is False
        )
        assert path.stat().st_mtime_ns == before

    @pytest.mark.parametrize("content", ["{not json", "[1, 2]"])
    def test_an_unreadable_file_is_left_untouched(self, home, content):
        path = home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(content)
        with pytest.raises(UserSettingsUnreadable):
            merge_user_json("claude_settings", {"skipDangerousModePermissionPrompt": True})
        assert path.read_text() == content  # the user's file, as it was
        assert list(path.parent.iterdir()) == [path]  # no temp file left

    def test_the_write_is_private_until_published(self, home, monkeypatch):
        """An explicit mode makes the atomic writer create its temp file 0600 (mkstemp)."""
        from cli_agent_orchestrator.utils import atomic_file

        seen = []
        real = atomic_file.locked_atomic_rewrite

        def spy(target, compute, **kwargs):
            seen.append(kwargs.get("mode"))
            return real(target, compute, **kwargs)

        monkeypatch.setattr(atomic_file, "locked_atomic_rewrite", spy)
        merge_user_json("claude_settings", {"a": 1})  # new file
        os.chmod(home / ".claude" / "settings.json", 0o644)
        merge_user_json("claude_settings", {"b": 2})  # existing file
        assert seen == [0o600, 0o644]

    def test_only_allowlisted_files_by_id(self, home):
        with pytest.raises(ValueError):
            merge_user_json("../../etc/passwd", {"x": 1})
        with pytest.raises(ValueError):
            merge_user_json("claude_settings", ["not", "an", "object"])
        assert set(user_settings.USER_JSON_FILES) == {"claude_settings"}


class TestOnTheRunner:
    def test_the_merge_and_the_uid_are_answered_by_the_runner(
        self, hub, backend, start_runner, home  # noqa: F811
    ):
        start_runner("a")
        backend.create_session("s1", "w", "aaaaaaaa")
        assert backend.merge_user_json(
            "s1", "w", "claude_settings", {"skipDangerousModePermissionPrompt": True}
        )
        # The runner thread shares this process's HOME here; on a cluster it is
        # the runner pod's own.
        assert json.loads((home / ".claude" / "settings.json").read_text()) == {
            "skipDangerousModePermissionPrompt": True
        }
        assert backend.execution_user_is_root("s1", "w") is (os.geteuid() == 0)

    def test_an_unknown_file_id_is_refused_by_the_runner(
        self, hub, backend, start_runner, home  # noqa: F811
    ):
        start_runner("a")
        backend.create_session("s1", "w", "aaaaaaaa")
        with pytest.raises(ValueError):
            backend.merge_user_json("s1", "w", "../../.ssh/authorized_keys", {"x": 1})


class _Remote:
    resolves_paths_locally = False

    def __init__(self, root=False, merge_error=None):
        self.calls = []
        self._root = root
        self._merge_error = merge_error

    def merge_user_json(self, session, window, file_id, updates):
        self.calls.append(("merge", session, window, file_id, updates))
        if self._merge_error:
            raise self._merge_error
        return True

    def execution_user_is_root(self, session, window):
        self.calls.append(("root", session, window))
        return self._root


def _provider():
    return ClaudeCodeProvider("aaaaaaaa", "s1", "w", "developer")


class TestTheProvider:
    def test_under_the_remote_backend_the_server_host_is_never_touched(self, monkeypatch):
        remote = _Remote(root=True)
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.claude_code.get_backend", lambda: remote
        )
        monkeypatch.setattr(
            ClaudeCodeProvider,
            "_ensure_skip_bypass_prompt_setting",
            staticmethod(lambda: pytest.fail("wrote the server's ~/.claude/settings.json")),
        )
        monkeypatch.setattr(os, "geteuid", lambda: pytest.fail("read the server's uid"))
        provider = _provider()
        provider._ensure_skip_bypass_prompt_on_execution_host()
        assert provider._execution_user_is_root() is True
        assert remote.calls == [
            ("merge", "s1", "w", "claude_settings", {"skipDangerousModePermissionPrompt": True}),
            ("root", "s1", "w"),
        ]

    def test_a_runner_that_cannot_merge_leaves_the_prompt_to_the_handler(self, monkeypatch):
        remote = _Remote(merge_error=RunnerReportedError("runner a: settings.json not readable"))
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.claude_code.get_backend", lambda: remote
        )
        _provider()._ensure_skip_bypass_prompt_on_execution_host()  # logged, not raised

    def test_locally_nothing_changes(self, monkeypatch):
        class Local:
            resolves_paths_locally = True

        written = []
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.claude_code.get_backend", lambda: Local()
        )
        monkeypatch.setattr(
            ClaudeCodeProvider,
            "_ensure_skip_bypass_prompt_setting",
            staticmethod(lambda: written.append("local")),
        )
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        provider = _provider()
        provider._ensure_skip_bypass_prompt_on_execution_host()
        assert written == ["local"]
        assert provider._execution_user_is_root() is True

    def test_the_launch_command_follows_the_runners_uid(self, monkeypatch):
        """yolo as root omits --dangerously-skip-permissions; the runner says who is root."""
        remote = _Remote(root=True)
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.claude_code.get_backend", lambda: remote
        )
        monkeypatch.setattr(os, "geteuid", lambda: 1000)  # the server is not root
        provider = ClaudeCodeProvider("aaaaaaaa", "s1", "w", "developer", allowed_tools=["*"])
        command = provider._build_claude_command(profile=None)
        assert "--dangerously-skip-permissions" not in command
        remote._root = False
        command = provider._build_claude_command(profile=None)
        assert "--dangerously-skip-permissions" in command
