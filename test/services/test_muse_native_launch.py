"""Unit tests for the Muse native launch argv + identity binding.

Muse Code 0.1.0's accepted interactive lifecycle resumes a caller-chosen
session id through the TUI: ``muse resume <id>`` (verified on the installed
0.1.0-R708.1 build).  The native identity is chosen (an input), like
Claude's, and the argv is ``muse resume <id>`` — never the superseded
one-shot ``muse exec --session-id`` form.
"""

import pytest

from cli_agent_orchestrator.services import muse_native_launch as mnl
from cli_agent_orchestrator.services import native_tui_launch, provider_contracts


def _uuid() -> str:
    return mnl.mint_session_id()


class TestBuildResumeArgv:
    def test_resume_argv_binds_session_id(self):
        sid = _uuid()
        argv = mnl.build_resume_argv(session_id=sid)
        assert argv == ["muse", "resume", sid]

    def test_profile_args_follow_identity_pair(self):
        sid = _uuid()
        argv = mnl.build_resume_argv(session_id=sid, extra_args=["--model", "muse-spark-1.2"])
        assert argv[:3] == ["muse", "resume", sid]
        assert argv[3:] == ["--model", "muse-spark-1.2"]

    def test_rejects_non_canonical_session_id(self):
        with pytest.raises(mnl.MuseNativeLaunchError):
            mnl.build_resume_argv(session_id="NOT-A-UUID")

    def test_rejects_identity_rebinding_extra(self):
        # The superseded --session-id and newest-session shortcuts are
        # forbidden in a managed resume.
        for bad in ("--session-id", "--last", "--continue"):
            with pytest.raises(mnl.MuseNativeLaunchError):
                mnl.build_resume_argv(session_id=_uuid(), extra_args=[bad, "x"])

    def test_rejects_disabling_retained_session_log(self):
        with pytest.raises(mnl.MuseNativeLaunchError, match="exact session resumability"):
            mnl.build_resume_argv(session_id=_uuid(), extra_args=["--no-session-log"])

    def test_accepts_provider_contract_resume_form(self):
        sid = _uuid()
        form = provider_contracts.validate_resume_argv(
            provider_contracts.PROVIDER_MUSE, ["resume", sid]
        )
        assert form.provider == provider_contracts.PROVIDER_MUSE
        assert form.native_id == sid

    def test_rejects_malformed_resume_form(self):
        with pytest.raises(provider_contracts.ResumeFormRefused):
            provider_contracts.validate_resume_argv(
                provider_contracts.PROVIDER_MUSE, ["resume", "NOT-A-UUID"]
            )


class TestResumesExactly:
    def test_resumes_exact_minted_id(self):
        sid = _uuid()
        argv = mnl.build_resume_argv(session_id=sid)
        assert mnl.resumes_exactly(argv, sid)

    def test_does_not_resume_other_id(self):
        sid = _uuid()
        other = _uuid()
        argv = mnl.build_resume_argv(session_id=sid)
        assert not mnl.resumes_exactly(argv, other)

    def test_no_identity_subcommand_rejected(self):
        assert not mnl.resumes_exactly(["muse", "chat"], _uuid())

    def test_superseded_exec_form_rejected(self):
        # The one-shot `muse exec --session-id` form must never bind as a
        # resume of the interactive lifecycle.
        assert not mnl.resumes_exactly(["muse", "exec", "--session-id", _uuid()], _uuid())


class TestTuiBinderRegistration:
    def test_muse_registered_in_binders(self):
        assert "muse_cli" in native_tui_launch.SUPPORTED_NATIVE_PROVIDERS

    def test_binder_resumes_and_verifies(self):
        sid = _uuid()
        binder = native_tui_launch._ARGV_BINDERS["muse_cli"]
        argv = binder["build"](
            session_id=sid,
            binary="muse",
            extra_args=None,
            launch_kind=native_tui_launch.LAUNCH_KIND_RESUME,
        )
        assert binder["binds_exactly"](argv, sid)

    def test_binder_refuses_new_launch_kind(self):
        # Muse has no interactive NEW launch kind: the id is minted first
        # and the TUI always resumes it.
        sid = _uuid()
        binder = native_tui_launch._ARGV_BINDERS["muse_cli"]
        with pytest.raises(native_tui_launch.NativeLaunchInvalid, match="resume the exact id"):
            binder["build"](
                session_id=sid,
                binary="muse",
                extra_args=None,
                launch_kind=native_tui_launch.LAUNCH_KIND_NEW,
            )
