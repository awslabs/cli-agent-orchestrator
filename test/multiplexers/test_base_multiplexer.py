"""Contract tests for BaseMultiplexer and LaunchSpec."""

from __future__ import annotations

import os
from typing import Optional
from unittest.mock import call, patch

import pytest

from cli_agent_orchestrator.multiplexers import BaseMultiplexer, LaunchSpec
from cli_agent_orchestrator.multiplexers.base import BaseMultiplexer as BaseMultiplexerDirect


# ---------------------------------------------------------------------------
# Fake concrete subclass — records calls, does nothing real
# ---------------------------------------------------------------------------


class FakeMultiplexer(BaseMultiplexer):
    """Minimal concrete implementation used to exercise BaseMultiplexer contracts."""

    def __init__(self) -> None:
        self._calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def _record(self, name: str, *args: object, **kwargs: object) -> None:
        self._calls.append((name, args, kwargs))

    def create_session(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        launch_spec: Optional[LaunchSpec] = None,
    ) -> str:
        self._record("create_session", session_name, window_name, terminal_id, working_directory, launch_spec)
        return window_name

    def create_window(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        launch_spec: Optional[LaunchSpec] = None,
    ) -> str:
        self._record("create_window", session_name, window_name, terminal_id, working_directory, launch_spec)
        return window_name

    def _paste_text(self, session_name: str, window_name: str, text: str) -> None:
        self._record("_paste_text", session_name, window_name, text)

    def _submit_input(
        self, session_name: str, window_name: str, enter_count: int = 1
    ) -> None:
        self._record("_submit_input", session_name, window_name, enter_count=enter_count)

    def send_special_key(
        self,
        session_name: str,
        window_name: str,
        key: str,
        *,
        literal: bool = False,
    ) -> None:
        self._record("send_special_key", session_name, window_name, key, literal=literal)

    def get_history(
        self, session_name: str, window_name: str, tail_lines: Optional[int] = None
    ) -> str:
        self._record("get_history", session_name, window_name, tail_lines)
        return ""

    def list_sessions(self) -> list[dict[str, str]]:
        self._record("list_sessions")
        return []

    def kill_session(self, session_name: str) -> bool:
        self._record("kill_session", session_name)
        return True

    def kill_window(self, session_name: str, window_name: str) -> bool:
        self._record("kill_window", session_name, window_name)
        return True

    def session_exists(self, session_name: str) -> bool:
        self._record("session_exists", session_name)
        return False

    def get_pane_working_directory(
        self, session_name: str, window_name: str
    ) -> Optional[str]:
        self._record("get_pane_working_directory", session_name, window_name)
        return None

    def pipe_pane(self, session_name: str, window_name: str, file_path: str) -> None:
        self._record("pipe_pane", session_name, window_name, file_path)

    def stop_pipe_pane(self, session_name: str, window_name: str) -> None:
        self._record("stop_pipe_pane", session_name, window_name)


# ---------------------------------------------------------------------------
# LaunchSpec tests
# ---------------------------------------------------------------------------


class TestLaunchSpec:
    def test_default_fields_are_none(self) -> None:
        spec = LaunchSpec()
        assert spec.argv is None
        assert spec.env is None
        assert spec.provider is None

    def test_fields_set_on_construction(self) -> None:
        spec = LaunchSpec(argv=["codex", "--yolo"], env={"FOO": "bar"}, provider="codex")
        assert spec.argv == ["codex", "--yolo"]
        assert spec.env == {"FOO": "bar"}
        assert spec.provider == "codex"

    def test_frozen_mutation_raises(self) -> None:
        spec = LaunchSpec(argv=["codex"])
        with pytest.raises((AttributeError, TypeError)):
            spec.argv = ["other"]  # type: ignore[misc]

    def test_equality_same_values(self) -> None:
        a = LaunchSpec(argv=["cmd"], provider="claude")
        b = LaunchSpec(argv=["cmd"], provider="claude")
        assert a == b

    def test_equality_different_values(self) -> None:
        a = LaunchSpec(argv=["cmd"])
        b = LaunchSpec(argv=["other"])
        assert a != b

    def test_hashable(self) -> None:
        spec = LaunchSpec(provider="codex")
        _ = {spec}  # must not raise


# ---------------------------------------------------------------------------
# Cannot instantiate abstract base directly
# ---------------------------------------------------------------------------


class TestBaseMultiplexerAbstract:
    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            BaseMultiplexer()  # type: ignore[abstract]

    def test_import_from_package_equals_direct_import(self) -> None:
        assert BaseMultiplexer is BaseMultiplexerDirect


# ---------------------------------------------------------------------------
# send_keys default implementation
# ---------------------------------------------------------------------------


class TestSendKeys:
    def test_send_keys_calls_paste_then_submit(self) -> None:
        mux = FakeMultiplexer()
        mux.send_keys("ses", "win", "hello")

        names = [c[0] for c in mux._calls]
        assert names == ["_paste_text", "_submit_input"]

    def test_send_keys_passes_text_to_paste(self) -> None:
        mux = FakeMultiplexer()
        mux.send_keys("ses", "win", "my text")

        paste_call = mux._calls[0]
        assert paste_call[0] == "_paste_text"
        assert paste_call[1] == ("ses", "win", "my text")

    def test_send_keys_passes_session_window_to_submit(self) -> None:
        mux = FakeMultiplexer()
        mux.send_keys("ses", "win", "t")

        submit_call = mux._calls[1]
        assert submit_call[0] == "_submit_input"
        assert submit_call[1][0] == "ses"
        assert submit_call[1][1] == "win"

    def test_send_keys_default_enter_count_is_1(self) -> None:
        mux = FakeMultiplexer()
        mux.send_keys("ses", "win", "t")

        submit_call = mux._calls[1]
        assert submit_call[2].get("enter_count") == 1

    def test_send_keys_forwards_enter_count(self) -> None:
        mux = FakeMultiplexer()
        mux.send_keys("ses", "win", "t", enter_count=3)

        submit_call = mux._calls[1]
        assert submit_call[2].get("enter_count") == 3

    def test_send_keys_paste_before_submit_ordering(self) -> None:
        mux = FakeMultiplexer()
        mux.send_keys("ses", "win", "t")

        assert mux._calls[0][0] == "_paste_text"
        assert mux._calls[1][0] == "_submit_input"
        assert len(mux._calls) == 2


# ---------------------------------------------------------------------------
# send_special_key signature — literal kwarg must exist
# ---------------------------------------------------------------------------


class TestSendSpecialKey:
    def test_send_special_key_literal_default_false(self) -> None:
        mux = FakeMultiplexer()
        mux.send_special_key("ses", "win", "Enter")

        call_rec = mux._calls[0]
        assert call_rec[0] == "send_special_key"
        assert call_rec[2].get("literal") is False

    def test_send_special_key_literal_true(self) -> None:
        mux = FakeMultiplexer()
        mux.send_special_key("ses", "win", "\x1b[B", literal=True)

        call_rec = mux._calls[0]
        assert call_rec[2].get("literal") is True


# ---------------------------------------------------------------------------
# _resolve_and_validate_working_directory parity with TmuxClient
# ---------------------------------------------------------------------------


class TestResolveAndValidateWorkingDirectory:
    def test_defaults_to_cwd(self) -> None:
        mux = FakeMultiplexer()
        with patch("os.getcwd", return_value="/home/user/project"):
            with patch("os.path.realpath", return_value="/home/user/project"):
                with patch("os.path.isdir", return_value=True):
                    result = mux._resolve_and_validate_working_directory(None)
        assert result == "/home/user/project"

    def test_valid_directory(self, tmp_path: object) -> None:
        mux = FakeMultiplexer()
        with patch("os.path.isdir", return_value=True):
            with patch("os.path.realpath", return_value="/home/user/project"):
                result = mux._resolve_and_validate_working_directory("/home/user/project")
        assert result == "/home/user/project"

    def test_blocked_root(self) -> None:
        mux = FakeMultiplexer()
        with patch("os.path.realpath", return_value="/"):
            with pytest.raises(ValueError, match="blocked system path"):
                mux._resolve_and_validate_working_directory("/")

    def test_blocked_etc(self) -> None:
        mux = FakeMultiplexer()
        with patch("os.path.realpath", return_value="/etc"):
            with pytest.raises(ValueError, match="blocked system path"):
                mux._resolve_and_validate_working_directory("/etc")

    def test_blocked_var(self) -> None:
        mux = FakeMultiplexer()
        with patch("os.path.realpath", return_value="/var"):
            with pytest.raises(ValueError, match="blocked system path"):
                mux._resolve_and_validate_working_directory("/var")

    def test_blocked_root_dir(self) -> None:
        mux = FakeMultiplexer()
        with patch("os.path.realpath", return_value="/"):
            with pytest.raises(ValueError, match="blocked system path"):
                mux._resolve_and_validate_working_directory("/")

    def test_nonexistent_directory(self) -> None:
        mux = FakeMultiplexer()
        with patch("os.path.realpath", return_value="/home/user/nonexistent_dir_xyz"):
            with patch("os.path.isdir", return_value=False):
                with pytest.raises(ValueError, match="does not exist"):
                    mux._resolve_and_validate_working_directory("/home/user/nonexistent_dir_xyz")

    def test_resolves_symlinks(self) -> None:
        mux = FakeMultiplexer()
        with patch("os.path.realpath", return_value="/home/user/real"):
            with patch("os.path.isdir", return_value=True):
                result = mux._resolve_and_validate_working_directory("/home/user/link")
        assert result == "/home/user/real"

    def test_expands_tilde(self) -> None:
        mux = FakeMultiplexer()
        with patch("os.path.expanduser", return_value="/home/user/project"):
            with patch("os.path.realpath", return_value="/home/user/project"):
                with patch("os.path.isdir", return_value=True):
                    result = mux._resolve_and_validate_working_directory("~/project")
        assert result == "/home/user/project"

    def test_allows_path_outside_home(self) -> None:
        mux = FakeMultiplexer()
        with patch("os.path.isdir", return_value=True):
            with patch("os.path.realpath", return_value="/Volumes/workplace/project"):
                result = mux._resolve_and_validate_working_directory(
                    "/Volumes/workplace/project"
                )
        assert result == "/Volumes/workplace/project"

    def test_allows_subdirectory_of_blocked_path(self) -> None:
        mux = FakeMultiplexer()
        with patch("os.path.isdir", return_value=True):
            with patch("os.path.realpath", return_value="/var/folders/abc/project"):
                result = mux._resolve_and_validate_working_directory(
                    "/var/folders/abc/project"
                )
        assert result == "/var/folders/abc/project"

    def test_raises_for_symlink_resolving_to_blocked(self) -> None:
        mux = FakeMultiplexer()
        with patch("os.path.realpath", return_value="/var"):
            with pytest.raises(ValueError, match="blocked system path"):
                mux._resolve_and_validate_working_directory("/some/link")

    def test_home_directory_itself_allowed(self) -> None:
        mux = FakeMultiplexer()
        with patch("os.path.isdir", return_value=True):
            with patch("os.path.realpath", return_value="/home/user"):
                result = mux._resolve_and_validate_working_directory("/home/user")
        assert result == "/home/user"

    def test_allows_opt_subdirectory(self) -> None:
        mux = FakeMultiplexer()
        with patch("os.path.isdir", return_value=True):
            with patch("os.path.realpath", return_value="/opt/projects/my-app"):
                result = mux._resolve_and_validate_working_directory(
                    "/opt/projects/my-app"
                )
        assert result == "/opt/projects/my-app"

    def test_raises_for_blocked_boot(self) -> None:
        mux = FakeMultiplexer()
        with patch("os.path.realpath", return_value="/boot"):
            with pytest.raises(ValueError, match="blocked system path"):
                mux._resolve_and_validate_working_directory("/boot")

    def test_symlinked_home_real_path(self) -> None:
        mux = FakeMultiplexer()
        with patch("os.path.realpath", return_value="/local/home/user/project"):
            with patch("os.path.isdir", return_value=True):
                result = mux._resolve_and_validate_working_directory("/home/user/project")
        assert result == "/local/home/user/project"

    def test_error_message_contains_original_path(self) -> None:
        mux = FakeMultiplexer()
        with patch("os.path.realpath", return_value="/home/user/does_not_exist_xyz"):
            with patch("os.path.isdir", return_value=False):
                with pytest.raises(ValueError) as exc_info:
                    mux._resolve_and_validate_working_directory("/home/user/does_not_exist_xyz")
        assert "does_not_exist_xyz" in str(exc_info.value) or "does not exist" in str(exc_info.value)
