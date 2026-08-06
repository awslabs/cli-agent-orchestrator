"""Unit tests for the Muse native launch argv + identity binding.

Muse Code 0.1.0 binds a caller-provided --session-id only on the headless
``muse exec`` surface; the interactive TUI rejects the flag (verified). So the
native identity is chosen (an input), like Claude's, and the argv is
``muse exec --session-id <uuid> ...``.
"""

import pytest

from cli_agent_orchestrator.services import muse_native_launch as mnl
from cli_agent_orchestrator.services import native_tui_launch


def _uuid() -> str:
    return mnl.mint_session_id()


class TestBuildLaunchArgv:
    def test_exec_argv_binds_session_id(self):
        sid = _uuid()
        argv = mnl.build_launch_argv(session_id=sid, model="muse-spark-1.2")
        assert argv[:5] == ["muse", "exec", "--session-id", sid, "--yolo"]
        assert "--model" in argv and argv[argv.index("--model") + 1] == "muse-spark-1.2"

    def test_initial_prompt_appended(self):
        sid = _uuid()
        argv = mnl.build_launch_argv(session_id=sid, initial_prompt="implement X")
        assert argv[-1] == "implement X"

    def test_rejects_non_canonical_session_id(self):
        with pytest.raises(mnl.MuseNativeLaunchError):
            mnl.build_launch_argv(session_id="NOT-A-UUID")

    def test_rejects_forbidden_option_in_extra(self):
        with pytest.raises(mnl.MuseNativeLaunchError):
            mnl.build_launch_argv(session_id=_uuid(), extra_args=["--resume", "x"])


class TestBindsExactly:
    def test_binds_exact_minted_id(self):
        sid = _uuid()
        argv = mnl.build_launch_argv(session_id=sid)
        assert mnl.binds_exactly(argv, sid)

    def test_does_not_bind_other_id(self):
        sid = _uuid()
        other = _uuid()
        argv = mnl.build_launch_argv(session_id=sid)
        assert not mnl.binds_exactly(argv, other)

    def test_no_identity_option_rejected(self):
        assert not mnl.binds_exactly(["muse", "exec", "hi"], _uuid())


class TestTuiBinderRegistration:
    def test_muse_registered_in_binders(self):
        assert "muse_cli" in native_tui_launch.SUPPORTED_NATIVE_PROVIDERS

    def test_binder_builds_and_verifies(self):
        sid = _uuid()
        binder = native_tui_launch._ARGV_BINDERS["muse_cli"]
        argv = binder["build"](session_id=sid, binary="muse", extra_args=None,
                               launch_kind=native_tui_launch.LAUNCH_KIND_NEW)
        assert binder["binds_exactly"](argv, sid)
