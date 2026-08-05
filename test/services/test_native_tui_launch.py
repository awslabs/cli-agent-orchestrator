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
import tempfile
import time
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
        rendered: Optional[Sequence[str]] = None,
        render_error: Optional[Exception] = None,
    ) -> None:
        self.observation = observation
        self.create_error = create_error
        self.observe_error = observe_error
        self.handle = handle
        self.rendered = list(rendered) if rendered is not None else []
        self.render_error = render_error
        self.created: list[list[str]] = []
        self.observe_calls = 0
        self.render_calls = 0
        self.render_targets: list[str] = []

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

    def capture_render(self, pane_id: str) -> list[str]:
        # The rendered-screen evidence channel (COND-0312): a separate read
        # from the exact observed pane, raised rather than empty when the pane
        # cannot be looked at.
        self.render_calls += 1
        self.render_targets.append(pane_id)
        if self.render_error is not None:
            raise self.render_error
        return list(self.rendered)


class SequencedPane(FakePane):
    """Return successive observations while preserving one created pane."""

    def __init__(self, observations: Sequence[Mapping[str, Any]]) -> None:
        super().__init__()
        self.observations = list(observations)

    def observe(self) -> Optional[Mapping[str, Any]]:
        self.observe_calls += 1
        index = min(self.observe_calls - 1, len(self.observations) - 1)
        return self.observations[index]


class _FakeClock:
    """A monotonic clock that advances only when the code under test sleeps.

    The render-convergence loop derives its deadline from ``time.monotonic``
    and waits out each poll with ``time.sleep``; driving both from one fake
    makes deadline behaviour exact — no real sleeps, no wall-clock races.
    ``overshoot`` models scheduler/host delay: every requested sleep resumes
    that much *late*, so the deadline is tested against real oversleep
    rather than an assumed epsilon.
    """

    def __init__(self, *, overshoot: float = 0.0) -> None:
        self.now = 1_000.0
        self.overshoot = overshoot

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds + self.overshoot


@pytest.fixture
def fake_clock(monkeypatch: Any) -> _FakeClock:
    clock = _FakeClock()
    monkeypatch.setattr(native_tui_launch.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(native_tui_launch.time, "sleep", clock.sleep)
    return clock


class _SlowBootPane(FakePane):
    """A rewritten-title pane whose header paints at a set fake-clock time.

    Models a cold/loaded Kimi boot: captures before ``render_at`` show a
    boot screen with no header, captures at or after it show the exact
    bound-session header.  Arrival time is a function of the launch's own
    clock, so a test controls it to the poll.  Capture times are recorded
    so a test can prove no read ever happened past the deadline.
    """

    def __init__(self, clock: _FakeClock, *, render_at: float) -> None:
        super().__init__(observation=_observation(_REWRITTEN_ARGV))
        self._clock = clock
        self._render_at = render_at
        self.capture_times: list[float] = []

    def capture_render(self, pane_id: str) -> list[str]:
        self.render_calls += 1
        self.render_targets.append(pane_id)
        self.capture_times.append(self._clock.now)
        if self._clock.now < self._render_at:
            return ["│  Welcome to Kimi Code!  (booting)"]
        return _native_header_rows()


def _canonical_workdir() -> str:
    """A real, existing, canonical directory the launcher will accept.

    Real rather than invented because the launcher stats it, and taken
    through ``realpath`` because on macOS the temporary root is reached
    through a symlink — which is the very shape these tests are here to
    reject when it reaches the launcher unresolved.
    """
    return os.path.realpath(tempfile.gettempdir())


def _observation(
    argv: Sequence[str], *, pid: int = 4321, cwd: Optional[str] = None
) -> dict[str, Any]:
    return {
        "pane_id": "%7",
        "pid": pid,
        "start_marker": "Thu Jul 24 10:00:00 2026",
        "argv": list(argv),
        "cwd": _canonical_workdir() if cwd is None else cwd,
    }


def _expected_argv(path: str) -> list[str]:
    return [path, native_tui_launch.kimi_native_launch.RESUME_OPTION, SESSION]


def _pinned_wrapper(tmp_path: Any, shebang: bytes) -> tuple[str, str]:
    wrapper = tmp_path / "wrapper"
    wrapper.write_bytes(shebang + b"exit 0\n")
    wrapper.chmod(0o755)
    path = os.path.realpath(str(wrapper))
    return path, hashlib.sha256(wrapper.read_bytes()).hexdigest()


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
        "working_directory": _canonical_workdir(),
        "transport": transport,
    }
    kwargs.update(overrides)
    return native_tui_launch.start(**kwargs)


# Kimi Code 0.31.0 rewrites its process title to ``kimi-code`` after parsing,
# so the kernel argv the pane observer reads no longer carries the resumed
# ``--session <id>`` (COND-0312).  These two constants model that live defect
# for the regression suite below.
_REWRITTEN_ARGV = ["kimi-code", "", "", "", ""]
PINNED_0310 = "0.31.0"


def _native_header_rows(
    *, session: str = SESSION, directory: Optional[str] = None, version: str = PINNED_0310
) -> list[str]:
    """The Kimi 0.31.0 native header, framed the way ``capture-pane`` paints it.

    The label rows sit inside the box's ``│`` verticals exactly as the live
    pane renders them, so the parser must tolerate that chrome.  The directory
    defaults to the worktree the session was minted in.
    """
    directory = _canonical_workdir() if directory is None else directory
    return [
        "│  Welcome to Kimi Code!                                                                              │",
        f"│  Directory: {directory}                                                                              │",
        f"│  Session:   {session}                                                                                │",
        "│  Model:     K3                                                                                       │",
        f"│  Version:   {version}                                                                                │",
    ]


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


def test_launch_waits_for_the_same_wrapper_process_to_exec_the_inner_binary(
    isolated_memory_db: Any,
    pinned_binary: tuple[str, str],
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    wrapper, _ = pinned_binary
    inner = tmp_path / "inner"
    inner.write_bytes(b"inner")
    inner.chmod(0o755)
    inner_path = os.path.realpath(str(inner))
    pane = SequencedPane(
        [
            _observation(["/usr/bin/python3", wrapper, "--session", SESSION]),
            _observation([inner_path, "--session", SESSION]),
        ]
    )
    monkeypatch.setattr(native_tui_launch.time, "sleep", lambda _: None)

    result = _start(
        pinned_binary,
        pane,
        expected_inner_executable=inner_path,
    )

    assert result["outcome"] == native_tui_launch.OUTCOME_LAUNCHED
    assert result["binary"] == wrapper
    assert result["pane_observation"]["argv"][0] == inner_path
    assert pane.observe_calls == 2
    assert native_attachment.get(PROVIDER, SESSION)["state"] == native_attachment.ATTACHED


def test_launch_waits_for_an_env_shebang_wrapper_to_exec_the_inner_binary(
    isolated_memory_db: Any,
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    pinned = _pinned_wrapper(tmp_path, b"#!/usr/bin/env python3\n")
    wrapper, _ = pinned
    inner = tmp_path / "inner"
    inner.write_bytes(b"inner")
    inner.chmod(0o755)
    inner_path = os.path.realpath(str(inner))
    pane = SequencedPane(
        [
            _observation(
                [
                    native_tui_launch.ENV_EXECUTABLE,
                    "python3",
                    wrapper,
                    "--session",
                    SESSION,
                ]
            ),
            _observation([inner_path, "--session", SESSION]),
        ]
    )
    monkeypatch.setattr(native_tui_launch.time, "sleep", lambda _: None)

    result = _start(pinned, pane, expected_inner_executable=inner_path)

    assert result["outcome"] == native_tui_launch.OUTCOME_LAUNCHED
    assert result["pane_observation"]["argv"][0] == inner_path
    assert pane.observe_calls == 2


def test_env_shebang_transient_preserves_whitespace_bearing_argv(
    isolated_memory_db: Any,
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    pinned = _pinned_wrapper(tmp_path, b"#!/usr/bin/env python3\n")
    wrapper, _ = pinned
    inner = tmp_path / "inner"
    inner.write_bytes(b"inner")
    inner.chmod(0o755)
    inner_path = os.path.realpath(str(inner))
    extra_args = ["--settings", '{"hook": "two words"}']
    launch_tail = [*extra_args, native_tui_launch.kimi_native_launch.RESUME_OPTION, SESSION]
    pane = SequencedPane(
        [
            _observation(
                [
                    native_tui_launch.ENV_EXECUTABLE,
                    "python3",
                    wrapper,
                    *launch_tail,
                ]
            ),
            _observation([inner_path, *launch_tail]),
        ]
    )
    monkeypatch.setattr(native_tui_launch.time, "sleep", lambda _: None)

    result = _start(
        pinned,
        pane,
        expected_inner_executable=inner_path,
        extra_args=extra_args,
    )

    assert result["outcome"] == native_tui_launch.OUTCOME_LAUNCHED
    assert result["pane_observation"]["argv"] == [inner_path, *launch_tail]
    assert pane.observe_calls == 2


def test_interpreter_shebang_transient_preserves_whitespace_bearing_argv(
    isolated_memory_db: Any,
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    pinned = _pinned_wrapper(tmp_path, b"#!/usr/bin/python3\n")
    wrapper, _ = pinned
    inner = tmp_path / "inner"
    inner.write_bytes(b"inner")
    inner.chmod(0o755)
    inner_path = os.path.realpath(str(inner))
    extra_args = ["--settings", '{"hook": "two words"}']
    launch_tail = [*extra_args, native_tui_launch.kimi_native_launch.RESUME_OPTION, SESSION]
    pane = SequencedPane(
        [
            _observation(["/usr/bin/python3", wrapper, *launch_tail]),
            _observation([inner_path, *launch_tail]),
        ]
    )
    monkeypatch.setattr(native_tui_launch.time, "sleep", lambda _: None)

    result = _start(
        pinned,
        pane,
        expected_inner_executable=inner_path,
        extra_args=extra_args,
    )

    assert result["outcome"] == native_tui_launch.OUTCOME_LAUNCHED
    assert result["pane_observation"]["argv"] == [inner_path, *launch_tail]
    assert pane.observe_calls == 2


def test_env_shebang_transient_refuses_a_whitespace_tail_mismatch(
    isolated_memory_db: Any,
    tmp_path: Any,
) -> None:
    pinned = _pinned_wrapper(tmp_path, b"#!/usr/bin/env python3\n")
    wrapper, _ = pinned
    inner = tmp_path / "inner"
    inner.write_bytes(b"inner")
    inner.chmod(0o755)
    extra_args = ["--settings", '{"hook": "two words"}']
    pane = FakePane(
        observation=_observation(
            [
                native_tui_launch.ENV_EXECUTABLE,
                "python3",
                wrapper,
                "--settings",
                '{"hook": "different words"}',
                native_tui_launch.kimi_native_launch.RESUME_OPTION,
                SESSION,
            ]
        )
    )

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(
            pinned,
            pane,
            expected_inner_executable=os.path.realpath(str(inner)),
            extra_args=extra_args,
        )

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH
    _assert_frozen(native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH)


def test_env_shebang_transient_accepts_a_wrapper_path_with_spaces(
    isolated_memory_db: Any,
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    wrapper_dir = tmp_path / "wrapper dir"
    wrapper_dir.mkdir()
    pinned = _pinned_wrapper(wrapper_dir, b"#!/usr/bin/env python3\n")
    wrapper, _ = pinned
    inner_dir = tmp_path / "inner dir"
    inner_dir.mkdir()
    inner = inner_dir / "inner"
    inner.write_bytes(b"inner")
    inner.chmod(0o755)
    inner_path = os.path.realpath(str(inner))
    pane = SequencedPane(
        [
            _observation(
                [
                    native_tui_launch.ENV_EXECUTABLE,
                    "python3",
                    wrapper,
                    "--session",
                    SESSION,
                ]
            ),
            _observation([inner_path, "--session", SESSION]),
        ]
    )
    monkeypatch.setattr(native_tui_launch.time, "sleep", lambda _: None)

    result = _start(pinned, pane, expected_inner_executable=inner_path)

    assert result["outcome"] == native_tui_launch.OUTCOME_LAUNCHED
    assert pane.observe_calls == 2


@pytest.mark.parametrize(
    ("shebang", "observed_prefix"),
    [
        (b"#!/usr/bin/env python3\n", [native_tui_launch.ENV_EXECUTABLE, "python3.13"]),
        (b"#!/usr/bin/env -S python3\n", [native_tui_launch.ENV_EXECUTABLE, "python3"]),
        (b"#!/usr/bin/env python3 -u\n", [native_tui_launch.ENV_EXECUTABLE, "python3"]),
        (b"not-a-shebang\n", [native_tui_launch.ENV_EXECUTABLE, "python3"]),
        (
            b"#!" + b"x" * (native_tui_launch.MAX_SHEBANG_LINE_BYTES + 1),
            [native_tui_launch.ENV_EXECUTABLE, "python3"],
        ),
    ],
)
def test_env_shebang_transient_refuses_unpinned_interpreter_forms(
    isolated_memory_db: Any,
    tmp_path: Any,
    shebang: bytes,
    observed_prefix: list[str],
) -> None:
    pinned = _pinned_wrapper(tmp_path, shebang)
    wrapper, _ = pinned
    inner = tmp_path / "inner"
    inner.write_bytes(b"inner")
    inner.chmod(0o755)
    pane = FakePane(observation=_observation([*observed_prefix, wrapper, "--session", SESSION]))

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(
            pinned,
            pane,
            expected_inner_executable=os.path.realpath(str(inner)),
        )

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH
    _assert_frozen(native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH)


def test_bare_env_dash_s_is_not_an_interpreter_token(tmp_path: Any) -> None:
    wrapper, _ = _pinned_wrapper(tmp_path, b"#!/usr/bin/env -S\n")
    assert native_tui_launch._env_shebang_interpreter(wrapper) is None


@pytest.mark.parametrize(
    "observed_argv",
    [
        [native_tui_launch.ENV_EXECUTABLE, "python3", "/tmp/not-the-wrapper", "--session", SESSION],
        [
            native_tui_launch.ENV_EXECUTABLE,
            "python3",
            "{wrapper}",
            "--session",
            SESSION,
            "--wrong-tail",
        ],
    ],
)
def test_env_shebang_transient_refuses_wrong_wrapper_or_tail(
    isolated_memory_db: Any,
    tmp_path: Any,
    observed_argv: list[str],
) -> None:
    pinned = _pinned_wrapper(tmp_path, b"#!/usr/bin/env python3\n")
    wrapper, _ = pinned
    inner = tmp_path / "inner"
    inner.write_bytes(b"inner")
    inner.chmod(0o755)
    argv = [wrapper if value == "{wrapper}" else value for value in observed_argv]

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(
            pinned,
            FakePane(observation=_observation(argv)),
            expected_inner_executable=os.path.realpath(str(inner)),
        )

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH
    _assert_frozen(native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH)


def test_env_shebang_transient_requires_the_canonical_env_binary(
    isolated_memory_db: Any,
    tmp_path: Any,
) -> None:
    pinned = _pinned_wrapper(tmp_path, b"#!/usr/bin/env python3\n")
    wrapper, _ = pinned
    fake_env = tmp_path / "env"
    fake_env.write_bytes(b"env")
    fake_env.chmod(0o755)
    inner = tmp_path / "inner"
    inner.write_bytes(b"inner")
    inner.chmod(0o755)

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(
            pinned,
            FakePane(
                observation=_observation(
                    [
                        os.path.realpath(str(fake_env)),
                        "python3",
                        wrapper,
                        "--session",
                        SESSION,
                    ]
                )
            ),
            expected_inner_executable=os.path.realpath(str(inner)),
        )

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH
    _assert_frozen(native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH)


def test_inner_exec_convergence_refuses_a_replaced_process_identity(
    isolated_memory_db: Any,
    pinned_binary: tuple[str, str],
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    wrapper, _ = pinned_binary
    inner = tmp_path / "inner"
    inner.write_bytes(b"inner")
    inner.chmod(0o755)
    inner_path = os.path.realpath(str(inner))
    pane = SequencedPane(
        [
            _observation(
                ["/usr/bin/python3", wrapper, "--session", SESSION],
                pid=4321,
            ),
            _observation([inner_path, "--session", SESSION], pid=4322),
        ]
    )
    monkeypatch.setattr(native_tui_launch.time, "sleep", lambda _: None)

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(
            pinned_binary,
            pane,
            expected_inner_executable=inner_path,
        )

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH
    _assert_frozen(native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH)


def test_inner_exec_convergence_freezes_when_the_wrapper_never_execs(
    isolated_memory_db: Any,
    pinned_binary: tuple[str, str],
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    wrapper, _ = pinned_binary
    inner = tmp_path / "inner"
    inner.write_bytes(b"inner")
    inner.chmod(0o755)
    inner_path = os.path.realpath(str(inner))
    pane = FakePane(observation=_observation(["/usr/bin/python3", wrapper, "--session", SESSION]))
    monkeypatch.setattr(native_tui_launch, "INNER_EXEC_CONVERGENCE_TIMEOUT_SECONDS", 0.0)

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(
            pinned_binary,
            pane,
            expected_inner_executable=inner_path,
        )

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH
    assert "did not converge" in caught.value.detail
    _assert_frozen(native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH)


def test_inner_exec_convergence_does_not_wait_for_a_foreign_process(
    isolated_memory_db: Any,
    pinned_binary: tuple[str, str],
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    inner = tmp_path / "inner"
    inner.write_bytes(b"inner")
    inner.chmod(0o755)
    inner_path = os.path.realpath(str(inner))
    pane = FakePane(
        observation=_observation(["/usr/bin/python3", "/tmp/not-the-wrapper", "--session", SESSION])
    )
    slept: list[float] = []
    monkeypatch.setattr(native_tui_launch.time, "sleep", slept.append)

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(
            pinned_binary,
            pane,
            expected_inner_executable=inner_path,
        )

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH
    assert slept == []
    _assert_frozen(native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH)


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
            working_directory=_canonical_workdir(),
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
        {"pid": 1, "start_marker": "m", "argv": ["/x/kimi"], "cwd": "/"},
        {"pane_id": "%1", "start_marker": "m", "argv": ["/x/kimi"], "cwd": "/"},
        {"pane_id": "%1", "pid": 1, "argv": ["/x/kimi"], "cwd": "/"},
        {"pane_id": "%1", "pid": 1, "start_marker": "m", "cwd": "/"},
        {"pane_id": "%1", "pid": 0, "start_marker": "m", "argv": [], "cwd": "/"},
        {"pane_id": "%1", "pid": True, "start_marker": "m", "argv": [], "cwd": "/"},
        {"pane_id": "%1", "pid": 1, "start_marker": "m", "argv": "not-a-list", "cwd": "/"},
        # A pane that cannot say where it is has not been shown to be in
        # the directory its session was minted in, so the observation is
        # incomplete rather than merely unverified.
        {"pane_id": "%1", "pid": 1, "start_marker": "m", "argv": ["/x/kimi"]},
        {"pane_id": "%1", "pid": 1, "start_marker": "m", "argv": ["/x/kimi"], "cwd": ""},
        {"pane_id": "%1", "pid": 1, "start_marker": "m", "argv": ["/x/kimi"], "cwd": 7},
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


# --------------------------------------------------------------------------
# Kimi 0.31.0 process-title rewrite: the rendered native header is the proof
# (COND-0312)
# --------------------------------------------------------------------------


def test_a_kimi_0310_pane_that_rewrote_its_title_attaches_via_the_rendered_header(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], monkeypatch: Any
) -> None:
    """The exact live defect (COND-0312).

    Kimi Code 0.31.0 rewrites ``process.title`` to ``kimi-code`` after parsing,
    so the kernel argv the observer reads back is ``['kimi-code', '', ...]`` and
    the resumed ``--session <id>`` is gone.  The argv proof necessarily freezes
    on that argv -- which is what grounded p1-closure.  The TUI renders a strict
    native header whose ``Session:`` line names the exact minted session, so the
    attachment is published from that header instead, without weakening any of
    the process-identity, cwd, or image checks.
    """
    monkeypatch.setattr(native_tui_launch.time, "sleep", lambda _: None)
    pane = FakePane(
        observation=_observation(_REWRITTEN_ARGV),
        rendered=_native_header_rows(),
    )

    result = _start(pinned_binary, pane, provider_version=PINNED_0310)

    assert result["outcome"] == native_tui_launch.OUTCOME_LAUNCHED
    assert pane.render_calls >= 1
    # The capture is targeted at the exact observed pane id, never the window.
    assert pane.render_targets
    assert all(target == "%7" for target in pane.render_targets)
    # Success evidence names the proof channel like the freeze reason does:
    # the rendered-header rule, not an inferred argv proof.
    assert result["session_proof"] == native_tui_launch.SESSION_PROOF_KIMI_RENDERED
    assert result["session_proof"] == native_tui_launch.kimi_native_launch.RULE_KIMI_NATIVE_HEADER
    stored = native_attachment.get(PROVIDER, SESSION)
    assert stored is not None and stored["state"] == native_attachment.ATTACHED


def test_a_rewritten_title_pane_with_a_wrong_rendered_session_freezes(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], fake_clock: _FakeClock
) -> None:
    """A header naming another session is observed for the whole runway.

    A wrong session is not "no header": the loop keeps watching in case the
    bound header still paints, and only the deadline ends the wait.  Frozen
    with the exact runway named, never published.
    """
    pane = FakePane(
        observation=_observation(_REWRITTEN_ARGV),
        rendered=_native_header_rows(session="session_someone_else"),
    )

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(pinned_binary, pane, provider_version=PINNED_0310)

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PANE_RENDER_MISMATCH
    assert "within 60 seconds" in caught.value.detail
    _assert_frozen(native_tui_launch.AMBIGUOUS_PANE_RENDER_MISMATCH)


def test_a_rewritten_title_pane_with_no_session_label_freezes(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], fake_clock: _FakeClock
) -> None:
    # The picker hazard, rendered: a header with no Session line at all.
    rows = [row for row in _native_header_rows() if "Session:" not in row]
    pane = FakePane(observation=_observation(_REWRITTEN_ARGV), rendered=rows)

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(pinned_binary, pane, provider_version=PINNED_0310)

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PANE_RENDER_MISMATCH
    _assert_frozen(native_tui_launch.AMBIGUOUS_PANE_RENDER_MISMATCH)


def test_a_rewritten_title_pane_with_a_duplicated_session_label_freezes(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], fake_clock: _FakeClock
) -> None:
    rows = [
        *_native_header_rows(),
        "│  Session:   session_other                                           │",
    ]
    pane = FakePane(observation=_observation(_REWRITTEN_ARGV), rendered=rows)

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(pinned_binary, pane, provider_version=PINNED_0310)

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PANE_RENDER_MISMATCH
    _assert_frozen(native_tui_launch.AMBIGUOUS_PANE_RENDER_MISMATCH)


def test_a_rewritten_title_pane_whose_rendered_version_is_unproven_freezes(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], fake_clock: _FakeClock
) -> None:
    # A header that names a version whose behaviour was never read must not
    # inherit the proof -- even when its Session line would otherwise match.
    pane = FakePane(
        observation=_observation(_REWRITTEN_ARGV),
        rendered=_native_header_rows(version="0.30.0"),
    )

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(pinned_binary, pane, provider_version=PINNED_0310)

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PANE_RENDER_MISMATCH
    _assert_frozen(native_tui_launch.AMBIGUOUS_PANE_RENDER_MISMATCH)


def test_a_rewritten_title_pane_that_never_renders_the_header_freezes(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], monkeypatch: Any
) -> None:
    monkeypatch.setattr(native_tui_launch, "KIMI_RENDER_CONVERGENCE_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(native_tui_launch.time, "sleep", lambda _: None)
    pane = FakePane(observation=_observation(_REWRITTEN_ARGV), rendered=[])

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(pinned_binary, pane, provider_version=PINNED_0310)

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PANE_RENDER_MISMATCH
    assert "did not render" in caught.value.detail
    _assert_frozen(native_tui_launch.AMBIGUOUS_PANE_RENDER_MISMATCH)


def test_an_unreadable_render_of_a_rewritten_title_pane_freezes(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], monkeypatch: Any
) -> None:
    monkeypatch.setattr(native_tui_launch.time, "sleep", lambda _: None)
    pane = FakePane(
        observation=_observation(_REWRITTEN_ARGV),
        render_error=native_tui_launch.NativeLaunchUnavailable("capture-pane failed"),
    )

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(pinned_binary, pane, provider_version=PINNED_0310)

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PANE_UNREADABLE
    _assert_frozen(native_tui_launch.AMBIGUOUS_PANE_UNREADABLE)


def test_a_replaced_process_identity_while_waiting_for_the_header_freezes(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], monkeypatch: Any
) -> None:
    """The rendered proof is tied to the admitted pane by continuous identity.

    A capture that has not yet rendered is followed by a re-observation; if
    that re-observation names a *different* pid/start-marker the pane is no
    longer the admitted process, and proving the session off it would bind
    a stranger.  Frozen as an image mismatch, never published.
    """
    monkeypatch.setattr(native_tui_launch.time, "sleep", lambda _: None)

    class _DriftingPane(FakePane):
        def __init__(self) -> None:
            super().__init__(
                observation=_observation(_REWRITTEN_ARGV, pid=4321),
                rendered=[],  # not rendered yet on the first capture
            )
            self._observations = [
                _observation(_REWRITTEN_ARGV, pid=4322),  # different process
            ]

        def observe(self) -> Optional[Mapping[str, Any]]:
            self.observe_calls += 1
            if self.observe_calls == 1:
                return self.observation
            return self._observations.pop(0)

        def capture_render(self, pane_id: str) -> list[str]:
            self.render_calls += 1
            self.render_targets.append(pane_id)
            return []  # still not rendered, so the loop re-observes

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(pinned_binary, _DriftingPane(), provider_version=PINNED_0310)

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH
    _assert_frozen(native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH)


def test_a_process_replaced_just_before_an_already_matching_header_freezes(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], monkeypatch: Any
) -> None:
    """The matching capture itself must be fenced by a fresh observation.

    The hole this closes (COND-0312 P1): the rendered header that proves the
    session is read from a capture taken *after* the observation that seeded
    the identity, and a same-pane process replacement in that window would let
    the launch publish the stale ``pid``/``start_marker`` of a dead process
    while the session proof came from pixels the replacement re-rendered.  The
    argv path binds session and identity in one read; the rendered path splits
    them, so the match is only accepted after a re-observation confirms the
    identity unchanged.  Freeze, never publish a stale identity.
    """
    monkeypatch.setattr(native_tui_launch.time, "sleep", lambda _: None)

    class _ImmediateMatchReplacedPane(FakePane):
        def __init__(self) -> None:
            super().__init__(
                observation=_observation(_REWRITTEN_ARGV, pid=4321),
                rendered=_native_header_rows(),  # already matching on capture #1
            )

        def observe(self) -> Optional[Mapping[str, Any]]:
            self.observe_calls += 1
            # The initial seed (call 1, in start()) names pid 4321; the live
            # process is then replaced before the first capture, so the fence
            # re-observation (call 2) names a different process.
            return _observation(_REWRITTEN_ARGV, pid=4321 if self.observe_calls == 1 else 9999)

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(pinned_binary, _ImmediateMatchReplacedPane(), provider_version=PINNED_0310)

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH
    _assert_frozen(native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH)
    # The replacement was caught at the fence, not papered over: nothing was
    # published under the stale identity.
    assert native_attachment.get(PROVIDER, SESSION)["state"] == native_attachment.AMBIGUOUS


def test_a_rewritten_title_pane_in_the_wrong_directory_freezes(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], tmp_path: Any, monkeypatch: Any
) -> None:
    """The rendered session proof does not relax the directory check.

    The header proves which session the pane runs; the kernel cwd still has
    to prove it runs in the directory that session was minted in.  A pane
    rendering the right session from the wrong directory is still frozen.
    """
    monkeypatch.setattr(native_tui_launch.time, "sleep", lambda _: None)
    elsewhere = os.path.realpath(str(tmp_path))
    workdir = _canonical_workdir()
    assert elsewhere != workdir
    pane = FakePane(
        observation=_observation(_REWRITTEN_ARGV, cwd=elsewhere),
        rendered=_native_header_rows(),
    )

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(pinned_binary, pane, working_directory=workdir, provider_version=PINNED_0310)

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PANE_WORKDIR_MISMATCH
    _assert_frozen(native_tui_launch.AMBIGUOUS_PANE_WORKDIR_MISMATCH)


def test_reentry_over_a_rewritten_title_pane_reconciles_via_the_rendered_header(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], monkeypatch: Any
) -> None:
    """Re-entry over a ``starting`` row proves the same way a fresh launch does."""
    monkeypatch.setattr(native_tui_launch.time, "sleep", lambda _: None)
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

    pane = FakePane(
        observation=_observation(_REWRITTEN_ARGV),
        rendered=_native_header_rows(),
    )
    result = _start(pinned_binary, pane, provider_version=PINNED_0310)

    assert result["outcome"] == native_tui_launch.OUTCOME_RECONCILED
    assert pane.created == [], "re-entry must never start a second TUI"
    assert native_attachment.get(PROVIDER, SESSION)["state"] == native_attachment.ATTACHED


def test_a_non_rewriting_build_keeps_using_the_argv_proof_unchanged(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], monkeypatch: Any
) -> None:
    """A build that preserves its argv (0.30.0 and earlier) is unaffected.

    The rendered proof is opt-in per proven build; a non-rewriting build with
    provider_version set must still attach straight off the argv and never
    capture the render.
    """
    path, _ = pinned_binary
    monkeypatch.setattr(native_tui_launch.time, "sleep", lambda _: None)
    pane = FakePane(observation=_observation(_expected_argv(path)))

    result = _start(pinned_binary, pane, provider_version="0.30.0")

    assert result["outcome"] == native_tui_launch.OUTCOME_LAUNCHED
    assert pane.render_calls == 0
    # An argv launch records the argv proof channel by name.
    assert result["session_proof"] == native_tui_launch.SESSION_PROOF_ARGV


# --------------------------------------------------------------------------
# The rendered-proof window is the shared cold-start runway (COND-0314)
# --------------------------------------------------------------------------


def test_the_render_bound_is_the_native_cold_start_runway() -> None:
    """The two layers that wait on the same boot share one constant.

    COND-0314 was a contradiction between timeout layers: the v2 seam
    tolerates a 60-second cold start before a launched pane becomes
    input-ready, while the rendered-header proof that same boot paints
    *first* froze at 15 seconds.  Both waits now take the one runway
    constant, so the relationship cannot drift back into contradiction.
    """
    from cli_agent_orchestrator.services import managed_launch_v2

    assert native_tui_launch.NATIVE_COLD_START_RUNWAY_SECONDS == 60.0
    assert (
        native_tui_launch.KIMI_RENDER_CONVERGENCE_TIMEOUT_SECONDS
        == native_tui_launch.NATIVE_COLD_START_RUNWAY_SECONDS
    )
    assert (
        managed_launch_v2.NATIVE_PANE_READY_TIMEOUT_SECONDS
        == native_tui_launch.NATIVE_COLD_START_RUNWAY_SECONDS
    )


def test_a_slow_but_valid_exact_header_is_admitted_once_within_the_runway(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], fake_clock: _FakeClock
) -> None:
    """The live r4 failure shape (COND-0314): a legitimate slow boot, admitted.

    The installed 15 s bound froze the production reviewer before task
    delivery with ``pane_render_does_not_show_bound_session`` — and the same
    stable pane then rendered the exact bound session, model, version, and
    worktree, too late to be admitted.  A boot that paints the exact header
    inside the cold-start runway the launch already tolerates must converge
    and publish exactly once: no relaunch, no second pane, no replay.
    """
    # Twenty seconds in: past the old 15 s bound, well inside the runway.
    pane = _SlowBootPane(fake_clock, render_at=fake_clock.now + 20.0)

    result = _start(pinned_binary, pane, provider_version=PINNED_0310)

    assert result["outcome"] == native_tui_launch.OUTCOME_LAUNCHED
    assert result["session_proof"] == native_tui_launch.SESSION_PROOF_KIMI_RENDERED
    assert len(pane.created) == 1, "one launch, never a retry or a recreated pane"
    assert pane.render_calls >= 1
    assert all(target == "%7" for target in pane.render_targets)
    stored = native_attachment.get(PROVIDER, SESSION)
    assert stored is not None and stored["state"] == native_attachment.ATTACHED


def test_a_header_arriving_exactly_at_the_runway_boundary_is_admitted(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], fake_clock: _FakeClock
) -> None:
    """The boundary is inclusive: the deadline bounds observation, not painting.

    After the initial launch-time capture, a capture is taken only while the
    monotonic clock has not passed the deadline, so a header visible exactly
    when the runway is exhausted (``now == deadline``) is still observed and
    admitted.  A wake that resumes *after* it is not observed at all — see
    the oversleep cases below.
    """
    pane = _SlowBootPane(
        fake_clock,
        render_at=fake_clock.now + native_tui_launch.KIMI_RENDER_CONVERGENCE_TIMEOUT_SECONDS,
    )

    result = _start(pinned_binary, pane, provider_version=PINNED_0310)

    assert result["outcome"] == native_tui_launch.OUTCOME_LAUNCHED
    assert native_attachment.get(PROVIDER, SESSION)["state"] == native_attachment.ATTACHED


def test_a_header_arriving_one_poll_past_the_runway_freezes_with_the_exact_deadline(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], fake_clock: _FakeClock
) -> None:
    """Past the bound is past the bound: frozen, with truthful evidence.

    The freeze detail names the exact deadline the launch observed under,
    so a later reconciler knows how long the pane was watched.  The value
    pinned here is the shared cold-start runway (60 seconds).
    """
    pane = _SlowBootPane(
        fake_clock,
        render_at=fake_clock.now
        + native_tui_launch.KIMI_RENDER_CONVERGENCE_TIMEOUT_SECONDS
        + native_tui_launch.KIMI_RENDER_CONVERGENCE_POLL_SECONDS,
    )

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(pinned_binary, pane, provider_version=PINNED_0310)

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PANE_RENDER_MISMATCH
    assert "did not render" in caught.value.detail
    assert "within 60 seconds" in caught.value.detail
    assert len(pane.created) == 1, "a freeze never relaunches"
    _assert_frozen(native_tui_launch.AMBIGUOUS_PANE_RENDER_MISMATCH)


def test_a_header_that_never_paints_freezes_at_the_exact_runway_deadline(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], fake_clock: _FakeClock
) -> None:
    """One bounded window, then fail closed — never a retry or a replay.

    Distinct from the zero-timeout case above: this drives the real bound
    to its deadline on the fake clock and pins both the freeze and the
    exact runway named in its detail.
    """
    pane = _SlowBootPane(fake_clock, render_at=float("inf"))

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(pinned_binary, pane, provider_version=PINNED_0310)

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PANE_RENDER_MISMATCH
    assert "did not render" in caught.value.detail
    assert "within 60 seconds" in caught.value.detail
    _assert_frozen(native_tui_launch.AMBIGUOUS_PANE_RENDER_MISMATCH)


def test_a_header_painted_after_the_deadline_freezes_when_the_final_sleep_overshoots(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], monkeypatch: Any
) -> None:
    """The monotonic deadline — not the requested sleep — bounds observation.

    The deterministic counterexample from review (probe output
    ``ADMITTED_AFTER_DEADLINE capture=1060.2 deadline=1060.0 overrun=0.2``):
    every requested sleep resumes 0.25 s late under scheduler/host load, the
    header paints 0.05 s *after* the deadline, and the capture-before-check
    loop admitted it from a capture taken 0.2 s out of bounds.  The overshoot
    is real time, not an epsilon the contract absorbs.  After the initial
    capture the loop must reject ``now > deadline`` before reading the
    screen: no capture ever happens past the bound, and the launch freezes
    with the exact deadline named.
    """
    clock = _FakeClock(overshoot=0.25)
    monkeypatch.setattr(native_tui_launch.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(native_tui_launch.time, "sleep", clock.sleep)
    deadline = clock.now + native_tui_launch.KIMI_RENDER_CONVERGENCE_TIMEOUT_SECONDS
    pane = _SlowBootPane(clock, render_at=deadline + 0.05)

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(pinned_binary, pane, provider_version=PINNED_0310)

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PANE_RENDER_MISMATCH
    assert "did not render" in caught.value.detail
    assert "within 60 seconds" in caught.value.detail
    assert pane.capture_times, "the initial launch-time capture always happens"
    assert max(pane.capture_times) <= deadline, "no capture may read the screen out of bounds"
    _assert_frozen(native_tui_launch.AMBIGUOUS_PANE_RENDER_MISMATCH)


def test_a_header_visible_at_the_last_in_bound_capture_is_admitted_despite_oversleep(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], monkeypatch: Any
) -> None:
    """Oversleep tightens the window; it never invents a freeze.

    With the same 0.25 s late resumes, a header that *is* visible at the
    last capture inside the bound is a legitimate slow boot and is admitted
    exactly once: the deadline bounds observation, it does not make a
    healthy slow pane less admissible.
    """
    clock = _FakeClock(overshoot=0.25)
    monkeypatch.setattr(native_tui_launch.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(native_tui_launch.time, "sleep", clock.sleep)
    deadline = clock.now + native_tui_launch.KIMI_RENDER_CONVERGENCE_TIMEOUT_SECONDS
    # Visible at the last in-bound capture, before the overslept wake that
    # resumes past the deadline.
    pane = _SlowBootPane(clock, render_at=deadline - 0.2)

    result = _start(pinned_binary, pane, provider_version=PINNED_0310)

    assert result["outcome"] == native_tui_launch.OUTCOME_LAUNCHED
    assert len(pane.created) == 1, "one launch, never a retry"
    assert pane.capture_times and max(pane.capture_times) <= deadline
    assert native_attachment.get(PROVIDER, SESSION)["state"] == native_attachment.ATTACHED


def test_other_providers_never_consult_the_kimi_render_runway(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], monkeypatch: Any
) -> None:
    """The runway is Kimi's; no other provider inherits or even reads it.

    The rendered-proof window exists because one Kimi build erases the
    resumed session id from its kernel argv.  Every other provider proves
    from the argv, so the loop — and its timeout — must not exist for them:
    patched to zero here, a consult would freeze this launch instantly, and
    a capture would show up in ``render_calls``.
    """
    path, _ = pinned_binary
    codex_session = "3f6c5c4e-1a2b-4c3d-8e9f-0a1b2c3d4e5f"
    argv = native_tui_launch.codex_native_launch.build_resume_argv(
        session_id=codex_session, codex_binary=path, extra_args=None
    )
    monkeypatch.setattr(native_tui_launch, "KIMI_RENDER_CONVERGENCE_TIMEOUT_SECONDS", 0.0)
    pane = FakePane(observation=_observation(argv))

    result = _start(
        pinned_binary,
        pane,
        provider="codex",
        native_session_id=codex_session,
        provider_version=PINNED_0310,
    )

    assert result["outcome"] == native_tui_launch.OUTCOME_LAUNCHED
    assert result["session_proof"] == native_tui_launch.SESSION_PROOF_ARGV
    assert pane.render_calls == 0


def test_the_session_proof_vocabulary_is_closed_and_unknown_values_freeze(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    """Success evidence is typed over a closed vocabulary (COND-0312 P2-2).

    ``SESSION_PROOFS`` is exactly the argv channel and the rendered-header
    rule.  An unrecognised proof channel reaching publication must never be
    read as the argv proof: the pane is live by then, so it freezes the way
    every other unresolved publication does rather than silently falling
    through to argv behaviour.
    """
    assert native_tui_launch.SESSION_PROOFS == frozenset(
        {native_tui_launch.SESSION_PROOF_ARGV, native_tui_launch.SESSION_PROOF_KIMI_RENDERED}
    )
    assert (
        native_tui_launch.SESSION_PROOF_KIMI_RENDERED
        == native_tui_launch.kimi_native_launch.RULE_KIMI_NATIVE_HEADER
    )

    path, _ = pinned_binary
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
    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        native_tui_launch._publish(
            provider=PROVIDER,
            native_session_id=SESSION,
            terminal_id=TERMINAL,
            generation=GENERATION,
            working_directory=_canonical_workdir(),
            observation=_observation(_expected_argv(path)),
            session_proof="not-a-real-proof-channel",
        )
    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PUBLISH_FAILED
    _assert_frozen(native_tui_launch.AMBIGUOUS_PUBLISH_FAILED)


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

    def window_identity(
        self,
        session_name: str,
        window_name: str,
        *,
        deadline_monotonic: Optional[float] = None,
    ) -> Any:
        self.identity_deadline = deadline_monotonic
        return self.identity

    def window_exists(
        self,
        session_name: str,
        window_name: str,
        *,
        deadline_monotonic: Optional[float] = None,
    ) -> bool:
        self.exists_deadline = deadline_monotonic
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


# --------------------------------------------------------------------------
# The concrete tmux rendered-screen capture (COND-0312 P2-3)
# --------------------------------------------------------------------------


class _CompletedProcess:
    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_tmux_binary(monkeypatch: pytest.MonkeyPatch, path: str = "/usr/bin/tmux") -> None:
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.tmux.tmux_binary", lambda: path, raising=False
    )


def test_capture_render_targets_the_exact_pane_id_and_is_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``capture-pane -p -t %N`` -- the exact pane, no escapes, nothing sent.

    Pins the real subprocess wrapper: the argv is exactly the read-only
    capture against the fencing observation's pane id (never the session/window
    active pane), with no ``-e`` so the rows are the composited viewport, and
    ``capture-pane`` sends nothing to the pane.
    """
    pane = _pane(FakeBackend())
    captured: dict[str, Any] = {}

    def fake_run(argv: Any, **_kwargs: Any) -> Any:
        captured["argv"] = list(argv)
        return _CompletedProcess(stdout="Welcome\nSession:   x\n")

    monkeypatch.setattr(native_tui_launch.subprocess, "run", fake_run)
    _patch_tmux_binary(monkeypatch)

    rows = pane.capture_render("%47")

    assert captured["argv"] == ["/usr/bin/tmux", "capture-pane", "-p", "-t", "%47"]
    assert "capture-pane" == captured["argv"][1]
    assert "-e" not in captured["argv"]
    assert "send-keys" not in captured["argv"]
    assert rows == ["Welcome", "Session:   x"]


def test_capture_render_refuses_an_empty_pane_id(monkeypatch: pytest.MonkeyPatch) -> None:
    pane = _pane(FakeBackend())
    _patch_tmux_binary(monkeypatch)
    monkeypatch.setattr(
        native_tui_launch.subprocess, "run", lambda *a, **k: _CompletedProcess(stdout="x")
    )
    with pytest.raises(native_tui_launch.NativeLaunchInvalid, match="pane_id"):
        pane.capture_render("")


@pytest.mark.parametrize(
    ("failure", "matcher"),
    [
        # A capture that cannot complete in the bound is an unresolved
        # observation, never an empty header.
        ("timeout", "captured within the bound"),
        # A non-zero tmux exit (e.g. a pane that died) must raise, not return
        # empty -- an empty return would read as "no header" and freeze on a
        # lie rather than on an unreadable pane.
        ("nonzero", "could not capture pane %47"),
        # An OS-level failure to spawn tmux is likewise unreadable, not absent.
        ("oserror", "could not be captured"),
    ],
)
def test_capture_render_maps_every_read_failure_to_unavailable(
    monkeypatch: pytest.MonkeyPatch, failure: str, matcher: str
) -> None:
    pane = _pane(FakeBackend())
    _patch_tmux_binary(monkeypatch)

    def fake_run(argv: Any, **_kwargs: Any) -> Any:
        if failure == "timeout":
            raise native_tui_launch.subprocess.TimeoutExpired(argv, 1)
        if failure == "oserror":
            raise OSError("nope")
        return _CompletedProcess(returncode=1, stderr="can't find pane")

    monkeypatch.setattr(native_tui_launch.subprocess, "run", fake_run)
    with pytest.raises(native_tui_launch.NativeLaunchUnavailable, match=matcher):
        pane.capture_render("%47")


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


def test_tmux_transport_reads_exact_process_argv(
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
    monkeypatch.setattr(
        native_tui_launch,
        "_process_argv",
        lambda pid: ["/bin/kimi", "--settings", '{"hook": "two words"}', "--session", SESSION],
    )
    monkeypatch.setattr(native_tui_launch, "_process_cwd", lambda pid: "/private/tmp/w")
    observed = pane.observe()
    assert observed == {
        "pane_id": "%3",
        "pid": 777,
        "start_marker": "Thu Jul 24 10:00:00 2026",
        "argv": ["/bin/kimi", "--settings", '{"hook": "two words"}', "--session", SESSION],
        "cwd": "/private/tmp/w",
    }


def test_tmux_transport_threads_one_deadline_through_the_whole_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend(identity={"pane_id": "%3"}, exists=True)
    pane = _pane(backend)
    deadline = time.monotonic() + 1.0
    observed_deadlines: list[float] = []

    def _pane_pid(*, deadline_monotonic: float) -> int:
        observed_deadlines.append(deadline_monotonic)
        return 777

    def _process_field(pid: int, field: str, *, deadline_monotonic: float) -> str:
        observed_deadlines.append(deadline_monotonic)
        if field == "lstart=":
            return "Thu Jul 24 10:00:00 2026"
        return f"/bin/kimi --session {SESSION}"

    def _process_cwd(pid: int, *, deadline_monotonic: float) -> str:
        observed_deadlines.append(deadline_monotonic)
        return "/private/tmp/w"

    def _process_argv(pid: int, *, deadline_monotonic: float) -> list[str]:
        observed_deadlines.append(deadline_monotonic)
        return ["/bin/kimi", "--session", SESSION]

    monkeypatch.setattr(pane, "_pane_pid", _pane_pid)
    monkeypatch.setattr(native_tui_launch, "_process_field", _process_field)
    monkeypatch.setattr(native_tui_launch, "_process_argv", _process_argv)
    monkeypatch.setattr(native_tui_launch, "_process_cwd", _process_cwd)

    pane.observe(deadline_monotonic=deadline)

    assert backend.identity_deadline == deadline
    assert observed_deadlines == [deadline, deadline, deadline, deadline, deadline]


def test_tmux_transport_raises_when_the_pane_cwd_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable cwd is an unreadable pane, not a pane that passes.

    Returning the observation without a cwd would let the launch publish
    an attachment having never checked the one thing that distinguishes
    a resumable pane from one that is about to die on the directory the
    session was filed under.
    """
    pane = _pane(FakeBackend(identity={"pane_id": "%3"}, exists=True))
    monkeypatch.setattr(pane, "_pane_pid", lambda: 777)
    monkeypatch.setattr(
        native_tui_launch,
        "_process_field",
        lambda pid, field: (
            "Thu Jul 24 10:00:00 2026" if field == "lstart=" else f"/bin/kimi --session {SESSION}"
        ),
    )
    monkeypatch.setattr(
        native_tui_launch,
        "_process_argv",
        lambda pid: ["/bin/kimi", "--session", SESSION],
    )
    monkeypatch.setattr(native_tui_launch, "_process_cwd", lambda pid: None)
    with pytest.raises(native_tui_launch.NativeLaunchUnavailable):
        pane.observe()


def test_tmux_transport_refuses_an_unreadable_exact_argv(
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
    monkeypatch.setattr(native_tui_launch, "_process_argv", lambda pid: None)

    with pytest.raises(native_tui_launch.NativeLaunchUnavailable):
        pane.observe()


def test_darwin_procargs2_parser_preserves_argument_boundaries() -> None:
    import struct

    argv = ["/usr/bin/env", "python3", "/tmp/wrapper", "alpha", "two words"]
    raw = (
        struct.pack("i", len(argv))
        + b"/usr/bin/env\0"
        + b"\0\0"
        + b"\0".join(os.fsencode(argument) for argument in argv)
        + b"\0"
    )

    assert native_tui_launch._parse_darwin_procargs2(raw) == argv


def test_process_argv_preserves_whitespace_boundaries() -> None:
    import subprocess
    import sys
    import time

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "two words"])
    try:
        for _ in range(50):
            observed = native_tui_launch._process_argv(process.pid)
            if observed is not None:
                break
            time.sleep(0.05)
        assert observed is not None
        assert observed[-1] == "two words"
    finally:
        process.kill()
        process.wait()
    assert native_tui_launch._process_argv(process.pid) is None


def test_process_cwd_reads_the_real_directory_of_a_live_process() -> None:
    """The cwd probe must report a live process's resolved directory.

    Run against a real process because the whole check rests on the
    kernel disagreeing with the launcher's own record when they diverge;
    a stubbed probe could only ever agree with whatever it was told.
    """
    import subprocess
    import time

    alias = os.path.join(tempfile.gettempdir(), "cao-cwd-probe")
    os.makedirs(alias, exist_ok=True)
    canonical = os.path.realpath(alias)
    process = subprocess.Popen(["/bin/sh", "-c", "sleep 30"], cwd=alias)
    try:
        for _ in range(50):
            observed = native_tui_launch._process_cwd(process.pid)
            if observed is not None:
                break
            time.sleep(0.05)
        # Started through whatever name the caller used; reported as the
        # resolved one -- exactly the asymmetry the launch check exists
        # to catch, and the reason comparing raw strings would not do.
        assert observed == canonical
    finally:
        process.kill()
        process.wait()
    assert native_tui_launch._process_cwd(process.pid) is None


# --------------------------------------------------------------------------
# The directory the bound session was minted in
# --------------------------------------------------------------------------


@pytest.mark.parametrize("suffix", ["", "/nested"])
def test_a_non_canonical_working_directory_claims_nothing_and_starts_nothing(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], tmp_path: Any, suffix: str
) -> None:
    """Refused before ``declare``, so the session is left free.

    Ordering is the whole assertion.  Every other refusal in this module
    that happens after a claim leaves the session frozen and needing an
    explicit human release; this one happens before, so a caller can fix
    the path and simply try again.  Both a symlinked leaf and a symlinked
    interior component are covered, because a check that only compared
    the last component would pass the second.
    """
    real = tmp_path / "real"
    (real / "nested").mkdir(parents=True)
    (tmp_path / "link").symlink_to(real)
    through_link = f"{tmp_path / 'link'}{suffix}"

    pane = FakePane(observation=_observation([]))
    with pytest.raises(native_tui_launch.NativeLaunchInvalid) as refusal:
        _start(pinned_binary, pane, working_directory=through_link)

    assert os.path.realpath(through_link) in str(refusal.value)
    assert pane.created == []
    # Nothing claimed: not attached, not starting, not frozen.
    assert native_attachment.get(PROVIDER, SESSION) is None


def test_a_working_directory_that_does_not_exist_is_refused(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], tmp_path: Any
) -> None:
    """``realpath`` resolves a path that is not there, so existence is checked.

    A canonical-looking path to nothing would otherwise pass every
    string test and fail only when the pane tried to start in it.
    """
    pane = FakePane(observation=_observation([]))
    with pytest.raises(native_tui_launch.NativeLaunchInvalid):
        _start(pinned_binary, pane, working_directory=str(tmp_path / "absent"))
    assert pane.created == []
    assert native_attachment.get(PROVIDER, SESSION) is None


def test_a_pane_running_in_the_wrong_directory_freezes_before_attaching(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], tmp_path: Any
) -> None:
    """A correct argv in the wrong directory is still the wrong pane.

    The process resumes exactly the bound session id, so the argv check
    passes.  It is in a directory that session was never filed under, so
    the provider will refuse to open it and the pane will exit.  Frozen
    rather than published, because publishing would make the generation
    bindable and a task would then be typed at a dying process.
    """
    elsewhere = os.path.realpath(str(tmp_path))
    workdir = os.path.realpath(tempfile.gettempdir())
    assert elsewhere != workdir

    path, _digest = pinned_binary
    pane = FakePane(observation=_observation(_expected_argv(path), cwd=elsewhere))
    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as frozen:
        _start(pinned_binary, pane, working_directory=workdir)

    assert frozen.value.reason == native_tui_launch.AMBIGUOUS_PANE_WORKDIR_MISMATCH
    # Both directories named, so a reconciler is not left guessing which
    # of the two moved.
    assert elsewhere in str(frozen.value) and workdir in str(frozen.value)
    _assert_frozen(native_tui_launch.AMBIGUOUS_PANE_WORKDIR_MISMATCH)


def test_a_pane_reporting_the_bound_directory_by_another_name_still_attaches(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    """One physical directory under two names is not a mismatch.

    The comparison resolves what the process reports before comparing.
    Refusing here would freeze healthy sessions on any platform whose
    process table happens to answer with an unresolved path.
    """
    workdir = os.path.realpath(tempfile.gettempdir())
    alias = os.path.join(workdir, "..", os.path.basename(workdir))

    path, _digest = pinned_binary
    pane = FakePane(observation=_observation(_expected_argv(path), cwd=alias))
    result = _start(pinned_binary, pane, working_directory=workdir)

    assert result["outcome"] == native_tui_launch.OUTCOME_LAUNCHED
    assert native_attachment.get(PROVIDER, SESSION)["state"] == native_attachment.ATTACHED


def test_reentry_over_a_pane_in_the_wrong_directory_freezes_too(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], tmp_path: Any
) -> None:
    """The reconcile path checks the directory as well.

    Re-entry publishes an attachment for a pane it did not start, which
    makes it the path *most* in need of the check: the directory the pane
    is in was never observed by whoever created it.
    """
    path, _digest = pinned_binary
    workdir = os.path.realpath(tempfile.gettempdir())
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

    pane = FakePane(
        observation=_observation(_expected_argv(path), cwd=os.path.realpath(str(tmp_path)))
    )
    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous):
        _start(pinned_binary, pane, working_directory=workdir)

    # Observed, never relaunched: the freeze must not be reached by way
    # of starting a second process.
    assert pane.created == []
    _assert_frozen(native_tui_launch.AMBIGUOUS_PANE_WORKDIR_MISMATCH)
