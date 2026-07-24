"""The native TUI launch path: claim, launch, prove, publish — or freeze.

The cases here are organised around the two ways a native launch
corrupts a provider session: starting a second TUI on a session another
controller already holds, and publishing an attachment for a process
that is not the one that was claimed.  Every "freeze" assertion below is
really an assertion that a *later* attempt cannot proceed.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Mapping, Optional, Sequence

import pytest

from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import native_attachment, native_tui_launch

PROVIDER = "kimi_cli"
SESSION = "sess-native-0001"
TERMINAL = "term-native-0001"
GENERATION = "gen-native-0001"


@pytest.fixture
def pinned_binary(tmp_path: Any) -> tuple[str, str]:
    """A real, executable, digest-known file standing in for the provider.

    The launcher verifies bytes on disk, so a fake path would exercise
    nothing.  Returned as ``(path, sha256)`` because every call site
    needs the pin as well as the path.
    """
    binary = tmp_path / "kimi"
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    path = os.path.realpath(str(binary))
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    return path, digest


def _intent() -> dict[str, Any]:
    return native_attachment.acquire_intent(
        acquisition_method=native_attachment.ACQUISITION_RESUME,
        acquisition_receipt={"kind": "pinned-resume", "receipt_id": "receipt-abc"},
        admits_only_new_instructions=True,
        replays_task_bytes=False,
    )


class FakePane:
    """A pane transport whose every outcome is chosen by the test.

    Records calls so a test can assert the *absence* of a second
    ``create_pane`` — which is the actual safety property for re-entry,
    and is invisible if you only look at return values.
    """

    def __init__(
        self,
        *,
        observation: Optional[Mapping[str, Any]] = None,
        create_error: Optional[Exception] = None,
        observe_error: Optional[Exception] = None,
        handle: Any = "native-window",
    ) -> None:
        self.observation = observation
        self.create_error = create_error
        self.observe_error = observe_error
        self.handle = handle
        self.created: list[list[str]] = []
        self.observe_calls = 0

    def create_pane(self, *, argv: Sequence[str]) -> str:
        self.created.append(list(argv))
        if self.create_error is not None:
            raise self.create_error
        return self.handle

    def observe(self) -> Optional[Mapping[str, Any]]:
        self.observe_calls += 1
        if self.observe_error is not None:
            raise self.observe_error
        return self.observation


def _observation(argv: Sequence[str], *, pid: int = 4321) -> dict[str, Any]:
    return {
        "pane_id": "%7",
        "pid": pid,
        "start_marker": "Thu Jul 24 10:00:00 2026",
        "argv": list(argv),
    }


def _expected_argv(path: str) -> list[str]:
    return [path, native_tui_launch.kimi_native_launch.RESUME_OPTION, SESSION]


def _start(pinned: tuple[str, str], transport: Any, **overrides: Any) -> dict[str, Any]:
    path, digest = pinned
    kwargs: dict[str, Any] = {
        "provider": PROVIDER,
        "native_session_id": SESSION,
        "terminal_id": TERMINAL,
        "generation": GENERATION,
        "execution_mode": em.NATIVE_TUI,
        "intent": _intent(),
        "binary": path,
        "binary_sha256": digest,
        "transport": transport,
    }
    kwargs.update(overrides)
    return native_tui_launch.start(**kwargs)


# --------------------------------------------------------------------------
# The golden path
# --------------------------------------------------------------------------


def test_launch_claims_ownership_before_starting_the_process(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    """Ownership must be durable before a process can hold the session.

    Asserted from inside ``create_pane``: by the time the process is
    about to exist, the store must already name this owner, because the
    window between "process running" and "ownership recorded" is exactly
    where a second launcher would see the session as free.
    """
    path, _ = pinned_binary
    seen: dict[str, Any] = {}

    class ObservingPane(FakePane):
        def create_pane(self, *, argv: Sequence[str]) -> str:
            seen["record"] = native_attachment.get(PROVIDER, SESSION)
            return super().create_pane(argv=argv)

    pane = ObservingPane(observation=_observation(_expected_argv(path)))
    result = _start(pinned_binary, pane)

    assert seen["record"] is not None
    assert seen["record"]["state"] == native_attachment.STARTING
    assert seen["record"]["owner"]["terminal_id"] == TERMINAL
    assert seen["record"]["owner"]["generation"] == GENERATION
    assert seen["record"]["owner"]["execution_mode"] == em.NATIVE_TUI
    assert result["outcome"] == native_tui_launch.OUTCOME_LAUNCHED


def test_launch_publishes_the_observed_process_identity(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    path, digest = pinned_binary
    argv = _expected_argv(path)
    pane = FakePane(observation=_observation(argv, pid=9182))
    result = _start(pinned_binary, pane)

    assert result["schema"] == native_tui_launch.LAUNCH_SCHEMA
    assert result["execution_mode"] == em.NATIVE_TUI
    assert result["argv"] == argv
    assert result["binary_sha256"] == digest
    assert pane.created == [argv]

    stored = native_attachment.get(PROVIDER, SESSION)
    assert stored is not None
    assert stored["state"] == native_attachment.ATTACHED
    assert stored["owner"]["process_identity"]["pid"] == 9182
    assert stored["owner"]["pane_id"] == "%7"


def test_launch_argv_digest_covers_the_exact_argv(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    """The digest must distinguish argvs that differ only in word boundaries.

    A digest built by joining on a space would give ``["a b"]`` and
    ``["a", "b"]`` the same value, which would let a receipt attest to an
    argv that is not the one that ran.
    """
    path, _ = pinned_binary
    argv = _expected_argv(path)
    result = _start(pinned_binary, FakePane(observation=_observation(argv)))
    expected = hashlib.sha256("\x00".join(argv).encode()).hexdigest()
    assert result["launch_argv_sha256"] == expected
    assert result["launch_argv_sha256"] != hashlib.sha256(" ".join(argv).encode()).hexdigest()


def test_relaunching_an_attached_session_never_touches_the_pane(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    path, _ = pinned_binary
    argv = _expected_argv(path)
    _start(pinned_binary, FakePane(observation=_observation(argv)))

    second = FakePane(observation=_observation(argv))
    result = _start(pinned_binary, second)

    assert result["outcome"] == native_tui_launch.OUTCOME_ALREADY_ATTACHED
    assert second.created == []
    assert second.observe_calls == 0


# --------------------------------------------------------------------------
# Mode separation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [em.ACP, "", "NATIVE_TUI", "native", None, 7])
def test_the_native_branch_refuses_every_non_native_mode(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], mode: Any
) -> None:
    pane = FakePane()
    with pytest.raises(native_tui_launch.NativeLaunchInvalid):
        _start(pinned_binary, pane, execution_mode=mode)
    assert pane.created == []
    assert native_attachment.get(PROVIDER, SESSION) is None


# --------------------------------------------------------------------------
# The pinned binary
# --------------------------------------------------------------------------


def test_a_drifted_binary_is_refused_with_nothing_claimed(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    path, _ = pinned_binary
    pane = FakePane(observation=_observation(_expected_argv(path)))
    with pytest.raises(native_tui_launch.NativeLaunchInvalid, match="digest"):
        _start(pinned_binary, pane, binary_sha256="0" * 64)
    assert pane.created == []
    assert native_attachment.get(PROVIDER, SESSION) is None


def test_a_bare_binary_name_is_refused(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    """``kimi`` is not a launch target; it is a question for ``PATH``."""
    _, digest = pinned_binary
    with pytest.raises(native_tui_launch.NativeLaunchInvalid, match="absolute"):
        _start(pinned_binary, FakePane(), binary="kimi", binary_sha256=digest)


def test_a_non_executable_binary_is_refused(isolated_memory_db: Any, tmp_path: Any) -> None:
    target = tmp_path / "not-exec"
    target.write_bytes(b"x")
    target.chmod(0o644)
    path = os.path.realpath(str(target))
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    with pytest.raises(native_tui_launch.NativeLaunchInvalid, match="executable"):
        _start((path, digest), FakePane())


@pytest.mark.parametrize("bad", ["", "abc", "z" * 64, "A" * 63])
def test_a_malformed_digest_pin_is_refused(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], bad: str
) -> None:
    with pytest.raises(native_tui_launch.NativeLaunchInvalid):
        _start(pinned_binary, FakePane(), binary_sha256=bad)


# --------------------------------------------------------------------------
# Freezing: every unresolved outcome
# --------------------------------------------------------------------------


def _assert_frozen(reason: str) -> None:
    stored = native_attachment.get(PROVIDER, SESSION)
    assert stored is not None
    assert stored["state"] == native_attachment.AMBIGUOUS
    assert stored["ambiguity_reason"] == reason


def test_a_raising_pane_create_freezes_rather_than_retries(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    pane = FakePane(create_error=RuntimeError("tmux said no"))
    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(pinned_binary, pane)
    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PANE_CREATE
    _assert_frozen(native_tui_launch.AMBIGUOUS_PANE_CREATE)


def test_a_pane_create_that_returns_no_handle_freezes(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(pinned_binary, FakePane(handle=None))
    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PANE_CREATE
    _assert_frozen(native_tui_launch.AMBIGUOUS_PANE_CREATE)


def test_an_unreadable_pane_and_an_absent_pane_freeze_with_different_reasons(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    """Both freeze, but the recorded reason must tell them apart.

    "We could not look" and "we looked and nothing was there" send a
    later reconciler to different evidence, so collapsing them to one
    reason destroys the only signal it has.
    """
    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous):
        _start(pinned_binary, FakePane(observe_error=OSError("ps unavailable")))
    _assert_frozen(native_tui_launch.AMBIGUOUS_PANE_UNREADABLE)

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous):
        native_tui_launch.start(
            provider=PROVIDER,
            native_session_id="sess-native-0002",
            terminal_id=TERMINAL,
            generation=GENERATION,
            execution_mode=em.NATIVE_TUI,
            intent=_intent(),
            binary=pinned_binary[0],
            binary_sha256=pinned_binary[1],
            transport=FakePane(observation=None),
        )
    absent = native_attachment.get(PROVIDER, "sess-native-0002")
    assert absent is not None
    assert absent["ambiguity_reason"] == native_tui_launch.AMBIGUOUS_PANE_ABSENT_AFTER_CREATE


def test_a_transport_raising_the_module_error_still_freezes(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    """The concrete tmux transport signals "unreadable" with this error.

    It must not travel out un-frozen just because it belongs to this
    module's own exception family — an unreadable pane is unresolved
    however it was reported.
    """
    pane = FakePane(observe_error=native_tui_launch.NativeLaunchUnavailable("pane unreadable"))
    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous):
        _start(pinned_binary, pane)
    _assert_frozen(native_tui_launch.AMBIGUOUS_PANE_UNREADABLE)


@pytest.mark.parametrize(
    "observation",
    [
        {"pid": 1, "start_marker": "m", "argv": ["/x/kimi"]},
        {"pane_id": "%1", "start_marker": "m", "argv": ["/x/kimi"]},
        {"pane_id": "%1", "pid": 1, "argv": ["/x/kimi"]},
        {"pane_id": "%1", "pid": 1, "start_marker": "m"},
        {"pane_id": "%1", "pid": 0, "start_marker": "m", "argv": []},
        {"pane_id": "%1", "pid": True, "start_marker": "m", "argv": []},
        {"pane_id": "%1", "pid": 1, "start_marker": "m", "argv": "not-a-list"},
    ],
)
def test_an_incomplete_observation_freezes_rather_than_being_filled_in(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], observation: dict[str, Any]
) -> None:
    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous):
        _start(pinned_binary, FakePane(observation=observation))
    _assert_frozen(native_tui_launch.AMBIGUOUS_PANE_UNREADABLE)


@pytest.mark.parametrize(
    "argv",
    [
        # The picker hazard realised: the resume option lost its argument.
        ["{binary}", "--session"],
        # Resuming a different session entirely.
        ["{binary}", "--session", "sess-native-9999"],
        # A bare interactive start — a brand-new session, not a resume.
        ["{binary}"],
        # The right tokens, not adjacent.
        ["{binary}", "--session", "--verbose", "sess-native-0001"],
        # Two resumes: which one won is not knowable from here.
        ["{binary}", "--session", "sess-native-0001", "--session", "sess-native-9999"],
    ],
)
def test_a_pane_not_resuming_the_bound_session_freezes_and_never_attaches(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], argv: list[str]
) -> None:
    path, _ = pinned_binary
    observed = [token.format(binary=path) for token in argv]
    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(pinned_binary, FakePane(observation=_observation(observed)))
    assert caught.value.reason == native_tui_launch.AMBIGUOUS_ARGV_MISMATCH
    _assert_frozen(native_tui_launch.AMBIGUOUS_ARGV_MISMATCH)


def test_a_frozen_session_refuses_every_later_launch(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    """The point of freezing: the next attempt cannot proceed.

    Not merely that this call raised — that a caller who retries, with a
    healthy transport, is still refused.  Recovery has to go through an
    explicit no-survivor proof.
    """
    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous):
        _start(pinned_binary, FakePane(create_error=RuntimeError("boom")))

    path, _ = pinned_binary
    healthy = FakePane(observation=_observation(_expected_argv(path)))
    with pytest.raises(native_tui_launch.NativeLaunchError):
        _start(pinned_binary, healthy)
    assert healthy.created == []


# --------------------------------------------------------------------------
# Re-entry over a ``starting`` row
# --------------------------------------------------------------------------


def test_reentry_after_a_crash_reconciles_without_relaunching(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    path, _ = pinned_binary
    argv = _expected_argv(path)

    native_attachment.declare(
        provider=PROVIDER,
        native_session_id=SESSION,
        terminal_id=TERMINAL,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        intent=_intent(),
    )
    native_attachment.mark_starting(
        provider=PROVIDER,
        native_session_id=SESSION,
        terminal_id=TERMINAL,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
    )

    pane = FakePane(observation=_observation(argv))
    result = _start(pinned_binary, pane)

    assert result["outcome"] == native_tui_launch.OUTCOME_RECONCILED
    assert pane.created == [], "re-entry must never start a second TUI"
    assert native_attachment.get(PROVIDER, SESSION)["state"] == native_attachment.ATTACHED


def test_reentry_finding_no_pane_freezes_instead_of_relaunching(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    """An absent pane after ``starting`` is unresolved, not free.

    It cannot distinguish "never started" from "started, ran, and
    exited", and those differ in whether the provider session was
    mutated.  Relaunching would replay onto a session that may already
    have advanced.
    """
    native_attachment.declare(
        provider=PROVIDER,
        native_session_id=SESSION,
        terminal_id=TERMINAL,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        intent=_intent(),
    )
    native_attachment.mark_starting(
        provider=PROVIDER,
        native_session_id=SESSION,
        terminal_id=TERMINAL,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
    )

    pane = FakePane(observation=None)
    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(pinned_binary, pane)
    assert caught.value.reason == native_tui_launch.AMBIGUOUS_START_CROSSED_NO_PANE
    assert pane.created == []
    _assert_frozen(native_tui_launch.AMBIGUOUS_START_CROSSED_NO_PANE)


def test_reentry_over_a_pane_running_another_session_freezes(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    native_attachment.declare(
        provider=PROVIDER,
        native_session_id=SESSION,
        terminal_id=TERMINAL,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        intent=_intent(),
    )
    native_attachment.mark_starting(
        provider=PROVIDER,
        native_session_id=SESSION,
        terminal_id=TERMINAL,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
    )
    path, _ = pinned_binary
    pane = FakePane(observation=_observation([path, "--session", "sess-other"]))
    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous):
        _start(pinned_binary, pane)
    _assert_frozen(native_tui_launch.AMBIGUOUS_ARGV_MISMATCH)


# --------------------------------------------------------------------------
# Cross-owner exclusion
# --------------------------------------------------------------------------


def test_a_second_generation_cannot_launch_over_a_live_attachment(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    path, _ = pinned_binary
    _start(pinned_binary, FakePane(observation=_observation(_expected_argv(path))))

    intruder = FakePane(observation=_observation(_expected_argv(path)))
    with pytest.raises(native_tui_launch.NativeLaunchConflict):
        _start(pinned_binary, intruder, generation="gen-native-0002")
    assert intruder.created == []

    stored = native_attachment.get(PROVIDER, SESSION)
    assert stored["owner"]["generation"] == GENERATION


def test_a_draining_owner_is_not_relaunched_into(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    path, _ = pinned_binary
    _start(pinned_binary, FakePane(observation=_observation(_expected_argv(path))))
    native_attachment.mark_draining(
        provider=PROVIDER,
        native_session_id=SESSION,
        terminal_id=TERMINAL,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
    )

    pane = FakePane(observation=_observation(_expected_argv(path)))
    with pytest.raises(native_tui_launch.NativeLaunchConflict, match="draining"):
        _start(pinned_binary, pane)
    assert pane.created == []


# --------------------------------------------------------------------------
# The concrete tmux transport
# --------------------------------------------------------------------------


class FakeBackend:
    def __init__(self, *, identity: Any = None, exists: bool = False) -> None:
        self.identity = identity
        self.exists = exists
        self.calls: list[tuple[Any, ...]] = []

    def create_window_with_argv(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        argv: list[str],
        working_directory: Any = None,
        extra_env: Any = None,
    ) -> str:
        self.calls.append((session_name, window_name, terminal_id, tuple(argv)))
        return window_name

    def window_identity(self, session_name: str, window_name: str) -> Any:
        return self.identity

    def window_exists(self, session_name: str, window_name: str) -> bool:
        return self.exists


def _pane(backend: FakeBackend) -> native_tui_launch.TmuxNativePane:
    return native_tui_launch.TmuxNativePane(
        backend, session_name="cao", window_name="w1", terminal_id=TERMINAL
    )


def test_tmux_transport_execs_the_argv_directly() -> None:
    """No shell, no typed command line — the TUI is the pane's own process."""
    backend = FakeBackend()
    handle = _pane(backend).create_pane(argv=["/bin/kimi", "--session", SESSION])
    assert handle == "w1"
    assert backend.calls == [("cao", "w1", TERMINAL, ("/bin/kimi", "--session", SESSION))]


def test_tmux_transport_reports_a_missing_window_as_absent() -> None:
    assert _pane(FakeBackend(identity=None, exists=False)).observe() is None


def test_tmux_transport_refuses_to_call_an_unreadable_window_absent() -> None:
    """A window that exists but will not report identity is not absence.

    Returning ``None`` here would license the caller to treat a possibly
    live provider process as "nothing there".
    """
    with pytest.raises(native_tui_launch.NativeLaunchUnavailable):
        _pane(FakeBackend(identity=None, exists=True)).observe()


def test_tmux_transport_raises_when_the_pane_pid_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pane = _pane(FakeBackend(identity={"pane_id": "%3"}, exists=True))
    monkeypatch.setattr(pane, "_pane_pid", lambda: None)
    with pytest.raises(native_tui_launch.NativeLaunchUnavailable):
        pane.observe()


def test_tmux_transport_splits_the_observed_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pane = _pane(FakeBackend(identity={"pane_id": "%3"}, exists=True))
    monkeypatch.setattr(pane, "_pane_pid", lambda: 777)
    monkeypatch.setattr(
        native_tui_launch,
        "_process_field",
        lambda pid, field: (
            "Thu Jul 24 10:00:00 2026" if field == "lstart=" else f"/bin/kimi --session {SESSION}"
        ),
    )
    observed = pane.observe()
    assert observed == {
        "pane_id": "%3",
        "pid": 777,
        "start_marker": "Thu Jul 24 10:00:00 2026",
        "argv": ["/bin/kimi", "--session", SESSION],
    }
